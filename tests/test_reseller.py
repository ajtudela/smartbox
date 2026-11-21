"""Tests for the reseller table and convenience accessors."""

from __future__ import annotations

import pytest

from smartbox.protocol.errors import ResellerNotExistError
from smartbox.protocol.reseller import API_HOST, AvailableResellers, api_base_url


def test_api_base_url_uses_the_current_host():
    assert API_HOST == "users-api.helki.com"
    assert api_base_url() == f"https://{API_HOST}"
    assert api_base_url(AvailableResellers("api").reseller) == f"https://{API_HOST}"


def test_api_base_url_uses_the_reseller_host():
    elnur = AvailableResellers("api-elnur").reseller
    assert elnur.api_host == "api-elnur.helki.com"
    assert api_base_url(elnur) == "https://api-elnur.helki.com"

    ehc = AvailableResellers("api-ehc").reseller
    assert ehc.api_host == "api-ehc.helki.com"
    assert api_base_url(ehc) == "https://api-ehc.helki.com"


def test_known_reseller():
    reseller = AvailableResellers("api")
    assert reseller.reseller.name == "Helki"
    assert reseller.api_url == "api"
    assert reseller.name == "Helki"
    assert reseller.web_url == "https://app.helki.com/"


def test_dormant_tenant_ids_are_not_selectable():
    assert "api-sangha" not in AvailableResellers.resellers
    assert "api-ath" not in AvailableResellers.resellers


def test_ducaheat_alias_resolves_to_the_tevolve_tenant():
    """The Ducaheat portal is the Tevolve tenant with the Ducasa theme.

    ``https://ducaheat.termoweb.net/`` serves the Tevolve build (decoded
    2026-11-21): serial 15, the Tevolve client credential,
    ``users-api.helki.com`` and ``ws_user``. The alias exists so Ducasa users
    find the brand in the picker; it changes no wire value.
    """
    tevolve = AvailableResellers("api-tevolve").reseller
    ducaheat = AvailableResellers("api-ducaheat").reseller

    assert ducaheat.name == "Ducasa (Ducaheat)"
    assert ducaheat.web_url == "https://ducaheat.termoweb.net/"
    assert ducaheat.api_url == "api-tevolve"
    assert ducaheat.serial_id == tevolve.serial_id == 15
    assert ducaheat.basic_auth == tevolve.basic_auth
    assert ducaheat.api_host == tevolve.api_host == API_HOST


def test_unknown_reseller_needs_all_fields():
    with pytest.raises(ResellerNotExistError):
        _ = AvailableResellers("api-unknown").reseller


def test_unknown_reseller_can_be_created():
    reseller = AvailableResellers(
        "api-unknown",
        basic_auth="secret",
        web_url="https://example.test/",
        serial_id=99,
        name="Example",
    ).reseller
    assert reseller.api_url == "api-unknown"
    assert reseller.name == "Example"
    assert reseller.web_url == "https://example.test/"
    assert reseller.serial_id == 99
