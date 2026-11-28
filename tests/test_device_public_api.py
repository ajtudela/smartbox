"""The package facade is the curated downstream API.

First-party code imports submodules directly; this test freezes what external
consumers may rely on.
"""

from __future__ import annotations

import smartbox
from smartbox import device


def test_root_package_binds_device() -> None:
    assert smartbox.device is device


EXPECTED = {
    "PRODUCT_CODES",
    "PRODUCT_CODE_LABELS",
    "Check",
    "ControllerLimits",
    "ControllerProfile",
    "NodeFacts",
    "OperationRefusedError",
    "ProductCode",
    "Reason",
    "SetupView",
    "StorageActivity",
    "StorageIntent",
    "StorageWrite",
    "accepts_extra_options",
    "available_modes",
    "check_boost",
    "check_boost_mode",
    "check_charging_schedule",
    "check_control_mode",
    "check_easy",
    "check_htr_mod_status",
    "check_legacy_boost",
    "check_max_stemp_limit",
    "check_node_power_limit",
    "check_prog_temps",
    "check_runback",
    "check_storage_lock",
    "check_storage_mode",
    "check_storage_ui_mode",
    "check_storage_ui_mode_available",
    "check_target_charge",
    "check_thermostat_prog",
    "code_info",
    "control_mode_available",
    "control_mode_choices",
    "control_mode_label",
    "controller_limits",
    "current_mode",
    "controller_health_fields",
    "is_heater_surface",
    "is_thermostat_surface",
    "product_code",
    "resolve_profile",
    "resolve_stored_profile",
    "require",
    "storage_activity",
    "write_for",
}


def test_public_facade_is_exact() -> None:
    assert set(device.__all__) == EXPECTED
    assert len(device.__all__) == len(EXPECTED)
    for name in EXPECTED:
        assert getattr(device, name) is not None, name


def test_implementation_details_stay_in_their_modules() -> None:
    for name in (
        "PROFILES_BASE",
        "PROFILES_HTR_MOD",
        "UI_MIN_TEMP_C",
        "HTR_WIRE",
        "boost_available",
        "is_elnur_storage",
        "ui_mode_options",
    ):
        assert not hasattr(device, name), name
