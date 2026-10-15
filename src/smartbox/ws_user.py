"""Per-user ``ws_user`` websocket transport for Smartbox.

One connection serves every device of the account. The wire protocol was
reverse-engineered from the vendor web apps and verified live against
api-lhz (api-notes.md, "Transports — socket_io vs ws_user"); the key
facts this module implements:

* plain WebSocket, JSON frames — no socket.io/engineio layer; the URL is
  ``wss://<host>:443/api/v2/ws_user?token=<enc>&user_id=<id>`` with
  ``user_id`` taken from the access token's JWT ``userId`` claim;
* the client sends exactly one message type, ``{"event": "all_data"}``,
  on open and again 1 s-debounced on every server
  ``{"event": "should_sync"}``;
* the server replies ``all_data`` (full snapshot whose ``devData`` maps
  1:1 onto the ``socket_io`` dev_data payload) and pushes
  ``{"event": "update", "devid": …, "data": {"path", "body"}}`` frames;
* sockets are cut by the server exactly at the token's 4 h expiry with
  close code 1006 (abnormal), so a fresh token per connect is
  load-bearing;
* data silence of many minutes is normal (liveness belongs to the
  server-side proxy PINGs plus the client heartbeat below) — never
  treat data cadence as a failure signal.
"""

import asyncio
import base64
from collections.abc import Callable
import contextlib
import json
import logging
import time
from typing import Any
import urllib.parse

import aiohttp

from smartbox.error import (
    APIUnavailableError,
    SmartboxError,
    WsUserUnsupportedError,
)
from smartbox.retry import (
    backoff_delay,
    jittered,
    refresh_auth_with_retry,
    sleep_unless_exiting,
)
from smartbox.session import AsyncSmartboxSession, _redacted_url

_WS_USER_PATH = "/api/v2/ws_user"
# Deterministic handshake rejections (per the fallback doctrine: these
# alone signal "endpoint unsupported"; 401/403 also occur with an
# expired-but-refreshable token and are retried in the run loop).
_UNSUPPORTED_HTTP_STATUSES = (404, 410)
# One-shot probe context: the session is freshly authenticated, so an
# authz-wall (401/403) is as deterministic as a 404/410.
_PROBE_UNSUPPORTED_STATUSES = (401, 403, 404, 410)
# Bounded capability detection: the ws_connect handshake itself is NOT
# bounded by ClientWSTimeout (in aiohttp 3.14 that knob is a
# post-handshake read timeout only, and takes max(existing, given)); the
# probe wraps the whole handshake in asyncio.timeout() instead, so a hung
# host cannot stall detection for the session's default 300 s request
# timeout. The resulting TimeoutError propagates as transient trouble
# (callers retry; the integration maps it to ConfigEntryNotReady).
_PROBE_TIMEOUT = 10.0
_DEFAULT_HEARTBEAT = 60.0
_DEFAULT_RECONNECT_ATTEMPTS = 10
_DEFAULT_BACKOFF_FACTOR = 1.0
_MAX_RECONNECT_SLEEP = 30.0
# Bounded teardown, mirroring smartbox.socket.
_DISCONNECT_TIMEOUT = 5.0
# The ws_connect handshake itself is bounded: without this, a hung
# handshake would park run() until the session's request timeout (up to
# 300 s with HA's shared session) and make cancel() unable to unblock it.
# See _PROBE_TIMEOUT for why ClientWSTimeout cannot do this job.
_CONNECT_TIMEOUT = 10.0
# A successful cycle shorter than this means the server accepts-then-kills
# sockets: the flat between-cycle pause would spin a per-second
# reconnect/token-grant loop, so those cycles escalate the backoff like
# failed attempts (the vendor reference caps hard; we prefer to keep
# trying, just slower).
_SHORT_CYCLE_SECONDS = 30.0
# Handshake 401/403s of freshly minted tokens tolerated per run() before
# the run demotes to WsUserUnsupportedError (the supervisor then
# re-probes and falls back to socket_io) — matches the one-shot probe's
# semantics for the same statuses.
_MAX_AUTH_REJECTED_CYCLES = 3

_LOGGER = logging.getLogger(__name__)

