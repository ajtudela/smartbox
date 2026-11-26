"""Live A/B probe for setpoint-in-auto and mode:"modified_auto" engagement.

On fw 1.9 htr nodes, POST {"stemp", "units"} inside mode auto stores the
value (GET-confirmed, hass-smartbox docs/api-contract.md §5.2) but reportedly
does NOT drive the heater (HA user report, 2026-09-28). The official app adds
"mode": "modified_auto" to the same body (api-contract.md §5.2). This probe
writes DISTINCT set points with and without the mode key on one auto node
so the physical heater's response is unambiguous, polls wire state during
configurable observation windows (watch the heater to see which set point
it follows), then restores the fleet.

Run tools/watch_status.py concurrently to capture websocket frames.

Usage:
  uv run python tools/probe_modified_auto.py [--addr 5] [--confirm]
      [--observe 90]
"""

import argparse
import asyncio
import json
import sys
from typing import Any, cast

from api_functional_tests import (
    BASELINE_PATH,
    diff_status,
    get_status,
    log,
    post_status,
)
from live_check import ENV_PATH, load_env

from smartbox import AsyncSmartboxSession


async def observe(
    session: AsyncSmartboxSession, dev_id: str, node: dict[str, Any], seconds: int
) -> None:
    """Poll the target node's wire state during an observation window."""
    interval = 5
    for elapsed in range(0, seconds, interval):
        await asyncio.sleep(interval)
        st = await get_status(session, dev_id, node)
        log(
            f"observe {elapsed + interval:>3}s: mode={st['mode']!r} "
            f"stemp={st['stemp']!r} mtemp={st['mtemp']!r} "
            f"active={st.get('active')!r} duty={st.get('duty')!r}"
        )


async def run_tests(addr: int, confirm: bool, observe_seconds: int) -> int:
    """Execute the probe + restore cycle."""
    env = load_env(ENV_PATH)
    session = AsyncSmartboxSession(
        api_name=env["SMARTBOX_API_NAME"],
        username=env["SMARTBOX_USERNAME"],
        password=env["SMARTBOX_PASSWORD"],
    )
    async with session:
        devices = cast("list[dict[str, Any]]", await session.get_devices())
        dev_id = devices[0]["dev_id"]
        nodes = cast("list[dict[str, Any]]", await session.get_nodes(dev_id))
        target = next((n for n in nodes if int(n["addr"]) == addr), None)
        if target is None:
            print(f"no node with addr={addr}", file=sys.stderr)
            return 1
        if target["type"] != "htr":
            print(
                f"node {addr} is {target['type']}, probe expects htr",
                file=sys.stderr,
            )
            return 1

        baseline = {
            int(n["addr"]): await get_status(session, dev_id, n) for n in nodes
        }
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True))
        base = baseline[addr]
        log(
            f"snapshot: {len(nodes)} nodes -> {BASELINE_PATH}; "
            f"target htr/{addr} mode={base['mode']} stemp={base['stemp']}"
        )
        if not confirm:
            log("DRY RUN - re-run with --confirm to execute writes.")
            return 0
        return await run_probes(
            session, dev_id, nodes, target, base, baseline, observe_seconds
        )


