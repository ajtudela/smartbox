"""The reviewed product-code table.

One row per ``EProductCodeValue`` entry. The profile family sets, labels and
per-code limits derive from this table; the control-mode UI/wire arrays and
storage UI-mode options stay in their own modules. Adding a code means adding
one row here, adding the reviewed fixture row (``tests/fixtures/
product_codes.json``) and updating the catalogue modules when the new product
needs one. There is no fixture generator; see ``docs/device-semantics.md``
("Changing a controller fact").

Decoded from the vendor 2.54.2 bundle; see `hass-smartbox docs/api-contract.md`
(the wire record) for the provenance citations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

_PID_CODE_START = 2
_PID_CODE_END = 4

Family = Literal["htr", "thermostat", "storage", "power"]
Actuation = Literal["TRIAC", "RELAY", "THERMOSTAT", "POWER"]
Surface = Literal["heater", "thermostat", "power"]
LimitsFamily = Literal[
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
]


def product_code(pid: str | None) -> str | None:
    """Return ``pid[2:4]`` uppercased, matching the app's dispatch."""
    if not isinstance(pid, str):
        return None
    value = pid.strip().upper()
    return value[_PID_CODE_START:_PID_CODE_END] if len(value) >= _PID_CODE_END else None


@dataclass(frozen=True)
class ProductCode:
    """One decoded product code and the controller facts it selects."""

    code: str
    family: Family
    actuation: Actuation
    limits_family: LimitsFamily  # key into profile._LIMITS_BY_FAMILY
    surface: Surface
    label: str
    supports_extra_options: bool
    lhz_duty: bool = False
    thm_heater: bool = False
    fixed_time_boost: bool = False
    water: bool = False
    stepped_charge: bool = False
    extra_energy: bool = False


