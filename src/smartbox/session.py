"""Interaction with smartbox API."""

import asyncio
import datetime
from http import HTTPStatus
import json
import logging
import time
from typing import Any, Self

import aiohttp
from aiohttp import ClientSession
from pydantic import ValidationError

from smartbox.error import APIUnavailableError, InvalidAuthError, SmartboxError
from smartbox.models import (
    AcmNodeStatus,
    DefaultNodeSetup,
    DefaultNodeStatus,
    DeviceAwayStatus,
    DeviceConnected,
    Devices,
    DeviceVersion,
    Guests,
    Home,
    Homes,
    HtrModNodeStatus,
    HtrNodeStatus,
    HtrSystemSetup,
    Node,
    NodeProg,
    Nodes,
    NodeVersion,
    PmoSetup,
    Samples,
    SmartboxNodeType,
    Token,
)
from smartbox.reseller import AvailableResellers, SmartboxReseller

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_DEFAULT_TIMEOUT = 30  # Total timeout per HTTP request (seconds)
_MIN_TOKEN_LIFETIME = (
    60  # Minimum time left before expiry before we refresh (seconds)
)
# Response statuses worth retrying: rate limiting and transient server faults.
_RETRYABLE_STATUS = frozenset(
    {
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)


def _retry_after_seconds(headers: object) -> float | None:
    """Return the ``Retry-After`` delay in seconds, if given as an integer."""
    get = getattr(headers, "get", None)
    if get is None:
        return None
    raw = get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        # HTTP-date form is not handled; fall back to exponential backoff.
        return None

# The status model is picked by node type, which is reliable, rather than by the
# shape of the response, which is not. Keyed by the enum's string value so an
# unknown ``Node.type`` (a plain str) still looks up cleanly.
_STATUS_MODELS: dict[str, type[DefaultNodeStatus]] = {
    SmartboxNodeType.ACM: AcmNodeStatus,
    SmartboxNodeType.HTR: HtrNodeStatus,
    SmartboxNodeType.HTR_MOD: HtrModNodeStatus,
}

_LOGGER = logging.getLogger(__name__)


class AsyncSession:
    """Base class for Session."""

    def __init__(
        self,
        username: str,
        password: str,
        websession: ClientSession | None = None,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        raw_response: bool = True,
        api_name: str = "api",
        basic_auth_credentials: str | None = None,
        x_serial_id: int | None = None,
        x_referer: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """Init the session."""
        self._reseller = AvailableResellers(
            api_url=api_name,
            basic_auth=basic_auth_credentials,
            serial_id=x_serial_id,
            web_url=x_referer,
        ).reseller
        self._api_host: str = f"https://{self.reseller.api_url}.helki.com"
        self._basic_auth_credentials: str | None = basic_auth_credentials
        self._retry_attempts: int = retry_attempts
        self._backoff_factor: float = backoff_factor
        self._timeout: float = timeout
        self._username: str = username
        self._password: str = password
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expires_at: datetime.datetime = datetime.datetime.now(
            datetime.UTC
        )
        # Serialise token refresh so a burst of concurrent requests does not
        # each fire its own ``_authentication`` on the shared token fields.
        self._auth_lock = asyncio.Lock()
        self._client_session: ClientSession | None = websession
        # Track ownership: a caller-injected session (e.g. Home Assistant's
        # shared one) must never be closed by us.
        self._owns_client_session: bool = websession is None
        self.raw_response: bool = raw_response
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if self.reseller.serial_id:
            self._headers.update({"x-serialid": str(self.reseller.serial_id)})
        if self.reseller.web_url:
            self._headers.update({"x-referer": self.reseller.web_url})

    async def __aenter__(self) -> Self:
        """Async context manager entry.

        Authentication and client-session creation are lazy and happen on the
        first API call, not here.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client session if this session owns it.

        A caller-injected ``ClientSession`` (e.g. Home Assistant's shared
        session) is left untouched: closing it would break every other consumer
        that shares it.
        """
        if self._owns_client_session and self._client_session:
            await self._client_session.close()
            self._client_session = None

    @property
    def reseller(self) -> SmartboxReseller:
        """Get the reseller."""
        return self._reseller

    @property
    def api_name(self) -> str:
        """Get the api sub domain url."""
        return self.reseller.api_url

    @property
    def api_host(self) -> str:
        """Get the base api url."""
        return self._api_host

    @property
    def access_token(self) -> str:
        """Get auth access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str:
        """Get auth refresh token."""
        return self._refresh_token

    @property
    def expiry_time(self) -> datetime.datetime:
        """Get auth expiracy."""
        return self._expires_at

    @property
    def client(self) -> ClientSession:
        """Return the underlying http client."""
        if not self._client_session:
            # Without an explicit timeout aiohttp waits its 5-minute default,
            # long enough to stack overlapping refreshes in a coordinator.
            self._client_session = ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            )
        return self._client_session

    async def health_check(self) -> dict[str, Any]:
        """Check if the API is alived."""
        api_url = f"{self._api_host}/health_check"
        try:
            async with self.client.get(api_url) as response:
                response.raise_for_status()
                return await response.json()
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e

    async def api_version(self) -> dict[str, str]:
        """Check if the API is alived."""
        api_url = f"{self._api_host}/version"
        try:
            async with self.client.get(api_url) as response:
                response.raise_for_status()
                return await response.json()
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e

    async def _authentication(self, credentials: dict[str, str]) -> None:
        """Do the authentication process to Smartbox. First one use login/mdp/basic_auth. Then the tokens."""
        token_headers = self._headers.copy()
        token_headers.pop("Authorization", None)
        token_headers.update(
            {
                "authorization": f"Basic {self.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        token_url = f"{self._api_host}/client/token"
        try:
            async with self.client.post(
                url=token_url,
                headers=token_headers,
                data=credentials,
            ) as response:
                response.raise_for_status()
                response_json = await response.json()
                try:
                    rtoken: Token = Token.model_validate(response_json)
                    self._access_token = rtoken.access_token
                    self._headers["Authorization"] = (
                        f"Bearer {self._access_token}"
                    )
                    self._refresh_token = rtoken.refresh_token
                    if rtoken.expires_in < _MIN_TOKEN_LIFETIME:
                        _LOGGER.warning(
                            "Token expires in %ss which is below minimum lifetime of %ss- will refresh again on next operation",
                            rtoken.expires_in,
                            _MIN_TOKEN_LIFETIME,
                        )
                    self._expires_at = datetime.datetime.now(
                        datetime.UTC
                    ) + datetime.timedelta(
                        seconds=rtoken.expires_in,
                    )
                    # Never log the token itself: debug logs are routinely
                    # attached to GitHub issues and a leaked access token is
                    # valid for hours. The last 4 chars are enough to correlate.
                    _LOGGER.debug(
                        "Authenticated session (%s), token ...%s, expires at %s",
                        credentials["grant_type"],
                        self._access_token[-4:],
                        self.expiry_time,
                    )
                except ValidationError as e:
                    msg = f"Received invalid auth response {response.status} with msg: {response.reason}"
                    raise InvalidAuthError(msg) from e
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        except aiohttp.ClientResponseError as e:
            raise InvalidAuthError(e) from e

    def _password_grant(self) -> dict[str, str]:
        return {
            "grant_type": "password",
            "username": self._username,
            "password": self._password,
        }

    def _refresh_grant(self) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }

    def _token_expiring(self) -> bool:
        remaining = self._expires_at - datetime.datetime.now(datetime.UTC)
        return remaining < datetime.timedelta(seconds=_MIN_TOKEN_LIFETIME)

    async def check_refresh_auth(self) -> None:
        """Authenticate or refresh the token if needed.

        Guarded by a lock and re-checked inside it, so N concurrent requests
        arriving with the token about to expire trigger a single refresh. When
        the refresh token itself is rejected, fall back to the password grant
        instead of failing every request until the process restarts.
        """
        async with self._auth_lock:
            if self._access_token and not self._token_expiring():
                return
            if not self._access_token:
                await self._authentication(self._password_grant())
                return
            try:
                await self._authentication(self._refresh_grant())
            except InvalidAuthError:
                _LOGGER.debug(
                    "Refresh token rejected, falling back to password grant",
                )
                await self._authentication(self._password_grant())

    async def _send(
        self,
        method: str,
        api_url: str,
        data: str | None,
    ) -> dict[str, Any]:
        """Send one request to the v2 API, retrying transient failures.

        Connection errors and 429/5xx responses are retried up to
        ``self._retry_attempts`` times with exponential backoff
        (``self._backoff_factor * 2 ** attempt``), honouring a numeric
        ``Retry-After`` header when present. A 401/403 becomes
        ``InvalidAuthError`` and any other 4xx a ``SmartboxError``, both
        immediately.
        """
        last_error: SmartboxError | None = None
        last_cause: BaseException | None = None
        for attempt in range(self._retry_attempts + 1):
            try:
                request = (
                    self.client.post(
                        api_url, headers=self._headers, data=data
                    )
                    if method == "POST"
                    else self.client.get(api_url, headers=self._headers)
                )
                async with request as response:
                    response.raise_for_status()
                    body = await response.json()
                    _LOGGER.debug("Response %s.", body)
                    return body
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientConnectorError,
            ) as e:
                last_error, last_cause = APIUnavailableError(e), e
                reason = type(e).__name__
                delay = self._backoff_factor * (2**attempt)
            except aiohttp.ClientResponseError as e:
                if e.status in (
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                ):
                    raise InvalidAuthError(e) from e
                if e.status not in _RETRYABLE_STATUS:
                    _LOGGER.exception(
                        "%s %s: %s, status: %s",
                        method,
                        api_url,
                        e.message,
                        e.status,
                    )
                    raise SmartboxError(e) from e
                last_error, last_cause = SmartboxError(e), e
                reason = f"HTTP {e.status}"
                delay = _retry_after_seconds(e.headers) or (
                    self._backoff_factor * (2**attempt)
                )

            if attempt >= self._retry_attempts:
                break
            _LOGGER.warning(
                "%s %s failed (%s); retrying in %.1fs (%s/%s)",
                method,
                api_url,
                reason,
                delay,
                attempt + 1,
                self._retry_attempts,
            )
            await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error from last_cause
        msg = "retry loop exited without a result"  # unreachable
        raise SmartboxError(msg)

    async def _api_request(self, path: str) -> dict[str, Any]:
        """Make a GET request to the v2 API (transient failures retried)."""
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        _LOGGER.debug("Getting %s.", api_url)
        return await self._send("GET", api_url, None)

    async def _api_post(
        self,
        data: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        """Make a POST request to the v2 API (transient failures retried)."""
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        data_str = json.dumps(data)
        _LOGGER.debug("Posting %s to %s.", data_str, api_url)
        return await self._send("POST", api_url, data_str)


class AsyncSmartboxSession(AsyncSession):
    """Asynchronous Smartbox Session. This should be the default one."""

    async def get_devices(self) -> list[dict[str, Any]] | Devices:
        """Get all devices."""
        response = await self._api_request("devs")
        _LOGGER.debug("Get devices %s", response)
        devices: Devices = Devices.model_validate(response)
        if self.raw_response is False:
            return devices
        return [
            device.model_dump(mode="json")
            for device in (devices.devs + devices.invited_to)
        ]

    async def get_homes(self) -> list[dict[str, Any]] | list[Home]:
        """Get homes."""
        response = await self._api_request("grouped_devs")
        homes: list[Home] = Homes.model_validate(response).root
        if self.raw_response is False:
            return homes
        return [home.model_dump(mode="json") for home in homes]

    async def get_home_guests(
        self, home_id: str
    ) -> list[dict[str, Any]] | Guests:
        """Get all devices."""
        response = await self._api_request(f"groups/{home_id}/guest_users")
        guests: Guests = Guests.model_validate(response)
        if self.raw_response is False:
            return guests
        return [guest.model_dump(mode="json") for guest in guests.guest_users]

    async def get_grouped_devices(self) -> list[dict[str, Any]] | Homes:
        """Get grouped devices."""
        response = await self._api_request("grouped_devs")
        homes: Homes = Homes.model_validate(response)
        if self.raw_response is False:
            return homes
        return [home.model_dump(mode="json") for home in homes.root]

    async def get_nodes(
        self,
        device_id: str,
    ) -> list[dict[str, Any]] | list[Node]:
        """Get nodes from devices."""
        response = await self._api_request(f"devs/{device_id}/mgr/nodes")
        _LOGGER.debug("Get nodes %s", response)
        if self.raw_response is True:
            return response["nodes"]
        return Nodes.model_validate(response).nodes

    async def get_device_connected(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceConnected:
        """Get device away status."""
        response = await self._api_request(f"devs/{device_id}/connected")
        status: DeviceConnected = DeviceConnected.model_validate(response)
        if self.raw_response is False:
            return status
        return status.model_dump(mode="json")

    async def get_device_away_status(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceAwayStatus:
        """Get device away status."""
        response = await self._api_request(f"devs/{device_id}/mgr/away_status")
        status: DeviceAwayStatus = DeviceAwayStatus.model_validate(response)
        if self.raw_response is False:
            return status
        return status.model_dump(mode="json")

    async def set_device_away_status(
        self,
        device_id: str,
        status_args: dict[str, Any],
    ) -> None:
        """Set device away status."""
        data = {k: v for k, v in status_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/mgr/away_status",
        )

    async def get_device_power_limit(
        self, device_id: str, node: dict[str, Any] | None = None
    ) -> int:
        """Get the power limit (watts) of the device, or of a ``pmo`` node.

        Read and write both use the ``power_limit`` resource. The ``pmo`` path
        mirrors ``set_device_power_limit`` but has not been checked against a
        real ``pmo`` device (see api-notes.md).
        """
        url = f"devs/{device_id}/htr_system/power_limit"
        if (
            node is not None
            and (_node := Node.model_validate(node))
            and _node.type == SmartboxNodeType.PMO
        ):
            url = f"devs/{device_id}/{_node.type}/{_node.addr}/power_limit"

        resp = await self._api_request(url)
        # The value may come back as an int or as a string that is not always a
        # plain integer literal, so parse defensively.
        return int(float(resp["power_limit"]))

    async def set_device_power_limit(
        self,
        device_id: str,
        power_limit: int,
        node: dict[str, Any] | None = None,
    ) -> None:
        """Set device power limit."""
        _node_type = "htr_system"
        if node is not None and (
            (_node := Node.model_validate(node))
            and _node.type == SmartboxNodeType.PMO
        ):
            _node_type = f"{_node.type}/{_node.addr}"
        data = {"power_limit": str(power_limit)}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node_type}/power_limit",
        )

    async def get_node_samples(
        self,
        device_id: str,
        node: dict[str, Any],
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any] | Samples:
        """Get samples (history) from node.

        ``start_time``/``end_time`` default to one hour before/after the moment
        the call is made. They must default to ``None`` here: a call-time
        expression would be evaluated once at import and freeze the window to
        process start-up.
        """
        if start_time is None:
            start_time = int(time.time() - 3600)
        if end_time is None:
            end_time = int(time.time() + 3600)
        _LOGGER.debug(
            "Get_Device_Samples_Node: from %s to %s",
            datetime.datetime.fromtimestamp(start_time, tz=datetime.UTC),
            datetime.datetime.fromtimestamp(end_time, tz=datetime.UTC),
        )
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/samples?start={start_time}&end={end_time}",
        )
        _LOGGER.debug("Get_Device_Samples_Node: %s", response)
        if self.raw_response is True:
            return response
        return Samples.model_validate(response)

    async def get_node_status(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | DefaultNodeStatus:
        """Get a node status.

        In typed mode the model is chosen by ``node["type"]`` (``HtrNodeStatus``,
        ``HtrModNodeStatus``, ``AcmNodeStatus``) and falls back to
        ``DefaultNodeStatus`` for any other type.
        """
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/status",
        )
        _LOGGER.debug("(%s) Status config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        model = _STATUS_MODELS.get(_node.type, DefaultNodeStatus)
        try:
            return model.model_validate(response)
        except ValidationError:
            _LOGGER.exception("Status config validation error %s", response)
            raise

    async def set_node_status(
        self,
        device_id: str,
        node: dict[str, Any],
        status_args: dict[str, Any],
    ) -> None:
        """Set a node status."""
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in status_args.items() if v is not None}
        if "stemp" in data and "units" not in data:
            msg = "Must supply unit with temperature fields"
            raise ValueError(msg)
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/status",
        )

    async def get_node_setup(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | DefaultNodeSetup | PmoSetup:
        """Get a node setup.

        In typed mode ``pmo`` nodes return ``PmoSetup`` and every other type
        returns ``DefaultNodeSetup``.
        """
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )
        _LOGGER.debug("(%s) Setup config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        model: type[DefaultNodeSetup | PmoSetup] = (
            PmoSetup
            if _node.type == SmartboxNodeType.PMO
            else DefaultNodeSetup
        )
        try:
            return model.model_validate(response)
        except ValidationError:
            _LOGGER.exception("Setup config validation error %s", response)
            raise

    async def set_node_setup(
        self,
        device_id: str,
        node: dict[str, Any],
        setup_args: dict[str, Any],
    ) -> None:
        """Set a node setup."""
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in setup_args.items() if v is not None}
        # The setup endpoint requires the whole configuration to be re-posted,
        # even for unchanged fields. The read-modify-write below must therefore
        # keep the payload intact: going through the Pydantic model would drop
        # every key the model does not declare and wipe it on the device, so we
        # always read the raw setup here regardless of ``raw_response``.
        setup_data = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )
        setup_data.update(data)
        await self._api_post(
            data=setup_data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )

    async def get_node_version(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeVersion:
        """Get a node setup."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/version",
        )
        _LOGGER.debug("(%s) Version config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        try:
            return NodeVersion.model_validate(response)
        except ValidationError:
            _LOGGER.exception("Version config validation error %s", response)
            raise

    async def get_node_prog(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeProg:
        """Get a node's weekly heating schedule."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/prog",
        )
        _LOGGER.debug("(%s) Prog data %s", _node.type, response)
        if self.raw_response is True:
            return response
        return NodeProg.model_validate(response)

    async def get_device_version(
        self,
        device_id: str,
    ) -> dict[str, Any] | DeviceVersion:
        """Get the manager/system firmware version of a device."""
        response = await self._api_request(f"devs/{device_id}/mgr/version")
        if self.raw_response is True:
            return response
        return DeviceVersion.model_validate(response)

    async def get_htr_system_setup(
        self,
        device_id: str,
    ) -> dict[str, Any] | HtrSystemSetup:
        """Get the heater-system configuration of a device."""
        response = await self._api_request(
            f"devs/{device_id}/htr_system/setup"
        )
        if self.raw_response is True:
            return response
        return HtrSystemSetup.model_validate(response)


class Session:
    """For retro compatibility, this class is a sync which called the async."""

    def __init__(self, *args: int, **kwargs: dict[str, object]) -> None:
        """Sync init a session."""
        self._async = AsyncSmartboxSession(*args, **kwargs)  # type: ignore[arg-type]

    def get_devices(self) -> list[dict[str, Any]]:
        """Sync get all devices."""
        return asyncio.run(self._async.get_devices())  # type: ignore[arg-type]

    def get_homes(self) -> list[dict[str, Any]]:
        """Sync get homes."""
        return asyncio.run(self._async.get_homes())  # type: ignore[arg-type]

    def get_grouped_devices(self) -> list[dict[str, Any]]:
        """Sync get grouped devices."""
        return asyncio.run(self._async.get_grouped_devices())  # type: ignore[arg-type]

    def get_nodes(self, device_id: str) -> list[dict[str, Any]]:
        """Sync get nodes of device."""
        return asyncio.run(self._async.get_nodes(device_id=device_id))  # type: ignore[arg-type]

    def get_status(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync get the status of a node."""
        return asyncio.run(
            self._async.get_node_status(device_id=device_id, node=node),  # type: ignore[arg-type]
        )

    def set_status(
        self,
        device_id: str,
        node: dict[str, Any],
        status_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync set the node status."""
        return asyncio.run(
            self._async.set_node_status(  # type: ignore[arg-type]
                device_id=device_id,
                node=node,
                status_args=status_args,
            ),
        )

    def get_setup(self, device_id: str, node: dict[str, Any]) -> dict[str, Any]:
        """Sync get the node setup."""
        return asyncio.run(
            self._async.get_node_setup(device_id=device_id, node=node),  # type: ignore[arg-type]
        )

    def set_setup(
        self,
        device_id: str,
        node: dict[str, Any],
        setup_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync set the node setup."""
        return asyncio.run(
            self._async.set_node_setup(  # type: ignore[arg-type]
                device_id=device_id,
                node=node,
                setup_args=setup_args,
            ),
        )

    def get_device_away_status(self, device_id: str) -> dict[str, bool]:
        """Sync get the device away status."""
        return asyncio.run(
            self._async.get_device_away_status(device_id=device_id),  # type: ignore[arg-type]
        )

    def set_device_away_status(
        self,
        device_id: str,
        status_args: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync set the device away status."""
        return asyncio.run(
            self._async.set_device_away_status(  # type: ignore[arg-type]
                device_id=device_id,
                status_args=status_args,
            ),
        )

    def get_device_power_limit(self, device_id: str) -> int:
        """Get the device power limit."""
        return asyncio.run(
            self._async.get_device_power_limit(device_id=device_id),  # type: ignore[arg-type]
        )

    def set_device_power_limit(self, device_id: str, power_limit: int) -> None:
        """Sync set of a device power limit."""
        return asyncio.run(
            self._async.set_device_power_limit(  # type: ignore[arg-type]
                device_id=device_id,
                power_limit=power_limit,
            ),
        )
