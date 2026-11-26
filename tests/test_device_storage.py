"""Neutral storage semantics."""

from smartbox.device.codes import PRODUCT_CODES
from smartbox.device.profile import ELNUR_STORAGE_CODES
from smartbox.device.storage import (
    StorageIntent,
    available_modes,
    boost_supported,
    classic_accumulator,
    current_mode,
    is_elnur_stepped_storage,
    lock_endpoint,
    ui_mode_available,
    ui_mode_options,
    write_for,
)

CLASSIC = {"factory_options": {"ui_mode": 2}}
ECOMBI = {"factory_options": {"ui_mode": 0}}
HELKI_MANUAL = {"operational_mode": 0}
HELKI_AUTO = {"operational_mode": 1}


def test_classic_accumulator_uses_target_charge() -> None:
    assert classic_accumulator("0814", CLASSIC)
    assert available_modes("acm", "0814", CLASSIC) == [
        StorageIntent.HEAT,
        StorageIntent.OFF,
    ]
    assert (
        current_mode("acm", "0814", CLASSIC, {"target_charge_per": 0})
        is StorageIntent.OFF
    )
    heat = write_for("acm", "0814", CLASSIC, StorageIntent.HEAT)
    assert heat is not None
    assert (heat.kind, heat.target_charge) == ("charge", 100)
    assert write_for("acm", "0814", CLASSIC, StorageIntent.AUTO) is None


def test_ecombi_and_helki_modes() -> None:
    assert available_modes("acm", "0814", ECOMBI) == [
        StorageIntent.AUTO,
        StorageIntent.HEAT,
        StorageIntent.OFF,
    ]
    assert available_modes("acm", "081C", HELKI_MANUAL) == [
        StorageIntent.HEAT,
        StorageIntent.OFF,
    ]
    assert available_modes("acm", "081C", HELKI_AUTO) == [
        StorageIntent.AUTO,
        StorageIntent.HEAT,
        StorageIntent.OFF,
    ]
    assert (
        current_mode("acm", "081C", HELKI_AUTO, {"mode": "off", "boost": True})
        is StorageIntent.HEAT
    )


def test_water_storage_filters_and_ui_modes() -> None:
    pure = {"extra_nrg_conf": {"enabled": True, "mode": 0}}
    combined = {"extra_nrg_conf": {"enabled": True, "mode": 1}}
    assert available_modes("acm", "082A", pure) == [
        StorageIntent.HEAT,
        StorageIntent.OFF,
    ]
    assert available_modes("acm", "082A", combined) == [
        StorageIntent.AUTO,
        StorageIntent.OFF,
    ]
    assert write_for("acm", "082A", combined, StorageIntent.HEAT) is None
    assert ui_mode_options("08140000") == (
        ("Classic combined", 1),
        ("Classic accumulator", 2),
    )
    assert ui_mode_options("082A0000") == ()
    assert ui_mode_available("08140000", CLASSIC) is True
    assert ui_mode_available("08140000", ECOMBI) is False
    assert boost_supported("acm", "081C") is True
    assert boost_supported("acm", "0814") is False


def test_stepped_charge_is_code_14_only() -> None:
    assert is_elnur_stepped_storage("08140000")
    for pid in (
        "08200000",
        "08250000",
        "082A0000",
        "081C0000",
        "082E0000",
        "08170000",
        "08990000",
        None,
    ):
        assert not is_elnur_stepped_storage(pid), pid


def test_every_elnur_storage_product_offers_ui_modes() -> None:
    water = {code for code, info in PRODUCT_CODES.items() if info.water}
    for code in ELNUR_STORAGE_CODES - water:
        assert ui_mode_options(f"08{code}0000"), code


def test_e2_extra_energy_hides_auto_outside_confort() -> None:
    """The E2 controller filters AUTO when extra energy is on (mode 2 keeps it)."""
    setup = {
        "factory_options": {"ui_mode": 0},
        "extra_nrg_conf": {"enabled": True, "mode": 1},
    }
    assert StorageIntent.AUTO not in available_modes("acm", "08200000", setup)
    assert StorageIntent.AUTO not in available_modes("acm", "08250000", setup)
    assert write_for("acm", "08200000", setup, StorageIntent.AUTO) is None

    setup["extra_nrg_conf"] = {"enabled": True, "mode": 2}
    assert StorageIntent.AUTO in available_modes("acm", "08200000", setup)
    assert write_for("acm", "08200000", setup, StorageIntent.AUTO) is not None

    setup["extra_nrg_conf"] = {"enabled": False, "mode": 1}
    assert StorageIntent.AUTO in available_modes("acm", "08200000", setup)

    # Code 14 does not inherit the E2 override.
    assert StorageIntent.AUTO in available_modes("acm", "08140000", setup)


def test_storage_current_mode_and_write_branches() -> None:
    assert current_mode("htr", "04020000", {}, {}) is None
    assert (
        current_mode("acm", "08140000", CLASSIC, {"mode": "off"}) is StorageIntent.OFF
    )
    assert (
        current_mode("acm", "08140000", CLASSIC, {"target_charge_per": 0})
        is StorageIntent.OFF
    )
    assert (
        current_mode("acm", "08140000", CLASSIC, {"target_charge_per": 50})
        is StorageIntent.HEAT
    )
    assert current_mode("acm", "081C0000", HELKI_MANUAL, {}) is StorageIntent.OFF

    assert write_for("htr", "04020000", {}, StorageIntent.HEAT) is None
    heat = write_for("acm", "08140000", CLASSIC, StorageIntent.HEAT)
    assert heat is not None
    assert heat.target_charge == 100
    off = write_for("acm", "08140000", CLASSIC, StorageIntent.OFF)
    assert off is not None
    assert off.target_charge == 0
    assert write_for("acm", "08140000", CLASSIC, StorageIntent.AUTO) is None

    water = {"extra_nrg_conf": {"enabled": True, "mode": 0}}
    assert write_for("acm", "082A0000", water, StorageIntent.HEAT) is not None
    water["extra_nrg_conf"] = {"enabled": True, "mode": 1}
    assert write_for("acm", "082A0000", water, StorageIntent.HEAT) is None

    assert boost_supported("htr", "04020000") is False
    assert boost_supported("acm", "081C0000") is True
    assert boost_supported("acm", "08140000") is False
    assert lock_endpoint("acm") is True
    assert lock_endpoint("htr") is False


def test_non_storage_nodes_keep_the_plain_heater_modes() -> None:
    assert available_modes("htr", "04020000", {}) == [
        StorageIntent.HEAT,
        StorageIntent.AUTO,
        StorageIntent.OFF,
    ]
