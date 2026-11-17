"""Wire-shape rules the merge path applies to raw payloads.

Everything here is a pure function of its arguments: it reads a wire payload
(and, for the diffs, the stored slot it is compared against) and returns what
the store should take. State, tasks and notification belong to
:mod:`smartbox.core.engine`; the typed accessors the entities read live in
:mod:`smartbox.protocol.values`.

The shapes encoded here are the ones ``hass-smartbox docs/api-contract.md`` records: absent
or ill-typed slots never clear a stored value, the box power limit arrives in
two copies with different types (§9, E17/E18), and a node entry is keyed by
``type`` / ``addr``.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from ..protocol.errors import SmartboxError
from ..protocol.values import (
    BoostConfig,
    celsius_to_node_units,
    is_lost,
    parse_number,
    prog_days,
    same_wire_value,
)
from .state import NODE_SLOTS, DeviceField, DeviceState, NodeState, node_key

__all__ = [
    "DEVICE_UPDATE_PATHS",
    "MALFORMED",
    "PROG_TEMP_KEYS",
    "boost_config",
    "coerce_power_limit",
    "device_slots_delta",
    "device_update_value",
    "entry_key",
    "is_empty_slot",
    "max_stemp_limit_node",
    "sanitize_prog_temps",
    "snapshot_slot_updates",
    "valid_power_limit",
    "write_intent",
]

_LOGGER = logging.getLogger(__name__)

# device-level update path -> DeviceField slot (the body shape is
# resolved per slot by device_update_value)
DEVICE_UPDATE_PATHS: dict[tuple[str, ...], DeviceField] = {
    ("connected",): "connected",
    ("mgr", "away_status"): "away",
    ("htr_system", "power_limit"): "power_limit",
    # A box-limit write pushes the INT copy through this frame as well, and a
    # "0" clear pushes ONLY this one (live 2026-10-18), so the key lands in
    # the same numeric slot; the frame's other htr_system setup keys are
    # unread.
    ("htr_system", "setup"): "power_limit",
}

# Returned by device_update_value for a body that cannot become a slot value.
MALFORMED = object()


def _copy_dict(value: object) -> dict[str, Any] | None:
    """Copy a wire dict payload; anything else means absent/ill-typed."""
    return dict(value) if isinstance(value, Mapping) else None


def boost_config(setup: Mapping[str, Any]) -> int:
    """Return the node's ``factory_options.boost_config`` (0 when absent).

    The vendor app's enum is 0 unsupported, 1 max-temp-fixed-time,
    2 set-temp-and-time, 3 full. Compared numerically so an unknown future
    value reads as "advertises boost" instead of raising while an entity
    state is written.
    """
    factory = setup.get("factory_options")
    value = factory.get("boost_config") if isinstance(factory, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return BoostConfig.UNSUPPORTED
    return int(value)


PROG_TEMP_KEYS = ("ice_temp", "eco_temp", "comf_temp")
_PROG_OFFSET_C = 0.5
_GLOBAL_MIN_C = 5.0
_GLOBAL_MAX_C = 30.0


def _parse(value: object) -> float | None:
    try:
        return parse_number(value)
    except SmartboxError:
        return None


def max_stemp_limit_node(state: NodeState | None) -> float | None:
    """Return the setup ceiling in the node's unit (None when disabled)."""
    if state is None:
        return None
    limit = _parse(state.setup.get("max_stemp_limit"))
    if limit is None or limit <= 0:
        return None
    units = state.status.get("units")
    if units == "F":
        return celsius_to_node_units(limit, "F")
    return limit


def _prog_bounds(
    state: NodeState,
) -> tuple[dict[str, tuple[float, float]], float] | None:
    """Return per-profile node-unit bounds and the ordering offset."""
    units = state.status.get("units")
    if not isinstance(units, str):
        return None
    if units == "F":
        min_node = celsius_to_node_units(_GLOBAL_MIN_C, "F")
        max_node = celsius_to_node_units(_GLOBAL_MAX_C, "F")
        offset = 1.0
    else:
        min_node = _GLOBAL_MIN_C
        max_node = _GLOBAL_MAX_C
        offset = _PROG_OFFSET_C
    limit = max_stemp_limit_node(state)
    if limit is not None:
        max_node = min(max_node, limit)
    bounds = {
        "ice_temp": (min_node, max_node - 2 * offset),
        "eco_temp": (min_node + offset, max_node - offset),
        "comf_temp": (min_node + 2 * offset, max_node),
    }
    return bounds, offset


