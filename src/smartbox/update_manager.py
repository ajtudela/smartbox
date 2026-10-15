"""Smartbox socket update manager."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
import re
from typing import Any, ClassVar

import jq
from pydantic import BaseModel

from smartbox.error import (
    APIUnavailableError,
    SmartboxError,
    SmartboxValidationError,
)
from smartbox.session import AsyncSmartboxSession
from smartbox.socket import SocketSession
from smartbox.state import DeviceState
from smartbox.ws_user import WsUserSocketSession

_LOGGER = logging.getLogger(__name__)

_SIMPLE_JQ_RE = re.compile(r"^\.(\w+)$")

# How long a node-status/setup frame reporting ``sync_status: lost`` may
# remain unconfirmed before the node is reported unavailable. Matches the
# vendor app's short unreachable window (live-probed 2026-09-30).
_DEFAULT_UNAVAILABLE_DELAY = 6.0
# How long an accepted write may go unconfirmed (no ok frame) before a
# one-shot discriminating GET decides whether the write landed.
_DEFAULT_WRITE_CONFIRM_TIMEOUT = 6.0
# Tolerance for comparing numeric strings of differing precision.
_FLOAT_EPSILON = 1e-6


@dataclass
class _PendingWrite:
    """A write awaiting its confirming websocket frame."""

    node: dict[str, Any]
    written: dict[str, Any]
    kind: str = "status"
    task: asyncio.Task | None = field(default=None)


class OptimisedJQMatcher:
    """jq matcher that doesn't bother with jq for simple one-level element queries."""

    _jq_cache: ClassVar[dict[str, Any]] = {}

    def __init__(self, jq_expr: str) -> None:
        """Create an OptimisedJQMatcher for any jq expression."""
        m = _SIMPLE_JQ_RE.match(jq_expr)
        self._fast_path = False
        if m:
            self._fast_path = True
            self._simple_elem = m.group(1)
        else:
            if jq_expr not in self._jq_cache:
                self._jq_cache[jq_expr] = jq.compile(jq_expr)
            self._compiled_jq = self._jq_cache[jq_expr]

    def match(self, input_data: dict[str, Any]) -> Iterable[Any]:
        """Return matches for the given dev data."""
        if self._fast_path:
            return [input_data.get(self._simple_elem)]
        return self._compiled_jq.input(input_data)

    def __repr__(self) -> str:
        """Printable representation."""
        if self._fast_path:
            return str(self)
        return repr(self._compiled_jq)

    def __str__(self) -> str:
        """Str representation."""
        if self._fast_path:
            return f"OptimisedJQMatcher('.{self._simple_elem}', fast_path=True)"
        return str(self._compiled_jq)


