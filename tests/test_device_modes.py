"""Neutral lock and boost policy."""

from smartbox.device.modes import boost_params_editable, lock_kind


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
