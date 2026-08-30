import datetime
import json
import logging
import math
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import ClientSession
from pydantic import ValidationError
import pytest

from smartbox import APIUnavailableError, InvalidAuthError, SmartboxError
from smartbox.models import DefaultNodeSetup
from smartbox.session import (
    _DEFAULT_BACKOFF_FACTOR,
    _DEFAULT_RETRY_ATTEMPTS,
    AsyncSession,
)
from tests.common import fake_get_request


@pytest.mark.asyncio
async def test_get_grouped_devices(async_smartbox_session):
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        url = "grouped_devs"
        mock_api_request.return_value = await fake_get_request(
            mock_api_request,
            url,
        )
        grouped_devices = await async_smartbox_session.get_grouped_devices()
        assert grouped_devices == mock_api_request.return_value
        mock_api_request.assert_called_once_with(url)
        async_smartbox_session.raw_response = False
        grouped_devices_model = (
            await async_smartbox_session.get_grouped_devices()
        )
        assert grouped_devices_model.root[0].name == grouped_devices[0]["name"]


@pytest.mark.asyncio
async def test_get_nodes(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        with patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
        ) as mock_api_request:
            url = f"devs/{mock_device['dev_id']}/mgr/nodes"
            mock_api_request.return_value = await fake_get_request(
                mock_api_request,
                url,
            )
            async_smartbox_session.raw_response = True
            nodes = await async_smartbox_session.get_nodes(
                device_id=mock_device["dev_id"],
            )
            assert nodes == mock_api_request.return_value["nodes"]
            mock_api_request.assert_called_with(url)
            async_smartbox_session.raw_response = False
            nodes_model = await async_smartbox_session.get_nodes(
                device_id=mock_device["dev_id"],
            )
            assert nodes_model[0].addr == nodes[0]["addr"]


@pytest.mark.asyncio
async def test_get_node_status(async_smartbox_session, caplog):
    for mock_device in await async_smartbox_session.get_devices():
        for mock_node in await async_smartbox_session.get_nodes(
            mock_device["dev_id"],
        ):
            with patch.object(
                async_smartbox_session,
                "_api_request",
                new_callable=AsyncMock,
            ) as mock_api_request:
                url = f"devs/{mock_device['dev_id']}/{mock_node['type']}/{mock_node['addr']}/status"
                mock_api_request.return_value = await fake_get_request(
                    mock_api_request,
                    url,
                )
                status = await async_smartbox_session.get_node_status(
                    mock_device["dev_id"],
                    mock_node,
                )
                assert status == mock_api_request.return_value
                mock_api_request.assert_called_with(url)

                async_smartbox_session.raw_response = False
                if mock_node["type"] != "pmo":
                    status_model = await async_smartbox_session.get_node_status(
                        mock_device["dev_id"],
                        mock_node,
                    )
                    assert status_model.act_duty == status["act_duty"]
                with pytest.raises(ValidationError):
                    mock_api_request.return_value = {
                        "sync_status": "synced",
                        "mode": "auto",
                    }
                    await async_smartbox_session.get_node_status(
                        mock_device["dev_id"],
                        mock_node,
                    )
                async_smartbox_session.raw_response = True
                assert "Status config validation error" in caplog.text


@pytest.mark.asyncio
async def test_get_node_samples(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        for mock_node in await async_smartbox_session.get_nodes(
            mock_device["dev_id"],
        ):
            with patch.object(
                async_smartbox_session,
                "_api_request",
                new_callable=AsyncMock,
            ) as mock_api_request:
                start_time = 1737722209
                end_time = 1737729409
                url = f"devs/{mock_device['dev_id']}/{mock_node['type']}/{mock_node['addr']}/samples"

                mock_api_request.return_value = await fake_get_request(
                    mock_api_request,
                    url,
                )
                samples = await async_smartbox_session.get_node_samples(
                    mock_device["dev_id"],
                    mock_node,
                    start_time=start_time,
                    end_time=end_time,
                )
                assert samples == mock_api_request.return_value
                mock_api_request.assert_called_with(
                    f"{url}?start={start_time}&end={end_time}",
                )

                async_smartbox_session.raw_response = False
                samples_model = await async_smartbox_session.get_node_samples(
                    mock_device["dev_id"],
                    mock_node,
                    start_time=start_time,
                    end_time=end_time,
                )
                assert samples_model.samples[0].counter == float(
                    samples["samples"][0]["counter"]
                )
                async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_get_node_samples_default_times(async_smartbox_session):
    """Default start and end times should be 1 hour before and after current time."""
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "htr",
        "installed": True,
        "lost": False,
    }

    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        mock_api_request.return_value = {"samples": []}
        now = int(time.time())
        await async_smartbox_session.get_node_samples(
            device_id=mock_device_id,
            node=mock_node,
        )
        called_url = mock_api_request.call_args[0][0]

        assert called_url.startswith(
            f"devs/{mock_device_id}/{mock_node['type']}/{mock_node['addr']}/samples?start="
        )

        match = re.search(r"start=(\d+)&end=(\d+)", called_url)
        assert match is not None, (
            "Start and end date not present in url"
        )

        called_start = int(match.group(1))
        called_end = int(match.group(2))

        assert abs(called_start - (now - 3600)) <= 20
        assert abs(called_end - (now + 3600)) <= 20


