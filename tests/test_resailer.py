import pytest
from pydantic import ValidationError
from smartbox.resailer import AvailableResailers, SmartboxResailer
from smartbox.error import ResailerNotExist


def test_available_resailers_existing_resailer():
    resailer = AvailableResailers(api_url="api").resailer
    assert resailer.name == "Smartbox"
    assert resailer.api_url == "api"
    assert resailer.basic_auth == "NTRiY2NiZmI0MWE5YTUxMTNmMDQ4OGQwOnZkaXZkaQ=="
    assert resailer.serial_id is None


def test_available_resailers_non_existing_resailer():
    with pytest.raises(ResailerNotExist):
        AvailableResailers(api_url="non-existing-api")


def test_available_resailers_custom_resailer():
    resailer = AvailableResailers(
        api_url="custom-api",
        basic_auth="custom-auth",
        web_url="https://custom-url.com",
        serial_id=99,
        name="Custom",
    ).resailer
    assert resailer.name == "Custom"
    assert resailer.api_url == "custom-api"
    assert resailer.basic_auth == "custom-auth"
    assert resailer.web_url == "https://custom-url.com"
    assert resailer.serial_id == 99
