# Phase 2 prompt for Claude Code

Paste this as the first message in a new Claude Code session at the repository root, with the venv active and `phase-1` tagged.

---

You are working on `timeexisting`. Read `CLAUDE.md`, `docs/logs.org`, and `docs/timeexisting-roadmap.org` sections 3, 5 and 19 before doing anything. Phase 1 is complete and tagged `phase-1`.

**Phase 2 scope: persistence. The headless collector, the append-only ledger, replay into a timeline, the single-instance lockfile, diagnostic logging, clean shutdown on Windows logoff, and the Startup shortcut. The collector records its own presence only: `collector_start`, `heartbeat`, `collector_stop`. No lock or sleep detection, no absence classification, no recovery logic, no balances; those are phases 3 and 4. The viewer does not read the ledger for display in this phase beyond a collector status line.**

Work in small, reviewable steps. After each step run `ruff format .`, `ruff check .` and `pytest`, show me the result, and stop for review. One concrete step at a time. Full replacement files, not fragments. Append a session entry to `docs/logs.org` and update its phase status table as your final step.

## Two corrections to the roadmap, apply them to the spec as well

1. **Section 3 says a console control handler catches logoff.** Under `pythonw.exe` there is no console, so `SetConsoleCtrlHandler` never fires. The detached collector needs a hidden message-only window receiving `WM_QUERYENDSESSION` and `WM_ENDSESSION`. Keep the console handler for when `te collect` runs in a terminal. Rewrite the collector lifecycle bullets in section 3 to say this.
2. **Section 5's event schema has no id**, but the phase 7 editor supersedes events by id. Add an `id` field, generated with `uuid.uuid7()` (Python 3.14, time-ordered), as a lowercase hex string. Update the schema table and the JSON example in section 5.

## Step 0: housekeeping

- In `.pre-commit-config.yaml`, rename hook `id: ruff` to `id: ruff-check` (the legacy alias warning).
- If `CLAUDE.md` does not already require `ruff format .` before stopping for review, add it to the non-negotiable list.
- Commit nothing yourself; I commit between steps.

## Step 1: paths, environment overrides, diagnostic logging

- `paths.py`: honour `TIMEEXISTING_CONFIG_DIR` and `TIMEEXISTING_STATE_DIR` when set, overriding `platformdirs`. Add `ledger_dir()`, `log_dir()`, `lock_file(role)`, `stop_file()`, all under the state directory. Directories are created on first use, never at import.
- Switch the autouse test isolation in `tests/conftest.py` to set both environment variables to `tmp_path` subdirectories, so every test, including subprocess-based ones, is cut off from real directories.
- `logging_setup.py`: `configure(role: str)` installs a `RotatingFileHandler` at `log_dir()/tracker.log`, 1 MB, 3 backups, format with timestamp, role, level, logger name. Never stdout, never stderr, because a `Live` display corrupts on interleaved writes. `te` and `te collect` both call it at startup with their role.
- Document the two environment variables in `README.org` under a "Multiple machines" heading: the repository carries the program, not the user's data; set the variables to a folder you sync yourself if you want one config and one ledger across devices.

## Step 2: event schema

`ledger/events.py`:

- `EventType` StrEnum with every value from roadmap section 5. Only three are written in this phase.
- `Source` StrEnum and `Confidence` StrEnum per section 5.
- `Event`, frozen with slots: `v`, `id`, `ts` (aware UTC `datetime`), `host`, `profile`, `event`, `source`, `confidence`, plus an optional `data: Mapping[str, str]` for event-specific fields added in later phases.
- `Event.new(...)` constructor that fills `v=1`, `id=uuid.uuid7().hex`, and requires `ts` as a parameter. It never calls a clock.
- `to_json(event) -> str` producing one line with no trailing newline, keys in a fixed order, `ts` as ISO 8601 with offset. `from_json(line) -> Event` raising `LedgerFormatError` on anything malformed, including a naive timestamp or an unknown schema version.
- Profile resolution lives here too: `profile_for(host, cfg)` looks up `cfg.profiles.hosts`, falling back to `cfg.profiles.default`.

