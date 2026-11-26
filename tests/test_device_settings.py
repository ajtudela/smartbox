"""Neutral setting gates."""

from typing import Any

from smartbox.device.settings import (
    away_offset_available,
    boost_available,
    duty_limit_available,
    duty_limit_options,
    duty_limit_value,
    max_temp_limit_available,
    pir_enabled,
    priority_available,
    prog_temps_available,
    true_radiant_available,
    window_mode_available,
)

BASE_SETUP: dict[str, Any] = {
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


def test_pir_gate_reads_the_extra_option() -> None:
    assert pir_enabled({"extra_options": {"pir_enabled": True}}) is True
    assert pir_enabled({"extra_options": {"pir_enabled": False}}) is False
    assert pir_enabled({"extra_options": {"pir_enabled": "true"}}) is False
    assert pir_enabled({}) is False


def test_setting_gates_reject_unsupported_shapes() -> None:
    assert window_mode_available("acm", "082A0000", BASE_SETUP) is False
    assert true_radiant_available("acm", "082A0000", BASE_SETUP) is False
    assert away_offset_available("htr_mod", "010A0000", BASE_SETUP) is False
    assert away_offset_available("acm", "082A0000", BASE_SETUP) is False
    assert away_offset_available("htr", "04020000", {}) is False


def test_max_temp_gate_rejects_unknown_and_htr_mod() -> None:
    assert max_temp_limit_available("htr", None, BASE_SETUP) is False
    assert max_temp_limit_available("htr_mod", "010A0000", BASE_SETUP) is False
    assert max_temp_limit_available("htr", "010A0100", BASE_SETUP) is False
    assert max_temp_limit_available("thm", "01280000", BASE_SETUP) is False


def test_duty_limit_options_returns_empty_without_the_gate_keys() -> None:
    assert duty_limit_options(BASE_SETUP, None) == ()
    assert duty_limit_options(BASE_SETUP, "04020000") == ()
    assert duty_limit_options({"extra_options": {}}, "01320000") == ()
    assert (
        duty_limit_options(
            {"power": "0", "extra_options": {"user_duty_limit": 0}},
            "01320000",
        )
        == ()
    )


def test_floor_thermostat_gates_follow_no_probes() -> None:
    no_probes = {"extra_options": {"sensor_mode": 3}}
    assert window_mode_available("thm", "01280000", no_probes) is False
    assert true_radiant_available("thm", "01280000", no_probes) is False
    assert boost_available(no_probes, node_type="thm", pid="01280000") is False


def test_prog_temps_gate_rejects_htr_mod_and_no_probes() -> None:
    assert prog_temps_available("htr", "010A0000", "1.9", {}) is False
    no_probes = {"extra_options": {"sensor_mode": 3}}
    assert prog_temps_available("thm", "01280000", "1.9", no_probes) is False


def test_duty_limit_value_reads_the_stored_factor() -> None:
    assert duty_limit_value({"extra_options": {"user_duty_limit": "50"}}) == 50
    assert duty_limit_value({}) is None
    assert duty_limit_value({"extra_options": {"user_duty_limit": "x"}}) is None


def test_duty_limit_options_skip_unreadable_and_unavailable_presets() -> None:
    setup = {
        "power": "1500",
        "extra_options": {"user_duty_limit": 0},
        "factory_options": {"lst_available": True, "lst_value": 0},
    }
    assert duty_limit_options(setup, "01320000") == (("Off", 0),)
    assert duty_limit_available(None, setup) is False
    assert duty_limit_available("04020000", setup) is False


def test_lhz_duty_choices_skip_duplicate_values() -> None:
    setup = {
        "power": "1500",
        "extra_options": {"user_duty_limit": 0},
        "factory_options": {"lst_available": True, "lst_value": 80},
    }
    assert duty_limit_options(setup, "01330000") == (
        ("Off", 0),
        ("70 %", 70),
        ("80 %", 80),
        ("90 %", 90),
    )
