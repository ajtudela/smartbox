import pytest
from smartbox.session import AsyncSmartboxSession, Session
from smartbox.update_manager import UpdateManager
from unittest.mock import AsyncMock, patch, MagicMock
from tests.common import fake_get_request


@pytest.fixture
def update_manager(mock_session):
    return UpdateManager(mock_session, "device_id")


@pytest.fixture
def async_smartbox_session():
    async_smartbox_session = AsyncSmartboxSession(
        api_name="test_api",
        basic_auth_credentials="test_credentials",
        username="test_user",
        password="test_password",
    )
    with patch(
        "smartbox.session.AsyncSmartboxSession._api_request",
        autospec=True,
        side_effect=fake_get_request,
    ):
        with patch(
            "smartbox.update_manager.SocketSession",
            autospec=True,
            side_effect=fake_get_request,
        ):
            yield async_smartbox_session


@pytest.fixture
def session():
    return Session(
        api_name="test_api",
        basic_auth_credentials="test_credentials",
        username="test_user",
        password="test_password",
    )
