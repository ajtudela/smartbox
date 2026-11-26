#!/usr/bin/env python
"""Probe the vendor web app's per-user websocket endpoint (`ws_user`).

The official web app speaks a plain JSON websocket protocol on
`wss://<api host>:443/api/v2/ws_user?token=<enc>&user_id=<id>` — no
socket.io/engineio layer (documented in hass-smartbox docs/api-contract.md
Appendix A, "Transports — socket_io vs ws_user").
This probe characterises the live behaviour of that endpoint on a real
account. The transport it validated is implemented in
`src/smartbox/ws_user.py` (see hass-smartbox docs/api-contract.md §4 and
Appendix A, "Transports — socket_io vs ws_user").

Steps:
1. Authenticate via the library's REST session (credentials from .env).
2. Extract user_id from the access-token JWT payload (same as the app).
3. Connect and send `{"event":"all_data"}` on open; on every server
   `{"event":"should_sync"}` re-send all_data after a 1 s debounce
   (mirrors the app's `scheduleAllDataSync`).
4. On disconnect: log the close code, refresh the token (the app
   reconnects with a fresh token every time) and reconnect — so a
   long run observes every close/reconnect cycle.
5. Log every frame (event type, size, compact payload) with inter-frame
   timing, then print a summary.

Fallback doctrine (mirrors the shipped transport's run loop): only
deterministic handshake rejections (HTTP 401/403/404/410) stop the run;
transient transport errors are retried and NEVER treated as
'endpoint unsupported'.

Credentials and tokens are NEVER printed; device IDs are masked, so the
output is safe to share. Requires real hardware/accounts (Phase 4).

Usage:
    uv run python tools/probe_ws_user.py [--duration 600] \
        [--heartbeat 60] [--token-file /tmp/wsu_store.json]
"""

import argparse
import asyncio
import base64
import binascii
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, cast

import aiohttp

from smartbox import AsyncSmartboxSession
from smartbox.error import (
    APIUnavailableError,
    InvalidAuthError,
    ResellerNotExistError,
    SmartboxError,
)
from smartbox.reseller import AvailableResellers
from smartbox.ws_user import _PROBE_UNSUPPORTED_STATUSES, build_ws_user_url

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
REQUIRED_KEYS = ("SMARTBOX_API_NAME", "SMARTBOX_USERNAME", "SMARTBOX_PASSWORD")
_MAX_CONSECUTIVE_FAILURES = 60  # tolerate long client-side outages
_RECONNECT_DELAY = 2.0