@pytest.mark.asyncio
async def test_get_device_away_status(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        with patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
        ) as mock_api_request:
            url = f"devs/{mock_device['dev_id']}/mgr/away_status"
            mock_api_request.return_value = await fake_get_request(
                mock_api_request,
                url,
            )
            nodes = await async_smartbox_session.get_device_away_status(
                device_id=mock_device["dev_id"],
            )
            assert nodes == mock_api_request.return_value
            mock_api_request.assert_called_with(url)

            async_smartbox_session.raw_response = False
            nodes_model = await async_smartbox_session.get_device_away_status(
                device_id=mock_device["dev_id"],
            )
            assert nodes_model.away == nodes["away"]
            async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_set_device_away_status(async_smartbox_session):
    with patch.object(
        async_smartbox_session,
        "_api_post",
        new_callable=AsyncMock,
    ) as mock_api_post:
        mock_api_post.return_value = {}
        status_args = {"status": "away"}
        result = await async_smartbox_session.set_device_away_status(
            device_id="test_device",
            status_args=status_args,
        )
        assert result is None
        mock_api_post.assert_called_once_with(
            data=status_args,
            path="devs/test_device/mgr/away_status",
        )


@pytest.mark.asyncio
async def test_get_device_power_limit(async_smartbox_session):
    power = 100
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "pmo",
        "installed": True,
        "lost": False,
    }
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        mock_api_request.return_value = {"power_limit": "100"}
        power_limit = await async_smartbox_session.get_device_power_limit(
            device_id="test_device",
        )
        assert power_limit == power
        mock_api_request.assert_called_once_with(
            "devs/test_device/htr_system/power_limit",
        )

        mock_api_request.return_value = {"power": "100"}
        power_limit = await async_smartbox_session.get_device_power_limit(
            device_id="test_device", node=mock_node
        )
        assert power_limit == power
        mock_api_request.assert_called_with(
            "devs/test_device/pmo/1/power",
        )


@pytest.mark.asyncio
async def test_set_device_power_limit(async_smartbox_session):
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "pmo",
        "installed": True,
        "lost": False,
    }
    with patch.object(
        async_smartbox_session,
        "_api_post",
        new_callable=AsyncMock,
    ) as mock_api_post:
        mock_api_post.return_value = {}
        power_limit = 100
        await async_smartbox_session.set_device_power_limit(
            device_id="test_device",
            power_limit=power_limit,
        )
        mock_api_post.assert_called_once_with(
            data={"power_limit": str(power_limit)},
            path="devs/test_device/htr_system/power_limit",
        )

        await async_smartbox_session.set_device_power_limit(
            device_id="test_device",
            node=mock_node,
            power_limit=power_limit,
        )
        mock_api_post.assert_called_with(
            data={"power_limit": str(power_limit)},
            path="devs/test_device/pmo/1/power_limit",
        )


def test_session_get_homes(session):
    with patch.object(
        session._async,
        "get_homes",
        new_callable=AsyncMock,
    ) as mock_get_homes:
        mock_get_homes.return_value = []
        homes = session.get_homes()
        assert homes == []
        mock_get_homes.assert_called_once()


def test_session_get_grouped_devices(session):
    with patch.object(
        session._async,
        "get_grouped_devices",
        new_callable=AsyncMock,
    ) as mock_get_grouped_devices:
        mock_get_grouped_devices.return_value = []
        grouped_devices = session.get_grouped_devices()
        assert grouped_devices == []
        mock_get_grouped_devices.assert_called_once()


