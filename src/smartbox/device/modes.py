"""Neutral lock and boost policy for decoded controllers.

These gates depend on the product code, firmware and setup only; the adapter
uses them to choose entity shapes and request paths. Storage semantics live in
``device.storage`` and the surface predicates in ``device.profile``.


Decoded from the vendor 2.54.2 bundle; see `hass-smartbox docs/api-contract.md`
(the wire record) for the provenance citations.
"""

from __future__ import annotations

from collections.abc import Mapping

from smartbox.core.merging import boost_config
from smartbox.protocol.values import BoostConfig

from .profile import (
    ELNUR_STORAGE_CODES,
    HAVERLAND_FIXED_TIME_CODES,
    HELKI_HEATER_CODES,
    HELKI_STORAGE_CODES,
    TEVOLVE_ATC_CODES,
    TEVOLVE_THERMOSTAT_CODES,
    TEVOLVE_TRIAC_CODES,
    TEVOLVE_WIFI_CODES,
    firmware_at_least,
    has_no_probes,
    product_code,
)

_EASY_CODES = TEVOLVE_TRIAC_CODES | TEVOLVE_WIFI_CODES
_RUNBACK_TRIAC_CODES = TEVOLVE_TRIAC_CODES - {"27"}


def lock_kind(
    node_type: str,
    pid: str | None,
    fw_version: str | None,
    setup: Mapping[str, object] | None = None,
) -> str:
    """Return the lock payload kind the controller family uses.

    ``int`` means the status field is 0/1/2 (UNBLOCKED / LOCALLY_BLOCKED /
    REMOTELY_BLOCKED) and the write must carry an integer. ``bool`` is the
    base ElectricHeater/Haverland settings path. Storage, the WiFi/ATC chain,
    code 47 and Haverland 1D at fw 1.7+ always write integers; the Helki/Lhz
    heater chain follows the app's ``getKeyboardLockPayload``, which writes
    integers only when ``factory_options.super_lock_available`` is set. The
    family decides; the live status type only decodes values, so a lost node
    at discovery does not switch a known integer-lock unit to a boolean write.
    """
    code = product_code(pid)
    if (
        node_type == "acm"
        or code in HELKI_STORAGE_CODES
        or code in ELNUR_STORAGE_CODES
        or code in TEVOLVE_WIFI_CODES
        or code in TEVOLVE_ATC_CODES
        or code == "47"
    ):
        return "int"
    if code == "1D":
        return "int" if firmware_at_least(fw_version, "1.7") else "bool"
    if code in HELKI_HEATER_CODES:
        factory = (setup or {}).get("factory_options")
        super_lock = isinstance(factory, Mapping) and bool(
            factory.get("super_lock_available")
        )
        return "int" if super_lock else "bool"
    return "bool"


def supports_easy(pid: str | None, setup: Mapping[str, object] | None = None) -> bool:
    """Return True for the controllers that expose the EASY flag.

    Tevolve triac/towel-rail/PIR and WiFi keep EASY; the ATC subclasses remove
    it, and the factory-default controllers have no such setting. The Tevolve
    thermostat (28) also drops its EASY/RUNBACK settings when the controller
    reports NO_PROBES (timer mode).
    """
    code = product_code(pid)
    if code in TEVOLVE_THERMOSTAT_CODES and has_no_probes(setup):
        return False
    return code in _EASY_CODES


def boost_uses_legacy_path(
    pid: str | None, setup: Mapping[str, object] | None = None
) -> bool:
    """Whether the controller's boost activation is the setup+status path.

    TevolveTriacHeaterController overrides activateBoost/deactivateBoost and
    always uses the legacy path, including when boost_config is FULL (3). The
    Haverland fixed-time families inherit ``setBasicBoost``: at boost_config
    0-2 their activation is the status flag with no temperature/duration. At
    FULL (3) the generic controller enters ``/boost_mode`` instead.
    """
    code = product_code(pid)
    if code in TEVOLVE_TRIAC_CODES and code != "27":
        return True
    if code in HAVERLAND_FIXED_TIME_CODES:
        return boost_config(setup or {}) != BoostConfig.FULL
    return False


def boost_params_editable(  # noqa: PLR0911 - one return per family/config
    node_type: str, pid: str | None, setup: Mapping[str, object] | None
) -> bool:
    """Whether the app exposes editable boost temperature/duration.

    ``boost_config`` absent is not fixed-time: the base controller falls back
    to SET_TEMP_AND_TIME when its BOOST mode is available. Storage (Helki),
    LhzRelay 36 and the Tevolve triac/towel-rail family add BOOST
    unconditionally. The triac family and storage also treat explicit FULL (3)
    as temp-and-time editable. The Haverland fixed-time families (17/1D/47)
    never expose parameters: their app activation calls ``setBasicBoost``.
    The WiFi families expose set-temp-and-time only with config 2, and the
    Tevolve thermostat's NO_PROBES timer mode has no BOOST at all.
    """
    code = product_code(pid)
    if node_type == "acm":
        # Storage BOOST is the Helki /boost endpoint, independent of
        # boost_config; the Elnur families have no BOOST surface.
        return code not in ELNUR_STORAGE_CODES
    if code in HAVERLAND_FIXED_TIME_CODES:
        return False
    if code in TEVOLVE_THERMOSTAT_CODES and has_no_probes(setup):
        return False
    factory = (setup or {}).get("factory_options")
    explicit = isinstance(factory, Mapping) and "boost_config" in factory
    config = boost_config(setup or {})
    if explicit:
        if config == BoostConfig.SET_TEMP_AND_TIME:
            return True
        if config == BoostConfig.FULL:
            return node_type == "acm" or (code in TEVOLVE_TRIAC_CODES and code != "27")
        return False
    if code == "36":
        return True
    return code in TEVOLVE_TRIAC_CODES and code != "27"


def boost_activation_takes_parameters(
    pid: str | None, setup: Mapping[str, object] | None
) -> bool:
    """Whether activation should carry the stored boost temperature/duration."""
    code = product_code(pid)
    if code in HAVERLAND_FIXED_TIME_CODES:
        return False
    return boost_config(setup or {}) != BoostConfig.MAX_TEMP_FIXED_TIME


def supports_runback(
    pid: str | None, setup: Mapping[str, object] | None = None
) -> bool:
    """Return True for controllers that expose the RUNBACK flag.

    Tevolve triac/towel-rail keep it; PIR removes it. WiFi/ATC keep it when the
    boost config is not FULL. On FULL the app routes activation through
    ``/boost_mode`` with a temperature and duration the switch does not carry,
    so the switch is withheld rather than posting the wrong status-only write.
    The Tevolve thermostat hides EASY/RUNBACK when it reports NO_PROBES.
    """
    code = product_code(pid)
    if code in TEVOLVE_THERMOSTAT_CODES and has_no_probes(setup):
        return False
    if code in _RUNBACK_TRIAC_CODES:
        return True
    if code in TEVOLVE_WIFI_CODES or code in TEVOLVE_ATC_CODES:
        return boost_config(setup or {}) in {
            BoostConfig.SET_TEMP_AND_TIME,
            BoostConfig.MAX_TEMP_FIXED_TIME,
        }
    return False
