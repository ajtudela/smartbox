"""Neutral control-mode catalogue for the library."""

from smartbox.device.control_modes import (
    control_mode_available,
    control_mode_choices,
    control_mode_label,
)


def test_confirmed_lhz_relay_map() -> None:
    choices = control_mode_choices("0402", "htr", {"control_mode": 5}, "C")
    assert choices == (
        ("PID (15 min. period)", 5),
        ("PID (30 min. period)", 6),
        ("Hysteresis (0.2 °C)", 0),
        ("Hysteresis (0.3 °C)", 1),
        ("Hysteresis (0.5 °C)", 2),
        ("Hysteresis (0.7 °C)", 3),
    )
    assert control_mode_label(choices, 5) == "PID (15 min. period)"


def test_triac_and_storage_wire_orders() -> None:
    triac = control_mode_choices("0401", "htr", {"control_mode": 4}, "C")
    assert triac is not None
    assert control_mode_label(triac, 4) == "PID"
    storage = control_mode_choices("041C", "acm", {"control_mode": 5}, "C")
    assert storage is not None
    assert control_mode_label(storage, 1) == "Hysteresis (0.2 °C)"


def test_fahrenheit_labels() -> None:
    choices = control_mode_choices(
        "0402", "htr", {"control_mode": 0, "units": "F"}, "F"
    )
    assert choices is not None
    assert control_mode_label(choices, 0) == "Hysteresis (0.4 °F)"


def test_thermostat_family_is_withheld() -> None:
    assert control_mode_choices("0404", "thm", {"control_mode": 2}, "C") is None
    assert not control_mode_available("0404", "thm", {"control_mode": 2}, "C")
