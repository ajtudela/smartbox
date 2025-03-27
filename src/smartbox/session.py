"""Interaction with smartbox API."""

import asyncio
import datetime
import json
import logging
import time
from typing import Any

import aiohttp
from aiohttp import ClientSession
from pydantic import ValidationError

from smartbox.error import APIUnavailableError, InvalidAuthError, SmartboxError
from smartbox.models import (
    AcmNodeStatus,
    DefaultNodeStatus,
    DeviceAwayStatus,
    DeviceConnected,
    Devices,
    Guests,
    Home,
    Homes,
    HtrModNodeStatus,
    HtrNodeStatus,
    Node,
    Nodes,
    NodeSetup,
    NodeStatus,
    Samples,
    SmartboxNodeType,
    Token,
)
from smartbox.reseller import AvailableResellers, SmartboxReseller

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_MIN_TOKEN_LIFETIME = (
    60  # Minimum time left before expiry before we refresh (seconds)
)

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
        self._username: str = username
        self._password: str = password
        self._access_token: str = ""
        self._client_session: ClientSession | None = websession
        self.raw_response: bool = raw_response
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if self.reseller.serial_id:
            self._headers.update({"x-serialid": str(self.reseller.serial_id)})
        if self.reseller.web_url:
            self._headers.update({"x-referer": self.reseller.web_url})

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
            return ClientSession()
        return self._client_session

    async def health_check(self) -> dict[str, Any]:
        """Check if the API is alived."""
        api_url = f"{self._api_host}/health_check"
        try:
            response = await self.client.get(api_url)
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        return await response.json()

    async def api_version(self) -> dict[str, str]:
        """Check if the API is alived."""
        api_url = f"{self._api_host}/version"
        try:
            response = await self.client.get(api_url)
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        return await response.json()

    async def _authentication(self, credentials: dict[str, str]) -> None:
        """Do the authentication process to Smartbox. First one use login/mdp/basic_auth. Then the tokens."""
        token_headers = self._headers.copy()
        del token_headers["Authorization"]
        token_headers.update(
            {
                "authorization": f"Basic {self.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        token_url = f"{self._api_host}/client/token"
        try:
            response = await self.client.post(
                url=token_url,
                headers=token_headers,
                data=credentials,
            )
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        except aiohttp.ClientResponseError as e:
            raise InvalidAuthError(e) from e
        try:
            rtoken: Token = Token.model_validate(await response.json())
            self._access_token = rtoken.access_token
            self._headers["Authorization"] = f"Bearer {self._access_token}"
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
            _LOGGER.debug(
                "Authenticated session (%s), access_token=%s, expires at %s",
                credentials["grant_type"],
                self.access_token,
                self.expiry_time,
            )
        except ValidationError as e:
            msg = "Received invalid auth response"
            raise InvalidAuthError(msg) from e

    async def check_refresh_auth(self) -> None:
        """Do we have to refresh auth."""
        if self._access_token == "":
            await self._authentication(
                {
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password,
                },
            )
        elif (
            self._expires_at - datetime.datetime.now(datetime.UTC)
        ) < datetime.timedelta(seconds=_MIN_TOKEN_LIFETIME):
            await self._authentication(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            )

    async def _api_request(self, path: str) -> dict[str, Any]:
        """Make a GET request."""
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        try:
            _LOGGER.debug("Getting %s.", api_url)
            response = await self.client.get(api_url, headers=self._headers)
            _LOGGER.debug("Response %s.", (await response.json()))
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        except aiohttp.ClientResponseError as e:
            _LOGGER.exception(
                "ClientResponseError: %s, status: %s",
                e.message,
                e.status,
            )
            raise SmartboxError(e) from e
        return await response.json()

    async def _api_post(
        self,
        data: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        """Make a POST request."""
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        try:
            data_str = json.dumps(data)
            _LOGGER.debug("Posting %s to %s.", data_str, api_url)
            response = await self.client.post(
                api_url,
                data=data_str,
                headers=self._headers,
            )
            _LOGGER.debug("Response %s.", (await response.json()))
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
        ) as e:
            raise APIUnavailableError(e) from e
        except aiohttp.ClientResponseError as e:
            _LOGGER.exception(
                "Smartbox Error: %s, status: %s",
                e.message,
                e.status,
            )
            raise SmartboxError(e) from e
        return await response.json()


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
        """Get device power limit."""
        power_param = "power_limit"
        url = f"devs/{device_id}/htr_system/{power_param}"

        if node is not None and (
            (_node := Node.model_validate(node))
            and _node.type == SmartboxNodeType.PMO
        ):
            power_param = "power"
            url = f"devs/{device_id}/{_node.type}/{_node.addr}/{power_param}"

        resp = await self._api_request(url)
        return int(resp[power_param])

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
        start_time: int | None = int(time.time() - 3600),
        end_time: int | None = int(time.time() + 3600),
    ) -> dict[str, Any] | Samples:
        """Get samples (history) from node."""
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
    ) -> (
        dict[str, Any]
        | AcmNodeStatus
        | HtrNodeStatus
        | HtrModNodeStatus
        | DefaultNodeStatus
        | None
    ):
        """Get a node status."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/status",
        )
        _LOGGER.debug("(%s) Status config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        try:
            return NodeStatus.model_validate(response).root
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
    ) -> dict[str, Any] | NodeSetup:
        """Get a node setup."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )
        _LOGGER.debug("(%s) Setup config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        try:
            return NodeSetup.model_validate(response)
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
        # setup seems to require all settings to be re-posted, so get current
        # values and update
        setup_data: dict[str, Any] = {}
        node_setup = await self.get_node_setup(device_id, node)
        if not isinstance(node_setup, dict):
            setup_data = node_setup.model_dump(mode="json")
        else:
            setup_data = node_setup
        setup_data.update(data)
        await self._api_post(
            data=setup_data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )


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
