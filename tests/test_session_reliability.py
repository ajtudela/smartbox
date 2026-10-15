"""Reliability/contract regressions from code-review round 5.

Covers: secret redaction in error text, error mapping for truncated and
undecodable bodies, the whole-call deadline (``_CALL_TIMEOUT``), retry
edge cases, the refresh-token -> password fallback, 401 re-auth, and the
raw_response contract (raw = unvalidated wire payload; model mode raises
``SmartboxValidationError``).
"""

import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import ClientSession
import pydantic
import pytest

from smartbox import (
    APIUnavailableError,
    InvalidAuthError,
    SmartboxError,
    SmartboxValidationError,
)
from smartbox.retry import jittered
from smartbox.session import AsyncSmartboxSession
from tests.common import mock_api_response


class _WireHandler(BaseHTTPRequestHandler):
    """Local wire for failure modes the real aiohttp client must survive."""

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_length: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header(
            "Content-Length",
            str(len(body) if content_length is None else content_length),
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if "encrypted_wifi_credentials" in self.path:
            self._send(500)
        elif self.path.startswith("/api/v2/truncated"):
            # Promise 100 bytes, deliver 5, drop the connection.
            self._send(200, b'{"a":', content_length=100)
            self.wfile.flush()
            self.connection.close()
        elif self.path.startswith("/api/v2/badjson"):
            self._send(200, b'{"a":1,')
        elif self.path.startswith("/api/v2/slow"):
            time.sleep(1)
            self._send(200, b"{}")
        else:
            self._send(404)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def wire_session(reseller):
    """Yield a session with a valid token, talking to the local wire."""
    server = HTTPServer(("127.0.0.1", 0), _WireHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    session = AsyncSmartboxSession(
        api_name="test_api",
        username="u",
        password="p",
        retry_attempts=2,
        backoff_factor=0,
    )
    session._api_host = f"http://127.0.0.1:{server.server_address[1]}"
    session._access_token = "tok"
    session._expires_at = datetime.datetime.now(
        datetime.UTC,
    ) + datetime.timedelta(hours=1)
    yield session
    server.shutdown()


@pytest.fixture
def mocked_client_session(async_session):
    """``async_session`` with auth stubbed out (client already mocked)."""
    with patch.object(
        async_session,
        "check_refresh_auth",
        new_callable=AsyncMock,
    ):
        yield async_session


def _exception_chain_text(exc: BaseException | None) -> list[str]:
    texts = []
    while exc is not None:
        texts.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    return texts


# --- error mapping & redaction -------------------------------------------


@pytest.mark.asyncio
async def test_wifi_password_not_leaked_on_error(wire_session, caplog):
    """Regression: aiohttp's error text embeds the full URL incl. ?pass=."""
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="smartbox.session"),
            pytest.raises(APIUnavailableError) as exc_info,
        ):
            await wire_session.get_encrypted_wifi_credentials(
                "ssid",
                "SUPERSECRETWIFI",
            )
    finally:
        await wire_session.aclose_owned_session()
    assert "HTTP 500" in str(exc_info.value)
    assert "SUPERSECRETWIFI" not in caplog.text
    assert all(
        "SUPERSECRETWIFI" not in text
        for text in _exception_chain_text(exc_info.value)
    )


@pytest.mark.asyncio
async def test_truncated_body_is_api_unavailable(wire_session):
    """Regression: ClientPayloadError escaped the library's error types."""
    try:
        with pytest.raises(APIUnavailableError, match="ClientPayloadError"):
            await wire_session._api_request("truncated")
    finally:
        await wire_session.aclose_owned_session()


@pytest.mark.asyncio
async def test_undecodable_json_is_smartbox_error(wire_session):
    """Regression: a raw json.JSONDecodeError escaped the library."""
    try:
        with pytest.raises(SmartboxError, match="undecodable JSON"):
            await wire_session._api_request("badjson")
    finally:
        await wire_session.aclose_owned_session()


# --- whole-call deadline -------------------------------------------------


