import pytest
from unittest.mock import AsyncMock, patch
from tests.common import fake_get_request


@pytest.mark.asyncio
async def test_get_homes(async_smartbox_session):
    with patch.object(
        async_smartbox_session, "_api_request", new_callable=AsyncMock
    ) as mock_api_request:
        url = "grouped_devs"
        mock_api_request.return_value = await fake_get_request(mock_api_request, url)
        homes = await async_smartbox_session.get_homes()
        assert homes == mock_api_request.return_value
        mock_api_request.assert_called_once_with(url)


@pytest.mark.asyncio
async def test_get_grouped_devices(async_smartbox_session):
    with patch.object(
        async_smartbox_session, "_api_request", new_callable=AsyncMock
    ) as mock_api_request:
        url = "grouped_devs"
        mock_api_request.return_value = await fake_get_request(mock_api_request, url)
        grouped_devices = await async_smartbox_session.get_grouped_devices()
        assert grouped_devices == mock_api_request.return_value
        mock_api_request.assert_called_once_with(url)


@pytest.mark.asyncio
async def test_get_nodes(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        with patch.object(
            async_smartbox_session, "_api_request", new_callable=AsyncMock
        ) as mock_api_request:
            url = f"devs/{mock_device['dev_id']}/mgr/nodes"
            mock_api_request.return_value = await fake_get_request(
                mock_api_request, url
            )
            nodes = await async_smartbox_session.get_nodes(
                device_id=mock_device["dev_id"]
            )
            assert nodes == mock_api_request.return_value["nodes"]
            mock_api_request.assert_called_with(url)


@pytest.mark.asyncio
async def test_get_node_status(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        for mock_node in await async_smartbox_session.get_nodes(mock_device["dev_id"]):
            with patch.object(
                async_smartbox_session, "_api_request", new_callable=AsyncMock
            ) as mock_api_request:
                url = f'devs/{mock_device["dev_id"]}/{mock_node["type"]}/{mock_node["addr"]}/status'
                mock_api_request.return_value = await fake_get_request(
                    mock_api_request, url
                )
                status = await async_smartbox_session.get_node_status(
                    mock_device["dev_id"], mock_node
                )
                assert status == mock_api_request.return_value
                mock_api_request.assert_called_with(url)


@pytest.mark.asyncio
async def test_get_node_samples(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        for mock_node in await async_smartbox_session.get_nodes(mock_device["dev_id"]):
            with patch.object(
                async_smartbox_session, "_api_request", new_callable=AsyncMock
            ) as mock_api_request:
                start_time = 1737722209
                end_time = 1737729409
                url = f'devs/{mock_device["dev_id"]}/{mock_node["type"]}/{mock_node["addr"]}/samples'

                mock_api_request.return_value = await fake_get_request(
                    mock_api_request, url
                )
                samples = await async_smartbox_session.get_node_samples(
                    mock_device["dev_id"],
                    mock_node,
                    start_time=start_time,
                    end_time=end_time,
                )
                assert samples == mock_api_request.return_value
                mock_api_request.assert_called_with(
                    f"{url}?start={start_time}&end={end_time}"
                )


@pytest.mark.asyncio
async def test_get_device_away_status(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        with patch.object(
            async_smartbox_session, "_api_request", new_callable=AsyncMock
        ) as mock_api_request:
            url = f"devs/{mock_device['dev_id']}/mgr/away_status"
            mock_api_request.return_value = await fake_get_request(
                mock_api_request, url
            )
            nodes = await async_smartbox_session.get_device_away_status(
                device_id=mock_device["dev_id"]
            )
            assert nodes == mock_api_request.return_value
            mock_api_request.assert_called_with(url)


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


@pytest.mark.asyncio
async def test_get_device_power_limit(async_smartbox_session):
    with patch.object(
        async_smartbox_session, "_api_request", new_callable=AsyncMock
    ) as mock_api_request:
        mock_api_request.return_value = {"power_limit": "100"}
        power_limit = await async_smartbox_session.get_device_power_limit(
            device_id="test_device"
        )
        assert power_limit == 100
        mock_api_request.assert_called_once_with(
            "devs/test_device/htr_system/power_limit"
        )


@pytest.mark.asyncio
async def test_set_device_power_limit(async_smartbox_session):
    with patch.object(
        async_smartbox_session, "_api_post", new_callable=AsyncMock
    ) as mock_api_post:
        mock_api_post.return_value = {}
        power_limit = 100
        await async_smartbox_session.set_device_power_limit(
            device_id="test_device", power_limit=power_limit
        )
        mock_api_post.assert_called_once_with(
            data={"power_limit": str(power_limit)},
            path="devs/test_device/htr_system/power_limit",
        )


def test_session_get_homes(session):
    with patch.object(
        session._async, "get_homes", new_callable=AsyncMock
    ) as mock_get_homes:
        mock_get_homes.return_value = []
        homes = session.get_homes()
        assert homes == []
        mock_get_homes.assert_called_once()


def test_session_get_grouped_devices(session):
    with patch.object(
        session._async, "get_grouped_devices", new_callable=AsyncMock
    ) as mock_get_grouped_devices:
        mock_get_grouped_devices.return_value = []
        grouped_devices = session.get_grouped_devices()
        assert grouped_devices == []
        mock_get_grouped_devices.assert_called_once()


def test_session_get_nodes(session):
    with patch.object(
        session._async, "get_nodes", new_callable=AsyncMock
    ) as mock_get_nodes:
        mock_get_nodes.return_value = []
        nodes = session.get_nodes(device_id="test_device")
        assert nodes == []
        mock_get_nodes.assert_called_once_with(device_id="test_device")


def test_session_get_device_away_status(session):
    with patch.object(
        session._async, "get_device_away_status", new_callable=AsyncMock
    ) as mock_get_device_away_status:
        mock_get_device_away_status.return_value = {}
        away_status = session.get_device_away_status(device_id="test_device")
        assert away_status == {}
        mock_get_device_away_status.assert_called_once_with(device_id="test_device")


def test_session_set_device_away_status(session):
    with patch.object(
        session._async, "set_device_away_status", new_callable=AsyncMock
    ) as mock_set_device_away_status:
        mock_set_device_away_status.return_value = {}
        status_args = {"status": "away"}
        result = session.set_device_away_status(
            device_id="test_device", status_args=status_args
        )
        assert result == {}
        mock_set_device_away_status.assert_called_once_with(
            device_id="test_device", status_args=status_args
        )


def test_session_get_device_power_limit(session):
    with patch.object(
        session._async, "get_device_power_limit", new_callable=AsyncMock
    ) as mock_get_device_power_limit:
        mock_get_device_power_limit.return_value = 100
        power_limit = session.get_device_power_limit(device_id="test_device")
        assert power_limit == 100
        mock_get_device_power_limit.assert_called_once_with(device_id="test_device")


def test_session_set_device_power_limit(session):
    with patch.object(
        session._async, "set_device_power_limit", new_callable=AsyncMock
    ) as mock_set_device_power_limit:
        mock_set_device_power_limit.return_value = None
        power_limit = 100
        session.set_device_power_limit(device_id="test_device", power_limit=power_limit)
        mock_set_device_power_limit.assert_called_once_with(
            device_id="test_device", power_limit=power_limit
        )


def test_session_get_devices(session):
    with patch.object(
        session._async, "get_devices", new_callable=AsyncMock
    ) as mock_get_devices:
        mock_get_devices.return_value = []
        devices = session.get_devices()
        assert devices == []
        mock_get_devices.assert_called_once()

    with patch.object(
        session._async, "get_devices", new_callable=AsyncMock
    ) as mock_get_devices:
        mock_get_devices.return_value = [
            {"id": "device1", "name": "Device 1"},
            {"id": "device2", "name": "Device 2"},
        ]
        devices = session.get_devices()
        assert devices == [
            {"id": "device1", "name": "Device 1"},
            {"id": "device2", "name": "Device 2"},
        ]
        mock_get_devices.assert_called_once()
