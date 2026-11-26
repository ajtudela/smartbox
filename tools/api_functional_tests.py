"""Functional API tests against live hardware: mode on/off, temperature.

Sequence (single target node): snapshot ALL nodes -> mode toggle -> manual
(the heater's "on") -> temperature bump -> locked toggle -> restore ->
verify every node against its snapshot. Controlled fields (mode, stemp,
locked, boost fields) must match exactly; volatile telemetry (mtemp,
power, ...) is reported as expected drift.

Writes happen ONLY with --confirm; the default is a dry run that prints
the plan. Run tools/watch_status.py concurrently to capture websocket
frames for each write.

Usage:
  uv run python tools/api_functional_tests.py [--addr 5] [--confirm]
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from typing import Any, cast

from live_check import ENV_PATH, load_env

from smartbox import AsyncSmartboxSession
from smartbox.error import SmartboxError

BASELINE_PATH = Path("/tmp/functional_baseline.json")
# Fields this test may change; restored and verified exactly.
CONTROLLED_KEYS = (
    "mode",
    "stemp",
    "locked",
    "boost",
    "boost_end_min",
    "boost_end_day",
)
# Telemetry that legitimately drifts between snapshot and verification.
VOLATILE_KEYS = (
    "mtemp",
    "power",
    "duty",
    "act_duty",
    "pcb_temp",
    "power_pcb_temp",
    "active",
    "presence",
    "sync_status",
    "true_radiant_active",
    "runback",
    "easy",
    "window_open",
    "error_code",
    "version",
)


def log(msg: str) -> None:
    """Timestamped progress line."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def get_status(
    session: AsyncSmartboxSession, dev_id: str, node: dict[str, Any]
) -> dict[str, Any]:
    """Raw node status."""
    return cast(
        "dict[str, Any]", await session.get_node_status(dev_id, node)
    )


async def post_status(
    session: AsyncSmartboxSession,
    dev_id: str,
    node: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    """POST a status update; return an acceptance/rejection string."""
    try:
        await session.set_node_status(dev_id, node, payload)
    except SmartboxError as err:
        return f"REJECTED {type(err).__name__}: {err}"
    return "accepted"


def diff_status(
    baseline: dict[str, Any], current: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Return (controlled mismatches, volatile drift) key descriptions."""
    mismatches = [
        f"{k}: baseline={baseline[k]!r} now={current.get(k)!r}"
        for k in CONTROLLED_KEYS
        if current.get(k) != baseline.get(k)
    ]
    drift = [
        k
        for k in VOLATILE_KEYS
        if current.get(k) != baseline.get(k) and k in baseline
    ]
    return mismatches, drift


async def run_tests(addr: int, confirm: bool) -> int:
    """Execute the full test + restore cycle."""
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

        baseline = {
            int(n["addr"]): await get_status(session, dev_id, n)
            for n in nodes
        }
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True))
        base = baseline[addr]
        log(
            f"snapshot: {len(nodes)} nodes -> {BASELINE_PATH}; "
            f"target htr/{addr} mode={base['mode']} stemp={base['stemp']} "
            f"locked={base['locked']}"
        )
        if not confirm:
            log("DRY RUN - re-run with --confirm to execute writes.")
            return 0

        # T1: mode -> auto (the heater's scheduled "on")
        t1_mode = "auto" if base["mode"] != "auto" else "off"
        log(f"T1 POST {{'mode': '{t1_mode}'}}")
        r1 = await post_status(session, dev_id, target, {"mode": t1_mode})
        log(f"T1 result: {r1}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"T1 verify GET: mode={st['mode']!r} (expect {t1_mode!r})")

        # T1b: modified_auto observation - bump temp while in auto
        if st["mode"] == "auto":
            bumped = f"{float(base['stemp']) + 0.5:.1f}"
            log(f"T1b POST {{'stemp': '{bumped}', 'units': 'C'}} while auto")
            r1b = await post_status(
                session, dev_id, target, {"stemp": bumped, "units": "C"}
            )
            log(f"T1b result: {r1b}")
            await asyncio.sleep(2)
            st = await get_status(session, dev_id, target)
            log(
                f"T1b verify GET: mode={st['mode']!r} stemp={st['stemp']!r} "
                "(watch for modified_auto)"
            )
        # T2: manual mode (the heater's "on")
        log("T2 POST {'mode': 'manual'}")
        r2 = await post_status(session, dev_id, target, {"mode": "manual"})
        log(f"T2 result: {r2}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"T2 verify GET: mode={st['mode']!r} stemp={st['stemp']!r}")

        # T3: temperature bump while manual (relative to CURRENT device
        # state — T1b may already have applied +0.5 over baseline)
        bumped = f"{float(st['stemp']) + 0.5:.1f}"
        log(f"T3 POST {{'stemp': '{bumped}', 'units': 'C'}}")
        r3 = await post_status(
            session, dev_id, target, {"stemp": bumped, "units": "C"}
        )
        log(f"T3 result: {r3}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"T3 verify GET: stemp={st['stemp']!r} (expect {bumped!r})")

        # T4: locked toggle
        log("T4 POST {'locked': True}")
        r4 = await post_status(session, dev_id, target, {"locked": True})
        log(f"T4 result: {r4}")
        await asyncio.sleep(2)
        st = await get_status(session, dev_id, target)
        log(f"T4 verify GET: locked={st['locked']!r} (expect True)")

        # Restore: one key per POST. Multi-key status posts are silently
        # PARTIALLY applied (locked+mode stored, stemp dropped) — the API
        # only reliably honors single-key updates
        # (see hass-smartbox docs/api-contract.md §5.1).
        for payload in (
            {"locked": base["locked"]},
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
    args = parser.parse_args()
    return asyncio.run(run_tests(args.addr, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