def test_session_get_nodes(session):
    with patch.object(
        session._async,
        "get_nodes",
        new_callable=AsyncMock,
    ) as mock_get_nodes:
        mock_get_nodes.return_value = []
        nodes = session.get_nodes(device_id="test_device")
        assert nodes == []
        mock_get_nodes.assert_called_once_with(device_id="test_device")


def test_session_get_device_away_status(session):
    with patch.object(
        session._async,
        "get_device_away_status",
        new_callable=AsyncMock,
    ) as mock_get_device_away_status:
        mock_get_device_away_status.return_value = {}
        away_status = session.get_device_away_status(device_id="test_device")
        assert away_status == {}
        mock_get_device_away_status.assert_called_once_with(
            device_id="test_device",
        )


def test_session_set_device_away_status(session):
    with patch.object(
        session._async,
        "set_device_away_status",
        new_callable=AsyncMock,
    ) as mock_set_device_away_status:
        mock_set_device_away_status.return_value = {}
        status_args = {"status": "away"}
        result = session.set_device_away_status(
            device_id="test_device",
            status_args=status_args,
        )
        assert result == {}
        mock_set_device_away_status.assert_called_once_with(
            device_id="test_device",
            status_args=status_args,
        )


def test_session_get_device_power_limit(session):
    power = 100
    with patch.object(
        session._async,
        "get_device_power_limit",
        new_callable=AsyncMock,
    ) as mock_get_device_power_limit:
        mock_get_device_power_limit.return_value = power
        power_limit = session.get_device_power_limit(device_id="test_device")
        assert power_limit == power
        mock_get_device_power_limit.assert_called_once_with(
            device_id="test_device",
        )


def test_session_set_device_power_limit(session):
    with patch.object(
        session._async,
        "set_device_power_limit",
        new_callable=AsyncMock,
    ) as mock_set_device_power_limit:
        mock_set_device_power_limit.return_value = None
        power_limit = 100
        session.set_device_power_limit(
            device_id="test_device",
            power_limit=power_limit,
        )
        mock_set_device_power_limit.assert_called_once_with(
            device_id="test_device",
            power_limit=power_limit,
        )


