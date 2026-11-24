"""Per-controller temperature ranges and programme catalogues.

The vendor app dispatches on ``version.pid[2:4]`` plus the wire node type and
then uses that controller's own setpoint and programme limits. This module is
the neutral, Home-Assistant-free home for those decoded facts. The integration
adapter consumes :func:`resolve_profile`; entity ids, services and HA types
stay in the integration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Profile = Literal["frost", "eco", "comfort", "off"]

PROFILES_BASE: tuple[Profile, ...] = ("frost", "eco", "comfort")
PROFILES_WITH_OFF: tuple[Profile, ...] = ("frost", "eco", "comfort", "off")
PROFILES_HTR_MOD: tuple[Profile, ...] = ("off", "frost", "eco", "comfort")

# The mobile UI's absolute floor. A controller with a higher minimum keeps it.
UI_MIN_TEMP_C = 1.0

# Family code sets from EProductCodeValue.
_HELKI_HEATER_CODES = frozenset({"32", "35", "33", "38", "36"})
_LHZ_HEATER_CODES = frozenset({"35", "33", "38", "36"})
_HAVERLAND_CODES = frozenset({"17", "1D", "47"})
_HAVERLAND_HTR_MOD_CODES = frozenset({"0A", "FF"})
_TEVOLVE_TRIAC_CODES = frozenset({"1A", "21", "23", "27", "28"})
_TEVOLVE_WIFI_CODES = frozenset({"31", "34", "39"})
_TEVOLVE_ATC_CODES = frozenset({"4C", "4D"})
_TEVOLVE_THERMOSTAT_CODES = frozenset({"28"})
_TEVOLVE_RELAY_CODES = frozenset({"1E"})
_HELKI_STORAGE_CODES = frozenset({"1C", "2E"})
_ELNUR_STORAGE_CODES = frozenset({"14", "20", "25", "2A"})
_THERMOSTAT_CODES = frozenset({"04", "09", "0F"})


@dataclass(frozen=True)
class ControllerLimits:
    """Setpoint and programme limits for one controller family, in Celsius."""

    min_c: float
    max_c: float
    ice_min_c: float | None = None
    ice_max_c: float | None = None
    profile_offset_c: float = 0.5
    comfort_eco_offset_c: float | None = None
    ice_editable: bool = True
    profiles: tuple[Profile, ...] = PROFILES_BASE
    htr_mod: bool = False
    max_limit_max_c: float | None = None
    boost_max_c: float | None = None
    boost_max_minutes: int = 1440
    prog_min_c: float | None = None
    prog_max_c: float | None = None


_BASE_LIMITS = ControllerLimits(min_c=0.0, max_c=30.0)
_HAVERLAND_LIMITS = ControllerLimits(
    min_c=5.0,
    max_c=30.0,
    ice_min_c=5.0,
    ice_max_c=7.0,
    profile_offset_c=2.0,
    profiles=PROFILES_WITH_OFF,
)
_HTR_MOD_LIMITS = ControllerLimits(
    min_c=5.0,
    max_c=28.0,
    profile_offset_c=1.0,
    profiles=PROFILES_HTR_MOD,
    htr_mod=True,
    ice_editable=False,
)
_TEVOLVE_TRIAC_LIMITS = ControllerLimits(min_c=7.0, max_c=35.0)
_TEVOLVE_PIR_LIMITS = ControllerLimits(
    min_c=7.0,
    max_c=35.0,
    comfort_eco_offset_c=3.5,
    ice_editable=False,
)
_TEVOLVE_WIFI_LIMITS = _TEVOLVE_TRIAC_LIMITS
_TEVOLVE_ATC_LIMITS = ControllerLimits(
    min_c=5.0, max_c=30.0, ice_min_c=5.0, ice_max_c=10.0, boost_max_minutes=120
)
_TEVOLVE_RELAY_LIMITS = ControllerLimits(min_c=0.0, max_c=35.0)
_ELNUR_STORAGE_LIMITS = ControllerLimits(min_c=3.0, max_c=30.0)
_THERMOSTAT_LIMITS = ControllerLimits(min_c=7.0, max_c=35.0)
_HELKI_LIMITS = ControllerLimits(min_c=0.0, max_c=30.0, profiles=PROFILES_WITH_OFF)
_CLIMASTAR_TRIAC_LIMITS = ControllerLimits(min_c=0.0, max_c=30.0, boost_max_minutes=300)

_CODE_LIMITS: dict[str, ControllerLimits] = {
    **dict.fromkeys(_HELKI_HEATER_CODES, _HELKI_LIMITS),
    **dict.fromkeys(_HAVERLAND_CODES, _HAVERLAND_LIMITS),
    **dict.fromkeys(_TEVOLVE_TRIAC_CODES, _TEVOLVE_TRIAC_LIMITS),
    "27": _TEVOLVE_PIR_LIMITS,
    **dict.fromkeys(_TEVOLVE_WIFI_CODES, _TEVOLVE_WIFI_LIMITS),
    **dict.fromkeys(_TEVOLVE_ATC_CODES, _TEVOLVE_ATC_LIMITS),
    "3D": _CLIMASTAR_TRIAC_LIMITS,
    **dict.fromkeys(_TEVOLVE_RELAY_CODES, _TEVOLVE_RELAY_LIMITS),
    **dict.fromkeys(_ELNUR_STORAGE_CODES, _ELNUR_STORAGE_LIMITS),
    **dict.fromkeys(_THERMOSTAT_CODES, _THERMOSTAT_LIMITS),
    **dict.fromkeys(
        (
            "01",
            "02",
            "07",
            "08",
            "0B",
            "0C",
            "0D",
            "10",
            "11",
            "12",
            "13",
            "16",
            "18",
            "1B",
            "1F",
            "24",
            "43",
            "FF",
        ),
        _BASE_LIMITS,
    ),
}

_PID_CODE_START = 2
_PID_CODE_END = 4


def product_code(pid: str | None) -> str | None:
    """Return ``pid[2:4]`` uppercased, matching the app's dispatch."""
    if not isinstance(pid, str):
        return None
    value = pid.strip().upper()
    return value[_PID_CODE_START:_PID_CODE_END] if len(value) >= _PID_CODE_END else None


