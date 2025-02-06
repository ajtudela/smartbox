"""Resailer of Smartbox."""

import logging
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from smartbox.error import ResailerNotExistError

SMARTBOX_GENERIC_BASIC_AUTH = "NTRiY2NiZmI0MWE5YTUxMTNmMDQ4OGQwOnZkaXZkaQ=="
_LOGGER = logging.getLogger(__name__)


class SmartboxResailer(BaseModel):
    """Model of Smartbox Resailer config."""

    name: str = "Smartbox"
    web_url: str = ""
    api_url: str
    basic_auth: str = SMARTBOX_GENERIC_BASIC_AUTH
    serial_id: int = 0


class AvailableResailers:
    """Resailers that have been verified."""

    resailers: ClassVar[dict[str, SmartboxResailer]] = {
        "api": SmartboxResailer(
            name="Helki",
            web_url="https://app.helki.com/",
            api_url="api",
            serial_id=1,
        ),
        "api-climastar": SmartboxResailer(
            name="Climastar",
            web_url="https://avantwifi.climastar.es/",
            api_url="api-climastar",
            serial_id=5,
        ),
        "api-elnur": SmartboxResailer(
            name="Elnur",
            web_url="https://remotecontrol.elnur.es/",
            api_url="api-elnur",
            serial_id=7,
        ),
        "api-hjm": SmartboxResailer(
            name="HJM",
            web_url="https://api.calorhjm.com/",
            api_url="api-hjm",
            serial_id=10,
        ),
        "api-haverland": SmartboxResailer(
            name="Haverland",
            web_url="https://i2control.haverland.com/",
            api_url="api-haverland",
            basic_auth="NTU2ZDc0MWI3OGUzYmU5YjU2NjA3NTQ4OnZkaXZkaQ==",
            serial_id=14,
        ),
        "api-lhz": SmartboxResailer(
            name="Technotherm",
            web_url="https://ttiapp.technotherm.com/",
            api_url="api-lhz",
            serial_id=16,
        ),
    }

    def __init__(
        self,
        api_url: str,
        basic_auth: str | None = None,
        web_url: str | None = None,
        serial_id: int | None = None,
        name: str = "Smartbox",
    ) -> None:
        """Check if resailer is already available or try to create one."""
        self._api_url = api_url
        self._basic_auth = basic_auth
        self._web_url = web_url
        self._serial_id = serial_id
        self._name = name

    @property
    def resailer(self) -> SmartboxResailer:
        """Get the resailer."""
        resailer = self.resailers.get(self._api_url, None)
        if resailer is None:
            if (
                self._basic_auth is None
                or self._web_url is None
                or self._serial_id is None
            ):
                msg = "This Resailer is not yet available or some arguments are missing."
                raise ResailerNotExistError(msg)
            try:
                _LOGGER.debug(
                    "Creating a new resailer api_url (%s), name=%s,  web_url %s, basic_auth=%s, serial_id=%s",
                    self._api_url,
                    self._name,
                    self._web_url,
                    self._basic_auth,
                    self._serial_id,
                )
                resailer = SmartboxResailer(
                    api_url=self._api_url,
                    basic_auth=self._basic_auth,
                    web_url=self._web_url,
                    serial_id=self._serial_id,
                    name=self._name,
                )
            except ValidationError as e:
                raise ResailerNotExistError from e
        _LOGGER.debug(
            "Resailer api_url (%s), name=%s,  web_url %s",
            self.api_url,
            self.name,
            self.web_url,
        )
        return resailer

    @property
    def api_url(self) -> str:
        """Get the api sub domain url."""
        return self.resailer.api_url

    @property
    def name(self) -> str:
        """Get the name of resailer."""
        return self.resailer.name

    @property
    def web_url(self) -> str:
        """Get the public websit of the resailer."""
        return self.resailer.web_url
