"""Golden profile facts for every decoded product code.

The fixture is the reviewed table exported from the integration's parity test.
It is deliberately hand-written data, not generated from this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from smartbox.device.profile import resolve_profile

FIXTURE = Path(__file__).parent / "fixtures" / "product_codes.json"


def _setup() -> dict[str, object]:
    return {
        "units": "C",
        "power": "2000.0",
        "max_stemp_limit": "30.0",
        "extra_options": {"user_duty_limit": 0, "sensor_mode": 0},
        "factory_options": {
            "lst_available": True,
            "lst_value": 25,
            "bbc_available": True,
            "bbc_value": 50,
            "ui_mode": 0,
            "super_lock_available": True,
        },
    }


def test_every_code_resolves_to_the_reviewed_profile() -> None:
    """Limits, bounds and profiles match the exported table for all 51 codes."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert len(data["codes"]) == 51
    for code, row in data["codes"].items():
        setup = {} if code in {"1C", "2E"} else _setup()
        profile = resolve_profile(row["node_type"], f"08{code}0000", "1.9", setup)
        limits = profile.limits
        if row["min_c"] is None:
            continue
        assert limits.min_c == row["min_c"], code
        assert limits.max_c == row["max_c"], code
        assert limits.profiles == tuple(row["profiles"]), code
        assert profile.prog_bounds == _pairs(row["prog_bounds"]), code
        assert profile.climate_bounds == tuple(row["climate_bounds"]), code
        assert profile.max_limit_bounds == tuple(row["max_limit_bounds"]), code
        assert profile.boost_bounds == tuple(row["boost_bounds"]), code
        assert limits.boost_max_minutes == row["boost_cap"], code
        assert limits.ice_editable == row["ice_editable"], code


def _pairs(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: tuple(bounds) for key, bounds in value.items()}


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
