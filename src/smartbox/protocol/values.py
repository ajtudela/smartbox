"""Boundary normalization primitives for raw wire payloads.

The wire is stringly-typed where core wants typed values: numbers arrive as
strings (``"20.0"``), setpoints live on a 0.5 °C grid, RTC months are
0-indexed, and sync status distinguishes "lost" payloads. Core builds its typed
accessors on THESE primitives; the module stays small and dependency-free.

Open wire questions, documented rather than special-cased:

- RTC month indexing is assumed ``n + 1`` (September -> 8 -> 9); December
  (``n=11`` -> 12) is unverified live.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import IntEnum
import math
from typing import Any

from .errors import SmartboxError

__all__ = [
    "BoostConfig",
    "bounded_repr",
    "celsius_to_node_units",
    "format_setpoint",
    "is_lost",
    "node_units_to_celsius",
    "normalize_prog",
    "parse_number",
    "parse_number_or_none",
    "prog_days",
    "require_number",
    "same_wire_value",
    "validate_prog_days",
    "validate_units",
]

_KNOWN_UNITS = ("C", "F")
# Cap on payload excerpts in error messages (log-hygiene guard).
_REPR_LIMIT = 60
# Weekly schedules are day-keyed "0".."6" (Sunday..Saturday).
_MINUTES_PER_DAY = 1440
_PROG_DAY_KEYS = frozenset({str(day) for day in range(7)})


class BoostConfig(IntEnum):
    """Boost configuration (wire integer, as the vendor app reads it).

    ``0`` is unsupported; ``1`` is a fixed max-temperature boost; ``2`` is
    temperature+time held in the setup's ``extra_options``; ``3`` is the full
    boost that uses the ``/boost_mode`` endpoint
    (docs/api-ws-user.md §2.3).
    """

    UNSUPPORTED = 0
    MAX_TEMP_FIXED_TIME = 1
    SET_TEMP_AND_TIME = 2
    FULL = 3


def bounded_repr(wire: object) -> str:
    """Truncate repr for error messages.

    Small values render verbatim; long ones truncate with a length
    note (never the full payload).
    """
    text = repr(wire)
    if len(text) <= _REPR_LIMIT:
        return text
    return f"{text[: _REPR_LIMIT - 3]}...(len={len(text)})"


def parse_number(wire: object) -> float:
    """Parse a wire number: int/float or a numeric string; else ``SmartboxError``.

    Strict on purpose: booleans are not numbers, and non-finite values (the
    wire's ``nan`` / ``inf`` / ``1e400`` spellings, which Python's ``float``
    accepts) are rejected rather than entering the store.
    """
    if isinstance(wire, bool):
        msg = f"expected a number, got bool: {bounded_repr(wire)}"
        raise SmartboxError(msg)
    number: float | None = None
    if isinstance(wire, (int, float)):
        try:
            number = float(wire)
        except OverflowError:
            number = None
    elif isinstance(wire, str):
        try:
            number = float(wire)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number):
        msg = f"expected a finite number or numeric string, got: {bounded_repr(wire)}"
        raise SmartboxError(msg)
    return number


def parse_number_or_none(wire: object) -> float | None:
    """Parse a wire number, reading anything malformed as ``None``.

    Best-effort reads (entity attributes and polled payload fields) must not
    raise on malformed server data: an unknown value renders as unknown. The
    strict :func:`parse_number` stays the write-path parser, where a bad value
    must be refused.
    """
    try:
        return parse_number(wire)
    except SmartboxError:
        return None


def celsius_to_node_units(temp: float, units: str) -> float:
    """Convert a wire Celsius temperature into the node's display unit.

    The setup's ``max_stemp_limit`` is stored in Celsius regardless of the
    node's unit; the vendor app converts both ways (its
    ``convertTempFromCelsiusToNodeUnits`` on read, the reverse on write,
    decoded 2026-10-18). Fahrenheit displays on whole degrees - the app shows
    a stored 24.0 °C as 75.0 °F - matching the setpoint grid.
    """
    if units == "C":
        return temp
    return float(math.floor(temp * 9.0 / 5.0 + 32.0 + 0.5))


def node_units_to_celsius(temp: float, units: str) -> float:
    """Convert a temperature in the node's display unit to the wire's Celsius.

    The counterpart of :func:`celsius_to_node_units`. A Celsius input is
    returned as-is (the caller's ``format_setpoint`` floors it onto the 0.5
    grid, exactly as before); an F input snaps to the nearest 0.5 °C (half
    up), the grid the wire accepts, so the field round-trips without
    drifting far.
    """
    if units == "C":
        return temp
    return math.floor(((temp - 32.0) * 5.0 / 9.0) * 2.0 + 0.5) / 2.0


def format_setpoint(temp: float, units: str) -> str:
    """Format a setpoint for the wire.

    The value is validated FIRST (non-number, bool, NaN or inf ->
    ``ValueError``), so every unit branch is safe - including the Fahrenheit
    passthrough, which would otherwise post a non-numeric ``stemp``.

    Celsius is quantized onto the 0.5 grid (the device quantizes off-grid values
    anyway: 21.3 -> 21.0, 22.8 -> 22.5) and formatted ``.1f``. Fahrenheit
    quantizes to whole degrees (the vendor app's F grid is integral, it rejects
    non-integer input client-side, hass-smartbox docs/api-contract.md §5.2) and is formatted
    ``.1f`` as well: the API rejects a whole-degree F body WITHOUT the decimal
    with a 400 (live 2026-10-18), while the app posts ``.toFixed(1)`` values.
    """
    require_number(temp, "temp")
    units = validate_units(units)
    if units == "C":
        return _format_celsius(temp)
    return f"{math.floor(temp + 0.5):.1f}"


def require_number(value: object, name: str) -> float:
    """Validate a numeric input and return it as a float (ValueError by contract).

    Cross-module contract: writes.py imports this for its op inputs, and uses
    the return value where the input's own type is opaque (a body dict).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{name} must be a number, got: {value!r}"
        raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract
    try:
        number = float(value)
    except OverflowError:
        msg = f"{name} must be finite, got: {value!r}"
        raise ValueError(msg) from None
    if not math.isfinite(number):
        msg = f"{name} must be finite, got: {value!r}"
        raise ValueError(msg)
    return number


