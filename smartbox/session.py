import asyncio
import datetime
import json
import logging
import time
from typing import Any
from pydantic import ValidationError

import aiohttp
from aiohttp import ClientSession

from smartbox.error import APIUnavailable, InvalidAuth, SmartboxError
from smartbox.models import (
    Devices,
    Homes,
    Home,
    Node,
    Nodes,
    NodeSetup,
    NodeStatus,
    Samples,
    DeviceAwayStatus,
    Token,
)

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_MIN_TOKEN_LIFETIME = 60  # Minimum time left before expiry before we refresh (seconds)

_LOGGER = logging.getLogger(__name__)


class AsyncSession:
    def __init__(
        self,
        api_name: str,
        basic_auth_credentials: str,
        username: str,
        password: str,
        websession: ClientSession | None = None,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        raw_response: bool = True,
        x_serial_id: int | None = None,
        x_referer: str | None = None,
    ):
        self._api_name: str = api_name
        self._api_host: str = f"https://{self._api_name}.helki.com"
        self._basic_auth_credentials: str = basic_auth_credentials
        self._retry_attempts: int = retry_attempts
        self._backoff_factor: float = backoff_factor
        self._username: str = username
        self._password: str = password
        self._access_token: str | None = None
        self._client_session: ClientSession | None = websession
        self.raw_response: bool = raw_response
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if x_serial_id:
            self._headers["x-serialid"] = str(x_serial_id)
        if x_referer:
            self._headers["x-referer"] = x_referer

    @property
    def api_name(self) -> str:
        return self._api_name

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def expiry_time(self) -> datetime.datetime:
        return self._expires_at

    @property
    def client(self) -> ClientSession:
        """Return the underlying http client."""

        if not self._client_session:
            return ClientSession()
        return self._client_session

    async def health_check(self) -> dict[str, Any]:
        api_url = f"{self._api_host}/health_check"
        try:
            response = await self.client.get(api_url)
        except aiohttp.ClientConnectionError as e:
            raise APIUnavailable from e
        return await response.json()

    async def _authentication(self, credentials: dict[str, str]) -> None:
        token_data = "&".join(f"{k}={v}" for k, v in credentials.items())
        token_headers = self._headers.copy()
        del token_headers["Authorization"]
        token_headers.update(
            {
                "authorization": f"Basic {self._basic_auth_credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )

        token_url = f"{self._api_host}/client/token"
        try:
            response = await self.client.post(
                url=token_url, headers=token_headers, data=token_data
            )
        except aiohttp.ClientConnectionError as e:
            raise APIUnavailable from e
        except aiohttp.ClientResponseError as e:
            raise InvalidAuth from e
        r = await response.json()
        try:
            r: Token = Token.model_validate(r)
        except ValidationError as e:
            raise SmartboxError("Received invalid auth response") from e
        self._access_token = r.access_token
        self._headers["Authorization"] = f"Bearer {self._access_token}"
        self._refresh_token = r.refresh_token
        if r.expires_in < _MIN_TOKEN_LIFETIME:
            _LOGGER.warning(
                (
                    f"Token expires in {r.expires_in}s"
                    f", which is below minimum lifetime of {_MIN_TOKEN_LIFETIME}s"
                    " - will refresh again on next operation"
                )
            )
        self._expires_at = datetime.datetime.now() + datetime.timedelta(
            seconds=r.expires_in
        )
        _LOGGER.debug(
            (
                f"Authenticated session ({credentials['grant_type']}), "
                f"access_token={self.access_token}, expires at {self.expiry_time}"
            )
        )

    async def check_refresh_auth(self) -> None:
        if self._access_token is None:
            await self._authentication(
                {
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password,
                }
            )
        if (self._expires_at - datetime.datetime.now()) < datetime.timedelta(
            seconds=_MIN_TOKEN_LIFETIME
        ):
            await self._authentication(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                }
            )

    async def _api_request(self, path: str) -> Any:
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        try:
            response = await self.client.get(api_url, headers=self._headers)
        except aiohttp.ClientConnectionError as e:
            raise APIUnavailable from e
        except aiohttp.ClientResponseError as e:
            _LOGGER.error(f"ClientResponseError: {e.message}, status: {e.status}")
            raise SmartboxError from e
        return await response.json()

    async def _api_post(self, data: Any, path: str) -> Any:
        await self.check_refresh_auth()
        api_url = f"{self._api_host}/api/v2/{path}"
        try:
            data_str = json.dumps(data)
            _LOGGER.debug(f"Posting {data_str} to {api_url}")
            response = await self.client.post(
                api_url, data=data_str, headers=self._headers
            )
        except aiohttp.ClientConnectionError as e:
            raise APIUnavailable from e
        except aiohttp.ClientResponseError as e:
            raise SmartboxError from e
        return await response.json()


class AsyncSmartboxSession(AsyncSession):
    async def get_devices(self) -> list[dict[str, Any]] | Devices:
        response = await self._api_request("devs")
        devices: Devices = Devices.model_validate(response)
        if self.raw_response is False:
            return devices
        devices = [device.model_dump(mode="json") for device in devices.devs]
        return devices

    async def get_homes(self) -> list[dict[str, Any]] | list[Home]:
        response = await self._api_request("grouped_devs")
        homes: list[Home] = Homes.model_validate(response).root
        if self.raw_response is False:
            return homes
        return [home.model_dump(mode="json") for home in homes]

    async def get_grouped_devices(self) -> list[dict[str, Any]] | Homes:
        response = await self._api_request("grouped_devs")
        homes: Homes = Homes.model_validate(response)
        if self.raw_response is False:
            return homes
        homes = [home for home in homes.root]
        return [home.model_dump(mode="json") for home in homes]

    async def get_nodes(self, device_id: str) -> list[dict[str, Any]] | list[Node]:
        response = await self._api_request(f"devs/{device_id}/mgr/nodes")
        nodes: Nodes = Nodes.model_validate(response)
        if self.raw_response is False:
            return nodes.nodes
        return [node.model_dump(mode="json") for node in nodes.nodes]

    async def get_device_away_status(
        self, device_id: str
    ) -> dict[str, Any] | DeviceAwayStatus:
        response = await self._api_request(f"devs/{device_id}/mgr/away_status")
        status: DeviceAwayStatus = DeviceAwayStatus.model_validate(response)
        if self.raw_response is False:
            return status
        return status.model_dump(mode="json")

    async def set_device_away_status(
        self, device_id: str, status_args: dict[str, Any]
    ) -> None:
        data = {k: v for k, v in status_args.items() if v is not None}
        await self._api_post(data=data, path=f"devs/{device_id}/mgr/away_status")

    async def get_device_power_limit(self, device_id: str) -> int:
        resp = await self._api_request(f"devs/{device_id}/htr_system/power_limit")
        return int(resp["power_limit"])

    async def set_device_power_limit(self, device_id: str, power_limit: int) -> None:
        data = {"power_limit": str(power_limit)}
        await self._api_post(data=data, path=f"devs/{device_id}/htr_system/power_limit")

    async def get_node_samples(
        self,
        device_id: str,
        node: dict[str, Any],
        start_time: int | None = int(time.time() - 3600),
        end_time: int | None = int(time.time() + 3600),
    ) -> dict[str, Any] | Samples:
        if start_time is None:
            start_time = int(time.time() - 3600)
        if end_time is None:
            end_time = int(time.time() + 3600)
        _LOGGER.debug(
            f"Get_Device_Samples_Node: from {datetime.datetime.fromtimestamp(start_time)} to {datetime.datetime.fromtimestamp(end_time)}"
        )
        node: Node = Node.model_validate(node)
        samples = Samples.model_validate(
            await self._api_request(
                f"devs/{device_id}/{node.type}/{node.addr}/samples?start={start_time}&end={end_time}"
            )
        )
        if self.raw_response is False:
            return samples
        return samples.model_dump(mode="json")

    async def get_node_status(
        self, device_id: str, node: dict[str, Any]
    ) -> dict[str, str] | NodeStatus:
        node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{node.type}/{node.addr}/status"
        )
        response = NodeStatus.model_validate(response)
        if self.raw_response is False:
            return response
        return response.model_dump(mode="json")

    async def set_node_status(
        self,
        device_id: str,
        node: dict[str, Any],
        status_args: dict[str, Any],
    ) -> None:
        node: Node = Node.model_validate(node)
        data = {k: v for k, v in status_args.items() if v is not None}
        if "stemp" in data and "units" not in data:
            raise ValueError("Must supply unit with temperature fields")
        await self._api_post(
            data=data, path=f"devs/{device_id}/{node.type}/{node.addr}/status"
        )

    async def get_node_setup(
        self, device_id: str, node: dict[str, Any]
    ) -> dict[str, Any] | NodeSetup:
        node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{node.type}/{node.addr}/setup"
        )
        response = NodeSetup.model_validate(response)
        if self.raw_response is False:
            return response
        return response.model_dump(mode="json")

    async def set_node_setup(
        self, device_id: str, node: dict[str, Any], setup_args: dict[str, Any]
    ) -> None:
        node: Node = Node.model_validate(node)
        data = {k: v for k, v in setup_args.items() if v is not None}
        # setup seems to require all settings to be re-posted, so get current
        # values and update
        setup_data = await self.get_node_setup(device_id, node)
        setup_data.update(data)
        await self._api_post(
            data=setup_data,
            path=f"devs/{device_id}/{node.type}/{node.addr}/setup",
        )


