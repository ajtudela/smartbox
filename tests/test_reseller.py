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
