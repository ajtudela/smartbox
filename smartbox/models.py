from typing import Optional

from pydantic import BaseModel, RootModel


class NodeFactoryOptions(BaseModel):
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
    boost_temp: str
    boost_time: int


class NodeSetup(BaseModel):
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
    name: str
    addr: int
    type: str
    installed: bool
    lost: bool


class Nodes(BaseModel):
    nodes: list[Node]


class DeviceAwayStatus(BaseModel):
    enabled: bool
    away: bool
    forced: bool


class Device(BaseModel):
    dev_id: str
    name: str
    product_id: str
    fw_version: str
    serial_id: str


class Devices(BaseModel):
    devs: list[Device]
    invited_to: list


class Home(BaseModel):
    id: str
    name: str
    devs: Optional[list[Device]] = None
    owner: bool


class Homes(RootModel[list[Home]]):
    root: list[Home]


class Sample(BaseModel):
    t: int
    temp: str
    counter: int


class Samples(BaseModel):
    samples: list[Sample]


class Token(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str
