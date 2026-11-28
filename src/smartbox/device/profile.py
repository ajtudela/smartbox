"""Per-controller temperature ranges and programme catalogues.

The vendor app dispatches on ``version.pid[2:4]`` plus the wire node type and
then uses that controller's own setpoint and programme limits. This module is
the neutral, Home-Assistant-free home for those decoded facts. The integration
adapter consumes :func:`resolve_profile`; entity ids, services and HA types
stay in the integration.


Decoded from the vendor 2.54.2 bundle; see `hass-smartbox docs/api-contract.md`
(the wire record) for the provenance citations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .codes import (
    PRODUCT_CODES,
    LimitsFamily,
    code_info,
    codes_for_limits,
    has_heating_switch_case,
    product_code,
)
from .setup import SetupView

Profile = Literal["frost", "eco", "comfort", "off"]

PROFILES_BASE: tuple[Profile, ...] = ("frost", "eco", "comfort")
PROFILES_WITH_OFF: tuple[Profile, ...] = ("frost", "eco", "comfort", "off")
PROFILES_HTR_MOD: tuple[Profile, ...] = ("off", "frost", "eco", "comfort")

# The mobile UI's absolute floor. A controller with a higher minimum keeps it.
UI_MIN_TEMP_C = 1.0

# Family sets derived from the reviewed product-code table.
_HELKI_HEATER_CODES = codes_for_limits("helki")
_LHZ_HEATER_CODES = frozenset(
    code for code, info in PRODUCT_CODES.items() if info.lhz_duty
)
_HAVERLAND_CODES = codes_for_limits("haverland")
_HAVERLAND_FIXED_TIME_CODES = frozenset(
    code for code, info in PRODUCT_CODES.items() if info.fixed_time_boost
)
_HAVERLAND_HTR_MOD_CODES = codes_for_limits("htr_mod")
_TEVOLVE_TRIAC_CODES = (
    codes_for_limits("triac") | codes_for_limits("pir") | codes_for_limits("thm28")
)
_TEVOLVE_WIFI_CODES = codes_for_limits("wifi")
_TEVOLVE_ATC_CODES = codes_for_limits("atc")
_TEVOLVE_THERMOSTAT_CODES = codes_for_limits("thm28")
_TEVOLVE_RELAY_CODES = codes_for_limits("relay")
_HELKI_STORAGE_CODES = codes_for_limits("storage_helki")
_ELNUR_STORAGE_CODES = codes_for_limits("storage_elnur") | codes_for_limits("water")
_THERMOSTAT_CODES = codes_for_limits("thermostat")


@dataclass(frozen=True)
class ControllerLimits:
    """Setpoint and programme limits for one controller family, in Celsius."""

    min_c: float
    max_c: float
    ice_min_c: float | None = None
    ice_max_c: float | None = None
    profile_offset_c: float = 0.5
    setpoint_step_c: float = 0.5
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
HTR_MOD_SETPOINT_STEP_C = 1.0

_HTR_MOD_LIMITS = ControllerLimits(
    min_c=5.0,
    max_c=28.0,
    profile_offset_c=1.0,
    setpoint_step_c=HTR_MOD_SETPOINT_STEP_C,
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

_LIMITS_BY_FAMILY: dict[LimitsFamily, ControllerLimits] = {
    "base": _BASE_LIMITS,
    "helki": _HELKI_LIMITS,
    "haverland": _HAVERLAND_LIMITS,
    "htr_mod": _HTR_MOD_LIMITS,
    "triac": _TEVOLVE_TRIAC_LIMITS,
    "pir": _TEVOLVE_PIR_LIMITS,
    "thm28": _TEVOLVE_TRIAC_LIMITS,
    "wifi": _TEVOLVE_WIFI_LIMITS,
    "atc": _TEVOLVE_ATC_LIMITS,
    "relay": _TEVOLVE_RELAY_LIMITS,
    "climastar": _CLIMASTAR_TRIAC_LIMITS,
    "storage_helki": _HELKI_LIMITS,
    "storage_elnur": _ELNUR_STORAGE_LIMITS,
    "water": _ELNUR_STORAGE_LIMITS,
    "thermostat": _THERMOSTAT_LIMITS,
    "power": _BASE_LIMITS,
}
_CODE_LIMITS: dict[str, ControllerLimits] = {
    code: _LIMITS_BY_FAMILY[info.limits_family] for code, info in PRODUCT_CODES.items()
}


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


def is_htr_mod_code(code: str | None, node_type: str | None = None) -> bool:
    """Return True for a decoded product code that uses htr_mod control."""
    if code not in _HAVERLAND_HTR_MOD_CODES:
        return False
    if code == "0A":
        return True
    # FF is htr_mod only on the htr_mod wire; a plain htr is a base heater.
    return node_type == "htr_mod"


def is_htr_mod_controller(pid: str | None, node_type: str | None = None) -> bool:
    """Return True for the product codes that use htr_mod control."""
    return is_htr_mod_code(product_code(pid), node_type)


def is_htr_mod_node(code: str | None, node_type: str | None = None) -> bool:
    """Decide htr_mod from a decoded code, falling back to the wire type.

    A reviewed code always wins; an absent or unreviewed code keeps the
    discovery wire type so a missing PID does not change the entity surface.
    """
    if code is not None and code in PRODUCT_CODES:
        return is_htr_mod_code(code, node_type)
    return node_type == "htr_mod"


def controller_health_fields(
    pid: str | None, node_type: str | None = None
) -> frozenset[str]:
    """Return the controller-health fields a heater controller reports.

    ``pcb_temp`` and ``error_code`` are observed live on the htr/htr_mod
    families; the decoded apps never read either field. TRIAC controllers add
    ``act_duty`` and ``power_pcb_temp``; the WiFi heater product also reports
    ``power_fail_det``. Base relay heaters report only the common health pair.
    ``true_radiant_active`` is a base heater status indicator, not health
    telemetry; use :func:`smartbox.device.settings.true_radiant_indicator_available`
    for it.
    """
    if node_type not in ("htr", "htr_mod"):
        return frozenset()
    info = code_info(pid)
    if info is None:
        return frozenset()
    fields = {"error_code", "pcb_temp"}
    if info.actuation == "TRIAC":
        fields.update({"act_duty", "power_pcb_temp"})
    if info.code == "32":
        fields.add("power_fail_det")
    return frozenset(fields)


# ESensorModeValue.FLOOR_PROBE: the only sensor is the floor probe.
SENSOR_MODE_FLOOR_PROBE = 2
SENSOR_MODE_NO_PROBES = 3


def has_no_probes(setup: Mapping[str, object] | None) -> bool:
    """Return True when the node's extra options select NO_PROBES."""
    return SetupView(setup or {}).sensor_mode == SENSOR_MODE_NO_PROBES


