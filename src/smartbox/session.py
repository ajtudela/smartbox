"""Interaction with smartbox API."""

import asyncio
from collections.abc import AsyncIterator, Coroutine
import contextlib
import contextvars
import datetime
import json
import logging
import time
from typing import Any, Self
import urllib.parse

import aiohttp
from aiohttp import ClientSession
from pydantic import BaseModel, ValidationError

from smartbox.error import (
    APIUnavailableError,
    InvalidAuthError,
    SmartboxError,
    SmartboxValidationError,
)
from smartbox.models import (
    AcmNodeStatus,
    DefaultNodeStatus,
    DeviceAwayStatus,
    DeviceConnected,
    Devices,
    Guests,
    Home,
    Homes,
    HtrModNodeStatus,
    HtrNodeStatus,
    Node,
    NodeProg,
    Nodes,
    NodeSetup,
    NodeStatus,
    NodeVersion,
    Samples,
    SmartboxNodeType,
    Token,
)
from smartbox.reseller import AvailableResellers, SmartboxReseller
from smartbox.retry import backoff_delay, jittered

_DEFAULT_RETRY_ATTEMPTS = 5
_DEFAULT_BACKOFF_FACTOR = 0.1
_MAX_RETRY_SLEEP = 10.0
_MIN_TOKEN_LIFETIME = (
    60  # Minimum time left before expiry before we refresh (seconds)
)
# Per-request timeouts for sessions the library creates itself.
_REQUEST_TIMEOUT_TOTAL = 30.0
_REQUEST_TIMEOUT_CONNECT = 10.0
# Deadline for one whole library call — token refresh, every retry and
# the backoff sleeps between them — regardless of whose websession is
# used. Past it the call fails with APIUnavailableError, so consumers
# can mark state unknown instead of looking stale.
_CALL_TIMEOUT = 30.0
HTTP_UNAUTHORIZED = 401
HTTP_NO_CONTENT = 204
HTTP_SERVER_ERROR_BEGIN = 500
HTTP_TOO_MANY_REQUESTS = 429

# Query-parameter / body keys that carry secrets (access tokens on the
# socket URL, passwords in invite-confirmation bodies, wifi passwords in
# the wifi-credential helper); their values must never reach log output.
# ``user_id`` is an account identifier, not a credential, but it is
# equally masked: routine INFO logs get pasted verbatim into issues and
# forums, and an account key has no debugging value there.
_SENSITIVE_KEYS = frozenset({"token", "pass", "password", "user_id"})

_LOGGER = logging.getLogger(__name__)