def test_session_get_devices(session):
    with patch.object(
        session._async,
        "get_devices",
        new_callable=AsyncMock,
    ) as mock_get_devices:
        mock_get_devices.return_value = []
        devices = session.get_devices()
        assert devices == []
        mock_get_devices.assert_called_once()

    with patch.object(
        session._async,
        "get_devices",
        new_callable=AsyncMock,
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


@pytest.mark.asyncio
async def test_set_node_status(async_smartbox_session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "htr",
        "installed": True,
        "lost": False,
    }
    status_args = {"status": "active"}

    with patch.object(
        async_smartbox_session,
        "_api_post",
        new_callable=AsyncMock,
    ) as mock_api_post:
        mock_api_post.return_value = None
        result = await async_smartbox_session.set_node_status(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )
        assert result is None
        mock_api_post.assert_called_once_with(
            data=status_args,
            path=f"devs/{mock_device_id}/{mock_node['type']}/{mock_node['addr']}/status",
        )

    # Test with temperature field without units
    status_args = {"stemp": 25}
    with pytest.raises(
        ValueError,
        match="Must supply unit with temperature fields",
    ):
        await async_smartbox_session.set_node_status(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )


def test_session_set_node_status(session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
    }
    status_args = {"status": "active"}

    with patch.object(
        session._async,
        "set_node_status",
        new_callable=AsyncMock,
    ) as mock_set_node_status:
        mock_set_node_status.return_value = {}
        result = session.set_status(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )
        assert result == {}
        mock_set_node_status.assert_called_once_with(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )

    # Test with temperature field without units
    status_args = {"stemp": 25}
    with patch.object(
        session._async,
        "set_node_status",
        new_callable=AsyncMock,
    ) as mock_set_node_status:
        mock_set_node_status.side_effect = ValueError(
            "Must supply unit with temperature fields",
        )
        with pytest.raises(
            ValueError,
            match="Must supply unit with temperature fields",
        ):
            session.set_status(
                device_id=mock_device_id,
                node=mock_node,
                status_args=status_args,
            )


def test_session_get_setup(session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
    }

    with patch.object(
        session._async,
        "get_node_setup",
        new_callable=AsyncMock,
    ) as mock_get_node_setup:
        mock_get_node_setup.return_value = {}
        setup = session.get_setup(device_id=mock_device_id, node=mock_node)
        assert setup == {}
        mock_get_node_setup.assert_called_once_with(
            device_id=mock_device_id,
            node=mock_node,
        )


def test_session_set_status(session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
    }
    status_args = {"status": "active"}

    with patch.object(
        session._async,
        "set_node_status",
        new_callable=AsyncMock,
    ) as mock_set_node_status:
        mock_set_node_status.return_value = {"status": "active"}
        result = session.set_status(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )
        assert result == {"status": "active"}
        mock_set_node_status.assert_called_once_with(
            device_id=mock_device_id,
            node=mock_node,
            status_args=status_args,
        )

    # Test with temperature field without units
    status_args = {"stemp": 25}
    with patch.object(
        session._async,
        "set_node_status",
        new_callable=AsyncMock,
    ) as mock_set_node_status:
        mock_set_node_status.side_effect = ValueError(
            "Must supply unit with temperature fields",
        )
        with pytest.raises(
            ValueError,
            match="Must supply unit with temperature fields",
        ):
            session.set_status(
                device_id=mock_device_id,
                node=mock_node,
                status_args=status_args,
            )


@pytest.mark.asyncio
async def test_set_node_setup(async_smartbox_session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "acm",
        "installed": True,
        "lost": False,
    }
    setup_args = {"setting1": "value1"}

    with (
        patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
        ) as mock_api_request,
        patch.object(
            async_smartbox_session,
            "_api_post",
            new_callable=AsyncMock,
        ) as mock_api_post,
    ):
        # The raw setup carries a key the Pydantic models do not declare
        # (``counter_offset``). It must survive the read-modify-write cycle and
        # be re-posted unchanged, otherwise it would be wiped on the device.
        mock_api_request.return_value = {
            "setting2": "value2",
            "counter_offset": 42,
        }
        mock_api_post.return_value = None

        result = await async_smartbox_session.set_node_setup(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )
        assert result is None
        setup_path = (
            f"devs/{mock_device_id}/{mock_node['type']}/"
            f"{mock_node['addr']}/setup"
        )
        mock_api_request.assert_awaited_once_with(setup_path)
        mock_api_post.assert_called_once_with(
            data={
                "setting1": "value1",
                "setting2": "value2",
                "counter_offset": 42,
            },
            path=setup_path,
        )

        # The internal read stays raw even when the session is in typed mode.
        mock_api_request.reset_mock()
        mock_api_post.reset_mock()
        async_smartbox_session.raw_response = False
        mock_api_request.return_value = {"counter_offset": 7}

        result = await async_smartbox_session.set_node_setup(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )
        assert result is None
        mock_api_post.assert_called_once_with(
            data={"setting1": "value1", "counter_offset": 7},
            path=setup_path,
        )
        async_smartbox_session.raw_response = True


def test_session_set_node_setup(session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
    }
    setup_args = {"setting1": "value1"}

    with patch.object(
        session._async,
        "set_node_setup",
        new_callable=AsyncMock,
    ) as mock_set_node_setup:
        mock_set_node_setup.return_value = {
            "setting1": "value1",
            "setting2": "value2",
        }
        result = session.set_setup(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )
        assert result == {"setting1": "value1", "setting2": "value2"}
        mock_set_node_setup.assert_called_once_with(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )


@pytest.mark.asyncio
async def test_async_session_init():
    api_name = "test_api"
    basic_auth_credentials = "test_credentials"
    username = "test_user"
    password = "test_password"
    retry_attempts = 3
    backoff_factor = 0.2
    serial_id = 10
    referer = "http"
    websession = ClientSession()

    session = AsyncSession(
        api_name=api_name,
        basic_auth_credentials=basic_auth_credentials,
        username=username,
        password=password,
        websession=websession,
        retry_attempts=retry_attempts,
        backoff_factor=backoff_factor,
        x_serial_id=serial_id,
        x_referer=referer,
    )

    assert session.api_name == api_name
    assert session.api_host == f"https://{api_name}.helki.com"
    assert session._basic_auth_credentials == basic_auth_credentials
    assert session._retry_attempts == retry_attempts
    assert math.isclose(session._backoff_factor, backoff_factor)
    assert session._username == username
    assert session._password == password
    assert session._access_token == ""
    assert session._client_session == websession
    assert session._headers["x-serialid"] == str(serial_id)
    assert session._headers["x-referer"] == referer
    await websession.close()


