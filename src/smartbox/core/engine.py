"""Core engine: snapshot ingestion, update merge, change notifications, writes.

``engine.supervise(transport)`` makes the engine the transport's handler,
driving the versioned store in :mod:`smartbox.core.state`. Merges are
synchronous and copy-on-write; the changes one frame produces are awaited in
order, one per changed slot.

Ordering contract - rules a reader must not reorder:

1. Per frame: merge, then run availability/confirmation on the RAW frame (a
   deduped frame still confirms a write), then notify. Node and device changes
   come first and feed changes last, so a frame that ends an outage never
   reports the recovery before the data it carried.
2. Data changes bump a revision: a frame, a snapshot entry, an optimistic write
   merge. Availability is derived state, so an availability flip changes
   ``NodeState.available`` without a bump.
3. Dedup is exact value-equality per slot: a value-equal payload is absorbed,
   every distinct value forwarded. The server does push duplicates.
4. A lost-marked payload is availability evidence, never slot data. It starts
   the Unavailable window and leaves the stored body alone, so an outage cannot
   blank an entity's last known values.
5. Write confirmation and lost windows belong to
   :class:`smartbox.core.deadlines.NodeDeadlines`: at most one pending write
   per node, a lost window that repeats never extend, and a frame that
   confirms only when it carries the keys the write set at those values. A
   frame that was already in flight (still carrying the pre-write value)
   neither confirms the write nor keeps the timeout from running; it still
   merges, and the write's own echo corrects the store moments later.
6. Every node deadline (write confirmation, lost window) is served by ONE
   timer, re-armed by ``_schedule`` on every change, so a superseded or removed
   deadline needs no cancellation.
7. ``stop()`` cancels the timer and refuses new writes; a later ``supervise()``
   starts a fresh run (the feed monitor re-arms).

The wire-shape rules the merges apply are pure functions in
:mod:`smartbox.core.merging`; feed liveness is :mod:`smartbox.core.feed`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import contextlib
from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Any

from ..protocol import timing
from ..protocol.errors import (
    AuthError,
    SmartboxError,
    TransportUnavailableError,
    TransportUnsupportedError,
)
from ..protocol.events import Event, Snapshot, SocketClosed, Update
from ..protocol.paths import NodeRef
from ..protocol.values import (
    BoostConfig,
    bounded_repr,
    is_lost,
    parse_number,
    prog_days,
    same_wire_value,
)
from ..protocol.writes import PostedWrite, WriteApi, WriteHost, WriteSlot
from .deadlines import DueDeadline, NodeDeadline, NodeDeadlines
from .feed import FEED_GRACE_SECONDS, FeedMonitor
from .merging import (
    DEVICE_UPDATE_PATHS,
    MALFORMED,
    PROG_TEMP_KEYS,
    boost_config,
    coerce_power_limit,
    device_slots_delta,
    device_update_value,
    entry_key,
    is_empty_slot,
    max_stemp_limit_node,
    sanitize_prog_temps,
    snapshot_slot_updates,
    valid_power_limit,
    write_intent,
)
from .state import (
    NODE_SLOTS,
    Change,
    DeviceChange,
    DeviceField,
    DeviceRemoved,
    DeviceState,
    NodeAdded,
    NodeChange,
    NodeField,
    NodeRemoved,
    NodeState,
    WorldState,
    node_key,
)

if TYPE_CHECKING:
    from ..protocol.transport import Transport

__all__ = ["ChangeHandler", "SmartboxEngine"]

_LOGGER = logging.getLogger(__name__)

ChangeHandler = Callable[[Change], Awaitable[None]]

_DEVICE_FIELDS: tuple[DeviceField, ...] = (
    "connected",
    "away",
    "power_limit",
    "nodes",
)
_NODE_PATH_LEN = 3

# Wire-timing defaults (const.py re-exports them for the adapter, so the
# window lengths have one source of truth): tests shrink them.
UNAVAILABLE_DELAY = 6.0
WRITE_CONFIRM_TIMEOUT = 6.0
# REST readback lags a completed write: docs/api-contract.md §5.4 records 5 s
# as verified sufficient. The confirmation window (6 s) plus this settle keeps
# the verification GET above that floor; the window is not a settle timer, so
# the two constants move together.
WRITE_VERIFY_SETTLE_SECONDS = 1.0
# The REST client bounds one call (token refresh, retries and a 401 resend
# included) at 30 s. The confirmation is armed before the POST so an echo
# already in flight is caught, so its in-flight hold must outlast that budget;
# the confirmation countdown (which ends in one verification GET) starts when
# the POST returns.
_WRITE_IN_FLIGHT_TIMEOUT = 30.0

# Lost frames schedule the unavailable flip, never fire it instantly;
# ok frames flip immediately. Availability reacts to lost-marked
# status/setup payloads, and a non-lost frame on either slot is
# reachability evidence (hass-smartbox docs/api-contract.md §4.1).
_AVAIL_LOST_SLOTS: tuple[str, ...] = ("status", "setup")

_WRITE_NODE_SLOTS: tuple[NodeField, ...] = ("status", "setup", "prog")

# The one device path whose frame body may be a bare scalar: the box
# power-limit frame is either an object carrying ``power_limit`` or the digit
# string itself (both vendor clients coerce object-or-scalar,
# docs/api-ws-user.md §1.4).
_POWER_LIMIT_PATH: tuple[str, str] = ("htr_system", "power_limit")

# The node slot each posted-write vocabulary entry names; the device-level
# slots ("away", "power_limit") have no node.
_NODE_WRITE_SLOTS: dict[WriteSlot, NodeField] = {
    "status": "status",
    "setup": "setup",
    "prog": "prog",
}


def _node_write_slot(posted: PostedWrite) -> NodeField | None:
    """Return the node slot a posted write names, or ``None`` for device writes."""
    return _NODE_WRITE_SLOTS.get(posted.slot)


def _boost_mode_intent(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Map a posted ``/boost_mode`` body onto the status frame that confirms it.

    The full-boost endpoint is confirmed by the pushed status body: an
    activation carries ``boost: true`` and its ``boost_mode``; deactivation
    carries ``boost: false`` (the app reads both from status).
    """
    if body.get("active") is not True:
        return {"boost": False}
    intent: dict[str, Any] = {"boost": True}
    mode = body.get("mode")
    if isinstance(mode, str):
        intent["boost_mode"] = mode
    return intent


class _EngineSlots:
    """``SlotReader`` over the engine store: the merge base for setup/prog.

    A REST read lags a successful write by several seconds
    (hass-smartbox docs/api-contract.md §5.4), so a write merges over the body the store
    already holds. ``None`` means the slot has no body yet and the op falls
    back to the read.
    """

    def __init__(self, engine: SmartboxEngine) -> None:
        self._engine = engine

    def setup(self, dev_id: str, node: NodeRef) -> Mapping[str, Any] | None:
        """Return the node's last known setup body, or ``None``."""
        state = self._engine.node(dev_id, node_key(node.node_type, node.addr))
        if state is None or not state.setup:
            return None
        return state.setup

    def prog(self, dev_id: str, node: NodeRef) -> Mapping[str, Any] | None:
        """Return the node's last known schedule, day-keyed, or ``None``."""
        state = self._engine.node(dev_id, node_key(node.node_type, node.addr))
        if state is None:
            return None
        return prog_days(state.prog)


# Supervision restart ladder (mirrors the transport's own policy shape).
_SUPERVISE_BACKOFF = 1.0
_SUPERVISE_BACKOFF_MAX = 30.0
# fixed threshold: a relative (backoff * 2) reset never fires for runs
# dying at 30-60 s once the ladder reaches the cap. Public: the adapter's
# in-place re-probe resets its own failure streak on the same threshold.
SUPERVISE_RUN_RESET_SECONDS = 60.0
_STOP_TIMEOUT = 5.0


@dataclass(frozen=True)
class _VerificationTarget:
    """The GET reader and path facts for one timed-out write.

    The revision is captured with the facts so the read can be dropped when a
    frame changed the node while it was in flight: that frame is newer.
    """

    reader: Callable[[str, NodeRef], Awaitable[dict[str, Any]]]
    node: NodeRef
    slot: NodeField
    node_type: str
    addr: str
    base_revision: int