def _redacted_url(url: str) -> str:
    """Return the URL with sensitive query-parameter values masked."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    redacted_query = "&".join(
        f"{k}=***" if k.lower() in _SENSITIVE_KEYS else f"{k}={v}"
        for k, v in query
    )
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment)
    )


def _redact_body(data: dict[str, Any] | None) -> str | None:
    """Serialise a request body for debug logs with secret values masked."""
    if data is None:
        return None
    return json.dumps(
        {
            k: "***" if k.lower() in _SENSITIVE_KEYS else v
            for k, v in data.items()
        }
    )


# (owning task, absolute loop-time deadline) of the library call in
# progress. The task is recorded because context variables are copied
# into tasks spawned from inside a call; such a task must get its own
# budget rather than silently inherit (and never enforce) this one.
_call_deadline: contextvars.ContextVar[
    tuple[asyncio.Task[Any] | None, float] | None
] = contextvars.ContextVar("smartbox_call_deadline", default=None)


def _active_deadline() -> float | None:
    """Deadline of the call budget enclosing the current task, if any."""
    current = _call_deadline.get()
    if current is None or current[0] is not asyncio.current_task():
        return None
    return current[1]


@contextlib.asynccontextmanager
async def _call_budget() -> AsyncIterator[None]:
    """Bound the enclosed work by ``_CALL_TIMEOUT``.

    Nested use shares the outermost deadline, so a compound call (token
    refresh + request + retries, or GET-merge-POST) gets one budget, not
    one per step. Expiry raises ``APIUnavailableError``.
    """
    if _active_deadline() is not None:
        yield
        return
    deadline = asyncio.get_running_loop().time() + _CALL_TIMEOUT
    token = _call_deadline.set((asyncio.current_task(), deadline))
    scope = asyncio.timeout_at(deadline)
    try:
        async with scope:
            yield
    except TimeoutError as e:
        if not scope.expired():
            raise
        msg = f"Smartbox API call did not complete within {_CALL_TIMEOUT}s"
        raise APIUnavailableError(msg) from e
    finally:
        _call_deadline.reset(token)


def _fits_budget(delay: float) -> bool:
    """Whether sleeping ``delay`` seconds still ends before the deadline."""
    deadline = _active_deadline()
    return (
        deadline is None or asyncio.get_running_loop().time() + delay < deadline
    )


def _validate[M: BaseModel](model: type[M], payload: object, what: str) -> M:
    """Validate a response payload (``raw_response=False`` mode).

    Payload drift raises ``SmartboxValidationError`` — the documented
    contract error — instead of leaking pydantic's exception type.
    """
    try:
        return model.model_validate(payload)
    except ValidationError as e:
        # The raised error carries pydantic's field-level detail; the full
        # payload goes to debug only (the caller decides how loud to be).
        _LOGGER.debug("%s validation error, payload: %s", what, payload)
        msg = f"Unexpected {what} payload: {e}"
        raise SmartboxValidationError(msg, payload) from e


def _raw_list(payload: object, what: str, key: str | None = None) -> list[Any]:
    """Return the wire list (optionally under ``key``) for raw mode.

    No model validation happens in raw mode; only a payload too
    malformed to even extract from raises ``SmartboxError``.
    """
    value = payload
    if key is not None:
        value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list):
        msg = f"Unexpected {what} payload: {payload!r}"
        raise SmartboxError(msg)
    return value


def _has_sensitive_keys(params: dict[str, Any] | None) -> bool:
    """Whether query params carry a secret (see ``_SENSITIVE_KEYS``)."""
    return params is not None and any(
        k.lower() in _SENSITIVE_KEYS for k in params
    )


class AsyncSession:
    """Base class for Session."""

    def __init__(
        self,
        username: str,
        password: str,
        websession: ClientSession | None = None,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        raw_response: bool = True,
        api_name: str = "api",
        basic_auth_credentials: str | None = None,
        x_serial_id: int | None = None,
        x_referer: str | None = None,
    ) -> None:
        """Init the session."""
        # A negative backoff_factor would silently disable backoff
        # (asyncio.sleep clamps negatives) and log "sleeping -1.0s".
        # ``retry_attempts`` is deliberately clamped at use site
        # (``max(1, ...)`` in ``_request_with_retry``), matching the
        # test-pinned contract that 0 still makes one attempt.
        if backoff_factor < 0:
            msg = f"backoff_factor must be >= 0 (got {backoff_factor})"
            raise ValueError(msg)
        self._reseller = AvailableResellers(
            api_url=api_name,
            basic_auth=basic_auth_credentials,
            serial_id=x_serial_id,
            web_url=x_referer,
        ).reseller
        self._api_host: str = f"https://{self.reseller.api_url}.helki.com"
        self._retry_attempts: int = retry_attempts
        self._backoff_factor: float = backoff_factor
        self._username: str = username
        self._password: str = password
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._expires_at: datetime.datetime = datetime.datetime.now(
            datetime.UTC
        )
        self._client_session: ClientSession | None = websession
        # True once the client property has created a session for us; a
        # caller-provided websession is owned (and closed) by the caller.
        self._owns_client_session = False
        # Serializes token refresh: concurrent REST + socket traffic must
        # not race two refreshes with the same refresh token.
        self._refresh_lock = asyncio.Lock()
        self.raw_response: bool = raw_response
        self._headers: dict[str, str] = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        if self.reseller.serial_id:
            self._headers.update({"x-serialid": str(self.reseller.serial_id)})
        if self.reseller.web_url:
            self._headers.update({"x-referer": self.reseller.web_url})

    async def aclose_owned_session(self) -> None:
        """Close the client session, but only if the library created it.

        A caller-provided websession is owned (and closed) by the caller.
        """
        if self._owns_client_session and self._client_session:
            await self._client_session.close()
            # Forget the closed session so the lazy ``client`` property
            # recreates it on the next (possibly new) event loop; keeping
            # the closed object here made a second sync Session call fail
            # with "RuntimeError: Session is closed".
            self._client_session = None

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        _LOGGER.debug("Entering AsyncSmartboxSession context")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit.

        Only closes the client session if this object created it: a
        caller-provided websession is owned (and closed) by the caller.
        """
        _LOGGER.debug("Exiting AsyncSmartboxSession context")
        await self.aclose_owned_session()

    @property
    def reseller(self) -> SmartboxReseller:
        """Get the reseller."""
        return self._reseller

    @property
    def api_name(self) -> str:
        """Get the api sub domain url."""
        return self.reseller.api_url

    @property
    def api_host(self) -> str:
        """Get the base api url."""
        return self._api_host

    @property
    def access_token(self) -> str:
        """Get auth access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str:
        """Get auth refresh token."""
        return self._refresh_token

    @property
    def expiry_time(self) -> datetime.datetime:
        """Get auth expiracy."""
        return self._expires_at

    @property
    def client(self) -> ClientSession:
        """Return the underlying http client.

        A session created here (rather than provided by the caller) gets
        bounded per-request timeouts. Every library call — whoever owns
        the websession — is additionally bounded by ``_CALL_TIMEOUT``.
        """
        if not self._client_session:
            self._client_session = ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=_REQUEST_TIMEOUT_TOTAL,
                    connect=_REQUEST_TIMEOUT_CONNECT,
                ),
            )
            self._owns_client_session = True
        return self._client_session

    async def health_check(self) -> dict[str, Any]:
        """Check if the API is alive."""
        return await self._request(
            "get",
            f"{self._api_host}/health_check",
            auth=False,
        )

    async def api_version(self) -> dict[str, str]:
        """Get the API version."""
        return await self._request(
            "get",
            f"{self._api_host}/version",
            auth=False,
        )

    async def _authentication(self, credentials: dict[str, str]) -> None:
        """Do the authentication process to Smartbox. First one use login/mdp/basic_auth. Then the tokens."""
        token_headers = self._headers.copy()
        token_headers.pop("Authorization", None)
        token_headers.update(
            {
                "authorization": f"Basic {self.reseller.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        token_url = f"{self._api_host}/client/token"
        try:
            async with self.client.post(
                url=token_url,
                headers=token_headers,
                data=credentials,
            ) as response:
                response.raise_for_status()
                response_json = await response.json()
                try:
                    rtoken: Token = Token.model_validate(response_json)
                    self._access_token = rtoken.access_token
                    self._headers["Authorization"] = (
                        f"Bearer {self._access_token}"
                    )
                    self._refresh_token = rtoken.refresh_token
                    if rtoken.expires_in < _MIN_TOKEN_LIFETIME:
                        _LOGGER.warning(
                            "Token expires in %ss which is below minimum lifetime of %ss- will refresh again on next operation",
                            rtoken.expires_in,
                            _MIN_TOKEN_LIFETIME,
                        )
                    self._expires_at = datetime.datetime.now(
                        datetime.UTC
                    ) + datetime.timedelta(
                        seconds=rtoken.expires_in,
                    )
                    _LOGGER.debug(
                        "Authenticated session (%s), access_token=%s…, expires at %s",
                        credentials["grant_type"],
                        self.access_token[:8],
                        self.expiry_time,
                    )
                except ValidationError as e:
                    # A 200 with a wrong-shaped token body is a malformed
                    # success, not a credential rejection (same contract
                    # as the undecodable-JSON / non-JSON cases below):
                    # mapping it to InvalidAuthError would surface a
                    # server-side drift as "credentials rejected" and end
                    # the websocket loop instead of waiting it out.
                    msg = f"Token endpoint returned a malformed token payload: {e}"
                    raise SmartboxError(msg) from e
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            TimeoutError,
        ) as e:
            # A total-timeout on a wedged request is a plain TimeoutError,
            # not a ClientConnectionError subclass; ClientPayloadError is
            # a body cut off mid-transfer (also not a
            # ClientConnectionError subclass). Both are transient — same
            # contract as the REST path (_send_mapped), and
            # SocketSession._refresh_auth waits them out with backoff
            # instead of the raw aiohttp exception ending the websocket
            # loop.
            raise APIUnavailableError(e) from e
        except json.JSONDecodeError as e:
            # Valid content-type, undecodable JSON body: a malformed
            # success, not bad credentials (mirrors _send_mapped).
            msg = f"Token endpoint returned an undecodable JSON body: {e}"
            raise SmartboxError(msg) from e
        except aiohttp.ContentTypeError as e:
            # 200 with a non-JSON body is a malformed success, not bad
            # credentials: ContentTypeError is a ClientResponseError with
            # status 200 and would otherwise surface as InvalidAuthError
            # and trigger the consumer's reauth flow.
            msg = f"Token endpoint returned a non-JSON body: {e}"
            raise SmartboxError(msg) from e
        except aiohttp.ClientResponseError as e:
            # 40x means the credentials/token were rejected; 5xx is
            # transient server trouble and must not surface as
            # InvalidAuthError (consumers react to that with a reauth flow).
            if e.status == HTTP_TOO_MANY_REQUESTS:
                # Rate limiting is transient, not a credential rejection:
                # surfacing it as InvalidAuthError would trigger the
                # password-login fallback while being rate-limited
                # (amplifying the limit) and end the websocket loop
                # instead of waiting the window out.
                raise APIUnavailableError(e) from e
            if e.status < HTTP_SERVER_ERROR_BEGIN:
                raise InvalidAuthError(e) from e
            raise APIUnavailableError(e) from e

    async def check_refresh_auth(self) -> None:
        """Do we have to refresh auth.

        Serialized by ``_refresh_lock``: the REST poller and the socket
        loop run concurrently and must not race two refreshes with the
        same refresh token. A rejected refresh token falls back to one
        password login before ``InvalidAuthError`` reaches the caller,
        so a dead refresh token alone does not force a reauth flow.
        """
        async with _call_budget(), self._refresh_lock:
            if self._access_token == "":
                await self._password_login()
            elif (
                self._expires_at - datetime.datetime.now(datetime.UTC)
            ) < datetime.timedelta(seconds=_MIN_TOKEN_LIFETIME):
                try:
                    await self._authentication(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": self._refresh_token,
                        },
                    )
                except InvalidAuthError as e:
                    _LOGGER.warning(
                        "Refresh token rejected (%s); logging in again with "
                        "credentials",
                        e,
                    )
                    # Forget the dead tokens: if the password login fails
                    # too, the next call goes straight to it.
                    self._access_token = ""
                    self._refresh_token = ""
                    await self._password_login()

    async def _password_login(self) -> None:
        """Authenticate with the stored username/password."""
        await self._authentication(
            {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            },
        )

    def _invalidate_access_token(self, rejected_token: str) -> None:
        """Force a token refresh after the server rejected ``rejected_token``.

        No-op if another request already replaced that token (concurrent
        401s must not trigger a refresh storm).
        """
        if self._access_token == rejected_token:
            self._expires_at = datetime.datetime.now(datetime.UTC)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """Issue an HTTP request with uniform error handling.

        Connection failures, request timeouts and truncated bodies become
        ``APIUnavailableError``; HTTP 401 becomes ``InvalidAuthError``
        (so consumers can trigger a reauth flow when a token dies
        mid-session); 5xx is treated as transient unavailability (same
        contract as the token endpoint, and retryable via
        ``_request_with_retry``); any other HTTP error or an undecodable
        JSON body becomes ``SmartboxError``. When ``auth`` is set, the
        access token is refreshed first if needed, and a 401 triggers one
        re-authentication and resend (the server acted on nothing, so the
        resend is safe for writes too). Bounded by ``_CALL_TIMEOUT``.
        """
        async with _call_budget():
            if not auth:
                return await self._send(method, url, params, data, auth=False)
            await self.check_refresh_auth()
            token = self._access_token
            try:
                return await self._send(method, url, params, data, auth=True)
            except InvalidAuthError:
                _LOGGER.info(
                    "Access token rejected by %s; re-authenticating and "
                    "retrying once",
                    _redacted_url(url),
                )
                self._invalidate_access_token(token)
                await self.check_refresh_auth()
                return await self._send(method, url, params, data, auth=True)

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        *,
        auth: bool,
    ) -> dict[str, Any]:
        """Send one HTTP request and map every failure to a library error.

        Error messages never embed query params: aiohttp's own exception
        text includes the full URL, which can carry the wifi password.
        For requests with sensitive params the aiohttp exception is kept
        out of the chain entirely (neither ``__cause__`` nor
        ``__context__``).
        """
        try:
            return await self._send_mapped(method, url, params, data, auth=auth)
        except (SmartboxError, InvalidAuthError, APIUnavailableError) as e:
            if not _has_sensitive_keys(params):
                raise
            error = e
        # Re-raised outside the handler, detached from the aiohttp error.
        error.__cause__ = None
        error.__context__ = None
        raise error

    async def _send_mapped(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        data: dict[str, Any] | None,
        *,
        auth: bool,
    ) -> dict[str, Any]:
        """Send one HTTP request; see ``_send`` for the error contract."""
        headers = (
            self._headers
            if auth
            else {
                k: v for k, v in self._headers.items() if k != "Authorization"
            }
        )
        body = json.dumps(data) if data is not None else None
        target = f"{method.upper()} {_redacted_url(url)}"
        try:
            _LOGGER.debug("%s", target)
            async with self.client.request(
                method, url, headers=headers, params=params, data=body
            ) as response:
                response.raise_for_status()
                if (
                    response.status == HTTP_NO_CONTENT
                    or response.content_length == 0
                ):
                    # Empty-body success (204, or empty 200 on DELETEs)
                    return {}
                try:
                    result = await response.json()
                except aiohttp.ContentTypeError:
                    # 200 without a JSON body; treat like the empty case
                    _LOGGER.debug("Non-JSON response body.")
                    return {}
                _LOGGER.debug("Response %s.", result)
                return result
        except aiohttp.ClientResponseError as e:
            # Built from status/reason only: str(e) embeds the full URL.
            msg = f"{target} failed: HTTP {e.status} {e.message}"
            if e.status == HTTP_UNAUTHORIZED:
                _LOGGER.warning("%s rejected: access token invalid", target)
                raise InvalidAuthError(msg) from e
            if e.status >= HTTP_SERVER_ERROR_BEGIN:
                # 5xx is transient server trouble: same contract as the
                # token endpoint, and retryable on GETs via the retry
                # wrapper above this call.
                raise APIUnavailableError(msg) from e
            _LOGGER.error("Smartbox Error: %s", msg)  # noqa: TRY400
            raise SmartboxError(msg) from e
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            TimeoutError,
        ) as e:
            # ClientPayloadError: body cut off mid-transfer (not a
            # ClientConnectionError subclass) — transient like a drop.
            # These messages carry host:port at most, never the query.
            msg = f"{target} failed: {type(e).__name__}: {e}"
            raise APIUnavailableError(msg) from e
        except json.JSONDecodeError as e:
            msg = f"{target} returned an undecodable JSON body: {e}"
            raise SmartboxError(msg) from e

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET with bounded retries on transient failures.

        Always makes at least one attempt. Retries and backoff share the
        call's ``_CALL_TIMEOUT`` budget: a backoff that would overrun it
        is skipped and the last real error raised instead.
        """
        attempts = max(1, self._retry_attempts)
        async with _call_budget():
            for attempt in range(attempts):
                try:
                    return await self._request(method, url, params=params)
                except APIUnavailableError as e:
                    remaining = attempts - attempt - 1
                    sleep_time = jittered(
                        backoff_delay(
                            attempt, self._backoff_factor, _MAX_RETRY_SLEEP
                        )
                    )
                    if remaining == 0 or not _fits_budget(sleep_time):
                        raise
                    _LOGGER.warning(
                        "%s; %s retries remaining, sleeping %ss",
                        e,
                        remaining,
                        sleep_time,
                    )
                    await asyncio.sleep(sleep_time)
        # The final attempt either returns or re-raises above.
        raise AssertionError  # pragma: no cover

    async def _api_request(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a GET request to the ``/api/v2`` API."""
        return await self._request_with_retry(
            "get",
            f"{self._api_host}/api/v2/{path}",
            params=params,
        )

    async def _api_post(
        self,
        data: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        """Make a POST request to the ``/api/v2`` API."""
        url = f"{self._api_host}/api/v2/{path}"
        _LOGGER.debug("Posting %s to %s.", _redact_body(data), url)
        return await self._request("post", url, data=data)

    async def _api_delete(
        self,
        path: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a DELETE request to the ``/api/v2`` API."""
        url = f"{self._api_host}/api/v2/{path}"
        _LOGGER.debug("Deleting %s.", url)
        return await self._request("delete", url, data=data)

    async def _api_get(
        self,
        api_path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a GET request at a host-relative path.

        Unlike ``_api_request`` (which prefixes ``/api/v2/``), this takes
        the full host-relative path (e.g. ``/api/notifications/v1/...``)
        and supports query params. Params are deliberately not logged:
        some carry secrets (wifi passwords).
        """
        return await self._request_with_retry(
            "get",
            f"{self._api_host}{api_path}",
            params=params,
        )

    async def _api_post_path(
        self,
        api_path: str,
        data: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """Make a POST request at a host-relative path.

        For endpoints outside ``/api/v2/`` (e.g. notifications) and for
        anonymous (unauthenticated) flows such as invite confirmation.
        """
        url = f"{self._api_host}{api_path}"
        _LOGGER.debug("Posting %s to %s.", _redact_body(data), url)
        return await self._request("post", url, data=data, auth=auth)


class AsyncSmartboxSession(AsyncSession):
    """Asynchronous Smartbox Session. This should be the default one."""

    # Response contract for every getter below:
    # - raw_response=True: the wire payload, unvalidated (only the
    #   documented list extraction); a payload too malformed to extract
    #   from raises SmartboxError.
    # - raw_response=False: a typed model; drift raises
    #   SmartboxValidationError.

    async def get_devices(self) -> list[dict[str, Any]] | Devices:
        """Get all devices.

        Raw mode returns the wire ``devs`` + ``invited_to`` device dicts
        as one flat list.
        """
        response = await self._api_request("devs")
        _LOGGER.debug("Get devices %s", response)
        if self.raw_response is False:
            return _validate(Devices, response, "devices")
        devs = _raw_list(response, "devices", "devs")
        # ``invited_to`` is optional on the wire (the model defaults it).
        invited_to = response.get("invited_to") or []
        return devs + _raw_list(invited_to, "invited devices")

    async def get_homes(self) -> list[dict[str, Any]] | list[Home]:
        """Get homes.

        Same wire data as ``get_grouped_devices``; this returns the
        flattened ``list[Home]`` view.
        """
        response = await self._api_request("grouped_devs")
        if self.raw_response is False:
            return _validate(Homes, response, "homes").root
        return _raw_list(response, "homes")

    async def get_home_guests(
        self, home_id: str
    ) -> list[dict[str, Any]] | Guests:
        """Get all guests for a home (raw mode: the ``guest_users`` list)."""
        response = await self._api_request(f"groups/{home_id}/guest_users")
        if self.raw_response is False:
            return _validate(Guests, response, "guests")
        return _raw_list(response, "guests", "guest_users")

    async def get_grouped_devices(self) -> list[dict[str, Any]] | Homes:
        """Get grouped devices."""
        response = await self._api_request("grouped_devs")
        if self.raw_response is False:
            return _validate(Homes, response, "grouped devices")
        return _raw_list(response, "grouped devices")

    async def get_nodes(
        self,
        device_id: str,
    ) -> list[dict[str, Any]] | list[Node]:
        """Get nodes from devices."""
        response = await self._api_request(f"devs/{device_id}/mgr/nodes")
        _LOGGER.debug("Get nodes %s", response)
        if self.raw_response is True:
            try:
                return response["nodes"]
            except (KeyError, TypeError) as e:
                msg = f"Unexpected nodes payload for device {device_id}: {response!r}"
                raise SmartboxError(msg) from e
        return _validate(Nodes, response, "nodes").nodes

    async def get_device_connected(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceConnected:
        """Get device connected status."""
        response = await self._api_request(f"devs/{device_id}/connected")
        if self.raw_response is False:
            return _validate(DeviceConnected, response, "device connected")
        return response

    async def get_device_away_status(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceAwayStatus:
        """Get device away status."""
        response = await self._api_request(f"devs/{device_id}/mgr/away_status")
        if self.raw_response is False:
            return _validate(DeviceAwayStatus, response, "away status")
        return response

    async def set_device_away_status(
        self,
        device_id: str,
        status_args: dict[str, Any],
    ) -> None:
        """Set device away status.

        Matches the vendor app write shape ``{"away", "enabled"}``,
        defaulting ``enabled: true`` when omitted. (Verified live
        2026-09-27: the server also honours ``{"away"}`` alone.)
        """
        data = {k: v for k, v in status_args.items() if v is not None}
        if "enabled" not in data:
            data["enabled"] = True
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/mgr/away_status",
        )

    async def get_device_discovery(self, device_id: str) -> dict[str, Any]:
        """Get device discovery status.

        Response shape per spec: ``{"discovery": "on"|"off"}``. Raw
        payload only (no typed model until a live sample is pinned).
        """
        return await self._api_request(f"devs/{device_id}/mgr/discovery")

    async def set_device_discovery(
        self,
        device_id: str,
        discovery_args: dict[str, Any],
    ) -> None:
        """Set device discovery status (pass-through body).

        Body per spec §2: ``{"discovery": "on"|"off", "type"?, "addr"?,
        "limit"?}``. Unverified on live hardware.
        """
        data = {k: v for k, v in discovery_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/mgr/discovery",
        )

    async def get_device_rtc_time(self, device_id: str) -> dict[str, Any]:
        """Get device date and time info. Raw payload only."""
        return await self._api_request(f"devs/{device_id}/mgr/rtc/time")

    async def set_device_name(self, device_id: str, name: str) -> None:
        """Rename a device (body ``{"name": ...}``). Unverified live."""
        await self._api_post(
            data={"name": name},
            path=f"devs/{device_id}/name",
        )

    async def delete_device(self, device_id: str) -> None:
        """Remove a device from the account. Destructive; unverified."""
        await self._api_delete(f"devs/{device_id}")

    async def move_device_to_group(
        self,
        device_id: str,
        group_args: dict[str, Any],
    ) -> None:
        """Move a device to a home/group (body pass-through). Unverified."""
        data = {k: v for k, v in group_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/group",
        )

    async def get_device_power_limit(
        self, device_id: str, node: dict[str, Any] | None = None
    ) -> int:
        """Get device power limit."""
        if node is not None:
            _node = Node.model_validate(node)
            if _node.type == SmartboxNodeType.PMO:
                # GET reads /power with key `power`; the write helper below
                # posts /power_limit — asymmetry unverified on pmo hardware.
                url = f"devs/{device_id}/{_node.type}/{_node.addr}/power"
                power_param = "power"
            else:
                url = f"devs/{device_id}/htr_system/power_limit"
                power_param = "power_limit"
        else:
            url = f"devs/{device_id}/htr_system/power_limit"
            power_param = "power_limit"

        resp = await self._api_request(url)
        try:
            return int(resp[power_param])
        except (KeyError, TypeError, ValueError) as e:
            msg = f"Unexpected power-limit response from {url}: {resp!r}"
            raise SmartboxError(msg) from e

    async def set_device_power_limit(
        self,
        device_id: str,
        power_limit: int,
        node: dict[str, Any] | None = None,
    ) -> None:
        """Set device power limit."""
        _node_type = "htr_system"
        if node is not None:
            _node = Node.model_validate(node)
            if _node.type == SmartboxNodeType.PMO:
                _node_type = f"{_node.type}/{_node.addr}"
        data = {"power_limit": str(power_limit)}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node_type}/power_limit",
        )

    async def get_node_samples(
        self,
        device_id: str,
        node: dict[str, Any],
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any] | Samples:
        """Get samples (history) from node.

        Defaults to a one-hour window centred on *call* time (default
        arguments are evaluated at import, so the timestamps must be
        computed inside the call).
        """
        now = int(time.time())
        if start_time is None:
            start_time = now - 3600
        if end_time is None:
            end_time = now + 3600
        _LOGGER.debug(
            "Get_Device_Samples_Node: from %s to %s",
            datetime.datetime.fromtimestamp(start_time, tz=datetime.UTC),
            datetime.datetime.fromtimestamp(end_time, tz=datetime.UTC),
        )
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/samples",
            params={"start": start_time, "end": end_time},
        )
        _LOGGER.debug("Get_Device_Samples_Node: %s", response)
        if self.raw_response is True:
            return response
        return _validate(Samples, response, "samples")

    async def get_node_status(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> (
        dict[str, Any]
        | AcmNodeStatus
        | HtrNodeStatus
        | HtrModNodeStatus
        | DefaultNodeStatus
    ):
        """Get a node status."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/status",
        )
        _LOGGER.debug("(%s) Status config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        return _validate(NodeStatus, response, "node status").root

    async def set_node_status(
        self,
        device_id: str,
        node: dict[str, Any],
        status_args: dict[str, Any],
    ) -> None:
        """Set a node status."""
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in status_args.items() if v is not None}
        if "stemp" in data and "units" not in data:
            msg = "Must supply unit with temperature fields"
            raise ValueError(msg)
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/status",
        )

    async def get_node_setup(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeSetup:
        """Get a node setup."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
        )
        _LOGGER.debug("(%s) Setup config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        return _validate(NodeSetup, response, "node setup")

    async def set_node_setup(
        self,
        device_id: str,
        node: dict[str, Any],
        setup_args: dict[str, Any],
    ) -> None:
        """Set a node setup."""
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in setup_args.items() if v is not None}
        # The setup endpoint requires the whole configuration to be
        # re-posted (api-notes.md), so the read-modify-write below must
        # keep the payload intact: reading through the Pydantic model
        # would drop every key the models do not declare and wipe it on
        # the device. The read is therefore always raw here, independent
        # of ``raw_response`` (one call budget for the GET-merge-POST).
        async with _call_budget():
            setup_data = await self._api_request(
                f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
            )
            setup_data.update(data)
            await self._api_post(
                data=setup_data,
                path=f"devs/{device_id}/{_node.type}/{_node.addr}/setup",
            )

    async def get_node_version(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeVersion:
        """Get a node version."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/version",
        )
        _LOGGER.debug("(%s) Version config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        return _validate(NodeVersion, response, "node version")

    async def set_node_mode(
        self,
        device_id: str,
        node: dict[str, Any],
        mode_args: dict[str, Any],
    ) -> None:
        """Set node mode via the dedicated ``/mode`` endpoint.

        Alternative to ``POST /status`` with ``{"mode": ...}`` (spec §4);
        body is pass-through, e.g. ``{"mode": "manual", "stemp": "20.0",
        "units": "C"}`` on capability-less units.
        """
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in mode_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/mode",
        )

    async def set_node_lock(
        self,
        device_id: str,
        node: dict[str, Any],
        lock_args: dict[str, Any],
    ) -> None:
        """Set node lock via the dedicated ``/lock`` endpoint.

        Alternative to ``POST /status`` with ``{"locked": ...}`` (spec §4);
        body is pass-through, e.g. ``{"locked": true}``.
        """
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in lock_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/lock",
        )

    async def set_node_boost(
        self,
        device_id: str,
        node: dict[str, Any],
        boost_args: dict[str, Any],
    ) -> None:
        """Set node boost config via the dedicated ``/boost`` endpoint.

        Body per spec §3: ``{active, temperature, units, time}``.
        Boost-capable products only — factory boost-disabled units accept
        but ignore the write (observed on /status; unverified on this
        endpoint for lack of capable hardware).
        """
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in boost_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/boost",
        )

    async def set_node_prog_temps(
        self,
        device_id: str,
        node: dict[str, Any],
        temps_args: dict[str, Any],
    ) -> None:
        """Set profile temperatures via the dedicated ``/prog_temps`` endpoint.

        Alternative to the vendor app's proven 4-key ``POST /status``
        body ``{ice_temp, eco_temp, comf_temp, units}`` (spec §4.4); body
        is pass-through.
        """
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in temps_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/prog_temps",
        )

    async def get_node_prog(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeProg:
        """Get a node programme (weekly schedule)."""
        _node: Node = Node.model_validate(node)
        response = await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/prog",
        )
        _LOGGER.debug("(%s) Prog config data %s", _node.type, response)
        if self.raw_response is True:
            return response
        return _validate(NodeProg, response, "node prog")

    async def set_node_prog(
        self,
        device_id: str,
        node: dict[str, Any],
        prog_args: dict[str, Any],
    ) -> None:
        """Set a node programme (weekly schedule).

        GET and POST both use the day-keyed object shape (``{"0": [...]}``);
        the array shape is rejected with 400 (observed live). The vendor
        app always sends the complete schedule, so the current prog is
        fetched and day-level-merged before POSTing. Verification reads
        must allow a settle delay — the GET reflects device state with a
        lag of several seconds after a write.
        """
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in prog_args.items() if v is not None}
        incoming = data.get("prog")
        if not isinstance(incoming, dict):
            # Guard against the silent no-op (no ``prog`` key) and the
            # AttributeError traceback (array shape) — the wire contract is
            # a day-keyed object nested under ``prog``. ``ValueError``, like
            # ``set_node_status``'s input validation (aligned in 2.6.0).
            msg = (
                "prog_args must contain a 'prog' object mapping day keys "
                f'("0".."6") to slot lists, got: {prog_args!r}'
            )
            # TRY004: isinstance-based guard, but the documented contract is
            # ValueError, aligned with set_node_status's input validation.
            raise ValueError(msg)  # noqa: TRY004
        # One call budget for the whole GET-merge-POST.
        async with _call_budget():
            node_prog = await self.get_node_prog(device_id, node)
            if isinstance(node_prog, dict):
                current_prog: dict[str, Any] = node_prog.get("prog", {})
            else:
                current_prog = node_prog.prog
            merged_prog: dict[str, Any] = {**current_prog, **incoming}
            await self._api_post(
                data={"prog": merged_prog},
                path=f"devs/{device_id}/{_node.type}/{_node.addr}/prog",
            )

    async def set_node_name(
        self, device_id: str, node: dict[str, Any], name: str
    ) -> None:
        """Rename a node (body ``{"name": ...}``). Unverified live."""
        _node: Node = Node.model_validate(node)
        await self._api_post(
            data={"name": name},
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/name",
        )

    async def set_node_select(
        self,
        device_id: str,
        node: dict[str, Any],
        select_args: dict[str, Any],
    ) -> None:
        """Set node selection (body pass-through). Unverified live."""
        _node: Node = Node.model_validate(node)
        data = {k: v for k, v in select_args.items() if v is not None}
        await self._api_post(
            data=data,
            path=f"devs/{device_id}/{_node.type}/{_node.addr}/select",
        )

    async def get_node_power(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any]:
        """Get node power info. Raw payload only."""
        _node: Node = Node.model_validate(node)
        return await self._api_request(
            f"devs/{device_id}/{_node.type}/{_node.addr}/power",
        )

    async def delete_node(
        self,
        device_id: str,
        node: dict[str, Any],
        purge: bool = False,
    ) -> None:
        """Delete a node (body ``{"purge": bool}``). Destructive; unverified."""
        _node: Node = Node.model_validate(node)
        await self._api_delete(
            f"devs/{device_id}/{_node.type}/{_node.addr}",
            data={"purge": purge},
        )

    async def get_group_geo_data(self, group_id: str) -> dict[str, Any]:
        """Get home/group geolocation data. Raw payload only."""
        return await self._api_request(f"groups/{group_id}/geo_data")

    async def set_group_name(self, group_id: str, name: str) -> None:
        """Rename a home/group (body ``{"name": ...}``). Unverified."""
        await self._api_post(
            data={"name": name},
            path=f"groups/{group_id}/name",
        )

    async def get_group_extra_data(self, group_id: str) -> dict[str, Any]:
        """Get home/group extra data. Raw payload only."""
        return await self._api_request(f"groups/{group_id}/extra_data")

    async def invite_user(
        self,
        user_id: str,
        home_id: str,
        email: str,
        confirmation_url: str,
    ) -> dict[str, Any]:
        """Invite a user to a home.

        Body per the vendor app: ``{email, groupid, confirmation_url}``;
        the caller composes ``confirmation_url`` (the app builds
        ``<frontend>/invite-confirm/nserie<serial_id>``). Unverified live.
        """
        return await self._api_post(
            data={
                "email": email,
                "groupid": home_id,
                "confirmation_url": confirmation_url,
            },
            path=f"users/{user_id}/invite",
        )

    async def revoke_invite(
        self,
        user_id: str,
        home_id: str,
        email: str,
    ) -> dict[str, Any]:
        """Revoke a pending home invite. Unverified live."""
        return await self._api_delete(
            f"users/{user_id}/invite",
            data={"groupid": home_id, "email": email},
        )

    async def confirm_invite(
        self,
        user_id: str,
        password: str,
        code: str,
    ) -> dict[str, Any]:
        """Confirm an invite (unauthenticated; body ``{pass, code}``)."""
        return await self._api_post_path(
            f"/api/v2/users/{user_id}/invite_confirmation",
            data={"pass": password, "code": code},
            auth=False,
        )

    async def get_quiet_home_notifications(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Get quiet-home presence notification config. Raw payload only."""
        return await self._api_get(
            f"/api/notifications/v1/{group_id}/presence/config",
        )

    async def set_quiet_home_notifications(
        self,
        group_id: str,
        notification_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Set quiet-home presence notification config (pass-through)."""
        data = {k: v for k, v in notification_data.items() if v is not None}
        return await self._api_post_path(
            f"/api/notifications/v1/{group_id}/presence/config",
            data=data,
        )

    async def test_quiet_home_notifications(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Trigger a quiet-home notification test. Unverified live."""
        return await self._api_post_path(
            f"/api/notifications/v1/{group_id}/test",
        )

    async def get_encrypted_wifi_credentials(
        self,
        ssid: str,
        wifi_password: str,
    ) -> dict[str, Any]:
        """Get the encrypted wifi passphrase (provisioning helper).

        The server performs the AES-256-CBC PBE encryption and returns
        ``{"encrypted_pass": ...}``. The wifi password travels as a query
        param and is deliberately not logged.
        """
        return await self._api_get(
            "/api/v2/encrypted_wifi_credentials",
            params={"ssid": ssid, "pass": wifi_password},
        )

    async def get_coordinates(
        self,
        country: str,
        state: str,
        city: str,
        zip_code: str,
    ) -> dict[str, Any]:
        """Server-side geocoding helper. Raw payload only."""
        return await self._api_get(
            "/api/location/v1/coordinates",
            params={
                "country": country,
                "state": state,
                "city": city,
                "zip": zip_code,
            },
        )


class Session:
    """For retro compatibility, this class is a sync which called the async."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Sync init a session."""
        self._async = AsyncSmartboxSession(*args, **kwargs)

    def _run_and_close(
        self,
        coro: Coroutine[Any, Any, Any],
    ) -> Any:  # noqa: ANN401
        """Run one coroutine and close the session the library created.

        Each sync call gets its own event loop (``asyncio.run``), so a
        lazily-created ``ClientSession`` must be closed before the loop
        tears down, or it dies unclosed.
        """

        async def _call() -> Any:  # noqa: ANN401
            try:
                return await coro
            finally:
                await self._async.aclose_owned_session()

        call_coro = _call()
        try:
            return asyncio.run(call_coro)
        except RuntimeError:
            # asyncio.run refused to start (the caller is inside a running
            # event loop): the pre-built coroutines were never entered.
            # Close them so they don't surface as "never awaited"
            # RuntimeWarnings; if they already ran, close() is a no-op.
            coro.close()
            call_coro.close()
            raise

    def health_check(self) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSession.health_check``."""
        return self._run_and_close(self._async.health_check())

    def api_version(self) -> dict[str, str]:
        """Sync wrapper for ``AsyncSession.api_version``."""
        return self._run_and_close(self._async.api_version())

    def get_devices(
        self,
    ) -> list[dict[str, Any]] | Devices:
        """Sync wrapper for ``AsyncSmartboxSession.get_devices``."""
        return self._run_and_close(self._async.get_devices())

    def get_homes(
        self,
    ) -> list[dict[str, Any]] | list[Home]:
        """Sync wrapper for ``AsyncSmartboxSession.get_homes``."""
        return self._run_and_close(self._async.get_homes())

    def get_grouped_devices(
        self,
    ) -> list[dict[str, Any]] | Homes:
        """Sync wrapper for ``AsyncSmartboxSession.get_grouped_devices``."""
        return self._run_and_close(self._async.get_grouped_devices())

    def get_nodes(
        self,
        device_id: str,
    ) -> list[dict[str, Any]] | list[Node]:
        """Sync wrapper for ``AsyncSmartboxSession.get_nodes``."""
        return self._run_and_close(self._async.get_nodes(device_id=device_id))

    def get_device_connected(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceConnected:
        """Sync wrapper for ``AsyncSmartboxSession.get_device_connected``."""
        return self._run_and_close(
            self._async.get_device_connected(device_id=device_id)
        )

    def get_status(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> (
        dict[str, Any]
        | AcmNodeStatus
        | HtrNodeStatus
        | HtrModNodeStatus
        | DefaultNodeStatus
    ):
        """Sync wrapper for ``AsyncSmartboxSession.get_node_status``."""
        return self._run_and_close(
            self._async.get_node_status(device_id=device_id, node=node)
        )

    def set_status(
        self,
        device_id: str,
        node: dict[str, Any],
        status_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_status``."""
        return self._run_and_close(
            self._async.set_node_status(
                device_id=device_id, node=node, status_args=status_args
            )
        )

    def get_setup(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeSetup:
        """Sync wrapper for ``AsyncSmartboxSession.get_node_setup``."""
        return self._run_and_close(
            self._async.get_node_setup(device_id=device_id, node=node)
        )

    def set_setup(
        self,
        device_id: str,
        node: dict[str, Any],
        setup_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_setup``."""
        return self._run_and_close(
            self._async.set_node_setup(
                device_id=device_id, node=node, setup_args=setup_args
            )
        )

    def get_prog(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeProg:
        """Sync wrapper for ``AsyncSmartboxSession.get_node_prog``."""
        return self._run_and_close(
            self._async.get_node_prog(device_id=device_id, node=node)
        )

    def set_prog(
        self,
        device_id: str,
        node: dict[str, Any],
        prog_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_prog``."""
        return self._run_and_close(
            self._async.set_node_prog(
                device_id=device_id, node=node, prog_args=prog_args
            )
        )

    def get_node_samples(
        self,
        device_id: str,
        node: dict[str, Any],
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any] | Samples:
        """Sync wrapper for ``AsyncSmartboxSession.get_node_samples``."""
        return self._run_and_close(
            self._async.get_node_samples(
                device_id=device_id,
                node=node,
                start_time=start_time,
                end_time=end_time,
            )
        )

    def get_node_version(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any] | NodeVersion:
        """Sync wrapper for ``AsyncSmartboxSession.get_node_version``."""
        return self._run_and_close(
            self._async.get_node_version(device_id=device_id, node=node)
        )

    def set_mode(
        self,
        device_id: str,
        node: dict[str, Any],
        mode_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_mode``."""
        return self._run_and_close(
            self._async.set_node_mode(
                device_id=device_id, node=node, mode_args=mode_args
            )
        )

    def set_lock(
        self,
        device_id: str,
        node: dict[str, Any],
        lock_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_lock``."""
        return self._run_and_close(
            self._async.set_node_lock(
                device_id=device_id, node=node, lock_args=lock_args
            )
        )

    def set_boost(
        self,
        device_id: str,
        node: dict[str, Any],
        boost_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_boost``."""
        return self._run_and_close(
            self._async.set_node_boost(
                device_id=device_id, node=node, boost_args=boost_args
            )
        )

    def set_prog_temps(
        self,
        device_id: str,
        node: dict[str, Any],
        temps_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_prog_temps``."""
        return self._run_and_close(
            self._async.set_node_prog_temps(
                device_id=device_id, node=node, temps_args=temps_args
            )
        )

    def set_node_name(
        self,
        device_id: str,
        node: dict[str, Any],
        name: str,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_name``."""
        return self._run_and_close(
            self._async.set_node_name(device_id=device_id, node=node, name=name)
        )

    def set_node_select(
        self,
        device_id: str,
        node: dict[str, Any],
        select_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_node_select``."""
        return self._run_and_close(
            self._async.set_node_select(
                device_id=device_id, node=node, select_args=select_args
            )
        )

    def get_node_power(
        self,
        device_id: str,
        node: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_node_power``."""
        return self._run_and_close(
            self._async.get_node_power(device_id=device_id, node=node)
        )

    def delete_node(
        self,
        device_id: str,
        node: dict[str, Any],
        purge: bool = False,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.delete_node``."""
        return self._run_and_close(
            self._async.delete_node(device_id=device_id, node=node, purge=purge)
        )

    def get_device_away_status(
        self,
        device_id: str,
    ) -> dict[str, bool] | DeviceAwayStatus:
        """Sync wrapper for ``AsyncSmartboxSession.get_device_away_status``."""
        return self._run_and_close(
            self._async.get_device_away_status(device_id=device_id)
        )

    def set_device_away_status(
        self,
        device_id: str,
        status_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_device_away_status``."""
        return self._run_and_close(
            self._async.set_device_away_status(
                device_id=device_id, status_args=status_args
            )
        )

    def get_device_power_limit(
        self,
        device_id: str,
        node: dict[str, Any] | None = None,
    ) -> int:
        """Sync wrapper for ``AsyncSmartboxSession.get_device_power_limit``."""
        return self._run_and_close(
            self._async.get_device_power_limit(device_id=device_id, node=node)
        )

    def set_device_power_limit(
        self,
        device_id: str,
        power_limit: int,
        node: dict[str, Any] | None = None,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_device_power_limit``."""
        return self._run_and_close(
            self._async.set_device_power_limit(
                device_id=device_id, power_limit=power_limit, node=node
            )
        )

    def get_device_discovery(
        self,
        device_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_device_discovery``."""
        return self._run_and_close(
            self._async.get_device_discovery(device_id=device_id)
        )

    def set_device_discovery(
        self,
        device_id: str,
        discovery_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_device_discovery``."""
        return self._run_and_close(
            self._async.set_device_discovery(
                device_id=device_id, discovery_args=discovery_args
            )
        )

    def get_device_rtc_time(
        self,
        device_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_device_rtc_time``."""
        return self._run_and_close(
            self._async.get_device_rtc_time(device_id=device_id)
        )

    def set_device_name(
        self,
        device_id: str,
        name: str,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_device_name``."""
        return self._run_and_close(
            self._async.set_device_name(device_id=device_id, name=name)
        )

    def delete_device(
        self,
        device_id: str,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.delete_device``."""
        return self._run_and_close(
            self._async.delete_device(device_id=device_id)
        )

    def move_device_to_group(
        self,
        device_id: str,
        group_args: dict[str, Any],
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.move_device_to_group``."""
        return self._run_and_close(
            self._async.move_device_to_group(
                device_id=device_id, group_args=group_args
            )
        )

    def get_group_geo_data(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_group_geo_data``."""
        return self._run_and_close(
            self._async.get_group_geo_data(group_id=group_id)
        )

    def set_group_name(
        self,
        group_id: str,
        name: str,
    ) -> None:
        """Sync wrapper for ``AsyncSmartboxSession.set_group_name``."""
        return self._run_and_close(
            self._async.set_group_name(group_id=group_id, name=name)
        )

    def get_group_extra_data(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_group_extra_data``."""
        return self._run_and_close(
            self._async.get_group_extra_data(group_id=group_id)
        )

    def get_home_guests(
        self,
        home_id: str,
    ) -> list[dict[str, Any]] | Guests:
        """Sync wrapper for ``AsyncSmartboxSession.get_home_guests``."""
        return self._run_and_close(self._async.get_home_guests(home_id=home_id))

    def invite_user(
        self,
        user_id: str,
        home_id: str,
        email: str,
        confirmation_url: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.invite_user``."""
        return self._run_and_close(
            self._async.invite_user(
                user_id=user_id,
                home_id=home_id,
                email=email,
                confirmation_url=confirmation_url,
            )
        )

    def revoke_invite(
        self,
        user_id: str,
        home_id: str,
        email: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.revoke_invite``."""
        return self._run_and_close(
            self._async.revoke_invite(
                user_id=user_id, home_id=home_id, email=email
            )
        )

    def confirm_invite(
        self,
        user_id: str,
        password: str,
        code: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.confirm_invite``."""
        return self._run_and_close(
            self._async.confirm_invite(
                user_id=user_id, password=password, code=code
            )
        )

    def get_quiet_home_notifications(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_quiet_home_notifications``."""
        return self._run_and_close(
            self._async.get_quiet_home_notifications(group_id=group_id)
        )

    def set_quiet_home_notifications(
        self,
        group_id: str,
        notification_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.set_quiet_home_notifications``."""
        return self._run_and_close(
            self._async.set_quiet_home_notifications(
                group_id=group_id, notification_data=notification_data
            )
        )

    def test_quiet_home_notifications(
        self,
        group_id: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.test_quiet_home_notifications``."""
        return self._run_and_close(
            self._async.test_quiet_home_notifications(group_id=group_id)
        )

    def get_encrypted_wifi_credentials(
        self,
        ssid: str,
        wifi_password: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_encrypted_wifi_credentials``."""
        return self._run_and_close(
            self._async.get_encrypted_wifi_credentials(
                ssid=ssid, wifi_password=wifi_password
            )
        )

    def get_coordinates(
        self,
        country: str,
        state: str,
        city: str,
        zip_code: str,
    ) -> dict[str, Any]:
        """Sync wrapper for ``AsyncSmartboxSession.get_coordinates``."""
        return self._run_and_close(
            self._async.get_coordinates(
                country=country, state=state, city=city, zip_code=zip_code
            )
        )
