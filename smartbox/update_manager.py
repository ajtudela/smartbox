"""Smartbox socket update manager."""

import jq
import logging
import re
from typing import Any, Callable, Dict, Iterable, List

from smartbox.session import AsyncSmartboxSession
from smartbox.socket import SocketSession

_LOGGER = logging.getLogger(__name__)

_SIMPLE_JQ_RE = re.compile(r"^\.(\w+)$")


class OptimisedJQMatcher(object):
    """jq matcher that doesn't bother with jq for simple one-level element queries."""

    def __init__(self, jq_expr: str):
        """Create an OptimisedJQMatcher for any jq expression."""
        m = _SIMPLE_JQ_RE.match(jq_expr)
        self._fast_path = False
        if m:
            self._fast_path = True
            self._simple_elem = m.group(1)
        else:
            self._compiled_jq = jq.compile(jq_expr)

    def match(self, input_data: Dict[str, Any]) -> Iterable:
        """Return matches for the given dev data."""
        if self._fast_path:
            return [input_data.get(self._simple_elem)]
        else:
            return self._compiled_jq.input(input_data)

    def __repr__(self) -> str:
        if self._fast_path:
            return str(self)
        else:
            return repr(self._compiled_jq)

    def __str__(self) -> str:
        if self._fast_path:
            return f"OptimisedJQMatcher('.{self._simple_elem}', fast_path=True)"
        else:
            return str(self._compiled_jq)


class DevDataSubscription(object):
    """Subscription for dev data callbacks."""

    def __init__(self, jq_expr: str, callback: Callable[[Dict[str, Any]], None]):
        """Create a dev data subscription for the given jq expression."""
        self._jq_matcher = OptimisedJQMatcher(jq_expr)
        self._callback = callback

    def match(self, input_data: Dict[str, Any]) -> None:
        """Return matches for this subscription for the given dev data."""
        _LOGGER.debug("Matching jq %s", self._jq_matcher)
        try:
            for match in self._jq_matcher.match(input_data):
                if match is not None:
                    self._callback(match)
        except ValueError:
            _LOGGER.exception("Error evaluating jq on dev data %s", input_data)


class UpdateSubscription(object):
    """Subscription for updates."""

    def __init__(
        self, path_regex: str, jq_expr: str, callback: Callable[[Dict[str, Any]], None]
    ):
        """Create an update subscription for the given path regex and body jq
        expression."""
        self._path_regex = re.compile(path_regex)
        self._jq_matcher = OptimisedJQMatcher(jq_expr)
        self._callback = callback

    def match(self, input_data: Dict[str, Any]) -> bool:
        """Return matches for this subscription for the given update."""
        path_match = self._path_regex.search(input_data["path"])
        if not path_match:
            return False
        path_match_kwargs = path_match.groupdict()
        matched = False
        _LOGGER.debug("Matching jq %s", self._jq_matcher)
        try:
            for data_match in self._jq_matcher.match(input_data):
                if data_match is not None:
                    matched = True
                    self._callback(data_match, **path_match_kwargs)
        except ValueError:
            _LOGGER.exception("Error evaluating jq on update %s", input_data)
        return matched


class UpdateManager(object):
    """Manages subscription callbacks to receive updates from a Smartbox socket."""

    BODY_PATH = ".body"

    def __init__(self, session: AsyncSmartboxSession, device_id: str, **kwargs):
        """Create an UpdateManager for a smartbox socket."""
        self._socket_session = SocketSession(
            session, device_id, self._dev_data_cb, self._update_cb, **kwargs
        )
        self._dev_data_subscriptions: List[DevDataSubscription] = []
        self._update_subscriptions: List[UpdateSubscription] = []

    @property
    def socket_session(self) -> SocketSession:
        """Get the underlying socket session."""
        return self._socket_session

    async def run(self) -> None:
        """Run the socket session asynchronously, waiting for updates."""
        await self._socket_session.run()

    def subscribe_to_dev_data(self, jq_expr: str, callback: Callable) -> None:
        """Subscribe to receive device data."""
        sub = DevDataSubscription(jq_expr, callback)
        self._dev_data_subscriptions.append(sub)

    def subscribe_to_updates(
        self, path_regex: str, jq_expr: str, callback: Callable[..., None]
    ) -> None:
        """Subscribe to receive device and node data updates.

        Named groups in path_regex are passed as kwargs to callback.
        """
        sub = UpdateSubscription(path_regex, jq_expr, callback)
        self._update_subscriptions.append(sub)

    def subscribe_to_device_away_status(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        """Subscribe to device away status updates."""
        self.subscribe_to_dev_data(".away_status", callback)
        self.subscribe_to_updates(r"^/mgr/away_status", self.BODY_PATH, callback)

    def subscribe_to_device_power_limit(self, callback: Callable[[int], None]) -> None:
        """Subscribe to device power limit updates."""
        self.subscribe_to_dev_data(
            ".htr_system.setup.power_limit", lambda p: callback(int(p))
        )
        self.subscribe_to_updates(
            r"^/htr_system/(setup|power_limit)",
            f"{self.BODY_PATH}.power_limit",
            lambda p: callback(int(p)),
        )

    def subscribe_to_node_status(
        self, callback: Callable[[str, int, Dict[str, Any]], None]
    ) -> None:
        """Subscribe to node status updates."""

        def dev_data_wrapper(data: Dict[str, Any]) -> None:
            callback(data["type"], int(data["addr"]), data["status"]),

        self.subscribe_to_dev_data(
            "(.nodes[] | {addr, type, status})?", dev_data_wrapper
        )

        def update_wrapper(data: Dict[str, Any], node_type: str, addr: str) -> None:
            callback(node_type, int(addr), data),

        self.subscribe_to_updates(
            r"^/(?P<node_type>[^/]+)/(?P<addr>\d+)/status",
            self.BODY_PATH,
            update_wrapper,
        )

    def subscribe_to_node_setup(
        self, callback: Callable[[str, int, Dict[str, Any]], None]
    ) -> None:
        """Subscribe to node setup updates."""

        def dev_data_wrapper(data: Dict[str, Any]) -> None:
            callback(data["type"], int(data["addr"]), data["setup"]),

        self.subscribe_to_dev_data(
            "(.nodes[] | {addr, type, setup})?", dev_data_wrapper
        )

        def update_wrapper(data: Dict[str, Any], node_type: str, addr: str) -> None:
            callback(node_type, int(addr), data),

        self.subscribe_to_updates(
            r"^/(?P<node_type>[^/]+)/(?P<addr>\d+)/setup",
            self.BODY_PATH,
            update_wrapper,
        )

    def _dev_data_cb(self, data: Dict[str, Any]) -> None:
        for sub in self._dev_data_subscriptions:
            sub.match(data)

    def _update_cb(self, data: Dict[str, Any]) -> None:
        matched = False
        for sub in self._update_subscriptions:
            if "path" not in data:
                _LOGGER.error("Path not found in update data: %s", data)
                continue
            if sub.match(data):
                matched = True
        if not matched:
            _LOGGER.debug("No matches for update %s", data)
