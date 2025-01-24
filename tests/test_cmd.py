import asyncio
import pytest

from smartbox.cmd import smartbox
from unittest.mock import patch, AsyncMock


DEFAULT_ARGS = [
    "-a",
    "api",
    "-b",
    "creds",
    "-u",
    "user",
    "-p",
    "pass",
]


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
async def test_setup(runner, async_smartbox_session):
    result = await runner.invoke(smartbox, [*DEFAULT_ARGS, "setup"])
    assert result.exit_code == 0
    assert "factory_options" in result.output


@pytest.mark.asyncio
async def test_device_away_status(runner, async_smartbox_session):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "device-away-status"],
    )
    assert result.exit_code == 0
    assert "away" in result.output


@pytest.mark.asyncio
async def test_set_device_away_status(async_smartbox_session):
    with patch.object(
        async_smartbox_session, "_api_post", new_callable=AsyncMock
    ) as mock_api_post:
        mock_api_post.return_value = {}
        status_args = {"status": "away"}
        result = await async_smartbox_session.set_device_away_status(
            device_id="test_device", status_args=status_args
        )
        assert result == {}
        mock_api_post.assert_called_once_with(
            data=status_args, path="devs/test_device/mgr/away_status"
        )

    with patch.object(
        async_smartbox_session, "_api_post", new_callable=AsyncMock
    ) as mock_api_post:
        mock_api_post.return_value = {"status": "home"}
        status_args = {"status": "home"}
        result = await async_smartbox_session.set_device_away_status(
            device_id="test_device", status_args=status_args
        )
        assert result == {"status": "home"}
        mock_api_post.assert_called_once_with(
            data=status_args, path="devs/test_device/mgr/away_status"
        )

    with patch.object(
        async_smartbox_session, "_api_post", new_callable=AsyncMock
    ) as mock_api_post:
        mock_api_post.return_value = {"status": "away"}
        status_args = {"status": "away", "extra": None}
        result = await async_smartbox_session.set_device_away_status(
            device_id="test_device", status_args=status_args
        )
        assert result == {"status": "away"}
        mock_api_post.assert_called_once_with(
            data={"status": "away"}, path="devs/test_device/mgr/away_status"
        )


@pytest.mark.asyncio
async def test_device_power_limit(runner, async_smartbox_session):
    result = await runner.invoke(
        smartbox,
        [*DEFAULT_ARGS, "device-power-limit"],
    )
    assert result.exit_code == 0
    assert "100" in result.output


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
async def test_set_device_power_limit(runner, mock_session):
    devices_future = asyncio.Future()
    devices_future.set_result([{"name": "Device1", "dev_id": "1"}])
    mock_session.return_value.get_devices.return_value = devices_future

    set_power_limit_future = asyncio.Future()
    set_power_limit_future.set_result({})
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
    mock_session.return_value.set_device_power_limit.assert_called_once_with("1", 100)
