"""Controller-supplied programme bounds on the write sanitizer."""

from __future__ import annotations

import pytest

from smartbox.core.merging import sanitize_prog_temps, write_intent
from smartbox.core.state import NodeState
from smartbox.protocol.values import thermostat_prog_days

BASE_BOUNDS = {
    "ice_temp": (0.0, 29.0),
    "eco_temp": (0.5, 29.5),
    "comf_temp": (1.0, 30.0),
}


def _state(units: str = "C", max_limit: str | None = None) -> NodeState:
    setup = {} if max_limit is None else {"max_stemp_limit": max_limit}
    return NodeState(
        status={
            "units": units,
            "ice_temp": "1.0",
            "eco_temp": "1.5",
            "comf_temp": "2.0",
        },
        setup=setup,
    )


def test_default_bounds_keep_the_historical_five_to_thirty_clamp() -> None:
    result = sanitize_prog_temps(
        _state(),
        {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 2.0},
    )
    assert result == {"ice_temp": 5.0, "eco_temp": 5.5, "comf_temp": 6.0}


def test_controller_bounds_allow_the_lower_base_profile() -> None:
    result = sanitize_prog_temps(
        _state(),
        {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 2.0},
        bounds_c=BASE_BOUNDS,
        offset_c=0.5,
    )
    assert result == {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 2.0}


def test_controller_bounds_apply_the_live_max_stemp_limit() -> None:
    result = sanitize_prog_temps(
        _state(max_limit="8.0"),
        {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 10.0},
        bounds_c=BASE_BOUNDS,
        offset_c=0.5,
    )
    assert result == {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 8.0}


def test_controller_bounds_convert_to_fahrenheit() -> None:
    result = sanitize_prog_temps(
        _state("F"),
        {"ice_temp": 41.0, "eco_temp": 42.0, "comf_temp": 43.0},
        bounds_c={
            "ice_temp": (5.0, 10.0),
            "eco_temp": (5.5, 29.5),
            "comf_temp": (6.0, 30.0),
        },
        offset_c=0.5,
    )
    assert result == {"ice_temp": 41.0, "eco_temp": 42.0, "comf_temp": 43.0}


def test_thermostat_prog_accepts_integral_numeric_string_minutes() -> None:
    """The server may reformat ``m`` as a numeric string."""
    days = {"0": [{"m": "360", "t": "19.0"}, {"m": 1440.0, "t": "17.0"}]}
    assert thermostat_prog_days(days) == {
        "0": [{"m": 360, "t": "19.0"}, {"m": 1440, "t": "17.0"}]
    }
    assert thermostat_prog_days({"0": [{"m": "360.5", "t": "19.0"}]}) is None


def test_thermostat_prog_write_intent_uses_m_t_days() -> None:
    """Thermostat {m,t} days are evidence, unlike heater int slots."""
    same = {"0": [{"m": 360, "t": "19.0"}]}
    state = NodeState(prog={"prog": same})
    assert write_intent("prog", state, {"prog": same, "units": "C"}) == {}

    changed = {"0": [{"m": 360, "t": "20.0"}]}
    intent = write_intent("prog", state, {"prog": changed, "units": "C"})
    assert intent == {"0": [{"m": 360, "t": "20.0"}]}


def test_prog_bounds_reject_partial_controller_mapping() -> None:
    """A missing profile is a ValueError, not a KeyError."""
    with pytest.raises(ValueError, match="missing profiles"):
        sanitize_prog_temps(
            _state(),
            {"ice_temp": 1.0, "eco_temp": 1.5, "comf_temp": 2.0},
            bounds_c={"ice_temp": (0.0, 29.0)},
        )
