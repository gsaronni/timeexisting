
# timeexisting

Personal work-time tracker. Python >= 3.14, src layout, console script `te`.

## Before touching code

Read `docs/timeexisting-roadmap.org`. It is the specification. Section 3 is the architecture,
section 4 the schedule model, section 14 the phase order, section 17 the conventions.
Work only inside the phase you were asked for. Do not pull features forward.

## Non-negotiable

- `domain/` imports nothing from `rich` and never calls a clock. `now` is always a parameter.
- Every colour string lives in `ui/theme.py`. Every user-visible string goes through the content packs.
- Aware UTC for storage and arithmetic. `Europe/Copenhagen` only at the render boundary.
- Filenames under `assets/ascii_art/` are canonical. Code adapts to them.
- Python 3.14: lazy annotations are default, so no `from __future__ import annotations`.
- Full replacement files, not fragments. One concrete step at a time, then stop for review.
- `ruff check .` and `pytest` pass before every commit.
