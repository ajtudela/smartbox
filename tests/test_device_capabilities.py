"""Operation eligibility checks shared by the CLI and the HA adapter."""

from __future__ import annotations

import pytest

from smartbox.device.capabilities import (
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


def _facts(
    node_type: str,
    pid: str | None,
    *,
    ui_mode: int | None = None,
    max_stemp: str | None = "23.0",
) -> NodeFacts:
    factory: dict[str, object] = {}
    if ui_mode is not None:
        factory["ui_mode"] = ui_mode
    setup: dict[str, object] = {"factory_options": factory}
    if max_stemp is not None:
        setup["max_stemp_limit"] = max_stemp
    return NodeFacts(node_type=node_type, pid=pid, setup=setup)


def test_storage_mode_and_lock_need_an_acm_node() -> None:
    # The factory default treats any acm as Helki storage, including unreviewed
    # and non-storage codes; only the non-acm wire types are refused.
    for pid in ("081C0000", "08140000", "082A0000", "04020000", None):
        assert check_storage_mode(_facts("acm", pid)).ok
        assert check_storage_lock(_facts("acm", pid)).ok
    for node_type, pid in (("htr", "04020000"), ("thm", "08040000")):
        check = check_storage_mode(_facts(node_type, pid))
        assert not check.ok
        assert check.reason is Reason.WRONG_FAMILY
        assert check.detail["code"] == pid[2:4]


def test_storage_mode_rejects_the_classic_accumulator_and_unoffered_modes() -> None:
    classic = check_storage_mode(_facts("acm", "08140000", ui_mode=2), "auto")
    assert not classic.ok
    assert classic.reason is Reason.NOT_AVAILABLE
    # A normal E2 Ecombi supports AUTO; a classic accumulator does not.
    assert check_storage_mode(_facts("acm", "08200000", ui_mode=0), "auto").ok
    bad = check_storage_mode(_facts("acm", "081C0000"), "self_learn")
    assert bad.reason is Reason.VALUE_NOT_OFFERED


def test_storage_ui_mode_offers_and_exposes_the_value() -> None:
    classic = _facts("acm", "08140000", ui_mode=2)
    assert check_storage_ui_mode(classic, 1).ok
    assert check_storage_ui_mode_available(classic).ok

    unoffered = check_storage_ui_mode(classic, 0)
    assert not unoffered.ok
    assert unoffered.reason is Reason.VALUE_NOT_OFFERED
    assert unoffered.detail["requested"] == 0

    hidden = check_storage_ui_mode(_facts("acm", "08140000", ui_mode=0))
    assert not hidden.ok
    assert hidden.reason is Reason.CURRENT_MODE_HIDES_SETTING

    water = check_storage_ui_mode(_facts("acm", "082A0000", ui_mode=0))
    assert not water.ok
    assert water.reason is Reason.NOT_AVAILABLE

    wrong = check_storage_ui_mode(_facts("htr", "04020000"), 2)
    assert not wrong.ok
    assert wrong.reason is Reason.WRONG_FAMILY


def test_target_charge_is_the_classic_accumulator_write() -> None:
    assert check_target_charge(_facts("acm", "08140000", ui_mode=2), 100).ok
    wrong_mode = check_target_charge(_facts("acm", "08140000", ui_mode=1), 100)
    assert wrong_mode.reason is Reason.WRONG_MODE
    wrong_family = check_target_charge(_facts("htr", "04020000"), 100)
    assert wrong_family.reason is Reason.WRONG_FAMILY
    bad_value = check_target_charge(_facts("acm", "08140000", ui_mode=2), 50)
    assert bad_value.reason is Reason.VALUE_NOT_OFFERED


def test_charging_schedule_covers_both_storage_families_but_not_water() -> None:
    assert check_charging_schedule(_facts("acm", "081C0000")).ok
    assert check_charging_schedule(_facts("acm", "08140000")).ok
    assert (
        check_charging_schedule(_facts("acm", "082A0000")).reason
        is Reason.NOT_AVAILABLE
    )
    assert (
        check_charging_schedule(_facts("htr", "04020000")).reason is Reason.WRONG_FAMILY
    )


def test_legacy_boost_is_helki_storage_only() -> None:
    assert check_legacy_boost(_facts("acm", "081C0000")).ok
    for pid in ("08140000", "082A0000"):
        assert check_legacy_boost(_facts("acm", pid)).reason is Reason.WRONG_FAMILY


def test_htr_mod_and_max_stemp_checks() -> None:
    assert check_htr_mod_status(_facts("htr_mod", "080A0000")).ok
    assert check_htr_mod_status(_facts("htr", "08FF0000")).reason is Reason.WRONG_FAMILY
    assert check_max_stemp_limit(_facts("htr", "04020000")).ok
    assert (
        check_max_stemp_limit(_facts("acm", "081C0000")).reason is Reason.NOT_AVAILABLE
    )


def test_boolean_flags_and_power_limit_checks() -> None:
    assert check_easy(_facts("htr", "081A0000")).ok
    assert check_easy(_facts("htr", "04020000")).reason is Reason.NOT_AVAILABLE
    assert check_runback(_facts("htr", "081A0000")).ok
    assert check_runback(_facts("htr", "04020000")).reason is Reason.NOT_AVAILABLE

    full = NodeFacts(
        node_type="htr",
        pid="08340000",
        setup={"factory_options": {"boost_config": 3}},
    )
    assert check_boost(full).ok
    assert check_boost_mode(full).ok
    assert check_boost(_facts("acm", "081C0000")).reason is Reason.WRONG_FAMILY
    assert check_boost_mode(_facts("acm", "081C0000")).reason is Reason.WRONG_FAMILY
    config_two = NodeFacts(
        node_type="htr",
        pid="08340000",
        setup={"factory_options": {"boost_config": 2}},
    )
    assert check_boost(config_two).ok
    assert check_boost_mode(config_two).reason is Reason.NOT_AVAILABLE

    powered = NodeFacts(node_type="htr", pid="04020000", setup={"power_limit": "1000"})
    assert check_node_power_limit(powered).ok
    assert (
        check_node_power_limit(_facts("htr", "04020000")).reason is Reason.NOT_AVAILABLE
    )


def test_control_mode_uses_the_product_wire_catalogue() -> None:
    facts = NodeFacts(
        node_type="htr",
        pid="04020000",
        setup={"control_mode": 5, "units": "C", "factory_options": {}},
    )
    assert check_control_mode(facts).ok
    assert check_control_mode(facts, 5).ok
    assert check_control_mode(facts, 6).ok
    assert check_control_mode(facts, 4).reason is Reason.VALUE_NOT_OFFERED
    hidden = [
        NodeFacts(
            node_type="htr_mod",
            pid="080A0000",
            setup={"control_mode": 5, "factory_options": {}},
        ),
        NodeFacts(
            node_type="acm",
            pid="082A0000",
            setup={"control_mode": 5, "factory_options": {}},
        ),
        NodeFacts(
            node_type="acm",
            pid="08140000",
            setup={"control_mode": 5, "factory_options": {"ui_mode": 2}},
        ),
        NodeFacts(
            node_type="thm",
            pid="08280000",
            setup={
                "control_mode": 5,
                "extra_options": {"sensor_mode": 3},
                "factory_options": {},
            },
        ),
        NodeFacts(
            node_type="thm",
            pid="08040000",
            setup={"factory_options": {}},
        ),
    ]
    for facts in hidden:
        assert check_control_mode(facts).reason is Reason.NOT_AVAILABLE, facts


def test_boost_mode_requires_the_fixed_time_mode_for_that_family() -> None:
    fixed = NodeFacts(
        node_type="htr",
        pid="08170000",
        setup={"factory_options": {"boost_config": 3}},
    )
    assert check_boost_mode(fixed, "max_temp_fixed_time").ok
    assert check_boost_mode(fixed, "runback").reason is Reason.VALUE_NOT_OFFERED
    generic = NodeFacts(
        node_type="htr",
        pid="08340000",
        setup={"factory_options": {"boost_config": 3}},
    )
    assert check_boost_mode(generic, "runback").ok
    assert check_boost_mode(generic, "max_temp_fixed_time").ok
    generic_relay = NodeFacts(
        node_type="htr",
        pid="08360000",
        setup={"factory_options": {"boost_config": 3}},
    )
    assert check_boost_mode(generic_relay, "runback").ok
    assert (
        check_boost_mode(generic_relay, "max_temp_fixed_time").reason
        is Reason.VALUE_NOT_OFFERED
    )


def test_programme_temperature_and_thermostat_programme_checks() -> None:
    assert check_prog_temps(_facts("htr", "04020000")).ok
    assert (
        check_prog_temps(_facts("acm", "08140000", ui_mode=2)).reason
        is Reason.NOT_AVAILABLE
    )
    assert check_thermostat_prog(_facts("thm", "08040000")).ok
    assert (
        check_thermostat_prog(_facts("htr", "04020000")).reason
        is Reason.WRONG_NODE_TYPE
    )


def test_require_raises_the_structured_refusal() -> None:
    check = check_charging_schedule(_facts("htr", "04020000"))
    with pytest.raises(OperationRefusedError) as err:
        require(check, "set_charging_schedule")
    assert err.value.operation == "set_charging_schedule"
    assert err.value.check.reason is Reason.WRONG_FAMILY
    assert err.value.check.detail["code"] == "02"
