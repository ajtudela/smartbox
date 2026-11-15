"""REST client: token plumbing, read operations and the POST seam.

Error contract:

- ``TimeoutError`` / ``ClientError`` / ``OSError`` -> ``TransportUnavailableError``;
  HTTP 5xx likewise, retried for GETs; 401 -> invalidate + ONE re-auth + resend
  (the server acted on nothing, so a resent write is safe too); any other 4xx
  -> ``SmartboxError``; a token-endpoint credential rejection -> ``AuthError``
  (see ``auth.py``).
- Every public call is bounded (~30 s including token refresh, retries and
  backoff); a backoff that would overrun the deadline is skipped.
- Raw dict passthrough: responses leave the client as the wire sent them.
  Normalization happens at the boundary, in ``values.py`` and ``writes.py``.
- Secrets hygiene: one DEBUG line per call (method, path, status), and
  ``ClientResponseError`` text stays out of messages and tracebacks because it
  renders the URL (``from None`` at the raise sites).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
from typing import TYPE_CHECKING, cast

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError

from . import timing
from .auth import AuthConfig, GrantCallback, TokenBundle, TokenManager
from .errors import AuthError, SmartboxError, TransportUnavailableError
from .paths import NodeRef, encode_segment, node_path
from .retry import jittered_backoff

if TYPE_CHECKING:
    from typing import Any


_LOGGER = logging.getLogger(__name__)

__all__ = [
    "NodeRef",
    "SmartboxClient",
    "encode_segment",
    "node_path",
]

_BACKOFF_FACTOR = 1.0
_CALL_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 3
_HTTP_NO_CONTENT = 204
_HTTP_BAD_REQUEST = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500


def _call_deadline() -> float:
    """Return the shared ~30 s deadline for one full client call."""
    return asyncio.get_running_loop().time() + _CALL_TIMEOUT


class SmartboxClient:
    """REST client over the ``/api/v2`` API with managed auth.

    Shares one HTTP session with its :class:`TokenManager` (injected, or lazily
    created and owned); ``close`` releases an owned session only.
    """

    def __init__(
        self,
        config: AuthConfig,
        session: ClientSession | None = None,
        *,
        on_grant: GrantCallback | None = None,
        initial_tokens: TokenBundle | None = None,
    ) -> None:
        """Inject a session, seed a stored bundle, and/or a persistence hook.

        ``on_grant`` receives every granted token bundle (the persistence seam
        for callers that keep tokens across process restarts);
        ``initial_tokens`` seeds the manager with the bundle from the previous
        process.
        """
        self._config = config
        self._auth = TokenManager(
            config,
            session=session,
            on_grant=on_grant,
            initial_tokens=initial_tokens,
        )

    @property
    def session(self) -> ClientSession:
        """The shared HTTP session (lazily created when not injected)."""
        return self._auth.session

    @property
    def auth(self) -> TokenManager:
        """The client's token manager (probe / token-source seam for callers)."""
        return self._auth

    async def close(self) -> None:
        """Close the OWNED session, if any; injected sessions stay open."""
        await self._auth.close()

    # --- read operations (raw passthrough) ---
    # No shipped-HA caller uses the six unmodeled reads below (get_devices /
    # get_dev_data / get_connected / get_away_status / get_power_limit /
    # get_discovery): they complete the protocol surface for the probe tooling,
    # which drives get_away_status and get_power_limit, and for platforms built
    # on the store later.

    async def get_devices(self) -> dict[str, Any]:
        """GET ``/api/v2/devs`` -> ``{devs: [...], invited_to?}`` raw."""
        return cast("dict[str, Any]", await self._get("devs"))

    # --- unauthenticated host endpoints (system health) ---

    async def get_api_version(self) -> dict[str, Any]:
        """GET ``<host>/version`` (outside ``/api/v2``, no token) raw."""
        return cast(
            "dict[str, Any]",
            await self._send("GET", "version", None, _call_deadline(), authed=False),
        )

    async def health_check(self) -> dict[str, Any]:
        """GET ``<host>/health_check`` (outside ``/api/v2``, no token) raw."""
        return cast(
            "dict[str, Any]",
            await self._send(
                "GET", "health_check", None, _call_deadline(), authed=False
            ),
        )

    async def get_grouped_devices(self) -> list[dict[str, Any]]:
        """GET ``/api/v2/grouped_devs`` -> homes (``extraData`` kept raw)."""
        return cast("list[dict[str, Any]]", await self._get("grouped_devs"))

    async def get_home_guests(self, home_id: str) -> dict[str, Any]:
        """GET ``/api/v2/groups/<home>/guest_users`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"groups/{encode_segment(home_id)}/guest_users"),
        )

    async def get_sessions(self) -> Any:  # noqa: ANN401 - raw wire payload
        """GET ``/client/sessions`` (user account, outside ``/api/v2``) raw."""
        return await self._get("client/sessions", api_v2=False)

    async def get_dev_data(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/dev_data`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/dev_data"),
        )

    async def get_nodes(self, dev_id: str) -> list[dict[str, Any]]:
        """GET ``/api/v2/devs/<id>/mgr/nodes``, unwrapping the ``nodes`` list."""
        payload = await self._get(f"devs/{encode_segment(dev_id)}/mgr/nodes")
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        if not isinstance(nodes, list):
            msg = f"unexpected nodes payload for device {dev_id!r}"
            raise SmartboxError(msg)
        return nodes

    async def get_node_status(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/<type>/<addr>/status`` raw."""
        return cast(
            "dict[str, Any]", await self._get(node_path(dev_id, node, "status"))
        )

    async def get_node_setup(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/<type>/<addr>/setup`` raw."""
        return cast("dict[str, Any]", await self._get(node_path(dev_id, node, "setup")))

    async def get_node_prog(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/<type>/<addr>/prog`` raw."""
        return cast("dict[str, Any]", await self._get(node_path(dev_id, node, "prog")))

    async def get_node_version(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/<type>/<addr>/version`` raw."""
        return cast(
            "dict[str, Any]", await self._get(node_path(dev_id, node, "version"))
        )

    async def get_node_samples(
        self,
        dev_id: str,
        node: NodeRef,
        from_epoch: int,
        to_epoch: int,
    ) -> list[dict[str, Any]]:
        """GET ``.../samples?start=&end=`` -> the rows.

        The wire wraps the rows in ``{"samples": [...]}``
        (hass-smartbox docs/api-contract.md §3.1 E19); a bare list is accepted for the
        endpoint's raw variant. Anything else is a malformed success.
        """
        payload = await self._get(
            node_path(dev_id, node, "samples"),
            params={"start": from_epoch, "end": to_epoch},
        )
        samples = payload.get("samples") if isinstance(payload, Mapping) else None
        if isinstance(samples, list):
            return samples
        if isinstance(payload, list):
            return payload
        msg = (
            f"unexpected samples payload for device {dev_id!r} "
            f"(type={type(payload).__name__})"
        )
        raise SmartboxError(msg)

    async def get_node_power(self, dev_id: str, node: NodeRef) -> int:
        """GET ``.../power`` -> the node's power value as int (PMO nodes).

        A separate read from the device-level power limit at
        ``htr_system/power_limit``; the asymmetry with that write is unverified
        on pmo hardware (hass-smartbox docs/api-contract.md §3.1).
        """
        response = await self._get(node_path(dev_id, node, "power"))
        try:
            return int(response["power"])
        except (KeyError, TypeError, ValueError) as e:
            # never embed the payload: type name + length only
            msg = (
                f"Unexpected power response for device {dev_id!r} "
                f"(type={type(response).__name__}, "
                f"length={len(response) if hasattr(response, '__len__') else 'n/a'})"
            )
            raise SmartboxError(msg) from e

    async def get_connected(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/connected`` -> ``{connected: bool}`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/connected"),
        )

    async def get_away_status(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/mgr/away_status`` -> ``{away, enabled, forced}`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/mgr/away_status"),
        )

    async def get_power_limit(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/htr_system/power_limit`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/htr_system/power_limit"),
        )

    async def get_rtc_time(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/mgr/rtc/time`` raw.

        The ``n`` field is a 0-INDEXED month (September -> 8), deliberately
        left unnormalized.
        """
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/mgr/rtc/time"),
        )

    async def get_discovery(self, dev_id: str) -> dict[str, Any]:
        """GET ``/api/v2/devs/<id>/mgr/discovery`` -> ``{discovery}`` raw."""
        return cast(
            "dict[str, Any]",
            await self._get(f"devs/{encode_segment(dev_id)}/mgr/discovery"),
        )

    # --- plumbing ---

    async def _get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        api_v2: bool = True,
    ) -> Any:  # noqa: ANN401 - raw wire payload passthrough (normalization later)
        """GET with bounded retries on transient failures.

        The whole call shares one ~30 s deadline (token refresh, retries,
        backoff); a backoff that would overrun it is skipped. One 401 triggers
        invalidate + re-auth + resend inside the same budget.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CALL_TIMEOUT
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                return await self._send("GET", path, params, deadline, api_v2=api_v2)
            except TransportUnavailableError:
                backoff = jittered_backoff(attempt, _BACKOFF_FACTOR, _CALL_TIMEOUT)
                if attempt == _RETRY_ATTEMPTS - 1 or loop.time() + backoff >= deadline:
                    raise
                _LOGGER.debug("smartbox GET %s failed; retrying", path)
                await timing.sleep(backoff)
        raise AssertionError  # pragma: no cover - loop always returns/raises

    async def post(self, path: str, body: Mapping[str, Any]) -> None:
        """One POST attempt; NEVER retried on transient failures.

        A 5xx or network failure is raised straight through, unlike
        :meth:`_get`: the server may have applied the write, so a retry could
        double-apply it. The 401 -> invalidate + re-auth + resend stays, since
        the server acted on nothing there.
        """
        loop = asyncio.get_running_loop()
        await self._send(
            "POST",
            path,
            None,
            loop.time() + _CALL_TIMEOUT,
            body=body,
        )

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        """Headers for one authenticated request (contract §1.2).

        The reseller serial travels on every authenticated call, the token
        POST included (the current vendor clients send it there and on the
        API calls alike); ``Content-Type`` is set by the JSON body aiohttp
        encodes.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        if self._config.serial_id:
            headers["x-serialid"] = str(self._config.serial_id)
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        deadline: float,
        *,
        body: Mapping[str, Any] | None = None,
        reauthed: bool = False,
        authed: bool = True,
        api_v2: bool = True,
    ) -> Any:  # noqa: ANN401 - raw wire payload passthrough (normalization later)
        """One authorized request; 401 -> one invalidate + re-auth + resend.

        ``authed=False`` hits a host endpoint outside ``/api/v2`` (the
        system-health ``/version`` and ``/health_check`` reads): no token is
        requested and a 401 is a plain 4xx, not a credential signal. The token
        wait is INSIDE the deadline, so a caller queued behind a concurrent
        grant cannot push the request past the call budget.
        """
        bundle: TokenBundle | None = None
        try:
            async with asyncio.timeout_at(deadline):
                if authed:
                    bundle = await self._auth.get_token()
                    prefix = "/api/v2/" if api_v2 else "/"
                    url = f"{self._config.api_host.rstrip('/')}{prefix}{path}"
                    headers = self._auth_headers(bundle.access_token)
                else:
                    url = f"{self._config.api_host.rstrip('/')}/{path}"
                    headers = {}
                async with self._auth.session.request(
                    method,
                    url,
                    headers=headers,
                    params=dict(params) if params else None,
                    json=dict(body) if body is not None else None,
                ) as resp:
                    status = resp.status
                    logged_path = f"api/v2/{path}" if api_v2 else path
                    _LOGGER.debug("smartbox %s /%s -> %d", method, logged_path, status)
                    if status < _HTTP_BAD_REQUEST:
                        return await _decode(resp)
                    if status == _HTTP_UNAUTHORIZED:
                        if not authed or bundle is None:
                            # A host endpoint carries no token: a 401 here
                            # is a plain 4xx, never a credential signal.
                            msg = (
                                f"smartbox API rejected {method} {path} "
                                f"(status={status})"
                            )
                            raise SmartboxError(msg) from None
                        if reauthed:
                            # The freshly granted token was rejected too: drop
                            # it, or every later call repeats refresh + 401.
                            await self._auth.invalidate()
                            msg = "smartbox rejected the re-issued token (status=401)"
                            raise AuthError(msg) from None
                        await self._auth.invalidate(bundle.access_token)
                        return await self._send(
                            method,
                            path,
                            params,
                            deadline,
                            body=body,
                            reauthed=True,
                            authed=authed,
                        )
                    if (
                        status >= _HTTP_SERVER_ERROR
                        or status == _HTTP_TOO_MANY_REQUESTS
                    ):
                        # Rate limiting is transient, exactly as the token
                        # endpoint classifies it: the GET ladder backs off and
                        # a write reports a retryable failure, never a
                        # rejection of the request itself.
                        msg = f"smartbox API unavailable ({method} {path}, status={status})"
                        raise TransportUnavailableError(msg) from None
                    msg = f"smartbox API rejected {method} {path} (status={status})"
                    raise SmartboxError(msg) from None
        except (TimeoutError, ClientError, OSError) as e:
            msg = f"smartbox API unreachable ({method} {path}, {type(e).__name__})"
            raise TransportUnavailableError(msg) from e


async def _decode(resp: ClientResponse) -> Any:  # noqa: ANN401 - raw wire payload passthrough
    """Decode a success body; empty and non-JSON bodies become ``{}``.

    204 and bodyless 200s decode to ``{}``: an empty body means "nothing to
    report". A JSON-typed body that fails to decode is a malformed success
    (``SmartboxError``, the message bounded to content type and length, never
    the body). Both failures are covered: malformed JSON, and bytes that are
    not decodable text at all - aiohttp decodes before parsing, so invalid
    UTF-8 raises ``UnicodeDecodeError``, a ``ValueError`` but not a
    ``JSONDecodeError``.
    """
    if resp.status == _HTTP_NO_CONTENT or resp.content_length == 0:
        return {}
    try:
        payload = await resp.json()
    except ContentTypeError:
        _LOGGER.debug("smartbox API returned a non-JSON body (status=%d)", resp.status)
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        msg = (
            "smartbox API returned an undecodable JSON body "
            f"(status={resp.status}, content_type={resp.content_type}, "
            f"length={resp.content_length})"
        )
        raise SmartboxError(msg) from e
    # aiohttp yields None for a bodyless or whitespace-only success with no
    # Content-Length; the client's contract for an empty success is {}
    return {} if payload is None else payload