class DevDataSubscription:
    """Subscription for dev data callbacks."""

    def __init__(
        self,
        jq_expr: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Create a dev data subscription for the given jq expression."""
        self._jq_matcher = OptimisedJQMatcher(jq_expr)
        self._callback = callback

    def match(self, input_data: dict[str, Any]) -> None:
        """Return matches for this subscription for the given dev data."""
        _LOGGER.debug("Matching jq %s", self._jq_matcher)
        try:
            # The ValueError guard covers jq evaluation only: exceptions
            # from the user callback must not be misattributed to jq.
            matches = list(self._jq_matcher.match(input_data))
        except ValueError:
            _LOGGER.exception("Error evaluating jq on dev data %s", input_data)
            return
        for match in matches:
            if match is None:
                continue
            try:
                # Isolate user callbacks: one raising callback must not
                # starve the other subscriptions or kill the socket loop.
                self._callback(match)
            except Exception:
                _LOGGER.exception(
                    "Error in dev_data callback for %s",
                    self._jq_matcher,
                )


class UpdateSubscription:
    """Subscription for updates."""

    def __init__(
        self,
        path_regex: str,
        jq_expr: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Create an update subscription for the given path regex and body jq expression."""
        self._path_regex = re.compile(path_regex)
        self._jq_matcher = OptimisedJQMatcher(jq_expr)
        self._callback = callback

    def match(self, input_data: dict[str, Any]) -> bool:
        """Return matches for this subscription for the given update."""
        path_match = self._path_regex.search(input_data.get("path", ""))
        if not path_match:
            return False
        path_match_kwargs = path_match.groupdict()
        matched = False
        _LOGGER.debug("Matching jq %s", self._jq_matcher)
        try:
            # The ValueError guard covers jq evaluation only: exceptions
            # from the user callback must not be misattributed to jq.
            data_matches = [
                data_match
                for data_match in self._jq_matcher.match(input_data)
                if data_match is not None
            ]
        except ValueError:
            _LOGGER.exception("Error evaluating jq on update %s", input_data)
            return False
        for data_match in data_matches:
            matched = True
            try:
                # Isolate user callbacks: one raising callback must not
                # starve the other subscriptions or kill the socket loop.
                self._callback(data_match, **path_match_kwargs)
            except Exception:
                _LOGGER.exception(
                    "Error in update callback for %s",
                    self._jq_matcher,
                )
        return matched


class UpdateManager:
    """Manages subscription callbacks to receive updates from a Smartbox socket."""

    BODY_PATH = ".body"

    def __init__(
        self,
        session: AsyncSmartboxSession,
        device_id: str,
        write_confirm_timeout: float = _DEFAULT_WRITE_CONFIRM_TIMEOUT,
        unavailable_delay: float = _DEFAULT_UNAVAILABLE_DELAY,
        ws_user_socket: WsUserSocketSession | None = None,
        **kwargs: Any,  # noqa: ANN401  # pass-through SocketSession knobs
    ) -> None:
        """Create an UpdateManager for a smartbox socket.

        Two transports are supported:

        * ``socket_io`` (default): this manager owns a per-device
          :class:`~smartbox.socket.SocketSession`; ``run()`` runs it.
        * ``ws_user``: pass a shared :class:`~smartbox.ws_user.WsUserSocketSession`
          as ``ws_user_socket`` — one per-user connection serves every
          device; ``run()`` registers this manager's ``device_id`` with
          the shared socket and waits for it to finish, and ``cancel()``
          unregisters. The shared socket's ``run()`` is owned by the
          caller.
        """
        self._session = session
        self._device_id = device_id
        self._write_confirm_timeout = write_confirm_timeout
        self._unavailable_delay = unavailable_delay
        self._ws_user_socket = ws_user_socket
        self._ws_user_cancelled = False
        if ws_user_socket is not None and kwargs:
            _LOGGER.warning(
                "Ignoring socket knobs %s in ws_user mode", sorted(kwargs)
            )
        if ws_user_socket is not None:
            self._socket_session: SocketSession | None = None
            # cancel() must stop a concurrent/awaited run() the same way
            # it does in socket_io mode (cancel ⇒ run() returns).
            self._ws_user_release_event: asyncio.Event | None = None
        else:
            self._socket_session = SocketSession(
                session,
                device_id,
                self._dev_data_cb,
                self._update_cb,
                **kwargs,
            )
        # Replica of the device state; created on first use so consumers
        # that never ask for it pay nothing per frame.
        self._state: DeviceState | None = None
        self._dev_data_subscriptions: list[DevDataSubscription] = []
        self._update_subscriptions: list[UpdateSubscription] = []
        # Node-availability tracking (see subscribe_to_node_availability).
        # The internal frame taps are registered lazily on first use so
        # consumers that never enable the feature see the exact same
        # subscription counts as before.
        self._availability_taps_registered = False
        self._availability_callbacks: list[
            Callable[[str, int, bool], None]
        ] = []
        self._node_availability: dict[tuple[str, int], bool] = {}
        # Per-node timers: lost-frame unavailability + write-confirmation.
        self._lost_timers: dict[tuple[str, int], asyncio.Task] = {}
        self._pending_confirms: dict[tuple[str, int, str], _PendingWrite] = {}

    @property
    def socket_session(self) -> SocketSession | None:
        """Get the underlying per-device socket session.

        None in ``ws_user`` mode (the shared socket takes over); use
        :attr:`ws_user_socket` there.
        """
        return self._socket_session

    @property
    def ws_user_socket(self) -> WsUserSocketSession | None:
        """The shared per-user socket (None in socket_io mode)."""
        return self._ws_user_socket

    @property
    def state(self) -> DeviceState:
        """Replica of the device state, fed by every dev_data/update frame.

        Created on first access: frames received before that are not
        replayed, so access it before calling :meth:`run`.
        """
        if self._state is None:
            self._state = DeviceState()
        return self._state

    async def run(self) -> None:
        """Run the socket session asynchronously, waiting for updates.

        In ws_user mode this registers with the shared per-user socket
        and parks until :meth:`cancel` releases it (or a prior cancel
        made it a no-op); the shared socket's ``run()`` loop is owned by
        the caller. It does NOT return when the shared socket's run loop
        exits: the caller's supervisor owns restarts, registrations
        survive them, and a return here would only make a per-device
        watchdog misdiagnose a healthy-but-restarting transport.
        """
        if self._ws_user_socket is not None:
            if self._ws_user_cancelled:
                return
            self._ws_user_socket.add_device(
                self._device_id,
                self._dev_data_cb,
                self._update_cb,
            )
            release = asyncio.Event()
            self._ws_user_release_event = release
            try:
                # Park until released — see run()'s docstring for why a
                # shared-socket exit must not wake us.
                await release.wait()
            finally:
                self._ws_user_release_event = None
                self._ws_user_socket.remove_device(self._device_id)
            return
        assert self._socket_session is not None  # noqa: S101 - narrowing
        await self._socket_session.run()

    async def cancel(self) -> None:
        """Disconnecting and cancelling tasks.

        In ws_user mode this only releases THIS manager (a parked
        ``run()`` returns and the device unregisters); the shared socket
        is owned by the caller and is NOT cancelled.
        """
        tasks: list[asyncio.Task] = list(self._lost_timers.values())
        tasks.extend(
            pending.task
            for pending in self._pending_confirms.values()
            if pending.task is not None
        )
        for task in tasks:
            task.cancel()
        self._lost_timers.clear()
        self._pending_confirms.clear()
        # The socket cancel below is the yield point at which the
        # cancellations above are actually processed; nothing here awaits
        # them separately (verified: no un-retrieved/destroyed warnings).
        if self._ws_user_socket is not None:
            # cancel ⇒ run() returns promptly (socket_io contract).
            self._ws_user_cancelled = True
            if self._ws_user_release_event is not None:
                self._ws_user_release_event.set()
            self._ws_user_socket.remove_device(self._device_id)
            return
        assert self._socket_session is not None  # noqa: S101 - narrowing
        await self._socket_session.cancel()

    def subscribe_to_dev_data(self, jq_expr: str, callback: Callable) -> None:
        """Subscribe to receive device data."""
        sub = DevDataSubscription(jq_expr, callback)
        self._dev_data_subscriptions.append(sub)

    def subscribe_to_updates(
        self,
        path_regex: str,
        jq_expr: str,
        callback: Callable[..., None],
    ) -> None:
        """Subscribe to receive device and node data updates.

        Named groups in path_regex are passed as kwargs to callback.
        """
        sub = UpdateSubscription(path_regex, jq_expr, callback)
        self._update_subscriptions.append(sub)

    def subscribe_to_device_away_status(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to device away status updates."""
        self.subscribe_to_dev_data(".away_status", callback)
        self.subscribe_to_updates(
            r"^/mgr/away_status",
            self.BODY_PATH,
            callback,
        )

    def subscribe_to_device_connected(
        self,
        callback: Callable[[bool], None],
    ) -> None:
        """Subscribe to device connected updates."""
        self.subscribe_to_dev_data(
            ".connected",
            lambda p: callback(bool(p)),
        )
        self.subscribe_to_updates(
            r"^/connected",
            f"{self.BODY_PATH}.connected",
            lambda p: callback(bool(p)),
        )

    def subscribe_to_device_power_limit(
        self,
        callback: Callable[[int], None],
    ) -> None:
        """Subscribe to device power limit updates."""
        self.subscribe_to_dev_data(
            ".htr_system.setup.power_limit",
            lambda p: callback(int(p)),
        )
        self.subscribe_to_updates(
            r"^/htr_system/(setup|power_limit)",
            f"{self.BODY_PATH}.power_limit",
            lambda p: callback(int(p)),
        )

    def _subscribe_to_node_key(
        self,
        key: str,
        callback: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        """Subscribe to one per-node resource (``status``, ``setup``, ...).

        Covers both the initial ``dev_data`` snapshot and the incremental
        ``update`` messages for ``/<node_type>/<addr>/<key>``.
        """

        def dev_data_wrapper(data: dict[str, Any]) -> None:
            value = data.get(key)
            if value is None:
                # jq's {addr, type, <key>} emits <key>: null for nodes
                # that don't carry the key — nothing to report.
                _LOGGER.debug("%s absent in dev data node, ignoring", key)
                return
            callback(data["type"], int(data["addr"]), value)

        self.subscribe_to_dev_data(
            f"(.nodes[] | {{addr, type, {key}}})?",
            dev_data_wrapper,
        )

        def update_wrapper(
            data: dict[str, Any],
            node_type: str,
            addr: str,
        ) -> None:
            callback(node_type, int(addr), data)

        self.subscribe_to_updates(
            rf"^/(?P<node_type>[^/]+)/(?P<addr>\d+)/{key}$",
            self.BODY_PATH,
            update_wrapper,
        )

    def subscribe_to_node_status(
        self,
        callback: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        """Subscribe to node status updates."""
        self._subscribe_to_node_key("status", callback)

    def subscribe_to_node_setup(
        self,
        callback: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        """Subscribe to node setup updates."""
        self._subscribe_to_node_key("setup", callback)

    def subscribe_to_node_version(
        self,
        callback: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        """Subscribe to node version updates."""
        self._subscribe_to_node_key("version", callback)

    def subscribe_to_node_prog(
        self,
        callback: Callable[[str, int, dict[str, Any]], None],
    ) -> None:
        """Subscribe to node prog (schedule) updates."""
        self._subscribe_to_node_key("prog", callback)

    def subscribe_to_node_availability(
        self,
        callback: Callable[[str, int, bool], None],
    ) -> None:
        """Subscribe to node availability (reachable/unreachable) updates.

        Callback signature: ``(node_type, addr, available)``. Availability
        reflects whether the box can currently reach the node: a node whose
        status frames carry ``sync_status: "lost"`` without a subsequent ok
        frame is reported unavailable; any ok frame reports it available
        again.

        The node status/setup frame taps used here are shared with
        :meth:`expect_write_confirmation`; each feature enables them
        independently — neither depends on the other.
        """
        # Identity-dedupe: a consumer that re-subscribes the same callback
        # (config reload, retry) must not double every availability event.
        if callback not in self._availability_callbacks:
            self._availability_callbacks.append(callback)
        self._ensure_availability_taps()

    def get_node_availability(self, node_type: str, addr: int) -> bool | None:
        """Return the last known availability of a node, or None if unknown."""
        return self._node_availability.get((node_type, addr))

    def expect_write_confirmation(
        self,
        node: dict[str, Any],
        written: dict[str, Any],
        kind: str = "status",
    ) -> None:
        """Arm a confirmation window for an accepted node write.

        Must be called right after a successful ``set_node_status`` /
        ``set_node_setup``. If no ok frame arrives within
        ``write_confirm_timeout``, a one-shot GET decides whether the write
        actually landed: the server acknowledges writes to unreachable
        nodes without applying them (live finding 2026-09-30), and keeps
        serving the last-known values, so the GET's contents discriminate
        "write applied (node alive)" from "write silently dropped (node
        unreachable)".

        Known limit: a write whose values EQUAL the node's last-known
        state can only be misreported while the node sits in the
        server's minutes-long stale-ok window after a power loss (the
        GET serves ok-marked last-known values); once the server has
        flipped the node to a lost-marked payload the verdict is
        Unavailable. Periodic re-checking was deliberately rejected
        (see api-notes.md).

        If ``unavailable_delay`` is configured shorter than
        ``write_confirm_timeout``, a lost frame during a pending write
        can emit a momentary Unavailable before the GET's verdict
        (correct in outcome).

        ``kind`` is ``"status"`` or ``"setup"`` and selects both the
        confirming frame type and the discriminating endpoint; anything
        else raises ``ValueError`` (an unknown kind would otherwise
        silently behave as ``"status"`` and never verify the real write).
        Requires a running event loop.
        """
        if kind not in ("status", "setup"):
            msg = f"kind must be 'status' or 'setup', got {kind!r}"
            raise ValueError(msg)
        self._ensure_availability_taps()
        node_type = str(node["type"])
        addr = int(node["addr"])
        written = {k: v for k, v in written.items() if v is not None}
        if not written:
            # Only-None/no-op write: nothing to discriminate on — the GET
            # check below would return "applied" unconditionally, dead node
            # included. Leave any prior confirmation for this node running.
            return
        key = (node_type, addr, kind)
        if (
            old := self._pending_confirms.pop(key, None)
        ) is not None and old.task is not None:
            old.task.cancel()
        pending = _PendingWrite(
            node=dict(node),  # snapshot: caller may mutate its dict
            written=written,
            kind=kind,
        )
        self._pending_confirms[key] = pending
        pending.task = asyncio.get_running_loop().create_task(
            self._write_confirm_expired(pending),
        )

    def _ensure_availability_taps(self) -> None:
        """Register the internal node status/setup frame taps once."""
        if self._availability_taps_registered:
            return
        self._availability_taps_registered = True
        # Both subscription helpers deliver (node_type, addr, data) —
        # register the shared frame handler directly.
        self.subscribe_to_node_status(self._availability_frame)
        self.subscribe_to_node_setup(self._availability_frame)

    def _availability_frame(
        self, node_type: str, addr: int, data: dict[str, Any]
    ) -> None:
        """Process one node status/setup frame for availability."""
        if data.get("sync_status", "ok") == "ok":
            # Cancels pending confirmations for BOTH kinds — deliberate,
            # not keyed by frame kind: the ok frame itself reports the
            # node Available, the same verdict a surviving discriminator
            # GET would reach on an alive node; and on a dead node no ok
            # frames arrive at all (writes get no device ack, no stale-ok
            # pushes observed), so a setup GET that needs to run is never
            # disturbed by one.
            self._cancel_node_timers(node_type, addr)
            self._emit_availability(node_type, addr, available=True)
        # A bare ``{"sync_status": "lost"}`` frame is ambiguous while a
        # write confirmation is pending (transient post-write frame):
        # the pending window decides. With no write in flight it means
        # the server considers the node unreachable — but still give
        # the node ``unavailable_delay`` seconds to produce an ok
        # frame, so a foreign client's transient frame cannot flip a
        # live node.
        elif (
            data.get("sync_status", "ok") == "lost"
            and (node_type, addr) not in self._lost_timers
        ):
            # Only "lost" is evidence of lost; an unknown future
            # sync_status value degrades to no-change (same no-evidence
            # rule as the confirm path).
            self._arm_lost_timer(node_type, addr)

    def _arm_lost_timer(self, node_type: str, addr: int) -> None:
        if (old := self._lost_timers.get((node_type, addr))) is not None:
            # Arm replaces: today nothing bypasses the absent-key guard in
            # _availability_frame, but a future call path must not leak a
            # live timer task under an overwritten key.
            old.cancel()
        self._lost_timers[(node_type, addr)] = (
            asyncio.get_running_loop().create_task(
                self._lost_timer_expired(node_type, addr),
            )
        )

    def _cancel_lost_timer(self, node_type: str, addr: int) -> None:
        if (
            timer := self._lost_timers.pop((node_type, addr), None)
        ) is not None:
            timer.cancel()

    def _cancel_node_timers(self, node_type: str, addr: int) -> None:
        self._cancel_lost_timer(node_type, addr)
        for key in [
            k
            for k in self._pending_confirms
            if k[0] == node_type and k[1] == addr
        ]:
            pending = self._pending_confirms.pop(key)
            if pending.task is not None:
                pending.task.cancel()

    def _retire_pending(self, pending: _PendingWrite) -> None:
        """Remove a confirmation whose task has finished without a verdict.

        Ownership-checked: never touches a newer re-armed confirmation
        under the same key, so the dict keeps meaning "currently armed".
        """
        node = pending.node
        key = (str(node["type"]), int(node["addr"]), pending.kind)
        if self._pending_confirms.get(key) is pending:
            del self._pending_confirms[key]

    async def _lost_timer_expired(self, node_type: str, addr: int) -> None:
        await asyncio.sleep(self._unavailable_delay)
        if (
            self._lost_timers.get((node_type, addr))
            is not asyncio.current_task()
        ):
            # Cancelled/replaced and the key is already gone — not ours to
            # decide (same ownership convention as the confirm verdict).
            return
        self._lost_timers.pop((node_type, addr), None)
        self._emit_availability(node_type, addr, available=False)

    async def _write_confirm_expired(self, pending: _PendingWrite) -> None:
        await asyncio.sleep(self._write_confirm_timeout)
        node = pending.node
        node_type = str(node["type"])
        addr = int(node["addr"])
        actual: Any
        try:
            if pending.kind == "setup":
                actual = await self._session.get_node_setup(
                    self._device_id,
                    node,
                )
            else:
                actual = await self._session.get_node_status(
                    self._device_id,
                    node,
                )
        except SmartboxValidationError as err:
            self._retire_pending(pending)
            payload = getattr(err, "payload", None)
            if (
                isinstance(payload, dict)
                and payload.get("sync_status") == "lost"
            ):
                # Typed mode: the dead node's status GET returns the bare
                # lost payload, which FAILS NodeStatus validation -
                # the payload IS the verdict (documented dead-node wire
                # shape), not API trouble.
                self._emit_availability(node_type, addr, available=False)
                return
            # Any other validation drift is not node death: no evidence.
            _LOGGER.warning(
                "Write confirmation check for %s/%s failed: %r",
                node_type,
                addr,
                err,
            )
            return
        except (SmartboxError, APIUnavailableError) as err:
            # No evidence either way (API trouble is not node death) —
            # leave the current state untouched; the next write re-arms.
            _LOGGER.warning(
                "Write confirmation check for %s/%s failed: %r",
                node_type,
                addr,
                err,
            )
            self._retire_pending(pending)
            return
        except Exception as err:  # noqa: BLE001
            # Node.model_validate on a malformed node dict, transport
            # timeouts, etc. must not die un-retrieved in this task (same
            # isolation convention as the callbacks above). No evidence —
            # leave the state untouched.
            _LOGGER.warning(
                "Write confirmation check for %s/%s failed unexpectedly: %r",
                node_type,
                addr,
                err,
            )
            self._retire_pending(pending)
            return
        if isinstance(actual, BaseModel):
            # Typed mode (raw_response=False): the confirming GET returns a
            # pydantic model, not the wire dict raw mode delivers. Adapt it
            # or the discriminator below would reject every model — alive
            # nodes' verdicts inverted to "not applied".
            actual = actual.model_dump()
        if self._pending_confirms.get((node_type, addr, pending.kind)) is not (
            pending
        ):
            # Replaced by a newer write (which cancels this task — this is
            # defense-in-depth for the window between the GET resolving and
            # the emit): the newer confirmation owns the verdict.
            return
        del self._pending_confirms[(node_type, addr, pending.kind)]
        if _written_applied(pending.written, actual):
            # The GET proved the node alive — a lost frame may still sit in
            # its grace window (e.g. the transient/foreign push whose ok
            # never came, which is why this GET ran at all). The verdict
            # owns the state: cancel the timer before emitting True.
            self._cancel_lost_timer(node_type, addr)
            self._emit_availability(node_type, addr, available=True)
        else:
            self._emit_availability(node_type, addr, available=False)

    def _emit_availability(
        self,
        node_type: str,
        addr: int,
        *,
        available: bool,
    ) -> None:
        """Emit availability, deduplicated per node."""
        key = (node_type, addr)
        if self._node_availability.get(key) == available:
            return
        self._node_availability[key] = available
        _LOGGER.debug("Node %s/%s availability: %s", node_type, addr, available)
        for callback in list(self._availability_callbacks):
            try:
                callback(node_type, addr, available)
            except Exception:
                _LOGGER.exception(
                    "Error in availability callback for %s/%s",
                    node_type,
                    addr,
                )

    def _dev_data_cb(self, data: dict[str, Any]) -> None:
        if self._state is not None:
            self._state.apply_dev_data(data)
        for sub in self._dev_data_subscriptions:
            sub.match(data)

    def _update_cb(self, data: dict[str, Any]) -> None:
        if "path" not in data:
            # Checked once, not per subscription: with the check inside
            # the loop a malformed update was logged once per subscription.
            _LOGGER.error("Path not found in update data: %s", data)
            return
        if self._state is not None:
            self._state.apply_update(data)
        matched = False
        for sub in self._update_subscriptions:
            if sub.match(data):
                matched = True
        if not matched:
            _LOGGER.debug("No matches for update %s", data)


def _written_applied(written: dict[str, Any], actual: Any) -> bool:  # noqa: ANN401
    """Check whether the GET result contains everything that was written.

    Deliberately loose: values may be numeric strings with differing
    precision ("20.0" vs "20.00"), so equality falls back to a numeric
    comparison; an explicit JSON null in the response counts as a missing
    key, and 0 == False compares equal. Tight enough for the htr/acm
    status/setup field domain the feature is used on. A payload marked
    sync_status "lost" matches nothing - the server is reporting the node
    unreachable, so nothing in it can evidence an applied write (dead-node
    /setup GETs keep the full field set with a lost marker).
    """
    if not isinstance(actual, dict):
        return False
    if actual.get("sync_status") == "lost":
        # The server explicitly reports the node unreachable: nothing in
        # this payload can evidence an applied write, not even values
        # matching the last-known state (dead-node /setup GETs keep the
        # full field set with a lost marker).
        return False
    for key, value in written.items():
        actual_value = actual.get(key)
        if actual_value is None:
            return False
        if actual_value == value:
            continue
        try:
            if abs(float(value) - float(actual_value)) < _FLOAT_EPSILON:
                continue
        except TypeError, ValueError:
            return False
        # Numeric comparison said "different".
        return False
    return True
