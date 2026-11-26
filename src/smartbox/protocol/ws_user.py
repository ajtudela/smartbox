"""Real ``ws_user`` transport: one WebSocket per user, push-only.

The wire facts and the fallback doctrine live in hass-smartbox
docs/api-contract.md §4 and docs/api-ws-user.md §1. What this module relies on:

- URL ``wss://<host>:443/api/v2/ws_user?token=...&user_id=...`` (see
  :func:`build_ws_url`). A FRESH token per connect is load-bearing: the endpoint
  validates ``exp`` at the handshake and cuts the socket exactly at token
  ``exp`` every ~4 h (close 1006).
- One ``{"event": "all_data"}`` message on open, re-sent (debounced) on
  ``should_sync``. The all_data reply is expected within
  ``_SNAPSHOT_TIMEOUT`` of connect (a socket that never delivers it is a
  wedged session, not a quiet one); afterwards liveness is the ``heartbeat``
  kwarg (server PINGs are auto-PONGed), data silence below
  ``_SILENCE_TIMEOUT`` is never a failure signal, and past it the socket is
  torn down as dead (see the constants).
- Backoff 1 s * 2^n with one-sided +0-20 % jitter, capped. Transient failures
  retry; only deterministic rejections (404/410, or >= 3 consecutive
  fresh-token 401/403s) raise ``TransportUnsupportedError``.
- Secrets hygiene: never log the URL, the token or frame bodies.

Writes are REST, never socket messages. ``check_ws_user_support`` is the
endpoint's own one-shot handshake probe.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import logging
import random
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote

from aiohttp import (
    ClientError,
    ClientSession,
    ClientWebSocketResponse,
    ServerTimeoutError,
    WSMsgType,
)

from . import timing
from .errors import TransportUnavailableError, TransportUnsupportedError
from .events import Event, Snapshot, SocketClosed, Update
from .jwt import jwt_expiry, jwt_user_id
from .retry import backoff_delay
from .transport import EventHandler, TokenSource, Transport, WsCredentials

if TYPE_CHECKING:
    from types import TracebackType

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "TokenSource",
    "TransportUnsupportedError",
    "WsCredentials",
    "WsUserTransport",
    "build_ws_url",
    "check_ws_user_support",
    "classify_ws_status",
]

_ALL_DATA_FRAME = json.dumps({"event": "all_data"})
_HANDSHAKE_AUTH_STATUSES = frozenset({401, 403})
_HANDSHAKE_UNSUPPORTED_STATUSES = frozenset({404, 410})
_AUTH_REJECTION_LIMIT = 3
_WS_PATH = "/api/v2/ws_user"
# Never-turned connection knobs, folded into constants (the constructor
# keeps only the knobs tests/consumers actually turn: heartbeat,
# should_sync_debounce, connect_timeout, session).
_RECONNECT_ATTEMPTS = 10
_BACKOFF_FACTOR = 1.0
_TEARDOWN_TIMEOUT = 5.0
_SHORT_CYCLE_SECONDS = 30.0
# The all_data reply is due right after the open (the vendor server sends it
# in response to the client's own all_data frame); the apps reconnect when it
# does not arrive within base-delay + 5 s (6 s on the first attempt,
# docs/api-ws-user.md §1.1). 10 s keeps that intent with room for a slow first
# snapshot on a busy account.
_SNAPSHOT_TIMEOUT = 10.0
# No frame for this long while the socket answers heartbeats means the socket
# is not delivering: the recorded worst-case idle cadence is a push every
# ~11 min (hass-smartbox docs/api-contract.md §6.3), so 45 min is several times clear of
# normal chatter. Past it the socket is closed, which routes the outage
# through the ordinary close -> grace-window path and reconnects.
_SILENCE_TIMEOUT = 45 * 60.0
# Clock-skew allowance when recognising the vendor's scheduled cut: the server
# ends the socket at the presented token's ``exp``, but our clock and the
# server's need not agree to the second.
_TOKEN_CUT_MARGIN = 30.0
_MAX_SLEEP = 30.0


def build_ws_url(api_host: str, token: str, user_id: str) -> str:
    """Build the public ws_user URL (``tools/live_probe.py`` exercises this).

    ``api_host`` is the REST host (``https://host``); it becomes ``wss://``
    with ``:443`` appended unless a port is present.
    """
    base = api_host.strip().rstrip("/")
    if base.startswith("https://"):
        base = f"wss://{base[len('https://') :]}"
    elif base.startswith("http://"):
        base = f"ws://{base[len('http://') :]}"
    elif "://" not in base:
        base = f"wss://{base}"
    host = base.split("://", 1)[1]
    if ":" not in host.rpartition("@")[2]:
        host = f"{host}:443"
    query = f"token={_quote(token)}&user_id={_quote(user_id)}"
    return f"{base.split('://', 1)[0]}://{host}{_WS_PATH}?{query}"


def _quote(value: str) -> str:
    """Percent-encode one query parameter value."""
    return quote(value, safe="")


def classify_ws_status(status: int) -> Literal["auth", "unsupported", "transient"]:
    """Map a handshake HTTP status to its failure class.

    Factored for reuse by the REST capability probe: 401/403 -> auth,
    404/410 -> unsupported (deterministic), everything else transient.
    """
    if status in _HANDSHAKE_AUTH_STATUSES:
        return "auth"
    if status in _HANDSHAKE_UNSUPPORTED_STATUSES:
        return "unsupported"
    return "transient"


class _HandshakeAuthRejectedError(Exception):
    """Internal control flow: the handshake rejected our token.

    Carries no payload: the caught ``ClientResponseError`` text renders the
    token-bearing URL, hence ``from None`` at the raise site.
    """


@dataclass(frozen=True)
class _CycleOutcome:
    """What one round of connect attempts produced.

    ``served`` (a connection lived; ``duration`` is its length) and
    ``rejected`` (a handshake auth rejection ended the round) are mutually
    exclusive; neither means the attempts were exhausted.
    """

    served: bool = False
    duration: float = 0.0
    rejected: bool = False


class WsUserTransport(Transport):
    """Frame pump for the ``ws_user`` endpoint.

    Owns liveness (``heartbeat``), reconnect policy (fresh token per connect,
    jittered exponential backoff) and the debounced resync send. It never
    interprets frames, and never refreshes tokens itself.
    """

    def __init__(
        self,
        token_source: TokenSource,
        api_host: str,
        *,
        heartbeat: float = 60.0,
        should_sync_debounce: float = 1.0,
        connect_timeout: float = 10.0,
        session: ClientSession | None = None,
    ) -> None:
        """Validate the knobs and wire the seams.

        ``session=None`` makes the transport create its own
        :class:`ClientSession` lazily and own its lifecycle; tests inject one.
        The never-turned knobs live as module constants.
        """
        for name, value in (
            ("heartbeat", heartbeat),
            ("connect_timeout", connect_timeout),
        ):
            if value <= 0:
                msg = f"{name} must be > 0, got {value}"
                raise ValueError(msg)
        if should_sync_debounce < 0:
            msg = f"should_sync_debounce must be >= 0, got {should_sync_debounce}"
            raise ValueError(msg)

        self._token_source = token_source
        self._api_host = api_host
        self._heartbeat = heartbeat
        self._should_sync_debounce = should_sync_debounce
        self._connect_timeout = connect_timeout
        self._session = session
        self._owns_session = session is None
        self._handler: EventHandler | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._stopping = asyncio.Event()
        self._closed = asyncio.Event()
        self._running = False
        self._resync_task: asyncio.Task[None] | None = None
        self._auth_rejections = 0
        self._short_cycles = 0
        self._last_close_code: int | None = None
        self._last_end_reason: Literal["close_frame", "exception"] | None = None
        self._token_exp_at_connect: float | None = None
        self._rng = random.SystemRandom()

    @property
    def facts(self) -> dict[str, int | bool | str | None]:
        """Live transport facts for diagnostics (plain values, no secrets)."""
        return {
            "running": self._running,
            "short_cycle_streak": self._short_cycles,
            "auth_rejection_streak": self._auth_rejections,
            "last_close_code": self._last_close_code,
            "last_end_reason": self._last_end_reason,
        }

    @property
    def token_source(self) -> TokenSource:
        """The credential seam this transport acquires tokens from.

        Read-only: the transport owns invalidation policy, the source the token.
        """
        return self._token_source

    def subscribe(self, handler: EventHandler) -> None:
        """Register the single async event handler (replaces any previous one)."""
        self._handler = handler

    async def run(self) -> None:
        """Connect and pump frames, reconnecting per policy.

        Parks until :meth:`close` or cancellation, and is restartable after any
        exit; only :meth:`close` is terminal. Raises ``AuthError`` when the
        token source fails and ``TransportUnsupportedError`` on a deterministic
        endpoint rejection; transport failures never escape.
        """
        if self._running:
            msg = "run() is already active on this transport"
            raise RuntimeError(msg)
        # clear the previous run's exit event so close() waits for THIS run
        self._closed = asyncio.Event()
        # A restart is a fresh policy run: stale rejection/short-cycle streaks
        # must not escalate the first failure of the new run.
        self._auth_rejections = 0
        self._short_cycles = 0
        self._running = True
        try:
            while not self._stopping.is_set():
                outcome = await self._cycle()
                if self._stopping.is_set():
                    break
                await self._pause(self._between_cycles_pause(outcome))
        finally:
            self._running = False
            self._closed.set()  # before cleanup that could raise
            await self._teardown()

    async def close(self) -> None:
        """Stop the pump and tear the socket down cleanly.

        Bounded and idempotent; terminal for later ``run()`` calls.
        """
        self._stopping.set()
        await self._teardown()
        if self._running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), _TEARDOWN_TIMEOUT)
        self._closed.set()

    # --- cycle machinery ---

    async def _cycle(self) -> _CycleOutcome:
        """Run up to ``reconnect_attempts`` connect attempts.

        A handshake auth rejection ends the round: the token is invalidated and
        control returns to the run loop, whose pause escalates on the rejection
        count. Transient failures - a connect error or a token-source outage
        (``TransportUnavailableError`` from ``acquire``) - retry within the
        round under the same ladder; exhausted attempts go around, and the
        caller re-acquires a token at the top of the next cycle. Only
        ``AuthError`` / ``TransportUnsupportedError`` escape ``run()``.
        """
        for attempt in range(_RECONNECT_ATTEMPTS):
            if self._stopping.is_set():
                return _CycleOutcome()
            try:
                creds = await self._token_source.acquire()
                ws = await self._connect(
                    build_ws_url(self._api_host, creds.token, creds.user_id),
                )
            except _HandshakeAuthRejectedError:
                await self._token_source.invalidate(creds)
                self._auth_rejections += 1
                if self._auth_rejections >= _AUTH_REJECTION_LIMIT:
                    msg = "ws_user endpoint keeps rejecting fresh tokens"
                    raise TransportUnsupportedError(msg) from None
                return _CycleOutcome(rejected=True)
            except (TransportUnavailableError, ClientError, TimeoutError, OSError) as e:
                # attempt 1 (and the last) at WARNING; retries at DEBUG -
                # an outage must not emit 10 WARNINGs per connect cycle
                if attempt + 1 == 1 or attempt + 1 == _RECONNECT_ATTEMPTS:
                    _LOGGER.warning(
                        "ws_user connect attempt %d failed: %s",
                        attempt + 1,
                        type(e).__name__,
                    )
                else:
                    _LOGGER.debug(
                        "ws_user connect attempt %d failed: %s",
                        attempt + 1,
                        type(e).__name__,
                    )
                await self._pause(self._jittered_backoff(attempt))
                continue

            if self._stopping.is_set():
                # close() may return while a handshake is still in flight
                # (its wait is 5 s, the handshake bound is 10 s). The socket
                # that completes afterwards is ours to close: do not send
                # all_data or serve a connection teardown already abandoned.
                await self._close_ws_quietly(ws)
                return _CycleOutcome()

            started = timing.monotonic()
            self._ws = ws
            # a fresh connect starts with clean end facts (a stale code
            # from the previous socket must not outlive it)
            self._last_close_code = None
            self._last_end_reason = None
            # the token THIS connection was opened with: its expiry is what
            # the server cuts the socket at, so it labels the close
            self._token_exp_at_connect = jwt_expiry(creds.token)
            _LOGGER.debug("ws_user connected (heartbeat=%gs)", self._heartbeat)
            try:
                await ws.send_str(_ALL_DATA_FRAME)
                duration = await self._serve(ws)
            except (ClientError, TimeoutError, OSError) as e:
                # Silent death discriminator: a short run is a suspicious
                # connect/kill loop, a long one is routine.
                duration = timing.monotonic() - started
                log_duration = "ws_user socket ended after %.1fs: %s"
                if duration < _SHORT_CYCLE_SECONDS:
                    _LOGGER.warning(log_duration, duration, type(e).__name__)
                else:
                    _LOGGER.debug(log_duration, duration, type(e).__name__)
                # The feed's only outage signal is SocketClosed. This path
                # skips _serve's normal dispatch (send_str or receive died),
                # so report the end here or the feed stays marked up while
                # every connect keeps failing.
                await self._report_ended_after_exception()
            finally:
                self._ws = None
                await self._close_ws_quietly(ws)
            self._auth_rejections = 0
            return _CycleOutcome(served=True, duration=duration)
        _LOGGER.debug("ws_user connect attempts exhausted; retrying next cycle")
        # The engine's feed state is driven by these events alone: a cycle that
        # never served a connection must still report the socket as ended, or a
        # transport that keeps failing to connect would leave the feed marked up
        # while no frame can arrive.
        await self._dispatch(SocketClosed(code=1006))
        return _CycleOutcome()

    async def _report_ended_after_exception(self) -> None:
        """Record and report a served connection that died by exception."""
        self._last_close_code = 1006
        self._last_end_reason = "exception"
        if not self._stopping.is_set():
            await self._dispatch(
                SocketClosed(
                    code=1006,
                    expected=self._closed_at_token_expiry(),
                )
            )

    def _ensure_session(self) -> ClientSession:
        """Return the injected session, or lazily create (and own) one."""
        if self._session is None:
            self._session = ClientSession()
            self._owns_session = True
        return self._session

    async def _connect(self, url: str) -> ClientWebSocketResponse:
        """Bound the handshake; classify deterministic rejections."""
        ctx = self._ensure_session().ws_connect(url, heartbeat=self._heartbeat)
        try:
            return await asyncio.wait_for(ctx.__aenter__(), self._connect_timeout)
        except TimeoutError:
            # aiohttp's ClientWSTimeout does not bound the handshake;
            # exit the context best-effort so the socket never leaks.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(
                    ctx.__aexit__(TimeoutError, None, None),
                    _TEARDOWN_TIMEOUT,
                )
            raise
        except ClientError as e:
            status = getattr(e, "status", None)
            if isinstance(status, int):
                kind = classify_ws_status(status)
                if kind == "unsupported":
                    msg = f"ws_user endpoint rejected the handshake (status={status})"
                    raise TransportUnsupportedError(msg) from None
                if kind == "auth":
                    _LOGGER.warning(
                        "ws_user handshake auth rejection (status=%s)", status
                    )
                    raise _HandshakeAuthRejectedError from None
            raise

    async def _serve(self, ws: ClientWebSocketResponse) -> float:
        """Pump frames until the socket ends; returns the served duration.

        Until the snapshot lands the wait is bounded by ``_SNAPSHOT_TIMEOUT``
        instead of ``_SILENCE_TIMEOUT``: a socket that never delivers the
        all_data reply is a wedged session, and closing it routes the retry
        through the ordinary reconnect path. The end facts
        (``last_close_code`` / ``last_end_reason``) are recorded in a
        ``finally``, so exception deaths record too.
        """
        started = timing.monotonic()
        code = 1006
        reason: Literal["close_frame", "exception"] = "exception"
        awaiting_snapshot = True
        # Absolute deadline: every frame before the snapshot must not restart
        # the watchdog, or a chatty socket that never answers all_data would
        # never be torn down (and updates for the missing world are dropped).
        snapshot_deadline = asyncio.get_running_loop().time() + _SNAPSHOT_TIMEOUT
        try:
            while not self._stopping.is_set():
                timeout_ctx = (
                    asyncio.timeout_at(snapshot_deadline)
                    if awaiting_snapshot
                    else asyncio.timeout(_SILENCE_TIMEOUT)
                )
                try:
                    async with timeout_ctx:
                        msg = await ws.receive()
                except ServerTimeoutError:
                    _LOGGER.warning("ws_user heartbeat failed; reconnecting")
                    break
                except TimeoutError:
                    if awaiting_snapshot:
                        _LOGGER.warning(
                            "ws_user: no snapshot within %gs of connect; reconnecting",
                            _SNAPSHOT_TIMEOUT,
                        )
                    else:
                        _LOGGER.warning(
                            "ws_user socket delivered nothing for %gs; reconnecting",
                            _SILENCE_TIMEOUT,
                        )
                    break
                if msg.type in _TERMINAL_MSG_TYPES:
                    if (
                        msg.type is not WSMsgType.ERROR
                        and isinstance(msg.data, int)
                        and msg.data
                    ):
                        code = msg.data
                    # ERROR is an exception death reported through the message
                    # queue, not a close frame from the peer.
                    reason = (
                        "exception" if msg.type is WSMsgType.ERROR else "close_frame"
                    )
                    break
                if msg.type is not WSMsgType.TEXT:
                    continue
                if await self._handle_text(msg.data):
                    awaiting_snapshot = False
            if not self._stopping.is_set():
                await self._dispatch(
                    SocketClosed(code=code, expected=self._closed_at_token_expiry())
                )
            return timing.monotonic() - started
        finally:
            self._last_close_code = code
            self._last_end_reason = reason

    def _closed_at_token_expiry(self) -> bool:
        """Report whether the socket ended at the expiry of ITS OWN token.

        The vendor cuts the connection at the presented token's ``exp`` (close
        1006, TCP dropped, no close frame). Recognising it keeps that scheduled
        4-hourly reconnect out of the availability episodes; the margin absorbs
        clock skew.
        """
        exp = self._token_exp_at_connect
        return exp is not None and time.time() >= exp - _TOKEN_CUT_MARGIN

    async def _handle_text(self, raw: str) -> bool:
        """Parse and dispatch one server frame; malformed frames are skipped.

        Returns True when the frame was the all_data snapshot, which is what
        ends the connect watchdog (see ``_serve``).
        """
        try:
            frame: Any = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.debug("ws_user: dropping unparseable frame (%d bytes)", len(raw))
            return False
        if not isinstance(frame, dict):
            _LOGGER.debug("ws_user: dropping non-object frame (%d bytes)", len(raw))
            return False
        event = frame.get("event")
        if event == "all_data":
            data = frame.get("data")
            if not isinstance(data, list):
                _LOGGER.debug("ws_user: dropping all_data with malformed data")
                return False
            _LOGGER.debug("ws_user frame %r delivered (%d homes)", event, len(data))
            await self._dispatch(Snapshot(payload=data))
            return True
        if event == "update":
            await self._handle_update(frame, raw)
        elif event == "should_sync":
            _LOGGER.debug("ws_user frame %r received; scheduling resync", event)
            self._schedule_all_data()
        else:
            _LOGGER.debug("ws_user: ignoring unknown frame %r", event)
        return False

    async def _handle_update(self, frame: dict[str, Any], raw: str) -> None:
        """Dispatch one update frame; a malformed one is skipped."""
        data = frame.get("data")
        dev_id = frame.get("devid")
        if not isinstance(data, dict) or not isinstance(dev_id, str):
            _LOGGER.debug(
                "ws_user: dropping malformed update frame (%d bytes)", len(raw)
            )
            return
        path = data.get("path")
        body = data.get("body")
        if not isinstance(path, str) or body is None:
            # An update carries a path and a body; the body is normally an
            # object but the box power-limit path may push a bare scalar
            # (docs/api-ws-user.md §1.4), so its shape is the core's call.
            _LOGGER.debug(
                "ws_user: dropping update without path/body (devid=%r)", dev_id
            )
            return
        _LOGGER.debug(
            "ws_user frame 'update' delivered (devid=%r, path=%r)", dev_id, path
        )
        await self._dispatch(Update(dev_id=dev_id, path=path, body=body))

    async def _dispatch(self, event: Event) -> None:
        """Deliver one event; a bad handler must never kill the pump."""
        handler = self._handler
        if handler is None:
            return
        try:
            await handler(event)
        except Exception:  # a bad handler must not kill the pump
            _LOGGER.exception("ws_user: event handler raised; continuing")

    def _schedule_all_data(self) -> None:
        """(Re)schedule the debounced ``all_data`` send."""
        if self._resync_task is not None:
            self._resync_task.cancel()
        self._resync_task = asyncio.create_task(self._send_all_data_debounced())

    async def _send_all_data_debounced(self) -> None:
        """Send ``all_data`` once the debounce window passes."""
        try:
            await timing.sleep(self._should_sync_debounce)
            ws = self._ws
            if ws is None:
                return
            await ws.send_str(_ALL_DATA_FRAME)
            _LOGGER.debug("ws_user: sent %r after debounce", "all_data")
        except asyncio.CancelledError:
            raise
        except (ClientError, TimeoutError, OSError) as e:
            _LOGGER.warning("ws_user resync send failed: %s", type(e).__name__)

    # --- timing helpers ---

    def _jittered_backoff(self, n: int) -> float:
        """1 s * 2^n with one-sided +0-20 % jitter, capped.

        Aligned to the vendor web-app reference (contract §4.1):
        ``1s*2^n * uniform(1.0, 1.2)``. The jitter is added, never subtracted,
        so a delay is never below the bare base. The exponent saturates in the
        shared helper, whose ``2**n`` would otherwise raise OverflowError and
        kill the run loop after ~1024 short cycles.
        """
        base = backoff_delay(n, _BACKOFF_FACTOR, _MAX_SLEEP)
        return min(base * self._rng.uniform(1.0, 1.2), _MAX_SLEEP)

    def _between_cycles_pause(self, outcome: _CycleOutcome) -> float:
        """Pause before the next cycle.

        Both a served cycle shorter than ``_SHORT_CYCLE_SECONDS``
        (accept-then-kill) and a handshake auth rejection escalate the pause
        via the backoff formula, so a pathological server cannot sustain a
        per-second reconnect and token-grant loop. A long-lived served cycle
        resets the short-cycle streak; any served connection resets the
        rejection streak.
        """
        if outcome.rejected:
            return self._jittered_backoff(self._auth_rejections)
        if outcome.served:
            if outcome.duration >= _SHORT_CYCLE_SECONDS:
                self._short_cycles = 0
                return _BACKOFF_FACTOR
            self._short_cycles += 1
            return self._jittered_backoff(self._short_cycles)
        return _BACKOFF_FACTOR  # attempts exhausted

    async def _pause(self, delay: float) -> None:
        """Wait out a pause, waking early when a stop is requested.

        The sleep goes through the timing seam, so this is a FIRST_COMPLETED
        race between the seam sleep and the stop event: waiting on the event
        alone would bypass the seam and put a real wall-clock delay back into
        the tests that drive reconnects.
        """
        sleeper = asyncio.create_task(timing.sleep(delay))
        stopper = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait(
                (sleeper, stopper),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleeper, stopper):
                task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    # --- teardown ---

    async def _teardown(self) -> None:
        """Bounded cleanup; never raises, safe to call repeatedly."""
        task, self._resync_task = self._resync_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(task, _TEARDOWN_TIMEOUT)
        ws, self._ws = self._ws, None
        if ws is not None:
            await self._close_ws_quietly(ws)
        if self._owns_session and self._session is not None:
            session, self._session = self._session, None
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(session.close(), _TEARDOWN_TIMEOUT)

    async def _close_ws_quietly(self, ws: ClientWebSocketResponse) -> None:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(ws.close(), _TEARDOWN_TIMEOUT)


_TERMINAL_MSG_TYPES = frozenset(
    {
        WSMsgType.CLOSE,
        WSMsgType.CLOSING,
        WSMsgType.CLOSED,
        WSMsgType.ERROR,
    },
)


# --- capability probe: the ws_user endpoint's own one-shot handshake ---

_CLOSE_TIMEOUT = 5.0
_PROBE_TIMEOUT = 10.0


class _WsConnectCtx(Protocol):
    """The slice of ``session.ws_connect`` the probe needs (duck-typed)."""

    async def __aenter__(self) -> ClientWebSocketResponse: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


async def _exit_ws_ctx_best_effort(ctx: _WsConnectCtx) -> None:
    """Leave a half-entered ``ws_connect`` context so no socket leaks."""
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(ctx.__aexit__(TimeoutError, None, None), _CLOSE_TIMEOUT)


async def check_ws_user_support(
    token: str, api_host: str, session: ClientSession
) -> None:
    """One-shot capability probe for the ``ws_user`` endpoint.

    ``token`` must be FRESH (a precondition): with a fresh token a 401/403 is
    as deterministic a rejection as 404/410, so all four map to
    ``TransportUnsupportedError``, while 5xx, network failures and a hung host
    raise ``TransportUnavailableError``. Returns silently on success. The
    handshake is bounded and its ws context exited best-effort; aiohttp errors
    that would render the token-bearing URL never enter the chain.
    """
    url = build_ws_url(api_host, token, jwt_user_id(token))
    ctx = session.ws_connect(url)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT):
            ws = await ctx.__aenter__()
    except TimeoutError:
        await _exit_ws_ctx_best_effort(ctx)
        msg = f"ws_user handshake did not complete within {_PROBE_TIMEOUT:g}s"
        raise TransportUnavailableError(msg) from None
    except (ClientError, OSError) as e:
        status = getattr(e, "status", None)
        if isinstance(status, int) and classify_ws_status(status) != "transient":
            msg = f"ws_user endpoint rejected the capability probe (status={status})"
            raise TransportUnsupportedError(msg) from None
        if isinstance(status, int):
            # aiohttp's error text renders the URL (which carries the
            # token) - status only, chain detached, same as the cycle.
            msg = f"ws_user probe failed with a transient HTTP status (status={status})"
            raise TransportUnavailableError(msg) from None
        msg = (
            "ws_user probe failed with a transient transport error "
            f"({type(e).__name__})"
        )
        # the no-status branch: chaining could render the token-bearing
        # URL; the type name in the message carries the cause info
        raise TransportUnavailableError(msg) from None
    with contextlib.suppress(Exception):
        await asyncio.wait_for(ws.close(), _CLOSE_TIMEOUT)
