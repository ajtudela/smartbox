import asyncio
import logging
import signal
import socketio
from typing import Any, Callable, Dict, Optional
import urllib

from .session import Session

_API_V2_NAMESPACE = "/api/v2/socket_io"
# We most commonly get disconnected when the session
# expires, so we don't want to try many times
_DEFAULT_RECONNECT_ATTEMPTS = 3
_DEFAULT_BACKOFF_FACTOR = 0.1

_LOGGER = logging.getLogger(__name__)


class SmartboxAPIV2Namespace(socketio.AsyncClientNamespace):
    def __init__(
        self,
        session: Session,
        namespace: str,
        dev_data_callback: Optional[Callable] = None,
        node_update_callback: Optional[Callable] = None,
    ) -> None:
        super().__init__(namespace)
        self._session = session
        self._namespace = namespace
        self._dev_data_callback = dev_data_callback
        self._node_update_callback = node_update_callback
        self._namespace_connected = False
        self._received_message = False
        self._received_dev_data = False

    def on_connect(self) -> None:
        _LOGGER.debug(f"Namespace {self._namespace} connected")
        self._namespace_connected = True

    async def on_disconnect(self) -> None:
        _LOGGER.info(f"Namespace {self._namespace} disconnected, disconnecting socket")
        self._namespace_connected = False
        self._received_message = False
        self._received_dev_data = False
        # we need to call disconnect to disconnect all namespaces
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return self._namespace_connected

    async def on_dev_data(self, data: Dict[str, Any]) -> None:
        _LOGGER.debug(f"Received dev_data: {data}")
        self._received_message = True
        self._received_dev_data = True
        if self._dev_data_callback is not None:
            self._dev_data_callback(data)

    async def on_update(self, data: Dict[str, Any]) -> None:
        _LOGGER.debug(f"Received update: {data}")
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


class SocketSession(object):
    def __init__(
        self,
        session: Session,
        device_id: str,
        dev_data_callback: Optional[Callable] = None,
        node_update_callback: Optional[Callable] = None,
        verbose: bool = False,
        add_sigint_handler: bool = False,
        ping_interval: int = 20,
        reconnect_attempts: int = _DEFAULT_RECONNECT_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    ) -> None:
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
            session, _API_V2_NAMESPACE, dev_data_callback, node_update_callback
        )
        self._sio.register_namespace(self._api_v2_ns)

        @self._sio.event
        async def connect():
            _LOGGER.debug("Received connect socket event")
            if add_sigint_handler:
                # engineio sets a signal handler on connect, which means we
                # have to set our own in the connect callback if we want to
                # override it
                _LOGGER.debug("Adding signal handler")
                event_loop = asyncio.get_event_loop()

                def sigint_handler():
                    _LOGGER.debug("Caught SIGINT, cancelling loop")
                    asyncio.ensure_future(self.cancel())

                event_loop.add_signal_handler(signal.SIGINT, sigint_handler)

    async def _send_ping(self):
        _LOGGER.debug(f"Starting ping task every {self._ping_interval}s")
        while True:
            await asyncio.sleep(self._ping_interval)
            if not self._api_v2_ns.connected:
                _LOGGER.debug("Namespace disconnected, not sending ping")
                continue
            _LOGGER.debug("Sending ping")
            await self._sio.send("ping", namespace=_API_V2_NAMESPACE)

    async def run(self) -> None:
        self._ping_task = self._sio.start_background_task(self._send_ping)

        # Will loop indefinitely unless our signal handler is set and called
        self._loop_should_exit = False

        _LOGGER.debug("Starting main loop")
        while not self._loop_should_exit:
            # TODO: accessors in session
            encoded_token = urllib.parse.quote(
                self._session._access_token, safe="~()*!.'"
            )
            url = (
                f"{self._session._api_host}"
                + f"/?token={encoded_token}&dev_id={self._device_id}"
            )

            # Try to connect
            _LOGGER.debug(
                f"Connecting to {url} (will try {self._reconnect_attempts} times)"
            )
            for attempt in range(self._reconnect_attempts):
                _LOGGER.debug(f"Connecting to {url} (attempt #{attempt})")
                try:
                    await self._sio.connect(
                        url,
                        namespaces=[
                            f"{_API_V2_NAMESPACE}?token={encoded_token}&dev_id={self._device_id}"
                        ],
                    )
                except socketio.exceptions.ConnectionError:
                    remaining = self._reconnect_attempts - attempt - 1
                    sleep_time = self._backoff_factor * (2**attempt)
                    _LOGGER.error(
                        "Received error on connection attempt"
                        f", {remaining} retries remaining"
                        f", sleeping {sleep_time}s"
                    )
                    if remaining > 0:
                        await asyncio.sleep(sleep_time)
                    else:
                        logging.warning(
                            f"Failed to connect after {self._reconnect_attempts}"
                            " attempts, falling through to refresh token"
                        )
                else:
                    _LOGGER.info(f"Successfully connected to {url}")
                    await self._sio.wait()
                    _LOGGER.info("Socket loop exited, disconnecting")
                    await self._sio.disconnect()
                    _LOGGER.debug("Breaking loop to refresh token")
                    break

            # Refresh token
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._session._check_refresh)

    async def cancel(self) -> None:
        _LOGGER.debug("Disconnecting and cancelling tasks")
        self._loop_should_exit = True
        await self._sio.disconnect()
        self._ping_task.cancel()

    @property
    def namespace(self):
        return self._api_v2_ns
