"""Tests for the sans-I/O device state replica."""

from unittest.mock import MagicMock

from smartbox.state import DeviceState, apply_merge_patch, route_update

DEV_DATA = {
    "connected": True,
    "away_status": {"enabled": False, "away": False, "forced": False},
    "htr_system": {"setup": {"power_limit": 1500}},
    "nodes": [
        {
            "addr": 2,
            "type": "htr",
            "status": {"mtemp": "20.0", "stemp": "21.0", "mode": "auto"},
            "setup": {"units": "C"},
        }
    ],
}


def test_merge_patch_rfc7386_examples():
    """Nested merge, replacement of non-objects and null deletion."""
    target = {"a": {"b": 1, "c": 2}, "d": [1]}
    patch = {"a": {"b": 3, "c": None}, "d": [2], "e": 5}
    assert apply_merge_patch(target, patch) == {
        "a": {"b": 3},
        "d": [2],
        "e": 5,
    }
    assert target == {"a": {"b": 1, "c": 2}, "d": [1]}


def test_merge_patch_keeps_nulls_when_asked():
    """With delete_nulls=False a null is a value, not a deletion."""
    assert apply_merge_patch({"a": 1}, {"a": None}, delete_nulls=False) == {
        "a": None
    }


def test_route_update():
    """Known paths route to keys; unknown ones to None."""
    assert route_update("/htr/2/status") == ("htr", 2, "status")
    assert route_update("/mgr/away_status") == ("device", "away_status")
    assert route_update("/connected") == ("device", "connected")
    assert route_update("/htr_system/power_limit") == ("device", "power_limit")
    assert route_update("/htr_system/setup") == ("device", "power_limit")
    assert route_update("/nope") is None
    assert route_update("") is None


def test_dev_data_populates_and_reports_changes():
    """The first dev_data reports every key as changed; a repeat none."""
    state = DeviceState()
    changed = state.apply_dev_data(DEV_DATA)
    assert ("htr", 2, "status") in changed
    assert ("device", "connected") in changed
    snap = state.snapshot()
    assert snap.device["power_limit"] == 1500
    assert snap.nodes[("htr", 2)]["status"]["stemp"] == "21.0"
    assert state.apply_dev_data(DEV_DATA) == set()


def test_update_merges_partial_status():
    """A partial status frame only changes the fields it carries."""
    state = DeviceState()
    state.apply_dev_data(DEV_DATA)
    changed = state.apply_update(
        {"path": "/htr/2/status", "body": {"stemp": "22.0"}}
    )
    assert changed == {("htr", 2, "status")}
    status = state.snapshot().nodes[("htr", 2)]["status"]
    assert status == {"mtemp": "20.0", "stemp": "22.0", "mode": "auto"}


def test_update_noop_and_unrouted_return_empty():
    """Identical values, unknown paths and bad bodies change nothing."""
    state = DeviceState()
    state.apply_dev_data(DEV_DATA)
    same = {"path": "/htr/2/status", "body": {"stemp": "21.0"}}
    assert state.apply_update(same) == set()
    assert state.apply_update({"path": "/zzz", "body": {"a": 1}}) == set()
    assert state.apply_update({"path": "/htr/2/status", "body": "x"}) == set()
    assert state.apply_update({"body": {}}) == set()


def test_device_level_updates():
    """connected, power_limit and away_status frames update the device."""
    state = DeviceState()
    state.apply_dev_data(DEV_DATA)
    assert state.apply_update(
        {"path": "/connected", "body": {"connected": False}}
    )
    assert state.apply_update(
        {"path": "/htr_system/power_limit", "body": {"power_limit": 2000}}
    )
    assert state.apply_update(
        {"path": "/mgr/away_status", "body": {"away": True}}
    )
    device = state.snapshot().device
    assert device["connected"] is False
    assert device["power_limit"] == 2000
    assert device["away_status"] == {
        "enabled": False,
        "away": True,
        "forced": False,
    }
    assert state.apply_update({"path": "/connected", "body": {}}) == set()


def test_update_before_dev_data_creates_node():
    """An update that races ahead of dev_data still lands in the tree."""
    state = DeviceState()
    state.apply_update({"path": "/htr/3/status", "body": {"mode": "off"}})
    assert state.snapshot().nodes[("htr", 3)]["status"] == {"mode": "off"}


def test_malformed_dev_data_node_is_skipped():
    """A node without type/addr is ignored, the rest still load."""
    state = DeviceState()
    state.apply_dev_data(
        {"nodes": [{"status": {}}, {"type": "htr", "addr": 1}]}
    )
    assert list(state.snapshot().nodes) == [("htr", 1)]


def test_snapshot_is_a_copy_with_structural_equality():
    """Mutating a snapshot never leaks into the replica."""
    state = DeviceState()
    state.apply_dev_data(DEV_DATA)
    first = state.snapshot()
    first.nodes[("htr", 2)]["status"]["stemp"] = "99"
    assert state.snapshot().nodes[("htr", 2)]["status"]["stemp"] == "21.0"
    assert state.snapshot() == state.snapshot()
    before = state.snapshot()
    state.apply_update({"path": "/htr/2/status", "body": {"stemp": "22.0"}})
    assert state.snapshot() != before


def test_observers_called_only_on_change_and_isolated():
    """Observers get (snapshot, changed); a raising one blocks no other."""
    state = DeviceState()
    bad = MagicMock(side_effect=RuntimeError("boom"))
    good = MagicMock()
    state.subscribe(bad)
    state.subscribe(good)
    state.subscribe(good)  # deduplicated
    state.apply_dev_data(DEV_DATA)
    good.assert_called_once()
    snapshot, changed = good.call_args.args
    assert ("htr", 2, "setup") in changed
    assert snapshot.nodes[("htr", 2)]["setup"] == {"units": "C"}
    good.reset_mock()
    state.apply_dev_data(DEV_DATA)
    good.assert_not_called()


def test_update_manager_feeds_state(update_manager):
    """UpdateManager.state is created lazily and fed by both callbacks."""
    manager = update_manager
    manager._dev_data_cb(DEV_DATA)  # before access: not replayed
    state = manager.state
    assert manager.state is state
    assert state.snapshot().nodes == {}
    manager._dev_data_cb(DEV_DATA)
    manager._update_cb({"path": "/htr/2/status", "body": {"mode": "off"}})
    assert state.snapshot().nodes[("htr", 2)]["status"]["mode"] == "off"