def load_env(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file (no third-party dependency)."""
    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def mask(value: str, keep: int = 4) -> str:
    """Mask an identifier, keeping only a short prefix."""
    return (value[:keep] + "…") if len(value) > keep else "…"


def jwt_payload(token: str) -> dict[str, Any]:
    """Decode the payload part of a JWT (no signature verification)."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return cast("dict[str, Any]", json.loads(base64.urlsafe_b64decode(part)))


def load_token_store(path: Path) -> dict[str, Any]:
    """Load a stored token bundle (token, user_id, issued_at, expires_at).

    Stores written by earlier probe rounds may carry extra keys (e.g.
    a refresh_token, no longer stored) — tolerated; REQUIRED keys are
    validated with a clear message instead of a raw KeyError.
    """
    store = cast("dict[str, Any]", json.loads(path.read_text()))
    missing = [key for key in ("token", "user_id") if not store.get(key)]
    if missing:
        msg = (
            f"token store {path} is missing {', '.join(missing)} "
            "(stale format?) — delete it and re-run without --token-file"
        )
        raise ValueError(msg)
    return store


def save_token_store(path: Path, bundle: dict[str, Any]) -> None:
    """Persist a token bundle with owner-only permissions.

    Tokens are secrets: the file must live OUTSIDE the git tree (or be
    gitignored). Written atomically via mkstemp in the target directory
    — 0600 at creation, unpredictable name, no symlink target — then
    renamed into place, so no window ever exposes it at umask defaults.
    Never print or commit its contents.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    with os.fdopen(fd, "w") as fh:
        json.dump(bundle, fh, indent=2)
        fh.flush()
        Path(tmp_name).chmod(0o600)
    Path(tmp_name).replace(path)


def frame_event(raw: str) -> tuple[str, Any, bool]:
    """Classify a text frame → (event, payload, is_push)."""
    data = json.loads(raw)
    event = data.get("event") or "<no event>"
    is_push = event in {"all_data", "update", "should_sync"}
    return event, data, is_push


class Probe:
    """Frame log + stats for the ws_user session."""

    def __init__(self) -> None:
        """Initialise empty counters."""
        self.frames = 0
        self.last_frame_time: float | None = None
        self.push_count = 0
        self.gaps: list[float] = []
        self.max_gap = 0.0
        self.max_gap_stamp = "-"
        self.event_counts: dict[str, int] = {}

    def on_frame(self, event: str, *, is_push: bool = False) -> None:
        """Record one received frame and its timing."""
        now = time.monotonic()
        self.frames += 1
        self.event_counts[event] = self.event_counts.get(event, 0) + 1
        if self.last_frame_time is not None:
            gap = now - self.last_frame_time
            self.gaps.append(gap)
            if gap > self.max_gap:
                self.max_gap = gap
                self.max_gap_stamp = time.strftime("%H:%M:%S")
        self.last_frame_time = now
        if is_push:
            self.push_count += 1

    def break_gap(self) -> None:
        """Reset gap tracking at a reconnect boundary."""
        self.last_frame_time = None

    def summary(self, duration: float, cycles: list[str]) -> str:
        """Render the session summary lines."""
        lines = [
            "== summary ==",
            (f"duration={duration:.0f}s frames={self.frames} "
             f"cycles={len(cycles) + 1}"),
            f"events: {json.dumps(self.event_counts, sort_keys=True)}",
        ]
        lines.extend(f"  cycle end: {cycle}" for cycle in cycles)
        if self.gaps:
            lines.append(
                f"inter-frame gaps: n={len(self.gaps)} "
                f"min={min(self.gaps):.1f}s max={max(self.gaps):.1f}s "
                f"(max at {self.max_gap_stamp})"
            )
        else:
            lines.append("inter-frame gaps: none observed")
        return "\n".join(lines)


def compact(data: Any, limit: int = 400) -> str:
    """Render a payload compactly, truncating long structures."""
    text = json.dumps(data, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def data_summary(event: str, data: Any) -> Any:
    """Reduce bulky payloads to a shareable compact summary."""
    out = data.get("data")
    if event == "all_data" and isinstance(out, list):
        out = {
            "homes": len(out),
            "devs_per_home": [len(h.get("devs", [])) for h in out],
        }
    return out


def text_frame_line(
    raw: str, size: int, probe_state: Probe, on_should_sync: Any
) -> str | None:
    """Handle one text frame; return its log line, or None if malformed."""
    try:
        event, data, is_push = frame_event(raw)
    except (AttributeError, KeyError, TypeError, ValueError):
        return f"<< <unparseable frame> {size}B"
    probe_state.on_frame(event, is_push=is_push)
    if event == "should_sync":
        on_should_sync()
    stamp = time.strftime("%H:%M:%S")
    return f"<< {stamp} {event} {size}B {compact(data_summary(event, data))}"


async def send_all_data(ws: Any) -> None:
    """Send a follow-up all_data request (debounce callback)."""
    try:
        await ws.send_json({"event": "all_data"})
        print(">> sent all_data re-request")
    except (aiohttp.ClientError, OSError) as e:
        print(f"·· could not send all_data re-request: {e!r}")


async def observe(
    ws: Any, probe_state: Probe, deadline: float
) -> asyncio.TimerHandle | None:
    """Receive frames until the deadline or socket close.

    Returns a still-pending all_data debounce timer, if any.
    """
    pending: asyncio.TimerHandle | None = None
    loop = asyncio.get_running_loop()

    def schedule_all_data() -> None:
        # Mirror the app's scheduleAllDataSync: debounce 1 s.
        nonlocal pending
        if pending is not None:
            pending.cancel()
        pending = loop.call_later(
            1.0, lambda: asyncio.ensure_future(send_all_data(ws))
        )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except aiohttp.ServerTimeoutError:
            # Client heartbeat died: our PING went unanswered — path dead.
            print("!! heartbeat timeout: no PONG — path dead, reconnecting")
            break
        except TimeoutError:
            break
        if msg.type is aiohttp.WSMsgType.TEXT:
            line = text_frame_line(
                msg.data, len(msg.data), probe_state, schedule_all_data
            )
            if line is not None:
                print(line)
            continue
        if msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            print(f"!! socket closed by peer: code={ws.close_code}")
            break
        # PING/PONG are handled client-side (autoping); note them.
        print(f"·· {msg.type}")
    return pending


async def run_cycle(
    session: AsyncSmartboxSession,
    user_id: str,
    probe_state: Probe,
    deadline: float,
    stored_token: str | None = None,
    heartbeat: float | None = None,
) -> tuple[str | None, asyncio.TimerHandle | None, bool, bool]:
    """One connect/observe cycle.

    Returns (cycle_end_note or None, pending_timer, stop, failed).
    """
    # Mirror the app: a fresh token for every (re)connect (fetched in
    # the block below, with transient REST failures retried by the
    # outer loop per the fallback doctrine). Token-store mode skips
    # REST entirely: the stored token is used verbatim.
    if stored_token is None:
        # Fresh token per connect (mirrors the app). Transient REST
        # failures are RETRIED by the outer loop per the fallback
        # doctrine; a genuine auth rejection stops the run.
        stamp = time.strftime("%H:%M:%S")
        try:
            await session.get_devices()
            token = session.access_token
        except InvalidAuthError as e:
            print(f"!! [{stamp}] re-auth rejected: {e!r} — stopping")
            return f"{stamp} re-auth rejected", None, True, True
        except (APIUnavailableError, SmartboxError, OSError) as e:
            # APIUnavailableError is also an aiohttp.ClientError, so
            # network outages land here as retriable failures.
            print(f"·· [{stamp}] re-auth unavailable: {e!r}")
            return f"{stamp} re-auth unavailable", None, False, True
    else:
        token = stored_token
    probe_state.break_gap()
    stamp = time.strftime("%H:%M:%S")
    try:
        async with session.client.ws_connect(
            build_ws_user_url(session.api_host, token, user_id),
            heartbeat=heartbeat,
        ) as ws:
            print(f"== [{stamp}] connected ==")
            await ws.send_json({"event": "all_data"})
            print(">> sent all_data request")
            pending = await observe(ws, probe_state, deadline)
            if time.monotonic() >= deadline:
                return None, pending, True, False
            return f"{stamp} closed code={ws.close_code}", pending, False, False
    except aiohttp.ClientResponseError as e:
        print(f"!! [{stamp}] handshake rejected: {e.status} {e.message}")
        note = f"{stamp} handshake rejected {e.status}"
        if e.status in _PROBE_UNSUPPORTED_STATUSES:
            return note, None, True, True
        return note, None, False, True
    except (aiohttp.ClientError, APIUnavailableError, OSError) as e:
        print(f"·· [{stamp}] transport error: {e!r}")
        return f"{stamp} transport error", None, False, True


def load_or_create_store(
    token_file: Path, token: str, payload: dict[str, Any], user_id: str,
) -> dict[str, Any]:
    """Load an existing token store or create one from a fresh token."""
    if token_file.exists():
        store = load_token_store(token_file)
        issued_at = float(store.get("issued_at", time.time()))
        expires_at = float(store.get("expires_at", 0))
        age = time.time() - issued_at
        print(
            f"== token store: reusing token issued {age / 3600:.2f} h ago "
            f"(exp in {(expires_at - time.time()) / 3600:.2f} h) =="
        )
    else:
        # The access token alone: nothing reads the stored refresh
        # token, and persisting a reusable credential the tool never
        # needs widens the blast radius for no benefit.
        store = {
            "token": token,
            "user_id": user_id,
            "issued_at": time.time(),
            "expires_at": float(payload.get("exp", 0)),
        }
        expires_at = float(store["expires_at"])
        save_token_store(token_file, store)
        print(
            f"== token store: created {token_file} (exp at "
            f"{time.strftime('%H:%M:%S', time.localtime(expires_at))}) =="
        )
    return store


async def probe(
    duration: float, token_file: Path | None = None, heartbeat: float | None = None
) -> int:
    """Run the ws_user probe. Returns an exit code."""
    env = load_env(ENV_PATH)
    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    if missing:
        print(f"FAIL: missing keys in {ENV_PATH.name}: {', '.join(missing)}")
        return 1
    try:
        session = AsyncSmartboxSession(
            api_name=env["SMARTBOX_API_NAME"],
            username=env["SMARTBOX_USERNAME"],
            password=env["SMARTBOX_PASSWORD"],
        )
    except ResellerNotExistError as e:
        known = ", ".join(sorted(AvailableResellers.resellers))
        print(f"FAIL: unknown reseller key. Valid keys: {known}\n({e})")
        return 1

    probe_state = Probe()
    cycles: list[str] = []
    failures = 0
    try:
        async with session:
            print(f"== reseller: {session.reseller.name} ==")
            print(f"== api host: {session.api_host} ==")
            # Authentication is lazy in the library — trigger it with a
            # real authed call (health_check/api_version are public).
            await session.get_devices()
            token = session.access_token
            try:
                payload = jwt_payload(token)
            except (ValueError, KeyError, binascii.Error, IndexError):
                print("FAIL: access token is not a JWT — cannot extract user_id")
                return 1
            user_id = payload.get("userId") or ""
            if not user_id:
                print("FAIL: no userId in token payload")
                return 1
            print(f"== user_id: {mask(user_id)} (from token payload) ==")

            # Store/reuse mode: authenticate ONCE, then reuse the
            # stored token verbatim in every cycle (no REST refresh).
            store = (
                load_or_create_store(
                    token_file, token, payload, user_id
                )
                if token_file is not None
                else None
            )

            start = time.monotonic()
            while True:
                if time.monotonic() - start >= duration:
                    break
                note, pending, stop, failed = await run_cycle(
                    session,
                    user_id,
                    probe_state,
                    start + duration,
                    stored_token=None if store is None else store["token"],
                    heartbeat=heartbeat,
                )
                if note is not None:
                    cycles.append(note)
                if pending is not None:
                    pending.cancel()
                if stop:
                    break
                failures = 0 if not failed else failures + 1
                if failures >= _MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"FAIL: {_MAX_CONSECUTIVE_FAILURES} consecutive "
                        "failures — giving up"
                    )
                    break
                await asyncio.sleep(_RECONNECT_DELAY)
    except (InvalidAuthError, APIUnavailableError, SmartboxError) as e:
        print(f"FAIL: {e!r}")
        return 1

    print(probe_state.summary(duration, cycles))
    print("OK: probe finished")
    return 0


def main() -> int:
    """Parse args and run the probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=600.0,
        help="how long to observe, in seconds (default 600)",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=None,
        help="client WS ping interval in seconds (aiohttp heartbeat); "
        "PONG required within half the interval or the connection "
        "closes itself",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="store/reuse a token bundle: first run authenticates and "
        "saves; later runs reuse the stored token verbatim (no REST "
        "refresh) so sockets see tokens of a controlled age. The file "
        "contains a live access token — keep it outside any repository "
        "or synced folder",
    )
    args = parser.parse_args()
    return asyncio.run(probe(args.duration, args.token_file, args.heartbeat))


if __name__ == "__main__":
    sys.exit(main())
