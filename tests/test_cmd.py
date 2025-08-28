import asyncio

import pytest

from smartbox.cmd import smartbox

DEFAULT_ARGS = [
    "-a",
    "test_api",
    "-u",
    "user",
    "-p",
    "pass",
]


@pytest.mark.asyncio
async def test_health_check(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result("")
    mock_session.return_value.health_check.return_value = devices_future

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "health-check"],
    )
    assert result.exit_code == 0
    assert "" in result.output


@pytest.mark.asyncio
async def test_api_version(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result(
        {"major": "1", "minor": "53", "subminor": "2", "commit": "NULL"}
    )
    mock_session.return_value.api_version.return_value = devices_future

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "api-version"],
    )
    assert result.exit_code == 0
    assert "subminor" in result.output


@pytest.mark.asyncio
async def test_devices(runner, async_smartbox_session):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "devices"],
    )
    assert result.exit_code == 0
    assert "device1" in result.output


@pytest.mark.asyncio
async def test_homes(runner, async_smartbox_session):
    result = await runner.invoke(smartbox, [*DEFAULT_ARGS, "homes"])
    assert result.exit_code == 0
    assert "home1" in result.output


@pytest.mark.asyncio
async def test_nodes(runner, async_smartbox_session):
    result = await runner.invoke(smartbox, [*DEFAULT_ARGS, "nodes"])
    assert result.exit_code == 0
    assert "device1" in result.output
    assert "Smart Light" in result.output


@pytest.mark.asyncio
async def test_status(runner, async_smartbox_session):
    result = await runner.invoke(smartbox, [*DEFAULT_ARGS, "status"])
    assert result.exit_code == 0
    assert "ok" in result.output
    assert "true_radiant_active" in result.output


@pytest.mark.asyncio
async def test_device_away_status(runner, async_smartbox_session):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "device-away-status"],
    )
    assert result.exit_code == 0
    assert "away" in result.output


