"""The argparse surface. Parses arguments and dispatches; no logic lives here."""

import argparse
import logging
import math
import sys
import threading
import time as time_module
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from importlib.resources import files
from zoneinfo import ZoneInfo

from timeexisting import logging_setup, paths
from timeexisting.collector import daemon, lockfile, reconcile, spawn
from timeexisting.config import loader
from timeexisting.config.models import Config, ConfigError
from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock, FixedClock, SystemClock
from timeexisting.domain.day_start import DayStart, Provenance, resolve_day_start
from timeexisting.domain.resolver import Resolved, resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.timeline_view import (
    CollectorState,
    DayRow,
    current_state,
    day_bounds,
    day_events,
    day_rows,
    hosts_on,
)
from timeexisting.ledger import store
from timeexisting.ledger.events import Event, EventType, Source, profile_for
from timeexisting.ledger.replay import IntervalType, Timeline, replay
from timeexisting.presence import backend
from timeexisting.ui.app import collector_phrase, local_timezone, run, run_demo
from timeexisting.ui.keys import key_source

logger = logging.getLogger(__name__)

_AT_FORMAT = "%Y-%m-%d %H:%M"
_DATE_FORMAT = "%Y-%m-%d"
_START_FORMAT = "%H:%M"

_FLAG_NAMES: dict[str, DayFlag] = {
    "no-lunch": DayFlag.NO_LUNCH,
    "half-day": DayFlag.HALF_DAY,
    "sick": DayFlag.SICK,
    "offsite": DayFlag.OFFSITE,
    "afspadsering": DayFlag.AFSPADSERING,
    "fri": DayFlag.FRI,
}

# Fixed weekday-of-month used to build a demo month scenario; never lands
# near a month boundary, so `day=15 + up to 2 days` never overflows.
_DEMO_MONTH_DAY = 15


def _to_utc(naive_local: datetime) -> datetime:
    return naive_local.replace(tzinfo=local_timezone()).astimezone(UTC)


def _at_type(value: str) -> datetime:
    try:
        naive = datetime.strptime(value, _AT_FORMAT)  # noqa: DTZ007 -- local wall-clock text, tz attached below
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'must look like "2026-09-17 07:30": {error}') from error
    return _to_utc(naive)


def _start_type(value: str) -> time:
    try:
        return datetime.strptime(value, _START_FORMAT).time()  # noqa: DTZ007 -- wall-clock text, no date involved
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'must look like "08:30": {error}') from error


def _date_type(value: str) -> date:
    try:
        return datetime.strptime(value, _DATE_FORMAT).date()  # noqa: DTZ007 -- a calendar date, no time involved
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'must look like "2026-10-16": {error}') from error


def _flag_type(value: str) -> DayFlag:
    try:
        return _FLAG_NAMES[value]
    except KeyError as error:
        choices = ", ".join(sorted(_FLAG_NAMES))
        raise argparse.ArgumentTypeError(f"must be one of {choices}: {value!r}") from error


