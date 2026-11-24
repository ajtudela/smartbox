"""Neutral setting gates."""

from smartbox.device.settings import (
    away_offset_available,
    boost_available,
    duty_limit_available,
    duty_limit_options,
    max_temp_limit_available,
    priority_available,
    prog_temps_available,
    true_radiant_available,
    window_mode_available,
)

BASE_SETUP = {
    "units": "C",
    "away_offset": "2.0",
    "max_stemp_limit": "30.0",
    "priority": 1,
    "power": "2000.0",
    "extra_options": {"user_duty_limit": 0},
    "factory_options": {
        "window_mode_available": True,
        "true_radiant_available": True,
        "lst_available": True,
        "lst_value": 25,
        "bbc_available": True,
        "bbc_value": 50,
    },
}


def test_boost_availability_rules() -> None:
    assert boost_available(BASE_SETUP, node_type="htr", pid="01170000") is True
    fixed = {"factory_options": {"boost_config": 1}}
    assert boost_available(fixed, node_type="htr", pid="011D0000") is False
    config2 = {"factory_options": {"boost_config": 2}}
    assert boost_available(config2, node_type="htr", pid="01320000") is True
    assert boost_available({}, node_type="htr", pid="04020000") is False
    assert boost_available({}, node_type="acm", pid="081C0000") is True
    assert boost_available({}, node_type="acm", pid="08140000") is False


def test_window_and_radiance_gates() -> None:
    assert window_mode_available("htr", "04020000", BASE_SETUP) is True
    assert true_radiant_available("htr", "04020000", BASE_SETUP) is True
    classic = {
        **BASE_SETUP,
        "factory_options": {**BASE_SETUP["factory_options"], "ui_mode": 2},
    }
    assert window_mode_available("acm", "08140000", classic) is False
    assert true_radiant_available("acm", "08140000", classic) is False
    assert window_mode_available("acm", "082A0000", BASE_SETUP) is False


def test_setting_gates_and_duty_order() -> None:
    assert away_offset_available("htr", "04020000", BASE_SETUP) is True
    assert away_offset_available("htr_mod", "010A0000", BASE_SETUP) is False
    assert max_temp_limit_available("htr", "04020000", BASE_SETUP) is True
    assert max_temp_limit_available("htr", "011A0000", BASE_SETUP) is False
    # The app's array is matched as an exact pid, not a prefix.
    assert max_temp_limit_available("htr", "0A01", BASE_SETUP) is False
    assert max_temp_limit_available("htr", "0A010000", BASE_SETUP) is True
    assert priority_available(BASE_SETUP, box_power_limit_available=True) is True
    assert priority_available(BASE_SETUP, box_power_limit_available=False) is False
    assert duty_limit_available("0138", BASE_SETUP) is True
    assert duty_limit_options(BASE_SETUP, "0138") == (
        ("Off", 0),
        ("LST", 25),
        ("Baby Care", 50),
        ("70 %", 70),
        ("80 %", 80),
        ("90 %", 90),
    )


def test_programme_temperature_gate() -> None:
    assert prog_temps_available("htr", "04020000", "1.9", BASE_SETUP) is True
    assert prog_temps_available("htr_mod", "010A0000", "1.9", BASE_SETUP) is False
    assert prog_temps_available("acm", "082A0000", "1.9", BASE_SETUP) is False