# Registered consumer callbacks receive ONLY the payload dict — the
# dev_id is implied by the registration (per-device fan-out happens in
# the dispatchers).
DevDataCallback = Callable[[dict[str, Any]], None]
NodeUpdateCallback = Callable[[dict[str, Any]], None]


class _HandshakeAuthRejectedError(Exception):
    """A 401/403 handshake rejection on a token the session trusts.

    Internal control flow only: the run loop turns it into a forced
    token refresh (the endpoint validates exp at handshake — verified —
    so retrying the same URL no-ops under clock skew). Deliberately
    carries NO payload: the rejected token is in the caller's scope,
    and a token in exception args risks a traceback printing it.
    """


def _jwt_user_id(token: str) -> str:
    """Extract the ``userId`` claim from an access-token JWT.

    Raises ``WsUserUnsupportedError`` when the token is not a JWT — that
    shape of token cannot address the per-user endpoint. An empty or
    never-fetched token raises ``SmartboxError`` instead: a caller error
    (auth was not awaited), which is transient trouble to retry, never a
    demotion to "unsupported".
    """
    if not token:
        _LOGGER.debug("No access token available for ws_user addressing")
        msg = "access token not available yet"
        raise SmartboxError(msg)
    try:
        payload_part = token.split(".")[1]
        payload_part += "=" * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part))
        user_id = payload["userId"]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
        _LOGGER.debug("Token is not a JWT with a userId claim: %s", e)
        msg = "access token does not carry a JWT userId claim"
        raise WsUserUnsupportedError(msg) from e
    if not isinstance(user_id, str):
        _LOGGER.debug("Token userId claim is not a string: %r", user_id)
        msg = "access token userId claim is not a string"
        raise WsUserUnsupportedError(msg)
    return str(user_id)


def build_ws_user_url(api_host: str, token: str, user_id: str) -> str:
    """Build the ws_user websocket URL exactly as the vendor web app does.

    Public so external tooling (the live probe) exercises the same URL
    construction the transport ships with.
    """
    quoted_token = urllib.parse.quote(token, safe="~()*!.'")
    quoted_user_id = urllib.parse.quote(user_id, safe="~()*!.'")
    return (
        api_host.replace("https:", "wss:")
        + f":443{_WS_USER_PATH}"
        + f"?token={quoted_token}&user_id={quoted_user_id}"
    )


