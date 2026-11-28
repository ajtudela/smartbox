"""Neutral device facts and operation eligibility shared by the adapters.

This is the package's downstream API: the reviewed product rows, the
controller profile resolver, the storage intents/mode catalogue, the setup
view and the operation checks. First-party adapters import submodules directly
to keep their dependency direction explicit; the curated facade is what a
downstream consumer can rely on across releases. Adapter-specific predicates
and setting gates stay in their modules.
"""

from .capabilities import (
    Check,
    NodeFacts,
    OperationRefusedError,
    Reason,
    check_boost,
    check_boost_mode,
    check_charging_schedule,
    check_control_mode,
    check_easy,
    check_htr_mod_status,
    check_legacy_boost,
    check_max_stemp_limit,
    check_node_power_limit,
    check_prog_temps,
    check_runback,
    check_storage_lock,
    check_storage_mode,
    check_storage_ui_mode,
    check_storage_ui_mode_available,
    check_target_charge,
    check_thermostat_prog,
    require,
)
from .codes import (
    PRODUCT_CODE_LABELS,
    PRODUCT_CODES,
    ProductCode,
    accepts_extra_options,
    code_info,
    product_code,
)
from .control_modes import (
    control_mode_available,
    control_mode_choices,
    control_mode_label,
)
from .profile import (
    ControllerLimits,
    ControllerProfile,
    controller_health_fields,
    controller_limits,
    is_heater_surface,
    is_thermostat_surface,
    resolve_profile,
    resolve_stored_profile,
)
from .setup import SetupView
from .storage import (
    StorageActivity,
    StorageIntent,
    StorageWrite,
    available_modes,
    current_mode,
    storage_activity,
    write_for,
)

__all__ = [
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
    "controller_health_fields",
    "controller_limits",
    "current_mode",
    "is_heater_surface",
    "is_thermostat_surface",
    "product_code",
    "require",
    "resolve_profile",
    "resolve_stored_profile",
    "storage_activity",
    "write_for",
]