def validate_units(units: str) -> str:
    """Validate the temperature unit; ValueError by contract.

    Cross-module contract: writes.py imports this for its op inputs.
    """
    if units not in _KNOWN_UNITS:
        msg = f"units must be one of {_KNOWN_UNITS}, got: {units!r}"
        raise ValueError(msg)
    return units


def _format_celsius(temp: float) -> str:
    """Quantize onto the 0.5 C grid (floor, matching device behaviour)."""
    return f"{math.floor(temp * 2) / 2:.1f}"


def is_lost(payload: Mapping[str, object]) -> bool:
    """Check for a bare-lost status or a lost-marked full payload.

    The availability discriminator: either ``{"sync_status": "lost"}`` alone or
    a full payload carrying it means the device read-through is out of sync.
    A lost-marked payload is availability evidence, never slot data.
    """
    return payload.get("sync_status") == "lost"


def _wire_number(value: object) -> float | None:
    """Return ``value`` as a number, or ``None`` when ``value`` is not numeric."""
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError, OverflowError:
            return None
    return None


def same_wire_value(actual: object, expected: object) -> bool:
    """Compare one wire value against the value a write set.

    The same quantity arrives as a number on one path and as a numeric string
    on another (``power_limit`` is posted stringified and echoed as an int:
    hass-smartbox docs/api-contract.md §5.3), and the server reformats the
    numbers it stores: an htr_mod setpoint posted on the app's one-decimal grid
    is echoed as a whole-number string (``"20.0"`` written, ``"20"`` stored:
    §5.2). Numeric values therefore compare as numbers whether they arrive as
    numbers or numeric strings. Anything else compares by equality. The numeric
    comparison uses ``float``, the same model as :func:`parse_number`, so
    ``"04"`` matches ``"4"``; wire values are small enough that its precision
    is not a concern.
    """
    if actual == expected:
        return True
    left = _wire_number(actual)
    right = _wire_number(expected)
    if left is None or right is None:
        return False
    return left == right


def prog_days(slot: object) -> dict[str, Any] | None:
    """Return the day-keyed schedule inside a prog slot, or ``None``.

    The slot arrives in both wire shapes: a ``{"prog": {...}}`` frame body and
    a bare day-keyed object (hass-smartbox docs/api-contract.md §5.4). An empty mapping is a
    valid empty schedule; a lost marker or any other shape is ``None`` so a
    writer can refuse a partial merge instead of posting garbage.
    """
    if not isinstance(slot, Mapping):
        return None
    if "prog" in slot:
        inner = slot["prog"]
        if not isinstance(inner, Mapping):
            return None
        candidate = inner
    else:
        # A bare day map. Any non-day key (a bare lost marker, say) means this
        # mapping is not a schedule.
        if any(day not in _PROG_DAY_KEYS for day in slot):
            return None
        candidate = slot
    for day, slots in candidate.items():
        if (
            not isinstance(day, str)
            or day not in _PROG_DAY_KEYS
            or not isinstance(slots, list)
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in slots)
        ):
            return None
    return dict(candidate)