@pytest.mark.asyncio
async def test_socket(runner, mocker, mock_session):
    mock_socket_session = mocker.patch("smartbox.cmd.SocketSession")
    mock_socket_session.return_value.run.return_value = asyncio.Future()
    mock_socket_session.return_value.run.return_value.set_result(None)

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "socket", "-d", "1"],
    )
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_set_status(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    nodes_future = asyncio.Future()
    nodes_future.set_result([{"name": "Node1", "addr": 1}])
    mock_session.return_value.get_nodes.return_value = nodes_future

    set_node_status = asyncio.Future()
    set_node_status.set_result({"mode": "auto"})
    mock_session.return_value.set_node_status.return_value = set_node_status

    result = await runner.invoke(
        smartbox,
        [
            *DEFAULT_ARGS,
            "set-status",
            "-d",
            "1",
            "-n",
            "1",
            "--mode",
            "auto",
        ],
    )
    assert result.exit_code == 0
    mock_session.return_value.set_node_status.assert_called_once_with(
        "1",
        {"name": "Node1", "addr": 1},
        {"mode": "auto", "locked": None, "stemp": None, "units": None},
    )


@pytest.mark.asyncio
async def test_set_setup(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    nodes_future = asyncio.Future()
    nodes_future.set_result([{"name": "Node1", "addr": 1}])
    mock_session.return_value.get_nodes.return_value = nodes_future

    set_node_setup = asyncio.Future()
    set_node_setup.set_result({})
    mock_session.return_value.set_node_setup.return_value = set_node_setup

    result = await runner.invoke(
        smartbox,
        [
            *DEFAULT_ARGS,
            "set-setup",
            "-d",
            "1",
            "-n",
            "1",
            "--control-mode",
            "1",
            "--offset",
            "2",
            "--priority",
            "high",
            "--true-radiant-enabled",
            "true",
            "--units",
            "C",
            "--window-mode-enabled",
            "false",
        ],
    )
    assert result.exit_code == 0
    mock_session.return_value.set_node_setup.assert_called_once_with(
        "1",
        {"name": "Node1", "addr": 1},
        {
            "control_mode": 1,
            "offset": "2",
            "priority": "high",
            "true_radiant_enabled": True,
            "units": "C",
            "window_mode_enabled": False,
        },
    )


@pytest.mark.asyncio
async def test_set_device_away_status(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    set_away_status_future = asyncio.Future()
    set_away_status_future.set_result({})
    mock_session.return_value.set_device_away_status.return_value = (
        set_away_status_future
    )

    result = await runner.invoke(
        smartbox,
        [
            *DEFAULT_ARGS,
            "set-device-away-status",
            "-d",
            "1",
            "--away",
            "true",
            "--enabled",
            "true",
            "--forced",
            "false",
        ],
    )
    assert result.exit_code == 0
    mock_session.return_value.set_device_away_status.assert_called_once_with(
        "1",
        {"away": True, "enabled": True, "forced": False},
    )


@pytest.mark.asyncio
async def test_node_samples(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    nodes_future = asyncio.Future()
    nodes_future.set_result([{"name": "Node1", "addr": 1}])
    mock_session.return_value.get_nodes.return_value = nodes_future

    node_samples_future = asyncio.Future()
    node_samples_future.set_result({"samples": "data"})
    mock_session.return_value.get_node_samples.return_value = (
        node_samples_future
    )

    result = await runner.invoke(
        smartbox,
        [
            *DEFAULT_ARGS,
            "node-samples",
            "-d",
            "1",
            "-n",
            "1",
            "-s",
            "1609459200",
            "-e",
            "1609462800",
        ],
    )
    assert result.exit_code == 0
    assert "sample" in result.output
    mock_session.return_value.get_node_samples.assert_called_once_with(
        "1",
        {"name": "Node1", "addr": 1},
        1609459200,
        1609462800,
    )


@pytest.mark.asyncio
async def test_reseller(runner, mocker, reseller):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "resellers"],
    )
    assert result.exit_code == 0
    assert "test_api" in result.output
    assert "http" in result.output


@pytest.mark.asyncio
async def test_device_power_limit(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    power_limit_future = asyncio.Future()
    power_limit_future.set_result({"power_limit": 100})
    mock_session.return_value.get_device_power_limit.return_value = (
        power_limit_future
    )

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "device-power-limit"],
    )
    assert result.exit_code == 0
    assert "Device1" in result.output
    assert "power_limit" in result.output
    mock_session.return_value.get_devices.assert_called_once()
    mock_session.return_value.get_device_power_limit.assert_called_once_with(
        "1"
    )


@pytest.mark.asyncio
async def test_setup(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    nodes_future = asyncio.Future()
    nodes_future.set_result([{"name": "Node1", "addr": 1}])
    mock_session.return_value.get_nodes.return_value = nodes_future

    setup_future = asyncio.Future()
    setup_future.set_result({"setup": "data"})
    mock_session.return_value.get_node_setup.return_value = setup_future

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "setup"],
    )
    assert result.exit_code == 0
    assert "Device1" in result.output
    assert "Node1" in result.output
    assert "setup" in result.output
    mock_session.return_value.get_devices.assert_called_once()
    mock_session.return_value.get_nodes.assert_called_once_with("1")
    mock_session.return_value.get_node_setup.assert_called_once_with(
        "1", {"name": "Node1", "addr": 1}
    )


@pytest.mark.asyncio
async def test_set_device_power_limit(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    set_power_limit_future = asyncio.Future()
    set_power_limit_future.set_result(None)
    mock_session.return_value.set_device_power_limit.return_value = (
        set_power_limit_future
    )

    result = await runner.invoke(
        smartbox,
        [
            *DEFAULT_ARGS,
            "set-device-power-limit",
            "-d",
            "1",
            "100",
        ],
    )
    assert result.exit_code == 0
    mock_session.return_value.set_device_power_limit.assert_called_once_with(
        "1", 100
    )


@pytest.mark.asyncio
async def test_guests(runner, mock_session):
    guests_future = asyncio.Future()
    guests_future.set_result(
        [{"email": "guest1@example.com"}, {"email": "guest2@example.com"}]
    )
    mock_session.return_value.get_home_guests.return_value = guests_future

    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "guests", "-h", "home1"],
    )
    assert result.exit_code == 0
    assert "guest1@example.com" in result.output
    assert "guest2@example.com" in result.output
    mock_session.return_value.get_home_guests.assert_called_once_with(
        home_id="home1"
    )


@pytest.mark.asyncio
async def test_device_connected_status(runner, async_smartbox_session):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "device-connected-status"],
    )
    assert result.exit_code == 0
    assert "connected" in result.output
