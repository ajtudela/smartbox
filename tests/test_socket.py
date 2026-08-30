import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
import socketio

from smartbox.socket import (
    _DEFAULT_BACKOFF_FACTOR,
    SmartboxAPIV2Namespace,
    SocketSession,
)


@pytest.fixture
def mock_sio(mocker):
    """Return a stand-in for ``socketio.AsyncClient`` with no real network."""
    sio = MagicMock(name="AsyncClient")
    sio.connect = AsyncMock()
    sio.wait = AsyncMock()
    sio.disconnect = AsyncMock()
    sio.emit = AsyncMock()
    sio.send = AsyncMock()
    sio.sleep = asyncio.sleep  # real sleep so _send_ping actually blocks
    sio.register_namespace = MagicMock()
    sio.event = lambda fn: fn  # decorator passthrough

    def start_background_task(target, *args, **kwargs):
        return asyncio.ensure_future(target(*args, **kwargs))

    sio.start_background_task = start_background_task
    sio.eio = MagicMock(ws=None)
    mocker.patch("smartbox.socket.socketio.AsyncClient", return_value=sio)
    return sio


@pytest.fixture
def mock_session():
    session = MagicMock(name="AsyncSmartboxSession")
    session.client = MagicMock()
    session.access_token = "tok"
    session.api_host = "https://api.example.com"
    session.check_refresh_auth = AsyncMock()
    return session


@pytest.fixture
def socket_session(mock_sio, mock_session):
    return SocketSession(mock_session, "dev1", MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_namespace_on_update_waits_for_dev_data():
    node_cb = MagicMock()
    ns = SmartboxAPIV2Namespace(MagicMock(), "/ns", MagicMock(), node_cb)
    ns.emit = AsyncMock()

    # First update before any dev_data: callback withheld, dev_data requested.
    await ns.on_update({"n": 1})
    node_cb.assert_not_called()
    ns.emit.assert_awaited_once()

    # After dev_data has been seen, updates flow through.
    await ns.on_dev_data({"nodes": []})
    await ns.on_update({"n": 2})
    node_cb.assert_called_once_with({"n": 2})


@pytest.mark.asyncio
async def test_run_retries_failed_connection_then_refreshes_token(
    socket_session, mock_sio, mocker
):
    socket_session._reconnect_attempts = 3
    mock_sio.connect.side_effect = socketio.exceptions.ConnectionError("no")

    slept: list[float] = []

    async def record_sleep(delay):
        slept.append(delay)

    mocker.patch("smartbox.socket.asyncio.sleep", new=record_sleep)

    async def stop_outer_loop():
        socket_session._loop_should_exit = True

    socket_session._session.check_refresh_auth.side_effect = stop_outer_loop

    await asyncio.wait_for(socket_session.run(), timeout=2)

    assert mock_sio.connect.await_count == 3
    # backoff after attempts 0 and 1 (1.0, 2.0); none after the last attempt.
    assert slept == [_DEFAULT_BACKOFF_FACTOR * 1, _DEFAULT_BACKOFF_FACTOR * 2]
    socket_session._session.check_refresh_auth.assert_awaited_once()
    assert socket_session._ping_task is None
    assert socket_session._running is False


@pytest.mark.asyncio
async def test_run_refuses_reentry(socket_session, mock_sio):
    async def block_forever(*_a, **_k):
        await asyncio.Event().wait()

    mock_sio.connect.side_effect = block_forever

    first = asyncio.create_task(socket_session.run())
    await asyncio.sleep(0)  # let it start
    assert socket_session._running is True

    await socket_session.run()  # second call: returns immediately, no-op
    assert mock_sio.start_background_task

    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert socket_session._ping_task is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(socket_session, mock_sio):
    socket_session._ping_task = asyncio.ensure_future(asyncio.sleep(3600))

    await socket_session.shutdown()
    assert socket_session._ping_task is None
    assert socket_session._loop_should_exit is True
    mock_sio.disconnect.assert_awaited()

    # Second shutdown and the cancel() alias must not raise.
    await socket_session.shutdown()
    await socket_session.cancel()