def controller_limits(  # noqa: PLR0911 - one return per dispatch branch
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
    info = code_info(pid)
    unrouted = info is None or not has_heating_switch_case(info)
    if node_type == "acm" and unrouted:
        # The factory's heating switch has no UNKNOWN/power-meter case, so an
        # acm with one of those codes falls through to HelkiStorage.
        return _helki_storage_limits(fw_version, setup)
    if node_type == "thm" and unrouted:
        # The THM factory default is ThermostatController (7-35).
        return _THERMOSTAT_LIMITS
    limits = _CODE_LIMITS.get(code or "")
    if code == "FF":
        # FF's row records the htr_mod limits; a plain htr uses the factory's
        # ElectricHeater default.
        return _BASE_LIMITS
    if limits is not None:
        return limits
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
        return {
            "eco": (
                HAVERLAND_HTR_MOD_POLICY.eco_min_c,
                HAVERLAND_HTR_MOD_POLICY.eco_max_c,
            ),
            "comfort": HAVERLAND_HTR_MOD_POLICY.comfort_c,
        }.get(profile)
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
HEATER_SURFACE_THM_CODES = frozenset(
    code for code, info in PRODUCT_CODES.items() if info.thm_heater
)


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
class HtrModPolicy:
    """Setpoint grid and profile bounds for one htr_mod hardware policy.

    ``step_c`` is the controller's setpoint step in Celsius. The eco offset
    ladder is integral, so the offset search walks comfort in 1 °C steps.
    """

    step_c: float
    comfort_c: tuple[float, float]
    eco_max_c: float
    offset_c: tuple[int, int]
    global_min_c: float = 5.0

    @property
    def eco_min_c(self) -> float:
        """Return the eco floor: the comfort floor less the largest offset."""
        return self.comfort_c[0] - self.offset_c[1]


# HaverlandHtrModController: the only htr_mod controller in the 2.54.2 bundles.
HAVERLAND_HTR_MOD_POLICY = HtrModPolicy(
    step_c=HTR_MOD_SETPOINT_STEP_C,
    comfort_c=(18.0, 28.0),
    eco_max_c=18.0,
    offset_c=(4, 8),
)


def htr_mod_policy(model_code: str | None) -> HtrModPolicy | None:
    """Return the htr_mod setpoint policy for a product code, if any.

    "0A" (heater module) and "FF" (legacy heater) are the codes the vendor
    app's factory routes to its htr_mod controller. An unread version returns
    the Haverland policy, because every htr_mod controller in the 2.54.2
    bundles is that one; other codes keep the plain pass-through.
    """
    if model_code is None:
        return HAVERLAND_HTR_MOD_POLICY
    code = model_code.strip().upper()
    return HAVERLAND_HTR_MOD_POLICY if code in _HAVERLAND_HTR_MOD_CODES else None


@dataclass(frozen=True)
class ControllerProfile:
    """The subset of a resolved controller that the adapter consumes today."""

    code: str | None
    limits: ControllerLimits
    climate_bounds: tuple[float, float]
    max_limit_bounds: tuple[float, float]
    boost_bounds: tuple[float, float]
    prog_bounds: dict[str, tuple[float, float]] | None
    programme_kind: str  # profiles | htr_mod | thresholds | on_off_auto | none


def _setup_max_limit_c(setup: Mapping[str, object] | None) -> float | None:
    """Return the node's stored max-stemp ceiling, ignoring the 0 sentinel."""
    return SetupView(setup or {}).max_stemp_limit


def resolve_profile(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object] | None = None,
    max_limit_c: float | None = None,
) -> ControllerProfile:
    """Resolve one controller profile from identity and setup only.

    Live status is deliberately not an input: a node lost at discovery must
    resolve the same profile. ``max_limit_c`` overrides the programme ceiling
    for a proposed-limit preview; ``None`` keeps the controller catalogue.
    Use :func:`resolve_stored_profile` to apply the setup's stored
    ``max_stemp_limit``.
    """
    limits = controller_limits(node_type, pid, fw_version, setup)
    effective_max_limit = (
        max_limit_c if max_limit_c is not None and max_limit_c > 0 else None
    )
    code = product_code(pid)
    info = code_info(pid)
    if node_type == "pmo":
        programme_kind = "none"
    elif is_thermostat_surface(node_type, pid):
        programme_kind = "thresholds"
    elif node_type == "acm" and info is not None and info.water:
        programme_kind = "on_off_auto"
    elif limits.htr_mod:
        programme_kind = "htr_mod"
    else:
        programme_kind = "profiles"
    prog_bounds = (
        None
        if programme_kind in {"htr_mod", "thresholds", "on_off_auto", "none"}
        else prog_bounds_c(limits, max_limit_c=effective_max_limit)
    )
    return ControllerProfile(
        code=code,
        limits=limits,
        climate_bounds=climate_bounds(limits, effective_max_limit),
        max_limit_bounds=max_limit_bounds(limits),
        boost_bounds=boost_temperature_bounds(limits),
        prog_bounds=prog_bounds,
        programme_kind=programme_kind,
    )


