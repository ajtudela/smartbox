"""Neutral eligibility checks for device operations.

The CLI and the Home Assistant adapter both need the same answer: can this
product, wire type and setup accept this operation with these arguments? The
rules live here so the two consumers do not recompose the family predicates and
gates independently.

A :class:`Check` carries a stable :class:`Reason` and structured detail; each
consumer supplies its own wording. Live ``status`` is not an input, so a node
lost at discovery resolves the same eligibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from smartbox.core.merging import boost_config
from smartbox.protocol.values import BoostConfig
from smartbox.protocol.writes import VALID_BOOST_MODES

from .codes import code_info, product_code
from .control_modes import control_mode_available, control_mode_choices
from .modes import boost_uses_legacy_path, supports_easy, supports_runback
from .profile import (
    TEVOLVE_ATC_CODES,
    TEVOLVE_WIFI_CODES,
    is_htr_mod_controller,
    is_thermostat_surface,
)
from .settings import (
    away_offset_available,
    boost_available,
    max_temp_limit_available,
    node_power_limit_available,
    prog_temps_available,
    true_radiant_available,
    window_mode_available,
)
from .setup import SetupView
from .storage import (
    available_modes,
    classic_accumulator,
    intent_for_mode,
    is_elnur_storage,
    is_helki_storage,
    is_water_storage,
    modes_for_intent,
    ui_mode_available,
    ui_mode_options,
)


class Reason(StrEnum):
    """Why an operation is refused."""

    WRONG_NODE_TYPE = "wrong_node_type"
    WRONG_FAMILY = "wrong_family"
    NOT_AVAILABLE = "not_available"
    VALUE_NOT_OFFERED = "value_not_offered"
    CURRENT_MODE_HIDES_SETTING = "current_mode_hides_setting"
    WRONG_MODE = "wrong_mode"


@dataclass(frozen=True)
class NodeFacts:
    """Identity and setup facts an eligibility check may read."""

    node_type: str
    pid: str | None
    fw_version: str | None = None
    setup: SetupView = field(default_factory=SetupView)

    def __init__(
        self,
        node_type: str,
        pid: str | None,
        fw_version: str | None = None,
        setup: Mapping[str, object] | SetupView | None = None,
    ) -> None:
        """Normalize the setup input to the typed view."""
        object.__setattr__(self, "node_type", node_type)
        object.__setattr__(self, "pid", pid)
        object.__setattr__(self, "fw_version", fw_version)
        object.__setattr__(
            self,
            "setup",
            setup if isinstance(setup, SetupView) else SetupView(setup or {}),
        )


@dataclass(frozen=True)
class Check:
    """One operation's eligibility result."""

    ok: bool
    reason: Reason | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def allowed(cls) -> Check:
        """Return an allowed result."""
        return cls(ok=True)

    @classmethod
    def refused(cls, reason: Reason, **detail: object) -> Check:
        """Return a refused result with a stable reason and detail."""
        return cls(ok=False, reason=reason, detail=detail)


class OperationRefusedError(Exception):
    """A capability check refused an operation.

    Consumers translate ``check.reason``/``detail``; the exception keeps the
    structured result available instead of flattening it to a string.
    """

    def __init__(self, check: Check, operation: str | None = None) -> None:
        """Build the refusal from one failed check."""
        self.check = check
        self.operation = operation
        reason = check.reason.value if check.reason is not None else "refused"
        super().__init__(f"{operation or 'operation'} refused: {reason}")


def require(check: Check, operation: str | None = None) -> Check:
    """Return an allowed check or raise :class:`OperationRefusedError`."""
    if not check.ok:
        raise OperationRefusedError(check, operation)
    return check


def _product_detail(facts: NodeFacts) -> dict[str, object]:
    info = code_info(facts.pid)
    return {
        "code": product_code(facts.pid),
        "label": info.label if info is not None else None,
        "node_type": facts.node_type,
    }


def _is_acm(facts: NodeFacts) -> bool:
    return facts.node_type == "acm"