def _setup_number(setup: Mapping[str, object], key: str) -> float | None:
    """Read one numeric setup field, tolerating wire strings."""
    value = setup.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value))
    except TypeError, ValueError:
        return None


def firmware_at_least(version: str | None, target: str) -> bool:
    """Compare dotted firmware versions numerically (missing parts are 0)."""
    if not isinstance(version, str):
        return False

    def parts(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in value.split(".") if part != "")
        except ValueError:
            return ()

    left = parts(version)
    right = parts(target)
    if not left:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) >= right + (0,) * (width - len(right))


def _helki_storage_limits(
    fw_version: str | None, setup: Mapping[str, object]
) -> ControllerLimits:
    """Return the Helki storage profile: setup bounds and fw-gated profiles."""
    min_c = _setup_number(setup, "min_stemp")
    max_c = _setup_number(setup, "max_stemp")
    profiles = (
        PROFILES_WITH_OFF if firmware_at_least(fw_version, "1.8") else PROFILES_BASE
    )
    return ControllerLimits(
        min_c=min_c if min_c is not None else 5.0,
        max_c=max_c if max_c is not None else 30.0,
        profiles=profiles,
    )


def is_htr_mod_controller(pid: str | None, node_type: str | None = None) -> bool:
    """Return True for the product codes that use htr_mod control."""
    code = product_code(pid)
    if code == "0A":
        return True
    return code == "FF" and node_type == "htr_mod"


SENSOR_MODE_NO_PROBES = 3


def has_no_probes(setup: Mapping[str, object] | None) -> bool:
    """Return True when the node's extra options select NO_PROBES."""
    if not isinstance(setup, Mapping):
        return False
    extra = setup.get("extra_options")
    if not isinstance(extra, Mapping):
        return False
    value = extra.get("sensor_mode")
    return not isinstance(value, bool) and value == SENSOR_MODE_NO_PROBES


def controller_limits(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object] | None = None,
) -> ControllerLimits:
    """Resolve the decoded controller limits for one node."""
    setup = setup or {}
    code = product_code(pid)
    if is_htr_mod_controller(pid, node_type):
        return _HTR_MOD_LIMITS
    if code in _HELKI_STORAGE_CODES:
        return _helki_storage_limits(fw_version, setup)
    limits = _CODE_LIMITS.get(code or "")
    if limits is not None:
        return limits
    if node_type == "acm":
        return _helki_storage_limits(fw_version, setup)
    return _BASE_LIMITS


