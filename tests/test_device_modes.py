"""Neutral lock and boost policy."""

from smartbox.device.modes import (
    boost_activation_mode,
    boost_params_configurable,
    boost_params_editable,
    lock_kind,
    runback_params_configurable,
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


def test_full_boost_visibility_follows_the_advertised_modes() -> None:
    setup = {"factory_options": {"boost_config": 3}}
    temp = {"boost_modes_available": ["temp_and_time"]}
    fixed = {"boost_modes_available": ["max_temp_fixed_time"]}
    both = {"boost_modes_available": ["temp_and_time", "runback"]}
    assert boost_activation_mode("0134", setup, temp) == "temp_and_time"
    assert boost_activation_mode("0134", setup, fixed) == "max_temp_fixed_time"
    assert boost_activation_mode("0134", setup, {}) is None
    assert boost_params_configurable("htr", "0134", setup, temp) is True
    assert boost_params_configurable("htr", "0134", setup, fixed) is False
    assert runback_params_configurable("0134", setup, both) is True
    assert runback_params_configurable("0134", setup, temp) is False


def test_legacy_boost_parameters() -> None:
    legacy = {"factory_options": {"boost_config": 2}}
    assert boost_params_editable("htr", "0132", legacy) is True
    assert boost_params_editable("htr", "0117", legacy) is False
    assert boost_params_editable("acm", "081C", {"factory_options": {}}) is True
    assert boost_params_editable("acm", "0814", {"factory_options": {}}) is False