def _demo_start(reference: datetime) -> datetime:
    local_reference = reference.astimezone(local_timezone())
    monday = local_reference - timedelta(days=local_reference.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _weekday_noon(local_monday: datetime, month: int) -> datetime:
    candidate = local_monday.replace(
        month=month, day=_DEMO_MONTH_DAY, hour=12, minute=0, second=0, microsecond=0
    )
    if candidate.weekday() >= 5:
        candidate += timedelta(days=7 - candidate.weekday())
    return candidate


def _demo_scenarios(reference: datetime, cfg: Config) -> tuple[tuple[str, Resolved], ...]:
    """The fixed carousel: pre-work, morning work, lunch, afternoon work, the
    last five minutes before the end, post-work, Saturday, Sunday, then one
    weekday at noon for each of the twelve months. Every phase and every
    month/season remark is reachable from this list.

    Also covers `OFF_HOURS` (not part of the step 1 list, but needed so the
    carousel genuinely contains every phase, per step 8), a moment in the
    small hours before `PRE_WORK` begins.

    Each entry is a precomputed `Resolved`, built from its own `DayPlan` (an
    unflagged day, `[credit].default_start`), so the carousel stays correct
    against whatever schedule the config actually describes.
    """
    monday_dt = _demo_start(reference).astimezone(local_timezone())
    monday = monday_dt.date()
    start = cfg.credit.default_start
    plan = build_day(monday, start, frozenset(), cfg)
    off_hours, lunch, afternoon = plan.segments[0], plan.segments[3], plan.segments[4]

    scenarios = [
        (pick("demo.scenario.off_hours"), resolve(off_hours.start + timedelta(hours=2), plan)),
        (pick("demo.scenario.pre_work"), resolve(plan.start - timedelta(minutes=30), plan)),
        (pick("demo.scenario.morning_work"), resolve(plan.start + timedelta(hours=1, minutes=30), plan)),
        (pick("demo.scenario.lunch"), resolve(lunch.start + (lunch.end - lunch.start) / 2, plan)),
        (pick("demo.scenario.afternoon_work"), resolve(afternoon.start + timedelta(hours=1), plan)),
        (pick("demo.scenario.final_stretch"), resolve(plan.nominal_end - timedelta(minutes=5), plan)),
        (pick("demo.scenario.post_work"), resolve(plan.nominal_end + timedelta(minutes=30), plan)),
    ]

    for offset, key in ((5, "saturday"), (6, "sunday")):
        weekend_day = monday + timedelta(days=offset)
        weekend_plan = build_day(weekend_day, start, frozenset(), cfg)
        noon = datetime.combine(weekend_day, time(12, 0), tzinfo=local_timezone())
        scenarios.append((pick(f"demo.scenario.{key}"), resolve(noon, weekend_plan)))

    for month in range(1, 13):
        moment = _weekday_noon(monday_dt, month)
        month_plan = build_day(moment.date(), start, frozenset(), cfg)
        scenarios.append((pick(f"demo.scenario.month.{month}"), resolve(moment, month_plan)))

    return tuple(scenarios)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="te",
        description="A work-time tracker with a dim view of work, time and tracking.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--at",
        type=_at_type,
        metavar='"YYYY-MM-DD HH:MM"',
        help="Run against a fixed Europe/Copenhagen time instead of the real clock.",
    )
    group.add_argument(
        "--demo",
        action="store_true",
        help="Cycle through a carousel of fixed states: every phase and every month.",
    )
    parser.add_argument(
        "--no-collector",
        action="store_true",
        help="Do not start a collector if none is running. Implied by --at and --demo.",
    )
    parser.add_argument(
        "-s",
        "--start",
        type=_start_type,
        metavar="HH:MM",
        help="Set the day's start time, skipping the interactive prompt.",
    )
    parser.add_argument(
        "-f",
        "--flag",
        dest="flags",
        action="append",
        type=_flag_type,
        default=[],
        metavar="FLAG",
        help=f"Apply a day flag; may be given more than once. One of: {', '.join(sorted(_FLAG_NAMES))}.",
    )

    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Manage the configuration file.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_subparsers.add_parser("path", help="Print the resolved config file path.")
    config_subparsers.add_parser("init", help="Write the packaged defaults there if absent.")
    config_subparsers.add_parser("check", help="Load and validate the configuration.")

    collect_parser = subparsers.add_parser(
        "collect", help="Run the collector in the foreground, or inspect or stop a running one."
    )
    collect_subparsers = collect_parser.add_subparsers(dest="collect_command")
    collect_subparsers.add_parser("status", help="Report the running collector, if any.")
    collect_subparsers.add_parser("stop", help="Ask the running collector to stop and wait for it.")
    timeline_parser = collect_subparsers.add_parser(
        "timeline", help="Print one day's typed intervals from the ledger. Read-only; needs no collector."
    )
    timeline_parser.add_argument(
        "--date",
        type=_date_type,
        metavar="YYYY-MM-DD",
        help="The local day to print; today when omitted.",
    )
    timeline_parser.add_argument(
        "--events", action="store_true", help="Also list the day's ledger events below the intervals."
    )

    return parser


def _build_clock(args: argparse.Namespace) -> tuple[Clock, str]:
    if args.at is not None:
        return FixedClock(args.at), ""
    return SystemClock(), ""


def _viewer_now(args: argparse.Namespace) -> datetime:
    if args.at is not None:
        return args.at.astimezone(local_timezone())
    return SystemClock().now().astimezone(local_timezone())


