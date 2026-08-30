import asyncio
import contextlib
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest
import socketio

from smartbox.socket import (
    _DEFAULT_BACKOFF_FACTOR,
    _MIN_SESSION_SECONDS,
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

    sio.handlers = {}

    def event(fn):
        sio.handlers[fn.__name__] = fn
        return fn

    sio.event = event

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


@pytest.mark.asyncio
async def test_shutdown_swallows_disconnect_error(socket_session, mock_sio):
    mock_sio.disconnect.side_effect = OSError("boom")
    await socket_session.shutdown()  # must not raise


@pytest.mark.asyncio
async def test_namespace_connect_and_disconnect_toggle_state():
    ns = SmartboxAPIV2Namespace(MagicMock(), "/ns", MagicMock(), MagicMock())
    assert ns.connected is False

    await ns.on_connect()
    assert ns.connected is True

    ns._received_message = True
    ns._received_dev_data = True
    await ns.on_disconnect("server closed")
    assert ns.connected is False
    assert ns._received_message is False
    assert ns._received_dev_data is False


@pytest.mark.asyncio
async def test_namespace_on_dev_data_invokes_callback():
    dev_cb = MagicMock()
    ns = SmartboxAPIV2Namespace(MagicMock(), "/ns", dev_cb, MagicMock())
    await ns.on_dev_data({"nodes": [1]})
    dev_cb.assert_called_once_with({"nodes": [1]})
    assert ns._received_dev_data is True


def test_verbose_enables_client_logging(mock_sio, mock_session, mocker):
    client = mocker.patch(
        "smartbox.socket.socketio.AsyncClient", return_value=mock_sio
    )
    SocketSession(mock_session, "dev1", verbose=True)
    _, kwargs = client.call_args
    assert kwargs["logger"] is True
    assert kwargs["engineio_logger"] is True


@pytest.mark.asyncio
async def test_connect_event_installs_sigint_handler(
    mock_sio, mock_session, mocker
):
    loop = MagicMock()
    mocker.patch("smartbox.socket.asyncio.get_event_loop", return_value=loop)
    session = SocketSession(mock_session, "dev1", add_sigint_handler=True)
    session.cancel = AsyncMock()

    await mock_sio.handlers["connect"]()

    loop.add_signal_handler.assert_called_once()
    sig, handler = loop.add_signal_handler.call_args.args
    assert sig == signal.SIGINT
    handler()  # the inner sigint_handler schedules cancel()
    await asyncio.sleep(0)
    session.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_connect_event_without_sigint_handler(mock_sio, mock_session):
    SocketSession(mock_session, "dev1", add_sigint_handler=False)
    await mock_sio.handlers["connect"]()  # just logs, no signal handler


@pytest.mark.asyncio
async def test_dev_data_emits_only_when_connected(socket_session, mock_sio):
    socket_session._api_v2_ns._namespace_connected = False
    await socket_session._dev_data()
    mock_sio.emit.assert_not_awaited()

    socket_session._api_v2_ns._namespace_connected = True
    await socket_session._dev_data()
    mock_sio.emit.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_ping_skips_when_disconnected_then_pings(
    socket_session, mock_sio, mocker
):
    async def yielding_sleep(*_a):
        await asyncio.sleep(0)

    mocker.patch.object(mock_sio, "sleep", new=yielding_sleep)
    ns = socket_session._api_v2_ns
    ns._namespace_connected = False

    task = asyncio.ensure_future(socket_session._send_ping())
    for _ in range(5):
        await asyncio.sleep(0)
    ns._namespace_connected = True
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    mock_sio.send.assert_awaited_with("ping", namespace=mocker.ANY)


@pytest.mark.asyncio
async def test_attempt_connection_success_returns_duration(
    socket_session, mock_sio
):
    seconds = await socket_session._attempt_connection("wss://x")
    assert seconds is not None
    assert seconds >= 0
    mock_sio.wait.assert_awaited_once()
    mock_sio.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_attempt_connection_returns_none_on_connection_error(
    socket_session, mock_sio
):
    mock_sio.connect.side_effect = socketio.exceptions.ConnectionError("no")
    assert await socket_session._attempt_connection("wss://x") is None


@pytest.mark.asyncio
async def test_attempt_connection_cancelled_during_connect(
    socket_session, mock_sio
):
    gate = asyncio.Event()

    async def slow_connect(*_a, **_k):
        await gate.wait()

    mock_sio.connect.side_effect = slow_connect

    task = asyncio.ensure_future(socket_session._attempt_connection("wss://x"))
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # a dangling-socket cleanup task was scheduled
    assert socket_session._background_tasks
    gate.set()  # let connect finish so the cleanup can run
    await asyncio.gather(
        *list(socket_session._background_tasks), return_exceptions=True
    )
    assert socket_session._background_tasks == set()
    mock_sio.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_attempt_connection_cancel_cleanup_swallows_error(
    socket_session, mock_sio
):
    gate = asyncio.Event()

    async def slow_connect(*_a, **_k):
        await gate.wait()

    mock_sio.connect.side_effect = slow_connect
    mock_sio.disconnect.side_effect = OSError("boom")

    task = asyncio.ensure_future(socket_session._attempt_connection("wss://x"))
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    gate.set()
    await asyncio.gather(
        *list(socket_session._background_tasks), return_exceptions=True
    )
    assert socket_session._background_tasks == set()  # error was swallowed


@pytest.mark.asyncio
async def test_cleanup_websocket_closes_orphan(socket_session, mock_sio):
    ws = MagicMock()
    ws.closed = False
    ws.close = AsyncMock()
    mock_sio.eio.ws = ws

    await socket_session._cleanup_websocket()
    ws.close.assert_awaited_once()

    ws.close.side_effect = OSError("boom")
    await socket_session._cleanup_websocket()  # error swallowed


@pytest.mark.asyncio
async def test_run_breaks_to_refresh_after_long_session(socket_session, mocker):
    mocker.patch.object(
        socket_session,
        "_attempt_connection",
        new=AsyncMock(return_value=_MIN_SESSION_SECONDS + 5),
    )

    async def stop_outer_loop():
        socket_session._loop_should_exit = True

    socket_session._session.check_refresh_auth.side_effect = stop_outer_loop

    await asyncio.wait_for(socket_session.run(), timeout=2)

    socket_session._attempt_connection.assert_awaited_once()
    socket_session._session.check_refresh_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_backs_off_on_short_session(socket_session, mocker):
    socket_session._reconnect_attempts = 2
    mocker.patch.object(
        socket_session,
        "_attempt_connection",
        new=AsyncMock(return_value=0.1),
    )
    slept: list[float] = []

    async def record_sleep(delay):
        slept.append(delay)

    mocker.patch("smartbox.socket.asyncio.sleep", new=record_sleep)

    async def stop_outer_loop():
        socket_session._loop_should_exit = True

    socket_session._session.check_refresh_auth.side_effect = stop_outer_loop

    await asyncio.wait_for(socket_session.run(), timeout=2)

    assert socket_session._attempt_connection.await_count == 2
    assert slept == [_DEFAULT_BACKOFF_FACTOR * 1]


def test_namespace_property(socket_session):
    assert socket_session.namespace is socket_session._api_v2_ns
