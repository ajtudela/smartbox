"""Shared retry/backoff primitives for the websocket transports.

``smartbox.socket`` (per-device socket_io) and ``smartbox.ws_user``
(per-user ws_user) implement the same auth-refresh and reconnect
policies; this module keeps the policy in one place so the two
transports cannot drift apart under the same failure.
"""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import logging
import random

from smartbox.error import APIUnavailableError, InvalidAuthError, SmartboxError

_LOGGER = logging.getLogger(__name__)

# Saturation bound for backoff_delay's exponent (see its docstring).
_MAX_BACKOFF_EXPONENT = 6


def backoff_delay(attempt: int, factor: float, cap: float) -> float:
    """Capped exponential backoff: ``factor * 2^attempt``, capped.

    The exponent is saturated before the power: an unsaturated
    ``2**attempt`` raises ``OverflowError`` (int too large to convert to
    float) once the caller's failure counter exceeds ~1024 — a multi-day
    outage must sleep the cap, not crash the retry loop.
    """
    return min(factor * (2 ** min(attempt, _MAX_BACKOFF_EXPONENT)), cap)


def jittered(delay: float, spread: float = 0.2) -> float:
    """Add up to ``spread`` (fraction of ``delay``) of random extra wait.

    Desynchronises clients that all retry after the same outage, which
    is what stops a reseller incident from turning into a thundering herd
    when it recovers.
    """
    # Backoff jitter only: not security-sensitive (S311 waived use).
    return delay + spread * delay * random.SystemRandom().random()


async def sleep_unless_exiting(event: asyncio.Event, delay: float) -> None:
    """Sleep up to ``delay`` seconds, waking early once ``event`` is set.

    The transports pass their exit event, so teardown (or cancel) must
    not wait out a backoff sleep.
    """
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=delay)


async def refresh_auth_with_retry(
    check_refresh_auth: Callable[[], Awaitable[None]],
    *,
    exit_requested: Callable[[], bool],
    sleep_unless_exiting: Callable[[float], Awaitable[None]],
    backoff_factor: float,
    max_sleep: float,
    log_context: str,
) -> None:
    """Refresh the REST token a transport's URL carries, retrying transient failures.

    Shared contract of both transports: transient failures (API
    unreachable, 5xx, malformed token response) are waited out with
    capped backoff; rejected credentials (``InvalidAuthError``, raised
    only after the session's password-login fallback also failed)
    propagate to the caller's run loop. Returns early once the caller's
    exit is requested.
    """
    failures = 0
    while not exit_requested():
        try:
            await check_refresh_auth()
        except InvalidAuthError:
            _LOGGER.warning(
                "Credentials rejected; stopping %s websocket loop", log_context
            )
            raise
        except (APIUnavailableError, SmartboxError) as e:
            sleep_time = backoff_delay(failures, backoff_factor, max_sleep)
            failures += 1
            _LOGGER.warning(
                "Auth refresh failed (%s); retrying in %ss", e, sleep_time
            )
            await sleep_unless_exiting(sleep_time)
        else:
            return