@pytest.mark.asyncio
async def test_async_session_init_defaults(reseller):
    api_name = "test_api"
    username = "test_user"
    password = "test_password"

    session = AsyncSession(
        api_name=api_name,
        username=username,
        password=password,
    )

    assert session.api_name == api_name
    assert session._api_host == f"https://{api_name}.helki.com"
    assert session._retry_attempts == _DEFAULT_RETRY_ATTEMPTS
    assert session._backoff_factor == _DEFAULT_BACKOFF_FACTOR
    assert session._username == username
    assert session._password == password
    assert session._access_token == ""
    assert session._client_session is None
    assert "x-serialid" in session._headers
    assert "x-referer" in session._headers


@pytest.mark.asyncio
async def test_authentication_success(async_session, caplog):
    credentials = {
        "grant_type": "password",
        "username": "test_user",
        "password": "test_password",
    }
    token_response = {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_in": 5,
        "token_type": "test_token_type",
    }

    with patch.object(
        async_session.client,
        "post",
    ) as mock_post:
        mock_response = MagicMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value=token_response)
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        with caplog.at_level(logging.WARNING, logger="smartbox.session"):
            await async_session._authentication(credentials)

        assert async_session._access_token == "test_access_token"
        assert async_session.access_token == "test_access_token"
        assert async_session._refresh_token == "test_refresh_token"
        assert async_session.refresh_token == "test_refresh_token"
        assert async_session._expires_at > datetime.datetime.now(datetime.UTC)

        assert "below minimum lifetime" in caplog.text
        assert "will refresh again on next operation" in caplog.text

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-referer": "http",
                "x-serialid": "10",
            },
            data=credentials,
        )


@pytest.mark.asyncio
async def test_authentication_invalid_response(async_session):
    credentials = {
        "grant_type": "password",
        "username": "test_user",
        "password": "test_password",
    }
    invalid_response = {"invalid_key": "invalid_value"}

    with patch.object(
        async_session.client,
        "post",
    ) as mock_post:
        mock_response = MagicMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value=invalid_response)
        mock_response.raise_for_status = MagicMock()

        mock_post.return_value = mock_response

        with pytest.raises(
            InvalidAuthError,
            match="Received invalid auth response",
        ):
            await async_session._authentication(credentials)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.reseller.serial_id}",
                "x-referer": f"{async_session.reseller.web_url}",
            },
            data=credentials,
        )


@pytest.mark.asyncio
async def test_authentication_client_response_error(async_session):
    credentials = {
        "grant_type": "password",
        "username": "test_user",
        "password": "test_password",
    }

    with patch.object(
        async_session.client,
        "post",
    ) as mock_post:
        mock_post.side_effect = aiohttp.ClientResponseError(
            request_info=None,
            history=None,
            status=401,
            message="Unauthorized",
        )

        with pytest.raises(InvalidAuthError):
            await async_session._authentication(credentials)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.reseller.serial_id}",
                "x-referer": f"{async_session.reseller.web_url}",
            },
            data=credentials,
        )


@pytest.mark.asyncio
async def test_authentication_client_response_unavailable(async_session):
    credentials = {
        "grant_type": "password",
        "username": "test_user",
        "password": "test_password",
    }

    with patch.object(
        async_session.client,
        "post",
    ) as mock_post:
        mock_post.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session._authentication(credentials)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.reseller.serial_id}",
                "x-referer": f"{async_session.reseller.web_url}",
            },
            data=credentials,
        )


@pytest.mark.asyncio
async def test_health_check_success(async_session):
    with patch.object(
        async_session.client,
        "get",
    ) as mock_get:
        mock_response = MagicMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value={"status": "ok"})
        mock_response.raise_for_status = MagicMock()

        mock_get.return_value = mock_response

        result = await async_session.health_check()
        assert result == {"status": "ok"}
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/health_check",
        )


@pytest.mark.asyncio
async def test_health_check_api_unavailable(async_session):
    with patch.object(
        async_session.client,
        "get",
    ) as mock_get:
        mock_get.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session.health_check()

        mock_get.assert_called_once_with(
            f"{async_session._api_host}/health_check",
        )


@pytest.mark.asyncio
async def test_api_version_success(async_session):
    with patch.object(
        async_session.client,
        "get",
    ) as mock_get:
        mock_response = AsyncMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None
        mock_response.json.return_value = {
            "major": "1",
            "minor": "53",
            "subminor": "2",
            "commit": "NULL",
        }

        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = await async_session.api_version()
        assert result == {
            "major": "1",
            "minor": "53",
            "subminor": "2",
            "commit": "NULL",
        }
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/version",
        )