class WsUserSocketSession:
    """Per-user websocket session serving every device of the account.

    Callbacks (both synchronous, called from the receive loop, receiving
    ONLY the payload dict — the dev_id is implied by the registration):

    * ``dev_data_callback(dev_data)`` — one call per device on every
      ``all_data`` snapshot; ``dev_data`` has the exact shape the
      ``socket_io`` transport delivers (``away_status``, ``connected``,
      ``htr_system``, ``nodes[]``, …);
    * ``node_update_callback(update)`` — every ``update`` frame for the
      device; ``update`` is ``{"path": …, "body": …}``, the same shape
      the ``socket_io`` transport delivers.

    Reconnect policy follows the vendor web app: a fresh token for every
    connect, backoff ``backoff_factor * 2^attempt`` with ±20 % jitter
    capped at ``_MAX_RECONNECT_SLEEP``, transient handshake/auth trouble
    retried (with an auth refresh), and only deterministic 404/410
    rejections raising :class:`WsUserUnsupportedError`.
    """

    def __init__(
        self,
        session: AsyncSmartboxSession,
        *,
        heartbeat: float | None = _DEFAULT_HEARTBEAT,
        reconnect_attempts: int = _DEFAULT_RECONNECT_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        should_sync_debounce: float = 1.0,
    ) -> None:
        """Create the session; no connection is opened until ``run()``.

        Devices register via :meth:`add_device`; frames are fanned out
        per ``dev_id`` to the registered callbacks.

        Knobs:

        * ``heartbeat`` — client WS PING interval in seconds (PONG
          required within half of it). This is the ONLY client-side
          dead-path detection (the invisible half-open failure — see
          api-notes.md, "Client-side outage & liveness"); ``None``
          disables it and leaves the socket blind to address changes
          and permanent link loss. Keep it set in production.
        * ``reconnect_attempts`` — connect attempts per cycle before
          falling through to the token refresh.
        * ``backoff_factor`` — base of the per-attempt backoff
          (``factor * 2^attempt``, ±20 % jitter, capped).
        * ``should_sync_debounce`` — delay before the snapshot
          re-request triggered by a server ``should_sync`` (the
          vendor's value: 1 s).
        """
        if heartbeat is not None and heartbeat <= 0:
            msg = f"heartbeat must be > 0 or None (got {heartbeat})"
            raise ValueError(msg)
        if reconnect_attempts < 1:
            msg = f"reconnect_attempts must be >= 1 (got {reconnect_attempts})"
            raise ValueError(msg)
        if backoff_factor <= 0:
            msg = f"backoff_factor must be > 0 (got {backoff_factor})"
            raise ValueError(msg)
        if should_sync_debounce < 0:
            msg = f"should_sync_debounce must be >= 0 (got {should_sync_debounce})"
            raise ValueError(msg)
        self._session = session
        # dev_id -> (dev_data_cb, update_cb); identity-deduplicated.
        self._devices: dict[
            str, tuple[DevDataCallback, NodeUpdateCallback]
        ] = {}
        self._heartbeat = heartbeat
        self._reconnect_attempts = reconnect_attempts
        self._backoff_factor = backoff_factor
        self._should_sync_debounce = should_sync_debounce
        # Per-cycle state: set in _attempt_cycle, cleared on teardown.
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._loop_should_exit = False
        self._exit_event = asyncio.Event()
        self._should_sync_task: asyncio.Task | None = None
        self._closed_event = asyncio.Event()
        # Consecutive accept-then-kill cycles in the current run().
        self._short_cycles = 0
        # Consecutive 401/403 handshake rejections of freshly minted
        # tokens in the current run().
        self._auth_rejected_cycles = 0
        # cancel() is terminal (socket_io contract: cancel ⇒ run() exits
        # immediately, forever); plain run() exits are restartable.
        self._cancelled = False

    def add_device(
        self,
        device_id: str,
        dev_data_callback: DevDataCallback,
        node_update_callback: NodeUpdateCallback,
    ) -> None:
        """Register a device's callbacks (idempotent per device_id).

        When the socket is already connected, a debounced ``all_data``
        re-request follows: a late registration (or a registration that
        re-arms after a socket restart) would otherwise miss the snapshot
        that raced it and sit silent until the next update frame.
        """
        self._devices[device_id] = (dev_data_callback, node_update_callback)
        ws = self._ws
        if ws is not None and not ws.closed:
            _LOGGER.debug(
                "Device %s registered on a live socket; scheduling all_data",
                device_id,
            )
            self._schedule_all_data()

    def remove_device(self, device_id: str) -> None:
        """Unregister a device (no-op when not registered)."""
        self._devices.pop(device_id, None)

    @property
    def closed(self) -> bool:
        """Whether the run loop has finished (or was cancelled)."""
        return self._closed_event.is_set()

    async def wait_closed(self) -> None:
        """Wait until the run loop finished (or was cancelled)."""
        await self._closed_event.wait()

    def _request_exit(self) -> None:
        """Flag the run loop to stop and wake any backoff sleep.

        Does NOT touch ``_closed_event``: that flag means "the run loop
        finished", and is set by ``run()``'s finally (also on exception
        exits) — restarts must not see it set from a request alone.
        """
        self._loop_should_exit = True
        self._exit_event.set()

    def _backoff_delay(self, attempt: int) -> float:
        """Backoff delay with the web app's ±20 % jitter, capped.

        The method computes the delay; the caller sleeps it via
        ``_sleep_unless_exiting``.
        """
        return jittered(
            backoff_delay(attempt, self._backoff_factor, _MAX_RECONNECT_SLEEP)
        )

    async def _refresh_auth(self) -> None:
        """Refresh the REST token the URL carries.

        Same contract as ``smartbox.socket.SocketSession._refresh_auth``
        (both delegate to ``smartbox.retry.refresh_auth_with_retry``):
        transient failures are waited out with capped backoff; rejected
        credentials propagate as ``InvalidAuthError``. Returns early once
        an exit is requested.
        """
        await refresh_auth_with_retry(
            self._session.check_refresh_auth,
            exit_requested=lambda: self._loop_should_exit,
            sleep_unless_exiting=self._sleep_unless_exiting,
            backoff_factor=self._backoff_factor,
            max_sleep=_MAX_RECONNECT_SLEEP,
            log_context="ws_user",
        )

    async def _sleep_unless_exiting(self, delay: float) -> None:
        """Sleep up to ``delay`` seconds, waking early once exit is requested."""
        await sleep_unless_exiting(self._exit_event, delay)

    async def _attempt_cycle(self, url: str) -> bool:
        """One connect/serve cycle.

        Returns True when the socket connected and served until it closed
        (the caller then refreshes auth and goes around); False on a
        failed connect/serve attempt (caller applies backoff). Raises
        ``WsUserUnsupportedError`` on a deterministic 404/410 handshake
        rejection, and ``_HandshakeAuthRejectedError`` on a 401/403 rejection
        (the endpoint validates exp at handshake — verified — so the
        caller must force a real token refresh instead of retrying the
        same URL, which would no-op under clock skew).
        """
        redacted = _redacted_url(url)
        _LOGGER.debug("Connecting to %s", redacted)
        ctx: Any = None
        entered = False
        try:
            try:
                ctx = self._session.client.ws_connect(
                    url, heartbeat=self._heartbeat
                )
                ws = await asyncio.wait_for(
                    ctx.__aenter__(), timeout=_CONNECT_TIMEOUT
                )
                entered = True
            except TimeoutError:
                # The handshake bound may have raced a concurrently
                # completed handshake: exit the context best-effort so
                # the connection does not leak (ClientWSTimeout cannot
                # bound the handshake itself — see _PROBE_TIMEOUT).
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        ctx.__aexit__(None, None, None),
                        timeout=_DISCONNECT_TIMEOUT,
                    )
                return False
            except aiohttp.ClientResponseError as e:
                if e.status in _UNSUPPORTED_HTTP_STATUSES:
                    _LOGGER.info(
                        "ws_user endpoint rejected the handshake with %s; "
                        "the API host does not serve it",
                        e.status,
                    )
                    msg = f"ws_user handshake rejected with HTTP {e.status}"
                    raise WsUserUnsupportedError(msg) from e
                if e.status in (401, 403):
                    _LOGGER.debug(
                        "Handshake rejected with HTTP %s; forcing token refresh",
                        e.status,
                    )
                    raise _HandshakeAuthRejectedError from e
                # Status only: the exception's __str__ renders the
                # URL, which carries the raw access token.
                _LOGGER.debug("Connection attempt failed: HTTP %s", e.status)
                return False
            except (aiohttp.ClientError, OSError) as e:
                # The handshake bound raised TimeoutError, handled
                # above; other transport trouble lands here.
                _LOGGER.debug("Connection attempt failed: %s", e)
                return False
            # Serve. Transport failures here (a failed all_data send, a
            # dead receive loop) are cycle-over — the same in-loop
            # backoff as connect failures, never a supervisor-visible
            # "unexpected exit".
            try:
                self._ws = ws
                if self._loop_should_exit:
                    _LOGGER.debug("Exit requested during connect; closing")
                    return True
                _LOGGER.info("Successfully connected to %s", redacted)
                await ws.send_json({"event": "all_data"})
                await self._observe(ws)
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                _LOGGER.debug("Serve block ended: %s", e)
                return False
            return True
        finally:
            self._ws = None
            if entered:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        ctx.__aexit__(None, None, None),
                        timeout=_DISCONNECT_TIMEOUT,
                    )

    async def _observe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Dispatch frames until the socket closes or exit is requested.

        Server PING/PONG traffic is swallowed by aiohttp's autoping and
        never surfaces here; the heartbeat death surfaces as a
        ``ServerTimeoutError`` from ``receive()``, handled below.
        """
        while not self._loop_should_exit:
            try:
                msg = await ws.receive()
            except (aiohttp.ClientError, OSError) as e:
                # Includes the heartbeat's ServerTimeoutError (a
                # ClientError subclass): path dead — cycle over.
                _LOGGER.debug("Receive loop ended: %s", e)
                return
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                _LOGGER.debug(
                    "Socket closed: code=%s extra=%r", ws.close_code, msg.extra
                )
                return
            if msg.type is not aiohttp.WSMsgType.TEXT:
                _LOGGER.debug("Ignoring %s frame", msg.type.name)
                continue
            self._dispatch(msg.data)

    def _dispatch(self, raw: str) -> None:
        """Dispatch one text frame to the registered callbacks."""
        try:
            frame = json.loads(raw)
        except ValueError, TypeError, AttributeError:
            _LOGGER.warning("Unparseable ws_user frame (%s chars)", len(raw))
            return
        if not isinstance(frame, dict):
            _LOGGER.warning(
                "ws_user frame is not an object (%s chars); ignoring", len(raw)
            )
            return
        event = frame.get("event")
        # One DEBUG line per delivered frame, mirroring socket.py's
        # per-message trace (the first thing a "stale entity" debug
        # session looks for). %r: the value is server-controlled.
        _LOGGER.debug("ws_user frame: %r (%s chars)", event, len(raw))
        if event == "all_data":
            self._dispatch_all_data(frame.get("data") or [])
        elif event == "update":
            self._dispatch_update(frame)
        elif event == "should_sync":
            _LOGGER.debug("Received should_sync; scheduling all_data")
            self._schedule_all_data()
        else:
            _LOGGER.debug("Ignoring unknown ws_user event: %s", event)

    def _dispatch_all_data(self, homes: Any) -> None:  # noqa: ANN401
        """Deliver one dev_data payload per registered device."""
        if not isinstance(homes, list):
            _LOGGER.warning("all_data snapshot is not a list; ignoring")
            return
        for home in homes:
            if not isinstance(home, dict):
                continue
            for dev in home.get("devs") or []:
                if not isinstance(dev, dict):
                    continue
                dev_id = dev.get("dev_id")
                dev_data = dev.get("devData")
                if not dev_id or not isinstance(dev_data, dict):
                    _LOGGER.warning(
                        "all_data entry missing dev_id/devData; ignoring"
                    )
                    continue
                if dev_id not in self._devices:
                    _LOGGER.debug(
                        "No consumer registered for device %s; ignoring "
                        "its all_data snapshot",
                        dev_id,
                    )
                    continue
                dev_data_cb, _ = self._devices[dev_id]
                self._safe_call("dev_data", dev_id, dev_data_cb, dev_data)

    def _dispatch_update(self, frame: dict[str, Any]) -> None:
        """Deliver one update frame to its device's callback."""
        dev_id = frame.get("devid")
        payload = frame.get("data")
        if not dev_id or not isinstance(payload, dict) or "path" not in payload:
            # devid only: the frame body is server-controlled content and
            # could forge multi-line log entries if interpolated.
            _LOGGER.warning(
                "Malformed update frame (devid=%r, data=%s); ignoring",
                dev_id,
                type(payload).__name__,
            )
            return
        callbacks = self._devices.get(dev_id)
        if callbacks is None:
            _LOGGER.debug(
                "No consumer registered for device %r; ignoring update", dev_id
            )
            return
        self._safe_call("node_update", dev_id, callbacks[1], payload)

    def _safe_call(
        self,
        kind: str,
        dev_id: str,
        callback: Callable[..., None],
        data: dict[str, Any],
    ) -> None:
        """Call a consumer callback, isolating its exceptions."""
        try:
            callback(data)
        except Exception:
            _LOGGER.exception(
                "Error in ws_user %s callback for %s", kind, dev_id
            )

    def _schedule_all_data(self) -> None:
        """Re-request the snapshot after the vendor's 1 s debounce."""
        if self._should_sync_task is not None:
            self._should_sync_task.cancel()
        self._should_sync_task = asyncio.get_running_loop().create_task(
            self._delayed_all_data()
        )

    async def _delayed_all_data(self) -> None:
        await asyncio.sleep(self._should_sync_debounce)
        ws = self._ws
        if ws is None or self._loop_should_exit:
            return
        try:
            await ws.send_json({"event": "all_data"})
        except (aiohttp.ClientError, OSError) as e:
            _LOGGER.debug("Could not send all_data re-request: %s", e)

    async def _connect_until_served_or_exhausted(
        self, url: str, token: str
    ) -> str:
        """Run one round of connect attempts with per-attempt backoff.

        Returns ``"served"`` when a cycle connected and served (a cycle
        shorter than ``_SHORT_CYCLE_SECONDS`` increments the
        accept-then-kill counter ``_short_cycles``, a long-lived one
        resets it), ``"auth_rejected"`` when the handshake rejected the
        token with 401/403 (the invalidation has been done), and
        ``"exhausted"`` when all attempts failed.

        Consumes ``_HandshakeAuthRejectedError`` by invalidating the
        rejected token (same-package access to the session's hook, not
        part of the public surface) — the caller's refresh then fetches
        a real new token, instead of retrying a URL that the server
        rejects under clock skew.
        """
        for attempt in range(self._reconnect_attempts):
            if self._loop_should_exit:
                return "exhausted"
            cycle_started = time.monotonic()
            try:
                if await self._attempt_cycle(url):
                    # Connected and served until closed: the caller
                    # refreshes the token and goes around (the server
                    # cuts sockets at token expiry, so this is the
                    # steady state).
                    if time.monotonic() - cycle_started < _SHORT_CYCLE_SECONDS:
                        self._short_cycles += 1
                    else:
                        self._short_cycles = 0
                    return "served"
            except _HandshakeAuthRejectedError:
                self._session._invalidate_access_token(token)  # noqa: SLF001
                return "auth_rejected"
            sleep_time = self._backoff_delay(attempt)
            remaining = self._reconnect_attempts - attempt - 1
            _LOGGER.warning(
                "Received error on connection attempt, %s retries "
                "remaining, sleeping %.1fs",
                remaining,
                sleep_time,
            )
            if remaining > 0:
                await self._sleep_unless_exiting(sleep_time)
            else:
                _LOGGER.warning(
                    "Failed to connect after %s attempts, falling "
                    "through to refresh token",
                    self._reconnect_attempts,
                )
        return "exhausted"

    async def run(self) -> None:
        """Connect and serve until cancelled or credentials are rejected.

        Re-runnable: a supervisor may call ``run()`` again after any exit
        (exception or teardown of the previous cycle); only ``cancel()``
        is terminal. Each entry resets the per-run exit flags.

        ``cancel()`` always returns bounded (mirrors SocketSession), but
        it cannot unblock a run parked inside an in-flight handshake
        (the socket is not owned yet); prompt teardown for that state
        relies on cancelling the run task itself — the integration's
        supervisor does exactly that.
        """
        self._closed_event.clear()
        if self._cancelled:
            _LOGGER.debug("ws_user session cancelled; run() is a no-op")
            self._closed_event.set()
            return
        self._loop_should_exit = False
        self._exit_event.clear()
        try:
            await self._refresh_auth()
            # Consecutive accept-then-kill cycles (connect OK, socket dead
            # within _SHORT_CYCLE_SECONDS): escalate their pause so a
            # pathological server does not get a per-second reconnect +
            # token-grant loop. Reset by one long-lived cycle.
            self._short_cycles = 0
            self._auth_rejected_cycles = 0
            while not self._loop_should_exit:
                token = self._session.access_token
                url = build_ws_user_url(
                    self._session.api_host,
                    token,
                    _jwt_user_id(token),
                )
                status = await self._connect_until_served_or_exhausted(
                    url, token
                )
                if self._loop_should_exit:
                    break
                await self._refresh_auth()
                if status == "auth_rejected":
                    # The endpoint keeps rejecting freshly minted tokens:
                    # it authz-walls ws_user (the same statuses the
                    # one-shot probe demotes on). Escalate while counting;
                    # past the bound, let the supervisor re-probe and
                    # fall back to socket_io instead of granting tokens
                    # every second forever.
                    self._auth_rejected_cycles += 1
                    if self._auth_rejected_cycles >= _MAX_AUTH_REJECTED_CYCLES:
                        msg = (
                            "ws_user handshake rejected fresh tokens "
                            f"{self._auth_rejected_cycles} times in a row"
                        )
                        raise WsUserUnsupportedError(msg)
                    pause = self._backoff_delay(self._auth_rejected_cycles)
                    _LOGGER.warning(
                        "Handshake keeps rejecting fresh tokens "
                        "(%s in a row); backing off %.1fs",
                        self._auth_rejected_cycles,
                        pause,
                    )
                else:
                    self._auth_rejected_cycles = 0
                    if self._short_cycles:
                        pause = self._backoff_delay(self._short_cycles)
                        _LOGGER.warning(
                            "Reconnect cycles keep dying young "
                            "(%s in a row); backing off %.1fs",
                            self._short_cycles,
                            pause,
                        )
                    else:
                        pause = self._backoff_factor
                await self._sleep_unless_exiting(pause)
        except asyncio.CancelledError:
            _LOGGER.debug("ws_user loop cancelled")
            raise
        finally:
            _LOGGER.debug("Cleaning up ws_user session")
            self._request_exit()
            # Wake waiters before any cleanup that could raise: the
            # event means "the run loop finished", and a helper failure
            # must not strand every device manager on wait_closed().
            self._closed_event.set()
            with contextlib.suppress(Exception):
                await self._cancel_should_sync()
            with contextlib.suppress(Exception):
                await self._close_ws_bounded()

    async def _cancel_should_sync(self) -> None:
        if (
            self._should_sync_task is not None
            and not self._should_sync_task.done()
        ):
            self._should_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(
                    self._should_sync_task, timeout=_DISCONNECT_TIMEOUT
                )
        self._should_sync_task = None

    async def _close_ws_bounded(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None or ws.closed:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=_DISCONNECT_TIMEOUT)
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            _LOGGER.debug("Bounded websocket close failed: %s", e)

    async def cancel(self) -> None:
        """Request exit and disconnect, bounded. Terminal for run()."""
        _LOGGER.debug("Disconnecting and cancelling ws_user tasks")
        self._cancelled = True
        self._request_exit()
        self._closed_event.set()
        with contextlib.suppress(Exception):
            await self._cancel_should_sync()
        with contextlib.suppress(Exception):
            await self._close_ws_bounded()


