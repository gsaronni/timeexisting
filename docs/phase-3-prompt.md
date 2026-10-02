# Phase 3 prompt for Claude Code

Paste this as the first message in a new Claude Code session at the repository root, with the venv active and `phase-2` tagged.

---

You are working on `timeexisting`. Read `CLAUDE.md`, `docs/logs.org` (every phase 2 entry in full, including the post-tag fixes) and `docs/timeexisting-roadmap.org` sections 3 to 8, 16 and 19 before doing anything. Then read the phase 2 code as it stands: `events.py`, `store.py`, `replay.py`, `lockfile.py`, `daemon.py`, `session_win32.py`, `spawn.py`, the checkpoint handling, and the start-prompt default added after the tag. The spec drifted from the code during phase 2 (heartbeats retired, checkpoint files introduced, a hidden top-level window instead of a message-only one). Where spec and code disagree, the code and `logs.org` describe what exists and the spec is what you correct.

**Phase 3 scope: absences are real. Lock and unlock detection, machine sleep and wake detection, the System event log as the source of exact boundaries, the clock-jump path reconciled against it, recovery after an unclean stop, presence credited on user evidence instead of collector liveness, and automatic detection of the day's start so the start prompt becomes a fallback. Replay produces a typed timeline. The resolver's arithmetic does not change in this phase except for the start time. No absence classes, no classification queue, no balances, no calendar, no power actions; those are phases 4 and 5. No Fedora D-Bus backend: the backend registry must leave room for it, but it is deferred because it cannot be live-checked on the work laptop.**

## Ground rules

- Work in small, reviewable steps. After each step run `ruff format .`, `ruff check .` and `pytest`, show me the result, and stop for review. One concrete step at a time. Full replacement files, not fragments. I commit between steps; you never commit.
- Every step ends with a number I can verify: a test count, an event count, a timestamp to compare against reality.
- The repository is public. No real hostname, username, path, ledger line or event log XML from this machine goes into any committed file. Test fixtures are synthetic. Event log XML fixtures are hand-written to the real schema with invented values.
- Windows-only code is imported lazily behind the backend registry. CI runs on Ubuntu and Windows and must stay green on both. Every `ctypes` and `win32evtlog` call sits behind a thin seam that tests replace; no test touches the real event log, the real session or the real clock.
- `domain/` and replay stay pure: no I/O, no clock, no platform imports. The existing architecture test enforces it; extend it to any new pure module.
- Aware UTC for storage and arithmetic. Event log `SystemTime` values are UTC already; parse them as such. 25 October 2026 (DST end) must not move a stored timestamp, and a test proves it for an interval spanning 02:00 to 03:00 local.
- The ledger is append-only and transition-only. Never rewrite an existing line. Never write a line per tick. Phase 2 shards must keep replaying without error.

## What phase 2 taught, and what this phase answers

1. **Collector liveness is not user presence.** A Modern Standby dark wake on 26 September at 07:50 was recorded as zero-length presence. A dark wake runs code on a machine nobody is sitting at.
2. **The post-tag start default inherits that bug.** It reads today's first ledger presence, so a dark wake before the real morning becomes the default start. Step 8 fixes it.
3. **The hidden top-level window works.** It already receives `WM_QUERYENDSESSION` and `WM_ENDSESSION`. Reuse it for session and power notifications; do not create a second window.

## The presence model

This is the contract for the phase. Write it into roadmap section 6 in step 1 and test it in step 3.

Replay derives three independent facts for every instant:

| Fact | States |
|---|---|
| Machine | awake, asleep, off, unknown |
| Session | unlocked, locked, unknown |
| Observation | observing, not observing |

**Present means awake, unlocked and evidenced.** Evidence opens presence; evidence never closes it.

| Opens presence | Closes presence |
|---|---|
| `unlock` (session notification or probe) | `lock` |
| Session logon time, if it falls on the current local day | `suspend` |
| User input after a resume, on a session that was already unlocked | `collector_stop` with `session_end`, `stop_file`, `signal` or `console_close` |
| | Loss of observation (unclean stop, downtime) |

