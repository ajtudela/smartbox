import datetime
import json
import logging
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from typing import Any, Dict, List

from smartbox.error import SmartboxError

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_MIN_TOKEN_LIFETIME = 60  # Minimum time left before expiry before we refresh (seconds)

_LOGGER = logging.getLogger(__name__)


class Session(object):
    def __init__(
        self,
        api_name: str,
        basic_auth_credentials: str,
        username: str,
        password: str,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    ) -> None:
        self._api_name = api_name
        self._api_host = f"https://{self._api_name}.helki.com"
        self._basic_auth_credentials = basic_auth_credentials

        self._requests = requests.Session()
        retry_strategy = Retry(  # type: ignore
            total=retry_attempts,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        http_adapter = HTTPAdapter(max_retries=retry_strategy)
        self._requests.mount("http://", http_adapter)
        self._requests.mount("https://", http_adapter)

        self._auth(
            {"grant_type": "password", "username": username, "password": password}
        )

    def _auth(self, credentials: Dict[str, str]) -> None:
        token_data = "&".join(f"{k}={v}" for k, v in credentials.items())
        token_headers = {
            "authorization": f"Basic {self._basic_auth_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        token_url = f"{self._api_host}/client/token"
        response = self._requests.post(
            token_url, data=token_data, headers=token_headers
        )
        response.raise_for_status()
        r = response.json()
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
                f"access_token={self._access_token}, expires at {self._expires_at}"
            )
        )

    def _has_token_expired(self) -> bool:
        return (self._expires_at - datetime.datetime.now()) < datetime.timedelta(
            seconds=_MIN_TOKEN_LIFETIME
        )

    def _check_refresh(self) -> None:
        if self._has_token_expired():
            self._auth(
                {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
            )

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            # TODO: generalise
            "x-serialid": "5",
        }

    def _api_request(self, path: str) -> Any:
        self._check_refresh()
        api_url = f"{self._api_host}/api/v2/{path}"
        response = self._requests.get(api_url, headers=self._get_headers())
        response.raise_for_status()
        return response.json()

    def _api_post(self, data: Any, path: str) -> Any:
        self._check_refresh()
        api_url = f"{self._api_host}/api/v2/{path}"
        # TODO: json dump
        try:
            data_str = json.dumps(data)
            _LOGGER.debug(f"Posting {data_str} to {api_url}")
            response = self._requests.post(
                api_url, data=data_str, headers=self._get_headers()
            )
            response.raise_for_status()
        except requests.HTTPError as e:
            # TODO: logging
            _LOGGER.error(e)
            _LOGGER.error(e.response.json())
            raise
        return response.json()

    def get_api_name(self) -> str:
        return self._api_name

    def get_access_token(self) -> str:
        return self._access_token

    def get_refresh_token(self) -> str:
        return self._refresh_token

    def get_expiry_time(self) -> datetime.datetime:
        return self._expires_at

    def get_devices(self) -> List[Dict[str, Any]]:
        response = self._api_request("devs")
        return response["devs"]

    def get_grouped_devices(self):
        response = self._api_request("grouped_devs")
        return response

    def get_nodes(self, device_id: str) -> List[Dict[str, Any]]:
        response = self._api_request(f"devs/{device_id}/mgr/nodes")
        return response["nodes"]

    def get_status(self, device_id: str, node: Dict[str, Any]) -> Dict[str, str]:
        return self._api_request(
            f"devs/{device_id}/{node['type']}/{node['addr']}/status"
        )

    def set_status(
        self,
        device_id: str,
        node: Dict[str, Any],
        status_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        data = {k: v for k, v in status_args.items() if v is not None}
        if "stemp" in data and "units" not in data:
            raise ValueError("Must supply unit with temperature fields")
        return self._api_post(
            data=data, path=f"devs/{device_id}/{node['type']}/{node['addr']}/status"
        )

    def get_setup(self, device_id: str, node: Dict[str, Any]) -> Dict[str, Any]:
        return self._api_request(
            f"devs/{device_id}/{node['type']}/{node['addr']}/setup"
        )

    def set_setup(
        self,
        device_id: str,
        node: Dict[str, Any],
        setup_args: Dict[str, Any],
    ) -> Dict[str, Any]:
        data = {k: v for k, v in setup_args.items() if v is not None}
        # setup seems to require all settings to be re-posted, so get current
        # values and update
        setup_data = self.get_setup(device_id, node)
        setup_data.update(data)
        return self._api_post(
            data=setup_data,
            path=f"devs/{device_id}/{node['type']}/{node['addr']}/setup",
        )

    def get_device_away_status(self, device_id: str) -> Dict[str, Any]:
        return self._api_request(f"devs/{device_id}/mgr/away_status")

    def set_device_away_status(
        self, device_id: str, status_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        data = {k: v for k, v in status_args.items() if v is not None}
        return self._api_post(data=data, path=f"devs/{device_id}/mgr/away_status")

    def get_device_power_limit(self, device_id: str) -> int:
        resp = self._api_request(f"devs/{device_id}/htr_system/power_limit")
        return int(resp["power_limit"])

    def set_device_power_limit(self, device_id: str, power_limit: int) -> None:
        data = {"power_limit": str(power_limit)}
        self._api_post(data=data, path=f"devs/{device_id}/htr_system/power_limit")
