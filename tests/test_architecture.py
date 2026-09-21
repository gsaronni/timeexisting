"""Structural invariants, enforced by walking the AST rather than grepping text.

A grep-based version of this test used to false-positive on a docstring in
`ui/app.py` that mentions `datetime.now()` in prose. Walking the AST instead
means only real `Import`, `Call` and string-literal nodes can trip these
checks: a docstring is a `Constant` node too, but it is excluded explicitly,
and a comment is never part of the tree in the first place.
"""

import ast
import re
from pathlib import Path

import pytest
from rich.color import ANSI_COLOR_NAMES
from rich.style import Style

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "timeexisting"
_CLOCK_MODULE = _SRC_ROOT / "domain" / "clock.py"
_THEME_MODULE = _SRC_ROOT / "ui" / "theme.py"

_ALL_MODULES = sorted(_SRC_ROOT.rglob("*.py"))
_DOMAIN_MODULES = sorted((_SRC_ROOT / "domain").rglob("*.py"))

_FORBIDDEN_CLOCK_CALLS = {
    "datetime.now",
    "datetime.datetime.now",
    "datetime.today",
    "datetime.datetime.today",
    "datetime.date.today",
    "time.time",
}

# Rich's own vocabulary, not a guess: every standard/extended colour name it
# recognises, plus the canonical (non-abbreviated) style modifier keywords.
_STYLE_MODIFIERS = {name for name, canonical in Style.STYLE_ATTRIBUTES.items() if name == canonical}
_STYLE_WORDS = set(ANSI_COLOR_NAMES) | _STYLE_MODIFIERS | {"on", "not", "default", "none"}
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _module_id(path: Path) -> str:
    return str(path.relative_to(_SRC_ROOT)).replace("\\", "/")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_ids(tree: ast.Module) -> set[int]:
    """Every `Constant` node that is a docstring: the first statement of a
    module, function or class body, when that statement is a bare string.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map each locally bound import name to its fully-qualified origin, so
    `from datetime import date` and `import datetime as dt` both resolve
    `date.today()` / `dt.datetime.now()` to their canonical dotted form.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                aliases[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = f"{node.module}.{alias.name}"
    return aliases


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _canonical(dotted: str | None, aliases: dict[str, str]) -> str | None:
    if dotted is None:
        return None
    root, *rest = dotted.split(".")
    return ".".join([aliases.get(root, root), *rest])


def _is_style_word(word: str) -> bool:
    return word in _STYLE_WORDS or bool(_HEX_COLOR.match(word))


def _looks_like_rich_style(value: str) -> bool:
    words = value.split()
    return bool(words) and all(_is_style_word(word) for word in words)


@pytest.mark.parametrize("path", _DOMAIN_MODULES, ids=_module_id)
def test_domain_never_imports_rich(path: Path) -> None:
    tree = _parse(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        offenders = [name for name in names if name == "rich" or name.startswith("rich.")]
        assert not offenders, f"{path}:{node.lineno} domain/ imports {offenders}"


@pytest.mark.parametrize("path", _ALL_MODULES, ids=_module_id)
def test_no_direct_clock_calls_outside_domain_clock(path: Path) -> None:
    if path == _CLOCK_MODULE:
        pytest.skip("domain/clock.py is where these calls belong")

    tree = _parse(path)
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        canonical = _canonical(_dotted(node.func), aliases)
        assert canonical not in _FORBIDDEN_CLOCK_CALLS, f"{path}:{node.lineno} calls {canonical} directly"


@pytest.mark.parametrize("path", _ALL_MODULES, ids=_module_id)
def test_no_rich_style_literals_outside_theme(path: Path) -> None:
    if path == _THEME_MODULE:
        pytest.skip("ui/theme.py is where these belong")

    tree = _parse(path)
    doc_ids = _docstring_ids(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in doc_ids:
            continue
        assert not _looks_like_rich_style(node.value), f"{path}:{node.lineno} hardcodes style {node.value!r}"
