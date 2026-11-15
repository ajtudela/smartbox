"""Shared duck-typed fakes for the protocol-layer tests.

Same style throughout: no network, no real sleeps, exceptions/scripted
responses queued up front.
"""

from __future__ import annotations

import base64
from collections import deque
import json
from typing import TYPE_CHECKING

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from smartbox.protocol.ws_user import TokenSource, WsCredentials

if TYPE_CHECKING:
    import asyncio
    from typing import Any


def make_request_info(
    url: str = "https://api.smartbox.invalid/",
) -> aiohttp.RequestInfo:
    """Build a minimal real ``RequestInfo`` for the fakes' raised errors.

    Recent aiohttp stubs reject ``request_info=None``; the source under
    test only reads ``status``/``message``, but the errors stay realistic.
    """
    parsed = URL(url)
    headers = CIMultiDictProxy[str](CIMultiDict[str]())
    return aiohttp.RequestInfo(parsed, "GET", headers, parsed)


def make_jwt(claims: dict) -> str:
    """Build a minimal 3-segment JWT with ``claims`` (tests only)."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"hdr.{payload.decode()}.sig"


class SleepController:
    """Replacement for the ``timing.sleep`` seam.

    Instant mode records the requested delay and returns immediately.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float = 0) -> None:
        self.delays.append(delay)


class FakeResponse:
    """Duck-typed aiohttp response (status / content_length / json).

    ``json_error`` models a malformed body under a JSON content type
    (a real ``resp.json()`` then raises ``json.JSONDecodeError``).
    """

    content_type: str = "application/json"
    content_length: int | None

    def __init__(
        self,
        status: int = 200,
        payload: object = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.json_error = json_error
        self.content_length = 0 if payload is None else 111

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=make_request_info(),
                history=(),
                status=self.status,
                message="http error",
            )

    async def json(self, _content_type: str | None = None) -> object:
        if self.json_error is not None:
            raise self.json_error
        if self.payload is None:
            raise aiohttp.ContentTypeError(
                request_info=make_request_info(),
                history=(),
                status=self.status,
                message="no body",
            )
        return self.payload


class FakeSession:
    """Duck-typed ClientSession: scripted request outcomes, recorded calls.

    ``enqueue`` takes a :class:`FakeResponse` or an Exception to raise.
    When the script runs dry, ``default`` (if set) is served; ``gate``
    (an asyncio.Event) blocks every request start when set - useful to
    freeze a grant mid-flight for concurrency tests.
    """

    def __init__(
        self,
        default: object = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: deque = deque()
        self.default = default
        self.gate = gate

    def enqueue(self, item: object) -> None:
        self.script.append(item)

    def request(
        self, method: str, url: str, headers=None, params=None, json=None
    ) -> _ReqCtx:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            },
        )
        return _ReqCtx(self)

    def post(self, url: str, headers=None, data=None) -> _ReqCtx:
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "data": data},
        )
        return _ReqCtx(self)


class _ReqCtx:
    """Duck-typed request context manager serving the next scripted item."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeResponse:
        if self._session.gate is not None:
            await self._session.gate.wait()
        if self._session.script:
            item = self._session.script.popleft()
        else:
            item = self._session.default
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[no-any-return]

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class ProbeWS:
    """Duck-typed ws connection for the capability probe."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class HandshakeRejected:
    """Script entry: a handshake failure raising a REALISTIC error.

    The caught error's ``__str__`` renders the actual token-bearing
    URL, so secrets-in-logs assertions are non-vacuous.
    """

    def __init__(self, status: int) -> None:
        self.status = status


class FakeWSSession:
    """Duck-typed session for ``check_ws_user_support``.

    ``enqueue`` takes a :class:`ProbeWS` (or nothing - a fresh ProbeWS
    is served), an Exception to raise from the handshake, or a
    :class:`HandshakeRejected`; ``hang`` blocks the FIRST handshake
    forever (for the timeout test).
    """

    def __init__(self, hang: asyncio.Event | None = None) -> None:
        self.script: deque = deque()
        self.hang = hang
        self.hang_used = False
        self.urls: list[str] = []
        self.exits = 0
        self.last_ws: ProbeWS | None = None

    def enqueue(self, item: object) -> None:
        self.script.append(item)

    def ws_connect(self, url: str) -> _WsCtx:
        self.urls.append(url)
        return _WsCtx(self, url)


class _WsCtx:
    def __init__(self, session: FakeWSSession, url: str) -> None:
        self._session = session
        self._url = url

    async def __aenter__(self) -> ProbeWS:
        session = self._session
        if session.hang is not None and not session.hang_used:
            session.hang_used = True
            await session.hang.wait()
        if session.script:
            item = session.script.popleft()
            if isinstance(item, HandshakeRejected):
                raise aiohttp.ClientResponseError(
                    request_info=make_request_info(self._url),
                    history=(),
                    status=item.status,
                    message="handshake rejected",
                )
            if isinstance(item, Exception):
                raise item
        else:
            item = ProbeWS()
        session.last_ws = item
        return item

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._session.exits += 1
        return False


class FakeTokenSource(TokenSource):
    """Records acquire/invalidate calls; call i yields ``t{i}``/``u{i}``.

    With ``fixed`` set, every acquire returns the SAME credentials -
    models deterministic token re-issues / no-op refreshes under
    clock skew (a verified wire behavior of the endpoint).
    """

    def __init__(
        self, fail_with: Exception | None = None, fixed: str | None = None
    ) -> None:
        self.calls = 0
        self.invalidated: list[WsCredentials] = []
        self.fail_with = fail_with
        self.fixed = fixed

    async def acquire(self) -> WsCredentials:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.fixed is not None:
            return WsCredentials(token=self.fixed, user_id="u-fixed")
        return WsCredentials(token=f"t{self.calls}", user_id=f"u{self.calls}")

    async def invalidate(self, rejected: WsCredentials) -> None:
        self.invalidated.append(rejected)