@pytest.mark.asyncio
async def test_call_budget_bounds_caller_provided_session(
    wire_session,
    monkeypatch,
):
    """The call deadline applies to caller-provided websessions too."""
    monkeypatch.setattr("smartbox.session._CALL_TIMEOUT", 0.3)
    websession = ClientSession()  # aiohttp default: 300s total timeout
    wire_session._client_session = websession
    start = time.monotonic()
    try:
        with pytest.raises(APIUnavailableError, match="did not complete"):
            await wire_session._api_request("slow")
    finally:
        await websession.close()
    assert time.monotonic() - start < 0.9


@pytest.mark.asyncio
async def test_call_budget_covers_retries_and_backoff(
    mocked_client_session,
    monkeypatch,
):
    """Retries + backoff share one budget; an overrunning backoff is skipped."""
    session = mocked_client_session
    monkeypatch.setattr("smartbox.session._CALL_TIMEOUT", 0.5)
    session._retry_attempts = 5
    session._backoff_factor = 0.2  # 0.2s fits; the next 0.4s would not
    with patch.object(session.client, "request") as mock_request:
        mock_request.side_effect = aiohttp.ClientConnectionError("down")
        start = time.monotonic()
        with pytest.raises(APIUnavailableError, match="down"):
            await session._api_request("test_path")
    assert mock_request.call_count == 2
    assert time.monotonic() - start < 0.45


@pytest.mark.asyncio
async def test_retry_attempts_zero_still_makes_one_attempt(
    mocked_client_session,
):
    """Regression: retry_attempts=0 raised TypeError (``raise None``)."""
    session = mocked_client_session
    session._retry_attempts = 0
    with patch.object(session.client, "request") as mock_request:
        mock_request.side_effect = aiohttp.ClientConnectionError()
        with pytest.raises(APIUnavailableError):
            await session._api_request("test_path")
    assert mock_request.call_count == 1


# --- auth recovery -------------------------------------------------------


def _expire(session, access="old", refresh="dead"):
    session._access_token = access
    session._refresh_token = refresh
    session._expires_at = datetime.datetime.now(datetime.UTC)


def _accept(session, token):
    session._access_token = token
    session._headers["Authorization"] = f"Bearer {token}"
    session._expires_at = datetime.datetime.now(
        datetime.UTC,
    ) + datetime.timedelta(hours=1)


@pytest.mark.asyncio
async def test_rejected_refresh_token_falls_back_to_password(async_session):
    """Regression: a dead refresh token was retried forever, never the password."""
    _expire(async_session)
    grants = []

    async def auth(credentials):
        grants.append(credentials["grant_type"])
        if credentials["grant_type"] == "refresh_token":
            msg = "401"
            raise InvalidAuthError(msg)
        _accept(async_session, "new")

    with patch.object(async_session, "_authentication", side_effect=auth):
        await async_session.check_refresh_auth()
    assert grants == ["refresh_token", "password"]
    assert async_session.access_token == "new"


@pytest.mark.asyncio
async def test_both_grants_rejected_raises_invalid_auth(async_session):
    """Both grants rejected -> InvalidAuthError; the next call skips refresh."""
    _expire(async_session)
    grants = []

    async def auth(credentials):
        grants.append(credentials["grant_type"])
        msg = "401"
        raise InvalidAuthError(msg)

    with patch.object(async_session, "_authentication", side_effect=auth):
        with pytest.raises(InvalidAuthError):
            await async_session.check_refresh_auth()
        with pytest.raises(InvalidAuthError):
            await async_session.check_refresh_auth()
    assert grants == ["refresh_token", "password", "password"]


@pytest.mark.asyncio
async def test_transient_refresh_failure_does_not_fall_back(async_session):
    """Only a rejected refresh token triggers the password login."""
    _expire(async_session)
    grants = []

    async def auth(credentials):
        grants.append(credentials["grant_type"])
        msg = "down"
        raise APIUnavailableError(msg)

    with (
        patch.object(async_session, "_authentication", side_effect=auth),
        pytest.raises(APIUnavailableError),
    ):
        await async_session.check_refresh_auth()
    assert grants == ["refresh_token"]
    assert async_session.refresh_token == "dead"


