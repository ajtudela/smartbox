"""Golden profile facts for every decoded product code.

The fixture is the canonical reviewed table; the integration parity test reads
this file from the sibling checkout. It is deliberately hand-written data, not
generated from this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from smartbox.device.modes import (
    boost_params_editable,
    lock_kind,
    supports_easy,
    supports_runback,
)
from smartbox.device.profile import (
    diagnostic_fields,
    is_heater_surface,
    is_thermostat_surface,
    profile_bounds,
    resolve_profile,
    resolve_stored_profile,
)
from smartbox.device.settings import (
    boost_available,
    duty_limit_options,
    max_temp_limit_available,
)
from smartbox.device.storage import is_elnur_storage, is_water_storage

FIXTURE = Path(__file__).parent / "fixtures" / "product_codes.json"


def _setup() -> dict[str, object]:
    return {
        "units": "C",
        "power": "2000.0",
        "max_stemp_limit": "30.0",
        "extra_options": {"user_duty_limit": 0, "sensor_mode": 0},
        "factory_options": {
            "boost_config": 2,
            "lst_available": True,
            "lst_value": 25,
            "bbc_available": True,
            "bbc_value": 50,
            "ui_mode": 0,
            "super_lock_available": True,
        },
    }


def _pairs(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: tuple(bounds) for key, bounds in value.items()}


def test_every_code_resolves_to_the_reviewed_profile() -> None:
    """Every recorded field matches the exported table for all 51 codes."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == 2
    assert len(data["codes"]) == 51
    for code, row in data["codes"].items():
        setup = {} if code in {"1C", "2E"} else _setup()
        pid = f"08{code}0000"
        node_type = row["node_type"]
        profile = resolve_profile(node_type, pid, "1.9", setup, max_limit_c=None)
        limits = profile.limits

        assert is_heater_surface(node_type, pid) is (row["surface"] == "heater"), code
        assert is_thermostat_surface(node_type, pid) is (
            row["surface"] == "thermostat"
        ), code
        assert is_water_storage(pid) is row["water"], code
        assert is_elnur_storage(pid) is row["elnur_storage"], code
        assert lock_kind(node_type, pid, "1.9", setup) == row["lock"], code
        assert supports_easy(pid) is row["easy"], code
        assert supports_runback(pid, setup) is bool(row["runback_configs"]), code
        assert boost_available(
            setup,
            node_type=node_type,
            pid=pid,
            fw_version="1.9",
        ) is bool(row["boost_configs"]), code
        assert boost_params_editable(node_type, pid, setup) is (
            2 in row["boost_params"]
        ), code
        assert duty_limit_options(setup, pid) == tuple(
            (label, value) for label, value in row["duty_options"]
        ), code
        assert max_temp_limit_available(node_type, pid, setup) is row["max_limit"], code

        if row["min_c"] is None:
            # The power monitor has no heater profile; its bounds are skipped.
            continue
        assert limits.min_c == row["min_c"], code
        assert limits.max_c == row["max_c"], code
        assert limits.setpoint_step_c == (
            1.0 if row["node_type"] == "htr_mod" else 0.5
        ), code
        assert limits.profiles == tuple(row["profiles"]), code
        expected_kind = (
            "thresholds"
            if row["surface"] == "thermostat"
            else "on_off_auto"
            if row["water"]
            else "htr_mod"
            if row["node_type"] == "htr_mod"
            else "profiles"
        )
        assert profile.programme_kind == expected_kind, code
        if expected_kind == "profiles":
            assert profile.prog_bounds == _pairs(row["prog_bounds"]), code
        else:
            assert profile.prog_bounds is None, code
        assert profile.climate_bounds == tuple(row["climate_bounds"]), code
        assert profile.max_limit_bounds == tuple(row["max_limit_bounds"]), code
        assert profile.boost_bounds == tuple(row["boost_bounds"]), code
        assert limits.boost_max_minutes == row["boost_cap"], code
        assert limits.ice_editable == row["ice_editable"], code


