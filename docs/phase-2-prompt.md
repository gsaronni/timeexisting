# Phase 2 prompt for Claude Code

Paste this as the first message in a new Claude Code session at the repository root, with the venv active and `phase-1` tagged.

---

You are working on `timeexisting`. Read `CLAUDE.md`, `docs/logs.org`, and `docs/timeexisting-roadmap.org` sections 3, 5 and 19 before doing anything. Phase 1 is complete and tagged `phase-1`.

**Phase 2 scope: persistence. The headless collector, the append-only ledger, replay into a timeline, the single-instance lockfile, diagnostic logging, clean shutdown on Windows logoff, and the Startup shortcut. The collector records its own presence only, as transitions: `collector_start` and `collector_stop`. Liveness between them is a per-tick checkpoint file, never a ledger line. The clock-jump suspend inference in step 6 is the only sleep signal; no lock detection, no OS-level sleep detection, no absence classification, no recovery beyond the checkpoint recovery in step 6, no balances; those are phases 3 and 4. The viewer does not read the ledger for display in this phase beyond a collector status line.**

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

First, retire the heartbeat vocabulary:

- Remove `EventType.HEARTBEAT` from `ledger/events.py`.
- Rename `Source.HEARTBEAT` to `Source.COLLECTOR`, value `collector`, per section 5.
- Rename the config key `collector.heartbeat` to `collector.tick` in the config model, the loader, `defaults.toml` and the tests.
- Update the helper in `tests/test_store.py` to build a `collector_start` with source `collector`, and the enum value sets in `tests/test_events.py`.

Then `ledger/replay.py`:

- `Timeline`, frozen: every event in order (nothing is collapsed, since the ledger holds transitions only), a tuple of `Presence` intervals and a tuple of `Gap`s.
- `Presence(start, end, host, end_inferred)`: `end` is the `ts` of the closing `collector_stop`, or `None` when there is none. `end_inferred` is true when that stop has confidence `inferred`.
- `Gap(start, end, host, start_inferred, end_inferred, reason)`: the span from a `collector_stop` to the same host's next `collector_start`. `start_inferred` and `end_inferred` carry the confidence of each edge; `reason` is the opening stop's `data["reason"]` (`unclean`, `suspended`, `signal`, `stop_file`, `session_end`, ...), recorded, not classified.
- `replay(events) -> Timeline`: sorts by `(ts, id)` itself rather than trusting input order. Per host, each `collector_start` pairs with the next `collector_stop` into a presence interval, and that stop plus the next start bound a gap.
- A `collector_start` followed by another `collector_start` with no stop between is an unclosed interval: `end=None`, kept, not guessed. No gap is produced after it, since where it ended is unknown. A host's final start with no stop is likewise `end=None`: the running collector, or one not yet recovered.
- A `collector_stop` with no open interval produces nothing and is kept in the events tuple. Events of any other type are kept and ignored for pairing.
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

- `run_collector(clock, sleep, cfg, *, max_ticks=None)`: acquire the lock, recover a leftover checkpoint (below), write `collector_start` with `data={"reason": "launch"}`, then tick every `cfg.collector.tick`. Each tick writes the checkpoint, checks for a clock jump, and checks `stop_file()` and the signal flag; exit when either is set or `max_ticks` is reached. On any exit, write `collector_stop` with `data={"reason": ...}` (`stop_file`, `signal`, `max_ticks`), delete the checkpoint, release the lock, delete the stop file.
- No ledger line is written per tick. The ledger sees `collector_start` once at launch and `collector_stop` once at exit, plus the suspend pair below.
- Checkpoint: `paths.checkpoint_file(host)` is `state_dir()/checkpoint-<host>.json`, holding `{"ts": <ISO 8601 UTC>, "pid": <int>}`. Each tick writes `checkpoint-<host>.json.tmp` beside it, fsyncs, and `os.replace`s it over the checkpoint. A `PermissionError` on replace (Windows, while something else holds the file) is logged at WARNING and retried on the next tick; the loop carries on.
- Recovery, after acquiring the lock and before `collector_start`: if a checkpoint exists, the previous run may have ended uncleanly. First read the host's last ledger event: if it is already a `collector_stop` with `ts` at or after the checkpoint's `ts`, the previous run ended cleanly and only the checkpoint's deletion was lost, so delete the checkpoint and write nothing. Otherwise write `collector_stop` at the checkpoint's `ts`, confidence `inferred`, `data={"reason": "unclean"}`, then delete the checkpoint. A checkpoint that cannot be parsed is logged and deleted, and no stop is written for it: nothing is guessed.
- Suspend: if the clock shows more than `2 × cfg.collector.tick` since the previous tick, the machine was suspended. Write `collector_stop` at the previous tick's `ts` (confidence `inferred`, reason `suspended`), then `collector_start` at now (confidence `observed`, reason `resumed`). A clock that moved backwards is logged and is not a suspend.
- `clock` and `sleep` are injected so tests run hundreds of ticks instantly. Tick spacing and the jump check are measured against the clock, not by counting sleeps.
- Day state is never captured at startup. Nothing in the loop knows what day it is; it writes timestamps. This is what makes midnight a non-event.
- `SIGINT` (and `SIGBREAK` on Windows, `SIGTERM` elsewhere) set a stop flag checked each tick, so Ctrl+C in a terminal produces a clean `collector_stop` with reason `signal`.
- `cli.py`: `te collect` runs the loop in the current process, in the foreground. Detaching is the caller's job, never the collector's. `te collect status` prints pid, host and start time, or "not running". `te collect stop` creates the stop file and waits up to two poll intervals for the lock to disappear, reporting either outcome.

