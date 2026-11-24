"""Per-product ``setup.control_mode`` maps from the decoded vendor app.

The wire value is the position in the controller's
``getIndexSortedUIHeatingControlModes()`` array. ``getUIHeatingControlModes()``
is the separate display/accepted list; the app converts a selected UI value to
the wire index with ``findIndex`` in the sorted array. Never use the UI list
order as the wire value.

The catalogue is derived from the decoded 2.54.2 manager bundle
(``new_vendors.decoded.js``): the product-code factory
(``NodeControllerManager.getHeatingNodeController``), the controller class
inheritance, and the two methods above. The wire record and provenance are in
``docs/api-contract.md`` §1.9. Three families are resolvable:

* htr: ``ElectricHeaterController`` and its subclasses. Wire order
  ``hyst_02, hyst_03, hyst_05, hyst_07, pid, pid_15, pid_30``.
* storage: ``HelkiStorageHeaterController`` (product codes ``1C`` and
  ``2E``). Wire order ``off, hyst_02, hyst_03, hyst_05, hyst_07, pid_15,
  pid_30``. The inherited UI list does not offer ``off``.
* thermostat: ``ThermostatController`` / ``TrvController``. The 2.54.2 UI
  list offers ``pid_60`` while the sorted wire array has plain ``pid``, so
  its own UI-to-wire conversion cannot find ``pid_60`` (it falls back to 0).
  That family is withheld until the discrepancy is resolved on a real device.

The factory switches on ``version.pid[2:4]`` only; codes absent from the
explicit table use ``ElectricHeaterController`` for htr/htr_mod and
``HelkiStorageHeaterController`` (off-first storage wire, relay actuation) for
acm. THM nodes are withheld by integration choice, except explicit htr-family
products such as code ``28`` (the Tevolve floor thermostat).

The UI list is actuation-dependent: triac ``pid, hyst_02, hyst_03, hyst_05,
hyst_07``; relay ``pid_15, pid_30, hyst_02, hyst_03, hyst_05, hyst_07``; a
relay with ``extra_options.sensor_mode == 2`` offers only the four hysteresis
entries. The 0402 relay htr live cycle on 2026-11-21 validated the htr/relay
pair: PID 15 -> 5, PID 30 -> 6, Hysteresis 0.2/0.3/0.5/0.7 -> 0/1/2/3.
"""

from __future__ import annotations

from collections.abc import Mapping

from .codes import PRODUCT_CODES, Actuation, Family, has_heating_switch_case
from .profile import (
    SENSOR_MODE_FLOOR_PROBE,
    has_no_probes,
    is_htr_mod_controller,
    product_code,
)
from .storage import (
    UI_MODE_CLASSIC_ACCUMULATOR,
    UI_MODE_CLASSIC_COMBINED,
    is_elnur_storage,
    is_water_storage,
)

# Derived from the reviewed product-code table: family decides the
# control-mode catalogue, actuation decides the wire array. The app's heating
# switch has no UNKNOWN ("00") or power-meter ("03"/"22") case, so those keep
# the node-type default (storage for an acm, thermostat for a thm, the
# ElectricHeater catalogue otherwise).
_HEATING_FAMILIES = frozenset({"htr", "storage", "thermostat"})
EXPLICIT_FAMILY: dict[str, Family] = {
    code: info.family
    for code, info in PRODUCT_CODES.items()
    if has_heating_switch_case(info) and info.family in _HEATING_FAMILIES
}
ACTUATION: dict[str, Actuation] = {
    code: info.actuation for code, info in PRODUCT_CODES.items()
}

HTR_WIRE = (
    "HYSTERESIS_02",
    "HYSTERESIS_03",
    "HYSTERESIS_05",
    "HYSTERESIS_07",
    "PID",
    "PID_15",
    "PID_30",
)
STORAGE_WIRE = (
    "OFF",
    "HYSTERESIS_02",
    "HYSTERESIS_03",
    "HYSTERESIS_05",
    "HYSTERESIS_07",
    "PID_15",
    "PID_30",
)

