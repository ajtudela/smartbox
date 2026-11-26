"""Tests for boundary normalization (``smartbox.protocol.values``)."""

from __future__ import annotations

import pytest

from smartbox.protocol.errors import SmartboxError
from smartbox.protocol.values import (
    celsius_to_node_units,
    format_setpoint,
    is_lost,
    node_units_to_celsius,
    normalize_prog,
    parse_number,
    parse_number_or_none,
    prog_days,
    require_number,
    same_wire_value,
    thermostat_days_equal,
    thermostat_prog_days,
    validate_prog_days,
)

# --- parse_number ---


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (20, 20.0),
        (0, 0.0),
        (-3.5, -3.5),
        (20.5, 20.5),
        ("20.0", 20.0),
        ("0.0", 0.0),
        ("0", 0.0),
        ("-5.5", -5.5),
        (" 21.5 ", 21.5),
    ],
)
def test_parse_number_accepts_numbers_and_numeric_strings(wire, expected):
    assert parse_number(wire) == expected


@pytest.mark.parametrize(
    "wire",
    [
        True,
        False,
        None,
        "abc",
        "",
        [],
        {},
        ["20"],
        20.0 + 0j,
        # non-finite spellings Python's float() would otherwise accept
        float("nan"),
        float("inf"),
        "nan",
        "inf",
        "Infinity",
        "1e400",
        10**400,  # int too large for float(): must not escape as OverflowError
    ],
)
def test_parse_number_rejects_malformed(wire):
    with pytest.raises(SmartboxError, match="number"):
        parse_number(wire)


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (20, 20.0),
        ("20.5", 20.5),
        (" 21.5 ", 21.5),
        # Everything parse_number rejects reads as unknown for entity-grade
        # accessors, which must never raise on malformed server data.
        (True, None),
        (None, None),
        ("abc", None),
        ("nan", None),
        (float("inf"), None),
        (["20"], None),
        ({}, None),
    ],
)
def test_parse_number_or_none_reads_malformed_wire_as_none(wire, expected):
    assert parse_number_or_none(wire) == expected


def test_require_number_rejects_an_int_too_large_for_float():
    with pytest.raises(ValueError, match="finite"):
        require_number(10**400, "temp")


# --- format_setpoint ---


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (21.3, "21.0"),  # device silently quantizes off-grid values; floor to the grid
        (22.4, "22.0"),
        (22.8, "22.5"),
        (21.0, "21.0"),
        (20.5, "20.5"),
        (0.4, "0.0"),
        (10.6, "10.5"),
        (21.25, "21.0"),  # half-grid edge: exactly between 21.0 and 21.5, floor wins
    ],
)
def test_format_setpoint_celsius_quantizes_to_half_grid(temp, expected):
    assert format_setpoint(temp, "C") == expected


def test_format_setpoint_fahrenheit_quantizes_to_whole_degrees():
    """The F grid is integral, and the body keeps the app's one decimal.

    A whole-degree F body without the decimal is rejected with a 400
    (live 2026-10-18), while the app posts ``.toFixed(1)`` values.
    """
    assert format_setpoint(72.3, "F") == "72.0"
    assert format_setpoint(72.5, "F") == "73.0"  # half up
    assert format_setpoint(72.75, "F") == "73.0"
    assert format_setpoint(72.0, "F") == "72.0"
    assert format_setpoint(-3.4, "F") == "-3.0"


def test_format_setpoint_rejects_unknown_units():
    with pytest.raises(ValueError, match="units"):
        format_setpoint(21.0, "K")


@pytest.mark.parametrize("units", ["C", "F"])
@pytest.mark.parametrize(
    "temp",
    ["abc", None, True, False, float("nan"), float("inf"), float("-inf"), [21.0]],
)
def test_format_setpoint_validates_the_temperature_value(units, temp):
    with pytest.raises(ValueError, match="temp"):
        format_setpoint(temp, units)


# --- is_lost ---


def test_is_lost_bare_lost_payload():
    assert is_lost({"sync_status": "lost"})


def test_is_lost_marked_full_payload():
    assert is_lost({"sync_status": "lost", "stemp": "20.0", "units": "C"})


@pytest.mark.parametrize(
    "payload",
    [
        {"sync_status": "ok", "stemp": "20.0"},
        {"stemp": "20.0"},  # no sync_status key
        {},
    ],
)
def test_is_lost_false(payload):
    assert not is_lost(payload)


# --- same_wire_value (write confirmation) ---


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (20, 20.0),
        ("20.0", 20.0),
        ("20", "20.0"),  # the server echoes a setpoint as a whole number
        ("4", "4.0"),  # and an eco offset the same way
        ("21", 21),
    ],
)
def test_same_wire_value_compares_numbers_numerically(actual, expected):
    assert same_wire_value(actual, expected)
    assert same_wire_value(expected, actual)


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (20, 21),
        ("20", "21"),
        ("20.0", "20.5"),
        ("manual", "eco"),
        ("manual", 20),
        (None, "20"),
        ("manual", 10**400),  # too large for float: unequal, not a crash
    ],
)
def test_same_wire_value_stays_strict_outside_numbers(actual, expected):
    assert not same_wire_value(actual, expected)
    assert not same_wire_value(expected, actual)