@pytest.mark.asyncio
async def test_api_version_unavailable(async_session):
    with patch.object(
        async_session.client,
        "get",
    ) as mock_get:
        mock_get.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session.api_version()

        mock_get.assert_called_once_with(
            f"{async_session._api_host}/version",
        )


@pytest.mark.asyncio
async def test_api_request_success(async_session):
    path = "test_path"
    expected_response = {"key": "value"}

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "get",
        ) as mock_get,
    ):
        mock_response = AsyncMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None
        mock_response.json.return_value = expected_response
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = await async_session._api_request(path)
        mock_check_refresh_auth.assert_called_once()
        assert result == expected_response
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_request_check_refresh_auth_called(async_session):
    path = "test_path"

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "get",
        ) as mock_get,
    ):
        mock_response = MagicMock()

        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value={})
        mock_response.raise_for_status = MagicMock()

        mock_get.return_value = mock_response

        await async_session._api_request(path)
        mock_check_refresh_auth.assert_called_once()
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_request_client_connection_error(async_session):
    path = "test_path"

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "get",
        ) as mock_get,
    ):
        mock_get.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session._api_request(path)

        mock_check_refresh_auth.assert_called_once()
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_request_client_response_error(async_session):
    path = "test_path"

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "get",
        ) as mock_get,
    ):
        mock_get.side_effect = aiohttp.ClientResponseError(
            request_info=None,
            history=None,
            status=500,
            message="Internal Server Error",
        )

        with pytest.raises(SmartboxError):
            await async_session._api_request(path)

        mock_check_refresh_auth.assert_called_once()
        mock_get.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_post_success(async_session):
    path = "test_path"
    data = {"key": "value"}
    expected_response = {"response_key": "response_value"}

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "post",
        ) as mock_post,
    ):
        mock_response = MagicMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value=expected_response)
        mock_response.raise_for_status = MagicMock()

        mock_post.return_value = mock_response

        result = await async_session._api_post(data, path)
        mock_check_refresh_auth.assert_called_once()
        assert result == expected_response
        mock_post.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            data=json.dumps(data),
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_post_check_refresh_auth_called(async_session):
    path = "test_path"
    data = {"key": "value"}

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "post",
        ) as mock_post,
    ):
        mock_response = MagicMock()
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        mock_response.json = AsyncMock(return_value={})
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        await async_session._api_post(data, path)
        mock_check_refresh_auth.assert_called_once()
        mock_post.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            data=json.dumps(data),
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_check_refresh_auth_token_expired(async_session):
    async_session._access_token = "test_access_token"
    async_session._expires_at = datetime.datetime.now(
        datetime.UTC
    ) - datetime.timedelta(
        seconds=10,
    )
    async_session._refresh_token = "test_refresh_token"

    with patch.object(
        async_session,
        "_authentication",
        new_callable=AsyncMock,
    ) as mock_authentication:
        await async_session.check_refresh_auth()
        mock_authentication.assert_called_once_with(
            {
                "grant_type": "refresh_token",
                "refresh_token": async_session._refresh_token,
            },
        )


@pytest.mark.asyncio
async def test_get_devices_raw_response_false(async_smartbox_session):
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        mock_api_request.return_value = {
            "invited_to": [],
            "devs": [
                {
                    "dev_id": "device1",
                    "name": "Device 1",
                    "product_id": "prod123",
                    "fw_version": "1.0.0",
                    "serial_id": "serial123",
                },
                {
                    "dev_id": "device2",
                    "name": "Device 2",
                    "product_id": "prod123",
                    "fw_version": "1.0.0",
                    "serial_id": "serial123",
                },
            ],
        }
        async_smartbox_session.raw_response = False
        devices = await async_smartbox_session.get_devices()
        assert devices.devs[0].dev_id == "device1"
        assert devices.devs[1].dev_id == "device2"
        mock_api_request.assert_called_once_with("devs")


