# Phase 0 prompt for Claude Code

Paste this as the first message in a Claude Code session opened at the repository root
(`C:\Users\you\proj\te`), with the venv active.

---

You are working on `timeexisting`, a personal work-time tracker. Read `CLAUDE.md`, then
`docs/timeexisting-roadmap.org` sections 2, 3, 14, 16 and 19 before doing anything.

The original program is preserved intact at `src/timeexisting/_legacy.py` and is excluded from
linting. It runs today with `python src/timeexisting/_legacy.py` and renders, but with every
ASCII art panel blank and with the defects listed below.

**Phase 0 scope: migrate `_legacy.py` into the package structure, fix every known defect, and put
the content and theme plumbing in place. Do not add features. Do not change the schedule model,
which is still the legacy 09:00 to 18:00 day with fixed breaks; phase 1 replaces it. Behaviour
after this phase matches the original program's intent, only correctly.**

Work in small, reviewable steps. After each step run `ruff check .` and `pytest`, show me the
result, and stop for review before continuing. One concrete step at a time. Full replacement
files, not fragments.

## Target structure

Distribute `_legacy.py` across the packages that already exist:

- `content/art.py`: manifest of art files and a `functools.cache`d loader using
  `importlib.resources.files("timeexisting").joinpath("assets/ascii_art")`. Never `__file__`
  path arithmetic. A startup validation reports any manifest entry with no file on disk.
- `content/phrases.py`: the pack loader. Reads `content/voices/<voice>/<locale>.toml`, falls back
  voice+en then grimdark+en, never raises on a missing key. Selection is a shuffled deque per key,
  refilled on exhaustion. Move every message table from `_legacy.py` into
  `content/voices/grimdark/en.toml` as the first real pack. The other pack files stay placeholders.
- `ui/theme.py`: every colour string in the program. No colour literal may remain anywhere else.
  Load from `content/palettes/grimdark.toml`; the other palettes stay placeholders.
- `ui/panels/`: one module per panel: `header.py`, `year.py`, `week.py`, `day.py`.
- `ui/layout.py`: layout built once.
- `ui/app.py`: the `Live` loop and clean shutdown.
- `domain/clock.py`: a `Clock` protocol with `SystemClock`, `FixedClock` and `ScaledClock`.
- `domain/phases.py`: an explicit `Phase` StrEnum and a pure resolver from `(now) -> Phase`
  for the legacy day shape. This is a stopgap that phase 1 replaces, but it is what the tests
  target and what kills defect 1.
- `cli.py`: argparse surface only. `te` runs the viewer; `te --at "2026-09-17 07:30"` injects a
  fixed clock; `te --demo` cycles states on a `ScaledClock`. `__main__.py` delegates to it.

Delete `_legacy.py` and its ruff exclusion only when nothing references it.

## Defects to fix

1. **Indentation defect in `make_time_panel`.** `if COFFEE_ART and self.showcase_mode:` sits one
   level out from its enclosing `if now.hour < START_HOUR:`. The pre-work `return` is nested inside
   the coffee check and the following `elif now.hour >= END_HOUR:` binds to the coffee `if`. Outside
   showcase mode a pre-work run falls through to the work-hours branch with `now < start_time`,
   producing negative elapsed time, a negative percentage, and a negative message index that wraps
   silently. Replace the chain with a `match` on `Phase`.
2. **Asset path.** The loader resolves `<script dir>/ascii_art/`; the files are at
   `assets/ascii_art/`. Fixed by `importlib.resources` as above.
3. **Asset filename drift.** `coffee.txt` is requested; the file is `coffe.txt`. Filenames on disk
   are canonical and are not renamed. Correct the manifest. Likewise the constants that load
   unrelated files (bouldering loads `surf.txt`, hiking loads `fencing.txt`, coding loads
   `chess.txt`): rename the entries to match the files and rebuild the weekend activity table
   around the real asset set.
4. **Bare `except:`** swallows `KeyboardInterrupt` and `SystemExit`. Narrow to `OSError`, log.
5. **`ShowcaseState` raises and mutates.** `datetime.now().replace(month=...)` raises on days 29
   to 31 for short months; `get_simulated_time` advances state inside a getter; `make_layout` calls
   it for the side effect. Delete the class. Demo mode runs on `ScaledClock` through the same
   renderer as production. One code path.
6. **Layout rebuilt every frame.** Build once, update panel contents only.
7. **Incoherent loop timing.** `refresh_per_second=1` against `time.sleep(0.1)`. Drive one cadence
   from the clock.
8. **Dead code.** `SLEEP_HOUR`, `WAKE_HOUR`, presleep and shutdown constants, unused imports
   (`Progress`, `BarColumn`, `TextColumn`, `Align`, `Columns`, `sys`).
9. **Two day indexings.** `make_week_panel` mixes Monday-zero and Sunday-zero. Standardise on
   `date.weekday()` and key the pack tables to it.
10. **`is_break_time`.** Delete it. Reimplement the legacy break windows as data inside the
    stopgap resolver, in minutes since midnight, so phase 1 can remove them cleanly.
11. **Crash on double Ctrl+C.** The `KeyboardInterrupt` handler calls `time.sleep(2)`, and a second
    Ctrl+C interrupts the handler. Stop `Live` first, print the exit line, never sleep on exit.
12. **Art gated behind showcase mode.** Every body panel renders art only when `showcase_mode` is
    true. Remove the gate. Art renders whenever the file exists; the panel sizes to content.
13. **Naive datetimes.** Every `datetime.now()` becomes an aware UTC call through `Clock`,
    converted to `Europe/Copenhagen` via `zoneinfo` at the render boundary only.

## Conventions

- Python 3.14. No `from __future__ import annotations`.
- `pathlib`, `tomllib`, `zoneinfo`, `enum.StrEnum`, `@dataclass(frozen=True, slots=True)`,
  `match`, `divmod`. Full type annotations on every public function.
- No module-level side effects. Nothing is read from disk at import time.
- `domain/` imports nothing from `rich` and never calls a clock.
- Do not hard-wrap prose in docstrings or comments at a fixed column.

## Tests

- `tests/test_phases.py`: the stopgap resolver at one-minute resolution across a full day at
  three dates (a weekday, a Saturday, a Sunday), asserting no negative duration, no negative
  percentage, no out-of-range message index, and exactly one phase per minute. This is what
  proves defect 1 is gone and cannot return.
- `tests/test_art.py`: every manifest entry resolves to a file, and every file in
  `assets/ascii_art/` is in the manifest.
- `tests/test_phrases.py`: fallback chain resolves for a missing locale, a missing voice, and a
  missing key; the deque never repeats within one cycle.