**Idle is never absence.** The operating system's power policy is the idle detector: no input turns the screen off, then sleeps and locks the machine, and video playback holds the display awake so a two-hour course does not trigger it. The tracker reads the lock and sleep that policy produces and never runs an idle timer of its own. Input is read only to open presence after a resume on an unlocked session. It is never read to close presence and never used to measure idle time. A two-hour meeting without locking is presence. This amends the roadmap sentence "foreground window and input idle time are never read": the foreground window is still never read, and input is read once, as positive evidence, under the condition above.

Replay emits a typed timeline of non-overlapping intervals:

| Interval | Meaning |
|---|---|
| `present` | Awake, unlocked, evidenced |
| `away` | Awake and locked. A dark wake inside a locked night lands here |
| `asleep` | Machine suspended, boundaries observed or inferred |
| `off` | Machine shut down or rebooting |
| `unevidenced` | Awake and unlocked with no evidence yet: a resume on an unlocked session before any input |
| `downtime` | Collector not observing. State unknown, never rendered as presence or absence |

Every interval carries `confidence` (`observed` or `inferred`) from its boundary events. Adjacent intervals of the same type and confidence merge. Zero-length intervals are dropped.

**Legacy days.** Phase 2 shards carry no lock, unlock, suspend or resume events. Replay reads them without error. A legacy `collector_stop` (`suspended`) followed by `collector_start` (`resumed`) is read as an inferred `suspend` and `resume`. Legacy collector liveness with no session events becomes `unevidenced`, never `present`. Confirm the exact phase 2 field values from the code before writing this mapping.

## Step 0: evidence inventory (read-only)

Nothing in this step is committed except, at the end, an aggregate entry in `docs/logs.org`. Put any scratch script under an ignored path or outside the repository.

- Run `powercfg /a` and report whether this machine uses Modern Standby (S0 Low Power Idle) or S3.
- Confirm the System log is readable unelevated through `win32evtlog.EvtQuery`.
- Over the last 21 days of the System log, count per event ID per day: Power-Troubleshooter 1; Kernel-Power 40, 41, 42, 107, 172, 506, 507, 566; Kernel-General 1, 12, 13; EventLog 6005, 6006, 6008. For Kernel-Power 42, list the distinct sleep reasons. For Kernel-Power 40, list which drivers stopped a power transition and whether a 107 followed within a minute (a failed hibernation). For Kernel-General 1, report the distribution of time deltas, so the clock-jump threshold is checked against real clock corrections. For 506 and 507, list the distinct reason values seen and how often.
- Classify every dark wake (collector activity inside a sleep span with no unlock) by cause, from the records around it: a timer or maintenance wake, a network or device wake, or a transition from sleep to hibernation (look for a Kernel-Power 42 whose target state is hibernation shortly after the wake, and for the wake source in Power-Troubleshooter 1). Report whether hibernation is enabled (`powercfg /a`) and, if `powercfg /sleepstudy` runs unelevated, whether its session list agrees. A known case: on Saturday 26 September at 07:50, 507 (exiting Modern Standby for sleep, hibernate or shutdown), then 42 with reason "Hibernate from Sleep - Standby Battery Budget Exceeded", then 40 naming `vpcivsp` (the Hyper-V virtual PCI driver that WSL2 uses), then 107 resumed from sleep 15 seconds later. This answers whether dark wakes are mostly the sleep-to-hibernate transition and how often a driver aborts it, and it is an input to phase 5, where `SetSuspendState` hibernates whenever hibernation is enabled.
- Report whether the session was locked at every resume. If it always is, the input-evidence path in step 7 has nothing to do on this machine and we may drop it.
- Read this host's ledger shard for the same days and lay the collector transitions against the log events. Report: how many collector gaps are fully covered by a log-recorded sleep; how many collector intervals fall inside a 506 to 507 window (dark wakes); for each resume, how long after the wake the corresponding record appeared in the log, if that is derivable from the record; any collector gap that no log event explains.