def climate_bounds(
    limits: ControllerLimits, max_limit_c: float | None = None
) -> tuple[float, float]:
    """Return the climate target range: the 1 °C UI floor over the controller."""
    minimum = max(UI_MIN_TEMP_C, limits.min_c)
    maximum = limits.max_c
    if max_limit_c is not None and max_limit_c < maximum:
        maximum = max_limit_c
    return minimum, maximum


def max_limit_bounds(limits: ControllerLimits) -> tuple[float, float]:
    """Return the editable max-stemp range."""
    floor = limits.min_c + 1.0
    minimum = max(16.0, floor)
    ceiling = (
        limits.max_limit_max_c if limits.max_limit_max_c is not None else limits.max_c
    )
    return minimum, ceiling


def boost_temperature_bounds(limits: ControllerLimits) -> tuple[float, float]:
    """Return the editable boost-temperature range."""
    ceiling = limits.boost_max_c if limits.boost_max_c is not None else limits.max_c
    return 5.0, ceiling


def _profile_ranges(
    limits: ControllerLimits, max_limit_c: float | None = None
) -> dict[str, tuple[float, float]]:
    """Return the programme ranges in Celsius, keyed by profile.

    The vendor app applies ``max_stemp_limit`` as the programme ceiling (it
    replaces ``maxCTemp`` in ``getUserDefinedTempLimits``), so the energy
    offsets are subtracted from that ceiling when the node has one.
    """
    offset = limits.profile_offset_c
    prog_min = limits.prog_min_c if limits.prog_min_c is not None else limits.min_c
    prog_max = limits.prog_max_c if limits.prog_max_c is not None else limits.max_c
    if max_limit_c is not None and max_limit_c < prog_max:
        prog_max = max_limit_c
    ice_min = limits.ice_min_c if limits.ice_min_c is not None else prog_min
    ice_max = (
        limits.ice_max_c if limits.ice_max_c is not None else prog_max - 2.0 * offset
    )
    gap = limits.comfort_eco_offset_c
    eco_max = prog_max - (gap if gap is not None else offset)
    comfort_min = prog_min + (gap + offset if gap is not None else 2.0 * offset)
    return {
        "frost": (ice_min, ice_max),
        "eco": (prog_min + offset, eco_max),
        "comfort": (comfort_min, prog_max),
    }


def profile_bounds(
    limits: ControllerLimits,
    profile: str,
    *,
    max_limit_c: float | None = None,
) -> tuple[float, float] | None:
    """Return ``(min, max)`` Celsius for one editable programme profile."""
    if profile == "off":
        return None
    if limits.htr_mod:
        return {"eco": (10.0, 18.0), "comfort": (18.0, 28.0)}.get(profile)
    if profile == "frost" and not limits.ice_editable:
        return None
    return _profile_ranges(limits, max_limit_c).get(profile)


def prog_bounds_c(
    limits: ControllerLimits, *, max_limit_c: float | None = None
) -> dict[str, tuple[float, float]] | None:
    """Return the write-sanitizer bounds in Celsius, or None for htr_mod."""
    if limits.htr_mod:
        return None
    names = {"ice_temp": "frost", "eco_temp": "eco", "comf_temp": "comfort"}
    ranges = _profile_ranges(limits, max_limit_c)
    return {key: ranges[profile] for key, profile in names.items()}


# The factory dispatches these THM products to an htr-family controller.
# Code 28 is TevolveThermostatController, which extends the triac heater and
# uses the heater int-slot programme instead of the thermostat {m,t} shape.
HEATER_SURFACE_THM_CODES = frozenset({"28"})


def is_heater_surface(node_type: str, pid: str | None) -> bool:
    """Whether the node gets heater entities, including the THM 28 floor unit."""
    if node_type in {"htr", "htr_mod", "acm"}:
        return True
    return node_type == "thm" and product_code(pid) in HEATER_SURFACE_THM_CODES


def is_thermostat_surface(node_type: str, pid: str | None) -> bool:
    """Whether the node gets the thermostat climate/schedule surfaces."""
    return node_type == "thm" and not is_heater_surface(node_type, pid)


def is_helki_heater_family(pid: str | None) -> bool:
    """Return True for the HelkiHeater/Lhz controller chain."""
    return product_code(pid) in _HELKI_HEATER_CODES


def is_lhz_family(pid: str | None) -> bool:
    """Return True for the controllers that hard-code 0/80/90 duty presets."""
    return product_code(pid) in _LHZ_HEATER_CODES