# --- normalize_prog (the schedule-shape normalizer) ---


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # REST shape: nested under "prog", wrapper keys dropped
        ({"prog": {"0": [1, 2]}, "sync_status": "ok"}, {"0": [1, 2]}),
        # bare day-keyed object passes through
        ({"0": [1, 2], "6": [3]}, {"0": [1, 2], "6": [3]}),
        # non-dict payloads -> None
        ("garbage", None),
        (["0", [1]], None),
        (None, None),
        # non-dict "prog" -> None
        ({"prog": "garbage"}, None),
        # day keys must be strings
        ({0: [1, 2]}, None),
        # values must be lists of ints
        ({"0": "nope"}, None),
        ({"0": [1, "2"]}, None),
        ({"0": [1.5]}, None),
        # a tuple value is not a wire list
        ({"0": (1, 2)}, None),
    ],
)
def test_normalize_prog_shapes(payload, expected):
    assert normalize_prog(payload) == expected


def test_normalize_prog_copies_slot_lists():
    slots = [1, 2]
    prog = normalize_prog({"0": slots})
    assert prog is not None
    prog["0"].append(3)
    assert slots == [1, 2]


def test_normalize_prog_rejects_bools_and_unknown_days():
    """A bool is an int in Python but not a wire profile index."""
    assert normalize_prog({"0": [True]}) is None
    assert normalize_prog({"7": [1]}) is None


# --- prog_days (the write-path schedule extractor) ---


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        # The two valid wire shapes, including a genuinely empty schedule.
        ({"0": [1, 2]}, {"0": [1, 2]}),
        ({"prog": {"0": [1, 2]}, "sync_status": "ok"}, {"0": [1, 2]}),
        ({}, {}),
        ({"prog": {}}, {}),
        # Lost markers and any other wrapper are not schedules: the write path
        # must refuse them rather than merging garbage into the POST body.
        ({"sync_status": "lost"}, None),
        ({"prog": {"sync_status": "lost"}}, None),
        ({"prog": "garbage"}, None),
        ({"0": [True]}, None),
        ({"7": [1]}, None),
        ({"0": [1, "2"]}, None),
        ("garbage", None),
    ],
)
def test_prog_days_shapes(slot, expected):
    assert prog_days(slot) == expected


def test_validate_prog_days_reports_mixed_unknown_keys_as_value_error():
    """A mixed-type key set must not raise TypeError from sorted()."""
    with pytest.raises(ValueError, match="unknown"):
        validate_prog_days({1: [1], "x": [1]})


def test_celsius_to_node_units_snaps_fahrenheit_to_whole_degrees():
    """The app shows a stored 24.0 °C as 75.0 °F (decoded 2026-10-18)."""
    assert celsius_to_node_units(24.0, "C") == 24.0
    assert celsius_to_node_units(24.0, "F") == 75.0
    assert celsius_to_node_units(30.0, "F") == 86.0
    assert celsius_to_node_units(16.0, "F") == 61.0


def test_node_units_to_celsius_snaps_fahrenheit_to_the_half_grid():
    """A Celsius input passes through (format_setpoint floors it); F snaps."""
    assert node_units_to_celsius(22.4, "C") == 22.4
    assert node_units_to_celsius(75.0, "F") == 24.0
    assert node_units_to_celsius(86.0, "F") == 30.0
    assert node_units_to_celsius(70.0, "F") == 21.0


@pytest.mark.parametrize(
    "slot",
    [
        "nope",
        {"7": []},
        {"0": "x"},
        {"0": [19.0]},
        {"0": [{"m": True, "t": 19}]},
        {"0": [{"m": 360, "t": True}]},
    ],
)
def test_thermostat_prog_days_rejects_malformed_shapes(slot):
    assert thermostat_prog_days(slot) is None


def test_thermostat_prog_days_reads_both_shapes():
    day = [{"m": 360, "t": 19}]
    assert thermostat_prog_days({"prog": {"0": day}}) == {"0": day}
    assert thermostat_prog_days({"0": day}) == {"0": day}


def test_thermostat_days_equal_compares_segment_by_segment():
    assert thermostat_days_equal("left", ["right"]) is False
    assert thermostat_days_equal([1], [1, 2]) is False
    assert thermostat_days_equal([1], ["1"]) is False
    assert thermostat_days_equal([{"m": 1, "t": 19}], [{"m": 2, "t": 19}]) is False
    assert thermostat_days_equal([{"m": 1, "t": 19}], [{"m": 1, "t": 20}]) is False
    assert thermostat_days_equal([{"m": 1, "t": 19}], [{"m": 1, "t": "19"}]) is True
