"""Storage-heater mode semantics from the 2.54.2 controller classes.

Two controller families share the ``acm`` wire type:

* HelkiStorage (1C/2E): AUTO/MANUAL/OFF/ON filtered by
  ``setup.operational_mode``, plus the legacy ``/boost`` endpoint.
* ElnurStorage (14/20/25/2A): ``factory_options.ui_mode`` selects ECOMBI,
  CLASSIC_COMBINED or CLASSIC_ACCUMULATOR. Classic accumulators write
  ``target_charge_per`` 100/0 instead of a mode.

The library returns neutral :class:`StorageIntent` values; the integration
adapter maps them to/from Home Assistant's ``HVACMode``.


Decoded from the vendor 2.54.2 bundle; see `hass-smartbox docs/api-contract.md`
(the wire record) for the provenance citations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .codes import PRODUCT_CODES, code_info
from .profile import ELNUR_STORAGE_CODES, HELKI_STORAGE_CODES, product_code
from .setup import SetupView

UI_MODE_ECOMBI = 0
UI_MODE_CLASSIC_COMBINED = 1
UI_MODE_CLASSIC_ACCUMULATOR = 2

WATER_STORAGE_CODE = next(code for code, info in PRODUCT_CODES.items() if info.water)
ELNUR_STEPPED_STORAGE_CODE = next(
    code for code, info in PRODUCT_CODES.items() if info.stepped_charge
)
EXTRA_ENERGY_MODE_USAGE = 0
EXTRA_ENERGY_MODE_COMBINED = 1
EXTRA_ENERGY_MODE_CONFORT = 2

OPERATIONAL_MANUAL_OFF = 0
OPERATIONAL_AUTO_OFF = 1


class StorageIntent(StrEnum):
    """Neutral HA-facing storage mode."""

    HEAT = "heat"
    AUTO = "auto"
    OFF = "off"


@dataclass(frozen=True)
class StorageWrite:
    """One resolved storage-heater mode write."""

    kind: str  # "charge" | "status"
    intent: StorageIntent
    target_charge: int | None = None
    mode: str | None = None


def is_helki_storage(pid: str | None) -> bool:
    """Return True for the 1C/2E storage controller."""
    return product_code(pid) in HELKI_STORAGE_CODES


def is_elnur_storage(pid: str | None) -> bool:
    """Return True for the Elnur 14/20/25/2A storage controllers."""
    return product_code(pid) in ELNUR_STORAGE_CODES


def is_water_storage(pid: str | None) -> bool:
    """Return True for the Elnur water-storage controller (2A)."""
    info = code_info(pid)
    return bool(info and info.water)


def is_elnur_stepped_storage(pid: str | None) -> bool:
    """Return True for the stepped-charge Elnur storage controller (14)."""
    info = code_info(pid)
    return bool(info and info.stepped_charge)


def water_extra_energy_enabled(setup: Mapping[str, object]) -> bool:
    """Whether the 2A extra-energy configuration is enabled."""
    return SetupView(setup).extra_nrg_enabled


def water_extra_energy_mode(setup: Mapping[str, object]) -> int | None:
    """Return the 2A extra-energy mode when numeric."""
    return SetupView(setup).extra_nrg_mode


def ui_mode(setup: Mapping[str, object]) -> int | None:
    """Return ``factory_options.ui_mode`` (0/1/2) when present."""
    return SetupView(setup).ui_mode


def _extra_energy_hides_auto(pid: str | None, setup: Mapping[str, object]) -> bool:
    """E2 storage filters AUTO when extra energy is on outside CONfort."""
    info = code_info(pid)
    return (
        bool(info and info.extra_energy)
        and water_extra_energy_enabled(setup)
        and water_extra_energy_mode(setup) != EXTRA_ENERGY_MODE_CONFORT
    )


_UI_MODE_OPTIONS: dict[str, tuple[tuple[str, int], ...]] = {
    "14": (
        ("Classic combined", UI_MODE_CLASSIC_COMBINED),
        ("Classic accumulator", UI_MODE_CLASSIC_ACCUMULATOR),
    ),
    "20": (
        ("Ecombi", UI_MODE_ECOMBI),
        ("Classic combined", UI_MODE_CLASSIC_COMBINED),
    ),
    "25": (
        ("Ecombi", UI_MODE_ECOMBI),
        ("Classic combined", UI_MODE_CLASSIC_COMBINED),
    ),
}


def ui_mode_options(pid: str | None) -> tuple[tuple[str, int], ...]:
    """Return the app's accepted storage UI-mode choices for this product."""
    return _UI_MODE_OPTIONS.get(product_code(pid) or "", ())


