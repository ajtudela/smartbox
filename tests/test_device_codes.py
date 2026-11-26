"""The reviewed product-code table and every derived table."""

from __future__ import annotations

import json
from pathlib import Path

from smartbox.device import control_modes
from smartbox.device.codes import (
    PRODUCT_CODE_LABELS,
    PRODUCT_CODES,
    ProductCode,
    accepts_extra_options,
    codes_for_limits,
    product_code,
)
from smartbox.device.control_modes import ACTUATION, EXPLICIT_FAMILY
from smartbox.device.profile import (
    _CODE_LIMITS,
    HAVERLAND_FIXED_TIME_CODES,
    HEATER_SURFACE_THM_CODES,
    LHZ_HEATER_CODES,
)
from smartbox.device.storage import _UI_MODE_OPTIONS, ui_mode_options
from smartbox.protocol.writes import WriteApi

FIXTURE = Path(__file__).parent / "fixtures" / "product_codes.json"
FIXTURE_DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_codes_cover_the_reviewed_fixture() -> None:
    """The row table and the reviewed fixture contain the same 51 codes."""
    codes = set(FIXTURE_DATA["codes"])
    assert set(PRODUCT_CODES) == codes
    assert len(PRODUCT_CODES) == 51
    assert "2.54.2" in FIXTURE_DATA["source"]
    assert (
        "f4687447d9cff9bd9c04c4651d320501bb3e28020091b463ac177e153797ea96"
        in FIXTURE_DATA["source"]
    )
    assert "5,856,496" in FIXTURE_DATA["source"]


def test_derived_tables_match_the_reviewed_fixture() -> None:
    """Every derived set and map agrees with the fixture, not just its keys."""
    rows = FIXTURE_DATA["codes"]
    assert set(_CODE_LIMITS) == set(rows)
    assert {
        code: row["family"]
        for code, row in rows.items()
        if code not in {"00", "03", "22"}
        and row["family"] in {"htr", "storage", "thermostat"}
    } == EXPLICIT_FAMILY
    assert {code: row["actuation"] for code, row in rows.items()} == ACTUATION
    assert {
        code: row["label"] for code, row in rows.items() if code != "00"
    } == PRODUCT_CODE_LABELS
    for family in {row["limits_family"] for row in rows.values()}:
        expected = {
            code for code, row in rows.items() if row["limits_family"] == family
        }
        assert codes_for_limits(family) == expected, family
    assert {code for code, row in rows.items() if row["lhz_duty"]} == LHZ_HEATER_CODES
    assert {
        code for code, row in rows.items() if row["fixed_time_boost"]
    } == HAVERLAND_FIXED_TIME_CODES
    assert {
        code for code, row in rows.items() if row["thm_heater"]
    } == HEATER_SURFACE_THM_CODES
    assert set(_UI_MODE_OPTIONS) <= set(rows)
    assert codes_for_limits("water") == {
        code for code, info in PRODUCT_CODES.items() if info.water
    }


def test_code_rows_use_known_vocabulary() -> None:
    """Families, actuations, surfaces and limits families stay in the table's set."""
    assert {info.family for info in PRODUCT_CODES.values()} <= {
        "htr",
        "thermostat",
        "storage",
        "power",
    }
    assert {info.actuation for info in PRODUCT_CODES.values()} <= {
        "TRIAC",
        "RELAY",
        "THERMOSTAT",
        "POWER",
    }
    assert {info.surface for info in PRODUCT_CODES.values()} <= {
        "heater",
        "thermostat",
        "power",
    }
    limits_families = {info.limits_family for info in PRODUCT_CODES.values()}
    assert limits_families <= {
        "base",
        "helki",
        "haverland",
        "htr_mod",
        "triac",
        "pir",
        "thm28",
        "wifi",
        "atc",
        "relay",
        "climastar",
        "storage_helki",
        "storage_elnur",
        "water",
        "thermostat",
        "power",
    }


def test_control_mode_catalogue_matches_the_fixture() -> None:
    """The wire arrays, UI lists and labels are the reviewed vendor catalogue."""
    modes = FIXTURE_DATA["control_modes"]
    assert list(control_modes.HTR_WIRE) == modes["htr_wire"]
    assert list(control_modes.STORAGE_WIRE) == modes["storage_wire"]
    assert list(control_modes.HTR_TRIAC_UI) == modes["htr_triac_ui"]
    assert list(control_modes.HTR_RELAY_UI) == modes["htr_relay_ui"]
    assert list(control_modes.HYSTERESIS_ONLY_UI) == modes["hysteresis_only_ui"]
    assert modes["labels_c"] == control_modes.MODE_LABELS_C
    assert modes["labels_f"] == control_modes.MODE_LABELS_F
    # The factory's heating switch has no UNKNOWN/power-meter case; those
    # codes must fall through to the node-type default.
    assert {"00", "03", "22"}.isdisjoint(EXPLICIT_FAMILY)


def test_storage_ui_mode_options_match_the_fixture() -> None:
    """Only the reviewed Elnur products offer a storage UI-mode setting."""
    for code, row in FIXTURE_DATA["codes"].items():
        expected = tuple((label, index) for label, index in row["storage_ui_modes"])
        assert ui_mode_options(f"08{code}0000") == expected, code


def test_fixture_columns_match_the_table() -> None:
    """The shared fixture carries the table's family/actuation/label columns."""
    rows = FIXTURE_DATA["codes"]
    table_fields = set(ProductCode.__dataclass_fields__) - {"code"}
    for code, info in PRODUCT_CODES.items():
        row = rows[code]
        assert info.code == code
        assert table_fields <= set(row), code
        assert row["family"] == info.family
        assert row["actuation"] == info.actuation
        assert row["limits_family"] == info.limits_family
        assert row["surface"] == info.surface
        assert row["label"] == info.label
        assert row["supports_extra_options"] is info.supports_extra_options
        assert row["lhz_duty"] is info.lhz_duty
        assert row["thm_heater"] is info.thm_heater
        assert row["fixed_time_boost"] is info.fixed_time_boost
        assert row["water"] is info.water
        assert row["stepped_charge"] is info.stepped_charge
        assert row["extra_energy"] is info.extra_energy


def test_product_code_reads_only_strings() -> None:
    assert product_code(None) is None
    assert product_code(12) is None  # type: ignore[arg-type]
    assert product_code(" 08320000 ") == "32"


def test_extra_options_flag_matches_the_write_layer() -> None:
    """The table and the protocol's setup-shape copy must agree per code."""
    for pid in [f"08{code}0000" for code in PRODUCT_CODES] + [
        None,
        "08990000",
        "12",
    ]:
        assert accepts_extra_options(pid) is WriteApi.supports_extra_options(pid), pid