## Step 7: Windows session end

`collector/session_win32.py`, imported only on Windows:

- When a console is attached, register a handler via `SetConsoleCtrlHandler` for `CTRL_CLOSE_EVENT`, `CTRL_LOGOFF_EVENT` and `CTRL_SHUTDOWN_EVENT`.
- Always, start a daemon thread that creates a message-only window with `pywin32` (`win32gui.CreateWindowEx` with `HWND_MESSAGE` as parent) and handles `WM_QUERYENDSESSION` (return `True`) and `WM_ENDSESSION` (when `wParam` is true, write `collector_stop` with reason `session_end`, fsync, delete the checkpoint, release the lock). Session end is a clean stop; a checkpoint left behind here would make the next login write a spurious `unclean` stop.
- Both paths call one shutdown function that is idempotent, since logoff can deliver both.
- Unit-test the shutdown function directly and the window procedure by calling it with synthetic messages. Live verification is manual and is listed under "Done when". Tell me if `pywin32` cannot create the window under `pythonw`; this is the step most likely to need a second approach.

## Step 8: viewer spawns and reports the collector

- On `te` startup, after config load and before the Rich surface: `status("collector")`. If none, spawn `pythonw.exe -m timeexisting collect` on Windows with `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, or `python -m timeexisting collect` with `start_new_session=True` elsewhere, stdout and stderr to `DEVNULL`. Locate `pythonw.exe` beside `sys.executable`. Wait up to three seconds for the lock to appear; if it does not, continue without it and show that in the status line.
- The viewer identifies the collector through `status("collector")`, never through the `Popen` pid: a venv's `pythonw.exe` is a launcher, and the real collector is its child process with a different pid.
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
- `tests/test_events.py`: `heartbeat` is no longer an `EventType`, and a ledger line carrying it is rejected.
- `tests/test_replay.py`: a start and a stop give one interval; stop then start give a gap carrying the stop's reason; an inferred stop (`unclean`, `suspended`) sets `end_inferred` on the interval and `start_inferred` on the gap; start, start gives an unclosed interval and no gap; a host's final start is open; a stop with no open interval produces nothing; unsorted input gives the same timeline; two hosts kept separate; a run crossing local midnight gives one continuous interval; a run across the night of 24 to 25 October 2026 (fall-back, local 03:00 becomes 02:00) gives strictly increasing UTC timestamps and no negative interval.
- `tests/test_lockfile.py`: acquire, second acquire raises `AlreadyRunning`, stale pid reclaimed, recycled pid with mismatched `create_time` treated as stale, release does not remove another process's lock.
- `tests/test_daemon.py`:
  - N ticks on a fixed-step clock write exactly `collector_start` (reason `launch`, source `collector`) and `collector_stop` to the ledger: no ledger line per tick.
  - The checkpoint holds the latest tick's `ts` and our pid after every tick; a clean stop deletes it.
  - Recovery from a leftover checkpoint writes an inferred `collector_stop` with reason `unclean` at the checkpoint's `ts`, before `collector_start`, and deletes it; an unparseable checkpoint is deleted and writes nothing.
  - A clock jump over `2 × tick` writes the `suspended`/`resumed` pair at the previous tick and at now; a jump of exactly `2 × tick` writes nothing; a backwards clock writes nothing.
  - A `PermissionError` from `os.replace` is logged, the loop continues, and the next tick's checkpoint lands.
  - Ticking across the 25 October 2026 fall-back writes no suspend pair.
  - Stop file ends the loop within one tick with reason `stop_file`; signal flag ends it with reason `signal`.
- Ledger fixtures, if any, go under `tests/fixtures/` as `.jsonl`; the `.gitignore` negation allows them.

## Conventions

As in `CLAUDE.md`. Python 3.14. Aware UTC everywhere in the ledger. `ledger/events.py` and `ledger/replay.py` never call a clock. Every user-visible string in the grimdark pack, every colour in `ui/theme.py`. Do not hard-wrap prose in docstrings, comments or documents.

## Done when

- `te collect` in a terminal writes only `collector_start` with reason `launch` to `%LOCALAPPDATA%\timeexisting\ledger\<host>.jsonl` while running, and rewrites `%LOCALAPPDATA%\timeexisting\checkpoint-<host>.json` every 30 seconds; Ctrl+C writes `collector_stop` with reason `signal` and deletes the checkpoint.
- Killing the collector in Task Manager and restarting it writes an inferred `collector_stop` with reason `unclean` whose `ts` is within one interval of the kill, followed by `collector_start`.
- Sleeping the laptop for five minutes with the collector running produces an inferred `collector_stop` with reason `suspended` at the last tick before sleep and a `collector_start` with reason `resumed` at wake.
- A second `te collect` in another terminal refuses with the pack message and exits non-zero.
- `te collect stop` stops a running collector within one poll and it writes `collector_stop` with reason `stop_file`.
- Ending the collector from Task Manager leaves a stale lock that the next `te collect` reclaims silently.
- `te` with no collector running spawns one; `te collect status` then shows it; the viewer footer shows it.
- `scripts\install-startup.ps1` creates the shortcut; after signing out and back in, `te collect status` shows a collector started at login, and the ledger shows a `collector_stop` with reason `session_end` at the sign-out time.
- `ruff format --check .`, `ruff check .`, `pytest` all clean. Tag `phase-2`.