class Session:
    def __init__(self, *args, **kwargs):
        self._async = AsyncSmartboxSession(*args, **kwargs)

    def get_devices(self) -> list[dict[str, Any]]:
        return asyncio.run(self._async.get_devices())

    def get_homes(self) -> list[dict[str, Any]]:
        return asyncio.run(self._async.get_homes())

    def get_grouped_devices(self) -> list[dict[str, Any]]:
        return asyncio.run(self._async.get_grouped_devices())

    def get_nodes(self, device_id: str) -> list[dict[str, Any]]:
        return asyncio.run(self._async.get_nodes(device_id=device_id))

    def get_status(self, device_id: str, node: dict[str, Any]) -> dict[str, str]:
        return asyncio.run(self._async.get_node_status(device_id=device_id, node=node))

    def set_status(
        self, device_id: str, node: dict[str, Any], status_args: dict[str, Any]
    ) -> dict[str, Any]:
        return asyncio.run(
            self._async.set_node_status(
                device_id=device_id, node=node, status_args=status_args
            )
        )

    def get_setup(self, device_id: str, node: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._async.get_node_setup(device_id=device_id, node=node))

    def set_setup(
        self, device_id: str, node: dict[str, Any], setup_args: dict[str, Any]
    ) -> dict[str, Any]:
        return asyncio.run(
            self._async.set_node_setup(
                device_id=device_id, node=node, setup_args=setup_args
            )
        )

    def get_device_away_status(self, device_id: str) -> dict[str, Any]:
        return asyncio.run(self._async.get_device_away_status(device_id=device_id))

    def set_device_away_status(
        self, device_id: str, status_args: dict[str, Any]
    ) -> dict[str, Any]:
        return asyncio.run(
            self._async.set_device_away_status(
                device_id=device_id,
                status_args=status_args,
            )
        )

    def get_device_power_limit(self, device_id: str) -> int:
        return asyncio.run(self._async.get_device_power_limit(device_id=device_id))

    def set_device_power_limit(self, device_id: str, power_limit: int) -> None:
        return asyncio.run(
            self._async.set_device_power_limit(
                device_id=device_id,
                power_limit=power_limit,
            )
        )