- Second reference case, Friday 2 October 2026: work started at 08:55 local, a meeting away from the laptop ran from 09:00 to 09:30, and the collector had been running since 28 September 16:43. A viewer launched before 10:05 showed a start of 08:55; a viewer relaunched at about 10:13, through the silent late branch, showed 09:33. Already established by hand: the laptop was not touched before 09:33 (confirmed by the user), so 08:55 has no machine evidence and the defect is that a typed start is not remembered across launches (step 8). The System log for that morning reads: 09:33:20 Kernel-Power 507, reason Lid; 09:33:35 Kernel-Power 105, power source change; 09:36:59 Kernel-Power 506, reason Idle Timeout, with 566 reason SessionUnlock at the same second; 09:37:44 Kernel-Power 507, reason Input Mouse, with 566 reason InputHid. The ledger has `collector_start` (resumed) at 09:33:44 and nothing for the 45-second standby. So phase 2 credited presence from the lid opening onto the lock screen, before any unlock. Use this morning to decide what the 566 reasons mean (the SessionUnlock reason on a standby entry is not self-explanatory), whether 506 and 507 reasons are reliable enough to carry boundaries, and where the first unlock actually falls; the presence model must put today's first presence at that unlock, not at 09:33.

Report as aggregate tables only: counts, durations, distributions. No hostnames, no raw XML, no ledger lines. Then stop. I review, and we decide which event IDs carry the sleep boundaries on this machine before step 1. If Power-Troubleshooter 1 turns out to be absent or partial under Modern Standby, the design uses 506 and 507 instead; do not assume either until the counts are in.

## Step 1: spec amendments

Edit `docs/timeexisting-roadmap.org` only. No code.