async def run_probes(
    session: AsyncSmartboxSession,
    dev_id: str,
    nodes: list[dict[str, Any]],
    target: dict[str, Any],
    base: dict[str, Any],
    baseline: dict[int, dict[str, Any]],
    observe_seconds: int,
) -> int:
    """Run the write probes themselves (confirm mode only)."""
    # P0: get the node into auto (remember the original mode for restore)
    if base["mode"] != "auto":
        log(f"P0 POST {{'mode': 'auto'}} (was {base['mode']!r})")
        r = await post_status(session, dev_id, target, {"mode": "auto"})
        log(f"P0 result: {r}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"P0 verify GET: mode={st['mode']!r} (expect 'auto')")

    # P1 control: setpoint bump WITHOUT the mode key. Decisive control: the
    # set point must sit ABOVE current room temp so the heater has a real
    # reason to run — if it then stays idle, the stored-but-not-engaged
    # behavior is proven; if it heats, stemp-only writes do apply.
    st_now = await get_status(session, dev_id, target)
    p1 = f"{max(float(base['stemp']) + 1.0, float(st_now['mtemp']) + 1.0):.1f}"
    log(f"P1 POST {{'stemp': '{p1}', 'units': 'C'}} (control, no mode key)")
    r = await post_status(session, dev_id, target, {"stemp": p1, "units": "C"})
    log(f"P1 result: {r}")
    await asyncio.sleep(2)
    st = await get_status(session, dev_id, target)
    log(f"P1 verify GET: mode={st['mode']!r} stemp={st['stemp']!r} (expect {p1!r})")
    log(
        f"P1 OBSERVATION WINDOW {observe_seconds}s - watch the heater: "
        f"does it follow {p1}?"
    )
    await observe(session, dev_id, target, observe_seconds)

    # P2: setpoint bump WITH mode:"modified_auto" (the app's body) — a
    # distinct set point so the physical response is unambiguous
    p2 = f"{float(p1) + 1.5:.1f}"
    log(
        f"P2 POST {{'stemp': '{p2}', 'units': 'C', 'mode': 'modified_auto'}}"
        " (the app's body)"
    )
    r = await post_status(
        session,
        dev_id,
        target,
        {"stemp": p2, "units": "C", "mode": "modified_auto"},
    )
    log(f"P2 result: {r}")
    await asyncio.sleep(2)
    st = await get_status(session, dev_id, target)
    log(
        f"P2 verify GET: mode={st['mode']!r} (expect 'modified_auto'?) "
        f"stemp={st['stemp']!r} (expect {p2!r})"
    )
    log(
        f"P2 OBSERVATION WINDOW {observe_seconds}s - watch the heater: "
        f"does it follow {p2}?"
    )
    await observe(session, dev_id, target, observe_seconds)

    # P3: does it revert by itself? (modified_auto_span semantics unclear)
    st = await get_status(session, dev_id, target)
    log(f"P3 post-window GET: mode={st['mode']!r} stemp={st['stemp']!r}")
    if st["mode"] == "modified_auto":
        log("P3 POST {'mode': 'auto'} (explicit revert)")
        r = await post_status(session, dev_id, target, {"mode": "auto"})
        log(f"P3 result: {r}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"P3 verify GET: mode={st['mode']!r} (expect 'auto')")

    # Restore: one key per POST (multi-key posts partially apply)
    for payload in (
        {"stemp": base["stemp"], "units": "C"},
        {"mode": base["mode"]},
    ):
        log(f"RESTORE POST {payload}")
        r = await post_status(session, dev_id, target, payload)
        log(f"RESTORE result: {r}")
        await asyncio.sleep(2)
    await asyncio.sleep(3)

    # Full-fleet verification against the snapshot
    failures = 0
    for node in nodes:
        node_addr = int(node["addr"])
        cur = await get_status(session, dev_id, node)
        mismatches, drift = diff_status(baseline[node_addr], cur)
        name = node["name"]
        if mismatches:
            failures += 1
            log(f"VERIFY htr/{node_addr} ({name}): MISMATCH {mismatches}")
        else:
            log(
                f"VERIFY htr/{node_addr} ({name}): OK "
                f"(controlled fields match; volatile drift: {drift})"
            )
    log(f"DONE failures={failures}")
    return 1 if failures else 0


def main() -> int:
    """Parse arguments and run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr", type=int, default=5, help="target node addr")
    parser.add_argument(
        "--confirm", action="store_true", help="execute writes (default dry run)"
    )
    parser.add_argument(
        "--observe",
        type=int,
        default=90,
        help="observation window seconds after each probe write",
    )
    args = parser.parse_args()
    return asyncio.run(run_tests(args.addr, args.confirm, args.observe))


if __name__ == "__main__":
    sys.exit(main())