## Step 3: store

`ledger/store.py`:

- `shard_path(host)` is `ledger_dir()/<host>.jsonl`. Hostname from `platform.node()`, sanitised to a safe filename.
- `append(event)`: open with `O_APPEND`, write one line plus `\n`, flush, `os.fsync`, close. One call per event. No buffering across calls.
- `read_shard(path) -> ReadResult` returning the valid events and a count of malformed lines. Malformed lines are skipped and logged at WARNING with line number, never fatal.
- `read_all() -> ReadResult` merging every shard in `ledger_dir()`, sorted by `ts` then `id`.

## Step 4: replay

`ledger/replay.py`:

- `Timeline`, frozen: the ordered events, a tuple of `Presence(start, end, host)` intervals, and a tuple of `Gap(start, end, host, opened_by, closed_by)` where `opened_by` and `closed_by` are the event types at each edge.
- `replay(events, heartbeat: timedelta) -> Timeline`: per host, consecutive heartbeats no more than `2 × heartbeat` apart merge into one presence interval; a longer spacing produces a gap. `collector_start` opens an interval, `collector_stop` closes one. A gap whose opening edge is a `heartbeat` rather than a `collector_stop` is an unclean stop; record that fact on the gap, do not classify it further.
- Heartbeats are not retained in the `Timeline` events tuple; they have been collapsed.
- Pure: `domain`-style rules apply to this module. No clock, no I/O, no Rich. Extend the architecture test to cover `ledger/replay.py` and `ledger/events.py`.

## Step 5: lockfile

`collector/lockfile.py`:

- `acquire(role) -> Lock` creates `lock_file(role)` atomically with `O_CREAT | O_EXCL`, writing JSON with `pid`, `host`, `role`, `create_time` from `psutil.Process().create_time()`, and `schema`.
- If the file exists, it is live only if `psutil.pid_exists(pid)` and that process's `create_time` matches the recorded value within one second. A recycled PID fails the second check and is stale. A stale lock is removed and acquisition retried once.
- A live lock raises `AlreadyRunning` carrying the recorded pid and start time.
- `release(lock)` removes the file only if it still holds our pid.
- `status(role) -> LockStatus | None` for read-only inspection.
- The refusal message comes from the grimdark pack under a new key `instance.already_running`. Seed it with "An instance already endures. There is only one of you, and it is already suffering." plus one more line in the same register.

## Step 6: collector loop

`collector/daemon.py`:

- `run_collector(clock, sleep, cfg, *, max_ticks=None)`: acquire the lock, write `collector_start`, then loop: write `heartbeat` every `cfg.collector.heartbeat`, check `stop_file()` every tick, exit when it exists or `max_ticks` is reached. On any exit, write `collector_stop` with `data={"reason": ...}`, release the lock, delete the stop file.
- `clock` and `sleep` are injected so tests run hundreds of ticks instantly. Heartbeat spacing is measured against the clock, not by counting sleeps.
- Day state is never captured at startup. Nothing in the loop knows what day it is; it writes timestamps. This is what makes midnight a non-event.
- `SIGINT` (and `SIGBREAK` on Windows, `SIGTERM` elsewhere) set a stop flag checked each tick, so Ctrl+C in a terminal produces a clean `collector_stop` with reason `signal`.
- `cli.py`: `te collect` runs the loop in the current process, in the foreground. Detaching is the caller's job, never the collector's. `te collect status` prints pid, host and start time, or "not running". `te collect stop` creates the stop file and waits up to two poll intervals for the lock to disappear, reporting either outcome.

## Step 7: Windows session end

`collector/session_win32.py`, imported only on Windows:

- When a console is attached, register a handler via `SetConsoleCtrlHandler` for `CTRL_CLOSE_EVENT`, `CTRL_LOGOFF_EVENT` and `CTRL_SHUTDOWN_EVENT`.
- Always, start a daemon thread that creates a message-only window with `pywin32` (`win32gui.CreateWindowEx` with `HWND_MESSAGE` as parent) and handles `WM_QUERYENDSESSION` (return `True`) and `WM_ENDSESSION` (when `wParam` is true, write `collector_stop` with reason `session_end`, fsync, release the lock).
- Both paths call one shutdown function that is idempotent, since logoff can deliver both.
- Unit-test the shutdown function directly and the window procedure by calling it with synthetic messages. Live verification is manual and is listed under "Done when". Tell me if `pywin32` cannot create the window under `pythonw`; this is the step most likely to need a second approach.

