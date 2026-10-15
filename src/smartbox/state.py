"""Sans-I/O device state replica.

``DeviceState`` keeps one tree per device and applies the two kinds of
frames the API produces to it: the ``dev_data`` snapshot (replaces the
tree) and incremental ``update`` frames of the form
``{"path": "/htr/2/status", "body": {...}}`` (merged into the tree as
RFC 7386 patches). Nothing here touches the network or the event loop,
so every transition is testable with plain function calls.
"""

from collections.abc import Callable
import copy
from dataclasses import dataclass, field
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ("device", <key>) for device-level state, (<node_type>, <addr>, <key>)
# for per-node resources (status, setup, version, prog, ...).
DeviceKey = tuple[str, str]
NodeKey = tuple[str, int, str]
StateKey = DeviceKey | NodeKey

_NODE_PATH_RE = re.compile(
    r"^/(?P<node_type>[^/]+)/(?P<addr>\d+)/(?P<key>[^/]+)$"
)
_POWER_LIMIT_PATH_RE = re.compile(r"^/htr_system/(setup|power_limit)$")


def apply_merge_patch(
    target: Any,  # noqa: ANN401
    patch: Any,  # noqa: ANN401
    *,
    delete_nulls: bool = True,
) -> Any:  # noqa: ANN401
    """Apply an RFC 7386 JSON Merge Patch and return the new document.

    ``target`` is never mutated. With ``delete_nulls=False`` a ``None``
    in the patch is stored as a value instead of deleting the key: the
    device frames are observations, not edit scripts, so a reported
    ``null`` must not erase state.
    """
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None and delete_nulls:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(
                result.get(key), value, delete_nulls=delete_nulls
            )
    return result


def route_update(path: str) -> StateKey | None:
    """Map an ``update`` frame path to the state key it writes.

    Returns ``None`` for paths the replica does not model.
    """
    node = _NODE_PATH_RE.match(path)
    if node:
        return (node["node_type"], int(node["addr"]), node["key"])
    if path == "/mgr/away_status":
        return ("device", "away_status")
    if path == "/connected":
        return ("device", "connected")
    if _POWER_LIMIT_PATH_RE.match(path):
        return ("device", "power_limit")
    return None


@dataclass(frozen=True)
class DeviceSnapshot:
    """Immutable, comparable copy of a device's state.

    Equality is structural, so a consumer can skip redundant refreshes
    (e.g. Home Assistant ``always_update=False``).
    """

    device: dict[str, Any] = field(default_factory=dict)
    nodes: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)


class DeviceState:
    """Single source of truth for one device's replicated state."""

    def __init__(self) -> None:
        """Create an empty replica."""
        self._device: dict[str, Any] = {}
        self._nodes: dict[tuple[str, int], dict[str, Any]] = {}
        self._observers: list[
            Callable[[DeviceSnapshot, set[StateKey]], None]
        ] = []

    def subscribe(
        self, callback: Callable[[DeviceSnapshot, set[StateKey]], None]
    ) -> None:
        """Call ``callback(snapshot, changed_keys)`` after every real change."""
        if callback not in self._observers:
            self._observers.append(callback)

    def snapshot(self) -> DeviceSnapshot:
        """Return a deep copy of the current state."""
        return DeviceSnapshot(
            device=copy.deepcopy(self._device),
            nodes=copy.deepcopy(self._nodes),
        )

    def apply_dev_data(self, data: dict[str, Any]) -> set[StateKey]:
        """Replace the whole tree from a ``dev_data`` snapshot."""
        device = {k: copy.deepcopy(v) for k, v in data.items() if k != "nodes"}
        # Power limit lives under htr_system.setup in dev_data but is
        # addressed as a device-level key by update frames.
        limit = device.get("htr_system", {}).get("setup", {}).get("power_limit")
        if limit is not None:
            device["power_limit"] = limit
        nodes: dict[tuple[str, int], dict[str, Any]] = {}
        for node in data.get("nodes") or []:
            try:
                nodes[(node["type"], int(node["addr"]))] = copy.deepcopy(node)
            except KeyError, TypeError, ValueError:
                _LOGGER.debug("Skipping malformed dev_data node %s", node)
        changed = self._diff(self._device, self._nodes, device, nodes)
        self._device, self._nodes = device, nodes
        self._notify(changed)
        return changed

    def apply_update(self, message: dict[str, Any]) -> set[StateKey]:
        """Merge one ``update`` frame into the tree.

        Returns the keys whose value changed (empty for unrouted paths,
        malformed frames and no-op updates).
        """
        key = route_update(message.get("path", ""))
        body = message.get("body")
        if key is None or not isinstance(body, dict):
            _LOGGER.debug("Update not applied to state: %s", message)
            return set()
        if len(key) == 2:
            changed = self._apply_device(key[1], body)
        else:
            changed = self._apply_node(key, body)
        self._notify(changed)
        return changed

    def _apply_device(self, name: str, body: dict[str, Any]) -> set[StateKey]:
        if name == "connected":
            new = body.get("connected")
            if new is None:
                return set()
        elif name == "power_limit":
            new = body.get("power_limit")
            if new is None:
                return set()
        else:
            new = apply_merge_patch(
                self._device.get(name), body, delete_nulls=False
            )
        if self._device.get(name) == new:
            return set()
        self._device[name] = new
        return {("device", name)}

    def _apply_node(self, key: NodeKey, body: dict[str, Any]) -> set[StateKey]:
        node_type, addr, resource = key
        node = self._nodes.setdefault(
            (node_type, addr), {"type": node_type, "addr": addr}
        )
        new = apply_merge_patch(node.get(resource), body, delete_nulls=False)
        if node.get(resource) == new:
            return set()
        node[resource] = new
        return {key}

    @staticmethod
    def _diff(
        old_device: dict[str, Any],
        old_nodes: dict[tuple[str, int], dict[str, Any]],
        new_device: dict[str, Any],
        new_nodes: dict[tuple[str, int], dict[str, Any]],
    ) -> set[StateKey]:
        changed: set[StateKey] = {
            ("device", k)
            for k in old_device.keys() | new_device.keys()
            if old_device.get(k) != new_device.get(k)
        }
        for node_id in old_nodes.keys() | new_nodes.keys():
            old, new = old_nodes.get(node_id, {}), new_nodes.get(node_id, {})
            changed.update(
                (*node_id, k)
                for k in old.keys() | new.keys()
                if k not in ("type", "addr") and old.get(k) != new.get(k)
            )
        return changed

    def _notify(self, changed: set[StateKey]) -> None:
        if not changed or not self._observers:
            return
        snapshot = self.snapshot()
        for observer in self._observers:
            try:
                # Isolate observers: one raising must not starve the rest
                # or kill the socket loop.
                observer(snapshot, set(changed))
            except Exception:
                _LOGGER.exception("Error in state observer")