def test_storage_profile_is_firmware_gated() -> None:
    """Helki storage gains the OFF slot at fw 1.8, and keeps setup bounds."""
    old = resolve_profile("acm", "081C0000", "1.7.9", {})
    new = resolve_profile("acm", "081C0000", "1.8.0", {})
    assert old.limits.profiles == ("frost", "eco", "comfort")
    assert new.limits.profiles == ("frost", "eco", "comfort", "off")
    custom = resolve_profile(
        "acm", "081C0000", "1.9", {"min_stemp": "4", "max_stemp": "26"}
    )
    assert (custom.limits.min_c, custom.limits.max_c) == (4.0, 26.0)


def test_unknown_pid_uses_the_electric_heater_default() -> None:
    """The factory default branch is 0-30 for an htr and HelkiStorage for an acm."""
    htr = resolve_profile("htr", "08990000", "1.9", {})
    acm = resolve_profile("acm", "08990000", "1.9", {})
    assert (htr.limits.min_c, htr.limits.max_c) == (0.0, 30.0)
    assert (acm.limits.min_c, acm.limits.max_c) == (5.0, 30.0)


def test_unknown_and_power_codes_on_acm_use_the_storage_controller() -> None:
    """00/03/22 have no heating-switch case, so the acm default applies."""
    for code in ("00", "03", "22"):
        limits = resolve_profile("acm", f"08{code}0000", "1.9", {}).limits
        assert limits.profiles == ("frost", "eco", "comfort", "off")
        assert (limits.min_c, limits.max_c) == (5.0, 30.0)


def test_unlisted_thm_uses_the_thermostat_limits() -> None:
    """The factory's THM default is ThermostatController (7-35)."""
    for pid in ("08000000", "08030000", "08220000", "08990000"):
        limits = resolve_profile("thm", pid, "1.9", {}).limits
        assert (limits.min_c, limits.max_c) == (7.0, 35.0)


def test_ff_is_htr_mod_only_on_the_htr_mod_wire() -> None:
    htr = resolve_profile("htr", "01FF0000", "1.9", {}).limits
    htr_mod = resolve_profile("htr_mod", "01FF0000", "1.9", {}).limits
    assert htr.htr_mod is False
    assert (htr.min_c, htr.max_c) == (0.0, 30.0)
    assert htr_mod.htr_mod is True


def test_explicit_zero_max_limit_keeps_the_catalogue() -> None:
    profile = resolve_profile("htr", "04020000", "1.9", {}, max_limit_c=0.0)
    assert profile.climate_bounds == (1.0, 30.0)


def test_boost_config_matrix_matches_the_fixture() -> None:
    """Every config 0-3 drives the same gates the parity matrix pins."""
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["codes"]
    for code, row in rows.items():
        pid = f"08{code}0000"
        for config in (0, 1, 2, 3):
            setup = _setup()
            setup["factory_options"] = {
                **setup["factory_options"],
                "boost_config": config,
            }
            assert boost_available(
                setup, node_type=row["node_type"], pid=pid, fw_version="1.9"
            ) is (config in row["boost_configs"]), (code, config)
            assert supports_runback(pid, setup) is (config in row["runback_configs"]), (
                code,
                config,
            )
            assert boost_params_editable(row["node_type"], pid, setup) is (
                config in row["boost_params"]
            ), (code, config)


def test_stored_profile_applies_the_setup_limit_and_ignores_the_sentinel() -> None:
    """resolve_stored_profile owns the setup read; resolve_profile stays explicit."""
    stored = resolve_stored_profile(
        "htr", "04020000", "1.9", {"max_stemp_limit": "23.0"}
    )
    assert stored.climate_bounds == (1.0, 23.0)
    assert stored.prog_bounds is not None
    assert stored.prog_bounds["comf_temp"] == (1.0, 23.0)
    disabled = resolve_stored_profile(
        "htr", "04020000", "1.9", {"max_stemp_limit": "0.0"}
    )
    assert disabled.climate_bounds == (1.0, 30.0)
    assert resolve_profile("htr", "04020000", "1.9").climate_bounds == (1.0, 30.0)


def test_surface_predicates() -> None:
    """THM 28 is a heater surface; other thermostats are not."""
    assert is_heater_surface("htr", "04020000")
    assert is_heater_surface("acm", "081C0000")
    assert is_heater_surface("thm", "01280000")
    assert not is_heater_surface("thm", "01040000")
    assert is_thermostat_surface("thm", "01040000")
    assert not is_thermostat_surface("thm", "01280000")