def _order_prog_values(
    result: dict[str, float | None], offset: float, changed: str | None
) -> None:
    """Apply the app's neighbor passes, exempting the changed profile."""
    for index in range(len(PROG_TEMP_KEYS) - 2, -1, -1):
        key = PROG_TEMP_KEYS[index]
        upper_key = PROG_TEMP_KEYS[index + 1]
        lower, upper = result[key], result[upper_key]
        if key == changed or lower is None or upper is None:
            continue
        result[key] = min(upper - offset, lower)
    for index in range(1, len(PROG_TEMP_KEYS)):
        key = PROG_TEMP_KEYS[index]
        lower_key = PROG_TEMP_KEYS[index - 1]
        lower, upper = result[lower_key], result[key]
        if key == changed or lower is None or upper is None:
            continue
        result[key] = max(lower + offset, upper)


def sanitize_prog_temps(
    state: NodeState | None,
    values: Mapping[str, float | None],
    *,
    changed: str | None = None,
) -> dict[str, float | None]:
    """Clamp profile temperatures the way the vendor app does before a write.

    The app clamps each editable profile to its per-profile bounds, then walks
    ice/eco/comfort to enforce the 0.5 °C offset (1 °F on an F node). The
    profile the user just changed is exempt from the neighbour pass, so setting
    a lower comfort value pushes eco/ice down instead of being pulled back up.
    Values missing from the caller are read from the current status first; a
    still-missing value stays None and is omitted from the body.
    """
    if state is None:
        return dict(values)
    profile = _prog_bounds(state)
    if profile is None:
        return dict(values)
    bounds, offset = profile
    result: dict[str, float | None] = {}
    for key in PROG_TEMP_KEYS:
        value = values.get(key)
        if value is None:
            value = _parse(state.status.get(key))
        if value is not None:
            low, high = bounds[key]
            value = max(low, min(max(low, high), value))
        result[key] = value
    _order_prog_values(result, offset, changed)
    return result


def normalize_node_addr(addr: object) -> int | None:
    """Return a canonical non-negative node address, or ``None``.

    The accepted set is exactly what ``int()`` parses as decimal: ASCII digits,
    no sign, no leading zeros. This keeps the engine key and the registry
    ``{dev_id}_{addr}`` id derived from the same spelling.
    """
    if isinstance(addr, bool):
        return None
    if isinstance(addr, int):
        return addr if addr >= 0 else None
    if not isinstance(addr, str) or not addr.isascii() or not addr.isdecimal():
        return None
    if len(addr) > 1 and addr.startswith("0"):
        return None
    return int(addr)


def is_empty_slot(entry: Mapping[str, Any]) -> bool:
    """Whether a node-list entry is an uninstalled slot.

    Empty slots carry ``installed: false``; they are not nodes
    (hass-smartbox docs/api-contract.md §6.6), and their status read returns
    404. A missing flag means installed.
    """
    return entry.get("installed") is False


def entry_key(entry: Mapping[str, Any], dev_id: str) -> str | None:
    """Canonical map key for a wire node entry; ``None`` when malformed."""
    node_type = entry.get("type")
    addr = normalize_node_addr(entry.get("addr"))
    if not isinstance(node_type, str) or not node_type or addr is None:
        # The wire key is ``type``; the vendored layers read what the server
        # sends, so a node entry keyed ``node_type`` or a non-canonical
        # address is not a node. Reject it at the parse boundary so no
        # consumer has to convert the address later.
        _LOGGER.warning(
            "Node entry missing/invalid type or addr on %r; ignoring", dev_id
        )
        return None
    return node_key(node_type, str(addr))


def device_slots_delta(
    device: DeviceState,
    dev_data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[DeviceField]]:
    """Diff the scalar device slots in a devData payload against the store.

    Absent/ill-typed slots are skipped (a snapshot never clears a slot by
    omission). Returns the field updates and the changed kinds.
    """
    updates: dict[str, Any] = {}
    kinds: list[DeviceField] = []
    connected = dev_data.get("connected")
    if isinstance(connected, bool) and connected != device.connected:
        updates["connected"] = connected
        kinds.append("connected")
    away = _copy_dict(dev_data.get("away_status"))
    if away and away != device.away:
        updates["away"] = away
        kinds.append("away")
    htr_system = _copy_dict(dev_data.get("htr_system"))
    if htr_system:
        power_limit = _snapshot_power_limit(htr_system)
        if power_limit is not None and power_limit != device.power_limit:
            updates["power_limit"] = power_limit
            kinds.append("power_limit")
    return updates, kinds