def _in_night_window(moment: time, cfg: Config) -> bool:
    night_start = cfg.credit.night_start
    night_end = cfg.credit.night_end
    if night_start <= night_end:
        return night_start <= moment < night_end
    return moment >= night_start or moment < night_end  # wraps midnight, e.g. 22:00-06:00


def _replay_shard(host: str, cfg: Config) -> Timeline | None:
    """This host's shard replayed with `absence.micro_max` and `collector.tick`, the one replay every reader in the viewer shares, or `None` when the shard cannot be read."""
    try:
        result = store.read_shard(store.shard_path(host))
    except OSError:
        return None
    return replay(result.events, micro_max=cfg.absence.micro_max, tick=cfg.collector.tick)


def _ledger_start(
    now: datetime, cfg: Config, *, boot_time: Callable[[], datetime | None] | None = None
) -> DayStart | None:
    """Today's start from this host's shard (roadmap section 4): a recorded `day_start`, else the first present instant of the typed timeline, with `boot_time`, by default the collector's `system_boot_time` seam, as the sanity clamp of rule 4. A ledger that cannot be read holds no start rather than failing the launch."""
    host = paths.current_host()
    timeline = _replay_shard(host, cfg)
    if timeline is None:
        logger.exception("could not read the ledger for the day start")
        return None
    boot = (boot_time or daemon.system_boot_time)()
    resolved = resolve_day_start(timeline, host, now, cfg, boot=boot)
    logger.info("day start from the ledger: %s (boot %s)", resolved, boot)
    return resolved


def _ledger_start_after_tick(
    now: datetime,
    cfg: Config,
    owner: lockfile.LockStatus,
    *,
    sleep: Callable[[float], object] = time_module.sleep,
    boot_time: Callable[[], datetime | None] | None = None,
) -> DayStart | None:
    """`_ledger_start` for a viewer that found a collector running. When the ledger holds no start, the collector may not have read the morning yet: after a night's sleep a viewer launched within a tick of the wake reads a ledger without the wake's unlock. So a resolution of `None` waits for the collector's next reconciled checkpoint, the tick after the one on disk, bounded by one tick plus the pending window (`reconcile.PENDING_WINDOW`, the longest a sleep waits for its System record), and resolves once more; the prompt is the caller's only when that is still `None`. A first resolution that succeeds waits for nothing and reads no checkpoint. The checkpoint on disk is read after the first replay, so a tick that lands between the two is waited out once more rather than missed."""
    known = _ledger_start(now, cfg, boot_time=boot_time)
    if known is not None:
        return known
    before = daemon.reconciled_checkpoint(owner.host, owner.pid)
    timeout = (cfg.collector.tick + reconcile.PENDING_WINDOW).total_seconds()
    logger.info("no start in the ledger; waiting up to %.0fs for the collector's next tick", timeout)
    if spawn.wait_for_reconciliation(owner, after=before, timeout=timeout, sleep=sleep) is None:
        logger.warning("the collector wrote no reconciled checkpoint within %.0fs; asking", timeout)
        return None
    return _ledger_start(now, cfg, boot_time=boot_time)


def _suggestion(ensured: spawn.Ensured | None, cfg: Config) -> Callable[[datetime], DayStart | None]:
    """The ledger reader `_resolve_start` consults: `_ledger_start_after_tick` for a collector this viewer found running, `_ledger_start` otherwise (one it spawned, which `spawn.ensure_running` already waited for, one that never took its lock, or none at all)."""
    if ensured is not None and not ensured.spawned and ensured.owner is not None:
        owner = ensured.owner
        return lambda moment: _ledger_start_after_tick(moment, cfg, owner)
    return lambda moment: _ledger_start(moment, cfg)


def _record_start(start: time, detail: str, now: datetime, cfg: Config) -> None:
    """Write a `day_start`, source `user`, for `now`'s local day: a typed or confirmed start is a fact, recorded the moment it is given so every later launch that day reads it back. A ledger that cannot be written loses the record, not the launch."""
    host = paths.current_host()
    moment = datetime.combine(now.date(), start, tzinfo=now.tzinfo).astimezone(UTC)
    event = Event.new(
        ts=now.astimezone(UTC),
        host=host,
        profile=profile_for(host, cfg),
        event=EventType.DAY_START,
        source=Source.USER,
        data={"start": moment.isoformat(), "detail": detail},
    )
    try:
        store.append(event)
    except OSError:
        logger.exception("could not record the day start")