@pytest.mark.asyncio
async def test_401_reauthenticates_and_resends_once(async_session):
    """A mid-session 401 forces a token refresh and exactly one resend."""
    _accept(async_session, "stale")
    async_session._refresh_token = "r"

    async def auth(credentials):
        _accept(async_session, "fresh")

    with (
        patch.object(
            async_session,
            "_authentication",
            side_effect=auth,
        ) as mock_auth,
        patch.object(async_session.client, "request") as mock_request,
    ):
        mock_request.side_effect = [
            aiohttp.ClientResponseError(
                request_info=MagicMock(),
                history=(),
                status=401,
                message="Unauthorized",
            ),
            mock_api_response({"ok": 1}),
        ]
        assert await async_session._api_request("test_path") == {"ok": 1}
    mock_auth.assert_called_once_with(
        {"grant_type": "refresh_token", "refresh_token": "r"},
    )
    assert mock_request.call_count == 2
    assert (
        mock_request.call_args.kwargs["headers"]["Authorization"]
        == "Bearer fresh"
    )


# --- raw_response contract -----------------------------------------------

_DEVICE = {
    "dev_id": "d1",
    "name": "Dev",
    "product_id": "p",
    "fw_version": "1",
    "serial_id": "s",
    "extra_wire_key": "kept",
}


@pytest.mark.parametrize(
    ("method", "kwargs", "payload", "expected"),
    [
        (
            "get_devices",
            {},
            {"devs": [_DEVICE], "invited_to": [{"dev_id": "d2"}]},
            [_DEVICE, {"dev_id": "d2"}],
        ),
        ("get_devices", {}, {"devs": [_DEVICE]}, [_DEVICE]),
        (
            "get_homes",
            {},
            [{"id": "h", "name": "Home", "extra_wire_key": 1}],
            [{"id": "h", "name": "Home", "extra_wire_key": 1}],
        ),
        (
            "get_grouped_devices",
            {},
            [{"id": "h", "name": "Home"}],
            [{"id": "h", "name": "Home"}],
        ),
        (
            "get_home_guests",
            {"home_id": "h"},
            {"guest_users": [{"email": "a@b"}]},
            [{"email": "a@b"}],
        ),
        (
            "get_device_connected",
            {"device_id": "d1"},
            {"connected": True, "extra": 1},
            {"connected": True, "extra": 1},
        ),
        (
            "get_device_away_status",
            {"device_id": "d1"},
            {"away": False},
            {"away": False},
        ),
    ],
)
@pytest.mark.asyncio
async def test_raw_mode_returns_unvalidated_wire_payload(
    async_smartbox_session,
    method,
    kwargs,
    payload,
    expected,
):
    """raw_response=True never validates: drifted payloads pass through."""
    async_smartbox_session.raw_response = True
    with patch.object(
        async_smartbox_session,
        "_api_request",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        result = await getattr(async_smartbox_session, method)(**kwargs)
    assert result == expected


@pytest.mark.parametrize(
    ("method", "kwargs", "payload"),
    [
        ("get_devices", {}, {"unexpected": "shape"}),
        ("get_devices", {}, [1, 2]),
        ("get_devices", {}, {"devs": [], "invited_to": "bogus"}),
        ("get_homes", {}, {"not": "a list"}),
        ("get_grouped_devices", {}, None),
        ("get_home_guests", {"home_id": "h"}, {"guests": []}),
    ],
)
@pytest.mark.asyncio
async def test_raw_mode_unextractable_payload_raises_smartbox_error(
    async_smartbox_session,
    method,
    kwargs,
    payload,
):
    async_smartbox_session.raw_response = True
    with (
        patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
            return_value=payload,
        ),
        pytest.raises(SmartboxError, match="Unexpected"),
    ):
        await getattr(async_smartbox_session, method)(**kwargs)


