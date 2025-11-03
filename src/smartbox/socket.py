"""Socket integration for smartbox."""

import asyncio
from collections.abc import Callable
import logging
import signal
from typing import Any
import urllib

import socketio

from smartbox.session import AsyncSmartboxSession

_API_V2_NAMESPACE = "/api/v2/socket_io"
# We most commonly get disconnected when the session
# expires, so we don't want to try many times
_DEFAULT_RECONNECT_ATTEMPTS = 3
_DEFAULT_BACKOFF_FACTOR = 0.1

_LOGGER = logging.getLogger(__name__)


class SmartboxAPIV2Namespace(socketio.AsyncClientNamespace):
    """Smartbox Namespace for socket.io."""

    def __init__(
        self,
        session: AsyncSmartboxSession,
        namespace: str,
        dev_data_callback: Callable | None = None,
        node_update_callback: Callable | None = None,
    ) -> None:
        """Init of a async namespace."""
        super().__init__(namespace)
        self._session = session
        self._namespace = namespace
        self._dev_data_callback = dev_data_callback
        self._node_update_callback = node_update_callback
        self._namespace_connected = False
        self._received_message = False
        self._received_dev_data = False

    async def on_connect(self) -> None:
        """Namespace connected."""
        _LOGGER.debug("Namespace %s connected", self._namespace)
        self._namespace_connected = True

    async def on_disconnect(self, reason: str) -> None:
        """Disconnection of namespace."""
        _LOGGER.debug(
            "Namespace %s disconnected, disconnecting socket. Reason: %s",
            self._namespace,
            reason,
        )
        self._namespace_connected = False
        self._received_message = False
        self._received_dev_data = False

    @property
    def connected(self) -> bool:
        """Are we connected."""
        return self._namespace_connected

    async def on_dev_data(self, data: dict[str, Any]) -> None:
        """Received dev data."""
        _LOGGER.debug("Received dev_data: %s", data)
        self._received_message = True
        self._received_dev_data = True
        if self._dev_data_callback is not None:
            self._dev_data_callback(data)

    async def on_update(self, data: dict[str, Any]) -> None:
        """Received update."""
        _LOGGER.debug("Received update: %s", data)
        if not self._received_message:
            # The connection is only usable once we've received a message from
            # the server (not on the connect event!!!), so we wait to receive
            # something before sending our first message
            await self.emit("dev_data", namespace=self._namespace)
            self._received_message = True
        if not self._received_dev_data:
            _LOGGER.debug("Dev data not received yet, ignoring update")
            return
        if self._node_update_callback is not None:
            self._node_update_callback(data)


class SocketSession:
    """Smartbox SocketSession class."""

    def __init__(
        self,
        session: AsyncSmartboxSession,
        device_id: str,
        dev_data_callback: Callable | None = None,
        node_update_callback: Callable | None = None,
        verbose: bool = False,
        add_sigint_handler: bool = False,
        ping_interval: int = 20,
        reconnect_attempts: int = _DEFAULT_RECONNECT_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        """Init socket session to smartbox."""
        self._session = session
        self._device_id = device_id
        self._ping_interval = ping_interval
        self._reconnect_attempts = reconnect_attempts
        self._backoff_factor = backoff_factor

        if verbose:
            self._sio = socketio.AsyncClient(
                logger=True,
                engineio_logger=True,
                reconnection_attempts=reconnect_attempts,
            )
        else:
            logging.getLogger("socketio").setLevel(logging.ERROR)
            logging.getLogger("engineio").setLevel(logging.ERROR)
            self._sio = socketio.AsyncClient()

        self._api_v2_ns = SmartboxAPIV2Namespace(
            session,
            _API_V2_NAMESPACE,
            dev_data_callback,
            node_update_callback,
        )
        self._sio.register_namespace(self._api_v2_ns)

        @self._sio.event
        async def connect() -> None:
            _LOGGER.debug("Received connect socket event")
            if add_sigint_handler:
                # engineio sets a signal handler on connect, which means we
                # have to set our own in the connect callback if we want to
                # override it
                _LOGGER.debug("Adding signal handler")
                event_loop = asyncio.get_event_loop()

                def sigint_handler() -> None:
                    _LOGGER.debug("Caught SIGINT, cancelling loop")
                    asyncio.ensure_future(self.cancel())

                event_loop.add_signal_handler(signal.SIGINT, sigint_handler)

    async def _dev_data(self) -> None:
        """Send first dev data."""
        if not self._api_v2_ns.connected:
            _LOGGER.debug("Namespace disconnected, not sending ping")
        _LOGGER.debug("Sending dev_data event")
        await self._sio.emit("dev_data", namespace=_API_V2_NAMESPACE)

    async def _send_ping(self) -> None:
        """End pings to be alive."""
        _LOGGER.debug("Starting ping task every %ss", self._ping_interval)
        while True:
            await self._sio.sleep(self._ping_interval)
            if not self._api_v2_ns.connected:
                _LOGGER.debug("Namespace disconnected, not sending ping")
                continue
            _LOGGER.debug("Sending ping")
            await self._sio.send("ping", namespace=_API_V2_NAMESPACE)

    async def run(self) -> None:
        """Run the websocket."""
        self._ping_task = self._sio.start_background_task(self._send_ping)

        # Will loop indefinitely unless our signal handler is set and called
        self._loop_should_exit = False

        _LOGGER.debug("Starting main loop")
        while not self._loop_should_exit:
            encoded_token = urllib.parse.quote(
                self._session.access_token,
                safe="~()*!.'",
            )
            url = f"{self._session.api_host}/?token={encoded_token}&dev_id={self._device_id}"

            # Try to connect
            _LOGGER.debug(
                "Connecting to %s (will try %s times)",
                url,
                self._reconnect_attempts,
            )
            for attempt in range(self._reconnect_attempts):
                _LOGGER.debug("Connecting to %s (attempt #%s)", url, attempt)
                try:
                    await self._sio.connect(url, transports=["websocket"])
                except socketio.exceptions.ConnectionError:
                    remaining = self._reconnect_attempts - attempt - 1
                    sleep_time = self._backoff_factor * (2**attempt)
                    _LOGGER.exception(
                        "Received error on connection attempt, %s retries remaining, sleeping %ss",
                        remaining,
                        sleep_time,
                    )
                    if remaining > 0:
                        await asyncio.sleep(sleep_time)
                    else:
                        _LOGGER.warning(
                            "Failed to connect after %s attempts, falling through to refresh token",
                            self._reconnect_attempts,
                        )
                else:
                    _LOGGER.info("Successfully connected to %s", url)
                    await self._dev_data()
                    await self._sio.wait()
                    _LOGGER.info("Socket loop exited, disconnecting")
                    await self._sio.disconnect()
                    _LOGGER.debug("Breaking loop to refresh token")
                    break

            # Refresh token
            await self._session.check_refresh_auth()

            # Update the query string with the new access token
            encoded_token = urllib.parse.quote(
                self._session.access_token,
                safe="~()*!.'",
            )
            url = f"{self._session.api_host}/?token={encoded_token}&dev_id={self._device_id}"

    async def cancel(self) -> None:
        """Disconnecting and cancelling tasks."""
        _LOGGER.debug("Disconnecting and cancelling tasks")
        self._loop_should_exit = True
        await self._sio.disconnect()
        self._ping_task.cancel()

    @property
    def namespace(self) -> SmartboxAPIV2Namespace:
        """Namespace property."""
        return self._api_v2_ns