def test_max_stemp_limit_clamps_programme_ranges() -> None:
    """The app's max-stemp ceiling replaces maxCTemp for every profile range."""
    profile = resolve_profile(
        "htr",
        "04020000",
        "1.9",
        {},
        max_limit_c=23.0,
    )
    assert profile.prog_bounds == {
        "ice_temp": (0.0, 22.0),
        "eco_temp": (0.5, 22.5),
        "comf_temp": (1.0, 23.0),
    }
    assert profile.climate_bounds == (1.0, 23.0)


def test_programme_ranges_follow_the_max_limit_per_family() -> None:
    """The app's maxCTemp replacement is applied to every controller family."""
    cases = {
        "04020000": {
            "frost": (0.0, 22.0),
            "eco": (0.5, 22.5),
            "comfort": (1.0, 23.0),
        },
        "01320000": {
            "frost": (0.0, 22.0),
            "eco": (0.5, 22.5),
            "comfort": (1.0, 23.0),
        },
        "01170000": {
            "frost": (5.0, 7.0),
            "eco": (7.0, 21.0),
            "comfort": (9.0, 23.0),
        },
        "014C0000": {
            "frost": (5.0, 10.0),
            "eco": (5.5, 22.5),
            "comfort": (6.0, 23.0),
        },
        "011E0000": {
            "frost": (0.0, 22.0),
            "eco": (0.5, 22.5),
            "comfort": (1.0, 23.0),
        },
        "013D0000": {
            "frost": (0.0, 22.0),
            "eco": (0.5, 22.5),
            "comfort": (1.0, 23.0),
        },
    }
    for pid, expected in cases.items():
        limits = resolve_profile("htr", pid, "1.9", {}).limits
        for profile, bounds in expected.items():
            assert profile_bounds(limits, profile, max_limit_c=23.0) == bounds, (
                pid,
                profile,
            )


def test_htr_mod_ignores_the_stored_max_limit() -> None:
    """htr_mod keeps its fixed eco/comfort ranges and no frost slider."""
    limits = resolve_profile("htr_mod", "010A0000", "1.9", {}).limits
    assert profile_bounds(limits, "frost", max_limit_c=23.0) is None
    assert profile_bounds(limits, "eco", max_limit_c=23.0) == (10.0, 18.0)
    assert profile_bounds(limits, "comfort", max_limit_c=23.0) == (18.0, 28.0)


def test_programme_kinds_are_explicit() -> None:
    """Thermostat, water storage and power nodes do not claim heater profiles."""
    assert resolve_profile("thm", "01040000", "1.9", {}).programme_kind == "thresholds"
    assert resolve_profile("acm", "082A0000", "1.9", {}).programme_kind == "on_off_auto"
    assert resolve_profile("pmo", "01030000", "1.9", {}).programme_kind == "none"
    assert resolve_profile("htr_mod", "010A0000", "1.9", {}).programme_kind == "htr_mod"
    assert resolve_profile("htr", "04020000", "1.9", {}).programme_kind == "profiles"


def test_max_limit_on_storage_and_triac_families() -> None:
    """The app override also shifts storage and triac programme ranges."""
    storage = resolve_profile("acm", "081C0000", "1.9", {}, max_limit_c=23.0)
    assert storage.prog_bounds == {
        "ice_temp": (5.0, 22.0),
        "eco_temp": (5.5, 22.5),
        "comf_temp": (6.0, 23.0),
    }
    triac = resolve_profile("htr", "011A0000", "1.9", {}, max_limit_c=30.0)
    assert triac.prog_bounds == {
        "ice_temp": (7.0, 29.0),
        "eco_temp": (7.5, 29.5),
        "comf_temp": (8.0, 30.0),
    }


def test_diagnostic_fields_are_gated_by_controller_family() -> None:
    """The field availability follows the decoded controller classes."""
    common = {"error_code", "pcb_temp", "true_radiant_active"}
    assert diagnostic_fields("04020000", "htr") == common
    assert diagnostic_fields("010A0000", "htr_mod") == common | {
        "act_duty",
        "power_pcb_temp",
    }
    assert diagnostic_fields("01320000", "htr") == common | {
        "act_duty",
        "power_pcb_temp",
        "power_fail_det",
    }
    assert diagnostic_fields(None, "htr") == frozenset()
    assert diagnostic_fields("01320000", "acm") == frozenset()