def ui_mode_available(pid: str | None, setup: Mapping[str, object]) -> bool:
    """Whether the app shows the storage UI-mode setting."""
    return bool(ui_mode_options(pid)) and ui_mode(setup) in (
        UI_MODE_CLASSIC_COMBINED,
        UI_MODE_CLASSIC_ACCUMULATOR,
    )


def operational_mode(setup: Mapping[str, object]) -> int | None:
    """Return ``setup.operational_mode`` (0 manual, 1 auto) when present."""
    return SetupView(setup).operational_mode


def classic_accumulator(pid: str | None, setup: Mapping[str, object]) -> bool:
    """Whether this Elnur unit uses the target-charge ON/OFF UI."""
    return is_elnur_storage(pid) and ui_mode(setup) == UI_MODE_CLASSIC_ACCUMULATOR


def available_modes(  # noqa: PLR0912 - one branch per controller/UI mode
    node_type: str, pid: str | None, setup: Mapping[str, object]
) -> list[StorageIntent]:
    """Return the storage modes the app exposes for this controller."""
    if node_type != "acm":
        return [StorageIntent.HEAT, StorageIntent.AUTO, StorageIntent.OFF]
    if classic_accumulator(pid, setup):
        return [StorageIntent.HEAT, StorageIntent.OFF]
    modes: list[StorageIntent] = []
    if is_elnur_storage(pid):
        if is_water_storage(pid):
            if not water_extra_energy_enabled(setup):
                modes.extend((StorageIntent.AUTO, StorageIntent.HEAT))
            elif water_extra_energy_mode(setup) == EXTRA_ENERGY_MODE_USAGE:
                modes.append(StorageIntent.HEAT)
            else:
                modes.append(StorageIntent.AUTO)
        else:
            mode = ui_mode(setup)
            if mode == UI_MODE_ECOMBI:
                modes.append(StorageIntent.AUTO)
            if mode != UI_MODE_CLASSIC_ACCUMULATOR:
                modes.append(StorageIntent.HEAT)
            if _extra_energy_hides_auto(pid, setup):
                modes = [item for item in modes if item != StorageIntent.AUTO]
    else:
        op = operational_mode(setup)
        if op == OPERATIONAL_AUTO_OFF:
            modes.append(StorageIntent.AUTO)
        modes.append(StorageIntent.HEAT)
    modes.append(StorageIntent.OFF)
    return modes or [StorageIntent.OFF]


_MODE_TO_INTENT: dict[str, StorageIntent] = {
    "manual": StorageIntent.HEAT,
    "on": StorageIntent.HEAT,
    "auto": StorageIntent.AUTO,
    "modified_auto": StorageIntent.AUTO,
    "off": StorageIntent.OFF,
}
_INTENT_TO_MODES: dict[StorageIntent, tuple[str, ...]] = {
    StorageIntent.HEAT: ("manual",),
    StorageIntent.AUTO: ("auto", "modified_auto"),
    StorageIntent.OFF: ("off",),
}


def intent_for_mode(mode: str | None) -> StorageIntent | None:
    """Map a status/setup wire mode to the neutral storage intent."""
    return _MODE_TO_INTENT.get(mode or "")


