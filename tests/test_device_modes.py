"""Neutral lock and boost policy."""

from smartbox.device.modes import (
    boost_activation_mode,
    boost_activation_takes_parameters,
    boost_modes_available,
    boost_params_editable,
    lock_kind,
    supports_easy,
    supports_runback,
)
from smartbox.device.profile import (
    ELNUR_STORAGE_CODES,
    HAVERLAND_FIXED_TIME_CODES,
    HELKI_STORAGE_CODES,
    TEVOLVE_ATC_CODES,
    TEVOLVE_WIFI_CODES,
)


def test_lock_kinds_follow_the_controller_payload() -> None:
    flag = {"factory_options": {"super_lock_available": True}}
    assert lock_kind("htr", "01320000", "1.9", flag) == "int"
    assert lock_kind("htr", "01320000", "1.9", {}) == "bool"
    assert lock_kind("htr", "011D0000", "1.6.0") == "bool"
    assert lock_kind("htr", "011D0000", "1.7.0") == "int"
    assert lock_kind("htr", "01470000", "1.6.0") == "int"
    assert lock_kind("acm", "081C0000", "1.9") == "int"
    assert lock_kind("htr", "04020000", "1.9") == "bool"


def test_legacy_boost_parameters() -> None:
    legacy = {"factory_options": {"boost_config": 2}}
    assert boost_params_editable("htr", "0132", legacy) is True
    assert boost_params_editable("htr", "0117", legacy) is False
    assert boost_params_editable("acm", "081C", {"factory_options": {}}) is True
    assert boost_params_editable("acm", "0814", {"factory_options": {}}) is False


def test_boost_activation_mode_covers_every_family() -> None:
    """Storage, fixed-time, WiFi/ATC and generic FULL branches."""
    full = {"factory_options": {"boost_config": 3}}

    for code in HELKI_STORAGE_CODES | ELNUR_STORAGE_CODES:
        assert boost_activation_mode(f"01{code}", full, {}) is None
    for code in HAVERLAND_FIXED_TIME_CODES:
        assert boost_activation_mode(f"01{code}", full, {}) == "max_temp_fixed_time"
    for code in TEVOLVE_WIFI_CODES | TEVOLVE_ATC_CODES:
        assert (
            boost_activation_mode(
                f"01{code}", full, {"boost_modes_available": ["temp_and_time"]}
            )
            == "temp_and_time"
        )
        assert (
            boost_activation_mode(
                f"01{code}",
                full,
                {"boost_modes_available": ["max_temp_fixed_time"]},
            )
            == "max_temp_fixed_time"
        )
        assert boost_activation_mode(f"01{code}", full, {}) is None

    assert boost_activation_mode("01360000", full, {}) == "temp_and_time"
    assert boost_activation_mode("01360000", {}, {}) is None


def test_boost_modes_available_filters_bad_values() -> None:
    assert boost_modes_available(None) == ()
    assert boost_modes_available({"boost_modes_available": "temp_and_time"}) == ()
    assert boost_modes_available(
        {"boost_modes_available": ["temp_and_time", 1, "runback"]}
    ) == ("temp_and_time", "runback")


def test_no_probes_thermostat_hides_easy_runback_and_boost_params() -> None:
    no_probes = {"extra_options": {"sensor_mode": 3}}
    assert supports_easy("01280000", no_probes) is False
    assert supports_runback("01280000", no_probes) is False
    assert boost_params_editable("thm", "01280000", no_probes) is False


def test_boost_parameter_editing_without_an_explicit_config() -> None:
    assert boost_params_editable("htr", "01360000", {}) is True
    assert boost_params_editable("htr", "011A0000", {}) is True
    assert boost_params_editable("htr", "01270000", {}) is False
    assert boost_params_editable("htr", "01020000", {}) is False


def test_boost_activation_parameter_carrying() -> None:
    assert boost_activation_takes_parameters("01170000", {}) is False
    assert boost_activation_takes_parameters("01360000", {}) is True
    full = {"factory_options": {"boost_config": 1}}
    assert boost_activation_takes_parameters("011A0000", full) is False
