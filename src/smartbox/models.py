"""Pydantic model of smartbox."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel


class SmartboxNodeType(StrEnum):
    """Node type."""

    HTR = "htr"
    THM = "thm"
    HTR_MOD = "htr_mod"
    ACM = "acm"
    PMO = "pmo"


class NodeFactoryOptions(BaseModel):
    """NodeFactoryOptions model."""

    temp_compensation_enabled: bool
    window_mode_available: bool
    true_radiant_available: bool
    duty_limit: int
    boost_config: int
    button_double_press: bool
    prog_resolution: int
    bbc_value: int
    bbc_available: bool
    lst_value: int
    lst_available: bool
    fil_pilote_available: bool
    backlight_time: int
    button_down_code: int
    button_up_code: int
    button_mode_code: int
    button_prog_code: int
    button_off_code: int
    button_boost_code: int
    splash_screen_type: int


class NodeExtraOptions(BaseModel):
    """NodeExtraOptions model."""

    boost_temp: str
    boost_time: int


class PmoSetup(BaseModel):
    """Pmo node setup."""

    # Keep unknown keys: the setup endpoint requires the full payload to be
    # re-posted, so any field the device returns must survive a round-trip.
    model_config = ConfigDict(extra="allow")

    circuit_type: int
    power_limit: int
    reverse: bool


class DefaultNodeSetup(BaseModel):
    """NodeSetup model."""

    # Keep unknown keys: the setup endpoint requires the full payload to be
    # re-posted, so any field the device returns must survive a round-trip.
    model_config = ConfigDict(extra="allow")

    sync_status: str
    control_mode: int
    units: str
    power: str
    offset: str
    away_mode: int
    away_offset: str
    modified_auto_span: int
    window_mode_enabled: bool
    true_radiant_enabled: bool
    user_duty_factor: int
    flash_version: str
    factory_options: NodeFactoryOptions
    extra_options: NodeExtraOptions


class NodeSetup(RootModel[DefaultNodeSetup | PmoSetup]):
    """NodeSetup model."""

    root: DefaultNodeSetup | PmoSetup

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Proxy attribute access to the resolved root model."""
        return getattr(self.root, name)


class NodeVersion(BaseModel):
    """NodeVersion model."""

    hw_version: str
    fw_version: str
    uid: str
    pid: str


class DefaultNodeStatus(BaseModel):
    """Fallback node status.

    As the last arm of the ``NodeStatus`` union this must tolerate whatever a
    given firmware or reseller returns: every field is optional and unknown keys
    are kept, so a status is degraded rather than rejected. Type-specific models
    keep their own fields required.
    """

    model_config = ConfigDict(extra="allow")

    mtemp: str | None = None
    units: str | None = None
    sync_status: str | None = None
    locked: bool | None = None
    mode: str | None = None
    error_code: str | None = None

    eco_temp: str | None = None
    comf_temp: str | None = None
    act_duty: int | None = None
    pcb_temp: str | None = None
    power_pcb_temp: str | None = None
    presence: bool | None = None
    window_open: bool | None = None
    true_radiant_active: bool | None = None
    boost: bool | None = None
    boost_end_min: int | None = None
    boost_end_day: int | None = None
    stemp: str | None = None
    power: str | None = None
    duty: int | None = None
    ice_temp: str | None = None
    active: bool | None = None


class HtrModNodeStatus(DefaultNodeStatus):
    """NodeStatus for htr_mod node."""

    on: bool
    selected_temp: str
    comfort_temp: str
    eco_offset: str
    ice_temp: str
    active: bool


class HtrNodeStatus(DefaultNodeStatus):
    """NodeStatus for HTR node."""

    stemp: str
    active: bool
    power: str
    duty: int


class AcmNodeStatus(DefaultNodeStatus):
    """NodeStatus for acm node."""

    stemp: str
    charging: bool
    charge_level: int
    power: str


class NodeStatus(
    RootModel[
        AcmNodeStatus | HtrNodeStatus | HtrModNodeStatus | DefaultNodeStatus
    ]
):
    """NodeStatus model."""

    root: AcmNodeStatus | HtrNodeStatus | HtrModNodeStatus | DefaultNodeStatus

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Proxy attribute access to the resolved root model."""
        return getattr(self.root, name)


class Node(BaseModel):
    """Node model."""

    name: str
    addr: int
    type: SmartboxNodeType
    installed: bool
    lost: bool | None = False


class Nodes(BaseModel):
    """Nodes model."""

    nodes: list[Node]


class DeviceAwayStatus(BaseModel):
    """DeviceAwayStatus model."""

    enabled: bool
    away: bool
    forced: bool


class Device(BaseModel):
    """Device model."""

    dev_id: str
    name: str
    product_id: str
    fw_version: str
    serial_id: str


class Devices(BaseModel):
    """Devices model."""

    devs: list[Device]
    invited_to: list[Device]


class Home(BaseModel):
    """Home model."""

    id: str
    name: str
    devs: list[Device] | None = None
    owner: bool


class Homes(RootModel[list[Home]]):
    """Homes model."""

    root: list[Home]


class Sample(BaseModel):
    """Default sample model."""

    t: int
    counter: float
    temp: str


class PmoSample(BaseModel):
    """Pmo sample model."""

    t: int
    counter: float
    max: int
    min: int


class Samples(BaseModel):
    """Samples model."""

    samples: list[PmoSample | Sample]


class Token(BaseModel):
    """Token model."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


class GuestUser(BaseModel):
    """Guest model."""

    pending: bool
    email: str


class Guests(BaseModel):
    """Guests model."""

    guest_users: list[GuestUser]


class DeviceConnected(BaseModel):
    """Connected status of devices."""

    connected: bool
