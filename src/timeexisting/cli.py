"""The argparse surface. Parses arguments and dispatches; no logic lives here."""

import argparse
import sys
from datetime import UTC, date, datetime, time, timedelta
from importlib.resources import files

from timeexisting import paths
from timeexisting.config import loader
from timeexisting.config.models import Config, ConfigError
from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock, FixedClock, SystemClock
from timeexisting.domain.resolver import Resolved, resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.ui.app import local_timezone, run, run_demo

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

    Each entry is a precomputed `Resolved`, built from its own `DayPlan` (an
    unflagged day, `[credit].default_start`), so the carousel stays correct
    against whatever schedule the config actually describes.
    """
    monday_dt = _demo_start(reference).astimezone(local_timezone())
    monday = monday_dt.date()
    start = cfg.credit.default_start
    plan = build_day(monday, start, frozenset(), cfg)
    lunch, afternoon = plan.segments[3], plan.segments[4]

    scenarios = [
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

    return parser


def _build_clock(args: argparse.Namespace) -> tuple[Clock, str]:
    if args.at is not None:
        return FixedClock(args.at), ""
    return SystemClock(), ""


def _viewer_day(args: argparse.Namespace) -> date:
    if args.at is not None:
        return args.at.astimezone(local_timezone()).date()
    return SystemClock().now().astimezone(local_timezone()).date()


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


def _resolve_start(args: argparse.Namespace, cfg: Config, day: date, *, read=input, write=print) -> time:
    if args.start is not None:
        start = args.start
    elif day.weekday() >= 5:
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


def _load_config_or_exit() -> Config:
    try:
        return loader.load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "config":
        _run_config_command(args.config_command)
        return

    cfg = _load_config_or_exit()

    if args.demo:
        run_demo(_demo_scenarios(SystemClock().now(), cfg), cfg)
        return

    start = _resolve_start(args, cfg, _viewer_day(args))
    flags = frozenset(args.flags)
    clock, label = _build_clock(args)
    run(clock, cfg, start, flags, label=label)
