from pydantic import BaseModel
from pydantic import ValidationError
from smartbox.error import ResailerNotExist

SMARTBOX_GENERIC_BASIC_AUTH = "NTRiY2NiZmI0MWE5YTUxMTNmMDQ4OGQwOnZkaXZkaQ=="


class SmartboxResailer(BaseModel):
    """Class of Smartbox Resailer config."""

    name: str = "Smartbox"
    web_url: str = ""
    api_url: str
    basic_auth: str = SMARTBOX_GENERIC_BASIC_AUTH
    serial_id: int | None = None


class AvailableResailers(object):
    _resailers: dict[str, SmartboxResailer] = {
        "api": SmartboxResailer(
            name="Smartbox",
            api_url="api",
        ),
        "api-helki": SmartboxResailer(
            name="Helki",
            web_url="https://app.helki.com/",
            api_url="api-helki",
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
        serial_id: str | None = None,
        name: str = "Smartbox",
    ) -> SmartboxResailer:
        resailer = self._resailers.get(api_url, None)
        if resailer is None:
            try:
                resailer = SmartboxResailer(
                    api_url=api_url,
                    basic_auth=basic_auth,
                    web_url=web_url,
                    serial_id=serial_id,
                    name=name,
                )
            except ValidationError as e:
                raise ResailerNotExist from e
        self._resailer = resailer

    @property
    def resailer(self) -> SmartboxResailer:
        return self._resailer