def _prompt_start(cfg: Config, *, default: time | None = None, read=input, write=print) -> time:
    """Ask for the day's start. `default`, shown in brackets and taken on an empty answer, falls back to `credit.default_start`."""
    if default is None:
        default = cfg.credit.default_start
    while True:
        raw = read(f"Start time [{default:%H:%M}]: ").strip()
        if not raw:
            return default
        try:
            return datetime.strptime(raw, _START_FORMAT).time()  # noqa: DTZ007 -- wall-clock text
        except ValueError:
            write(f'"{raw}" must look like "08:30" or be empty.')


def _warn_outside_flex_band(
    start: time, cfg: Config, *, write=lambda text: print(text, file=sys.stderr)
) -> None:
    if not (cfg.contract.flex_start <= start <= cfg.contract.flex_end):
        write(
            pick("start.outside_flex").format(
                start=f"{start:%H:%M}",
                flex_start=f"{cfg.contract.flex_start:%H:%M}",
                flex_end=f"{cfg.contract.flex_end:%H:%M}",
            )
        )


def _resolve_start(
    args: argparse.Namespace,
    cfg: Config,
    now: datetime,
    *,
    read=input,
    write=print,
    suggest: Callable[[datetime], DayStart | None] | None = None,
    record: Callable[[time, str], None] | None = None,
) -> DayStart:
    """The day's start by roadmap section 4. `suggest` supplies it from the ledger (`_ledger_start`): a start recorded earlier today, else the first present instant of the typed timeline. `--start` wins over everything; otherwise the ledger's start is used whenever it has one, and only when it has none does the prompt ask, on a weekday between the night window and `credit.latest`, defaulting to `credit.default_start`. At any other hour the default is taken silently, as `inferred`. Weekends take the default without consulting the ledger.

    A `--start` or a typed or confirmed answer is a fact, and `record` writes it to the ledger as a `day_start` the moment it is given, except under `--at`, where the run is a view of a fixed moment and writes nothing."""
    suggest = suggest or (lambda moment: _ledger_start(moment, cfg))
    if record is None:
        record = (
            (lambda _start, _detail: None)
            if args.at is not None
            else (lambda s, d: _record_start(s, d, now, cfg))
        )
    if args.start is not None:
        resolved = DayStart(args.start, Provenance.FLAG)
        record(resolved.start, str(Provenance.FLAG))
    elif now.date().weekday() >= 5:
        resolved = DayStart(cfg.credit.default_start, Provenance.INFERRED)
    elif (known := suggest(now)) is not None:
        resolved = known
    elif _in_night_window(now.time(), cfg) or now.time() > cfg.credit.latest:
        # Nothing reasonable to ask at this hour: the fallback is taken without asking, and labelled as inferred.
        resolved = DayStart(cfg.credit.default_start, Provenance.INFERRED)
    else:
        resolved = DayStart(_prompt_start(cfg, read=read, write=write), Provenance.PROMPT)
        record(resolved.start, str(Provenance.PROMPT))
    _warn_outside_flex_band(resolved.start, cfg)
    return resolved


_INTERRUPTED_EXIT = 130  # 128 + SIGINT, the shell convention for Ctrl+C


def _start_or_exit(
    args: argparse.Namespace,
    cfg: Config,
    now: datetime,
    *,
    read=input,
    write=print,
    suggest: Callable[[datetime], DayStart | None] | None = None,
) -> DayStart:
    """`_resolve_start`, but Ctrl+C at the prompt exits quietly with a pack line and code 130 instead of a traceback. No Rich surface exists yet, so plain output is safe."""
    try:
        return _resolve_start(args, cfg, now, read=read, write=write, suggest=suggest)
    except KeyboardInterrupt:
        logger.info("start-time prompt interrupted")
        write("")  # the prompt left the cursor mid-line
        write(pick("start.interrupted"))
        raise SystemExit(_INTERRUPTED_EXIT) from None


def _config_path() -> None:
    print(paths.config_file())


