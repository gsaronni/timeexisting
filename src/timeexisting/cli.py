"""The argparse surface. Parses arguments and dispatches; no logic lives here."""

import argparse
import logging
import math
import sys
import threading
import time as time_module
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from importlib.resources import files

from timeexisting import logging_setup, paths
from timeexisting.collector import daemon, lockfile
from timeexisting.config import loader
from timeexisting.config.models import Config, ConfigError
from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock, FixedClock, SystemClock
from timeexisting.domain.resolver import Resolved, resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.ui.app import local_timezone, run, run_demo

logger = logging.getLogger(__name__)

_AT_FORMAT = "%Y-%m-%d %H:%M"
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
        "--start",
        type=_start_type,
        metavar="HH:MM",
        help="Set the day's start time, skipping the interactive prompt.",
    )
    parser.add_argument(
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


def _prompt_start(cfg: Config, *, read=input, write=print) -> time:
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
            f"Warning: start {start:%H:%M} is outside the flex band "
            f"{cfg.contract.flex_start:%H:%M}-{cfg.contract.flex_end:%H:%M}."
        )


def _resolve_start(args: argparse.Namespace, cfg: Config, now: datetime, *, read=input, write=print) -> time:
    if args.start is not None:
        start = args.start
    elif now.date().weekday() >= 5:
        start = cfg.credit.default_start
    elif _in_night_window(now.time(), cfg) or now.time() > cfg.credit.latest:
        # Nothing reasonable to ask at this hour: use the default silently.
        start = cfg.credit.default_start
    else:
        start = _prompt_start(cfg, read=read, write=write)
    _warn_outside_flex_band(start, cfg)
    return start


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


def _collect_run(cfg: Config) -> None:
    stop_event = threading.Event()
    try:
        with daemon.signal_handlers(stop_event):
            daemon.run_collector(
                SystemClock(), daemon.interruptible_sleep(stop_event), cfg, stop_event=stop_event
            )
    except (lockfile.AlreadyRunning, lockfile.LockError) as error:
        logger.warning("collector refused to start: %s", error)
        print(pick("instance.already_running"), file=sys.stderr)
        raise SystemExit(1) from error


def _collect_status() -> None:
    owner = lockfile.status(daemon.ROLE)
    if owner is None:
        print(pick("collect.status.not_running"))
        return
    since = owner.started.astimezone(local_timezone())
    print(
        pick("collect.status.running").format(pid=owner.pid, host=owner.host, since=f"{since:%Y-%m-%d %H:%M}")
    )


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


def _run_collect_command(command: str | None, cfg: Config) -> None:
    if command is None:
        _collect_run(cfg)
    elif command == "status":
        _collect_status()
    elif command == "stop":
        _collect_stop(cfg)


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
        _run_collect_command(args.collect_command, cfg)
        return

    if args.demo:
        run_demo(_demo_scenarios(SystemClock().now(), cfg), cfg)
        return

    start = _resolve_start(args, cfg, _viewer_now(args))
    flags = frozenset(args.flags)
    clock, label = _build_clock(args)
    run(clock, cfg, start, flags, label=label)
