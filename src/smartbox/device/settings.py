"""Neutral entity-setting gates decoded from the vendor app.

These predicates take the identity facts (node type, product code, firmware,
setup and the box capability) instead of a Home Assistant node object. The
integration's ``identity`` module wraps them for its frozen
``SmartboxNodeInfo``.


Decoded from the vendor 2.54.2 bundle; see `hass-smartbox docs/api-contract.md`
(the wire record) for the provenance citations.
"""

from __future__ import annotations

from collections.abc import Mapping

from smartbox.core.merging import boost_config
from smartbox.protocol.values import BoostConfig, parse_number_or_none

from .codes import accepts_extra_options
from .profile import (
    controller_limits,
    firmware_at_least,
    has_no_probes,
    is_heater_surface,
    is_helki_heater_family,
    is_htr_mod_controller,
    is_lhz_family,
    is_lhz_relay,
    is_tevolve_atc_family,
    is_tevolve_triac_family,
    is_tevolve_wifi_family,
    product_code,
)
from .storage import (
    UI_MODE_CLASSIC_ACCUMULATOR,
    UI_MODE_CLASSIC_COMBINED,
    boost_supported as storage_boost_supported,
    is_elnur_storage,
    is_water_storage,
    ui_mode,
)


def _factory_options(setup: Mapping[str, object]) -> dict[str, object]:
    factory = setup.get("factory_options")
    return dict(factory) if isinstance(factory, Mapping) else {}