def _config_init() -> None:
    target = paths.config_file()
    if target.exists():
        print(f"Config already exists at {target}")
        return
    defaults = files("timeexisting").joinpath("config").joinpath("defaults.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(defaults.read_bytes())
    print(f"Wrote defaults to {target}")


def _config_check() -> None:
    _load_config_or_exit()
    print(f"ok {paths.config_file()}")


def _run_config_command(command: str) -> None:
    if command == "path":
        _config_path()
    elif command == "init":
        _config_init()
    elif command == "check":
        _config_check()


_STOP_POLL_SECONDS = 0.25


def _session_watch() -> Callable[[daemon.Shutdown, daemon.Inbox], object] | None:
    if sys.platform != "win32":
        return None
    from timeexisting.collector import session_win32

    return session_win32.install


def _collect_run(cfg: Config) -> None:
    stop_event = threading.Event()
    try:
        with daemon.signal_handlers(stop_event):
            daemon.run_collector(
                SystemClock(),
                daemon.interruptible_sleep(stop_event),
                cfg,
                stop_event=stop_event,
                on_start=_session_watch(),
                event_log=backend.event_log(),
                session=backend.session_probe(),
                input_probe=backend.input_probe(),
                boot_time=daemon.system_boot_time,
            )
    except (lockfile.AlreadyRunning, lockfile.LockError) as error:
        logger.warning("collector refused to start: %s", error)
        print(pick("instance.already_running"), file=sys.stderr)
        raise SystemExit(1) from error


def _collect_status(cfg: Config) -> None:
    """The lock's owner, then the derived state of this host's ledger when it holds an open interval: the footer's sentence, printed once."""
    owner = lockfile.status(daemon.ROLE)
    if owner is None:
        print(pick("collect.status.not_running"))
        return
    since = owner.started.astimezone(local_timezone())
    print(
        pick("collect.status.running").format(pid=owner.pid, host=owner.host, since=f"{since:%Y-%m-%d %H:%M}")
    )
    state = _LedgerState(cfg)()
    if state.kind is not None:
        print(collector_phrase(state, False))


def _duration_text(duration: timedelta) -> str:
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return pick("collect.timeline.hours").format(hours=hours, minutes=minutes)
    if minutes:
        return pick("collect.timeline.minutes").format(minutes=minutes)
    return pick("collect.timeline.seconds").format(seconds=seconds)


def _row_text(row: DayRow, zone: ZoneInfo, day_end: datetime) -> str:
    end = "24:00" if row.end == day_end else f"{row.end.astimezone(zone):%H:%M}"
    return (
        pick("collect.timeline.row")
        .format(
            start=f"{row.start.astimezone(zone):%H:%M}",
            end=end,
            kind=str(row.kind),
            confidence=str(row.confidence),
            source=str(row.source),
            duration=_duration_text(row.duration),
            tail=pick("collect.timeline.open") if row.open else "",
        )
        .rstrip()
    )


def _event_text(event: Event, zone: ZoneInfo) -> str:
    detail = event.data.get("detail") or event.data.get("reason") or event.data.get("kind") or ""
    return (
        pick("collect.timeline.event_row")
        .format(
            time=f"{event.ts.astimezone(zone):%H:%M:%S}",
            kind=str(event.event),
            source=str(event.source),
            confidence=str(event.confidence),
            detail=detail,
        )
        .rstrip()
    )


def _collect_timeline(
    cfg: Config,
    day: date,
    now: datetime,
    *,
    events: bool = False,
    write: Callable[[str], object] = print,
    error: Callable[[str], object] = lambda text: print(text, file=sys.stderr),
) -> None:
    """`te collect timeline` (roadmap section 3): every shard replayed with `absence.micro_max`, and `day`'s intervals per host printed in the configured zone with type, confidence, source and duration. An open interval runs to `now`. With `events`, the day's ledger events follow each host's rows. Malformed lines are counted on stderr. Reads only; writes nothing."""
    zone = ZoneInfo(cfg.display.timezone)
    result = store.read_all()
    timeline = replay(result.events, micro_max=cfg.absence.micro_max, tick=cfg.collector.tick)
    _, day_end = day_bounds(day, zone)
    printed = False
    for host in hosts_on(timeline, day, zone):
        rows = day_rows(timeline, host, day, zone, now)
        listed = day_events(timeline, host, day, zone) if events else ()
        if not rows and not listed:
            continue
        printed = True
        write(pick("collect.timeline.header").format(date=day.isoformat(), host=host, zone=zone.key))
        for row in rows:
            write(_row_text(row, zone, day_end))
        if events:
            write(pick("collect.timeline.events"))
            for event in listed:
                write(_event_text(event, zone))
    if not printed:
        write(pick("collect.timeline.empty").format(date=day.isoformat()))
    if result.malformed:
        error(pick("collect.timeline.malformed").format(count=result.malformed))


def _collect_stop(cfg: Config, *, sleep: Callable[[float], object] = time_module.sleep) -> None:
    if lockfile.status(daemon.ROLE) is None:
        print(pick("collect.status.not_running"))
        return
    paths.stop_file().touch()
    polls = math.ceil(2 * cfg.collector.poll.total_seconds() / _STOP_POLL_SECONDS)
    for _ in range(polls):
        sleep(_STOP_POLL_SECONDS)
        if lockfile.status(daemon.ROLE) is None:
            print(pick("collect.stop.stopped"))
            return
    print(pick("collect.stop.timeout"))
    raise SystemExit(1)


def _run_collect_command(args: argparse.Namespace, cfg: Config) -> None:
    command = args.collect_command
    if command is None:
        _collect_run(cfg)
    elif command == "status":
        _collect_status(cfg)
    elif command == "stop":
        _collect_stop(cfg)
    elif command == "timeline":
        now = SystemClock().now()
        day = args.date or now.astimezone(ZoneInfo(cfg.display.timezone)).date()
        _collect_timeline(cfg, day, now, events=args.events)


def _checkpoint_since(host: str) -> datetime | None:
    try:
        return daemon.read_checkpoint(host)
    except ValueError:
        return None


class _LedgerState:
    """The footer's `CollectorState`, asked once per viewer tick. The lock says whether a collector runs; the derived state comes from this host's shard, replayed again only when the shard's size or mtime has changed, so a 1 Hz viewer does not replay the ledger every second. Without a collector, `since` is the last checkpoint tick, the closest thing to when observation stopped."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._stamp: tuple[int, int] | None = None
        self._derived: tuple[IntervalType, datetime] | None = None

    def __call__(self) -> CollectorState:
        host = paths.current_host()
        owner = lockfile.status(daemon.ROLE)
        if owner is None:
            return CollectorState(running=False, kind=None, since=_checkpoint_since(host))
        derived = self._current(host)
        if derived is None:
            return CollectorState(running=True, kind=None, since=owner.started)
        kind, since = derived
        return CollectorState(running=True, kind=kind, since=since)

    def _current(self, host: str) -> tuple[IntervalType, datetime] | None:
        try:
            stat = store.shard_path(host).stat()
            stamp: tuple[int, int] | None = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            stamp = None
        if stamp is None or stamp != self._stamp:
            timeline = _replay_shard(host, self._cfg)
            self._derived = None if timeline is None else current_state(timeline, host)
            self._stamp = stamp
        return self._derived


def _start_collector(args: argparse.Namespace) -> spawn.Ensured | None:
    """Make sure a collector runs before the Rich surface exists, unless this session opted out, in which case `None`. What came back says whether the collector was found or spawned, and whether a spawned one ever took its lock."""
    if args.no_collector or args.at is not None or args.demo:
        return None
    return spawn.ensure_running()


def _load_config_or_exit() -> Config:
    try:
        return loader.load_config()
    except ConfigError as error:
        logger.error("config rejected: %s", error)
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    runs_collector = args.command == "collect" and args.collect_command is None
    role = daemon.ROLE if runs_collector else "viewer"
    logging_setup.configure(role)
    logger.info("%s started: %s", role, sys.argv[1:] if argv is None else argv)
    if args.command == "config":
        _run_config_command(args.config_command)
        return

    cfg = _load_config_or_exit()

    if args.command == "collect":
        _run_collect_command(args, cfg)
        return

    ensured = _start_collector(args)

    if args.demo:
        with key_source() as keys:
            run_demo(_demo_scenarios(SystemClock().now(), cfg), cfg, keys=keys)
        return

    start = _start_or_exit(args, cfg, _viewer_now(args), suggest=_suggestion(ensured, cfg))
    flags = frozenset(args.flags)
    clock, label = _build_clock(args)
    with key_source() as keys:
        run(
            clock,
            cfg,
            start.start,
            flags,
            label=label,
            collector_state=_LedgerState(cfg),
            spawn_failed=ensured is not None and ensured.failed,
            start_inferred=start.provenance is Provenance.INFERRED,
            keys=keys,
        )