class SmartboxEngine:
    """Drives the versioned store from typed protocol events.

    Multiple subscribers are allowed (appended, notified in order) -
    unlike the transport seam's single handler, core-side consumers
    (availability tracking, entity coordinators) each need the full
    change stream. Handler exceptions are isolated log-and-continue,
    the transport's own dispatch rule.
    """

    def __init__(
        self,
        write_host: WriteHost | None = None,
        *,
        unavailable_delay: float = UNAVAILABLE_DELAY,
        write_confirm_timeout: float = WRITE_CONFIRM_TIMEOUT,
        feed_grace_s: float = FEED_GRACE_SECONDS,
    ) -> None:
        """Start with an empty world and no subscribers.

        ``write_host`` (a :class:`SmartboxClient`, or the test suite's
        ``tests/fake_api.py`` fake) enables the write path; without one the
        write methods raise ``RuntimeError``. The delay knobs default to the
        module constants, so tests can shrink them and ``const.py`` can
        re-export them for the adapter; sleeps and deadlines go through the
        shared timing seam. ``feed_grace_s`` goes to the feed monitor.
        """
        self._world = WorldState()
        self._subscribers: list[ChangeHandler] = []
        self._unavailable_delay = unavailable_delay
        self._write_confirm_timeout = write_confirm_timeout
        self._write_host = write_host
        # Outstanding node deadlines (write confirmations + lost windows);
        # one timer serves both (see _schedule), so no deadline needs its own
        # cancellation.
        self._deadlines = NodeDeadlines(
            write_confirm_timeout=write_confirm_timeout,
            unavailable_delay=unavailable_delay,
        )
        # One lock per (device, node): a setup or prog write posts the WHOLE
        # object, so two in flight at once would each build their body from
        # the same base and the later POST would restore the earlier key.
        self._write_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._timer_task: asyncio.Task[None] | None = None
        # Owned by the timer, but independent of it: a deadline mutation
        # re-times the timer (cancelling it), and these tasks must survive
        # that (see _run_timer).
        self._resolution_tasks: set[asyncio.Task[None]] = set()
        self._supervise_task: asyncio.Task[None] | None = None
        self._stopping = False
        # Feed (websocket) liveness; the monitor notifies through this
        # engine's pump because it runs in its own timer task.
        self._feed = FeedMonitor(self._notify, grace_s=feed_grace_s)
        # Terminal supervision state (surface for the adapter).
        self.unsupported = False
        self.auth_failed = False
        self.terminal_error: AuthError | TransportUnsupportedError | None = None

    # --- state access ---

    @property
    def world(self) -> WorldState:
        """Return the current world (copy-on-write: old references stay valid)."""
        return self._world

    def device(self, dev_id: str) -> DeviceState | None:
        """Return one device's current state, or ``None`` if unknown."""
        return self._world.devices.get(dev_id)

    def node(self, dev_id: str, key: str) -> NodeState | None:
        """Return one node's state (``{node_type}/{addr}``), or ``None``."""
        device_state = self._world.devices.get(dev_id)
        if device_state is None:
            return None
        return device_state.node_states.get(key)

    @property
    def deadlines(self) -> tuple[NodeDeadline, ...]:
        """Every armed node deadline, earliest expiry first.

        A lost entry flips the node Unavailable when it elapses; a write entry
        starts one verification GET. Diagnostics reports it; tests assert on it
        rather than on the engine's bookkeeping.
        """
        return self._deadlines.view()

    @property
    def timer_armed(self) -> bool:
        """Whether the single node timer is waiting on an armed deadline."""
        return (
            self._timer_task is not None
            and not self._timer_task.done()
            and self._deadlines.next_delay() is not None
        )

    # --- subscription ---

    def subscribe(self, handler: ChangeHandler) -> None:
        """Register a change handler (multiple allowed, order kept)."""
        self._subscribers.append(handler)

    # --- event handling ---

    async def handle_event(self, event: Event) -> None:
        """Handle one typed protocol event (the transport's handler)."""
        if self._stopping:
            # A transport task can outlive the engine briefly; teardown is
            # total, so a late event must not re-arm deadlines or the feed.
            return
        if isinstance(event, SocketClosed):
            await self._feed.on_socket_closed(event)
            return
        # Any other event is proof the feed works: clear the grace window
        # silently, or report the recovery. The changes come back here so the
        # engine can append them AFTER the frame's own.
        feed_changes = self._feed.note_frame()
        if isinstance(event, Snapshot):
            changes = self._ingest_snapshot(event.payload)
        elif isinstance(event, Update):
            changes = self._apply_update(event.dev_id, event.path, event.body)
            # Availability windows + write confirmation run on the RAW
            # frame (deduped frames still confirm/cancel).
            changes += self._availability_changes(event.dev_id, event.path, event.body)
        else:  # pragma: no cover - Event is a closed union
            return
        await self._notify([*changes, *feed_changes])

    # --- feed state ---

    @property
    def feed(self) -> FeedMonitor:
        """The feed's liveness state machine (see :mod:`smartbox.core.feed`)."""
        return self._feed

    async def _notify(self, changes: Sequence[Change]) -> None:
        if self._stopping:
            # Teardown: subscribers' entities are being removed, so a late
            # notification would only drive callbacks for gone entities.
            return
        for change in changes:
            for handler in self._subscribers:
                try:
                    await handler(change)
                except Exception:  # a bad handler must not kill the pump
                    _LOGGER.exception("Change subscriber raised; continuing")

    # --- snapshot ingestion ---

    def _ingest_snapshot(self, payload: Sequence[Mapping[str, Any]]) -> list[Change]:
        """Merge a homes/devs snapshot; every slot dedups independently."""
        if not isinstance(payload, list):
            _LOGGER.warning("Snapshot payload is not a list; ignoring")
            return []
        changes: list[Change] = []
        devices = dict(self._world.devices)
        seen_devices: set[str] = set()
        # A snapshot we could not fully read is not authoritative for
        # membership: pruning on it would delete real devices whose home or
        # entry happened to be malformed.
        membership_trusted = True
        for home in payload:
            if not isinstance(home, Mapping):
                _LOGGER.warning("Snapshot home entry is not a dict; skipping")
                membership_trusted = False
                continue
            home_id = home.get("id")
            home_id = home_id if isinstance(home_id, str) else None
            devs = home.get("devs")
            if not isinstance(devs, list):
                _LOGGER.debug("Snapshot home %r carries no devs; skipping", home_id)
                membership_trusted = False
                continue
            for dev in devs:
                if not self._merge_snapshot_dev(
                    dev, home_id, devices, seen_devices, changes
                ):
                    membership_trusted = False
        if membership_trusted:
            changes.extend(self._prune_missing_devices(devices, seen_devices))
        self._world = WorldState(devices=devices)
        return changes

    def _merge_snapshot_dev(
        self,
        dev: object,
        home_id: str | None,
        devices: dict[str, DeviceState],
        seen_devices: set[str],
        changes: list[Change],
    ) -> bool:
        """Merge one snapshot dev entry; False when it cannot be read."""
        if not isinstance(dev, Mapping):
            _LOGGER.warning("Snapshot dev entry is not a dict; skipping")
            return False
        dev_id = dev.get("dev_id")
        if isinstance(dev_id, str) and dev_id:
            # A malformed devData entry must not prune the stored
            # device: membership is only authoritative for entries we
            # could read.
            seen_devices.add(dev_id)
        dev_data = dev.get("devData")
        if (
            not isinstance(dev_id, str)
            or not dev_id
            or not isinstance(dev_data, Mapping)
        ):
            _LOGGER.warning("Snapshot dev entry missing dev_id/devData; ignoring")
            return False
        previous = devices.get(dev_id)
        # An unreadable home id must not clear the stored metadata.
        effective_home = (
            home_id
            if home_id is not None
            else (previous.home_id if previous is not None else None)
        )
        device, new = self._merge_device_snapshot(
            dev_id, effective_home, dev_data, previous
        )
        # Store whenever the merge produced a DIFFERENT object, not
        # only when it produced change events: a home-id change and a
        # same-membership node list are deliberately silent (no event,
        # no revision bump), so gating on the event list would drop
        # them (a device first seen in an update frame would keep
        # home_id None).
        if new or device is not previous:
            devices[dev_id] = device
            changes.extend(new)
        return True

    def _prune_missing_devices(
        self, devices: dict[str, DeviceState], seen_devices: set[str]
    ) -> list[Change]:
        """Drop devices absent from a non-empty snapshot.

        The snapshot is the account's full device list; a device that
        disappeared must not keep rendering its last values as available. An
        EMPTY payload is not evidence of an empty account.
        """
        changes: list[Change] = []
        if not seen_devices:
            return changes
        for dev_id in set(devices) - seen_devices:
            old = devices.pop(dev_id)
            for key in old.node_states:
                self._forget_node(dev_id, key)
                changes.append(NodeRemoved(dev_id, key))
            changes.append(DeviceChange(dev_id, "nodes", old))
            changes.append(DeviceRemoved(dev_id))
        return changes

    def _merge_device_snapshot(
        self,
        dev_id: str,
        home_id: str | None,
        dev_data: Mapping[str, Any],
        old: DeviceState | None,
    ) -> tuple[DeviceState, list[Change]]:
        """Merge one devData payload; returns the device and its changes."""
        device = old if old is not None else DeviceState(dev_id=dev_id)
        updates, kinds = device_slots_delta(device, dev_data)
        node_map, node_changes, membership_changed, additions = (
            self._merge_snapshot_nodes(dev_id, dev_data.get("nodes"), device)
        )
        if node_map == device.node_states:
            node_map = device.node_states
        if membership_changed:
            # node MEMBERSHIP changed; data changes notify per node instead
            kinds.append("nodes")
        home_changed = home_id != device.home_id
        if home_changed:
            updates["home_id"] = home_id
        if not kinds and not home_changed and node_map is device.node_states:
            return device, []
        device = replace(
            device,
            revision=device.revision + (1 if kinds else 0),
            **updates,
            node_states=node_map,
        )
        changes: list[Change] = [
            DeviceChange(dev_id, kind, device)
            for kind in _DEVICE_FIELDS
            if kind in kinds
        ]
        changes.extend(additions)
        changes.extend(node_changes)
        return device, changes

    def _merge_snapshot_nodes(
        self,
        dev_id: str,
        raw_nodes: object,
        device: DeviceState,
    ) -> tuple[dict[str, NodeState], list[Change], bool, list[NodeAdded]]:
        """Merge a snapshot node list.

        Returns the new map, its slot changes, whether membership changed, and
        the nodes this snapshot ADDED. Additions are only reported for a
        fully parsed list: a partially unreadable snapshot is not authoritative
        for membership, so it cannot prove a node is new either.
        """
        node_map = dict(device.node_states)
        if not isinstance(raw_nodes, list):
            return node_map, [], False, []
        valid_keys: set[str] = set()
        entry_names: dict[str, str | None] = {}
        node_changes: list[Change] = []
        malformed = False
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                _LOGGER.warning("Snapshot node entry is not a dict; ignoring")
                malformed = True
                continue
            if is_empty_slot(entry):
                continue
            key = entry_key(entry, dev_id)
            if key is None:
                malformed = True
                continue
            valid_keys.add(key)
            name = entry.get("name")
            entry_names[key] = name if isinstance(name, str) else None
            node_changes.extend(self._merge_node_entry(dev_id, entry, key, node_map))
        if malformed:
            # A list we cannot fully parse is not trustworthy for membership:
            # keep every stored node rather than deleting real ones on a
            # server-side key drift.
            return node_map, node_changes, False, []
        for key in set(node_map) - valid_keys:
            del node_map[key]  # the snapshot's node list is authoritative
            self._forget_node(dev_id, key)
            node_changes.append(NodeRemoved(dev_id, key))
        added = valid_keys - set(device.node_states)
        additions = [
            NodeAdded(dev_id, key, entry_names.get(key)) for key in sorted(added)
        ]
        membership_changed = valid_keys != set(device.node_states)
        return node_map, node_changes, membership_changed, additions

    def _merge_node_entry(
        self,
        dev_id: str,
        entry: Mapping[str, Any],
        key: str,
        node_map: dict[str, NodeState],
    ) -> list[Change]:
        """Merge one snapshot node entry into ``node_map`` (in place).

        Availability comes from the status slot: non-lost -> available,
        lost-marked -> unavailable, absent -> unchanged, so a reconnected node
        recovers from the snapshot alone. The snapshot is the reconnect's
        confirming frame, so a non-lost body for a pending write's own slot
        confirms it, and a lost-marked status while a write is pending follows
        the frame path's suppression rule.
        """
        old = node_map.get(key) or NodeState()
        if key not in node_map:
            node_map[key] = old  # a bare record still means the node exists
        updates = snapshot_slot_updates(old, entry)
        node = old
        changes: list[Change] = []
        if updates:
            node = replace(old, revision=old.revision + 1, **updates)
            node_map[key] = node
            changes = [
                NodeChange(dev_id, key, slot, node)
                for slot in NODE_SLOTS
                if slot in updates
            ]
        return self._snapshot_availability(dev_id, entry, key, node, node_map, changes)

    def _snapshot_availability(
        self,
        dev_id: str,
        entry: Mapping[str, Any],
        key: str,
        node: NodeState,
        node_map: dict[str, NodeState],
        changes: list[Change],
    ) -> list[Change]:
        """Confirm a pending write + derive availability from a snapshot entry."""
        pending_slot = self._deadlines.pending_slot(dev_id, key)
        pending_at_entry = pending_slot is not None
        if pending_slot is not None:
            body = entry.get(pending_slot)
            if (
                isinstance(body, Mapping)
                and not is_lost(body)
                and self._deadlines.confirm_write(dev_id, key, pending_slot, body)
            ):
                # The snapshot carried the written slot with the write's own
                # values - the same evidence the frame path confirms on - so
                # the pending is confirmed here too, or it times out and flips
                # a node that just reported itself alive.
                self._schedule()
                pending_slot = None
        # Node loss is visible on BOTH /status and /setup (§6.3): an offline
        # node's status GET may still serve the last ok body for minutes while
        # its setup already carries the lost marker. Any present non-empty
        # slot is reachability evidence; a lost marker in either one wins.
        evidence = [
            body
            for slot in _AVAIL_LOST_SLOTS
            if isinstance(body := entry.get(slot), Mapping) and body
        ]
        if not evidence:
            return changes
        lost = any(is_lost(body) for body in evidence)
        if lost:
            # A lost marker mid-write is still availability evidence; like the
            # frame path, arm the normal window and let an ok frame cancel it.
            if pending_at_entry:
                if self._deadlines.schedule_lost(dev_id, key):
                    self._schedule()
                return changes
        elif self._deadlines.cancel_lost(dev_id, key):
            self._schedule()
        if node.available != (not lost):
            node = replace(node, available=not lost)
            node_map[key] = node
            changes.append(NodeChange(dev_id, key, "availability", node))
        return changes

    # --- update frames ---

    def _apply_update(
        self,
        dev_id: str,
        path: str,
        body: object,
    ) -> list[Change]:
        if not isinstance(dev_id, str) or not dev_id:
            _LOGGER.warning("Update frame missing dev_id; ignoring")
            return []
        segments = [segment for segment in str(path).split("/") if segment]
        payload: Mapping[str, Any]
        if isinstance(body, Mapping):
            if not body:
                # The vendor clients apply a frame only when path AND body are
                # truthy (docs/api-ws-user.md §1.4): an empty body must not
                # blank a stored slot, and it is not reachability evidence.
                _LOGGER.debug("Update %r carries an empty body; ignoring", path)
                return []
            payload = body
        elif body and (
            tuple(segments) == _POWER_LIMIT_PATH
            or (len(segments) == _NODE_PATH_LEN and segments[2] == "power_limit")
        ):
            # Both power-limit paths accept the digit string as a bare body.
            payload = {"power_limit": body}
        else:
            _LOGGER.warning("Update %r body is not an object; ignoring", path)
            return []
        device = self._world.devices.get(dev_id)
        if device is None:
            # Frames update what the snapshot created; a frame for an unknown
            # dev id is dropped, as both vendor clients do
            # (docs/api-ws-user.md §1.3).
            _LOGGER.debug("Update for unknown device %r; ignoring", dev_id)
            return []
        devices = dict(self._world.devices)
        if len(segments) == _NODE_PATH_LEN and segments[2] == "power_limit":
            merged = self._merge_node_power_limit_update(
                device, segments[0], segments[1], payload
            )
        elif len(segments) == _NODE_PATH_LEN and segments[2] in NODE_SLOTS:
            merged = self._merge_node_update(
                device, segments[0], segments[1], segments[2], payload
            )
        else:
            merged = self._merge_device_update(dev_id, device, segments, payload)
        if merged is None:
            return []
        new_device, changes = merged
        if changes or new_device is not device:
            devices[dev_id] = new_device
            self._world = WorldState(devices=devices)
        return changes

    def _merge_node_update(
        self,
        device: DeviceState,
        node_type: str,
        addr: str,
        slot: NodeField,
        body: Mapping[str, Any],
    ) -> tuple[DeviceState, list[Change]] | None:
        """FULL-body replacement of one node slot; ``None`` when nothing changed.

        A lost-marked body never replaces the slot: it carries the sync marker
        rather than the slot's keys, so storing it would blank the entity's last
        known values while the node is still serving. Availability reads the raw
        frame instead (see :meth:`_availability_changes`).
        """
        if is_lost(body):
            return None
        key = node_key(node_type, addr)
        old = device.node_states.get(key)
        if old is None:
            # The node list is authoritative: a slot frame for a node the box
            # does not report must not rebuild it as a shell. Writes and
            # availability flips already refuse to; /mgr/nodes and the
            # snapshot are the ways a node appears.
            _LOGGER.debug(
                "Update for node %r/%r the node list does not hold; ignoring",
                device.dev_id,
                key,
            )
            return None
        current: Any = getattr(old, slot)
        if current == body:
            return None  # value-equal duplicate absorbed
        slot_update: dict[str, Any] = {slot: dict(body)}
        node = replace(old, revision=old.revision + 1, **slot_update)
        node_map = dict(device.node_states)
        node_map[key] = node
        return replace(device, node_states=node_map), [
            NodeChange(device.dev_id, key, slot, node)
        ]

    def _merge_node_power_limit_update(
        self,
        device: DeviceState,
        node_type: str,
        addr: str,
        payload: Mapping[str, Any],
    ) -> tuple[DeviceState, list[Change]] | None:
        """Patch the node setup's ``power_limit`` from its own frame.

        ``/pmo/{addr}/power_limit`` writes the node's OWN limit, which lives
        in the setup object (docs/api-ws-user.md §1.4, §2.3). The frame may
        carry ``{"power_limit": <str|int>}`` or the bare scalar.
        """
        key = node_key(node_type, addr)
        old = device.node_states.get(key)
        if old is None:
            _LOGGER.debug(
                "Power-limit frame for node %r/%r the node list does not hold; ignoring",
                device.dev_id,
                key,
            )
            return None
        value = valid_power_limit(payload.get("power_limit"))
        if value is None:
            _LOGGER.warning(
                "Power-limit frame for %r/%r carries unusable value %r; ignoring",
                device.dev_id,
                key,
                payload.get("power_limit"),
            )
            return None
        if value == old.setup.get("power_limit"):
            return device, []
        node = replace(
            old, revision=old.revision + 1, setup={**old.setup, "power_limit": value}
        )
        node_map = dict(device.node_states)
        node_map[key] = node
        return replace(device, node_states=node_map), [
            NodeChange(device.dev_id, key, "setup", node)
        ]

    def _merge_device_update(
        self,
        dev_id: str,
        device: DeviceState,
        segments: list[str],
        body: Mapping[str, Any],
    ) -> tuple[DeviceState, list[Change]] | None:
        """Merge one device-level update frame; ``None`` when malformed."""
        if is_lost(body):
            # Lost markers are node-level availability evidence; a device
            # body carrying one is not slot data.
            return None
        if segments == ["mgr", "nodes"]:
            return self._merge_nodes_frame(dev_id, device, body)
        slot = DEVICE_UPDATE_PATHS.get(tuple(segments))
        if slot is None or (
            segments == ["htr_system", "setup"] and "power_limit" not in body
        ):
            if segments == ["htr_system", "setup"]:
                # The frame usually carries the limit (and the write always
                # does); a keyless one is nothing to apply, not malformed.
                _LOGGER.debug("htr_system/setup frame carries no power_limit; ignoring")
            else:
                _LOGGER.debug(
                    "Update path /%r not modeled; ignoring", "/".join(segments)
                )
            return device, []
        value: Any
        if slot == "away":
            # Away frames carry any subset of {away, enabled, forced}: merge
            # over the stored dict, never a wholesale replacement.
            value = {**device.away, **body}
        else:
            value = device_update_value(slot, body)
        if value is MALFORMED:
            _LOGGER.warning("Update %r body unusable; ignoring", "/".join(segments))
            return None
        current: Any = getattr(device, slot)
        if value == current:
            return device, []
        device = replace(device, **{slot: value}, revision=device.revision + 1)
        return device, [DeviceChange(dev_id, slot, device)]

    def _merge_nodes_frame(
        self,
        dev_id: str,
        device: DeviceState,
        body: Mapping[str, Any],
    ) -> tuple[DeviceState, list[Change]] | None:
        """Reconcile the node map against the frame's node list."""
        raw = body.get("nodes")
        if not isinstance(raw, list):
            _LOGGER.warning("Update /mgr/nodes body carries no nodes list; ignoring")
            return None
        node_map = dict(device.node_states)
        keys: set[str] = set()
        additions: dict[str, str | None] = {}
        malformed = False
        for entry in raw:
            if not isinstance(entry, Mapping):
                _LOGGER.warning("/mgr/nodes entry is not a dict; ignoring")
                malformed = True
                continue
            if is_empty_slot(entry):
                continue
            key = entry_key(entry, dev_id)
            if key is None:
                malformed = True
                continue
            keys.add(key)
            if key not in node_map:
                node_map[key] = NodeState()
                name = entry.get("name")
                additions[key] = name if isinstance(name, str) else None
        if keys == set(device.node_states) and not malformed:
            return device, []  # same membership: nothing to reconcile
        added = [
            NodeAdded(dev_id, key, additions.get(key)) for key in sorted(additions)
        ]
        if malformed:
            # Do not delete nodes on a list we could not fully parse; still
            # add any new node the parsed entries named.
            if not added:
                return device, []
            device = replace(device, node_states=node_map, revision=device.revision + 1)
            return device, [DeviceChange(dev_id, "nodes", device), *added]
        for key in set(node_map) - keys:
            del node_map[key]
            self._forget_node(dev_id, key)
        removed = [NodeRemoved(dev_id, key) for key in set(device.node_states) - keys]
        device = replace(device, node_states=node_map, revision=device.revision + 1)
        return device, [DeviceChange(dev_id, "nodes", device), *removed, *added]

    # --- write path ---

    async def set_setpoint(
        self, dev_id: str, node: NodeRef, temp: float, units: str
    ) -> None:
        """Write the plain setpoint (see ``WriteApi.set_setpoint``)."""
        await self._write(
            dev_id, node, lambda api: api.set_setpoint(dev_id, node, temp, units)
        )

    async def engage_modified_auto(
        self,
        dev_id: str,
        node: NodeRef,
        temp: float,
        units: str,
    ) -> None:
        """Write the app-shaped modified_auto override."""
        await self._write(
            dev_id,
            node,
            lambda api: api.engage_modified_auto(dev_id, node, temp, units),
        )

    async def set_mode(self, dev_id: str, node: NodeRef, mode: str) -> None:
        """Write the node mode (see ``WriteApi.set_mode``)."""
        await self._write(dev_id, node, lambda api: api.set_mode(dev_id, node, mode))

    async def set_lock(self, dev_id: str, node: NodeRef, locked: bool) -> None:
        """Write the child lock (see ``WriteApi.set_lock``)."""
        await self._write(dev_id, node, lambda api: api.set_lock(dev_id, node, locked))

    async def set_boost_mode(
        self,
        dev_id: str,
        node: NodeRef,
        *,
        active: bool,
        mode: str | None = None,
        temperature: float | None = None,
        time_minutes: int | None = None,
    ) -> None:
        """Write the full-boost ``/boost_mode`` endpoint (app-shaped body)."""
        await self._write(
            dev_id,
            node,
            lambda api: api.set_boost_mode(
                dev_id,
                node,
                active=active,
                mode=mode,
                temperature=temperature,
                time_minutes=time_minutes,
            ),
        )

    async def set_boost(
        self,
        dev_id: str,
        node: NodeRef,
        on: bool,
        *,
        temperature: float | None = None,
        time_minutes: int | None = None,
    ) -> None:
        """Write boost, choosing the path the unit's ``boost_config`` uses.

        The vendor app treats ``boost_config`` 3 (FULL) as the ``/boost_mode``
        endpoint with ``{active, mode, temp, time}``; 1 and 2 use the status
        flag (2 additionally stores the temp/time in the setup, which the
        boost number entities own). The app is the spec here
        (docs/api-ws-user.md §2.3).
        """
        state = self.node(dev_id, node_key(node.node_type, node.addr))
        config = boost_config(state.setup if state is not None else {})
        if config == BoostConfig.FULL:
            await self.set_boost_mode(
                dev_id,
                node,
                active=on,
                mode="temp_and_time" if on else None,
                temperature=temperature if on else None,
                time_minutes=time_minutes if on else None,
            )
            return
        if (
            config == BoostConfig.SET_TEMP_AND_TIME
            and on
            and (temperature is not None or time_minutes is not None)
        ):
            # The app's legacy activation writes the defaults into the setup
            # (extra_options.boost_temp/boost_time) and then sets the status
            # flag. Both are the node's own display-unit values.
            options: dict[str, object] = {}
            if temperature is not None:
                options["boost_temp"] = f"{temperature}"
            if time_minutes is not None:
                options["boost_time"] = int(time_minutes)
            await self._write(
                dev_id,
                node,
                lambda api: api.set_extra_options(dev_id, node, options),
            )
        await self._write(dev_id, node, lambda api: api.set_boost(dev_id, node, on))

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
    ) -> None:
        """Write the app-shaped htr_mod status body.

        Carries the climate entity's htr_mod writes (hvac modes, presets,
        setpoints); the key set follows the vendor app's htr_mod managers.
        """
        await self._write(
            dev_id,
            node,
            lambda api: api.set_htr_mod_status(
                dev_id,
                node,
                on=on,
                mode=mode,
                selected_temp=selected_temp,
                comfort_temp=comfort_temp,
                eco_offset=eco_offset,
                units=units,
            ),
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
        changed: str | None = None,
    ) -> None:
        """Write the profile temps (see ``WriteApi.set_prog_temps``).

        The vendor app clamps the three profiles to their per-profile bounds
        and to the ice/eco/comfort offset before posting the 4-key status body;
        ``changed`` exempts the profile the user just moved from the neighbour
        adjustment. The 4-key body is a STATUS POST, so its confirmation is a
        status frame and not a prog frame; ``set_prog`` (the schedule itself)
        is the op that posts to the prog path.
        """
        state = self.node(dev_id, node_key(node.node_type, node.addr))
        values = {
            "ice_temp": ice_temp,
            "eco_temp": eco_temp,
            "comf_temp": comf_temp,
        }
        sanitized = sanitize_prog_temps(state, values, changed=changed)
        await self._write(
            dev_id,
            node,
            lambda api: api.set_prog_temps(
                dev_id,
                node,
                ice_temp=sanitized["ice_temp"],
                eco_temp=sanitized["eco_temp"],
                comf_temp=sanitized["comf_temp"],
                units=units or (state.status.get("units") if state else None),
            ),
        )

    async def set_window_mode(self, dev_id: str, node: NodeRef, enabled: bool) -> None:
        """Write the setup window-mode key (GET-merge-POST)."""
        await self._write(
            dev_id, node, lambda api: api.set_window_mode(dev_id, node, enabled)
        )

    async def set_true_radiant(self, dev_id: str, node: NodeRef, enabled: bool) -> None:
        """Write the setup true-radiant key (GET-merge-POST)."""
        await self._write(
            dev_id, node, lambda api: api.set_true_radiant(dev_id, node, enabled)
        )

    async def set_away_offset(self, dev_id: str, node: NodeRef, offset: float) -> None:
        """Write the setup away offset (GET-merge-POST)."""
        await self._write(
            dev_id, node, lambda api: api.set_away_offset(dev_id, node, offset)
        )

    async def set_max_stemp_limit(
        self,
        dev_id: str,
        node: NodeRef,
        limit: float | None,
        units: str | None,
    ) -> None:
        """Write the setup max-setpoint limit (GET-merge-POST).

        The vendor app follows a lowered ceiling with a 4-key profile-temp
        write when an existing profile sits above the new limit; replicate
        that correction so a profile cannot stay above its own ceiling.
        """
        await self._write(
            dev_id,
            node,
            lambda api: api.set_max_stemp_limit(dev_id, node, limit, units),
        )
        await self._correct_prog_temps_after_limit(dev_id, node)

    async def _correct_prog_temps_after_limit(self, dev_id: str, node: NodeRef) -> None:
        """Post clamped profile temps when a new ceiling leaves one above it."""
        state = self.node(dev_id, node_key(node.node_type, node.addr))
        limit = max_stemp_limit_node(state)
        if state is None or limit is None:
            return
        units = state.status.get("units")
        if not isinstance(units, str):
            return
        current: dict[str, float | None] = {}
        for key in PROG_TEMP_KEYS:
            try:
                current[key] = parse_number(state.status.get(key))
            except SmartboxError:
                current[key] = None
        if not any(value is not None and value > limit for value in current.values()):
            return
        await self.set_prog_temps(
            dev_id,
            node,
            ice_temp=current["ice_temp"],
            eco_temp=current["eco_temp"],
            comf_temp=current["comf_temp"],
            units=units,
        )

    async def set_priority(self, dev_id: str, node: NodeRef, priority: str) -> None:
        """Write the setup priority (GET-merge-POST)."""
        await self._write(
            dev_id, node, lambda api: api.set_priority(dev_id, node, priority)
        )

    async def set_extra_options(
        self,
        dev_id: str,
        node: NodeRef,
        options: Mapping[str, object],
    ) -> None:
        """Merge setup extra options (GET-merge-POST, full object)."""
        await self._write(
            dev_id, node, lambda api: api.set_extra_options(dev_id, node, options)
        )

    async def set_node_power_limit(
        self, dev_id: str, node: NodeRef, value: int
    ) -> None:
        """Write the node's own power limit (see ``WriteApi.set_node_power_limit``).

        No confirmation window: the server's echo for this endpoint is
        unverified, so the optimistic setup patch carries the value until a
        frame corrects it.
        """
        await self._write(
            dev_id, node, lambda api: api.set_node_power_limit(dev_id, node, value)
        )

    async def set_prog(
        self, dev_id: str, node: NodeRef, days: Mapping[str, Sequence[int]]
    ) -> None:
        """Write the weekly schedule at day level (GET-merge-POST)."""
        await self._write(dev_id, node, lambda api: api.set_prog(dev_id, node, days))

    async def set_away(self, dev_id: str, away: bool) -> None:
        """Write the device away flag (device-level; no node confirmation)."""
        await self._write(dev_id, None, lambda api: api.set_away(dev_id, away))

    async def set_power_limit(self, dev_id: str, value: int) -> None:
        """Write the device power limit (device-level; no node confirmation)."""
        await self._write(dev_id, None, lambda api: api.set_power_limit(dev_id, value))

    async def _write(
        self,
        dev_id: str,
        node: NodeRef | None,
        op: Callable[[WriteApi], Awaitable[tuple[str, dict[str, Any]]]],
    ) -> None:
        """Arm the confirmation at the POST, merge the posted body, notify.

        The op names the request it sent (path, body, node, slot) on the
        ``PostedWrite`` handed to the arming hook, which fires immediately
        before the POST goes out: the echo the server pushes while the POST is
        in flight still confirms THIS write, and a merge's own read stays
        outside the window.

        Confirmation is per-slot (status write -> status frame, setup ->
        full-setup body, prog -> prog frame). Device-level writes (``node``
        ``None``) arm nothing: lost semantics are per-node, so a device failure
        surfaces through its raise.

        Writes to one node are serialized: a setup or prog write posts the whole
        object, so two of them in flight at once would each build their body
        from the same base and the later POST would restore the earlier key.

        A failure disarms the pending it armed. A write that changed nothing
        arms nothing: the wire pushes no frame for a no-op. A write issued
        while stopping is refused, because its POST would land after teardown.
        """
        if self._stopping:
            msg = "SmartboxEngine is stopping; writes are unavailable"
            raise RuntimeError(msg)
        if self._write_host is None:
            msg = "SmartboxEngine was built without a write host; writes unavailable"
            raise RuntimeError(msg)
        # This write's own arming, as ``(node key, generation)``: the
        # generation lets it prove later that the pending it disarms is its own.
        armed: tuple[str, int] | None = None
        posted: PostedWrite | None = None
        # The store revision the POST was built from. A frame that lands
        # while the request is in flight is authoritative; the optimistic
        # merge must not write the older client body back over it.
        base_revision: int | None = None

        def _on_post(sent: PostedWrite) -> None:
            nonlocal armed, posted, base_revision
            posted = sent
            base_revision = self._revision_for_post(dev_id, sent)
            armed = self._arm_for_post(dev_id, sent)

        # A fresh WriteApi per call: the arming hook above is per-call state,
        # and the store is the merge base a setup or prog write re-posts.
        api = WriteApi(self._write_host, on_post=_on_post, store=_EngineSlots(self))
        async with self._write_lock(dev_id, node):
            try:
                await op(api)
            except Exception, asyncio.CancelledError:
                if armed is not None:
                    self._disarm_own_write(dev_id, *armed)  # nothing stays armed
                raise
            if armed is not None:
                # The POST landed; start the 6 s confirmation countdown now.
                # The arm before the POST held a longer window, so a token
                # refresh or a slow request cannot start verification while
                # the write is still in flight.
                self._refresh_own_write(dev_id, *armed)
            await self._merge_posted_write(posted, armed, base_revision)

    def _arm_for_post(self, dev_id: str, posted: PostedWrite) -> tuple[str, int] | None:
        """Arm this write's confirmation as its POST goes out.

        Nothing is armed for a device-level write (lost semantics are
        per-node), for a node the store does not hold (nothing to confirm and
        nothing to merge into), or for a write that changed nothing (the wire
        pushes no frame for a no-op, so its window would only end in a
        pointless verification read). Raises while stopping: the POST would
        land after teardown.
        """
        if self._stopping:
            msg = "SmartboxEngine is stopping; writes are unavailable"
            raise RuntimeError(msg)
        target_slot = _node_write_slot(posted)
        if posted.node is None:
            return None
        target_key = node_key(posted.node.node_type, posted.node.addr)
        current = self.node(dev_id, target_key)
        if current is None:
            return None
        slot: NodeField
        if posted.slot == "boost_mode":
            # The confirming frame is a status frame carrying boost/boost_mode.
            slot = "status"
            changed = _boost_mode_intent(posted.body)
        elif target_slot is None:
            return None
        else:
            slot = target_slot
            changed = write_intent(slot, current, posted.body)
        if not changed:
            return None
        # Status/setup writes carry exactly the keys they changed; a prog echo
        # carries the whole week but only the changed days are evidence; a
        # boost-mode write is confirmed by the status boost/boost_mode keys.
        return (
            target_key,
            self._arm_write_confirm(
                dev_id,
                target_key,
                slot,
                changed,
                body=posted.body,
                timeout=self._write_confirm_timeout + _WRITE_IN_FLIGHT_TIMEOUT,
            ),
        )

    def _write_lock(self, dev_id: str, node: NodeRef | None) -> asyncio.Lock:
        """Return the lock that serializes writes to one node (or device)."""
        key = "" if node is None else node_key(node.node_type, node.addr)
        return self._write_locks.setdefault((dev_id, key), asyncio.Lock())

    def _revision_for_post(self, dev_id: str, posted: PostedWrite) -> int | None:
        """Return the store revision the posted body was merged over."""
        if posted.node is None:
            device = self.device(dev_id)
            return None if device is None else device.revision
        node = self.node(dev_id, node_key(posted.node.node_type, posted.node.addr))
        return None if node is None else node.revision

    def _revision_is_unchanged(
        self, dev_id: str, key: str | None, base_revision: int
    ) -> bool:
        """Whether the slot the write touches still has the POST's revision."""
        if key is None:
            device = self.device(dev_id)
            return device is not None and device.revision == base_revision
        node = self.node(dev_id, key)
        return node is not None and node.revision == base_revision

    async def _merge_posted_write(
        self,
        posted: PostedWrite | None,
        armed: tuple[str, int] | None,
        base_revision: int | None,
    ) -> None:
        """Merge a completed POST into the store, then notify its changes.

        The store target comes from the write's own posted facts, never from
        parsing the REST path back out of the request. ``posted`` is ``None``
        only for a host that posts outside the WriteApi seam, which has nothing
        to merge.

        ``base_revision`` is the revision the body was built from. A frame
        that changed the same slot while the POST was in flight wins: its
        value is newer than the body the client sent, so merging the body now
        would revert authoritative data (the away ``forced`` flag is the
        concrete case).
        """
        if posted is None:
            return
        dev_id = posted.dev_id
        key = (
            None
            if posted.node is None
            else node_key(posted.node.node_type, posted.node.addr)
        )
        if key is not None and self.node(dev_id, key) is None:
            # The node list is authoritative: the node was removed before or
            # during the POST, so merging now would rebuild it as a shell.
            if armed is not None:
                self._disarm_own_write(dev_id, *armed)
            _LOGGER.debug(
                "Write to %r/%r completed after the node was removed; not re-adding it",
                dev_id,
                key,
            )
            return
        if base_revision is not None and not self._revision_is_unchanged(
            dev_id, key, base_revision
        ):
            _LOGGER.debug(
                "Write to %r/%r completed after a frame changed the slot; "
                "keeping the frame's value",
                dev_id,
                key,
            )
            return
        if key is None:
            changes = self._optimistic_device_merge(dev_id, posted.slot, posted.body)
        elif posted.slot == "node_power_limit":
            changes = self._optimistic_node_power_limit(dev_id, key, posted.body)
        elif posted.slot == "boost_mode":
            changes = self._optimistic_boost_mode_merge(dev_id, key, posted.body)
        else:
            slot = _node_write_slot(posted)
            if slot is None:  # pragma: no cover - WriteSlot is a closed union
                _LOGGER.warning(
                    "Optimistic merge: unmodeled write slot %r; skipping",
                    posted.slot,
                )
                return
            changes = self._optimistic_node_merge(dev_id, key, slot, posted.body)
        if not changes and armed is not None:
            # Nothing changed: the wire pushes no frame for a value-equal
            # write, so the pending could only ever time out and flip a
            # healthy node Unavailable.
            self._disarm_own_write(dev_id, *armed)
        await self._notify(changes)

    # --- copy-on-write store writes ---

    def _store_node(self, dev_id: str, key: str, node: NodeState) -> NodeState:
        """Write one replaced node into the copy-on-write world.

        Returns ``node`` so the caller can put it on the change event. The
        caller has already checked that the device and the node exist.
        """
        device = self._world.devices[dev_id]
        node_map = dict(device.node_states)
        node_map[key] = node
        devices = dict(self._world.devices)
        devices[dev_id] = replace(device, node_states=node_map)
        self._world = WorldState(devices=devices)
        return node

    def _store_device(self, dev_id: str, device: DeviceState) -> DeviceState:
        """Write one replaced device into the copy-on-write world."""
        devices = dict(self._world.devices)
        devices[dev_id] = device
        self._world = WorldState(devices=devices)
        return device

    def _optimistic_device_merge(
        self, dev_id: str, slot: WriteSlot, body: Mapping[str, Any]
    ) -> list[Change]:
        """Merge a posted device-level body into its device slot."""
        if slot == "away":
            return self._optimistic_away_merge(dev_id, body)
        if slot == "power_limit":
            return self._optimistic_power_limit(dev_id, body)
        _LOGGER.warning("Optimistic merge: unmodeled write slot %r; skipping", slot)
        return []  # pragma: no cover - WriteSlot is a closed union

    def _optimistic_node_merge(
        self,
        dev_id: str,
        key: str,
        slot: NodeField,
        body: Mapping[str, Any],
    ) -> list[Change]:
        """Optimistically merge one node slot; the node must already exist.

        status is a KEY MERGE over the stored dict - unlike the frame path's
        full-body replacement, only the written keys exist here yet. setup and
        prog store the complete posted object.

        A write never creates a node: the node list is authoritative, so a write
        that outlives its node's removal must not put the node back.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        old = device.node_states.get(key)
        if old is None:
            return []
        current: Any = getattr(old, slot)
        if slot == "status":
            base = current if isinstance(current, Mapping) else {}
            value: Any = {**base, **body}
        else:
            value = dict(body)
        if value == current:
            return []
        node = self._store_node(
            dev_id, key, replace(old, revision=old.revision + 1, **{slot: value})
        )
        return [NodeChange(dev_id, key, slot, node)]

    def _optimistic_node_power_limit(
        self, dev_id: str, key: str, body: Mapping[str, Any]
    ) -> list[Change]:
        """Patch the node setup's ``power_limit`` from a posted limit write.

        The dedicated endpoint posts only that one key, so the optimistic merge
        patches the stored setup dict instead of replacing it; a later setup
        frame (or the node's own ``power_limit`` frame) corrects the value if
        the server disagrees. The write arms NO confirmation: the echo shape is
        unverified (no pmo hardware probed), and a wrong slot guess would merge
        the wrong slot and log a false mismatch.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        old = device.node_states.get(key)
        if old is None:
            return []
        raw = body.get("power_limit")
        if raw is None:
            return []
        value = coerce_power_limit(raw)
        if isinstance(value, bool) or not isinstance(value, int):
            _LOGGER.warning("Optimistic merge: unusable power_limit %r; skipping", raw)
            return []
        if value == old.setup.get("power_limit"):
            return []
        node = self._store_node(
            dev_id,
            key,
            replace(
                old,
                revision=old.revision + 1,
                setup={**old.setup, "power_limit": value},
            ),
        )
        return [NodeChange(dev_id, key, "setup", node)]

    def _optimistic_boost_mode_merge(
        self, dev_id: str, key: str, body: Mapping[str, Any]
    ) -> list[Change]:
        """Patch the status slot from a posted ``/boost_mode`` body.

        The endpoint's body uses ``active``/``mode``/``temp``; the store's
        status slot uses ``boost``/``boost_mode``/``stemp`` (the app reads
        those). ``boost_end_min`` is server-computed and left for the frame.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        old = device.node_states.get(key)
        if old is None:
            return []
        active = body.get("active")
        if not isinstance(active, bool):
            return []
        status = dict(old.status)
        status["boost"] = active
        if active:
            mode = body.get("mode")
            if isinstance(mode, str):
                status["boost_mode"] = mode
            temp = body.get("temp")
            if isinstance(temp, str):
                status["stemp"] = temp
        if status == old.status:
            return []
        node = self._store_node(
            dev_id, key, replace(old, revision=old.revision + 1, status=status)
        )
        return [NodeChange(dev_id, key, "status", node)]

    def _optimistic_away_merge(
        self, dev_id: str, body: Mapping[str, Any]
    ) -> list[Change]:
        """Merge the posted away body into the stored away dict.

        ``forced`` is server-owned (the pushed frame carries the real value,
        docs/api-ws-user.md §2.3); the posted ``false`` must not overwrite it.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        posted = {key: value for key, value in body.items() if key != "forced"}
        value = {**device.away, **posted}
        if value == device.away:
            return []
        device = self._store_device(
            dev_id,
            replace(device, away=value, revision=device.revision + 1),
        )
        return [DeviceChange(dev_id, "away", device)]

    def _optimistic_power_limit(
        self, dev_id: str, body: Mapping[str, Any]
    ) -> list[Change]:
        """Set the power_limit slot to the posted value.

        The posted body is the stringified wire form (E18); the slot keeps the
        numeric value (E17) so the number entity renders a number.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        value = valid_power_limit(body.get("power_limit"))
        if value is None:
            _LOGGER.warning(
                "Optimistic merge: unusable power_limit %r; skipping",
                body.get("power_limit"),
            )
            return []
        if value == device.power_limit:
            return []
        device = self._store_device(
            dev_id,
            replace(device, power_limit=value, revision=device.revision + 1),
        )
        return [DeviceChange(dev_id, "power_limit", device)]

    # --- pending-write confirmation ---

    def _arm_write_confirm(
        self,
        dev_id: str,
        key: str,
        slot: NodeField,
        intent: Mapping[str, Any] | None,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> int:
        """(Re)arm the node's pending write (see ``NodeDeadlines.arm_write``)."""
        generation = self._deadlines.arm_write(
            dev_id, key, slot, intent, body=body, timeout=timeout
        )
        self._schedule()
        return generation

    def _refresh_own_write(self, dev_id: str, key: str, generation: int) -> None:
        """Restart the confirmation countdown when this write is still pending."""
        if self._deadlines.refresh_write(dev_id, key, generation):
            self._schedule()

    def _disarm_own_write(self, dev_id: str, key: str, generation: int) -> None:
        """Disarm this write's pending, when it is still the armed one."""
        if self._deadlines.disarm_write(dev_id, key, generation):
            self._schedule()

    # --- the single node timer ---

    def _schedule(self) -> None:
        """(Re)arm the one timer for the earliest outstanding deadline.

        Every deadline mutation goes through here, so cancellation is implicit:
        a superseded or disarmed entry is simply no longer in the maps the next
        delay is computed from.
        """
        if self._stopping:
            return
        current = asyncio.current_task()
        if self._timer_task is not None and self._timer_task is not current:
            self._timer_task.cancel()
            self._timer_task = None
        delay = self._deadlines.next_delay()
        if delay is not None and self._timer_task is None:
            self._timer_task = asyncio.create_task(self._run_timer(delay))

    async def _run_timer(self, delay: float) -> None:
        """Wait out the earliest deadline, then resolve every due deadline.

        Lost flips are applied here, before this task yields: a frame arriving
        afterwards is newer and wins. A write timeout is verified by its own
        task, so its GET neither blocks the timer nor the other deadlines
        taken with it.
        """
        try:
            await timing.sleep(delay)
            changes: list[Change] = []
            for due in self._deadlines.take_due():
                if due.kind == "lost":
                    changes.extend(
                        self._set_available(
                            due.dev_id, due.node_key, value=False, reason=due.reason
                        )
                    )
                else:
                    self._spawn_verification(due)
            await self._notify(changes)
        except asyncio.CancelledError:
            return
        finally:
            if self._timer_task is asyncio.current_task():
                self._timer_task = None
            self._schedule()

    def _spawn_verification(self, due: DueDeadline) -> None:
        """Verify one timed-out write in its own task."""
        task = asyncio.create_task(self._verify_and_notify(due))
        self._resolution_tasks.add(task)
        task.add_done_callback(self._resolution_tasks.discard)

    async def _verify_and_notify(self, due: DueDeadline) -> None:
        """Verify one timed-out write, then deliver its changes."""
        try:
            changes = await self._verify_write_timeout(due)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "Write verification failed for %r/%r", due.dev_id, due.node_key
            )
            return
        await self._notify(changes)

    def _verification_target(self, due: DueDeadline) -> _VerificationTarget | None:
        """Return the GET reader and path facts for a timed-out write."""
        if self._write_host is None or self._stopping:
            return (
                None  # pragma: no cover - writes require a host; stop clears deadlines
            )
        dev_id, key = due.dev_id, due.node_key
        device = self._world.devices.get(dev_id)
        if device is None or key not in device.node_states:
            return None  # pragma: no cover - node removal between spawn and here
        slot = due.slot or "status"
        base_revision = device.node_states[key].revision
        node_type, addr = key.split("/", 1)
        node = NodeRef(node_type, addr)
        if slot == "setup":
            reader = self._write_host.get_node_setup
        elif slot == "prog":
            reader = self._write_host.get_node_prog
        else:
            reader = self._write_host.get_node_status
        return _VerificationTarget(reader, node, slot, node_type, addr, base_revision)

    async def _verify_write_timeout(self, due: DueDeadline) -> list[Change]:
        """Verify one timed-out write with a GET of its own slot.

        The response is accepted at face value: it is the server's state for the
        slot, merged like any other update. The one ordering check is causality:
        a frame that changed the node while the read was in flight is newer than
        the read, so the read is dropped instead of merged over it.
        """
        target = self._verification_target(due)
        if target is None:
            return []

        dev_id, key = due.dev_id, due.node_key
        await timing.sleep(WRITE_VERIFY_SETTLE_SECONDS)
        try:
            body = await target.reader(dev_id, target.node)
        except Exception as err:  # noqa: BLE001 - an optional check must not propagate
            # API trouble is not node death and not write evidence.
            _LOGGER.info(
                "Write confirmation check for %r/%r failed: %r", dev_id, key, err
            )
            return []
        if (
            self._stopping
            or self.node(dev_id, key) is None
            or not self._revision_is_unchanged(dev_id, key, target.base_revision)
        ):
            # Teardown, the node was removed while reading (applying the body
            # would resurrect it), or a frame landed and is newer than this
            # read: in each case the read is not applied.
            return []
        if is_lost(body):
            self._deadlines.cancel_lost(dev_id, key)
            return self._set_available(
                dev_id,
                key,
                value=False,
                reason="write verification returned a lost node",
            )
        usable = self._verification_body(body, target.slot, dev_id, key)
        if usable is None:
            return []
        changes = self._apply_update(
            dev_id, f"/{target.node_type}/{target.addr}/{target.slot}", usable
        )
        if not self._write_applied(due.intent, target.slot, body):
            _LOGGER.warning(
                "Write to %r/%r was accepted but not applied; posted=%s read=%s",
                dev_id,
                key,
                bounded_repr(due.body),
                bounded_repr(body),
            )
        return changes

    @staticmethod
    def _verification_body(
        body: object, slot: NodeField, dev_id: str, key: str
    ) -> Mapping[str, Any] | None:
        """Return a usable verification body, or ``None`` for no evidence."""
        if not isinstance(body, Mapping) or not body:
            return None
        if slot == "prog" and prog_days(body) is None:
            _LOGGER.warning(
                "Write confirmation check for %r/%r returned malformed prog data",
                dev_id,
                key,
            )
            return None
        return body

    @staticmethod
    def _write_applied(
        intent: Mapping[str, Any] | None,
        slot: NodeField | None,
        body: Mapping[str, Any],
    ) -> bool:
        """Whether a verification GET carries every value the write set."""
        if intent is None:  # pragma: no cover - write timeouts always carry intent
            return True
        evidence: Mapping[str, Any] = body
        if slot == "prog":
            days = prog_days(body)
            if days is None:  # pragma: no cover - rejected by _verification_body
                return False
            evidence = days
        return all(
            key in evidence and same_wire_value(evidence[key], value)
            for key, value in intent.items()
        )

    # --- availability windows ---

    def _availability_changes(
        self, dev_id: str, path: str, body: object
    ) -> list[Change]:
        """Availability + confirmation logic on the raw node frame.

        ``path`` is a ws update path (``/<type>/<addr>/<slot>``). A lost-marked
        status/setup frame
        schedules the unavailable flip; a non-lost frame on either of those
        slots cancels it and reports the recovery. A non-lost frame that
        carries a pending write's own keys confirms it and re-marks the node
        available. Version frames do neither. Snapshot ingestion derives
        availability from the status and setup slots (see
        :meth:`_snapshot_availability`).
        """
        segments = [segment for segment in str(path).split("/") if segment]
        if (
            len(segments) != _NODE_PATH_LEN
            or segments[2] not in _WRITE_NODE_SLOTS
            or not isinstance(body, Mapping)
            or not body
        ):  # version frames confirm nothing; empty and ill-typed bodies are
            # not evidence of anything (docs/api-ws-user.md §1.4)
            return []
        key = node_key(segments[0], segments[1])
        device = self._world.devices.get(dev_id)
        if device is None or key not in device.node_states:
            # Frames for unknown devices/nodes are not evidence: the engine
            # already drops their data, so they must not arm deadlines either.
            return []
        slot = segments[2]
        if is_lost(body):
            # A lost frame owns availability even while a write is pending; an
            # ok frame cancels the window. The write timeout separately
            # verifies the write with a GET.
            if slot in _AVAIL_LOST_SLOTS and self._deadlines.schedule_lost(dev_id, key):
                self._schedule()
            return []
        evidence: Mapping[str, Any] | None = body
        if slot == "prog":
            # A prog frame wraps the week under ``prog`` (or carries a bare
            # day map); compare the day evidence, not the wrapper.
            evidence = prog_days(body)
        if evidence is not None and self._deadlines.confirm_write(
            dev_id, key, slot, evidence
        ):
            # The confirming frame also clears a co-armed lost window. Cancel
            # it before the single re-arm so the timer sees the final maps.
            self._deadlines.cancel_lost(dev_id, key)
            self._schedule()
            return self._set_available(
                dev_id,
                key,
                value=True,
                reason=f"{slot} frame confirmed the pending write",
            )
        if slot in _AVAIL_LOST_SLOTS:
            # An ok status or setup frame is reachability evidence: it
            # cancels a scheduled flip and reports the recovery
            # (hass-smartbox docs/api-contract.md §4.1).
            if self._deadlines.cancel_lost(dev_id, key):
                self._schedule()
            return self._set_available(
                dev_id, key, value=True, reason=f"non-lost {slot} frame"
            )
        return []

    def _forget_node(self, dev_id: str, key: str) -> None:
        """Drop a removed node's deadlines.

        The node list is authoritative, so a window armed before the removal
        must not fire afterwards: it would rebuild the node as a shell the box
        no longer reports.
        """
        if self._deadlines.forget(dev_id, key):
            self._schedule()

    def _set_available(
        self, dev_id: str, key: str, *, value: bool, reason: str
    ) -> list[Change]:
        """Flip node availability (no revision bump: derived state).

        A node the store does not hold is left alone: a deadline that outlives
        its node's removal must not rebuild it as a shell the box no longer
        reports.
        """
        device = self._world.devices.get(dev_id)
        if device is None:
            return []
        old = device.node_states.get(key)
        if old is None or old.available == value:
            return []
        _LOGGER.debug("Node %r/%r marked %s (%s)", dev_id, key, value, reason)
        node = self._store_node(dev_id, key, replace(old, available=value))
        return [NodeChange(dev_id, key, "availability", node)]

    # --- supervision ---

    def supervise(self, transport: Transport) -> None:
        """Subscribe the engine to the transport and hold its run task.

        The engine becomes the transport's single handler. ``run()`` reconnects
        internally, so a CLEAN exit means ``close()`` was called and supervision
        stops. ``TransportUnsupportedError`` and ``AuthError`` are terminal: the
        flags (``unsupported`` / ``auth_failed`` / ``terminal_error``) are set
        and the exception stays on the supervision task for the adapter. Any
        other exit restarts ``run()`` with a capped backoff.

        Callable again once the task has finished; a call while supervision is
        RUNNING raises.
        """
        task = self._supervise_task
        if task is not None and not task.done():
            msg = "supervise() already running"
            raise RuntimeError(msg)
        transport.subscribe(self.handle_event)
        self.unsupported = False
        self.auth_failed = False
        self.terminal_error = None
        self._stopping = False
        # stop() shut the feed monitor down (terminal for that run); a new
        # supervision run must be able to report outages again.
        self._feed.start()
        self._supervise_task = asyncio.create_task(self._supervise_loop(transport))

    @property
    def supervision_task(self) -> asyncio.Task[None] | None:
        """The supervision task (holds terminal exceptions for the adapter)."""
        return self._supervise_task

    async def _supervise_loop(self, transport: Transport) -> None:
        """Restart ``run()`` on unexpected exits until stopped/terminal."""
        backoff = _SUPERVISE_BACKOFF
        while True:
            started = timing.monotonic()
            try:
                await transport.run()
            except (AuthError, TransportUnsupportedError) as e:
                self.auth_failed = isinstance(e, AuthError)
                self.unsupported = isinstance(e, TransportUnsupportedError)
                self.terminal_error = e
                # The transport has stopped for good: nothing will push frames
                # again, so every stored value is stale and the entities must
                # say so rather than render their last value as available.
                await self._feed.mark_down(f"transport stopped ({type(e).__name__})")
                raise
            except TransportUnavailableError as e:
                # transient (token-source outage, timeouts): one line, no
                # traceback spam; the ladder still restarts
                _LOGGER.warning("smartbox transport unavailable; restarting: %s", e)
                # The feed's outage signal is the transport's close event. A
                # run that died before it could dispatch one still means no
                # frames can arrive: open the same grace window so stale
                # values cannot keep rendering as available across the restart.
                await self._feed.on_socket_closed(SocketClosed(code=1006))
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("smartbox transport died unexpectedly; restarting")
                await self._feed.on_socket_closed(SocketClosed(code=1006))
            else:
                return  # clean close: nothing to restart
            if self._stopping:
                return
            if timing.monotonic() - started >= SUPERVISE_RUN_RESET_SECONDS:
                backoff = _SUPERVISE_BACKOFF  # long-lived run: reset the ladder
            await timing.sleep(backoff)
            backoff = min(backoff * 2, _SUPERVISE_BACKOFF_MAX)

    async def stop(self) -> None:
        """Stop supervision: cancel the run task (bounded) + pending timers.

        Closing the transport itself stays the caller's job - the seam's
        ``run()`` handles cancellation as its teardown path.
        """
        self._stopping = True
        self._cancel_timers()
        current = asyncio.current_task()
        pending = [task for task in self._resolution_tasks if task is not current]
        if pending:
            await asyncio.wait(pending, timeout=_STOP_TIMEOUT)
        self._resolution_tasks.clear()
        task = self._supervise_task
        self._supervise_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), _STOP_TIMEOUT)

    def _cancel_timers(self) -> None:
        """Cancel the node timer, every deadline, and the feed window."""
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
        for task in self._resolution_tasks:
            task.cancel()
        self._deadlines.clear()
        self._feed.stop()
