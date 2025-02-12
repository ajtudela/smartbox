import datetime
import json
import math
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import ClientSession
from pydantic import ValidationError
import pytest

from smartbox import APIUnavailableError, InvalidAuthError, SmartboxError
from smartbox.models import NodeSetup
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
                assert (
                    samples_model.samples[0].counter
                    == samples["samples"][0]["counter"]
                )
                async_smartbox_session.raw_response = True


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
            "get_node_setup",
            new_callable=AsyncMock,
        ) as mock_get_node_setup,
        patch.object(
            async_smartbox_session,
            "_api_post",
            new_callable=AsyncMock,
        ) as mock_api_post,
    ):
        mock_get_node_setup.return_value = {"setting2": "value2"}
        mock_api_post.return_value = None

        result = await async_smartbox_session.set_node_setup(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )
        assert result is None
        mock_api_post.assert_called_once_with(
            data={"setting1": "value1", "setting2": "value2"},
            path=f"devs/{mock_device_id}/{mock_node['type']}/{mock_node['addr']}/setup",
        )
        data = {
            "sync_status": "synced",
            "control_mode": 1,
            "units": "C",
            "power": "on",
            "offset": "0.5",
            "away_mode": 0,
            "away_offset": "1.0",
            "modified_auto_span": 10,
            "window_mode_enabled": True,
            "true_radiant_enabled": True,
            "user_duty_factor": 5,
            "flash_version": "1.0.0",
            "factory_options": {
                "temp_compensation_enabled": True,
                "window_mode_available": True,
                "true_radiant_available": True,
                "duty_limit": 10,
                "boost_config": 1,
                "button_double_press": True,
                "prog_resolution": 5,
                "bbc_value": 2,
                "bbc_available": True,
                "lst_value": 3,
                "lst_available": True,
                "fil_pilote_available": True,
                "backlight_time": 30,
                "button_down_code": 1,
                "button_up_code": 2,
                "button_mode_code": 3,
                "button_prog_code": 4,
                "button_off_code": 5,
                "button_boost_code": 6,
                "splash_screen_type": 1,
            },
            "extra_options": {"boost_temp": "22.5", "boost_time": 60},
        }
        setup = NodeSetup(**data)
        mock_get_node_setup.return_value = setup

        result = await async_smartbox_session.set_node_setup(
            device_id=mock_device_id,
            node=mock_node,
            setup_args=setup_args,
        )
        assert result is None


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


@pytest.mark.asyncio
async def test_async_session_init_defaults(resailer):
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
async def test_authentication_success(async_session):
    credentials = {
        "grant_type": "password",
        "username": "test_user",
        "password": "test_password",
    }
    token_response = {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_in": 3600,
        "token_type": "test_token_type",
    }

    with patch.object(
        async_session.client,
        "post",
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.return_value.json = AsyncMock(return_value=token_response)
        mock_post.return_value.raise_for_status = AsyncMock()

        await async_session._authentication(credentials)

        assert async_session._access_token == "test_access_token"
        assert async_session.access_token == "test_access_token"
        assert async_session._refresh_token == "test_refresh_token"
        assert async_session.refresh_token == "test_refresh_token"
        assert async_session._expires_at > datetime.datetime.now(datetime.UTC)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.resailer.basic_auth}",
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
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.return_value.json = AsyncMock(return_value=invalid_response)
        mock_post.return_value.raise_for_status = AsyncMock()

        with pytest.raises(
            InvalidAuthError,
            match="Received invalid auth response",
        ):
            await async_session._authentication(credentials)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.resailer.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.resailer.serial_id}",
                "x-referer": f"{async_session.resailer.web_url}",
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
        new_callable=AsyncMock,
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
                "authorization": f"Basic {async_session.resailer.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.resailer.serial_id}",
                "x-referer": f"{async_session.resailer.web_url}",
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
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session._authentication(credentials)

        mock_post.assert_called_once_with(
            url=f"{async_session._api_host}/client/token",
            headers={
                "authorization": f"Basic {async_session.resailer.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-serialid": f"{async_session.resailer.serial_id}",
                "x-referer": f"{async_session.resailer.web_url}",
            },
            data=credentials,
        )


@pytest.mark.asyncio
async def test_health_check_success(async_session):
    with patch.object(
        async_session.client,
        "get",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok"})
        mock_response.raise_for_status = AsyncMock()
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
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.side_effect = aiohttp.ClientConnectionError()

        with pytest.raises(APIUnavailableError):
            await async_session.health_check()

        mock_get.assert_called_once_with(
            f"{async_session._api_host}/health_check",
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
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=expected_response)
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
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={})
        mock_response.raise_for_status = AsyncMock()
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
            new_callable=AsyncMock,
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
            new_callable=AsyncMock,
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
            new_callable=AsyncMock,
        ) as mock_post,
    ):
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value=expected_response)
        mock_response.raise_for_status = AsyncMock()
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
            new_callable=AsyncMock,
        ) as mock_post,
    ):
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={})
        mock_response.raise_for_status = AsyncMock()
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
            new_callable=AsyncMock,
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
            new_callable=AsyncMock,
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

    client = async_smartbox_session.client
    assert isinstance(client, ClientSession)


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