async def check_ws_user_support(session: AsyncSmartboxSession) -> None:
    """Probe the ``ws_user`` endpoint once and raise when it is not served.

    Returns quietly when the handshake succeeds. Raises
    :class:`WsUserUnsupportedError` on a deterministic HTTP
    401/403/404/410 rejection or a non-JWT access token (per the fallback
    doctrine these alone mean "endpoint unsupported"); transient
    network/API trouble propagates as-is so callers can distinguish
    "unsupported" from "unreachable right now".

    Precondition: call with a freshly (re)authenticated session — a 401
    with a stale-but-refreshable token is indistinguishable from a host
    authz-walling the endpoint, so the probe treats both as unsupported
    (a caller with a fresh token, e.g. right after ``check_refresh_auth``,
    has no such ambiguity).
    """
    token = session.access_token
    user_id = _jwt_user_id(token)
    url = build_ws_user_url(session.api_host, token, user_id)
    _LOGGER.debug("Probing ws_user support at %s", _redacted_url(url))
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            async with session.client.ws_connect(url):
                _LOGGER.debug("ws_user handshake OK; endpoint supported")
    except aiohttp.ClientResponseError as e:
        if e.status in _PROBE_UNSUPPORTED_STATUSES:
            _LOGGER.info(
                "ws_user handshake rejected with HTTP %s; endpoint not served",
                e.status,
            )
            msg = f"ws_user handshake rejected with HTTP {e.status}"
            raise WsUserUnsupportedError(msg) from e
        # Anything else (5xx, 429, 405, …) is the SERVER's state, not a
        # capability verdict: surface it as the retryable API error so
        # callers (and their test doubles) cannot mistake it for
        # "unsupported" — and its __str__ (which renders the URL with
        # the raw token) never escapes to a log line.
        msg = f"ws_user probe failed: HTTP {e.status}"
        raise APIUnavailableError(msg) from e
