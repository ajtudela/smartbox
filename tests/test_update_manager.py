import pytest
from unittest.mock import MagicMock, patch
from smartbox.session import AsyncSmartboxSession
from smartbox.socket import SocketSession

from smartbox.update_manager import (
    OptimisedJQMatcher,
    DevDataSubscription,
    UpdateSubscription,
    UpdateManager,
)


@pytest.fixture
def mock_session():
    return MagicMock(spec=AsyncSmartboxSession)


def test_optimised_jq_matcher_simple():
    matcher = OptimisedJQMatcher(".simple")
    input_data = {"simple": "value"}
    assert list(matcher.match(input_data)) == ["value"]


def test_optimised_jq_matcher_complex():
    matcher = OptimisedJQMatcher(".complex | .nested")
    input_data = {"complex": {"nested": "value"}}
    assert list(matcher.match(input_data)) == ["value"]


def test_dev_data_subscription():
    callback = MagicMock()
    subscription = DevDataSubscription(".data", callback)
    input_data = {"data": "value"}
    subscription.match(input_data)
    callback.assert_called_once_with("value")


def test_update_subscription():
    callback = MagicMock()
    subscription = UpdateSubscription(r"^/path", ".data", callback)
    input_data = {"path": "/path", "data": "value"}
    assert subscription.match(input_data)
    callback.assert_called_once_with("value")


def test_update_manager_subscribe_to_dev_data(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_dev_data(".data", callback)
    assert len(update_manager._dev_data_subscriptions) == 1


def test_update_manager_subscribe_to_updates(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_updates(r"^/path", ".data", callback)
    assert len(update_manager._update_subscriptions) == 1


def test_update_manager_dev_data_cb(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_dev_data(".data", callback)
    input_data = {"data": "value"}
    update_manager._dev_data_cb(input_data)
    callback.assert_called_once_with("value")


def test_update_manager_update_cb(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_updates(r"^/path", ".data", callback)
    input_data = {"path": "/path", "data": "value"}
    update_manager._update_cb(input_data)
    callback.assert_called_once_with("value")


def test_update_manager_socket_session(update_manager):
    assert isinstance(update_manager.socket_session, SocketSession)
