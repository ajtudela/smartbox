"""Storage-heater mode semantics from the 2.54.2 controller classes.

Two controller families share the ``acm`` wire type:

* HelkiStorage (1C/2E): AUTO/MANUAL/OFF/ON filtered by
  ``setup.operational_mode``, plus the legacy ``/boost`` endpoint.
* ElnurStorage (14/20/25/2A): ``factory_options.ui_mode`` selects ECOMBI,
  CLASSIC_COMBINED or CLASSIC_ACCUMULATOR. Classic accumulators write
  ``target_charge_per`` 100/0 instead of a mode.

The library returns neutral :class:`StorageIntent` values; the integration
adapter maps them to/from Home Assistant's ``HVACMode``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .profile import product_code

UI_MODE_ECOMBI = 0
UI_MODE_CLASSIC_COMBINED = 1
UI_MODE_CLASSIC_ACCUMULATOR = 2

OPERATIONAL_MANUAL_OFF = 0
OPERATIONAL_AUTO_OFF = 1

_HELKI_STORAGE_CODES = frozenset({"1C", "2E"})
_ELNUR_STORAGE_CODES = frozenset({"14", "20", "25", "2A"})


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
    return product_code(pid) in _HELKI_STORAGE_CODES


def is_elnur_storage(pid: str | None) -> bool:
    """Return True for the Elnur 14/20/25/2A storage controllers."""
    return product_code(pid) in _ELNUR_STORAGE_CODES


def is_water_storage(pid: str | None) -> bool:
    """Return True for the Elnur water-storage controller (2A)."""
    return product_code(pid) == "2A"


def _extra_nrg_conf(setup: Mapping[str, object]) -> Mapping[str, object]:
    value = setup.get("extra_nrg_conf")
    return value if isinstance(value, Mapping) else {}


def water_extra_energy_enabled(setup: Mapping[str, object]) -> bool:
    """Whether the 2A extra-energy configuration is enabled."""
    return bool(_extra_nrg_conf(setup).get("enabled"))


def water_extra_energy_mode(setup: Mapping[str, object]) -> int | None:
    """Return the 2A extra-energy mode (0 EXTRA_ENERGY_USAGE) when numeric."""
    value = _extra_nrg_conf(setup).get("mode")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def ui_mode(setup: Mapping[str, object]) -> int | None:
    """Return ``factory_options.ui_mode`` (0/1/2) when present."""
    factory = setup.get("factory_options")
    if not isinstance(factory, Mapping):
        return None
    value = factory.get("ui_mode")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


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
    value = setup.get("operational_mode")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def classic_accumulator(pid: str | None, setup: Mapping[str, object]) -> bool:
    """Whether this Elnur unit uses the target-charge ON/OFF UI."""
    return is_elnur_storage(pid) and ui_mode(setup) == UI_MODE_CLASSIC_ACCUMULATOR


def available_modes(
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
            elif water_extra_energy_mode(setup) == 0:
                modes.append(StorageIntent.HEAT)
            else:
                modes.append(StorageIntent.AUTO)
        else:
            mode = ui_mode(setup)
            if mode == UI_MODE_ECOMBI:
                modes.append(StorageIntent.AUTO)
            if mode != UI_MODE_CLASSIC_ACCUMULATOR:
                modes.append(StorageIntent.HEAT)
    else:
        op = operational_mode(setup)
        if op == OPERATIONAL_AUTO_OFF:
            modes.append(StorageIntent.AUTO)
        modes.append(StorageIntent.HEAT)
    modes.append(StorageIntent.OFF)
    return modes or [StorageIntent.OFF]


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
    if mode == "off" or mode is None:
        return StorageIntent.OFF
    if mode in ("auto", "modified_auto"):
        return StorageIntent.AUTO
    if mode in ("manual", "on"):
        return StorageIntent.HEAT
    return None


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
        if intent == StorageIntent.HEAT and water_extra_energy_mode(setup) != 0:
            return None
        if intent == StorageIntent.AUTO and water_extra_energy_mode(setup) == 0:
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
    "OPERATIONAL_AUTO_OFF",
    "OPERATIONAL_MANUAL_OFF",
    "UI_MODE_CLASSIC_ACCUMULATOR",
    "UI_MODE_CLASSIC_COMBINED",
    "UI_MODE_ECOMBI",
    "StorageIntent",
    "StorageWrite",
    "available_modes",
    "boost_supported",
    "classic_accumulator",
    "current_mode",
    "is_elnur_storage",
    "is_helki_storage",
    "is_water_storage",
    "lock_endpoint",
    "operational_mode",
    "ui_mode",
    "ui_mode_available",
    "ui_mode_options",
    "water_extra_energy_enabled",
    "water_extra_energy_mode",
    "write_for",
]