def _floor_thermostat_no_probes(
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> bool:
    """TevolveThermostatController hides its probe-dependent settings."""
    return (
        node_type == "thm"
        and is_heater_surface(node_type, pid)
        and has_no_probes(setup)
    )


def _elnur_classic_ui(pid: str | None, setup: Mapping[str, object]) -> bool:
    """ElnurStorage hides radiance/window settings in the classic UI modes."""
    return is_elnur_storage(pid) and ui_mode(setup) in {
        UI_MODE_CLASSIC_COMBINED,
        UI_MODE_CLASSIC_ACCUMULATOR,
    }


def window_mode_available(
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> bool:
    """Whether the window-mode switch applies to this node."""
    if _floor_thermostat_no_probes(node_type, pid, setup):
        return False
    if is_water_storage(pid) or _elnur_classic_ui(pid, setup):
        return False
    return bool(_factory_options(setup).get("window_mode_available", False))


def true_radiant_available(
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> bool:
    """Whether the true-radiant switch applies to this node."""
    if _floor_thermostat_no_probes(node_type, pid, setup):
        return False
    if is_water_storage(pid) or _elnur_classic_ui(pid, setup):
        return False
    return bool(_factory_options(setup).get("true_radiant_available", False))


def away_offset_available(
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> bool:
    """Whether the away-offset number applies to this node."""
    if is_htr_mod_controller(pid, node_type):
        return False
    if _floor_thermostat_no_probes(node_type, pid, setup) or is_water_storage(pid):
        return False
    return "away_offset" in setup


# The app's max-stemp gate also hides the setting for this exact full PID
# (`MAX_STEMP_LIMIT_UNSUPPORTED_PIDS.includes(version.pid.toUpperCase())`).
_MAX_STEMP_UNSUPPORTED_PIDS = frozenset({"0A01"})


def max_temp_limit_available(  # noqa: PLR0911 - one return per family gate
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> bool:
    """Whether the maximum target temperature limit is configurable."""
    if is_htr_mod_controller(pid, node_type):
        return False
    if not isinstance(pid, str) or not pid:
        return False
    if pid.upper() in _MAX_STEMP_UNSUPPORTED_PIDS:
        return False
    if not is_heater_surface(node_type, pid):
        return False
    if node_type == "acm":
        return False
    if is_tevolve_triac_family(pid) or is_tevolve_wifi_family(pid):
        return False
    if product_code(pid) == "1D":
        return False
    return "max_stemp_limit" in setup


def node_power_limit_available(setup: Mapping[str, object]) -> bool:
    """Whether the node's own power limit is configurable."""
    return "power_limit" in setup


DUTY_LIMIT_OFF = "Off"

_DUTY_LIMIT_PRESETS = (
    ("LST", "lst_available", "lst_value"),
    ("Baby Care", "bbc_available", "bbc_value"),
)
_DUTY_LIMIT_70 = 70


def _percent(value: object) -> int | None:
    number = parse_number_or_none(value)
    if number is None or number <= 0:
        return None
    return int(number)


def duty_limit_value(setup: Mapping[str, object]) -> int | None:
    """Return the stored power-limit factor, or None when absent or unreadable."""
    extra = setup.get("extra_options")
    raw = extra.get("user_duty_limit") if isinstance(extra, Mapping) else None
    number = parse_number_or_none(raw)
    return None if number is None else int(number)


def duty_limit_options(  # noqa: PLR0911 - one branch per family/setup shape
    setup: Mapping[str, object], pid: str | None = None
) -> tuple[tuple[str, int], ...]:
    """Return the (label, value) power-limit-factor choices for a node setup."""
    if pid is None or not is_helki_heater_family(pid):
        return ()
    if not accepts_extra_options(pid):
        return ()
    extra = setup.get("extra_options")
    if not isinstance(extra, Mapping) or "user_duty_limit" not in extra:
        return ()
    power = parse_number_or_none(setup.get("power"))
    if not power:
        return ()
    factory = setup.get("factory_options")
    if not isinstance(factory, Mapping):
        return ((DUTY_LIMIT_OFF, 0),)

    choices: list[tuple[str, int]] = [(DUTY_LIMIT_OFF, 0)]
    seen = {0}

    def add(label: str, value: int | None) -> None:
        if value is None or value in seen:
            return
        seen.add(value)
        choices.append((label, value))

    lst_value = _percent(factory.get("lst_value"))
    bbc_value = _percent(factory.get("bbc_value"))
    if is_lhz_family(pid):
        return _lhz_duty_choices(factory, choices, seen, lst_value, bbc_value)

    for label, available_key, value_key in _DUTY_LIMIT_PRESETS:
        if not factory.get(available_key):
            continue
        add(label, _percent(factory.get(value_key)))
    return _sorted_duty(choices)


def _sorted_duty(
    choices: list[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(choices, key=lambda choice: choice[1]))


def _lhz_duty_choices(
    factory: Mapping[str, object],
    choices: list[tuple[str, int]],
    seen: set[int],
    lst_value: int | None,
    bbc_value: int | None,
) -> tuple[tuple[str, int], ...]:
    def add(label: str, value: int | None) -> None:
        if value is None or value in seen:
            return
        seen.add(value)
        choices.append((label, value))

    add("80 %", 80)
    add("90 %", 90)
    if lst_value != _DUTY_LIMIT_70:
        add("70 %", _DUTY_LIMIT_70)
    if factory.get("bbc_available"):
        add("Baby Care", bbc_value)
    if factory.get("lst_available"):
        add("LST", lst_value)
    return _sorted_duty(choices)


def duty_limit_available(pid: str | None, setup: Mapping[str, object]) -> bool:
    """Whether the power-limit select is worth exposing on this node."""
    if pid is None or not is_helki_heater_family(pid):
        return False
    return len(duty_limit_options(setup, pid)) > 1


def priority_available(
    setup: Mapping[str, object], *, box_power_limit_available: bool
) -> bool:
    """Whether the radiator priority select applies on this node."""
    return "priority" in setup and box_power_limit_available


def prog_temps_available(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object],
) -> bool:
    """Whether the Frost/Eco/Comfort programme temperatures are settable."""
    if node_type not in {"htr", "acm"} and not (
        node_type == "thm" and is_heater_surface("thm", pid)
    ):
        return False
    limits = controller_limits(node_type, pid, fw_version, setup)
    if limits.htr_mod:
        return False
    if (
        node_type == "acm"
        and ui_mode(setup) == UI_MODE_CLASSIC_ACCUMULATOR
        and is_elnur_storage(pid)
    ):
        return False
    if node_type == "acm" and is_water_storage(pid):
        return False
    if node_type == "thm" and is_heater_surface("thm", pid) and has_no_probes(setup):
        return False
    return all(profile in limits.profiles for profile in ("frost", "eco", "comfort"))


def _storage_boost_available(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    brand_name: str | None,
) -> bool:
    """Helki storage's /boost path plus its Electrorad/Evconfort fw gate."""
    if not storage_boost_supported(node_type, pid):
        return False
    legacy_brand = isinstance(brand_name, str) and brand_name.strip().lower() in {
        "electrorad",
        "evconfort",
    }
    return not (legacy_brand and not firmware_at_least(fw_version, "1.8"))


def _heater_boost_available(  # noqa: PLR0911 - one return per controller family
    setup: Mapping[str, object],
    node_type: str | None,
    pid: str | None,
) -> bool:
    """Return the heater-controller BOOST rules from the 2.54.2 mode lists."""
    code = product_code(pid)
    if node_type == "htr_mod" and is_htr_mod_controller(pid, node_type):
        return False
    if code == "28" and has_no_probes(setup):
        return False
    if code == "17":
        return True
    if code in {"1D", "47"}:
        return boost_config(setup) != BoostConfig.MAX_TEMP_FIXED_TIME
    if is_lhz_relay(pid):
        return True
    if code == "3D" or is_helki_heater_family(pid):
        return boost_config(setup) == BoostConfig.SET_TEMP_AND_TIME
    if is_tevolve_triac_family(pid):
        return code != "27"
    if is_tevolve_wifi_family(pid) or is_tevolve_atc_family(pid):
        return boost_config(setup) != BoostConfig.UNSUPPORTED
    return False


def boost_available(
    setup: Mapping[str, object],
    *,
    node_type: str | None = None,
    pid: str | None = None,
    fw_version: str | None = None,
    brand_name: str | None = None,
) -> bool:
    """Whether this controller advertises a usable boost mode."""
    if node_type == "acm":
        return _storage_boost_available(node_type, pid, fw_version, brand_name)
    return _heater_boost_available(setup, node_type, pid)
