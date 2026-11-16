"""Tests for the token lifecycle (``smartbox_protocol.auth``).

Plain pytest. All sleeps are intercepted via the ``timing.sleep`` seam
(instant recorder); no test ever really waits for backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging

import aiohttp
import pytest

from smartbox.protocol import auth as auth_mod, timing
from smartbox.protocol.auth import (
    AuthConfig,
    ManagedTokenSource,
    TokenBundle,
    TokenManager,
)
from smartbox.protocol.errors import AuthError, SmartboxError, TransportUnavailableError
from smartbox.protocol.ws_user import WsCredentials
from tests.fakes import FakeResponse, FakeSession, SleepController, make_jwt
from tests.utils import assert_backoff_delays

API_HOST = "https://api.example.test"

CONFIG = AuthConfig(
    api_host=API_HOST,
    basic_auth_creds="cmVzZWxsZXJfc2VjcmV0",
    serial_id="1234",
    username="user@example.test",
    password="hunter2",
    referer="https://app.example.test/",
)

JWT_U1 = make_jwt({"userId": "u1", "exp": 9999999999})
JWT_U2 = make_jwt({"userId": "u2", "exp": 9999999999})

GRANT_U1 = {"access_token": JWT_U1, "refresh_token": "rt1", "expires_in": 14400}
GRANT_U2 = {"access_token": JWT_U2, "refresh_token": "rt2", "expires_in": 14400}


def grant(payload: dict) -> FakeResponse:
    return FakeResponse(payload=payload)


@pytest.fixture
def sleeps(monkeypatch):
    controller = SleepController()
    monkeypatch.setattr(timing, "sleep", controller)
    return controller


@pytest.fixture
def session():
    return FakeSession()


def make_manager(session) -> TokenManager:
    return TokenManager(CONFIG, session=session)


def grant_calls(session) -> list[dict]:
    return [c for c in session.calls if c["method"] == "POST"]


# --- config / bundle validation ---


@pytest.mark.parametrize(
    "field",
    ["api_host", "basic_auth_creds", "serial_id", "username", "password"],
)
def test_auth_config_empty_field_rejected(field):
    kwargs = {
        "api_host": API_HOST,
        "basic_auth_creds": "abc",
        "serial_id": "1",
        "username": "e@x.test",
        "password": "pw",
    }
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        AuthConfig(**kwargs)


def test_auth_config_blank_referer_rejected():
    with pytest.raises(ValueError, match="referer"):
        AuthConfig(
            api_host=API_HOST,
            basic_auth_creds="abc",
            serial_id="1",
            username="e@x.test",
            password="pw",
            referer=" ",
        )


@pytest.mark.parametrize(
    ("access", "refresh", "expires"),
    [
        ("ok", None, 10),
        (None, "ok", 10),
        ("ok", "ok", "soon"),
        # Unusable lifetimes. Python's json parses the non-standard
        # Infinity / NaN tokens, so these reach the payload validator
        # from a real (or hostile) response body.
        ("ok", "ok", float("inf")),
        ("ok", "ok", float("nan")),
        ("ok", "ok", 0),
        ("ok", "ok", -30),
        ("ok", "ok", 10**15),
        ("ok", "ok", 10**400),  # far beyond the float range
    ],
)
def test_token_bundle_from_grant_rejects_bad_payloads(access, refresh, expires):
    with pytest.raises(SmartboxError):
        TokenBundle.from_grant(access, refresh, expires)


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
def test_token_bundle_rejects_empty_tokens(field):
    """The persistence record codec must not accept an unusable pair."""
    values = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": dt.datetime.now(dt.UTC),
    }
    values[field] = ""
    with pytest.raises(ValueError, match=field):
        TokenBundle(**values)


def test_token_bundle_from_grant_rejects_json_infinity():
    payload = json.loads('{"expires_in": Infinity}')
    with pytest.raises(SmartboxError, match="unusable expires_in"):
        TokenBundle.from_grant("t", "r", payload["expires_in"])


def test_token_bundle_from_grant_derives_expiry_utc():
    before = dt.datetime.now(dt.UTC)
    bundle = TokenBundle.from_grant("t", "r", 100)
    after = dt.datetime.now(dt.UTC)
    low = before + dt.timedelta(seconds=100)
    high = after + dt.timedelta(seconds=100)
    assert low <= bundle.expires_at <= high


def test_dataclass_reprs_contain_no_secret_material():
    """Sensitive dataclasses never leak credentials through their repr."""
    config = AuthConfig(
        api_host="https://api.example.test",
        basic_auth_creds="BASIC_SECRET",
        serial_id="1234",
        username="user@example.test",
        password="PASSWORD_SECRET",
        referer="https://app.example.test",
    )
    bundle = TokenBundle(
        access_token="ACCESS_SECRET",
        refresh_token="REFRESH_SECRET",
        expires_at=dt.datetime.now(dt.UTC),
    )
    # Every credential-shaped dataclass belongs here: WsCredentials is the
    # per-connect credential the transport hands around.
    creds = WsCredentials(token="WS_ACCESS_SECRET", user_id="u1")
    for text in (repr(config), repr(bundle), repr(creds)):
        for secret in (
            "BASIC_SECRET",
            "PASSWORD_SECRET",
            "ACCESS_SECRET",
            "REFRESH_SECRET",
            "WS_ACCESS_SECRET",
        ):
            assert secret not in text


# --- grant lifecycle ---


async def test_first_get_token_runs_password_grant(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    bundle = await make_manager(session).get_token()
    assert bundle.access_token == JWT_U1
    assert bundle.refresh_token == "rt1"
    assert bundle.expires_at > dt.datetime.now(dt.UTC)
    calls = grant_calls(session)
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == f"{API_HOST}/client/token"
    assert call["headers"]["Authorization"] == "Basic cmVzZWxsZXJfc2VjcmV0"  # verbatim
    assert call["headers"]["x-serialid"] == "1234"
    assert call["headers"]["x-referer"] == "https://app.example.test/"
    assert call["data"] == {
        "grant_type": "password",
        "username": "user@example.test",
        "password": "hunter2",
    }


async def test_fresh_token_reused_without_grant(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    first = await manager.get_token()
    second = await manager.get_token()
    assert second is first
    assert len(grant_calls(session)) == 1


async def test_refresh_grant_when_below_min_lifetime(sleeps, session):
    session.enqueue(
        grant({"access_token": JWT_U1, "refresh_token": "rt1", "expires_in": 59})
    )
    session.enqueue(grant(GRANT_U2))
    manager = make_manager(session)
    await manager.get_token()
    refreshed = await manager.get_token()  # default min_lifetime 60 > 59
    assert refreshed.access_token == JWT_U2
    calls = grant_calls(session)
    assert calls[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt1"}


async def test_rotation_stored_next_refresh_uses_new_token(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(
        grant({"access_token": JWT_U2, "refresh_token": "rt2", "expires_in": 30})
    )
    session.enqueue(
        grant({"access_token": JWT_U1, "refresh_token": "rt3", "expires_in": 30})
    )
    manager = make_manager(session)
    await manager.get_token()
    await manager.get_token(min_lifetime_s=10**9)  # force refresh -> rotates to rt2
    await manager.get_token(min_lifetime_s=10**9)  # must use rt2, not rt1
    data = grant_calls(session)[2]["data"]
    assert data == {"grant_type": "refresh_token", "refresh_token": "rt2"}


async def test_concurrent_callers_never_double_grant(sleeps):
    gate = asyncio.Event()
    session = FakeSession(gate=gate)
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    task = asyncio.gather(manager.get_token(), manager.get_token())
    await asyncio.sleep(0)  # let the first caller block inside the gated grant
    gate.set()
    first, second = await asyncio.wait_for(task, 2)
    assert first.access_token == second.access_token == JWT_U1
    assert len(grant_calls(session)) == 1


async def test_queued_caller_gets_full_budget_after_lock_waits(monkeypatch):
    """Token contention: the lock wait is NOT inside the caller's timeout.

    A queued caller parked on the lock (because a slow holder holds it)
    gets its own full grant budget after acquiring the lock - it must
    not spuriously fail with TransportUnavailableError while waiting.
    """
    monkeypatch.setattr(auth_mod, "_CALL_TIMEOUT", 0.05)  # tiny budget
    session = FakeSession()
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    hold = asyncio.Event()

    async def holder():
        async with manager._lock:
            await hold.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)  # let the holder acquire the lock
    queued = asyncio.create_task(manager.get_token())
    await asyncio.sleep(0.15)  # outlast the tiny budget while parked
    assert not queued.done()  # still waiting on the lock: no spurious raise
    hold.set()
    result = await asyncio.wait_for(queued, 2)
    assert result.access_token == JWT_U1
    holder_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await holder_task


async def test_get_token_outer_deadline_bounds_the_locked_grant(monkeypatch):
    """A grant that ignores its own deadline is still bounded.

    The lock-then-budget order gives a queued caller its own full budget;
    the outer deadline is what keeps a holder that never finishes (a hung
    grant inside the lock) from parking every later caller for good.
    """
    monkeypatch.setattr(auth_mod, "_CALL_TIMEOUT", 0.05)

    async def hang(self, min_lifetime_s):
        await asyncio.sleep(1.0)
        msg = "the outer deadline should have cut this short"
        raise AssertionError(msg)

    monkeypatch.setattr(TokenManager, "_get_token_locked", hang)
    with pytest.raises(TransportUnavailableError, match="did not complete within"):
        await make_manager(FakeSession()).get_token()


async def test_invalidate_forces_real_refresh_grant_when_fresh(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(grant(GRANT_U2))
    manager = make_manager(session)
    await manager.get_token()
    await manager.invalidate()
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U2
    calls = grant_calls(session)
    assert calls[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt1"}


async def test_invalidate_noop_when_token_already_replaced(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    await manager.get_token()
    await manager.invalidate(rejected_token="some-older-token")
    await manager.get_token()
    assert len(grant_calls(session)) == 1  # no refresh storm


# --- refresh rejection -> one password fallback ---


async def test_refresh_rejected_falls_back_to_one_password_grant(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(grant(GRANT_U2))
    manager = make_manager(session)
    await manager.get_token()
    bundle = await manager.get_token(min_lifetime_s=10**9)
    assert bundle.access_token == JWT_U2
    calls = grant_calls(session)
    assert len(calls) == 3
    assert calls[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt1"}
    assert calls[2]["data"]["grant_type"] == "password"


async def test_refresh_and_password_both_rejected_raises_auth_error(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(FakeResponse(status=403))
    manager = make_manager(session)
    await manager.get_token()
    with pytest.raises(AuthError):
        await manager.get_token(min_lifetime_s=10**9)
    calls = grant_calls(session)
    assert [c["data"]["grant_type"] for c in calls[1:]] == ["refresh_token", "password"]


async def test_reauthenticate_clears_tokens_and_password_grants(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(grant(GRANT_U2))
    manager = make_manager(session)
    await manager.get_token()
    bundle = await manager.reauthenticate()
    assert bundle.access_token == JWT_U2
    assert grant_calls(session)[1]["data"] == {
        "grant_type": "password",
        "username": "user@example.test",
        "password": "hunter2",
    }
    await manager.get_token()  # steady state carries on without another grant
    assert len(grant_calls(session)) == 2


async def test_reauthenticate_rejected_raises_auth_error(sleeps, session):
    session.enqueue(FakeResponse(status=403))
    with pytest.raises(AuthError, match="403"):
        await make_manager(session).reauthenticate()


async def test_reauthenticate_timeout_is_transport_unavailable(monkeypatch):
    """A hung reauthentication is bounded and reported as transient trouble."""

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(auth_mod, "_CALL_TIMEOUT", 0.01)
    monkeypatch.setattr(TokenManager, "_grant_with_retry", hang)
    with pytest.raises(TransportUnavailableError, match="re-authentication"):
        await make_manager(FakeSession()).reauthenticate()


async def test_reauthenticate_lock_first_queued_caller_gets_full_budget(monkeypatch):
    """Reauthenticate mirrors get_token: the lock wait is NOT in the budget.

    A caller parked on the lock must get its own full grant budget after
    acquiring, not a spurious TransportUnavailableError while waiting.
    """
    monkeypatch.setattr(auth_mod, "_CALL_TIMEOUT", 0.05)  # tiny budget
    session = FakeSession()
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    hold = asyncio.Event()

    async def holder():
        async with manager._lock:
            await hold.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)  # let the holder acquire the lock
    queued = asyncio.create_task(manager.reauthenticate())
    await asyncio.sleep(0.15)  # outlast the tiny budget while parked
    assert not queued.done()  # still waiting on the lock: no spurious raise
    hold.set()
    result = await asyncio.wait_for(queued, 2)
    assert result.access_token == JWT_U1
    holder_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await holder_task


# --- transient failures / deadline ---


async def test_transient_grant_failures_retried_with_backoff(sleeps, session):
    session.enqueue(TimeoutError())
    session.enqueue(aiohttp.ClientError("conn reset"))
    session.enqueue(FakeResponse(status=502))
    session.enqueue(grant(GRANT_U1))
    bundle = await make_manager(session).get_token()
    assert bundle.access_token == JWT_U1
    assert_backoff_delays(sleeps.delays, [1.0, 2.0, 4.0])


async def test_grant_deadline_bounded(sleeps):
    session = FakeSession(default=aiohttp.ClientError("down"))
    with pytest.raises(TransportUnavailableError):
        await asyncio.wait_for(make_manager(session).get_token(), 2.0)


async def test_token_endpoint_5xx_retried_then_transport_unavailable(sleeps, session):
    for _ in range(4):
        session.enqueue(FakeResponse(status=500))
    with pytest.raises(TransportUnavailableError):
        await make_manager(session).get_token()
    assert len(grant_calls(session)) == 4
    assert_backoff_delays(sleeps.delays, [1.0, 2.0, 4.0])


async def test_token_endpoint_429_retried_transient_not_auth_error(sleeps, session):
    session.enqueue(FakeResponse(status=429))
    session.enqueue(FakeResponse(status=429))
    session.enqueue(grant(GRANT_U1))
    bundle = await make_manager(session).get_token()
    assert bundle.access_token == JWT_U1
    assert_backoff_delays(sleeps.delays, [1.0, 2.0])


async def test_token_endpoint_429_persists_raises_transport_unavailable(
    sleeps, session
):
    for _ in range(4):
        session.enqueue(FakeResponse(status=429))
    with pytest.raises(TransportUnavailableError):
        await make_manager(session).get_token()
    assert len(grant_calls(session)) == 4
    assert_backoff_delays(sleeps.delays, [1.0, 2.0, 4.0])


async def test_refresh_grant_429_never_triggers_password_fallback(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    session.enqueue(FakeResponse(status=429))
    session.enqueue(grant(GRANT_U2))
    manager = make_manager(session)
    await manager.get_token()
    bundle = await manager.get_token(min_lifetime_s=10**9)
    assert bundle.access_token == JWT_U2
    calls = grant_calls(session)
    assert len(calls) == 3  # password, refresh(429), retried refresh
    assert calls[1]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt1"}
    assert calls[2]["data"]["grant_type"] == "refresh_token"  # no password fallback


async def test_malformed_grant_payload_is_smartbox_error(sleeps, session):
    session.enqueue(FakeResponse(payload={"unexpected": "shape"}))
    with pytest.raises(SmartboxError):
        await make_manager(session).get_token()


async def test_undecodable_grant_body_is_malformed_success(sleeps, session):
    """A 200 whose JSON body will not decode is not retried nor a reauth."""
    session.enqueue(
        FakeResponse(
            json_error=json.JSONDecodeError("Expecting value", "<html>err</html>", 0),
        )
    )
    with pytest.raises(SmartboxError, match="undecodable"):
        await make_manager(session).get_token()
    assert len(grant_calls(session)) == 1
    assert sleeps.delays == []


async def test_non_json_grant_body_is_malformed_success(sleeps, session):
    """A 200 with an unusable body (ContentTypeError) is a malformed success."""
    session.enqueue(FakeResponse(status=200, payload=None))
    with pytest.raises(SmartboxError, match="non-JSON"):
        await make_manager(session).get_token()
    assert len(grant_calls(session)) == 1
    assert sleeps.delays == []


async def test_non_utf8_grant_body_is_malformed_success(sleeps, session):
    """A 200 whose body is not decodable text is a malformed success too.

    aiohttp decodes the bytes before parsing, so invalid UTF-8 raises
    UnicodeDecodeError rather than JSONDecodeError; it must not escape as a
    raw decode error nor read as rejected credentials.
    """
    session.enqueue(
        FakeResponse(
            json_error=UnicodeDecodeError(
                "utf-8", b"\xff\xfe", 0, 1, "invalid start byte"
            ),
        )
    )
    with pytest.raises(SmartboxError, match="undecodable"):
        await make_manager(session).get_token()
    assert len(grant_calls(session)) == 1
    assert sleeps.delays == []


async def test_malformed_token_body_never_falls_back_to_password(sleeps, session):
    """A malformed refresh response must not read as rejected credentials."""
    session.enqueue(grant(GRANT_U1))
    session.enqueue(
        FakeResponse(
            json_error=json.JSONDecodeError("Expecting value", "", 0),
        )
    )
    manager = make_manager(session)
    await manager.get_token()
    with pytest.raises(SmartboxError):
        await manager.get_token(min_lifetime_s=10**9)
    calls = grant_calls(session)
    assert len(calls) == 2
    assert calls[1]["data"]["grant_type"] == "refresh_token"


# --- session ownership ---


async def test_injected_session_never_closed(sleeps, session):
    manager = make_manager(session)
    session.enqueue(grant(GRANT_U1))
    await manager.get_token()
    await manager.close()
    assert session.calls  # still intact -> an injected session is not closed


async def test_owned_session_created_lazily_and_closed(sleeps):
    manager = TokenManager(CONFIG)
    session = manager.session
    assert manager.session is session
    await manager.close()
    assert session.closed


# --- ws TokenSource adapter ---


async def test_managed_token_source_acquire_and_invalidate(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    source = ManagedTokenSource(make_manager(session))
    creds = await source.acquire()
    assert creds == WsCredentials(token=JWT_U1, user_id="u1")
    assert len(grant_calls(session)) == 1
    await source.invalidate(creds)
    session.enqueue(
        grant({"access_token": JWT_U2, "refresh_token": "rt2", "expires_in": 30})
    )
    refreshed = await source.acquire()
    assert refreshed.token == JWT_U2
    assert grant_calls(session)[1]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt1",
    }


# --- secrets hygiene ---


async def test_no_secrets_in_logs(sleeps, session, caplog):
    caplog.set_level(logging.DEBUG, logger="smartbox.protocol.auth")
    session.enqueue(grant(GRANT_U1))
    session.enqueue(FakeResponse(status=401))  # refresh rejected
    session.enqueue(FakeResponse(status=403))  # password rejected
    manager = make_manager(session)
    await manager.get_token()
    with pytest.raises(AuthError):
        await manager.get_token(min_lifetime_s=10**9)
    log_text = caplog.text
    for fragment in (
        JWT_U1,
        "rt1",
        JWT_U2,
        "hunter2",
        "cmVzZWxsZXJfc2VjcmV0",
        "user@example.test",
    ):
        assert fragment not in log_text


# --- token_expiry surface (diagnostics) ---


async def test_token_expiry_none_before_first_grant(sleeps, session):
    manager = make_manager(session)
    assert manager.token_expiry is None


async def test_token_expiry_tracks_cached_bundle(sleeps, session):
    session.enqueue(grant(GRANT_U1))
    manager = make_manager(session)
    bundle = await manager.get_token()
    assert manager.token_expiry == bundle.expires_at


# --- persistence seam (initial_tokens + on_grant) ---


def stored_bundle(
    access: str = JWT_U1, refresh: str = "rt1", lifetime_s: float = 14400
) -> TokenBundle:
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=lifetime_s),
    )


async def test_initial_tokens_skip_the_grant(sleeps, session):
    seen: list[TokenBundle] = []

    async def collect(tokens: TokenBundle) -> None:
        seen.append(tokens)

    manager = TokenManager(
        CONFIG, session=session, initial_tokens=stored_bundle(), on_grant=collect
    )
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U1
    assert grant_calls(session) == []
    assert manager.seed_accepted is True
    assert manager.last_grant is None
    assert seen == []


async def test_expired_initial_tokens_run_a_refresh_grant(sleeps, session):
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(
        CONFIG, session=session, initial_tokens=stored_bundle(lifetime_s=-60)
    )
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U2
    assert manager.seed_accepted is True
    assert manager.last_grant == "refresh"
    calls = grant_calls(session)
    assert len(calls) == 1
    assert calls[0]["data"] == {"grant_type": "refresh_token", "refresh_token": "rt1"}


async def test_on_grant_receives_password_and_rotated_refresh(sleeps, session):
    seen: list[TokenBundle] = []

    async def collect(tokens: TokenBundle) -> None:
        seen.append(tokens)

    session.enqueue(grant(GRANT_U1))
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(CONFIG, session=session, on_grant=collect)
    first = await manager.get_token()
    await manager.invalidate(first.access_token)
    second = await manager.get_token()
    assert [b.refresh_token for b in seen] == ["rt1", "rt2"]
    assert seen[-1] is second


async def test_on_grant_receives_the_password_fallback(sleeps, session):
    """A dead refresh token is replaced by a password grant that is persisted."""
    seen: list[TokenBundle] = []

    async def collect(tokens: TokenBundle) -> None:
        seen.append(tokens)

    session.enqueue(grant(GRANT_U1))
    session.enqueue(FakeResponse(status=401))
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(CONFIG, session=session, on_grant=collect)
    await manager.get_token()
    bundle = await manager.get_token(min_lifetime_s=10**9)
    assert bundle.access_token == JWT_U2
    assert [b.access_token for b in seen] == [JWT_U1, JWT_U2]


async def test_on_grant_receives_reauthentication(sleeps, session):
    seen: list[TokenBundle] = []

    async def collect(tokens: TokenBundle) -> None:
        seen.append(tokens)

    session.enqueue(grant(GRANT_U1))
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(CONFIG, session=session, on_grant=collect)
    await manager.get_token()
    bundle = await manager.reauthenticate()
    assert seen[-1] is bundle


async def test_on_grant_failure_is_logged_and_never_breaks_auth(
    sleeps, session, caplog
):
    async def explode(_: TokenBundle) -> None:
        msg = "disk full"
        raise RuntimeError(msg)

    session.enqueue(grant(GRANT_U1))
    manager = TokenManager(CONFIG, session=session, on_grant=explode)
    with caplog.at_level(logging.WARNING, logger="smartbox.protocol.auth"):
        bundle = await manager.get_token()
    assert bundle.access_token == JWT_U1
    assert "token persistence failed (RuntimeError)" in caplog.text


async def test_slow_persistence_does_not_consume_the_acquisition_budget(
    sleeps, session, monkeypatch
):
    """The callback runs outside the acquisition timeout (a slow disk is not auth)."""

    async def slow(_: TokenBundle) -> None:
        await asyncio.sleep(0.1)

    session.enqueue(grant(GRANT_U1))
    manager = TokenManager(CONFIG, session=session, on_grant=slow)
    monkeypatch.setattr(auth_mod, "_CALL_TIMEOUT", 0.5)
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U1


async def test_persistence_is_bounded_and_logged(sleeps, session, caplog, monkeypatch):
    """A wedged callback is cut off; the granted token is still returned."""

    async def stuck(_: TokenBundle) -> None:
        await asyncio.sleep(5)

    session.enqueue(grant(GRANT_U1))
    manager = TokenManager(CONFIG, session=session, on_grant=stuck)
    monkeypatch.setattr(auth_mod, "_PERSIST_TIMEOUT", 0.01)
    with caplog.at_level(logging.WARNING, logger="smartbox.protocol.auth"):
        bundle = await manager.get_token()
    assert bundle.access_token == JWT_U1
    assert "token persistence failed (TimeoutError)" in caplog.text


# --- TokenBundle record codec ---


def test_token_bundle_rejects_a_naive_expiry():
    with pytest.raises(ValueError, match="timezone-aware"):
        TokenBundle(
            access_token=JWT_U1,
            refresh_token="rt1",
            expires_at=dt.datetime(2030, 1, 1),
        )


def test_token_bundle_record_roundtrip():
    bundle = stored_bundle(access="a", refresh="r")
    assert TokenBundle.from_record(bundle.to_record()) == bundle


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"access_token": "a", "refresh_token": "r"},
        {
            "access_token": "",
            "refresh_token": "r",
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
        {
            "access_token": "a",
            "refresh_token": "",
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
        {"access_token": "a", "refresh_token": "r", "expires_at": "soon"},
        {"access_token": "a", "refresh_token": "r", "expires_at": 5},
        # Naive timestamps are rejected by TokenBundle.__post_init__.
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": "2030-01-01T00:00:00",
        },
    ],
)
def test_token_bundle_from_record_rejects_bad_shapes(record):
    with pytest.raises(ValueError):
        TokenBundle.from_record(record)


# --- seeded-token account validation ---


def account_jwt(email: str | None = None, serial: object = None) -> str:
    claims: dict[str, object] = {"userId": "u1", "exp": 9999999999}
    if email is not None:
        claims["email"] = email
    if serial is not None:
        claims["serialId"] = serial
    return make_jwt(claims)


async def test_seeded_tokens_for_another_email_are_ignored(sleeps, session):
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(
        CONFIG,
        session=session,
        initial_tokens=stored_bundle(access=account_jwt("other@example.test", "1234")),
    )
    assert manager.seed_accepted is False
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U2
    assert manager.last_grant == "password"
    assert grant_calls(session)[0]["data"]["grant_type"] == "password"


async def test_seeded_tokens_for_another_serial_are_ignored(sleeps, session):
    session.enqueue(grant(GRANT_U2))
    manager = TokenManager(
        CONFIG,
        session=session,
        initial_tokens=stored_bundle(access=account_jwt("user@example.test", "999")),
    )
    bundle = await manager.get_token()
    assert bundle.access_token == JWT_U2


async def test_seeded_tokens_for_the_same_account_skip_the_grant(sleeps, session):
    manager = TokenManager(
        CONFIG,
        session=session,
        initial_tokens=stored_bundle(access=account_jwt("User@Example.test", 1234)),
    )
    bundle = await manager.get_token()
    assert bundle.access_token == account_jwt("User@Example.test", 1234)
    assert grant_calls(session) == []


async def test_opaque_seeded_tokens_are_accepted(sleeps, session):
    """A non-JWT token cannot be checked; it is used as stored."""
    manager = TokenManager(
        CONFIG, session=session, initial_tokens=stored_bundle(access="opaque")
    )
    bundle = await manager.get_token()
    assert bundle.access_token == "opaque"
    assert grant_calls(session) == []
