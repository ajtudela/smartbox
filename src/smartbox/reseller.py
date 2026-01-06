"""Reseller of Smartbox."""

import logging
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from smartbox.error import ResellerNotExistError

SMARTBOX_GENERIC_BASIC_AUTH = "NTRiY2NiZmI0MWE5YTUxMTNmMDQ4OGQwOnZkaXZkaQ=="
_LOGGER = logging.getLogger(__name__)


class SmartboxReseller(BaseModel):
    """Model of Smartbox Reseller config."""

    name: str = "Smartbox"
    web_url: str = ""
    api_url: str
    basic_auth: str = SMARTBOX_GENERIC_BASIC_AUTH
    serial_id: int = 0


class AvailableResellers:
    """Resellers that have been verified."""

    resellers: ClassVar[dict[str, SmartboxReseller]] = {
        "api": SmartboxReseller(
            name="Helki",
            web_url="https://app.helki.com/",
            api_url="api",
            serial_id=1,
        ),
        "api-ehc": SmartboxReseller(
            name="Electric Heating Company",
            web_url="https://control.electric-heatingcompany.co.uk/",
            api_url="api-ehc",
            serial_id=3,
        ),
        "api-climastar": SmartboxReseller(
            name="Climastar",
            web_url="https://avantwifi.climastar.es/",
            api_url="api-climastar",
            serial_id=5,
        ),
        "api-elnur": SmartboxReseller(
            name="Elnur",
            web_url="https://remotecontrol.elnur.es/",
            api_url="api-elnur",
            serial_id=7,
        ),
        "api-valderoma": SmartboxReseller(
            name="Valderoma",
            web_url="https://wifi.valderoma.fr/",
            api_url="api-climastar",
            serial_id=8,
        ),
        "api-hjm": SmartboxReseller(
            name="HJM",
            web_url="https://api.calorhjm.com/",
            api_url="api-hjm",
            serial_id=10,
        ),
        "api-evconfort": SmartboxReseller(
            name="Electrorad",
            web_url="https://app.controls-electrorad.co.uk/",
            api_url="api-evconfort",
            serial_id=12,
        ),
        "api-haverland": SmartboxReseller(
            name="Haverland",
            web_url="https://i2control.haverland.com/",
            api_url="api-haverland",
            basic_auth="NTU2ZDc0MWI3OGUzYmU5YjU2NjA3NTQ4OnZkaXZkaQ==",
            serial_id=14,
        ),
        "api-technoterm": SmartboxReseller(
            name="Technotherm",
            web_url="https://ttiapp.technotherm.com/",
            api_url="api-lhz",
            serial_id=16,
        ),
        "api-smartcontrol": SmartboxReseller(
            name="SmartControl",
            web_url="https://app.smart-control.eu/",
            api_url="api-lhz",
            serial_id=17,
        ),
        "api-lhz": SmartboxReseller(
            name="Lhz",
            web_url="https://api-lhz.helki.com",
            api_url="api-lhz",
            basic_auth="NTU2ZDc0MWI3OGUzYmU5YjU2NjA3NTQ4OnZkaXZkaQ==",
            serial_id=2,
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
        """Check if reseller is already available or try to create one."""
        self._api_url = api_url
        self._basic_auth = basic_auth
        self._web_url = web_url
        self._serial_id = serial_id
        self._name = name

    @property
    def reseller(self) -> SmartboxReseller:
        """Get the reseller."""
        reseller = next(
            (
                value
                for key, value in self.resellers.items()
                if key == self._api_url
            ),
            None,
        )
        if reseller is None:
            if (
                self._basic_auth is None
                or self._web_url is None
                or self._serial_id is None
            ):
                msg = f"This reseller {self._api_url} is not yet available or some arguments are missing."
                raise ResellerNotExistError(msg)
            try:
                _LOGGER.debug(
                    "Creating a new reseller api_url (%s), name=%s,  web_url %s, basic_auth=%s, serial_id=%s",
                    self._api_url,
                    self._name,
                    self._web_url,
                    self._basic_auth,
                    self._serial_id,
                )
                reseller = SmartboxReseller(
                    api_url=self._api_url,
                    basic_auth=self._basic_auth,
                    web_url=self._web_url,
                    serial_id=self._serial_id,
                    name=self._name,
                )
            except ValidationError as e:
                raise ResellerNotExistError from e
        _LOGGER.debug(
            "Reseller api_url (%s), name=%s,  web_url %s",
            reseller.api_url,
            reseller.name,
            reseller.web_url,
        )
        return reseller

    @property
    def api_url(self) -> str:
        """Get the api sub domain url."""
        return self.reseller.api_url

    @property
    def name(self) -> str:
        """Get the name of reseller."""
        return self.reseller.name

    @property
    def web_url(self) -> str:
        """Get the public websit of the reseller."""
        return self.reseller.web_url
