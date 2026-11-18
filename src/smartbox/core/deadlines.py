"""Armed node deadlines: write confirmations and lost-frame windows.

The engine decides what an elapsed deadline MEANS (a lost window flips the
node Unavailable; a write timeout starts one verification GET) and drives the
single timer; this module owns which deadlines are armed, when they expire,
and what a frame does to them. One timer serves both kinds, so a superseded or
removed deadline needs no cancellation.

Rules the engine relies on:

- At most one pending write per node, identified by its arm generation. An arm
  remembers the window it superseded: a write that disarms itself (value-equal
  body, failed POST) restores that window exactly as it was armed, so the node
  keeps its verification window.
- A lost window is never extended: repeated lost frames leave the first
  deadline standing.
- A frame confirms a pending write only when it carries the keys the write set
  at those values. A prog frame is normalized to its day map first, so the
  whole-week echo proves only the days the write changed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from ..protocol import timing
from ..protocol.values import same_wire_value

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .state import NodeField

__all__ = ["DueDeadline", "NodeDeadline", "NodeDeadlines"]


@dataclass(frozen=True)
class NodeDeadline:
    """One armed node deadline: the machinery's read-only view.

    ``kind`` is ``"write"`` (carrying the slot the write posted to) or
    ``"lost"``. :attr:`SmartboxEngine.deadlines` exposes what is armed, so
    tests and diagnostics can see which node is about to lose availability or
    be verified. ``expires_in`` is seconds from now, slightly negative while
    the timer is being scheduled.
    """

    dev_id: str
    node_key: str
    kind: Literal["write", "lost"]
    slot: NodeField | None = None
    expires_in: float = 0.0


@dataclass(frozen=True)
class DueDeadline:
    """An elapsed deadline handed to the engine.

    ``kind`` is ``"lost"`` or ``"write"``. A write carries the slot and intent
    the confirmation was armed for, so the engine can verify it with one GET.
    """

    dev_id: str
    node_key: str
    kind: Literal["write", "lost"]
    reason: str
    slot: NodeField | None = None
    intent: Mapping[str, Any] | None = None
    body: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _PendingWrite:
    """One armed pending-write confirmation (generation guards re-arms).

    ``slot`` is the slot the write posted to: a frame confirms only on that
    slot (a status write echoes a full status body, a setup write the full
    merged setup body - hass-smartbox docs/api-contract.md §5.3). ``intent`` is what the
    write changed, with the values it set (prog day keys after normalization);
    every key must be present at that value in the frame.

    ``generation`` is the arm counter at arm time, so a write that disarms
    itself can prove the pending it drops is its own. ``superseded`` is the
    pending this arm replaced, restored if this one disarms itself.

    ``deadline`` is when the confirmation expires (see ``timing.monotonic``).
    """

    generation: int
    slot: NodeField
    intent: Mapping[str, Any] | None
    deadline: float
    body: Mapping[str, Any] | None = None
    superseded: _PendingWrite | None = None


class NodeDeadlines:
    """The armed deadlines, keyed by ``(dev_id, node key)``.

    Every mutator reports whether the maps changed, so the engine re-arms its
    timer only when it has to.
    """

    def __init__(
        self, *, write_confirm_timeout: float, unavailable_delay: float
    ) -> None:
        """Store the window lengths and start with nothing armed."""
        self._write_confirm_timeout = write_confirm_timeout
        self._unavailable_delay = unavailable_delay
        self._pending_writes: dict[tuple[str, str], _PendingWrite] = {}
        self._lost_flips: dict[tuple[str, str], float] = {}
        self._arm_generation = 0

    # --- queries ---

    def pending_slot(self, dev_id: str, key: str) -> NodeField | None:
        """Return the slot a pending write posted to, or ``None``."""
        pending = self._pending_writes.get((dev_id, key))
        return None if pending is None else pending.slot

    def view(self) -> tuple[NodeDeadline, ...]:
        """Every armed deadline, earliest expiry first."""
        now = timing.monotonic()
        armed = [
            NodeDeadline(dev_id, key, "write", pending.slot, pending.deadline - now)
            for (dev_id, key), pending in self._pending_writes.items()
        ]
        armed.extend(
            NodeDeadline(dev_id, key, "lost", None, deadline - now)
            for (dev_id, key), deadline in self._lost_flips.items()
        )
        armed.sort(key=lambda deadline: deadline.expires_in)
        return tuple(armed)

    def next_delay(self) -> float | None:
        """Seconds until the earliest deadline, or ``None`` when none is armed."""
        deadlines = [pending.deadline for pending in self._pending_writes.values()]
        deadlines.extend(self._lost_flips.values())
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - timing.monotonic())

    # --- write confirmations ---

    def arm_write(
        self,
        dev_id: str,
        key: str,
        slot: NodeField,
        intent: Mapping[str, Any] | None,
        *,
        body: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> int:
        """(Re)arm the node's pending write: one pending, refreshed deadline.

        Returns the generation, so the writing call can later prove the pending
        it wants to disarm is still its own. The arm it replaces is kept, so a
        write that disarms itself restores that window instead of leaving the
        node with no verification path. ``timeout`` overrides the configured
        confirmation window for the in-flight hold the engine arms before the
        POST; :meth:`refresh_write` shortens it once the POST returns.
        """
        token = (dev_id, key)
        old = self._pending_writes.get(token)
        window = self._write_confirm_timeout if timeout is None else timeout
        self._arm_generation += 1
        self._pending_writes[token] = _PendingWrite(
            generation=self._arm_generation,
            slot=slot,
            intent=intent,
            deadline=timing.monotonic() + window,
            body=body,
            superseded=old,
        )
        return self._arm_generation

    def refresh_write(self, dev_id: str, key: str, generation: int) -> bool:
        """Restart the confirmation countdown after the POST returned.

        The engine arms before the POST so a confirming frame already in
        flight is caught, but a token refresh or a slow POST can take longer
        than the confirmation window. The countdown that ends in the
        verification GET starts here, when the request has actually landed.
        """
        token = (dev_id, key)
        entry = self._pending_writes.get(token)
        if entry is None or entry.generation != generation:
            return False
        self._pending_writes[token] = replace(
            entry,
            deadline=timing.monotonic() + self._write_confirm_timeout,
        )
        return True

    def disarm_write(self, dev_id: str, key: str, generation: int) -> bool:
        """Disarm the node's pending write, but only when it is still this one.

        The generation check keeps two overlapping writes to one node from
        disarming each other. If this write had superseded an earlier in-flight
        one, that write is still unconfirmed, so its window is restored here.
        """
        token = (dev_id, key)
        entry = self._pending_writes.get(token)
        if entry is None or entry.generation != generation:
            return False
        if entry.superseded is not None:
            self._pending_writes[token] = entry.superseded
        else:
            del self._pending_writes[token]
        return True

    def confirm_write(
        self, dev_id: str, key: str, slot: NodeField, body: Mapping[str, Any]
    ) -> bool:
        """Disarm the node's pending write when ``body`` is its confirmation.

        A non-lost frame on the pending's own slot is enough where the write
        takes no key-level evidence (``intent is None`` - the prog write, whose
        confirming frame echoes the whole week + ``sync_status``, live
        2026-10-18, docs/api-ws-user.md §4). Everywhere else the body must
        carry each key the write set, at the value it set: the server echoes a
        status write in a full status body and a setup write's key in the
        merged setup body (hass-smartbox docs/api-contract.md §5.3), while a frame that was
        already in flight when the write armed still carries the pre-write
        value.
        """
        pending = self._pending_writes.get((dev_id, key))
        if pending is None or pending.slot != slot:
            return False
        if pending.intent is not None and not all(
            key in body and same_wire_value(body[key], value)
            for key, value in pending.intent.items()
        ):
            return False
        del self._pending_writes[(dev_id, key)]
        return True

    # --- lost windows ---

    def schedule_lost(self, dev_id: str, key: str) -> bool:
        """Schedule the unavailable flip after the delay window.

        Never rescheduled: repeated lost frames must not extend the window. A
        lost frame arms the normal window even while a write is pending: the
        window owns availability, while the write timeout independently
        verifies the write with a GET. An ok frame cancels the window. True
        when this call armed a window.
        """
        token = (dev_id, key)
        if token in self._lost_flips:
            return False
        self._lost_flips[token] = timing.monotonic() + self._unavailable_delay
        return True

    def cancel_lost(self, dev_id: str, key: str) -> bool:
        """Cancel an armed lost window; True when one was armed."""
        return self._lost_flips.pop((dev_id, key), None) is not None

    # --- bulk ---

    def forget(self, dev_id: str, key: str) -> bool:
        """Drop a removed node's deadlines.

        The node list is authoritative, so a window armed before the removal
        must not fire afterwards: it would rebuild the node as a shell the box
        no longer reports.
        """
        token = (dev_id, key)
        had_pending = self._pending_writes.pop(token, None) is not None
        had_lost = self._lost_flips.pop(token, None) is not None
        return had_pending or had_lost

    def take_due(self) -> list[DueDeadline]:
        """Remove and return every elapsed deadline."""
        now = timing.monotonic()
        due: list[DueDeadline] = []
        for (dev_id, key), deadline in list(self._lost_flips.items()):
            if deadline <= now:
                del self._lost_flips[(dev_id, key)]
                due.append(
                    DueDeadline(
                        dev_id,
                        key,
                        "lost",
                        f"{self._unavailable_delay:.1f}s lost window elapsed",
                    )
                )
        for (dev_id, key), pending in list(self._pending_writes.items()):
            if pending.deadline <= now:
                del self._pending_writes[(dev_id, key)]
                due.append(
                    DueDeadline(
                        dev_id,
                        key,
                        "write",
                        "pending write timed out",
                        pending.slot,
                        pending.intent,
                        pending.body,
                    )
                )
        return due

    def clear(self) -> None:
        """Drop every deadline (teardown)."""
        self._pending_writes.clear()
        self._lost_flips.clear()
