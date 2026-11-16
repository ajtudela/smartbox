"""Versioned domain state for the core layer.

The store is VERSIONED RAW STATE: wire payloads are kept as plain dicts exactly
as delivered (no validation, no model classes), and typed accessors are built
on ``smartbox.protocol.values`` by consumers. Every container is replaced
copy-on-write, so a reference to a previous revision stays valid forever and
everything inside a state object is read-only.

Revisions are per object, not per field:

- ``NodeState.revision`` counts updates that changed at least one of the node's
  status/setup/prog/version slots (starts 0). A frame changing two slots bumps
  it once and emits one change event per slot, all carrying the new revision; an
  accepted write's optimistic merge bumps it too.
- ``DeviceState.revision`` is the same for the device-level slots. Per-node
  merges rebuild the owning ``DeviceState`` without bumping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "NODE_SLOTS",
    "Change",
    "DeviceChange",
    "DeviceField",
    "DeviceRemoved",
    "DeviceState",
    "FeedChange",
    "NodeAdded",
    "NodeChange",
    "NodeField",
    "NodeRemoved",
    "NodeState",
    "WorldState",
    "node_device_id",
    "node_key",
]

NodeField = Literal["status", "setup", "prog", "version", "availability"]
DeviceField = Literal["connected", "away", "power_limit", "nodes"]

# The raw slots a node entry or frame carries, in merge order.
NODE_SLOTS: tuple[NodeField, ...] = ("status", "setup", "prog", "version")


def node_key(node_type: str, addr: str | int) -> str:
    """Canonical node map key: ``{node_type}/{addr}`` (addr stringified).

    Snapshot node entries may carry a numeric ``addr`` while update
    paths carry it as a path segment - both normalize to the same key.
    """
    return f"{node_type}/{addr}"


def node_device_id(dev_id: str, key: str) -> str:
    """Registry node id (``{dev_id}_{addr}``) for a canonical node key."""
    return f"{dev_id}_{int(key.partition('/')[2])}"


@dataclass(frozen=True)
class NodeState:
    """One node's raw slots + revision.

    ``prog`` / ``version`` are ``None`` until the wire delivered them: a node
    that never sent prog differs from one carrying an empty schedule.
    ``available`` is engine-derived (``None`` = unknown) and written ONLY by
    availability transitions, which never bump the revision - revisions count
    frames, not derived flips.
    """

    status: dict[str, Any] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    prog: dict[str, Any] | None = None
    version: dict[str, Any] | None = None
    revision: int = 0
    available: bool | None = None


@dataclass(frozen=True)
class DeviceState:
    """One device's raw slots, node map + revision.

    ``away`` is a raw wire dict (empty until seen) and ``power_limit`` the raw
    wire value; ``node_states`` is the node map keyed by :func:`node_key`, and
    it alone defines membership (the wire node list's installed entries are
    read for their keys).
    ``home_id`` is metadata only - no revision bump, no change event.
    """

    dev_id: str
    home_id: str | None = None
    connected: bool | None = None
    away: dict[str, Any] = field(default_factory=dict)
    power_limit: Any = None
    node_states: dict[str, NodeState] = field(default_factory=dict)
    revision: int = 0


@dataclass(frozen=True)
class WorldState:
    """The whole tracked world: device map (homes flattened away)."""

    devices: dict[str, DeviceState] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeChange:
    """One node slot changed; ``node`` is the NEW state + revision."""

    dev_id: str
    node_key: str
    kind: NodeField
    node: NodeState


@dataclass(frozen=True)
class DeviceChange:
    """One device slot changed; ``device`` is the NEW state + revision."""

    dev_id: str
    kind: DeviceField
    device: DeviceState


@dataclass(frozen=True)
class NodeAdded:
    """A fully parsed membership source introduced a node the store did not hold.

    The source is a snapshot or a ``/mgr/nodes`` update. Membership evidence,
    not slot data: the adapter refreshes identity and adds the missing
    entities.
    """

    dev_id: str
    node_key: str
    name: str | None = None

    @property
    def node_id(self) -> str:
        """Registry node id (``{dev_id}_{addr}``) for entity lookup."""
        return node_device_id(self.dev_id, self.node_key)


@dataclass(frozen=True)
class NodeRemoved:
    """A fully parsed membership source no longer holds this node.

    The store has already dropped the node; this is the adapter's cue to
    confirm the absence against the backend before it cleans the device
    registry. Membership evidence, not slot data: a node that is merely
    unreachable is still a member and never reports here.
    """

    dev_id: str
    node_key: str

    @property
    def node_id(self) -> str:
        """Registry node id (``{dev_id}_{addr}``) for device lookup."""
        return node_device_id(self.dev_id, self.node_key)


@dataclass(frozen=True)
class DeviceRemoved:
    """A fully parsed snapshot no longer holds this device (box)."""

    dev_id: str


@dataclass(frozen=True)
class FeedChange:
    """The feed (websocket) state changed; not tied to one dev/node.

    ``available`` turns False once the wire has been silent past the engine's
    grace window, and True as soon as a frame arrives (``outage_s`` then
    carries the measured outage). Every entity re-renders on this change: while
    the feed is down every value in the store is stale, whatever a node's own
    availability slot says.
    """

    available: bool
    reason: str | None = None
    outage_s: float | None = None


Change = (
    NodeChange | DeviceChange | FeedChange | NodeAdded | NodeRemoved | DeviceRemoved
)
