"""Tests for the REST client plumbing (``smartbox_protocol.client``).

Plain pytest; the HTTP layer is a scripted FakeSession (shared in
``fakes.py``), sleeps are intercepted via the ``timing.sleep`` seam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback

import aiohttp
import pytest

from smartbox.protocol import client as client_mod, timing, ws_user as ws_user_mod
from smartbox.protocol.auth import AuthConfig
from smartbox.protocol.client import NodeRef, SmartboxClient
from smartbox.protocol.errors import (
    AuthError,
    SmartboxError,
    TransportUnavailableError,
    TransportUnsupportedError,
)
from smartbox.protocol.ws_user import check_ws_user_support
from tests.fakes import (
    FakeResponse,
    FakeSession,
    FakeWSSession,
    HandshakeRejected,
    SleepController,
    make_jwt,
    make_request_info,
)
from tests.utils import assert_backoff_delays, live_fixture

API_HOST = "https://api.example.test"

CONFIG = AuthConfig(
    api_host=API_HOST,
    basic_auth_creds="cmVzZWxsZXJfc2VjcmV0",
    serial_id="1234",
    username="user@example.test",
    password="hunter2",
)

JWT_U1 = make_jwt({"userId": "u1", "exp": 9999999999})
JWT_U2 = make_jwt({"userId": "u2", "exp": 9999999999})

GRANT_U1 = {"access_token": JWT_U1, "refresh_token": "rt1", "expires_in": 14400}
GRANT_U2 = {"access_token": JWT_U2, "refresh_token": "rt2", "expires_in": 14400}

NODE = NodeRef(node_type="htr", addr="2")


@pytest.fixture
def sleeps(monkeypatch):
    controller = SleepController()
    monkeypatch.setattr(timing, "sleep", controller)
    monkeypatch.setattr(timing, "sleep", controller)
    return controller


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def client(session) -> SmartboxClient:
    return SmartboxClient(CONFIG, session=session)


def grant_calls(session) -> list[dict]:
    return [c for c in session.calls if c["method"] == "POST"]


def api_calls(session) -> list[dict]:
    return [c for c in session.calls if c["method"] == "GET"]


# --- read operations: raw passthrough ---


async def test_get_devices_passthrough(sleeps, session, client):
    payload = {"devs": [{"dev_id": "d1", "name": "Heater", "serial_id": 42}]}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    result = await client.get_devices()
    assert result == payload
    call = api_calls(session)[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{API_HOST}/api/v2/devs"
    assert call["headers"]["Authorization"] == f"Bearer {JWT_U1}"


async def test_get_grouped_devices_keeps_extra_data(sleeps, session, client):
    payload = [
        {"id": "h1", "name": "Home", "extraData": {"x": 1}, "devs": [], "owner": True}
    ]
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_grouped_devices() == payload
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/grouped_devs"


async def test_get_home_guests_uses_groups_path(sleeps, session, client):
    payload = {"guest_users": [{"id": "g1"}]}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_home_guests("home 1") == payload
    assert (
        api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/groups/home%201/guest_users"
    )


async def test_get_dev_data_passthrough_unchanged(sleeps, session, client):
    payload = {"mtemp": "190", "units": "C", "htr_system": {"power_limit": 2500}}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_dev_data("dev1") == payload
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/devs/dev1/dev_data"


async def test_get_nodes_unwraps_nodes_list(sleeps, session, client):
    nodes = [{"node_type": "htr", "addr": "2"}, {"node_type": "htr", "addr": "3"}]
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"nodes": nodes}))
    assert await client.get_nodes("dev1") == nodes
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/devs/dev1/mgr/nodes"


async def test_get_nodes_malformed_payload_raises(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"nope": []}))
    with pytest.raises(SmartboxError, match="nodes"):
        await client.get_nodes("dev1")


async def test_2xx_undecodable_json_raises_smartbox_error(sleeps, session, client):
    """A JSON-typed 2xx body that fails to decode enters the contract."""
    body = json.JSONDecodeError("Expecting value", "<html>err</html>", 0)
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"truncated": 1}, json_error=body))
    with pytest.raises(SmartboxError, match="undecodable JSON body") as excinfo:
        await client.get_devices()
    assert "<html>" not in str(excinfo.value)  # body never reaches the message


async def test_2xx_empty_body_still_decodes_to_empty_dict(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse())
    assert await client.get_devices() == {}


async def test_2xx_non_utf8_body_raises_smartbox_error(sleeps, session, client):
    """Bytes that are not decodable text enter the contract, not the caller.

    aiohttp decodes before parsing, so invalid UTF-8 under a JSON content
    type raises UnicodeDecodeError (a ValueError, but not a
    JSONDecodeError); a proxy answering with junk must not surface a raw
    decode error.
    """
    body = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"x": 1}, json_error=body))
    with pytest.raises(SmartboxError, match="undecodable JSON body"):
        await client.get_devices()


class _NoContentLengthNullBody(FakeResponse):
    """A chunked 200: no Content-Length at all, and aiohttp yields None."""

    content_length = None

    async def json(self, _content_type=None):
        return None


async def test_2xx_bodyless_chunked_response_decodes_to_empty_dict(session, client):
    """A bodyless success is {} whatever framing the proxy used.

    Without a Content-Length the empty-body check cannot see it, so the
    decoded value itself has to fall back.
    """
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(_NoContentLengthNullBody())
    assert await client.get_connected("dev1") == {}


class _NonJsonBody(FakeResponse):
    """A 200 with a body that is not JSON (a captive portal / proxy)."""

    content_type = "text/html"
    content_length = 111

    async def json(self, _content_type: str | None = None) -> object:
        raise aiohttp.ContentTypeError(
            request_info=make_request_info(),
            history=(),
            status=self.status,
            message="not json",
        )


async def test_2xx_non_json_body_decodes_to_empty_dict(session, client):
    """A non-JSON 200 is "nothing to report", not an error.

    Distinct from the undecodable-JSON case above: a JSON content type with
    a broken body is a malformed success, while a body that is not JSON at
    all (a proxy page) is treated as empty.
    """
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(_NonJsonBody())
    assert await client.get_connected("dev1") == {}


async def test_token_wait_is_inside_the_call_deadline(session, client, monkeypatch):
    """The per-call budget covers the token wait, not just the request.

    A caller queued behind a concurrent grant must not be able to push the
    request past the documented bound.
    """
    monkeypatch.setattr(client_mod, "_CALL_TIMEOUT", 0.05)

    async def slow_token(*_args, **_kwargs):
        await asyncio.sleep(1.0)
        msg = "the call deadline should have cut this short"
        raise AssertionError(msg)

    monkeypatch.setattr(client._auth, "get_token", slow_token)
    with pytest.raises(TransportUnavailableError, match="unreachable"):
        await client.get_nodes("dev1")


async def test_get_power_limit_raw_passthrough_string_form(sleeps, session, client):
    """GET behavior: the raw payload passes through as-is.

    The wire serves the limit as a digit string (E17/E18); the client
    deliberately passes the raw body through rather than extracting an int,
    so the callers see exactly what the host sent.
    """
    payload = {"power_limit": "2500"}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_power_limit("dev1") == payload


@pytest.mark.parametrize(
    ("leaf", "expected_path"),
    [
        ("status", "devs/dev1/htr/2/status"),
        ("setup", "devs/dev1/htr/2/setup"),
        ("prog", "devs/dev1/htr/2/prog"),
        ("version", "devs/dev1/htr/2/version"),
    ],
)
async def test_node_leaf_paths(sleeps, session, client, leaf, expected_path):
    payload = {"anything": "raw"}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    method = getattr(client, f"get_node_{leaf}")
    assert await method("dev1", NODE) == payload
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/{expected_path}"


@pytest.mark.parametrize(
    ("method_name", "expected_path", "payload"),
    [
        ("get_connected", "devs/dev1/connected", {"connected": True}),
        (
            "get_away_status",
            "devs/dev1/mgr/away_status",
            {"away": False, "enabled": False, "forced": False},
        ),
        ("get_power_limit", "devs/dev1/htr_system/power_limit", {"power_limit": 2500}),
        (
            "get_rtc_time",
            "devs/dev1/mgr/rtc/time",
            {"d": 4, "h": 10, "m": 30, "n": 8, "s": 0, "w": 4, "y": 126},
        ),
        ("get_discovery", "devs/dev1/mgr/discovery", {"discovery": "off"}),
    ],
)
async def test_dev_endpoints_raw(
    sleeps, session, client, method_name, expected_path, payload
):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await getattr(client, method_name)("dev1") == payload
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/{expected_path}"


async def test_get_node_samples_unwraps_the_wire_and_carries_start_end(
    sleeps, session, client
):
    """The wire wraps the rows in ``{"samples": [...]}``; the call unwraps."""
    samples = [{"t": 1000, "counter": 190}]
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"samples": samples}))
    assert await client.get_node_samples("dev1", NODE, 1000, 2000) == samples
    call = api_calls(session)[0]
    assert call["url"] == f"{API_HOST}/api/v2/devs/dev1/htr/2/samples"
    assert call["params"] == {"start": 1000, "end": 2000}


async def test_get_node_samples_accepts_a_bare_list(sleeps, session, client):
    samples = [{"t": 1000, "counter": 190}]
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=samples))
    assert await client.get_node_samples("dev1", NODE, 1000, 2000) == samples


async def test_get_node_samples_rejects_a_malformed_payload(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"samples": "nope"}))
    with pytest.raises(SmartboxError, match="unexpected samples payload"):
        await client.get_node_samples("dev1", NODE, 1000, 2000)


async def test_shared_session_used_by_client_and_auth(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={}))
    assert client.session is session
    await client.get_devices()
    assert grant_calls(session)
    assert api_calls(session)  # both went through it


# --- real-payload passthrough (live fixtures) ---


async def test_live_fixture_node_setup_passthrough_unchanged(sleeps, session, client):
    payload = live_fixture("htr_setup_addr2.json")
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_node_setup("dev1", NODE) == payload


async def test_live_fixture_node_status_passthrough_unchanged(sleeps, session, client):
    payload = live_fixture("htr_status_boost_off.json")
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_node_status("dev1", NODE) == payload


# --- error contract ---


async def test_401_invalidates_and_resends_exactly_once(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(FakeResponse(payload=GRANT_U2))
    session.enqueue(FakeResponse(payload={"devs": []}))
    assert await client.get_devices() == {"devs": []}
    assert len(grant_calls(session)) == 2
    assert grant_calls(session)[1]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt1",
    }
    assert len(api_calls(session)) == 2
    # steady state after the resend: no further grants
    session.enqueue(FakeResponse(payload={"devs": []}))
    await client.get_devices()
    assert len(grant_calls(session)) == 2


async def test_second_401_after_reauth_raises_auth_error(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(FakeResponse(payload=GRANT_U2))
    session.enqueue(FakeResponse(status=401))
    with pytest.raises(AuthError):
        await client.get_devices()


async def test_5xx_retried_then_transport_unavailable(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    for _ in range(3):
        session.enqueue(FakeResponse(status=503))
    with pytest.raises(TransportUnavailableError, match="503"):
        await client.get_devices()
    assert_backoff_delays(sleeps.delays, [1.0, 2.0])
    assert len(api_calls(session)) == 3


async def test_5xx_recovered_on_retry(sleeps, session, client):
    payload = {"devs": [{"dev_id": "d1"}]}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(status=500))
    session.enqueue(FakeResponse(payload=payload))
    assert await client.get_devices() == payload
    assert_backoff_delays(sleeps.delays, [1.0])


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), aiohttp.ClientError("reset"), OSError("net down")],
)
async def test_transient_network_failures_retried(sleeps, session, client, failure):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(failure)
    session.enqueue(FakeResponse(payload={"devs": []}))
    assert await client.get_devices() == {"devs": []}
    assert len(api_calls(session)) == 2


async def test_other_4xx_raises_smartbox_error_with_status(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(status=404))
    with pytest.raises(SmartboxError, match="404"):
        await client.get_devices()


async def test_unauthenticated_endpoint_401_is_plain_smartbox_error(
    sleeps, session, caplog
):
    """A 401 on a host endpoint is a plain 4xx: no token plumbing, no NameError.

    The 401 branch used to reference the token bundle unconditionally,
    which raised NameError for the unauthenticated ``/version`` /
    ``/health_check`` reads.
    """
    client = SmartboxClient(CONFIG, session=session)
    session.enqueue(FakeResponse(status=401))
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(SmartboxError, match="status=401"),
    ):
        await client.get_api_version()
    # No grant was ever attempted (no token requested for host endpoints).
    assert [call["method"] for call in session.calls] == ["GET"]


async def test_deadline_bounded(sleeps):
    session = FakeSession(default=FakeResponse(status=500))
    client = SmartboxClient(CONFIG, session=session)
    with pytest.raises(TransportUnavailableError):
        await asyncio.wait_for(client.get_devices(), 2.0)


# --- secrets hygiene ---


async def test_no_secrets_in_logs(sleeps, session, client, caplog):
    caplog.set_level(logging.DEBUG, logger="smartbox.protocol.client")
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(FakeResponse(payload=GRANT_U2))
    session.enqueue(FakeResponse(payload={"devs": []}))
    await client.get_devices()
    log_text = caplog.text
    for fragment in (JWT_U1, JWT_U2, "rt1", "rt2", "Bearer"):
        assert fragment not in log_text
    assert "GET /api/v2/devs" in log_text  # one debug line per call
    assert "smartbox GET /api/v2/devs -> 200" in log_text


# --- capability probe ---


async def test_probe_ok_returns_none_and_closes_ws(sleeps, monkeypatch):
    session = FakeWSSession()
    result = await check_ws_user_support(JWT_U1, API_HOST, session)
    assert result is None
    assert session.urls == [
        f"wss://api.example.test:443/api/v2/ws_user?token={JWT_U1}&user_id=u1",
    ]
    assert session.last_ws.closed


def handshake_error(status: int) -> HandshakeRejected:
    """Realistic handshake failure.

    The caught error's text renders the actual token-bearing ws_user
    URL, so the no-token assertions are non-vacuous.
    """
    return HandshakeRejected(status)


@pytest.mark.parametrize("status", [401, 403, 404, 410])
async def test_probe_deterministic_rejection_raises_unsupported(status):
    session = FakeWSSession()
    session.enqueue(handshake_error(status))
    with pytest.raises(TransportUnsupportedError, match=str(status)):
        await check_ws_user_support(JWT_U1, API_HOST, session)


@pytest.mark.parametrize(
    "failure",
    [
        handshake_error(500),
        aiohttp.ClientError("reset"),
        OSError("net down"),
    ],
)
async def test_probe_transient_failure_raises_unavailable(failure):
    session = FakeWSSession()
    session.enqueue(failure)
    with pytest.raises(TransportUnavailableError):
        await check_ws_user_support(JWT_U1, API_HOST, session)


async def test_probe_http_error_chain_carries_no_token_url(caplog):
    # a ClientResponseError's text renders the full URL (with the raw
    # token) - the raised error must detach it from the chain, and the
    # probe must not log it either
    caplog.set_level(logging.DEBUG)
    session = FakeWSSession()
    session.enqueue(handshake_error(503))
    with pytest.raises(TransportUnavailableError) as excinfo:
        await check_ws_user_support(JWT_U1, API_HOST, session)
    assert excinfo.value.__cause__ is None
    assert "status=503" in str(excinfo.value)
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
            chain=True,
        ),
    )
    assert JWT_U1 not in rendered
    assert "ws_user?token" not in rendered
    log_text = caplog.text
    for fragment in (JWT_U1, "ws_user?token", "token="):
        assert fragment not in log_text


async def test_probe_statusless_error_chain_detached():
    """A ClientError WITHOUT status must not chain (its text may bear the URL).

    The message keeps the exception type name instead.
    """
    session = FakeWSSession()
    session.enqueue(
        aiohttp.ClientError(
            f"connection reset for wss://api.example.test/ws_user?token={JWT_U1}"
        ),
    )
    with pytest.raises(TransportUnavailableError) as excinfo:
        await check_ws_user_support(JWT_U1, API_HOST, session)
    assert excinfo.value.__cause__ is None
    assert "ClientError" in str(excinfo.value)
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
            chain=True,
        ),
    )
    assert JWT_U1 not in rendered


async def test_probe_hang_is_bounded(monkeypatch):
    monkeypatch.setattr(ws_user_mod, "_PROBE_TIMEOUT", 0.05)
    session = FakeWSSession(hang=asyncio.Event())
    with pytest.raises(TransportUnavailableError, match="did not complete within"):
        await asyncio.wait_for(
            check_ws_user_support(JWT_U1, API_HOST, session),
            2.0,
        )
    assert session.exits == 1  # half-open ws context exited best-effort


async def test_get_node_power_returns_int(sleeps, session, client):
    """PMO power read: the ``/power`` GET, int-normalized."""
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"power": "2500"}))
    assert await client.get_node_power("dev1", NODE) == 2500
    assert api_calls(session)[0]["url"] == f"{API_HOST}/api/v2/devs/dev1/htr/2/power"


async def test_get_node_power_malformed_payload_raises(sleeps, session, client):
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={"nope": 1}))
    with pytest.raises(SmartboxError, match="power"):
        await client.get_node_power("dev1", NODE)


async def test_get_node_power_giant_payload_error_bounded(sleeps, session, client):
    """A giant malformed payload never lands verbatim in the message."""
    giant = {"nope": "x" * 5000}
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload=giant))
    with pytest.raises(SmartboxError) as excinfo:
        await client.get_node_power("dev1", NODE)
    assert len(str(excinfo.value)) < 300
    assert "x" * 100 not in str(excinfo.value)


# --- unauthenticated host endpoints (system health) ---


@pytest.mark.parametrize(
    ("method_name", "expected_url", "payload"),
    [
        (
            "get_api_version",
            f"{API_HOST}/version",
            {"major": 1, "minor": 2, "subminor": 3, "commit": "abc"},
        ),
        ("health_check", f"{API_HOST}/health_check", {"message": "OK"}),
    ],
)
async def test_unauthed_host_endpoints(
    sleeps, session, client, method_name, expected_url, payload
):
    """Host endpoints outside /api/v2 need NO token (no grant call)."""
    session.enqueue(FakeResponse(payload=payload))
    assert await getattr(client, method_name)() == payload
    call = api_calls(session)[0]
    assert call["url"] == expected_url
    assert "Authorization" not in call["headers"]
    assert grant_calls(session) == []


async def test_authenticated_calls_carry_the_reseller_serial(sleeps, session, client):
    """Every authenticated call carries `x-serialid` (contract §1.2)."""
    session.enqueue(FakeResponse(payload=GRANT_U1))
    session.enqueue(FakeResponse(payload={}))
    await client.get_devices()
    call = api_calls(session)[0]
    assert call["headers"] == {
        "Authorization": f"Bearer {JWT_U1}",
        "x-serialid": CONFIG.serial_id,
    }
