from importlib.resources import files
from io import StringIO

import pytest
from rich.console import Console

from timeexisting.content.art import ART_DIR, MANIFEST, load_art, missing_art
from timeexisting.ui.art import art_block, centered_art_block
from timeexisting.ui.theme import load_theme

THEME = load_theme()


def test_every_manifest_entry_resolves_to_a_file():
    art_dir = files("timeexisting").joinpath(ART_DIR)
    for key, filename in MANIFEST.items():
        assert art_dir.joinpath(filename).is_file(), f"{key} -> {filename} does not exist"


def test_every_manifest_entry_loads_nonempty_text():
    for key in MANIFEST:
        assert load_art(key).strip()


def test_every_file_on_disk_is_in_the_manifest():
    art_dir = files("timeexisting").joinpath(ART_DIR)
    on_disk = {entry.name for entry in art_dir.iterdir() if entry.is_file()}
    assert on_disk == set(MANIFEST.values())


def test_missing_art_is_empty_for_a_clean_manifest():
    assert missing_art() == []


def _file_lines(key: str) -> list[str]:
    raw = files("timeexisting").joinpath(ART_DIR).joinpath(MANIFEST[key]).read_text(encoding="utf-8")
    return [line.rstrip() for line in raw.rstrip().split("\n")]


def _rendered_lines(renderable, width: int) -> list[str]:
    console = Console(file=StringIO(), width=width, color_system=None)
    console.print(renderable)
    return [line.rstrip() for line in console.file.getvalue().rstrip("\n").split("\n")]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


@pytest.mark.parametrize("key", MANIFEST)
def test_art_block_keeps_the_leading_whitespace_of_every_line(key):
    assert _rendered_lines(art_block(load_art(key), THEME.muted), width=200) == _file_lines(key)


@pytest.mark.parametrize("key", MANIFEST)
def test_centered_art_block_shifts_every_line_by_the_same_offset(key):
    expected = _file_lines(key)
    rendered = _rendered_lines(centered_art_block(load_art(key), THEME.muted), width=200)

    assert [line.lstrip() for line in rendered] == [line.lstrip() for line in expected]
    offsets = {_indent(got) - _indent(want) for got, want in zip(rendered, expected, strict=True) if want}
    assert len(offsets) == 1
