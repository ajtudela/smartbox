"""Neutral storage semantics."""

from smartbox.device.storage import (
    StorageIntent,
    available_modes,
    boost_supported,
    classic_accumulator,
    current_mode,
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