def resolve_stored_profile(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object] | None = None,
) -> ControllerProfile:
    """Resolve a profile with the setup's stored ``max_stemp_limit`` applied.

    The wire field is Celsius; a non-positive value is the app's disabled
    sentinel and leaves the controller catalogue unclamped.
    """
    return resolve_profile(
        node_type,
        pid,
        fw_version,
        setup,
        max_limit_c=_setup_max_limit_c(setup),
    )


__all__ = [
    "ELNUR_STORAGE_CODES",
    "HAVERLAND_CODES",
    "HAVERLAND_FIXED_TIME_CODES",
    "HAVERLAND_HTR_MOD_CODES",
    "HAVERLAND_HTR_MOD_POLICY",
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
    "HtrModPolicy",
    "boost_temperature_bounds",
    "climate_bounds",
    "controller_health_fields",
    "controller_limits",
    "firmware_at_least",
    "htr_mod_policy",
    "is_heater_surface",
    "is_helki_heater_family",
    "is_htr_mod_code",
    "is_htr_mod_controller",
    "is_htr_mod_node",
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
    "resolve_stored_profile",
]

# Public family sets: the adapter's lock/boost/surface gates still consume
# these until their own slices move.
HELKI_HEATER_CODES = _HELKI_HEATER_CODES
LHZ_HEATER_CODES = _LHZ_HEATER_CODES
HAVERLAND_CODES = _HAVERLAND_CODES
HAVERLAND_HTR_MOD_CODES = _HAVERLAND_HTR_MOD_CODES
HAVERLAND_FIXED_TIME_CODES = _HAVERLAND_FIXED_TIME_CODES
TEVOLVE_TRIAC_CODES = _TEVOLVE_TRIAC_CODES
TEVOLVE_WIFI_CODES = _TEVOLVE_WIFI_CODES
TEVOLVE_ATC_CODES = _TEVOLVE_ATC_CODES
TEVOLVE_THERMOSTAT_CODES = _TEVOLVE_THERMOSTAT_CODES
TEVOLVE_RELAY_CODES = _TEVOLVE_RELAY_CODES
HELKI_STORAGE_CODES = _HELKI_STORAGE_CODES
ELNUR_STORAGE_CODES = _ELNUR_STORAGE_CODES
THERMOSTAT_CODES = _THERMOSTAT_CODES
