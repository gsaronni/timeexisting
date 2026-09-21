# Phase 1 prompt for Claude Code

Paste this as the first message in a new Claude Code session at the repository root, with the venv active and `phase-0` tagged.

---

You are working on `timeexisting`. Read `CLAUDE.md`, `docs/log.org`, and `docs/timeexisting-roadmap.org` sections 3, 4, 15 and 19 before doing anything. Phase 0 is complete and tagged `phase-0`.

**Phase 1 scope: replace the legacy 09:00 to 18:00 schedule with the real one from roadmap section 4, add configuration loading, add day flags, and fix the demo so it is a carousel of states rather than an accelerated day. No ledger, no persistence, no presence detection; those are phases 2 and 3. The start time comes from a flag or a prompt in this phase, never from the ledger.**

Work in small, reviewable steps. After each step run `ruff check .` and `pytest`, show me the result, and stop for review. One concrete step at a time. Full replacement files, not fragments. Append a session entry to `docs/log.org` and update its phase status table as your final step.

## Step 1: demo carousel

`te --demo` currently runs a `ScaledClock` through one day. Replace it with a carousel: a fixed list of `(label, datetime)` scenarios rendered through `FixedClock`, stepping every 4 seconds, looping. The list covers, in order: pre-work, morning work, lunch, afternoon work, the last five minutes before the end, post-work, Saturday, Sunday, then one weekday at noon for each of the twelve months so every season and month remark appears. The footer shows the scenario label and `[DEMO]`. Keep `--speed` as a separate option for the scaled clock; the two are different tools.

## Step 2: architecture test by AST

`tests/test_architecture.py` currently greps and false-positives on a docstring in `ui/app.py`. Rewrite it with `ast`: walk every module under `src/timeexisting/`, and assert that no module under `domain/` imports `rich`; that no `Call` node resolves to `datetime.now`, `datetime.today`, `date.today` or `time.time` outside `domain/clock.py`; and that no string literal matching a Rich style token appears outside `ui/theme.py`. Docstrings and comments are not code and must not trigger.

## Step 3: configuration

- `config/models.py`: frozen dataclasses for every table in roadmap section 15, plus `[power]` from section 10 (loaded but unused until phase 5). Durations are `timedelta`, clock times are `datetime.time`, parsed by one helper each.
- `config/loader.py`: `tomllib` load of `config/defaults.toml` (packaged, ships the section 15 values) overlaid by the user's `config.toml` from the `platformdirs` config directory if it exists. Unknown keys are an error. A config error prints to stderr and exits with code 2 before any Rich surface is created.
- `paths.py`: the only module that knows where config, state, ledger and logs live. Use `platformdirs.user_config_dir("timeexisting")` and `user_state_dir("timeexisting")`.
- `te config path` prints the resolved config path; `te config init` writes the defaults there if absent.

## Step 4: schedule builder

`domain/segments.py`: `Phase` StrEnum (`PRE_WORK`, `WORKING`, `LUNCH`, `POST_WORK`, `OFF_DAY`, `OFF_HOURS`) and a frozen `Segment(start, end, phase, label, paid)`.

`domain/schedule.py`: `build_day(day: date, start: time, flags: frozenset[DayFlag], cfg: Config) -> DayPlan`. A `DayPlan` holds the ordered segments plus `start`, `nominal_end`, `target`, `deduction`. For a working day with no flags: pre-work until `start`, working until 11:00, lunch 11:00 to 11:30, working until `nominal_end = start + target + deduction`, post-work after. Weekends produce a single `OFF_DAY` segment. Night window from `[credit]` produces `OFF_HOURS` at both ends of every day.

`DayFlag` StrEnum: `NO_LUNCH` (deduction zero, `nominal_end` 30m earlier, no lunch segment), `HALF_DAY` (target halved), `SICK` and `OFFSITE` and `AFSPADSERING` and `FRI` (all produce an `OFF_DAY` plan with the flag recorded on it). Balance effects of flags are phase 4; here they only shape the day.

Delete the legacy break windows and everything in `domain/phases.py` that phase 0 kept as a stopgap. There are no scheduled breaks in the new model.

## Step 5: resolver

`domain/resolver.py`: `resolve(now: datetime, plan: DayPlan) -> Resolved`. `Resolved` carries the active segment, time elapsed in it, time remaining in it, the next segment, expected credit accrued so far (scheduled working time elapsed, since there is no ledger yet), progress fraction toward `target`, and whether `now` is past `nominal_end`. Pure: no clock, no I/O, no Rich.

## Step 6: start-time resolution

In `cli.py`: `--start HH:MM` sets the start. Without it, on a working day, prompt once before the Rich surface opens: "Start time [09:00]:" accepting `HH:MM` or empty for the default from `[credit].default_start`. Validate against the flex band 08:00 to 09:00 from config with a warning, not a refusal, outside it. `--flag` may be given more than once and maps to `DayFlag`. On a weekend or when `--at` falls on one, no prompt.

## Step 7: panels

`ui/panels/day.py` renders from `Resolved` only, by `match` on phase:

- `PRE_WORK`: countdown to `start`, art, existing message pool.
- `WORKING`: progress toward `target` as a percentage and a Rich progress bar, `hh:mm:ss` remaining to `nominal_end`, `start` and `nominal_end` shown, next segment named, seconds visible.
- `LUNCH`: the lunch panel with minutes remaining. New pool key `lunch`.
- `POST_WORK`: as today, with `nominal_end` in place of the fixed 18:00.
- `OFF_DAY`: the existing weekend panel; for a flagged off day use the flag name to pick a pool key (`holiday`, `sick`, `afspadsering`, `fri`, `offsite`). Populate `sick` and `afspadsering` with two lines each in the grimdark pack; the rest may fall back to the weekend pool for now.
- `OFF_HOURS`: a dim panel, pool key `off_hours`, two lines.

`ui/panels/week.py`: replace the day-count percentage with expected progress through the 37h week, computed from the schedule: completed working days times target plus today's scheduled elapsed working time, over the weekly target. Monday at 10:00 is no longer 0.0%.

## Step 8: tests

- `tests/test_schedule.py`: `build_day` at starts 08:00, 08:30, 09:00 asserts the table in roadmap section 4 exactly; every flag produces the documented plan; segments are contiguous and non-overlapping.
- `tests/test_resolver.py`: for each of the three starts, every minute of the day resolves to exactly one phase, elapsed plus remaining equals the segment length, expected credit is monotonic and reaches `target` at `nominal_end` and never exceeds it, and no value is negative.
- `tests/test_config.py`: defaults load, an unknown key fails, a malformed duration fails, an overlay overrides one value and leaves the rest.
- `tests/test_demo.py`: the carousel list contains every phase and every month.

## Conventions

As in `CLAUDE.md`. Python 3.14. Aware UTC in the model; `Europe/Copenhagen` only in `ui/`. `domain/` imports nothing from `rich` and never calls a clock. Every colour in `ui/theme.py`. Every new user-visible string in the grimdark pack, not in code. Do not hard-wrap prose in docstrings, comments or documents.

## Done when

`te --at "2026-09-21 11:05" --start 08:30` shows lunch with 25 minutes remaining. `te --at "2026-09-21 16:23" --start 08:30` shows working with one minute remaining. `te --at "2026-09-21 16:24" --start 08:30` shows post-work. `te --at "2026-09-21 16:24" --start 08:30 --flag no-lunch` shows post-work with `nominal_end` 15:54. `te --demo` cycles every state and every month. All tests green. Tag `phase-1`.