- Section 3: collector lifecycle bullets describe the hidden top-level window as the receiver of session-end, session-change and power-broadcast messages, and a single-writer rule (see step 5). Add the read-only diagnostic `te collect timeline` to the subcommand list.
- Section 4: start resolution order becomes `--start`, then the first presence-opening event of the day, then the session logon time if today, then `boot_time()` as a sanity clamp only, then the prompt as the last fallback. Nothing assumed silently still holds: an inferred start is labelled as inferred.
- Section 5: event vocabulary and sources updated to what steps 2 to 7 add. Remove any remaining heartbeat language.
- Section 6: replace the detection table with the presence model above and the event IDs step 0 confirmed. Record the input-evidence amendment explicitly. Record that `OpenInputDesktop` is a reconciliation probe, not the primary signal, and why (step 5).
- Section 8: rewrite the recovery table against checkpoint files instead of heartbeats, using the rules in step 6.
- Section 16: phase 3 row matches this scope. Section 18: add the Fedora D-Bus backend as deferred, holiday exclusion in `{remaining_week}` as waiting for phase 4, ledger backup (destination and mechanism undecided), and Teams presence. Record it as decided in design, built in phase 5, with these rules: on the first presence-opening event of a day for the work profile, a free day (weekend, `helligdag`, `ferie`, `sygdom`, a full day of `afspadsering`, `fri`) stops the Teams process if running, and a working day starts it if not running; a `sick` flag set during the day stops it then; a partial-day leave entry does nothing; each direction acts at most once per day, so a Teams the user restarts or stops by hand is never fought; the action is written to the diagnostic log, not the ledger; process name (`ms-teams`) and launch identifier (`MSTeams_8wekyb3d8bbwe!MSTeams`, the standard AppID for the new Teams, launched as `shell:AppsFolder\<AppID>`) are config defaults, overridable locally; the stop and start cycle was verified by hand on 2 October 2026, with both processes restarted within two seconds of the start command; no-op off Windows and off the work profile. Rejected: synthetic keystrokes (fragile, and they would forge the tracker's own input evidence). Microsoft Graph `setUserPreferredPresence` stays an alternative only if the tenant allows a personal app registration.

## Step 2: event schema

In `events.py`, add `lock`, `unlock`, `suspend`, `resume`, `boot` and `input` events, and the sources `wts`, `probe`, `power`, `eventlog`, `clock`, `logon`. The change is additive. If the reader already tolerates unknown event types and fields, the schema version stays at 1; if it does not, tell me before changing anything, because old shards must stay valid without migration. `suspend` and `resume` may carry an optional `detail` string (the log's wake source or 506/507 reason). Tests: round-trip every new event, and a phase 2 shard fixture still replays to the same result as before.

## Step 3: the pure presence model

Rewrite the presence derivation in replay to emit the typed timeline above. Pure, synthetic fixtures only. Required tests, each a named case:

- Plain working day: logon, lock at lunch, unlock, lock in the evening, sleep. Expected interval sequence asserted exactly.
- Dark wake with session locked: suspend, resume, suspend with no unlock in between yields `asleep`, `away`, `asleep` and zero `present` time.
- Dark wake on an unlocked session yields `unevidenced`, then `present` only from the `input` event.
- Lock without sleep, then sleep, then wake, then unlock: `away`, `asleep`, `away`, `present`.
- Unclean stop: `downtime` from the checkpoint to the next start, unless step 6's rules classify it.
- Midnight crossing with the collector running: intervals split at local midnight only for day views, never in storage.
- DST end on 25 October 2026.
- The 26 September shape reproduced synthetically: zero `present` time.
- Every phase 2 legacy shape from the code.

## Step 4: event log reader

A Windows backend module that queries the System log through `win32evtlog.EvtQuery` with an XPath filter on provider, event ID and `TimeCreated` since a given UTC instant, and returns typed boundary records. Split it in two: a pure parser from event XML to records, tested against hand-written XML fixtures, and a thin I/O function that is the only thing touching the API. Parse only the IDs step 0 confirmed. A non-Windows backend returns nothing. A query failure is logged and returns nothing; it never stops the collector.

## Step 5: session and power notifications

On the existing hidden window:

- Register with `WTSRegisterSessionNotification` for this session and handle `WM_WTSSESSION_CHANGE`: `WTS_SESSION_LOCK` writes `lock`, `WTS_SESSION_UNLOCK` writes `unlock`, both source `wts`, confidence `observed`. Unregister on teardown. Take constants from the Windows headers, not from memory.
- Handle `WM_POWERBROADCAST`: `PBT_APMSUSPEND` writes `suspend` source `power`; the resume messages write `resume` source `power`. Record which resume message arrived in `detail`; whether `PBT_APMRESUMESUSPEND` (user-initiated) arrives at all under Modern Standby is something the live check answers, not an assumption.
- **Single writer.** The window thread never appends to the ledger. It puts transitions on a queue that the collector's tick loop drains, so ledger order is append order and the existing fsync path is the only write path.
- **Probe reconciliation.** Each tick, poll `OpenInputDesktop` and compare with the last known session state held in the checkpoint. A disagreement that persists for two consecutive ticks writes the missing transition with source `probe` and confidence `inferred`. Two ticks, not one, because the UAC secure desktop also makes `OpenInputDesktop` fail and must not be read as a lock.
- **Startup probe.** At collector start, record the current session state. If unlocked and the session logon time (from `WTSQuerySessionInformation`) falls on today's local date, write `unlock` source `logon` at the logon time; otherwise write `unlock` source `probe` at the start time, confidence `inferred`.

Live check I run before you continue: Win+L, wait one minute, unlock. Expected: exactly one `lock` and one `unlock` with source `wts`, each within two seconds of the real moment, and no `probe` lines.

## Step 6: clock jump, event log reconciliation and recovery

The phase 2 clock-jump path currently writes an inferred `collector_stop` (`suspended`) and `collector_start` (`resumed`) pair. Replace it:

- On a detected jump, or on a `resume` notification, query the event log for the gap. The record can lag the wake, so hold the gap as pending in the checkpoint and retry each tick for a bounded window (two minutes unless step 0's lag distribution argues otherwise). A covering record writes `suspend` and `resume` at the exact logged times, source `eventlog`, `observed`. No record within the window writes them at the checkpoint and the detected wake time, source `clock`, `inferred`. A pending gap survives a crash because it lives in the checkpoint.
- If a `power` notification already wrote the pair, the event log result refines nothing in the ledger: log the comparison in the diagnostic log only. One pair per sleep.
- Recovery after an unclean stop, rules in order: the log shows a sleep covering the gap, backfill observed `suspend` and `resume`; `boot_time()` or Kernel-General 12/13 falls inside the gap, write `boot` with the logged times and mark the stretch `off`; the gap is shorter than the micro threshold, fold it silently; otherwise leave it as `downtime`. The phase 2 inferred unclean `collector_stop` at the checkpoint stays as written.
- Tests cover each rule with synthetic checkpoints, log records and clocks, including a record that arrives on the third retry and one that never arrives.

## Step 7: input evidence

Only if step 0 showed resumes onto an unlocked session; otherwise skip this step, record why in `logs.org`, and leave `unevidenced` as the permanent state for that case.

Only while the derived state is awake, unlocked and unevidenced, read `GetLastInputInfo` once per tick. If the last input tick is later than the resume tick, write one `input` event, source `win32`, at the time derived from the input tick, and stop reading until the next resume. `LASTINPUTINFO.dwTime` and `GetTickCount` are 32-bit and wrap after about 49.7 days: compare with unsigned 32-bit subtraction and test the wrap. Never read input in any other state.

## Step 8: day start detection

The start resolution from step 1 replaces the post-tag default. The first presence-opening event of the current local day, for the work profile, is the start; the prompt appears only when there is none, and its default comes from the same source, never from collector liveness. The resolver labels the start's provenance (`flag`, `observed`, `logon`, `inferred`, `prompt`) and the viewer renders an inferred start differently from an observed one. **A start the user typed or confirmed is a fact, not a default.** Write it to the ledger as a `day_start` event, source `user`, the moment it is entered or confirmed (at the prompt or through `--start`), and let it win on every later viewer launch that day, ahead of any observed evidence. Without this, a relaunch through the silent late branch re-derives the start and can move it: on 2 October a confirmed 08:55 became 09:33 on relaunch. A later `day_start` for the same day supersedes an earlier one. Tests: the dark-wake morning picks the real unlock, not 07:50; a day with only legacy lines falls back to the prompt; `--start` still wins; a start confirmed at the first launch survives a second launch that takes the silent late branch, reproducing 2 October.

## Step 9: viewer and diagnostics

- The collector status line shows the current derived state and since when: present, away, asleep after resume, unevidenced, or not observing. Strings through the pack loader, colours through `theme.py`.
- `te collect timeline [--date YYYY-MM-DD]` prints the day's typed intervals in local time with type, confidence, source and duration, plain text, no Rich live surface. This is how I check the model against my memory of the day.

## Step 10: live checks and the log

Give me the checklist below as one block, then wait while I run it over a working day. Each item has an expected result I compare against `te collect timeline`:

1. Lock, walk away five minutes, unlock: one `away` interval of about five minutes.
2. Lock and let the laptop sleep for at least fifteen minutes: `away` then `asleep` with `eventlog` or `power` boundaries.
3. Close the lid for ten minutes: `asleep`, observed.
4. The overnight interval: no `present` time between leaving and the next morning's unlock, whatever dark wakes occurred.
5. Kill the collector in Task Manager, wait three minutes, start `te`: `downtime` of about three minutes, collector respawned.
6. Restart the machine: `session_end`, `off`, then `logon`. This also closes the phase 2 session_end live check.
7. The next morning, `te` opens without the start prompt and shows the observed start.

When I report the results, append a session entry to `docs/logs.org`, update its phase status table, and stop. Tagging `phase-3` is mine.