_NODE = {"name": "N", "addr": 1, "type": "htr", "installed": True}


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("get_devices", {}),
        ("get_homes", {}),
        ("get_grouped_devices", {}),
        ("get_home_guests", {"home_id": "h"}),
        ("get_nodes", {"device_id": "d1"}),
        ("get_device_connected", {"device_id": "d1"}),
        ("get_device_away_status", {"device_id": "d1"}),
        ("get_node_status", {"device_id": "d1", "node": _NODE}),
        ("get_node_setup", {"device_id": "d1", "node": _NODE}),
        ("get_node_version", {"device_id": "d1", "node": _NODE}),
        ("get_node_prog", {"device_id": "d1", "node": _NODE}),
        ("get_node_samples", {"device_id": "d1", "node": _NODE}),
    ],
)
@pytest.mark.asyncio
async def test_model_mode_drift_raises_smartbox_validation_error(
    async_smartbox_session,
    method,
    kwargs,
):
    """raw_response=False: drift raises the contract error, chained."""
    async_smartbox_session.raw_response = False
    with (
        patch.object(
            async_smartbox_session,
            "_api_request",
            new_callable=AsyncMock,
            return_value={"drifted": True},
        ),
        pytest.raises(SmartboxValidationError) as exc_info,
    ):
        await getattr(async_smartbox_session, method)(**kwargs)
    assert isinstance(exc_info.value, SmartboxError)
    assert isinstance(exc_info.value.__cause__, pydantic.ValidationError)


# --- token endpoint error mapping (round 6) -------------------------------


@pytest.mark.asyncio
async def test_token_endpoint_client_payload_error_is_unavailable(
    async_session,
):
    """Regression: a truncated token body killed SocketSession.run().

    The raw aiohttp ClientPayloadError escaped ``_authentication`` (and
    then ``_refresh_auth``'s retry net), ending the websocket loop for
    good; the REST path already mapped it to ``APIUnavailableError``.
    """
    _expire(async_session)
    with (
        patch.object(async_session.client, "post") as mock_post,
        pytest.raises(APIUnavailableError),
    ):
        mock_post.side_effect = aiohttp.ClientPayloadError("truncated")
        await async_session.check_refresh_auth()


@pytest.mark.asyncio
async def test_token_endpoint_undecodable_json_is_smartbox_error(
    async_session,
):
    """Regression: a malformed 200 token body escaped the library."""
    _expire(async_session)
    resp = AsyncMock()
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(
        side_effect=json.JSONDecodeError("Expecting value", "{bad", 0)
    )
    with (
        patch.object(async_session.client, "post", return_value=resp),
        pytest.raises(SmartboxError, match="undecodable JSON body"),
    ):
        await async_session.check_refresh_auth()


@pytest.mark.asyncio
async def test_token_endpoint_429_is_unavailable_not_invalid_auth(
    async_session,
):
    """Rate limiting is transient, not a credential rejection.

    As InvalidAuthError it would trigger the password-login fallback
    while being rate-limited (amplifying the limit) and end the
    websocket loop; as APIUnavailableError the websocket loop waits the
    window out with capped backoff.
    """
    _expire(async_session)
    err = aiohttp.ClientResponseError(
        MagicMock(),
        (),
        status=429,
        message="Too Many Requests",
    )
    with (
        patch.object(async_session.client, "post") as mock_post,
        pytest.raises(APIUnavailableError),
    ):
        mock_post.side_effect = err
        await async_session.check_refresh_auth()


@pytest.mark.asyncio
async def test_token_endpoint_400_is_invalid_auth(async_session):
    """Genuine non-transient rejections still surface as InvalidAuthError."""
    _expire(async_session)
    err = aiohttp.ClientResponseError(
        MagicMock(),
        (),
        status=400,
        message="invalid_grant",
    )
    with (
        patch.object(async_session.client, "post") as mock_post,
        pytest.raises(InvalidAuthError),
    ):
        mock_post.side_effect = err
        await async_session.check_refresh_auth()


def test_jittered_stays_within_spread():
    """Jitter only ever adds up to ``spread`` of the base delay."""
    for _ in range(200):
        assert 1.0 <= jittered(1.0) <= 1.2
    assert jittered(0.0) == 0.0