@pytest.mark.asyncio
async def test_api_post_client_connection_error(async_session):
    path = "test_path"
    data = {"key": "value"}

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "post",
        ) as mock_post,
    ):
        mock_post.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session._api_post(data, path)

        mock_check_refresh_auth.assert_called_once()
        mock_post.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            data=json.dumps(data),
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_api_post_client_response_error(async_session):
    path = "test_path"
    data = {"key": "value"}

    with (
        patch.object(
            async_session,
            "check_refresh_auth",
            new_callable=AsyncMock,
        ) as mock_check_refresh_auth,
        patch.object(
            async_session.client,
            "post",
        ) as mock_post,
    ):
        mock_post.side_effect = aiohttp.ClientResponseError(
            request_info=None,
            history=None,
            status=500,
            message="Internal Server Error",
        )

        with pytest.raises(SmartboxError):
            await async_session._api_post(data, path)

        mock_check_refresh_auth.assert_called_once()
        mock_post.assert_called_once_with(
            f"{async_session._api_host}/api/v2/{path}",
            data=json.dumps(data),
            headers=async_session._headers,
        )


@pytest.mark.asyncio
async def test_get_node_setup(async_smartbox_session, caplog):
    for mock_device in await async_smartbox_session.get_devices():
        mock_device_id = mock_device["dev_id"]
        for mock_node in await async_smartbox_session.get_nodes(mock_device_id):
            with patch.object(
                async_smartbox_session,
                "_api_request",
                new_callable=AsyncMock,
            ) as mock_api_request:
                url = f"devs/{mock_device_id}/{mock_node['type']}/{mock_node['addr']}/setup"
                mock_api_request.return_value = await fake_get_request(
                    mock_api_request,
                    url,
                )

                async_smartbox_session.raw_response = True
                setup = await async_smartbox_session.get_node_setup(
                    device_id=mock_device_id,
                    node=mock_node,
                )
                assert setup == mock_api_request.return_value
                mock_api_request.assert_called_with(url)

                async_smartbox_session.raw_response = False
                setup_model = await async_smartbox_session.get_node_setup(
                    device_id=mock_device_id,
                    node=mock_node,
                )
                if isinstance(setup_model, DefaultNodeSetup):
                    assert setup_model.away_mode == setup["away_mode"]
                with pytest.raises(ValidationError):
                    mock_api_request.return_value = {
                        "sync_status": "synced",
                        "control_mode": 1,
                    }
                    await async_smartbox_session.get_node_setup(
                        device_id=mock_device_id, node=mock_node
                    )
                assert "Setup config validation error" in caplog.text
                async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_get_node_version(async_smartbox_session, caplog):
    for mock_device in await async_smartbox_session.get_devices():
        mock_device_id = mock_device["dev_id"]
        for mock_node in await async_smartbox_session.get_nodes(mock_device_id):
            with patch.object(
                async_smartbox_session,
                "_api_request",
                new_callable=AsyncMock,
            ) as mock_api_request:
                url = f"devs/{mock_device_id}/{mock_node['type']}/{mock_node['addr']}/version"
                mock_api_request.return_value = await fake_get_request(
                    mock_api_request,
                    url,
                )

                async_smartbox_session.raw_response = True
                version = await async_smartbox_session.get_node_version(
                    device_id=mock_device_id,
                    node=mock_node,
                )
                assert version == mock_api_request.return_value
                mock_api_request.assert_called_with(url)

                async_smartbox_session.raw_response = False
                version_model = await async_smartbox_session.get_node_version(
                    device_id=mock_device_id,
                    node=mock_node,
                )
                assert version_model.fw_version == version["fw_version"]
                assert version_model.hw_version == version["hw_version"]
                assert version_model.uid == version["uid"]
                assert version_model.pid == version["pid"]

                with pytest.raises(ValidationError):
                    mock_api_request.return_value = {"fw_version": "1.0.0"}
                    await async_smartbox_session.get_node_version(
                        device_id=mock_device_id,
                        node=mock_node,
                    )
                assert "Version config validation error" in caplog.text
                async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_get_homes(async_smartbox_session):
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        url = "grouped_devs"
        mock_api_request.return_value = await fake_get_request(
            mock_api_request,
            url,
        )
        homes = await async_smartbox_session.get_homes()
        assert homes == mock_api_request.return_value
        mock_api_request.assert_called_once_with(url)
        async_smartbox_session.raw_response = False
        homes_model = await async_smartbox_session.get_homes()
        assert homes_model[0].name == homes[0]["name"]


def test_session_get_status(session):
    mock_device_id = "test_device"
    mock_node = {
        "name": "Living Room",
        "addr": 1,
        "type": "thermostat",
        "installed": True,
        "lost": False,
    }

    with patch.object(
        session._async,
        "get_node_status",
        new_callable=AsyncMock,
    ) as mock_get_node_status:
        mock_get_node_status.return_value = {"status": "active"}
        status = session.get_status(device_id=mock_device_id, node=mock_node)
        assert status == {"status": "active"}
        mock_get_node_status.assert_called_once_with(
            device_id=mock_device_id,
            node=mock_node,
        )