def coerce_power_limit(value: object) -> object:
    """Return the digit-string wire form of a power limit as an int.

    The write and its update frame carry ``{"power_limit": "<int as string>"}``
    (E18) while the snapshot's ``setup`` copy is an int. The slot serves one
    numeric value (E17), so the number entity and the dedup never see a type
    flip.
    """
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def valid_power_limit(value: object) -> int | None:
    """Return a usable integer power limit, or ``None`` for malformed wire data."""
    coerced = coerce_power_limit(value)
    if isinstance(coerced, bool) or not isinstance(coerced, int):
        return None
    if coerced < 0:
        # The write API refuses negative limits; a frame carrying one is
        # malformed, not a setting.
        return None
    return coerced


def _snapshot_power_limit(htr_system: Mapping[str, Any]) -> object | None:
    """Extract the box power limit from a ``devData.htr_system`` subtree.

    Both copies are present and kept in sync: ``power_limit.power_limit`` as
    the digit string ``'3600'`` and ``setup.power_limit`` as the int ``3600``.
    The vendor web client reads the object copy, the mobile client falls back
    to ``setup`` when it is empty, so that order is used here. A bare scalar
    under ``power_limit`` (older families) counts as the object copy, and an
    unreported limit stays ``None`` so the slot keeps its value.
    """
    limit = htr_system.get("power_limit")
    if isinstance(limit, Mapping):
        limit = limit.get("power_limit")
    if (usable := valid_power_limit(limit)) is not None:
        return usable
    setup = htr_system.get("setup")
    fallback = setup.get("power_limit") if isinstance(setup, Mapping) else None
    return valid_power_limit(fallback)


def device_update_value(field: DeviceField, body: Mapping[str, Any]) -> Any:  # noqa: ANN401 - raw wire passthrough
    """Resolve an update body into a slot value; ``MALFORMED`` if unusable."""
    if field == "connected":
        connected = body.get("connected")
        if not isinstance(connected, bool):
            return MALFORMED
        return connected
    if field == "power_limit":
        if "power_limit" not in body:
            return MALFORMED
        value = valid_power_limit(body["power_limit"])
        return MALFORMED if value is None else value
    return dict(body)  # the remaining path's body IS the slot value


def snapshot_slot_updates(old: NodeState, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the slot bodies a snapshot node entry changes.

    Absent or ill-typed slots never clear a stored value, and a value-equal
    slot is not an update - the snapshot path diffs where the frame path
    replaces wholesale. A lost-marked slot is availability evidence, never slot
    data, so it cannot blank the last known body either.
    """
    updates: dict[str, Any] = {}
    for slot in NODE_SLOTS:
        value = entry.get(slot)
        # An empty mapping is "no body", not a value: it must not blank the
        # stored slot or count as reachability (hass-smartbox docs/api-contract.md §6.6).
        if isinstance(value, Mapping) and value and not is_lost(value):
            copied = dict(value)
            if copied != getattr(old, slot):
                updates[slot] = copied
    return updates


def write_intent(
    slot: str, current: NodeState, body: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the keys a posted body changes, with the values it sets.

    A setup op posts the whole object but changes one key, so the evidence that
    it landed is the difference against the stored body, not the whole object.
    A prog write takes no key-level evidence (its confirming frame echoes the
    whole week, docs/api-ws-user.md §4), but its day map is still compared
    here: a prog write whose day map is unchanged is a no-op.
    """
    stored = getattr(current, slot)
    if slot == "prog":
        # The store may hold the day map bare or nested under ``prog``; the
        # posted body is always nested. Compare the day maps, not the wrapper:
        # the confirming frame echoes the whole week, and only the changed days
        # are evidence that THIS write landed.
        stored_days = prog_days(stored) if isinstance(stored, Mapping) else None
        body_days = prog_days(body)
        if body_days is None:
            return dict(body)
        if stored_days is None:
            return dict(body_days)
        return {
            key: value
            for key, value in body_days.items()
            if not (key in stored_days and same_wire_value(value, stored_days[key]))
        }
    if not isinstance(stored, Mapping):
        return dict(body)
    return {
        key: value
        for key, value in body.items()
        if not (key in stored and same_wire_value(value, stored[key]))
    }