def check_storage_mode(facts: NodeFacts, mode: str | None = None) -> Check:
    """Storage mode writes target an acm controller; validate the mode."""
    if not _is_acm(facts):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    if classic_accumulator(facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    if mode is not None:
        intent = intent_for_mode(mode)
        available = available_modes(facts.node_type, facts.pid, facts.setup)
        offered = [
            {"label": wire, "value": wire}
            for item in available
            for wire in modes_for_intent(item)
        ]
        if intent is None or intent not in available:
            return Check.refused(
                Reason.VALUE_NOT_OFFERED,
                offered=offered,
                requested=mode,
                **_product_detail(facts),
            )
    return Check.allowed()


def check_storage_lock(facts: NodeFacts) -> Check:
    """Check the /lock endpoint's product scope."""
    if not _is_acm(facts):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    return Check.allowed()


def check_storage_ui_mode(facts: NodeFacts, value: int | None = None) -> Check:
    """Elnur storage UI mode, including the offered and exposed values."""
    if not (facts.node_type == "acm" and is_elnur_storage(facts.pid)):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    options = ui_mode_options(facts.pid)
    if not options:
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    offered = [{"label": label, "value": index} for label, index in options]
    if value is not None and value not in {index for _label, index in options}:
        return Check.refused(
            Reason.VALUE_NOT_OFFERED,
            offered=offered,
            requested=value,
            **_product_detail(facts),
        )
    if not ui_mode_available(facts.pid, facts.setup):
        return Check.refused(
            Reason.CURRENT_MODE_HIDES_SETTING,
            offered=offered,
            **_product_detail(facts),
        )
    return Check.allowed()


def check_storage_ui_mode_available(facts: NodeFacts) -> Check:
    """Whether the current product/setup exposes the UI-mode setting at all."""
    return check_storage_ui_mode(facts)


def check_target_charge(facts: NodeFacts, charge: int) -> Check:
    """Target charge is the Elnur classic-accumulator write."""
    if not (facts.node_type == "acm" and is_elnur_storage(facts.pid)):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    if not classic_accumulator(facts.pid, facts.setup):
        return Check.refused(Reason.WRONG_MODE, **_product_detail(facts))
    if charge not in (0, 100):
        return Check.refused(
            Reason.VALUE_NOT_OFFERED,
            offered=[{"label": "0", "value": 0}, {"label": "100", "value": 100}],
            requested=charge,
            **_product_detail(facts),
        )
    return Check.allowed()


def check_charging_schedule(facts: NodeFacts) -> Check:
    """Check that the node accepts the storage charging fields."""
    if not _is_acm(facts):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    if is_water_storage(facts.pid):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_legacy_boost(facts: NodeFacts) -> Check:
    """Check that the node uses the Helki storage /boost endpoint."""
    info = code_info(facts.pid)
    if (
        facts.node_type != "acm"
        or is_elnur_storage(facts.pid)
        or (info is not None and not is_helki_storage(facts.pid))
    ):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    return Check.allowed()


def check_htr_mod_status(facts: NodeFacts) -> Check:
    """Check that the node uses an htr_mod controller code."""
    if not is_htr_mod_controller(facts.pid, facts.node_type):
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    return Check.allowed()


def check_max_stemp_limit(facts: NodeFacts) -> Check:
    """Whether the product exposes the max-stemp setting."""
    if not max_temp_limit_available(facts.node_type, facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_prog_temps(facts: NodeFacts) -> Check:
    """Whether the product exposes editable programme temperatures."""
    if not prog_temps_available(
        facts.node_type, facts.pid, facts.fw_version, facts.setup
    ):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_thermostat_prog(facts: NodeFacts) -> Check:
    """Whether the node carries the thermostat {m,t} programme shape."""
    if not is_thermostat_surface(facts.node_type, facts.pid):
        return Check.refused(Reason.WRONG_NODE_TYPE, **_product_detail(facts))
    return Check.allowed()


def check_away_offset(facts: NodeFacts) -> Check:
    """Whether the away-offset setting applies to this node."""
    if not away_offset_available(facts.node_type, facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_true_radiant(facts: NodeFacts) -> Check:
    """Whether the true-radiant setting applies to this node."""
    if not true_radiant_available(facts.node_type, facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_window_mode(facts: NodeFacts) -> Check:
    """Whether the window-mode setting applies to this node."""
    if not window_mode_available(facts.node_type, facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_easy(facts: NodeFacts) -> Check:
    """Whether the controller exposes the EASY flag."""
    if facts.node_type == "pmo" or not supports_easy(facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_runback(facts: NodeFacts) -> Check:
    """Whether the controller exposes the RUNBACK flag."""
    if facts.node_type == "pmo" or not supports_runback(facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_boost(facts: NodeFacts) -> Check:
    """Whether the status-flag boost path applies to this controller."""
    if facts.node_type == "acm":
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    if not boost_available(
        facts.setup,
        node_type=facts.node_type,
        pid=facts.pid,
        fw_version=facts.fw_version,
    ):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_boost_mode(  # noqa: PLR0911 - one return per refusal
    facts: NodeFacts, mode: str | None = None
) -> Check:
    """Whether the FULL /boost_mode path is available, and this mode."""
    if facts.node_type == "acm":
        return Check.refused(Reason.WRONG_FAMILY, **_product_detail(facts))
    if facts.node_type == "thm":
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    boost = check_boost(facts)
    if not boost.ok:
        return boost
    if boost_uses_legacy_path(facts.pid, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    if boost_config(facts.setup) != BoostConfig.FULL:
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    if mode is not None:
        info = code_info(facts.pid)
        if info is not None and info.fixed_time_boost:
            allowed = {"max_temp_fixed_time"}
        elif info is not None and (
            info.code in TEVOLVE_WIFI_CODES or info.code in TEVOLVE_ATC_CODES
        ):
            # The endpoint-present override can post fixed-time on these too.
            allowed = set(VALID_BOOST_MODES)
        else:
            allowed = {"temp_and_time", "runback", "advanced_slot"}
        if mode not in allowed:
            return Check.refused(
                Reason.VALUE_NOT_OFFERED,
                offered=[{"label": item, "value": item} for item in sorted(allowed)],
                requested=mode,
                **_product_detail(facts),
            )
    return Check.allowed()


def check_node_power_limit(facts: NodeFacts) -> Check:
    """Whether the node carries its own ``setup.power_limit``."""
    if not node_power_limit_available(facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    return Check.allowed()


def check_control_mode(facts: NodeFacts, value: int | None = None) -> Check:
    """Whether the node exposes the control-mode setting, and this value."""
    if not control_mode_available(facts.pid, facts.node_type, facts.setup):
        return Check.refused(Reason.NOT_AVAILABLE, **_product_detail(facts))
    choices = control_mode_choices(facts.pid, facts.node_type, facts.setup) or ()
    offered = [{"label": label, "value": index} for label, index in choices]
    if value is not None and value not in {index for _label, index in choices}:
        return Check.refused(
            Reason.VALUE_NOT_OFFERED,
            offered=offered,
            requested=value,
            **_product_detail(facts),
        )
    return Check.allowed()


__all__ = [
    "Check",
    "NodeFacts",
    "OperationRefusedError",
    "Reason",
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
    "require",
]