def thermostat_prog_days(  # noqa: PLR0911 - one return per invalid shape
    slot: object,
) -> dict[str, list[dict[str, object]]] | None:
    """Return the day-keyed thermostat ``{m, t}`` programme, or ``None``.

    Thermostat days are lists of objects, unlike the heater int-slot arrays.
    ``m`` is the segment end minute (0..1440) and ``t`` a number in the node's
    unit. Malformed/partial shapes return ``None`` so callers can refuse a
    merge instead of posting garbage.
    """
    if not isinstance(slot, Mapping):
        return None
    if "prog" in slot:
        inner = slot["prog"]
        if not isinstance(inner, Mapping):
            return None
        candidate = inner
    else:
        if any(day not in _PROG_DAY_KEYS for day in slot):
            return None
        candidate = slot
    result: dict[str, list[dict[str, object]]] = {}
    for day, segments in candidate.items():
        if day not in _PROG_DAY_KEYS or not isinstance(segments, (list, tuple)):
            return None
        out: list[dict[str, object]] = []
        for segment in segments:
            if not isinstance(segment, Mapping):
                return None
            minute = segment.get("m")
            temp = segment.get("t")
            if isinstance(minute, bool):
                return None
            minute_value = parse_number_or_none(minute)
            if (
                minute_value is None
                or not float(minute_value).is_integer()
                or not 0 <= minute_value <= _MINUTES_PER_DAY
            ):
                return None
            if isinstance(temp, bool) or parse_number_or_none(temp) is None:
                return None
            out.append({"m": int(minute_value), "t": temp})
        result[day] = out
    return result


def thermostat_days_equal(left: object, right: object) -> bool:
    """Compare two thermostat day lists segment by segment."""
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return left == right
    if len(left) != len(right):
        return False
    for actual, expected in zip(left, right, strict=True):
        if not isinstance(actual, Mapping) or not isinstance(expected, Mapping):
            if actual != expected:
                return False
            continue
        if not same_wire_value(actual.get("m"), expected.get("m")):
            return False
        if not same_wire_value(actual.get("t"), expected.get("t")):
            return False
    return True


def normalize_prog(payload: object) -> dict[str, list[int]] | None:
    """Validate a schedule payload into day-keyed slot lists, or ``None``.

    Accepts the REST shape ``{"prog": {...}, ...}`` or a bare day-keyed object.
    Lenient by contract: entity rendering must not raise on malformed wire
    data, so an unexpected shape reads as "no schedule yet".
    """
    if isinstance(payload, Mapping) and isinstance(payload.get("prog"), Mapping):
        payload = payload["prog"]
    if not isinstance(payload, Mapping):
        return None
    prog: dict[str, list[int]] = {}
    for day, slots in payload.items():
        if not (
            isinstance(day, str)
            and day in _PROG_DAY_KEYS
            and isinstance(slots, list)
            and all(
                isinstance(slot, int) and not isinstance(slot, bool) for slot in slots
            )
        ):
            return None
        prog[day] = list(slots)
    return prog


def validate_prog_days(days: Mapping[str, Sequence[int]]) -> None:
    """Reject wrong shapes client-side; the API rejects the array shape itself.

    Days are non-empty lists of ints. No exact slot count: 48 half-hourly
    slots are as valid as 24 hourly ones.
    """
    if not isinstance(days, Mapping):
        msg = f"prog days must be a dict of day keys to int lists, got: {bounded_repr(days)}"
        raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract
    unknown = set(days) - _PROG_DAY_KEYS
    if unknown:
        msg = (
            "prog day keys must be a subset of '0'..'6', got unknown: "
            f"{sorted(unknown, key=repr)}"
        )
        raise ValueError(msg)
    for day, slots in days.items():
        if (
            not isinstance(slots, (list, tuple))
            or not slots
            or not all(
                isinstance(slot, int) and not isinstance(slot, bool) for slot in slots
            )
        ):
            msg = (
                f"prog day {day!r} must be a non-empty list of ints, "
                f"got: {bounded_repr(slots)}"
            )
            raise ValueError(msg)