def test_client_with_existing_session():
    mock_session = AsyncMock(spec=ClientSession)
    async_smartbox_session = AsyncSession(
        username="test_user",
        password="test_password",
        websession=mock_session,
    )

    client = async_smartbox_session.client
    assert client == mock_session


@pytest.mark.asyncio
async def test_client_without_existing_session():
    async_smartbox_session = AsyncSession(
        username="test_user",
        password="test_password",
    )
    with patch("smartbox.session.ClientSession") as mock_client_session:
        client = async_smartbox_session.client
        assert client == mock_client_session.return_value


@pytest.mark.asyncio
async def test_check_refresh_auth_no_access_token(async_session):
    async_session._access_token = ""

    with patch.object(
        async_session,
        "_authentication",
        new_callable=AsyncMock,
    ) as mock_authentication:
        await async_session.check_refresh_auth()
        mock_authentication.assert_called_once_with(
            {
                "grant_type": "password",
                "username": async_session._username,
                "password": async_session._password,
            },
        )


@pytest.mark.asyncio
async def test_check_refresh_auth_token_valid(async_session):
    async_session._access_token = "test_access_token"
    async_session._expires_at = datetime.datetime.now(
        datetime.UTC
    ) + datetime.timedelta(seconds=3600)

    with patch.object(
        async_session,
        "_authentication",
        new_callable=AsyncMock,
    ) as mock_authentication:
        await async_session.check_refresh_auth()
        mock_authentication.assert_not_called()


@pytest.mark.asyncio
async def test_get_home_guests(async_smartbox_session):
    mock_home_id = "test_home"
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
    ) as mock_api_request:
        url = f"groups/{mock_home_id}/guest_users"
        mock_api_request.return_value = await fake_get_request(
            mock_api_request,
            url,
        )
        guests = await async_smartbox_session.get_home_guests(
            home_id=mock_home_id,
        )
        assert guests == mock_api_request.return_value["guest_users"]
        mock_api_request.assert_called_once_with(url)

        async_smartbox_session.raw_response = False
        guests_model = await async_smartbox_session.get_home_guests(
            home_id=mock_home_id,
        )
        assert guests_model.guest_users[0].email == guests[0]["email"]
        async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_get_deviceconnected_status(async_smartbox_session):
    for mock_device in await async_smartbox_session.get_devices():
        with patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
        ) as mock_api_request:
            url = f"devs/{mock_device['dev_id']}/connected"
            mock_api_request.return_value = await fake_get_request(
                mock_api_request,
                url,
            )
            nodes = await async_smartbox_session.get_device_connected(
                device_id=mock_device["dev_id"],
            )
            assert nodes == mock_api_request.return_value
            mock_api_request.assert_called_with(url)

            async_smartbox_session.raw_response = False
            nodes_model = await async_smartbox_session.get_device_connected(
                device_id=mock_device["dev_id"],
            )
            assert nodes_model.connected == nodes["connected"]
            async_smartbox_session.raw_response = True


@pytest.mark.asyncio
async def test_async_session_context_manager_success():
    """Testing __aenter__ and __aexit__."""
    mock_client = AsyncMock(spec=ClientSession)
    session = AsyncSession(
        username="test_user",
        password="test_password",
        websession=mock_client,
    )
    mock_socket = AsyncMock()
    session._socket = mock_socket

    async with session as s:
        assert s is session
        mock_client.close.assert_not_called()
        mock_socket.disconnect.assert_not_called()
    mock_client.close.assert_awaited_once()
    mock_socket.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_session_context_manager_with_exception():
    """Testing that __aexit__ cleans up properly even in case of a crash."""
    mock_client = AsyncMock(spec=ClientSession)
    session = AsyncSession(
        username="test_user",
        password="test_password",
        websession=mock_client,
    )
    mock_socket = AsyncMock()
    session._socket = mock_socket

    class DummyError(Exception):
        """Dummy exception for testing context manager error handling."""

    with pytest.raises(DummyError):
        async with session:
            msg = "This is a test error to check context manager exception handling."
            raise DummyError(msg)
    mock_client.close.assert_awaited_once()
    mock_socket.disconnect.assert_awaited_once()
