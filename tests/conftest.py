from unittest.mock import patch

from asyncclick.testing import CliRunner
import pytest

from smartbox.reseller import SmartboxReseller
from smartbox.session import AsyncSession, AsyncSmartboxSession, Session
from smartbox.update_manager import UpdateManager
from tests.common import fake_get_request


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_session(mocker):
    return mocker.patch("smartbox.cmd.AsyncSmartboxSession")


@pytest.fixture
def update_manager(mock_session):
    return UpdateManager(mock_session, "device_id")


@pytest.fixture
def async_smartbox_session(mocker, reseller):
    async_smartbox_session = AsyncSmartboxSession(
        api_name="test_api",
        username="test_user",
        password="test_password",
    )
    with patch(
        "smartbox.session.AsyncSmartboxSession._api_request",
        autospec=True,
        side_effect=fake_get_request,
    ):
        yield async_smartbox_session


@pytest.fixture
def session(reseller):
    return Session(
        api_name="test_api",
        username="test_user",
        password="test_password",
    )


@pytest.fixture
def async_session(reseller, mocker):
    api_name = "test_api"
    username = "test_user"
    password = "test_password"

    session = AsyncSession(
        api_name=api_name,
        username=username,
        password=password,
    )

    class MockAiohttpResponse:
        def __init__(self, *args, **kwargs):
            self.url = args[0] if args else kwargs.get("url", "")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            """Context manager exit, no special handling needed."""

        async def json(self):
            path = "unknown"
            if isinstance(self.url, str):
                if "api/v2/" in self.url:
                    path = self.url.split("api/v2/")[-1]
                elif "client/token" in self.url:
                    return {
                        "access_token": "fake_token",
                        "refresh_token": "fake_refresh",
                        "expires_in": 3600,
                    }

            try:
                return await fake_get_request(None, path)
            except FileNotFoundError:
                return {}

    mock_client = mocker.MagicMock()
    mock_client.get.side_effect = MockAiohttpResponse
    mock_client.post.side_effect = MockAiohttpResponse

    with patch(
        "smartbox.session.AsyncSession.client",
        new_callable=mocker.PropertyMock,
        return_value=mock_client,
    ):
        yield session


@pytest.fixture
def reseller(mocker):
    return mocker.patch(
        "smartbox.reseller.AvailableResellers.resellers",
        new_callable=mocker.PropertyMock,
        return_value={
            "test_api": SmartboxReseller(
                name="test",
                api_url="test_api",
                basic_auth="test_credentials",
                serial_id=10,
                web_url="http",
            ),
        },
    )