def is_lhz_relay(pid: str | None) -> bool:
    """Return True for the LhzRelayHeaterController (code 36)."""
    return product_code(pid) == "36"


def is_tevolve_triac_family(pid: str | None) -> bool:
    """Return True for Tevolve triac/towel-rail/PIR controllers."""
    return product_code(pid) in _TEVOLVE_TRIAC_CODES


def is_tevolve_wifi_family(pid: str | None) -> bool:
    """Return True for the Tevolve WiFi controller chain."""
    return product_code(pid) in _TEVOLVE_WIFI_CODES


def is_tevolve_atc_family(pid: str | None) -> bool:
    """Return True for the Tevolve/Ferroli ATC controllers."""
    return product_code(pid) in _TEVOLVE_ATC_CODES


@dataclass(frozen=True)
class ControllerProfile:
    """The subset of a resolved controller that the adapter consumes today."""

    code: str | None
    limits: ControllerLimits
    climate_bounds: tuple[float, float]
    max_limit_bounds: tuple[float, float]
    boost_bounds: tuple[float, float]
    prog_bounds: dict[str, tuple[float, float]] | None


def resolve_profile(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object] | None = None,
    max_limit_c: float | None = None,
) -> ControllerProfile:
    """Resolve one controller profile from identity and setup only.

    Live status is deliberately not an input: a node lost at discovery must
    resolve the same profile. Runtime capability gates stay in the adapter.
    """
    limits = controller_limits(node_type, pid, fw_version, setup)
    return ControllerProfile(
        code=product_code(pid),
        limits=limits,
        climate_bounds=climate_bounds(limits, max_limit_c),
        max_limit_bounds=max_limit_bounds(limits),
        boost_bounds=boost_temperature_bounds(limits),
        prog_bounds=prog_bounds_c(limits, max_limit_c=max_limit_c),
    )


__all__ = [
    "ELNUR_STORAGE_CODES",
    "HAVERLAND_CODES",
    "HAVERLAND_FIXED_TIME_CODES",
    "HAVERLAND_HTR_MOD_CODES",
    "HEATER_SURFACE_THM_CODES",
    "HELKI_HEATER_CODES",
    "HELKI_STORAGE_CODES",
    "LHZ_HEATER_CODES",
    "PROFILES_BASE",
    "PROFILES_HTR_MOD",
    "PROFILES_WITH_OFF",
    "TEVOLVE_ATC_CODES",
    "TEVOLVE_RELAY_CODES",
    "TEVOLVE_THERMOSTAT_CODES",
    "TEVOLVE_TRIAC_CODES",
    "TEVOLVE_WIFI_CODES",
    "THERMOSTAT_CODES",
    "UI_MIN_TEMP_C",
    "ControllerLimits",
    "ControllerProfile",
    "boost_temperature_bounds",
    "climate_bounds",
    "controller_limits",
    "firmware_at_least",
    "is_heater_surface",
    "is_helki_heater_family",
    "is_htr_mod_controller",
    "is_lhz_family",
    "is_lhz_relay",
    "is_tevolve_atc_family",
    "is_tevolve_triac_family",
    "is_tevolve_wifi_family",
    "is_thermostat_surface",
    "max_limit_bounds",
    "product_code",
    "profile_bounds",
    "prog_bounds_c",
    "resolve_profile",
]

# Public family sets: the adapter's lock/boost/surface gates still consume
# these until their own slices move.
HELKI_HEATER_CODES = _HELKI_HEATER_CODES
LHZ_HEATER_CODES = _LHZ_HEATER_CODES
HAVERLAND_CODES = _HAVERLAND_CODES
HAVERLAND_HTR_MOD_CODES = _HAVERLAND_HTR_MOD_CODES
HAVERLAND_FIXED_TIME_CODES = _HAVERLAND_CODES
TEVOLVE_TRIAC_CODES = _TEVOLVE_TRIAC_CODES
TEVOLVE_WIFI_CODES = _TEVOLVE_WIFI_CODES
TEVOLVE_ATC_CODES = _TEVOLVE_ATC_CODES
TEVOLVE_THERMOSTAT_CODES = _TEVOLVE_THERMOSTAT_CODES
TEVOLVE_RELAY_CODES = _TEVOLVE_RELAY_CODES
HELKI_STORAGE_CODES = _HELKI_STORAGE_CODES
ELNUR_STORAGE_CODES = _ELNUR_STORAGE_CODES
THERMOSTAT_CODES = _THERMOSTAT_CODES
