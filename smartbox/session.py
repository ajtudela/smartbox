import datetime
import json
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from typing import Any, Dict, List
from warnings import warn
from smartbox.error import SmartboxError
from aiohttp import (
    ClientResponse,
    ClientSession,
)
import asyncio

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_MIN_TOKEN_LIFETIME = 60  # Minimum time left before expiry before we refresh (seconds)

_LOGGER = logging.getLogger(__name__)


from smartbox.models import (
    Devices,
    Nodes,
    Node,
    Homes,
    NodeStatus,
    NodeSetup,
)


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
    ):
        self._api_name = api_name
        self._api_host = f"https://{self._api_name}.helki.com"
        self._basic_auth_credentials = basic_auth_credentials
        self._retry_attempts = retry_attempts
        self._backoff_factor = backoff_factor
        self._usernama = username
        self._password = password
        self._access_token = None
        self._client_session: ClientSession | None = websession

    @property
    def api_name(self) -> str:
        return self._api_name

    @property
    def access_token(self) -> str:
        return self._access_token

    def get_access_token(self) -> str:
        return self.access_token

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

    async def _authentication(self, credentials: Dict[str, str]):
        token_data = "&".join(f"{k}={v}" for k, v in credentials.items())
        token_headers = {
            "authorization": f"Basic {self._basic_auth_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        token_url = f"{self._api_host}/client/token"
        async with self.client as session:
            response = await session.post(
                url=token_url, headers=token_headers, data=token_data
            )
            response.raise_for_status()
        r = await response.json()

        if "access_token" not in r or "refresh_token" not in r or "expires_in" not in r:
            _LOGGER.error(
                f"Received invalid auth response, please check credentials: {r}"
            )
            raise SmartboxError("Received invalid auth response")
        self._access_token = r["access_token"]
        self._refresh_token = r["refresh_token"]
        if r["expires_in"] < _MIN_TOKEN_LIFETIME:
            _LOGGER.warning(
                (
                    f"Token expires in {r['expires_in']}s"
                    f", which is below minimum lifetime of {_MIN_TOKEN_LIFETIME}s"
                    " - will refresh again on next operation"
                )
            )
        self._expires_at = datetime.datetime.now() + datetime.timedelta(
            seconds=r["expires_in"]
        )
        _LOGGER.debug(
            (
                f"Authenticated session ({credentials['grant_type']}), "
                f"access_token={self.access_token}, expires at {self.expiry_time}"
            )
        )

    async def _check_refresh(self) -> None:
        if self._access_token is None:
            await self._authentication(
                {
                    "grant_type": "password",
                    "username": self._usernama,
                    "password": self._password,
                }
            )
        expired = (self._expires_at - datetime.datetime.now()) < datetime.timedelta(
            seconds=_MIN_TOKEN_LIFETIME
        )
        if expired:
            await self._authentication(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                }
            )

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _api_request(self, path: str) -> Any:
        await self._check_refresh()
        api_url = f"{self._api_host}/api/v2/{path}"
        async with self.client as session:
            response = await session.get(api_url, headers=self._get_headers())
            response.raise_for_status()
        return await response.json()

    async def _api_post(self, data: Any, path: str) -> Any:
        self._check_refresh()
        api_url = f"{self._api_host}/api/v2/{path}"
        # TODO: json dump
        try:
            data_str = json.dumps(data)
            _LOGGER.debug(f"Posting {data_str} to {api_url}")
            async with self.client as session:
                response = await session.post(
                    api_url, data=data_str, headers=self._get_headers()
                )
                response.raise_for_status()
        except requests.HTTPError as e:
            # TODO: logging
            _LOGGER.error(e)
            _LOGGER.error(e.response.json())
            raise
        return await response.json()


