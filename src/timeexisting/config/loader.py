"""Loads `config/defaults.toml` (packaged, ships the section 15 values)
overlaid by the user's `config.toml` from the platformdirs config directory,
if it exists. Unknown keys are an error. Never rewrites config.
"""

import tomllib
from importlib.resources import files

from timeexisting import paths
from timeexisting.config.models import (
    AbsenceConfig,
    CollectorConfig,
    Config,
    ConfigError,
    ContractConfig,
    CreditConfig,
    DisplayConfig,
    DriftConfig,
    LunchConfig,
    NotifyConfig,
    PowerConfig,
    ProfilesConfig,
    parse_clock_time,
    parse_duration,
)


def _load_toml(opener, *, source: str) -> dict:
    try:
        with opener() as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"malformed TOML in {source}: {error}") from error


def _load_defaults() -> dict:
    path = files("timeexisting").joinpath("config").joinpath("defaults.toml")
    return _load_toml(lambda: path.open("rb"), source="the packaged defaults")


def _load_overlay() -> dict:
    path = paths.config_file()
    if not path.is_file():
        return {}
    return _load_toml(lambda: path.open("rb"), source=str(path))


# Tables whose keys are open-ended data, not a fixed schema: an overlay may
# add any key here without tripping the unknown-key check. `profiles.hosts`
# maps arbitrary hostnames (from `platform.node()`) to profile names.
_OPEN_MAP_PATHS = frozenset({"profiles.hosts"})


def _merge(defaults: dict, overlay: dict, prefix: str = "") -> dict:
    merged = dict(defaults)
    for key, value in overlay.items():
        key_path = f"{prefix}.{key}" if prefix else key
        if key not in defaults:
            raise ConfigError(f"unknown config key: {key_path}")
        if key_path in _OPEN_MAP_PATHS:
            merged[key] = value
            continue
        default_value = defaults[key]
        if isinstance(default_value, dict) and isinstance(value, dict):
            merged[key] = _merge(default_value, value, key_path)
        else:
            merged[key] = value
    return merged


def _table(data: dict, name: str) -> dict:
    try:
        return data[name]
    except KeyError as error:
        raise ConfigError(f"missing config table: {name}") from error


def _field(table: dict, name: str, table_name: str) -> object:
    try:
        return table[name]
    except KeyError as error:
        raise ConfigError(f"missing config key: {table_name}.{name}") from error


def _build_contract(table: dict) -> ContractConfig:
    return ContractConfig(
        weekly_target=parse_duration(_field(table, "weekly_target", "contract")),
        daily_target=parse_duration(_field(table, "daily_target", "contract")),
        working_days=tuple(_field(table, "working_days", "contract")),
    )


def _build_lunch(table: dict) -> LunchConfig:
    return LunchConfig(
        window_start=parse_clock_time(_field(table, "window_start", "lunch")),
        window_end=parse_clock_time(_field(table, "window_end", "lunch")),
        deduction=parse_duration(_field(table, "deduction", "lunch")),
        min_minutes=int(_field(table, "min_minutes", "lunch")),
    )


def _build_credit(table: dict) -> CreditConfig:
    return CreditConfig(
        earliest=parse_clock_time(_field(table, "earliest", "credit")),
        latest=parse_clock_time(_field(table, "latest", "credit")),
        night_start=parse_clock_time(_field(table, "night_start", "credit")),
        night_end=parse_clock_time(_field(table, "night_end", "credit")),
        default_start=parse_clock_time(_field(table, "default_start", "credit")),
    )


def _build_absence(table: dict) -> AbsenceConfig:
    return AbsenceConfig(
        micro_max=parse_duration(_field(table, "micro_max", "absence")),
        meeting_slots=tuple(parse_duration(slot) for slot in _field(table, "meeting_slots", "absence")),
        meeting_tolerance=parse_duration(_field(table, "meeting_tolerance", "absence")),
        ask_above=parse_duration(_field(table, "ask_above", "absence")),
    )


def _build_drift(table: dict) -> DriftConfig:
    return DriftConfig(
        green_max=parse_duration(_field(table, "green_max", "drift")),
        amber_max=parse_duration(_field(table, "amber_max", "drift")),
    )


def _build_collector(table: dict) -> CollectorConfig:
    return CollectorConfig(
        heartbeat=parse_duration(_field(table, "heartbeat", "collector")),
        poll=parse_duration(_field(table, "poll", "collector")),
    )


def _build_notify(table: dict) -> NotifyConfig:
    return NotifyConfig(
        kitchen_start=parse_clock_time(_field(table, "kitchen_start", "notify")),
        kitchen_repeat=parse_clock_time(_field(table, "kitchen_repeat", "notify")),
        kitchen_end=parse_clock_time(_field(table, "kitchen_end", "notify")),
    )


def _build_display(table: dict) -> DisplayConfig:
    return DisplayConfig(
        timezone=str(_field(table, "timezone", "display")),
        refresh_hz=float(_field(table, "refresh_hz", "display")),
        voice=str(_field(table, "voice", "display")),
        locale=str(_field(table, "locale", "display")),
        palette=str(_field(table, "palette", "display")),
    )


def _build_profiles(table: dict) -> ProfilesConfig:
    return ProfilesConfig(
        default=str(_field(table, "default", "profiles")),
        hosts=dict(_field(table, "hosts", "profiles")),
    )


def _build_power(table: dict) -> PowerConfig:
    return PowerConfig(
        action=str(_field(table, "action", "power")),
        trigger=str(_field(table, "trigger", "power")),
        warning=parse_duration(_field(table, "warning", "power")),
        postpone=parse_duration(_field(table, "postpone", "power")),
        postpone_max=int(_field(table, "postpone_max", "power")),
        weekends=bool(_field(table, "weekends", "power")),
        holidays=bool(_field(table, "holidays", "power")),
        sick=bool(_field(table, "sick", "power")),
    )


def _build_config(data: dict) -> Config:
    return Config(
        version=int(_field(data, "version", "")),
        contract=_build_contract(_table(data, "contract")),
        lunch=_build_lunch(_table(data, "lunch")),
        credit=_build_credit(_table(data, "credit")),
        absence=_build_absence(_table(data, "absence")),
        drift=_build_drift(_table(data, "drift")),
        collector=_build_collector(_table(data, "collector")),
        notify=_build_notify(_table(data, "notify")),
        display=_build_display(_table(data, "display")),
        profiles=_build_profiles(_table(data, "profiles")),
        power=_build_power(_table(data, "power")),
    )


def load_config() -> Config:
    defaults = _load_defaults()
    overlay = _load_overlay()
    merged = _merge(defaults, overlay) if overlay else defaults
    return _build_config(merged)