HTR_TRIAC_UI = (
    "PID",
    "HYSTERESIS_02",
    "HYSTERESIS_03",
    "HYSTERESIS_05",
    "HYSTERESIS_07",
)
HTR_RELAY_UI = (
    "PID_15",
    "PID_30",
    "HYSTERESIS_02",
    "HYSTERESIS_03",
    "HYSTERESIS_05",
    "HYSTERESIS_07",
)
HYSTERESIS_ONLY_UI = (
    "HYSTERESIS_02",
    "HYSTERESIS_03",
    "HYSTERESIS_05",
    "HYSTERESIS_07",
)

MODE_LABELS_C: dict[str, str] = {
    "HYSTERESIS_02": "Hysteresis (0.2 °C)",
    "HYSTERESIS_03": "Hysteresis (0.3 °C)",
    "HYSTERESIS_04": "Hysteresis (0.4 °C)",
    "HYSTERESIS_05": "Hysteresis (0.5 °C)",
    "HYSTERESIS_07": "Hysteresis (0.7 °C)",
    "HYSTERESIS_08": "Hysteresis (0.8 °C)",
    "HYSTERESIS_16": "Hysteresis (1.6 °C)",
    "PID": "PID",
    "PID_10": "PID (10 min. period)",
    "PID_15": "PID (15 min. period)",
    "PID_20": "PID (20 min. period)",
    "PID_25": "PID (25 min. period)",
    "PID_30": "PID (30 min. period)",
    "PID_45": "PID (45 min. period)",
    "PID_60": "PID (60 min. period)",
}

MODE_LABELS_F: dict[str, str] = {
    **MODE_LABELS_C,
    "HYSTERESIS_02": "Hysteresis (0.4 °F)",
    "HYSTERESIS_03": "Hysteresis (0.5 °F)",
    "HYSTERESIS_04": "Hysteresis (0.7 °F)",
    "HYSTERESIS_05": "Hysteresis (0.9 °F)",
    "HYSTERESIS_07": "Hysteresis (1.2 °F)",
    "HYSTERESIS_08": "Hysteresis (1.4 °F)",
    "HYSTERESIS_16": "Hysteresis (2.9 °F)",
}

# The app omits PID_15/PID_30 from the relay UI when the floor probe is the
# only sensor; see SENSOR_MODE_FLOOR_PROBE.

# Elnur storage products whose app hides the control-mode setting. Code 2A
# omits it from getSettings(); codes 14/20/25 hide it in the classic UI modes.
_CLASSIC_UI_MODES = frozenset({UI_MODE_CLASSIC_COMBINED, UI_MODE_CLASSIC_ACCUMULATOR})


def _family(code: str | None, node_type: str) -> str | None:
    """Return the controller family for a product code and node type."""
    if node_type not in {"htr", "htr_mod", "acm", "thm"}:
        return None
    explicit = EXPLICIT_FAMILY.get(code or "")
    if explicit is not None:
        return explicit
    # The app's factory default is ElectricHeaterController for htr/htr_mod,
    # but HelkiStorageHeaterController for acm (its wire array starts with OFF).
    # A thm without an explicit htr-family code stays withheld.
    if node_type == "acm":
        return "storage"
    if node_type == "thm":
        return None
    return "htr"


def _ui_for_actuation(actuation: str, sensor_mode: object) -> tuple[str, ...]:
    """Return the vendor UI list for a heater family."""
    if actuation != "RELAY":
        return HTR_TRIAC_UI
    if sensor_mode == SENSOR_MODE_FLOOR_PROBE:
        return HYSTERESIS_ONLY_UI
    return HTR_RELAY_UI


def _choices(
    ui: tuple[str, ...], wire: tuple[str, ...], labels: dict[str, str]
) -> tuple[tuple[str, int], ...] | None:
    """Pair each offered UI label with its wire index; None on a mismatch."""
    try:
        return tuple((labels[mode], wire.index(mode)) for mode in ui)
    except KeyError, ValueError:
        # A UI mode has no label or is absent from the wire array; the vendor
        # bundle's own conversion would also fall back to 0, so do not offer
        # a select.
        return None


