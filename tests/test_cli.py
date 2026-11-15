"""Smoke tests for the optional CLI surface."""

from __future__ import annotations

from asyncclick.testing import CliRunner

from smartbox.cli import _api_base_url, smartbox
from smartbox.protocol import AvailableResellers
from smartbox.protocol.reseller import SMARTBOX_GENERIC_BASIC_AUTH


async def test_cli_help_lists_commands():
    result = await CliRunner().invoke(smartbox, ["--help"])
    assert result.exit_code == 0
    assert "listen" in result.output
    assert "set-boost-mode" in result.output
    assert "--api-host" in result.output


async def test_cli_resellers_does_not_need_credentials():
    result = await CliRunner().invoke(smartbox, ["resellers"])
    assert result.exit_code == 0
    assert "API host" in result.output
    assert "users-api.helki.com" in result.output
    assert "api-elnur.helki.com" in result.output
    assert "api-ehc.helki.com" in result.output
    assert "brand key" in result.output
    assert "generic" in result.output
    assert "custom" in result.output
    assert "Electric Heating Company" in result.output
    assert "Haverland" in result.output
    assert SMARTBOX_GENERIC_BASIC_AUTH not in result.output


def test_cli_api_host_override_normalizes():
    """--api-host accepts a bare host or a full HTTPS URL."""
    helki = AvailableResellers("api").reseller
    assert _api_base_url(helki, None) == "https://users-api.helki.com"
    assert _api_base_url(helki, "api.test") == "https://api.test"
    assert _api_base_url(helki, "https://api.test/") == "https://api.test"

    elnur = AvailableResellers("api-elnur").reseller
    assert _api_base_url(elnur, None) == "https://api-elnur.helki.com"
