"""Pydantic model of smartbox."""

from pydantic import BaseModel, RootModel


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


class NodeSetup(BaseModel):
    """NodeSetup model."""

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


class NodeStatus(BaseModel):
    """NodeStatus model."""

    eco_temp: str
    comf_temp: str
    act_duty: int
    pcb_temp: str
    power_pcb_temp: str
    presence: bool
    window_open: bool
    true_radiant_active: bool
    boost: bool
    boost_end_min: int
    boost_end_day: int
    error_code: str
    stemp: str
    power: str
    duty: int
    mtemp: str
    ice_temp: str
    units: str
    sync_status: str
    locked: int
    active: bool
    mode: str


class Node(BaseModel):
    """Node model."""

    name: str
    addr: int
    type: str
    installed: bool
    lost: bool


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
    invited_to: list


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
    """Sample model."""

    t: int
    temp: str
    counter: int


class Samples(BaseModel):
    """Samples model."""

    samples: list[Sample]


class Token(BaseModel):
    """Token model."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