## Step 8: viewer spawns and reports the collector

- On `te` startup, after config load and before the Rich surface: `status("collector")`. If none, spawn `pythonw.exe -m timeexisting collect` on Windows with `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, or `python -m timeexisting collect` with `start_new_session=True` elsewhere, stdout and stderr to `DEVNULL`. Locate `pythonw.exe` beside `sys.executable`. Wait up to three seconds for the lock to appear; if it does not, continue without it and show that in the status line.
- `te --no-collector` skips the spawn, for `--at` and `--demo` sessions. `--at` and `--demo` imply it.
- The footer gains one line: collector alive since `HH:MM`, or not running. Text from the pack, colour from the theme.

## Step 9: startup shortcut

`scripts/install-startup.ps1`, PowerShell 7 only:

- Creates `timeexisting collector.lnk` in `[Environment]::GetFolderPath('Startup')` via `WScript.Shell`, target the venv's `pythonw.exe` (resolved from the script's repository location, `.venv\Scripts\pythonw.exe`), arguments `-m timeexisting collect`, working directory the repository root.
- `-Uninstall` removes it. `-WhatIf` supported. Refuses with a clear message if the venv or `pythonw.exe` is missing.
- Referenced from `README.org` with one line.

## Step 10: tests

- `tests/test_events.py`: round trip for every field, fixed key order, naive timestamp rejected, unknown version rejected, ids are unique and sort in creation order.
- `tests/test_store.py`: append then read, fsync called once per append (monkeypatch `os.fsync`), malformed lines skipped and counted, two shards merged in timestamp order.
- `tests/test_replay.py`: continuous heartbeats give one interval; a spacing over twice the heartbeat gives a gap; unclean stop recorded on the gap; two hosts kept separate; a run crossing local midnight gives one continuous interval; a run across the night of 24 to 25 October 2026 (fall-back, local 03:00 becomes 02:00) gives strictly increasing UTC timestamps and no negative interval.
- `tests/test_lockfile.py`: acquire, second acquire raises `AlreadyRunning`, stale pid reclaimed, recycled pid with mismatched `create_time` treated as stale, release does not remove another process's lock.
- `tests/test_daemon.py`: N ticks on a fixed clock produce `collector_start`, the expected heartbeats, and `collector_stop`; stop file ends the loop within one tick; signal flag ends it with reason `signal`.
- Ledger fixtures, if any, go under `tests/fixtures/` as `.jsonl`; the `.gitignore` negation allows them.

## Conventions

As in `CLAUDE.md`. Python 3.14. Aware UTC everywhere in the ledger. `ledger/events.py` and `ledger/replay.py` never call a clock. Every user-visible string in the grimdark pack, every colour in `ui/theme.py`. Do not hard-wrap prose in docstrings, comments or documents.

## Done when

- `te collect` in a terminal writes `collector_start` and a heartbeat every 30 seconds to `%LOCALAPPDATA%\timeexisting\ledger\<host>.jsonl`; Ctrl+C writes `collector_stop` with reason `signal`.
- A second `te collect` in another terminal refuses with the pack message and exits non-zero.
- `te collect stop` stops a running collector within one poll and it writes `collector_stop` with reason `stop_file`.
- Ending the collector from Task Manager leaves a stale lock that the next `te collect` reclaims silently.
- `te` with no collector running spawns one; `te collect status` then shows it; the viewer footer shows it.
- `scripts\install-startup.ps1` creates the shortcut; after signing out and back in, `te collect status` shows a collector started at login, and the ledger shows a `collector_stop` with reason `session_end` at the sign-out time.
- `ruff format --check .`, `ruff check .`, `pytest` all clean. Tag `phase-2`.
