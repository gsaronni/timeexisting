"""The argparse surface. Parses arguments and dispatches; no logic lives here."""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from timeexisting import paths
from timeexisting.config import loader
from timeexisting.config.models import ConfigError
from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock, FixedClock, SystemClock
from timeexisting.ui.app import local_timezone, run, run_demo

_AT_FORMAT = "%Y-%m-%d %H:%M"

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


def _demo_scenarios(reference: datetime) -> tuple[tuple[str, datetime], ...]:
    """The fixed carousel: pre-work, morning work, lunch, afternoon work, the
    last five minutes before the end, post-work, Saturday, Sunday, then one
    weekday at noon for each of the twelve months. Every phase and every
    month/season remark is reachable from this list.
    """
    monday = _demo_start(reference).astimezone(local_timezone())

    def moment(days: int, hour: int, minute: int = 0) -> datetime:
        return (monday + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    scenarios = [
        (pick("demo.scenario.pre_work"), moment(0, 8, 0)),
        (pick("demo.scenario.morning_work"), moment(0, 10, 0)),
        (pick("demo.scenario.lunch"), moment(0, 13, 30)),
        (pick("demo.scenario.afternoon_work"), moment(0, 15, 0)),
        (pick("demo.scenario.final_stretch"), moment(0, 17, 55)),
        (pick("demo.scenario.post_work"), moment(0, 18, 30)),
        (pick("demo.scenario.saturday"), moment(5, 12, 0)),
        (pick("demo.scenario.sunday"), moment(6, 12, 0)),
    ]
    for month in range(1, 13):
        scenarios.append((pick(f"demo.scenario.month.{month}"), _weekday_noon(monday, month)))

    return tuple((label, when.astimezone(UTC)) for label, when in scenarios)


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
    try:
        loader.load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error
    print(f"ok {paths.config_file()}")


def _run_config_command(command: str) -> None:
    if command == "path":
        _config_path()
    elif command == "init":
        _config_init()
    elif command == "check":
        _config_check()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "config":
        _run_config_command(args.config_command)
        return
    if args.demo:
        run_demo(_demo_scenarios(SystemClock().now()))
        return
    clock, label = _build_clock(args)
    run(clock, label=label)
