"""Pydantic model of smartbox."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class SmartboxNodeType(StrEnum):
    """Known node types.

    The list is not exhaustive: the third-party API adds types without notice,
    so ``Node.type`` is ``SmartboxNodeType | str`` and an unrecognised type is
    kept verbatim (URL building still works, only the typed models narrow).
    """

    HTR = "htr"
    THM = "thm"
    HTR_MOD = "htr_mod"
    ACM = "acm"
    PMO = "pmo"


# The models below describe what a smartbox device *usually* returns, but the
# API is undocumented, third-party and varies by reseller and firmware, so every
# field is optional and unknown keys are kept (``extra="allow"``). The models are
# a shape hint, not a schema: a status/setup is degraded, never rejected. Once
# more resellers have been sampled a small verified core could be tightened back.


class NodeFactoryOptions(BaseModel):
    """NodeFactoryOptions model. Field set varies widely between resellers."""

    model_config = ConfigDict(extra="allow")

    temp_compensation_enabled: bool | None = None
    window_mode_available: bool | None = None
    true_radiant_available: bool | None = None
    duty_limit: int | None = None
    boost_config: int | None = None
    button_double_press: bool | None = None
    prog_resolution: int | None = None
    bbc_value: int | None = None
    bbc_available: bool | None = None
    lst_value: int | None = None
    lst_available: bool | None = None
    fil_pilote_available: bool | None = None
    backlight_time: int | None = None
    button_down_code: int | None = None
    button_up_code: int | None = None
    button_mode_code: int | None = None
    button_prog_code: int | None = None
    button_off_code: int | None = None
    button_boost_code: int | None = None
    splash_screen_type: int | None = None


class NodeExtraOptions(BaseModel):
    """NodeExtraOptions model."""

    model_config = ConfigDict(extra="allow")

    boost_temp: str | None = None
    boost_time: int | None = None


class PmoSetup(BaseModel):
    """Pmo node setup."""

    model_config = ConfigDict(extra="allow")

    circuit_type: int | None = None
    power_limit: int | None = None
    reverse: bool | None = None


class DefaultNodeSetup(BaseModel):
    """Node setup for every non-``pmo`` node type."""

    model_config = ConfigDict(extra="allow")

    sync_status: str | None = None
    control_mode: int | None = None
    units: str | None = None
    power: str | None = None
    offset: str | None = None
    away_mode: int | None = None
    away_offset: str | None = None
    modified_auto_span: int | None = None
    window_mode_enabled: bool | None = None
    true_radiant_enabled: bool | None = None
    user_duty_factor: int | None = None
    flash_version: str | None = None
    factory_options: NodeFactoryOptions | None = None
    extra_options: NodeExtraOptions | None = None


class NodeSetup(RootModel[DefaultNodeSetup | PmoSetup]):
    """Lenient wrapper kept for backwards compatibility.

    ``get_node_setup`` returns the concrete ``PmoSetup`` / ``DefaultNodeSetup``
    selected by node type; this root model still validates a bare setup blob.
    """

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


class NodeProg(BaseModel):
    """Weekly heating schedule of a node.

    ``prog`` maps a weekday index (``"0"``..``"6"``) to 24 hourly slot values.
    """

    model_config = ConfigDict(extra="allow")

    sync_status: str | None = None
    prog: dict[str, list[int]] | None = None


class DeviceVersion(BaseModel):
    """Manager/system firmware version of a device (``mgr/version``)."""

    model_config = ConfigDict(extra="allow")

    fw_version: str | None = None
    hw_version: str | None = None
    product_id: str | None = None


class HtrSystemExtraNrgConf(BaseModel):
    """The ``extra_nrg_conf`` block of the heater-system setup."""

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None


class HtrSystemSetup(BaseModel):
    """Heater-system configuration of a device (``htr_system/setup``)."""

    model_config = ConfigDict(extra="allow")

    power_limit: int | None = None
    refresh_period: int | None = None
    extra_nrg_conf: HtrSystemExtraNrgConf | None = None


class DefaultNodeStatus(BaseModel):
    """Node status shared by every node type.

    Every field is optional and unknown keys are kept, so a status from an
    uncommon firmware or reseller is degraded rather than rejected.
    """

    model_config = ConfigDict(extra="allow")

    mtemp: str | None = None
    units: str | None = None
    sync_status: str | None = None
    locked: bool | None = None
    mode: str | None = None
    # Observed as a string on most devices and an integer on others.
    error_code: str | int | None = None

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


class HtrNodeStatus(DefaultNodeStatus):
    """Status of an ``htr`` node.

    An ``htr`` node exposes the shared fields with nothing extra; the class
    exists so ``get_node_status`` can hand back a type-named object.
    """


class HtrModNodeStatus(DefaultNodeStatus):
    """Status of an ``htr_mod`` (modulating) node."""

    on: bool | None = None
    selected_temp: str | None = None
    comfort_temp: str | None = None
    eco_offset: str | None = None


class AcmNodeStatus(DefaultNodeStatus):
    """Status of an ``acm`` (accumulator) node."""

    charging: bool | None = None
    charge_level: int | None = None


class NodeStatus(RootModel[DefaultNodeStatus]):
    """Lenient wrapper kept for backwards compatibility.

    ``get_node_status`` returns the concrete type-specific model; this root
    model still validates a bare status blob.
    """

    root: DefaultNodeStatus

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Proxy attribute access to the resolved root model."""
        return getattr(self.root, name)


class Node(BaseModel):
    """Node model."""

    name: str
    addr: int
    # left-to-right so a known type deserialises to the enum and only a genuinely
    # unknown one falls through to a plain string.
    type: SmartboxNodeType | str = Field(union_mode="left_to_right")
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