def modes_for_intent(intent: StorageIntent) -> tuple[str, ...]:
    """Return the wire modes a caller can pass for one neutral intent."""
    return _INTENT_TO_MODES.get(intent, ())


def current_mode(  # noqa: PLR0911 - one return per storage mode
    node_type: str,
    pid: str | None,
    setup: Mapping[str, object],
    status: Mapping[str, object],
) -> StorageIntent | None:
    """Return the app's current storage mode."""
    if node_type != "acm":
        return None
    if status.get("boost", False):
        return StorageIntent.HEAT
    if classic_accumulator(pid, setup):
        if status.get("mode") == "off":
            return StorageIntent.OFF
        target = status.get("target_charge_per")
        if target in (0, "0", 0.0):
            return StorageIntent.OFF
        return StorageIntent.HEAT
    mode = status.get("mode")
    if not isinstance(mode, str) or mode == "off":
        return StorageIntent.OFF
    return intent_for_mode(mode)


def write_for(  # noqa: PLR0911 - one return per mode
    node_type: str,
    pid: str | None,
    setup: Mapping[str, object],
    intent: StorageIntent,
) -> StorageWrite | None:
    """Translate one neutral mode into the storage operation the app sends."""
    if node_type != "acm":
        return None
    if classic_accumulator(pid, setup):
        if intent == StorageIntent.HEAT:
            return StorageWrite("charge", StorageIntent.HEAT, target_charge=100)
        if intent == StorageIntent.OFF:
            return StorageWrite("charge", StorageIntent.OFF, target_charge=0)
        return None
    if is_water_storage(pid) and water_extra_energy_enabled(setup):
        if (
            intent == StorageIntent.HEAT
            and water_extra_energy_mode(setup) != EXTRA_ENERGY_MODE_USAGE
        ):
            return None
        if (
            intent == StorageIntent.AUTO
            and water_extra_energy_mode(setup) == EXTRA_ENERGY_MODE_USAGE
        ):
            return None
    if intent == StorageIntent.AUTO and _extra_energy_hides_auto(pid, setup):
        return None
    if intent == StorageIntent.HEAT:
        return StorageWrite("status", StorageIntent.HEAT, mode="manual")
    if intent == StorageIntent.AUTO:
        return StorageWrite("status", StorageIntent.AUTO, mode="auto")
    if intent == StorageIntent.OFF:
        return StorageWrite("status", StorageIntent.OFF, mode="off")
    return None


def boost_supported(node_type: str, pid: str | None) -> bool:
    """Storage boost is the Helki 1C/2E ``/boost`` path."""
    return node_type == "acm" and not is_elnur_storage(pid)


def lock_endpoint(node_type: str) -> bool:
    """Storage lock writes use the dedicated ``/lock`` endpoint."""
    return node_type == "acm"


__all__ = [
    "ELNUR_STEPPED_STORAGE_CODE",
    "EXTRA_ENERGY_MODE_COMBINED",
    "EXTRA_ENERGY_MODE_CONFORT",
    "EXTRA_ENERGY_MODE_USAGE",
    "OPERATIONAL_AUTO_OFF",
    "OPERATIONAL_MANUAL_OFF",
    "UI_MODE_CLASSIC_ACCUMULATOR",
    "UI_MODE_CLASSIC_COMBINED",
    "UI_MODE_ECOMBI",
    "WATER_STORAGE_CODE",
    "StorageIntent",
    "StorageWrite",
    "available_modes",
    "boost_supported",
    "classic_accumulator",
    "current_mode",
    "intent_for_mode",
    "is_elnur_stepped_storage",
    "is_elnur_storage",
    "is_helki_storage",
    "is_water_storage",
    "lock_endpoint",
    "modes_for_intent",
    "operational_mode",
    "ui_mode",
    "ui_mode_available",
    "ui_mode_options",
    "water_extra_energy_enabled",
    "water_extra_energy_mode",
    "write_for",
]
