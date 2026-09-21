"""The argparse surface. Parses arguments and dispatches; no logic lives here."""

import argparse
from datetime import UTC, datetime, timedelta

from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock, FixedClock, ScaledClock, SystemClock
from timeexisting.ui.app import local_timezone, run

_AT_FORMAT = "%Y-%m-%d %H:%M"

# 1 real second = 24 simulated minutes: a full day cycles in 60 real
# seconds, a full week in 7 real minutes.
_DEMO_FACTOR = 1440.0


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
        help="Cycle through a full day and week on a scaled clock.",
    )
    return parser


def _build_clock(args: argparse.Namespace) -> tuple[Clock, str]:
    if args.at is not None:
        return FixedClock(args.at), ""
    if args.demo:
        start = _demo_start(SystemClock().now())
        return ScaledClock(start, _DEMO_FACTOR), pick("app.demo_label")
    return SystemClock(), ""


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    clock, label = _build_clock(args)
    run(clock, label=label)
