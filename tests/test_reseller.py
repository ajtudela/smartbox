import pytest

from smartbox import AsyncSmartboxSession, ResellerNotExistError
from smartbox.reseller import AvailableResellers


def test_available_resellers_existing_reseller():
    reseller = AvailableResellers(api_url="api").reseller
    assert reseller.name == "Helki"
    assert reseller.api_url == "api"


def test_available_resellers_non_existing_reseller():
    with pytest.raises(ResellerNotExistError):
        AvailableResellers(api_url="non-existing-api").reseller


def test_available_resellers_custom_reseller():
    serial_id = 99
    _reseller = AvailableResellers(
        api_url="custom-api",
        basic_auth="custom-auth",
        web_url="https://custom-url.com",
        serial_id=serial_id,
        name="Custom",
    )
    reseller = _reseller.reseller
    assert reseller.name == "Custom"
    assert reseller.api_url == "custom-api"
    assert reseller.basic_auth == "custom-auth"
    assert reseller.web_url == "https://custom-url.com"
    assert reseller.serial_id == serial_id

    assert _reseller.name == "Custom"
    assert _reseller.api_url == "custom-api"
    assert _reseller.web_url == "https://custom-url.com"


def test_reseller_invalid_data():
    with pytest.raises(ResellerNotExistError):
        AvailableResellers(
            api_url="invalid-api",
            basic_auth="invalid-auth",
            web_url="invalid-url",
            serial_id="invalid-serial-id",
        ).reseller


@pytest.mark.asyncio
async def test_all_resellers():
    for key in AvailableResellers.resellers:
        _session = AsyncSmartboxSession(username="", password="", api_name=key)
        check = await _session.health_check()
        assert check is not None
        version = await _session.api_version()
        assert "major" in version
