"""The typed setup view centralizes the wire mapping conventions."""

from __future__ import annotations

from smartbox.device.capabilities import NodeFacts
from smartbox.device.setup import SetupView


def test_setup_view_accessors() -> None:
    view = SetupView(
        {
            "units": "F",
            "control_mode": 5,
            "operational_mode": 1,
            "power_limit": "1000",
            "max_stemp_limit": "0.0",
            "factory_options": {"ui_mode": 2, "boost_config": 3},
            "extra_options": {"sensor_mode": 3, "user_duty_limit": 25},
            "extra_nrg_conf": {"enabled": True, "mode": 1},
        }
    )
    assert view["units"] == "F"
    assert view.control_mode == 5
    assert view.operational_mode == 1
    assert view.max_stemp_limit is None
    assert view.ui_mode == 2
    assert view.sensor_mode == 3
    assert view.extra_options["user_duty_limit"] == 25
    assert view.extra_nrg_enabled is True
    assert view.extra_nrg_mode == 1
    assert len(view) == len(view.raw)
    assert dict(view) == view.raw


def test_setup_view_normalizes_non_mappings_and_bools() -> None:
    assert SetupView(None).ui_mode is None  # type: ignore[arg-type]
    view = SetupView(
        {"control_mode": True, "factory_options": {"ui_mode": "2", "boost_config": 3}}
    )
    assert view.control_mode is None
    assert view.ui_mode is None
    assert SetupView({"operational_mode": 1.0}).operational_mode == 1
    assert SetupView({"operational_mode": 1.5}).operational_mode is None


def test_node_facts_normalize_the_setup() -> None:
    facts = NodeFacts(node_type="htr", pid="04020000", setup={"units": "C"})
    assert isinstance(facts.setup, SetupView)
    assert facts.setup["units"] == "C"
