"""Pydantic model of smartbox."""

from enum import StrEnum
from itertools import pairwise
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
    """NodeFactoryOptions model.

    Field presence is firmware-family-dependent: older families carry the
    full set (see tests/fixtures/devs), the fw-1.9 htr family only the
    first four plus operating_mode/power_factor/super_lock_available (see
    tests/fixtures/live).
    """

    temp_compensation_enabled: bool
    window_mode_available: bool
    true_radiant_available: bool
    duty_limit: int
    # Older-family fields (absent on fw-1.9 htr units).
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
    # fw-1.9-family additions.
    operating_mode: int | None = None
    power_factor: int | None = None
    super_lock_available: bool | None = None


class NodeExtraOptions(BaseModel):
    """NodeExtraOptions model."""

    boost_temp: str
    boost_time: int


class PmoSetup(BaseModel):
    """Pmo node setup."""

    # Keep unknown keys: the setup endpoint requires the full payload to
    # be re-posted, so any field the device returns must survive a
    # round-trip. Lossless-tolerant by design, unlike the status models
    # (strict with a rich error for drift) - see DefaultNodeSetup.
    model_config = ConfigDict(extra="allow")

    circuit_type: int
    power_limit: int
    reverse: bool


class DefaultNodeSetup(BaseModel):
    """NodeSetup model.

    ``user_duty_factor``/``flash_version``/``extra_options`` are absent on
    the fw-1.9 htr family; ``max_stemp_limit``/``priority``/``revision``
    only appear there.

    Unknown keys are kept (``extra="allow"``): the setup endpoint
    requires the full payload to be re-posted, so a typed read must not
    lose fields our models do not declare yet (e.g. ``counter_offset``
    on pmo nodes) — a consumer replaying the dump into a write would
    wipe them on the device. Deliberate asymmetry with the status
    models, which stay strict with a rich error for drift: setup
    payloads feed destructive full-repost writes where loss matters,
    statuses are read-only observations where drift is the signal.
    """

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
    factory_options: NodeFactoryOptions
    # Older-family fields (absent on fw-1.9 htr units).
    user_duty_factor: int | None = None
    flash_version: str | None = None
    extra_options: NodeExtraOptions | None = None
    # fw-1.9-family additions.
    max_stemp_limit: str | None = None
    priority: str | None = None
    revision: int | None = None


class NodeSetup(RootModel[DefaultNodeSetup | PmoSetup]):
    """NodeSetup model."""

    root: DefaultNodeSetup | PmoSetup

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Get the root model directly."""
        return getattr(self.root, name)


class NodeProg(BaseModel):
    """Node programme (weekly schedule).

    ``prog`` maps day keys "0".."6" (Mon..Sun) to per-slot profile-index
    ints: 24 hourly slots at ``prog_resolution`` 0, 48 half-hourly at 1.
    Profile indices on plain htr: 0=ICE, 1=ECO, 2=COMF. The GET response
    also carries ``sync_status``.
    """

    prog: dict[str, list[int]]
    sync_status: str | None = None


class NodeVersion(BaseModel):
    """NodeVersion model."""

    model_config = ConfigDict(extra="allow")

    hw_version: str
    fw_version: str
    uid: str
    pid: str


class DefaultNodeStatus(BaseModel):
    """Default Node Status."""

    mtemp: str
    units: str
    sync_status: str
    locked: bool
    mode: str
    # Firmware-dependent: newer units report an int code, older ones a
    # string like "none" (observed families pinned in tests/fixtures/live).
    error_code: str | int

    eco_temp: str
    comf_temp: str
    # Absent on newer htr firmware (fw 1.9 family); required on older ones.
    act_duty: int | None = None
    pcb_temp: str
    power_pcb_temp: str | None = None
    presence: bool
    window_open: bool
    true_radiant_active: bool
    boost: bool
    boost_end_min: int
    boost_end_day: int
    stemp: str
    power: str
    duty: int
    ice_temp: str
    active: bool


class HtrModNodeStatus(DefaultNodeStatus):
    """NodeStatus for htr_mod node."""

    on: bool
    selected_temp: str
    comfort_temp: str
    eco_offset: str


class HtrNodeStatus(DefaultNodeStatus):
    """NodeStatus for HTR node."""


class AcmNodeStatus(DefaultNodeStatus):
    """NodeStatus for acm node."""

    charging: bool
    charge_level: int


class NodeStatus(
    RootModel[
        AcmNodeStatus | HtrNodeStatus | HtrModNodeStatus | DefaultNodeStatus
    ]
):
    """NodeStatus model.

    Union resolution is pydantic smart-union: complete frames match their
    specific class (verified: htr_mod payloads resolve to
    HtrModNodeStatus). Note the degradation mode for *partial* frames:
    a payload missing one of the htr_mod extras validates as
    HtrNodeStatus and silently drops those extras, since HtrNodeStatus
    accepts any DefaultNodeStatus-shaped payload.
    """

    root: AcmNodeStatus | HtrNodeStatus | HtrModNodeStatus | DefaultNodeStatus

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Get the root model directly."""
        return getattr(self.root, name)


class Node(BaseModel):
    """Node model."""

    model_config = ConfigDict(extra="allow")

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

    model_config = ConfigDict(extra="allow")

    enabled: bool
    away: bool
    forced: bool


class Device(BaseModel):
    """Device model."""

    model_config = ConfigDict(extra="allow")

    dev_id: str
    name: str
    product_id: str
    fw_version: str
    serial_id: str


class Devices(BaseModel):
    """Devices model."""

    devs: list[Device]
    invited_to: list[Device] = []


class Home(BaseModel):
    """Home model."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    devs: list[Device] | None = None
    owner: bool


class Homes(RootModel[list[Home]]):
    """Homes model."""

    root: list[Home]


class Sample(BaseModel):
    """Sample model: temperature history (t, counter, temp).

    ``counter`` is a cumulative reading that may reset to zero; see
    ``Samples.counter_increments``.
    """

    t: int
    counter: float
    temp: str


class PmoSample(BaseModel):
    """PMO sample model: consumption history (t, counter, max, min)."""

    t: int
    counter: float
    max: int
    min: int


class Samples(BaseModel):
    """Samples model."""

    samples: list[PmoSample | Sample]

    def counter_increments(self) -> list[tuple[int, float, bool]]:
        """Per-interval counter increments with counter-reset detection.

        ``counter`` is a cumulative reading that can restart from zero
        (device reboot, firmware update). A drop between two consecutive
        samples is therefore a reset, never negative consumption.

        Returns
        -------
        list[tuple[int, float, bool]]
            One ``(t, increment, reset)`` per sample after the first, in
            timestamp order. On a reset the increment is the new counter
            value (what accrued since the restart) and ``reset`` is
            ``True`` so the consumer can decide whether to trust it. The
            raw ``samples`` are left untouched.

        """
        ordered = sorted(self.samples, key=lambda sample: sample.t)
        increments: list[tuple[int, float, bool]] = []
        for previous, current in pairwise(ordered):
            reset = current.counter < previous.counter
            delta = (
                current.counter
                if reset
                else (current.counter - previous.counter)
            )
            increments.append((current.t, delta, reset))
        return increments


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
