"""Named write operations: the wire write rules as a named API.

Each op documents the body it posts. The rules they encode (live-verified;
canonical copies in hass-smartbox docs/api-contract.md §5):

- ONE mutable key per status POST: multi-key bodies are silently PARTIALLY
  APPLIED (a ``{locked, stemp, mode}`` restore stored locked+mode and dropped
  stemp). The multi-key bodies this module sends are the app-shaped ones: the
  ``modified_auto`` engage body, the 4-key profile temps, and the htr_mod
  setpoint body (hass-smartbox docs/api-contract.md §5.1); the htr_mod body
  carries no ``units`` key (the scale is used only to format ``comfort_temp``).
- The app-shaped ``{stemp, units, mode: "modified_auto"}`` body engages the
  override ONLY when that mode is sent explicitly; a plain ``{stemp, units}``
  write in auto applies immediately but does not persist across programme
  transitions. Engaging is the CALLER's policy - it needs the node's mode and
  the setup's ``modified_auto_span``.
- Off-grid Celsius setpoints are quantized by the device, so the client rounds
  onto the 0.5 grid and formats ``.1f``; Fahrenheit quantizes to whole degrees
  (the vendor app's F grid is integral, hass-smartbox docs/api-contract.md §5.2).
- ``"0.0"`` is the sentinel for a disabled ``max_stemp_limit``; remembering the
  last non-zero value is the CALLER's concern.
- Setup and prog writes re-post the WHOLE object, because the API wipes unknown
  keys otherwise. Prog merges at DAY level (string keys ``"0"``..``"6"``); a
  bare 7-array list is rejected with 400. The merge base is the caller's
  :class:`SlotReader` when it holds the body: a successful write is not visible
  to GET for several seconds (hass-smartbox docs/api-contract.md §5.4), so merging over a
  REST read would post a previous write's key back at its pre-write value. A
  GET is the fallback while the store has no body yet.
- A write is confirmed by the frame the server pushes. If no frame arrives,
  core reads the written slot once after the confirmation window; the GET
  reconciles a silently dropped write, it does not replace the frame path.
  Each op reports what it posted (:class:`PostedWrite`), and core decides what
  proves it landed.
- Writes are NEVER blind-retried on transient failures (the server may
  have applied the write): single POST attempt, errors per the client's
  error contract (4xx -> ``SmartboxError``, 5xx/network/timeout ->
  ``TransportUnavailableError``, one 401 -> invalidate + re-auth +
  resend).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Literal, Protocol

from .errors import SmartboxError
from .paths import NodeRef, encode_segment, node_path
from .values import (
    bounded_repr,
    format_setpoint,
    node_units_to_celsius,
    prog_days,
    require_number,
    validate_prog_days,
    validate_units,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

__all__ = [
    "VALID_PRIORITIES",
    "PostedWrite",
    "SlotReader",
    "WriteApi",
    "WriteHost",
    "WriteSlot",
]

_VALID_MODES = ("off", "manual", "auto", "modified_auto", "self_learn", "presence")
VALID_PRIORITIES = ("low", "medium", "high")
# The /boost_mode mode vocabulary, exactly as the current app sends it
# (docs/api-ws-user.md §2.3; app mobile 2.41.0 vendors.js@1533601).
VALID_BOOST_MODES = (
    "temp_and_time",
    "max_temp_fixed_time",
    "advanced_slot",
    "runback",
)

# The slot vocabulary a posted write names for its caller: node slots (the
# engine's confirmation/merge vocabulary), the two device-level writes, the
# node's own power limit, and the full-boost endpoint (whose confirming frame
# is a status frame carrying boost/boost_mode).
WriteSlot = Literal[
    "status",
    "setup",
    "prog",
    "away",
    "power_limit",
    "node_power_limit",
    "boost_mode",
    "boost",
]


class WriteHost(Protocol):
    """The seam :class:`WriteApi` and timeout verification need from a host.

    Satisfied by :class:`~smartbox.protocol.client.SmartboxClient` and by the
    test suite's in-memory fake (``tests/fake_api.py``).
    """

    async def post(self, path: str, body: Mapping[str, object]) -> None:
        """One POST attempt with the write error contract (never retried).

        Public on purpose: :class:`WriteApi` composes around it.
        """

    async def get_node_status(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """Raw GET of the node's status (write-confirmation verification)."""

    async def get_node_setup(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """Raw GET of the node's setup (the GET half of GET-merge-POST)."""

    async def get_node_prog(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """Raw GET of the node's schedule (the GET half of GET-merge-POST)."""


class SlotReader(Protocol):
    """A caller's own view of a node slot, when it has one.

    Core supplies this so a setup or prog write merges over the body it last
    saw instead of a REST read that still lags the previous write
    (hass-smartbox docs/api-contract.md §5.4). Each method returns the slot body, or ``None``
    while the store holds nothing for that slot - the GET fallback then applies.
    """

    def setup(self, dev_id: str, node: NodeRef) -> Mapping[str, Any] | None:
        """Return the node's last known setup body, or ``None``."""

    def prog(self, dev_id: str, node: NodeRef) -> Mapping[str, Any] | None:
        """Return the node's last known schedule, day-keyed, or ``None``."""

    def version(self, dev_id: str, node: NodeRef) -> Mapping[str, Any] | None:
        """Return the node's last known version body, or ``None``."""


@dataclass(frozen=True)
class PostedWrite:
    """One request as it goes out, handed to the caller's ``on_post`` hook.

    ``path`` is the REST path the host posts to; ``dev_id`` / ``node`` /
    ``slot`` are the same request's addressing in store terms, so a caller
    never has to re-parse the path to learn what the write touched. ``node`` is
    ``None`` for the device-level ops. ``body`` is the object that was posted:
    a setup op posts the whole setup, so the body is not the same thing as the
    keys the write changes, which the caller derives from the body and its own
    stored copy.
    """

    path: str
    body: dict[str, Any]
    dev_id: str
    node: NodeRef | None
    slot: WriteSlot


# App ``shouldOmitSetupExtraOptions`` product codes. These controllers reject
# or silently drop an ``extra_options`` object, so every setup POST omits it.
_PID_CODE_END = 4
_DAYS_PER_WEEK = 7
_MINUTES_PER_DAY = 1440
_THERMOSTAT_DAY_KEYS = frozenset({"0", "1", "2", "3", "4", "5", "6"})
_OMIT_EXTRA_OPTIONS_CODES = frozenset(
    {"01", "02", "07", "08", "0D", "10", "11", "13", "16", "18", "1B", "1E", "1F"}
)


class WriteApi:
    """The named write operations, composed around a write host.

    The engine builds one per write (with its arming hook and store reader);
    tests and the probe tooling build theirs directly. Ops validate their
    inputs (``ValueError``) BEFORE any network call and return the request they
    sent as ``(path, body)``, so a caller merges exactly what went out.
    """

    def __init__(
        self,
        client: WriteHost,
        on_post: Callable[[PostedWrite], None] | None = None,
        store: SlotReader | None = None,
    ) -> None:
        """Wrap one host; all writes share its token plumbing.

        ``on_post`` fires just before the request goes out: the engine arms its
        write confirmation through it, and a GET-merge-POST op's GET stays
        outside the window. The posted body comes back from the op's return
        value instead. ``store`` is the caller's fresher view of the setup and
        prog slots; without it every merge reads over REST.
        """
        self._client = client
        self._on_post = on_post
        self._store = store

    # --- status ops (single key + units companion) ---

    async def set_mode(
        self, dev_id: str, node: NodeRef, mode: str
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"mode": <mode>}``."""
        if mode not in _VALID_MODES:
            msg = f"mode must be one of {_VALID_MODES}, got: {mode!r}"
            raise ValueError(msg)
        return await self._post(
            node_path(dev_id, node, "status"),
            {"mode": mode},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_setpoint(
        self, dev_id: str, node: NodeRef, temp: float, units: str
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"stemp": <formatted>, "units": <units>}``.

        The plain write: in auto it applies immediately, without persisting the
        override across programme transitions (see ``engage_modified_auto``).
        """
        return await self._post(
            node_path(dev_id, node, "status"),
            {"stemp": format_setpoint(temp, units), "units": units},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def engage_modified_auto(
        self,
        dev_id: str,
        node: NodeRef,
        temp: float,
        units: str,
    ) -> tuple[str, dict[str, Any]]:
        """POST the app-shaped body ``{"stemp", "units", "mode": "modified_auto"}``.

        Applies the setpoint and engages the override in one write;
        ``{"mode": "auto"}`` reverts. The caller decides when to use it.
        """
        return await self._post(
            node_path(dev_id, node, "status"),
            {
                "stemp": format_setpoint(temp, units),
                "units": units,
                "mode": "modified_auto",
            },
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_lock(
        self, dev_id: str, node: NodeRef, locked: bool | int
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"locked": bool | 0|1|2}``.

        The base ElectricHeater/Haverland settings path uses a boolean; the
        storage, Helki-wifi and Tevolve-wifi families use the wire enum
        ``0`` (unblocked), ``1`` (locally blocked) or ``2`` (remotely blocked).
        """
        if not isinstance(locked, bool) and (
            not isinstance(locked, int) or locked not in (0, 1, 2)
        ):
            msg = f"locked must be a bool or 0/1/2, got: {locked!r}"
            raise ValueError(msg)
        return await self._post(
            node_path(dev_id, node, "status"),
            {"locked": locked},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_easy(
        self, dev_id: str, node: NodeRef, enabled: bool
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"easy": bool}``."""
        _require_bool(enabled, "easy")
        return await self._post(
            node_path(dev_id, node, "status"),
            {"easy": enabled},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_runback(
        self, dev_id: str, node: NodeRef, enabled: bool
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"runback": bool}``."""
        _require_bool(enabled, "runback")
        return await self._post(
            node_path(dev_id, node, "status"),
            {"runback": enabled},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_boost(
        self, dev_id: str, node: NodeRef, on: bool
    ) -> tuple[str, dict[str, Any]]:
        """POST status body ``{"boost": bool}``.

        Accepted but ignored on boost-disabled hardware.
        """
        _require_bool(on, "on")
        return await self._post(
            node_path(dev_id, node, "status"),
            {"boost": on},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_boost_legacy(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        on: bool,
        temperature: float | None = None,
        time_minutes: int | None = None,
        units: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """POST the storage ``/boost`` body.

        Activation carries ``{boost, boost_time, stemp, units}``; deactivation
        is ``{"boost": false}`` alone. This is the Helki storage path, which is
        independent of ``boost_config`` (docs/api-ws-user.md §2.3).
        """
        _require_bool(on, "on")
        if not on:
            if temperature is not None or time_minutes is not None or units is not None:
                msg = "an inactive legacy boost takes only boost=false"
                raise ValueError(msg)
            body: dict[str, object] = {"boost": False}
        else:
            if temperature is None or time_minutes is None or units is None:
                msg = "an active legacy boost needs temperature, time_minutes and units"
                raise ValueError(msg)
            require_number(temperature, "temperature")
            _require_int(time_minutes, "time_minutes")
            validate_units(units)
            body = {
                "boost": True,
                "boost_time": time_minutes,
                "stemp": f"{temperature:.1f}",
                "units": units,
            }
        return await self._post(
            node_path(dev_id, node, "boost"),
            body,
            dev_id=dev_id,
            node=node,
            slot="boost",
        )

    async def set_target_charge(
        self, dev_id: str, node: NodeRef, value: int
    ) -> tuple[str, dict[str, Any]]:
        """POST status ``{"target_charge_per": 0|100}``."""
        _require_int(value, "target_charge_per")
        if value not in (0, 100):
            msg = f"target_charge_per must be 0 or 100, got: {value!r}"
            raise ValueError(msg)
        return await self._post(
            node_path(dev_id, node, "status"),
            {"target_charge_per": value},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_storage_mode(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        mode: str,
        stemp: float | None = None,
        units: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """POST the storage ``/mode`` body ``{mode, stemp?, units?}``."""
        if mode not in _VALID_MODES:
            msg = f"mode must be one of {_VALID_MODES}, got: {mode!r}"
            raise ValueError(msg)
        body: dict[str, object] = {"mode": mode}
        if stemp is not None:
            require_number(stemp, "stemp")
            body["stemp"] = f"{stemp:.1f}"
        if units is not None:
            validate_units(units)
            body["units"] = units
        return await self._post(
            node_path(dev_id, node, "mode"),
            body,
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_lock_endpoint(
        self, dev_id: str, node: NodeRef, locked: bool | int
    ) -> tuple[str, dict[str, Any]]:
        """POST the storage ``/lock`` body ``{"locked": 0|1|2}``."""
        if (
            isinstance(locked, bool)
            or not isinstance(locked, int)
            or locked not in (0, 1, 2)
        ):
            msg = f"locked must be 0/1/2, got: {locked!r}"
            raise ValueError(msg)
        return await self._post(
            node_path(dev_id, node, "lock"),
            {"locked": locked},
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_thermostat_prog(
        self,
        dev_id: str,
        node: NodeRef,
        days: Mapping[str, Sequence[Mapping[str, object]]],
        units: str,
    ) -> tuple[str, dict[str, Any]]:
        """POST the thermostat ``{prog, units}`` body.

        Thermostat days are lists of ``{m, t}`` objects: ``m`` is the
        minute-of-day ending the segment, ``t`` the temperature in the node's
        unit. The app formats ``t`` with ``toFixed(1)``.
        """
        validate_units(units)
        if not isinstance(days, Mapping):
            msg = f"thermostat prog must be a day mapping, got: {bounded_repr(days)}"
            raise ValueError(msg)  # noqa: TRY004 - input validation contract
        unknown = set(days) - _THERMOSTAT_DAY_KEYS
        if unknown:
            msg = f"unknown thermostat day keys: {sorted(unknown, key=repr)}"
            raise ValueError(msg)
        missing = _THERMOSTAT_DAY_KEYS - set(days)
        if missing:
            msg = (
                f"thermostat prog must carry all seven days; missing: {sorted(missing)}"
            )
            raise ValueError(msg)
        normalized: dict[str, list[dict[str, object]]] = {}
        for day, segments in days.items():
            if not isinstance(segments, (list, tuple)):
                msg = f"thermostat day {day!r} must be a list, got: {bounded_repr(segments)}"
                raise ValueError(msg)  # noqa: TRY004 - input validation contract
            out: list[dict[str, object]] = []
            for segment in segments:
                if not isinstance(segment, Mapping):
                    msg = f"thermostat segment must be a mapping, got: {bounded_repr(segment)}"
                    raise ValueError(msg)  # noqa: TRY004 - input validation contract
                minute = segment.get("m")
                temp = segment.get("t")
                if isinstance(minute, bool) or not isinstance(minute, int):
                    msg = f"thermostat segment m must be an int, got: {bounded_repr(minute)}"
                    raise ValueError(msg)  # noqa: TRY004 - input validation contract
                if not 0 <= minute <= _MINUTES_PER_DAY:
                    msg = f"thermostat segment m must be 0..1440, got: {minute!r}"
                    raise ValueError(msg)
                if isinstance(temp, bool):
                    msg = f"thermostat segment t must be a number, got: {bounded_repr(temp)}"
                    raise ValueError(msg)  # noqa: TRY004 - input validation contract
                try:
                    temp_value = float(str(temp))
                except TypeError, ValueError:
                    msg = f"thermostat segment t must be a number, got: {bounded_repr(temp)}"
                    raise ValueError(msg) from None
                if not math.isfinite(temp_value):
                    msg = f"thermostat segment t must be finite, got: {bounded_repr(temp)}"
                    raise ValueError(msg)
                out.append({"m": minute, "t": f"{temp_value:.1f}"})
            normalized[day] = out
        return await self._post(
            node_path(dev_id, node, "prog"),
            {"prog": normalized, "units": units},
            dev_id=dev_id,
            node=node,
            slot="prog",
        )

    async def set_charging_conf(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        active_days: list[int] | None = None,
        slot_1: dict[str, int] | None = None,
        slot_2: dict[str, int] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Merge and POST the storage ``charging_conf`` object."""
        if active_days is None and slot_1 is None and slot_2 is None:
            msg = "at least one of active_days, slot_1 or slot_2 is required"
            raise ValueError(msg)
        validated: dict[str, object] = {}
        if active_days is not None:
            if len(active_days) != _DAYS_PER_WEEK:
                msg = f"active_days must have 7 entries, got: {active_days!r}"
                raise ValueError(msg)
            validated["active_days"] = [1 if value else 0 for value in active_days]
        for key, value in (("slot_1", slot_1), ("slot_2", slot_2)):
            if value is None:
                continue
            start = value.get("start")
            end = value.get("end")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
            ):
                msg = f"{key} needs integer start/end, got: {value!r}"
                raise ValueError(msg)  # noqa: TRY004 - input validation contract
            if not 0 <= start <= _MINUTES_PER_DAY or not 0 <= end <= _MINUTES_PER_DAY:
                msg = f"{key} bounds must be 0..1440, got: {value!r}"
                raise ValueError(msg)
            validated[key] = {"start": start, "end": end}

        current = await self._setup_base(dev_id, node)
        existing = current.get("charging_conf")
        conf: dict[str, object] = (
            dict(existing) if isinstance(existing, Mapping) else {}
        )
        conf.update(validated)
        return await self._post(
            node_path(dev_id, node, "setup"),
            self._setup_body(dev_id, node, current, "charging_conf", conf),
            dev_id=dev_id,
            node=node,
            slot="setup",
        )

    async def set_storage_ui_mode(
        self, dev_id: str, node: NodeRef, ui_mode: int
    ) -> tuple[str, dict[str, Any]]:
        """Merge ``factory_options.ui_mode`` and POST the setup."""
        _require_int(ui_mode, "ui_mode")
        if ui_mode not in (0, 1, 2):
            msg = f"ui_mode must be 0/1/2, got: {ui_mode!r}"
            raise ValueError(msg)
        current = await self._setup_base(dev_id, node)
        factory = current.get("factory_options")
        merged = dict(factory) if isinstance(factory, Mapping) else {}
        merged["ui_mode"] = ui_mode
        return await self._post(
            node_path(dev_id, node, "setup"),
            self._setup_body(dev_id, node, current, "factory_options", merged),
            dev_id=dev_id,
            node=node,
            slot="setup",
        )

    async def set_boost_mode(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        active: bool,
        mode: str | None = None,
        temperature: float | None = None,
        time_minutes: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """POST ``{active, mode?, temp?, time?}`` to ``/boost_mode``.

        This is the full-boost path the current vendor app uses when
        ``setup.factory_options.boost_config`` is ``FULL`` (3)
        (docs/api-ws-user.md §2.3). ``mode`` is required on activation and
        must be one of :data:`VALID_BOOST_MODES`; deactivation is
        ``{"active": false}`` alone. ``temperature`` is in the node's own
        display unit and is formatted ``.1f`` like the app's
        ``toFixed(1)``; ``time_minutes`` is the requested duration.
        """
        _require_bool(active, "active")
        body: dict[str, object] = {"active": active}
        if not active:
            if mode is not None or temperature is not None or time_minutes is not None:
                msg = "an inactive boost_mode write takes only active=false"
                raise ValueError(msg)
            return await self._post(
                node_path(dev_id, node, "boost_mode"),
                body,
                dev_id=dev_id,
                node=node,
                slot="boost_mode",
            )
        if mode not in VALID_BOOST_MODES:
            msg = f"boost mode must be one of {VALID_BOOST_MODES}, got: {mode!r}"
            raise ValueError(msg)
        body["mode"] = mode
        if temperature is not None:
            require_number(temperature, "temperature")
            body["temp"] = f"{temperature:.1f}"
        if time_minutes is not None:
            _require_int(time_minutes, "time_minutes")
            if time_minutes < 0:
                msg = f"time_minutes must be >= 0, got: {time_minutes!r}"
                raise ValueError(msg)
            body["time"] = time_minutes
        return await self._post(
            node_path(dev_id, node, "boost_mode"),
            body,
            dev_id=dev_id,
            node=node,
            slot="boost_mode",
        )

    async def set_htr_mod_status(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        on: bool | None = None,
        mode: str | None = None,
        selected_temp: str | None = None,
        comfort_temp: float | None = None,
        eco_offset: str | None = None,
        units: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """POST the htr_mod status body.

        htr_mod nodes carry ``on`` / ``selected_temp`` / ``comfort_temp`` /
        ``eco_offset`` in status (hass-smartbox docs/api-contract.md §7 item 7).
        The current app (2.54.2) does not put ``units`` in this body; it uses
        the scale only to place ``comfort_temp`` on the setpoint grid. The
        shape is app-evidenced, but live acceptance of the exact no-``units``
        body remains open (hass-smartbox docs/api-contract.md §7 item 7).
        A body carrying ``units`` was accepted (201) and ignored.
        """
        if units is not None:
            units = validate_units(units)
        body: dict[str, object] = {
            name: value
            for name, value in (
                ("on", on),
                ("mode", mode),
                ("selected_temp", selected_temp),
                ("comfort_temp", comfort_temp),
                ("eco_offset", eco_offset),
            )
            if value is not None
        }
        if not body:
            msg = "set_htr_mod_status requires at least one field"
            raise ValueError(msg)
        _validate_htr_mod_body(body, units)
        return await self._post(
            node_path(dev_id, node, "status"),
            body,
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    async def set_prog_temps(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        ice_temp: float | None,
        eco_temp: float | None,
        comf_temp: float | None,
        units: str | None,
    ) -> tuple[str, dict[str, Any]]:
        """POST the app body ``{ice_temp, eco_temp, comf_temp, units}``.

        The caller supplies current values for the profiles it does not change.
        ``None`` temps are omitted and at least one is required. Every temp is
        formatted in the status's scale, so an off-grid Celsius value is
        quantized like any other setpoint. The app's 4-key body always carries
        ``units``; a status without a scale is refused rather than posting a
        body the app never sends (docs/webapi-spec.md §4 item 4). The engine
        applies the app's per-profile clamp before calling this primitive.
        """
        provided = [t for t in (ice_temp, eco_temp, comf_temp) if t is not None]
        if not provided:
            msg = "set_prog_temps requires at least one of ice/eco/comf temps"
            raise ValueError(msg)
        if units is None:
            msg = "set_prog_temps requires units for the status body"
            raise ValueError(msg)
        validate_units(units)
        body: dict[str, object] = {}
        for name, temp in (
            ("ice_temp", ice_temp),
            ("eco_temp", eco_temp),
            ("comf_temp", comf_temp),
        ):
            if temp is not None:
                require_number(temp, name)
                body[name] = format_setpoint(temp, units)
        body["units"] = units
        return await self._post(
            node_path(dev_id, node, "status"),
            body,
            dev_id=dev_id,
            node=node,
            slot="status",
        )

    # --- setup ops (GET current setup -> merge one key -> POST complete) ---

    async def set_window_mode(
        self, dev_id: str, node: NodeRef, enabled: bool
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``window_mode_enabled`` (bool)."""
        _require_bool(enabled, "enabled")
        return await self._setup_write(dev_id, node, "window_mode_enabled", enabled)

    async def set_true_radiant(
        self, dev_id: str, node: NodeRef, enabled: bool
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``true_radiant_enabled`` (bool)."""
        _require_bool(enabled, "enabled")
        return await self._setup_write(dev_id, node, "true_radiant_enabled", enabled)

    async def set_away_offset(
        self, dev_id: str, node: NodeRef, offset: float
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``away_offset`` (stringified float).

        The app slider is 0-based, so the offset must be >= 0.
        """
        require_number(offset, "offset")
        if offset < 0:
            msg = f"away offset must be >= 0, got: {offset!r}"
            raise ValueError(msg)
        return await self._setup_write(dev_id, node, "away_offset", str(float(offset)))

    async def set_max_stemp_limit(
        self,
        dev_id: str,
        node: NodeRef,
        limit: float | None,
        units: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``max_stemp_limit``.

        The wire field is CELSIUS regardless of the node's display unit (the
        vendor app converts its node-unit input to Celsius before posting,
        hass-smartbox docs/api-contract.md §5.2/§7 item 2), so a limit given in the node's
        units is converted first. ``None`` or ``0`` writes the ``"0.0"``
        sentinel, which disables the limit. ``units=None`` passes the limit
        through raw (the caller knows the wire scale).
        """
        if isinstance(limit, bool):
            msg = f"limit must be a number or None, got bool: {limit!r}"
            raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract
        if limit is None or limit == 0:
            return await self._setup_write(dev_id, node, "max_stemp_limit", "0.0")
        require_number(limit, "limit")
        if limit < 0:
            msg = f"max_stemp_limit must be >= 0, got: {limit!r}"
            raise ValueError(msg)
        if units is None:
            body = str(limit)
        else:
            validate_units(units)
            celsius = node_units_to_celsius(limit, units)
            if celsius < 0:
                msg = (
                    "max_stemp_limit converts to a negative Celsius value: "
                    f"{limit!r} {units}"
                )
                raise ValueError(msg)
            body = format_setpoint(celsius, "C")
            if body == "0.0":
                msg = (
                    "max_stemp_limit rounds to the disabled sentinel 0.0: "
                    f"{limit!r} {units}"
                )
                raise ValueError(msg)
        return await self._setup_write(dev_id, node, "max_stemp_limit", body)

    async def set_setup_value(
        self, dev_id: str, node: NodeRef, key: str, value: object
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST one setup key.

        Named setters above are preferred; this is the CLI/debug escape hatch
        for keys the integration does not model yet.
        """
        return await self._setup_write(dev_id, node, key, value)

    async def set_priority(
        self, dev_id: str, node: NodeRef, priority: str
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``priority`` (low/medium/high)."""
        if priority not in VALID_PRIORITIES:
            msg = f"priority must be one of {VALID_PRIORITIES}, got: {priority!r}"
            raise ValueError(msg)
        return await self._setup_write(dev_id, node, "priority", priority)

    async def set_control_mode(
        self, dev_id: str, node: NodeRef, control_mode: int
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``control_mode`` (product-specific index).

        The index is the position in the product controller's wire-order
        mode array; the caller maps the vendor-app label to it. IntEnum and
        other int subclasses are accepted; bool is not.
        """
        if (
            isinstance(control_mode, bool)
            or not isinstance(control_mode, int)
            or control_mode < 0
        ):
            msg = f"control_mode must be a non-negative int, got: {control_mode!r}"
            raise ValueError(msg)
        return await self._setup_write(dev_id, node, "control_mode", control_mode)

    async def set_extra_options(
        self,
        dev_id: str,
        node: NodeRef,
        options: Mapping[str, object],
    ) -> tuple[str, dict[str, Any]]:
        """GET-merge-POST setup key ``extra_options`` (FULL object merge).

        The given keys are merged into the existing object, so unknown keys
        survive, and the COMPLETE setup is posted.
        """
        if not isinstance(options, Mapping):
            msg = f"options must be a mapping, got: {options!r}"
            raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract
        current = await self._setup_base(dev_id, node)
        existing = current.get("extra_options")
        if not isinstance(existing, Mapping):
            existing = {}
        merged = {**existing, **options}
        body = self._setup_body(dev_id, node, current, "extra_options", merged)
        return await self._post(
            node_path(dev_id, node, "setup"),
            body,
            dev_id=dev_id,
            node=node,
            slot="setup",
        )

    # --- device ops ---

    async def set_away(self, dev_id: str, away: bool) -> tuple[str, dict[str, Any]]:
        """POST ``devs/<id>/mgr/away_status`` with the app's three keys.

        The current app sends ``enabled: true`` / ``forced: false`` hardcoded
        alongside ``away`` (docs/api-ws-user.md §2.3); a bare ``{away}`` body
        is honored too (live-verified), so the extra keys cost nothing and
        keep the shape the vendor's own client exercises. The server sets
        ``forced: true`` on the write regardless (live 2026-10-18), so the
        pushed frame - not the posted body - is authoritative for that key.
        """
        _require_bool(away, "away")
        return await self._post(
            f"devs/{encode_segment(dev_id)}/mgr/away_status",
            {"away": away, "enabled": True, "forced": False},
            dev_id=dev_id,
            node=None,
            slot="away",
        )

    async def set_power_limit(
        self, dev_id: str, value: int
    ) -> tuple[str, dict[str, Any]]:
        """POST ``devs/<id>/htr_system/power_limit`` body ``{"power_limit": "<int as str>"}``.

        STRINGIFIED, as the vendor app posts ``powerLimit.toFixed(0)``
        (contract §9/E18). ``0`` means no limit; the box endpoint's ``"0"``
        clear is live-verified (2026-10-18), while node-level acceptance
        remains unverified, so the value passes through as-is.
        """
        _require_int(value, "power_limit")
        if value < 0:
            msg = f"power_limit must be >= 0, got: {value!r}"
            raise ValueError(msg)
        return await self._post(
            f"devs/{encode_segment(dev_id)}/htr_system/power_limit",
            {"power_limit": str(value)},
            dev_id=dev_id,
            node=None,
            slot="power_limit",
        )

    # --- prog op ---

    async def set_node_power_limit(
        self, dev_id: str, node: NodeRef, value: int
    ) -> tuple[str, dict[str, Any]]:
        """POST ``devs/<dev>/<type>/<addr>/power_limit`` (stringified body).

        The node's OWN limit, a separate endpoint from the box ``htr_system``
        one: the app posts the same stringified ``toFixed(0)`` form, and ``0``
        means no limit (the shared helper ships "0" for unlimited,
        docs/api-ws-user.md §2.3). Node-level acceptance of "0" is unverified
        (no pmo hardware probed); the op is otherwise app-evidenced.
        """
        _require_int(value, "power_limit")
        if value < 0:
            msg = f"power_limit must be >= 0, got: {value!r}"
            raise ValueError(msg)
        return await self._post(
            node_path(dev_id, node, "power_limit"),
            {"power_limit": str(value)},
            dev_id=dev_id,
            node=node,
            slot="node_power_limit",
        )

    async def set_prog(
        self,
        dev_id: str,
        node: NodeRef,
        days: Mapping[str, Sequence[int]],
    ) -> tuple[str, dict[str, Any]]:
        """Merge the caller's days into the schedule and POST the whole object.

        The base is the caller's own schedule body when it has one, else a REST
        read; either way the posted body is the complete day-keyed object nested
        under ``"prog"`` (string day keys ``"0"``..``"6"``, Sunday..Saturday). A
        value-equal schedule is returned without a POST: the wire pushes no
        confirming frame for it.
        """
        validate_prog_days(days)
        current = await self._prog_base(dev_id, node)
        merged = {**current, **days}
        path = node_path(dev_id, node, "prog")
        if merged == current:
            # A value-equal schedule pushes no frame, so posting it would only
            # arm a confirmation that must time out.
            return path, {"prog": merged}
        return await self._post(
            path,
            {"prog": merged},
            dev_id=dev_id,
            node=node,
            slot="prog",
        )

    # --- plumbing ---

    async def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        dev_id: str,
        node: NodeRef | None,
        slot: WriteSlot,
    ) -> tuple[str, dict[str, Any]]:
        # The client owns the wire plumbing (single attempt, 401 resend);
        # this is the deliberate seam the fake also implements.
        posted = dict(body)
        if self._on_post is not None:
            self._on_post(
                PostedWrite(path=path, body=posted, dev_id=dev_id, node=node, slot=slot)
            )
        await self._client.post(path, posted)
        return path, posted

    def _omit_extra_options(self, dev_id: str, node: NodeRef) -> bool:
        """Return whether this controller's setup POST omits ``extra_options``.

        The vendor app's ``shouldOmitSetupExtraOptions`` list covers the older
        ElectricHeater families; posting the object to them is accepted but
        dropped, and can disturb the rest of the setup. The store's version
        slot carries the pid; a direct CLI write without a store keeps the
        caller's body as-is.
        """
        if self._store is None:
            return False
        version_method = getattr(self._store, "version", None)
        if not callable(version_method):
            return False
        version = version_method(dev_id, node)
        if not isinstance(version, Mapping):
            return False
        pid = version.get("pid")
        if not isinstance(pid, str) or len(pid) < _PID_CODE_END:
            return False
        return pid[2:_PID_CODE_END].upper() in _OMIT_EXTRA_OPTIONS_CODES

    @staticmethod
    def supports_extra_options(pid: str | None) -> bool:
        """Whether this product code accepts an ``extra_options`` object."""
        if not isinstance(pid, str) or len(pid) < _PID_CODE_END:
            return True
        return pid[2:_PID_CODE_END].upper() not in _OMIT_EXTRA_OPTIONS_CODES

    def _setup_body(
        self,
        dev_id: str,
        node: NodeRef,
        current: Mapping[str, Any],
        key: str,
        value: object,
    ) -> dict[str, Any]:
        """Build a full setup body, honouring the legacy extra_options rule."""
        body = {**current, key: value}
        if self._omit_extra_options(dev_id, node):
            body.pop("extra_options", None)
        return body

    async def _setup_write(
        self, dev_id: str, node: NodeRef, key: str, value: object
    ) -> tuple[str, dict[str, Any]]:
        """Merge ONE key over the setup body and POST the COMPLETE object."""
        current = await self._setup_base(dev_id, node)
        return await self._post(
            node_path(dev_id, node, "setup"),
            self._setup_body(dev_id, node, current, key, value),
            dev_id=dev_id,
            node=node,
            slot="setup",
        )

    async def _setup_base(self, dev_id: str, node: NodeRef) -> Mapping[str, Any]:
        """Return the setup body to merge over: the store's, else a REST read.

        A setup POST replaces the whole object, so an empty read is refused:
        the client decodes 204/empty/non-JSON success bodies to ``{}``, and
        posting only the new key from that would wipe the node's setup.
        """
        if self._store is not None:
            stored = self._store.setup(dev_id, node)
            if stored:
                return stored
        current = await self._client.get_node_setup(dev_id, node)
        if not isinstance(current, Mapping) or not current:
            msg = (
                f"node setup read for {dev_id!r}/{node.node_type}/{node.addr} "
                "returned no body; refusing a partial write"
            )
            raise SmartboxError(msg)
        return current

    async def _prog_base(self, dev_id: str, node: NodeRef) -> Mapping[str, Any]:
        """Return the day-keyed schedule to merge over: the store's, else a read.

        An empty day map is a valid empty schedule; a lost marker or any other
        shape is refused rather than merged into the outgoing body.
        """
        if self._store is not None:
            stored = self._store.prog(dev_id, node)
            if stored is not None:
                return stored
        days = prog_days(await self._client.get_node_prog(dev_id, node))
        if days is None:
            msg = (
                f"node schedule read for {dev_id!r}/{node.node_type}/{node.addr} "
                "returned no usable day map; refusing a partial write"
            )
            raise SmartboxError(msg)
        return days


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, got: {value!r}"
        raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract


def _require_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an int, got: {value!r}"
        raise ValueError(msg)  # noqa: TRY004 - input validation is ValueError by contract


def _validate_htr_mod_body(body: dict[str, object], units: str | None) -> None:
    """Field-level validation for the htr_mod status body (mutates quantization)."""
    if "on" in body:
        _require_bool(body["on"], "on")
    if "mode" in body and body["mode"] not in _VALID_MODES:
        msg = f"mode must be one of {_VALID_MODES}, got: {body['mode']!r}"
        raise ValueError(msg)
    for field in ("selected_temp", "eco_offset"):
        if field in body and not isinstance(body[field], str):
            msg = f"{field} must be a string, got: {body[field]!r}"
            raise ValueError(msg)
    if "comfort_temp" in body:
        comfort = require_number(body["comfort_temp"], "comfort_temp")
        if units is None:
            msg = "comfort_temp requires units"
            raise ValueError(msg)
        # Same setpoint grid as every other temperature write: the app sends
        # .1f on the 0.5 C grid, so the value is quantized here rather than
        # carried through as the caller computed it. The scale is not posted.
        body["comfort_temp"] = format_setpoint(comfort, units)