def control_mode_choices(
    pid: str | None,
    node_type: str,
    setup: Mapping[str, object] | None,
    units: str | None = None,
) -> tuple[tuple[str, int], ...] | None:
    """Return ``(label, wire index)`` choices for a node, or None.

    Only the htr and storage families are offered. The thermostat family is
    withheld because the 2.54.2 UI list and wire array disagree on PID_60;
    other THM codes are withheld by integration choice, except explicit
    htr-family products such as code 28. The map is product-level: a stored
    index outside the offered set still returns the map, so the entity can
    offer the app's other choices and render the stored value as unknown. Use
    :func:`control_mode_available` for the entity-creation gate.
    """
    setup = setup or {}
    code = product_code(pid)
    if code is None:
        # No product code yet: the factory route is unknown, so offer nothing.
        return None
    if node_type == "thm" and EXPLICIT_FAMILY.get(code) != "htr":
        # The thermostat family is withheld for the pid_60 mismatch; other
        # unlisted THM codes are withheld by integration choice. Code 28 is
        # an explicit htr-family floor thermostat whose app controller offers
        # the inherited htr arrays, so it keeps the select.
        return None
    family = _family(code, node_type)
    if family in (None, "thermostat"):
        return None
    units = units or ("F" if setup.get("units") == "F" else "C")
    labels = MODE_LABELS_F if units == "F" else MODE_LABELS_C
    extra = setup.get("extra_options")
    sensor_mode = extra.get("sensor_mode") if isinstance(extra, Mapping) else None

    if family == "storage":
        # An explicit storage row must be relay-actuated. The factory's
        # unlisted-acm fallback is HelkiStorageHeaterController regardless of
        # what the code table says about an UNKNOWN/power-meter code.
        explicit = EXPLICIT_FAMILY.get(code or "")
        if explicit == "storage" and ACTUATION.get(code or "") != "RELAY":
            return None
        ui = _ui_for_actuation("RELAY", sensor_mode)
        wire = STORAGE_WIRE
    else:
        ui = _ui_for_actuation(ACTUATION.get(code or "", "TRIAC"), sensor_mode)
        wire = HTR_WIRE
    choices = _choices(ui, wire, labels)
    if choices is None:
        return None
    return choices


def control_mode_available(  # noqa: PLR0911 - one gate per controller family
    pid: str | None,
    node_type: str,
    setup: Mapping[str, object] | None,
    units: str | None = None,
) -> bool:
    """Whether this node should get the control-mode select.

    The setting must be present in setup and the product must have a mappable
    catalogue. The app hides it on Elnur water storage (code ``2A``) and on
    Elnur storage (``14``, inherited by ``20``/``25``) in the classic UI
    modes, so those products get no entity even when the setup carries an
    index.
    """
    setup = setup or {}
    if "control_mode" not in setup:
        return False
    if node_type == "thm" and product_code(pid) == "28" and has_no_probes(setup):
        # TevolveThermostat's NO_PROBES timer hides CONTROL_MODE.
        return False
    if node_type == "htr_mod" and is_htr_mod_controller(pid, node_type):
        return False
    if control_mode_choices(pid, node_type, setup, units) is None:
        return False
    if is_water_storage(pid):
        return False
    if is_elnur_storage(pid):
        factory = setup.get("factory_options")
        ui_mode = factory.get("ui_mode") if isinstance(factory, Mapping) else None
        if ui_mode in _CLASSIC_UI_MODES:
            return False
    return True


def control_mode_label(
    choices: tuple[tuple[str, int], ...], value: int | None
) -> str | None:
    """Return the app label for one wire index, or None when unmapped."""
    if value is None:
        return None
    for label, index in choices:
        if index == value:
            return label
    return None
