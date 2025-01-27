from unittest.mock import patch

import pytest
from asyncclick.testing import CliRunner

from smartbox.session import AsyncSession, AsyncSmartboxSession, Session
from smartbox.update_manager import UpdateManager
from tests.common import fake_get_request


@pytest.fixture
def runner():
    return CliRunner(mix_stderr=False)


@pytest.fixture
def mock_session(mocker):
    return mocker.patch("smartbox.cmd.AsyncSmartboxSession")


@pytest.fixture
def update_manager(mock_session):
    return UpdateManager(mock_session, "device_id")


@pytest.fixture
def async_smartbox_session(mocker):
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


@pytest.fixture
def async_session():
    api_name = "test_api"
    basic_auth_credentials = "test_credentials"
    username = "test_user"
    password = "test_password"

    session = AsyncSession(
        api_name=api_name,
        basic_auth_credentials=basic_auth_credentials,
        username=username,
        password=password,
    )
    with patch(
        "smartbox.session.AsyncSession.client",
        autospec=True,
        side_effect=fake_get_request,
    ):
        yield session