PRODUCT_CODES: dict[str, ProductCode] = {
    # Row order: code, family, actuation, limits_family, surface, label,
    # supports_extra_options, then the optional lhz_duty/thm_heater/
    # fixed_time_boost/water/stepped_charge flags.
    "00": ProductCode(
        "00", "htr", "TRIAC", "base", "heater", "Heater", supports_extra_options=True
    ),
    "01": ProductCode(
        "01",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "02": ProductCode(
        "02",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "03": ProductCode(
        "03",
        "power",
        "POWER",
        "power",
        "power",
        "Power monitor",
        supports_extra_options=True,
    ),
    "04": ProductCode(
        "04",
        "thermostat",
        "THERMOSTAT",
        "thermostat",
        "thermostat",
        "Thermostat",
        supports_extra_options=True,
    ),
    "07": ProductCode(
        "07",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "08": ProductCode(
        "08",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "09": ProductCode(
        "09",
        "thermostat",
        "THERMOSTAT",
        "thermostat",
        "thermostat",
        "Thermostat",
        supports_extra_options=True,
    ),
    "0A": ProductCode(
        "0A",
        "htr",
        "TRIAC",
        "htr_mod",
        "heater",
        "Heater module",
        supports_extra_options=True,
    ),
    "0B": ProductCode(
        "0B",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=True,
    ),
    "0C": ProductCode(
        "0C",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=True,
    ),
    "0D": ProductCode(
        "0D",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "0F": ProductCode(
        "0F",
        "thermostat",
        "THERMOSTAT",
        "thermostat",
        "thermostat",
        "Thermostatic valve",
        supports_extra_options=True,
    ),
    "10": ProductCode(
        "10",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "11": ProductCode(
        "11",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "12": ProductCode(
        "12", "htr", "RELAY", "base", "heater", "Heater", supports_extra_options=True
    ),
    "13": ProductCode(
        "13",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "14": ProductCode(
        "14",
        "htr",
        "TRIAC",
        "storage_elnur",
        "heater",
        "Storage heater",
        supports_extra_options=True,
        stepped_charge=True,
    ),
    "16": ProductCode(
        "16",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "17": ProductCode(
        "17",
        "htr",
        "RELAY",
        "haverland",
        "heater",
        "Relay heater",
        supports_extra_options=True,
        fixed_time_boost=True,
    ),
    "18": ProductCode(
        "18",
        "htr",
        "RELAY",
        "base",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "1A": ProductCode(
        "1A",
        "htr",
        "TRIAC",
        "triac",
        "heater",
        "Triac heater",
        supports_extra_options=True,
    ),
    "1B": ProductCode(
        "1B",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "1C": ProductCode(
        "1C",
        "storage",
        "RELAY",
        "storage_helki",
        "heater",
        "Storage heater",
        supports_extra_options=True,
    ),
    "1D": ProductCode(
        "1D",
        "htr",
        "TRIAC",
        "haverland",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
        fixed_time_boost=True,
    ),
    "1E": ProductCode(
        "1E",
        "htr",
        "RELAY",
        "relay",
        "heater",
        "Relay heater",
        supports_extra_options=False,
    ),
    "1F": ProductCode(
        "1F",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "Triac heater",
        supports_extra_options=False,
    ),
    "20": ProductCode(
        "20",
        "htr",
        "TRIAC",
        "storage_elnur",
        "heater",
        "Storage heater",
        supports_extra_options=True,
        extra_energy=True,
    ),
    "21": ProductCode(
        "21",
        "htr",
        "TRIAC",
        "triac",
        "heater",
        "Triac heater",
        supports_extra_options=True,
    ),
    "22": ProductCode(
        "22",
        "power",
        "POWER",
        "power",
        "power",
        "Power monitor",
        supports_extra_options=True,
    ),
    "23": ProductCode(
        "23",
        "htr",
        "RELAY",
        "triac",
        "heater",
        "Relay heater",
        supports_extra_options=True,
    ),
    "24": ProductCode(
        "24", "htr", "TRIAC", "base", "heater", "Heater", supports_extra_options=True
    ),
    "25": ProductCode(
        "25",
        "htr",
        "TRIAC",
        "storage_elnur",
        "heater",
        "Storage heater",
        supports_extra_options=True,
        extra_energy=True,
    ),
    "27": ProductCode(
        "27", "htr", "TRIAC", "pir", "heater", "Heater", supports_extra_options=True
    ),
    "28": ProductCode(
        "28",
        "htr",
        "RELAY",
        "thm28",
        "heater",
        "Thermostat",
        supports_extra_options=True,
        thm_heater=True,
    ),
    "2A": ProductCode(
        "2A",
        "htr",
        "TRIAC",
        "water",
        "heater",
        "Water storage heater",
        supports_extra_options=True,
        water=True,
    ),
    "2E": ProductCode(
        "2E",
        "storage",
        "RELAY",
        "storage_helki",
        "heater",
        "Storage heater",
        supports_extra_options=True,
    ),
    "31": ProductCode(
        "31",
        "htr",
        "RELAY",
        "wifi",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "32": ProductCode(
        "32",
        "htr",
        "TRIAC",
        "helki",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "33": ProductCode(
        "33",
        "htr",
        "RELAY",
        "helki",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
        lhz_duty=True,
    ),
    "34": ProductCode(
        "34",
        "htr",
        "TRIAC",
        "wifi",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "35": ProductCode(
        "35",
        "htr",
        "RELAY",
        "helki",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
        lhz_duty=True,
    ),
    "36": ProductCode(
        "36",
        "htr",
        "RELAY",
        "helki",
        "heater",
        "Relay heater",
        supports_extra_options=True,
        lhz_duty=True,
    ),
    "38": ProductCode(
        "38",
        "htr",
        "TRIAC",
        "helki",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
        lhz_duty=True,
    ),
    "39": ProductCode(
        "39",
        "htr",
        "TRIAC",
        "wifi",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "3D": ProductCode(
        "3D",
        "htr",
        "TRIAC",
        "climastar",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "43": ProductCode(
        "43",
        "htr",
        "TRIAC",
        "base",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "47": ProductCode(
        "47",
        "htr",
        "TRIAC",
        "haverland",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
        fixed_time_boost=True,
    ),
    "4C": ProductCode(
        "4C",
        "htr",
        "TRIAC",
        "atc",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "4D": ProductCode(
        "4D",
        "htr",
        "TRIAC",
        "atc",
        "heater",
        "WiFi heater",
        supports_extra_options=True,
    ),
    "FF": ProductCode(
        "FF", "htr", "TRIAC", "htr_mod", "heater", "Heater", supports_extra_options=True
    ),
}


def code_info(pid: str | None) -> ProductCode | None:
    """Return the reviewed row for one wire PID."""
    return PRODUCT_CODES.get(product_code(pid) or "")


def accepts_extra_options(pid: str | None) -> bool:
    """Whether the product accepts an ``extra_options`` object in setup POSTs.

    The reviewed table records the app's ``shouldOmitSetupExtraOptions`` rule.
    The protocol write layer keeps its own copy for the wire shape; the library
    test asserts the two agree for every reviewed code.
    """
    info = code_info(pid)
    return True if info is None else info.supports_extra_options


def has_heating_switch_case(info: ProductCode) -> bool:
    """Whether the app's heating factory switch names this product row.

    UNKNOWN (``00``) and the power meters (``03``/``22``) have no case; an
    acm/thm node carrying one of them uses the node-type default.
    """
    return info.code != "00" and info.family != "power"


def codes_for_limits(limits_family: LimitsFamily) -> frozenset[str]:
    """Return the codes that use one limits family."""
    return frozenset(
        code
        for code, info in PRODUCT_CODES.items()
        if info.limits_family == limits_family
    )


# The UNKNOWN row keeps the node-type label in `product_label`, so it is not
# part of the shipped label map.
PRODUCT_CODE_LABELS: dict[str, str] = {
    code: info.label for code, info in PRODUCT_CODES.items() if code != "00"
}


__all__ = [
    "PRODUCT_CODES",
    "PRODUCT_CODE_LABELS",
    "Actuation",
    "Family",
    "LimitsFamily",
    "ProductCode",
    "Surface",
    "accepts_extra_options",
    "code_info",
    "codes_for_limits",
    "has_heating_switch_case",
    "product_code",
]
