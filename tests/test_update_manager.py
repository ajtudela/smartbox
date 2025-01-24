import pytest
from unittest.mock import MagicMock
from smartbox.session import AsyncSmartboxSession
from smartbox.socket import SocketSession

from smartbox.update_manager import (
    OptimisedJQMatcher,
    DevDataSubscription,
    UpdateSubscription,
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


def test_optimised_jq_matcher_repr_simple():
    matcher = OptimisedJQMatcher(".simple")
    assert repr(matcher) == "OptimisedJQMatcher('.simple', fast_path=True)"


def test_optimised_jq_matcher_repr_complex():
    matcher = OptimisedJQMatcher(".complex | .nested")
    assert repr(matcher) == repr(matcher._compiled_jq)


def test_optimised_jq_matcher_str_simple():
    matcher = OptimisedJQMatcher(".simple")
    assert str(matcher) == "OptimisedJQMatcher('.simple', fast_path=True)"


def test_optimised_jq_matcher_str_complex():
    matcher = OptimisedJQMatcher(".complex | .nested")
    assert str(matcher) == str(matcher._compiled_jq)


def test_dev_data_subscription_match():
    callback = MagicMock()
    subscription = DevDataSubscription(".data", callback)
    input_data = {"data": "value"}
    subscription.match(input_data)
    callback.assert_called_once_with("value")


def test_dev_data_subscription_match_no_match():
    callback = MagicMock()
    subscription = DevDataSubscription(".data", callback)
    input_data = {"other_data": "value"}
    subscription.match(input_data)
    callback.assert_not_called()


def test_update_manager_subscribe_to_device_away_status(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_device_away_status(callback)
    assert len(update_manager._dev_data_subscriptions) == 1
    assert len(update_manager._update_subscriptions) == 1

    # Test dev data callback
    dev_data = {"away_status": "away"}
    update_manager._dev_data_cb(dev_data)
    callback.assert_called_once_with("away")

    # Test update callback
    callback.reset_mock()
    update_data = {"path": "/mgr/away_status", "body": "away"}
    update_manager._update_cb(update_data)
    callback.assert_called_once_with("away")


def test_update_manager_subscribe_to_node_status(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_node_status(callback)
    assert len(update_manager._dev_data_subscriptions) == 1
    assert len(update_manager._update_subscriptions) == 1

    # Test dev data callback
    dev_data = {"nodes": [{"type": "node_type", "addr": 1, "status": {"key": "value"}}]}
    update_manager._dev_data_cb(dev_data)
    callback.assert_called_once_with("node_type", 1, {"key": "value"})

    # Test update callback
    callback.reset_mock()
    update_data = {"path": "/node_type/1/status", "body": {"key": "value"}}
    update_manager._update_cb(update_data)
    callback.assert_called_once_with("node_type", 1, {"key": "value"})


def test_update_manager_subscribe_to_node_setup(update_manager):
    callback = MagicMock()
    update_manager.subscribe_to_node_setup(callback)
    assert len(update_manager._dev_data_subscriptions) == 1
    assert len(update_manager._update_subscriptions) == 1

    # Test dev data callback
    dev_data = {"nodes": [{"type": "node_type", "addr": 1, "setup": {"key": "value"}}]}
    update_manager._dev_data_cb(dev_data)
    callback.assert_called_once_with("node_type", 1, {"key": "value"})

    # Test update callback
    callback.reset_mock()
    update_data = {"path": "/node_type/1/setup", "body": {"key": "value"}}
    update_manager._update_cb(update_data)
    callback.assert_called_once_with("node_type", 1, {"key": "value"})