class AsyncSmartboxSession(AsyncSession):

    async def get_devices(self) -> List[Dict[str, Any]]:
        response = await self._api_request("devs")
        devices = Devices.model_validate(response).devs
        devices = [device.model_dump(mode="json") for device in devices]
        return devices

    async def get_homes(self) -> List[Dict[str, Any]]:
        response = await self._api_request("grouped_devs")
        homes = Homes.model_validate(response)
        return [home.model_dump(mode="json") for home in homes.root]

    async def get_grouped_devices(self) -> List[Dict[str, Any]]:
        response = await self._api_request("grouped_devs")
        homes = Homes.model_validate(response).root
        homes = [home.devs.model_dump(mode="json") for home in homes]
        return homes

    async def get_nodes(self, device_id: str) -> List[Dict[str, Any]]:
        response = await self._api_request(f"devs/{device_id}/mgr/nodes")
        nodes = Nodes.model_validate(response).nodes
        return [node.model_dump(mode="json") for node in nodes]

    async def get_device_away_status(self, device_id: str) -> Dict[str, Any]:
        return await self._api_request(f"devs/{device_id}/mgr/away_status")

    async def set_device_away_status(
        self, device_id: str, status_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        data = {k: v for k, v in status_args.items() if v is not None}
        return self._api_post(data=data, path=f"devs/{device_id}/mgr/away_status")

    async def get_device_power_limit(self, device_id: str) -> int:
        resp = self._api_request(f"devs/{device_id}/htr_system/power_limit")
        return int(resp["power_limit"])

    async def set_device_power_limit(self, device_id: str, power_limit: int) -> None:
        data = {"power_limit": str(power_limit)}
        self._api_post(data=data, path=f"devs/{device_id}/htr_system/power_limit")

    async def get_node_status(
        self, device_id: str, node: Dict[str, Any]
    ) -> Dict[str, str]:
        node = Node.model_validate(node)
        return NodeStatus.model_validate(
            await self._api_request(f"devs/{device_id}/{node.type}/{node.addr}/status")
        ).model_dump(mode="json")

    async def set_node_status(
        self,
        device_id: str,
        node: Dict[str, Any],
        status_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        node = Node.model_validate(node)
        data = {k: v for k, v in status_args.items() if v is not None}
        if "stemp" in data and "units" not in data:
            raise ValueError("Must supply unit with temperature fields")
        return self._api_post(
            data=data, path=f"devs/{device_id}/{node.type}/{node.addr}/status"
        )

    async def get_node_setup(
        self, device_id: str, node: Dict[str, Any]
    ) -> Dict[str, Any]:
        node = Node.model_validate(node)
        return NodeSetup.model_validate(
            await self._api_request(f"devs/{device_id}/{node.type}/{node.addr}/setup")
        ).model_dump(mode="json")

    async def set_node_setup(
        self, device_id: str, node: Dict[str, Any], setup_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        node = Node.model_validate(node)
        data = {k: v for k, v in setup_args.items() if v is not None}
        # setup seems to require all settings to be re-posted, so get current
        # values and update
        setup_data = self.get_setup(device_id, node)
        setup_data.update(data)
        return await self._api_post(
            data=setup_data,
            path=f"devs/{device_id}/{node.type}/{node.addr}/setup",
        )


class Session:
    def __init__(self, *args, **kwargs):
        self._async = AsyncSmartboxSession(*args, **kwargs)

    def get_devices(self) -> List[Dict[str, Any]]:
        return asyncio.run(self._async.get_devices())

    def get_homes(self) -> List[Dict[str, Any]]:
        return asyncio.run(self._async.get_homes())

    def get_grouped_devices(self) -> List[Dict[str, Any]]:
        return asyncio.run(self._async.get_grouped_devices())

    def get_nodes(self, device_id: str) -> List[Dict[str, Any]]:
        return asyncio.run(self._async.get_nodes(device_id=device_id))

    def get_status(self, device_id: str, node: Dict[str, Any]) -> Dict[str, str]:
        return asyncio.run(self._async.get_node_status(device_id=device_id, node=node))

    def set_status(
        self, device_id: str, node: Dict[str, Any], status_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        return asyncio.run(
            self._async.set_node_status(
                device_id=device_id, node=node, status_args=status_args
            )
        )

    def get_setup(self, device_id: str, node: Dict[str, Any]) -> Dict[str, Any]:
        return asyncio.run(self._async.get_node_setup(device_id=device_id, node=node))

    def set_setup(
        self, device_id: str, node: Dict[str, Any], setup_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        return asyncio.run(
            self.async_set_node_setup(
                device_id=device_id, node=node, setup_args=setup_args
            )
        )

    def get_device_away_status(self, device_id: str) -> Dict[str, Any]:
        return asyncio.run(self._async.get_device_away_status(device_id=device_id))

    def set_device_away_status(
        self, device_id: str, status_args: Dict[str, Any]
    ) -> Dict[str, Any]:
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
