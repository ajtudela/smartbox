"""Live probe for writes inside the post-write settle window.

After every accepted POST the server pushes a transient
{"sync_status": "lost"} frame for the node
(hass-smartbox docs/api-contract.md §5.4)
— a settle window of a few seconds. User reports (HA, 2026-09-28) that a
setpoint write issued right after a mode write appears stored in the UI
(optimistic) but the heater never applies it — hypothesised: the second
write lands inside the first write's settle window and is silently
dropped.

S1 fires a mode write and an immediate setpoint write (no delay). S2
re-sends the same setpoint after the window (control). If S1 shows the
setpoint NOT stored/applied while S2 does, the drop is proven.

Run tools/watch_status.py concurrently to capture websocket frames.

Usage:
  uv run python tools/probe_settle_window.py [--addr 5] [--confirm]
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


async def run_probe(addr: int, confirm: bool) -> int:
    """Execute the settle-window probe + restore cycle."""
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
            int(n["addr"]): await get_status(session, dev_id, n) for n in nodes
        }
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True))
        base = baseline[addr]
        log(f"snapshot: target htr/{addr} mode={base['mode']} stemp={base['stemp']}")
        if not confirm:
            log("DRY RUN - re-run with --confirm to execute writes.")
            return 0

        # Ensure auto (remembers the original mode for restore)
        if base["mode"] != "auto":
            log(f"S0 POST {{'mode': 'auto'}} (was {base['mode']!r})")
            r = await post_status(session, dev_id, target, {"mode": "auto"})
            log(f"S0 result: {r}")
            await asyncio.sleep(5)
        else:
            # Refresh the settle window with a real (no-op) mode write
            log("S0 POST {'mode': 'auto'} (no-op mode write to open a window)")
            r = await post_status(session, dev_id, target, {"mode": "auto"})
            log(f"S0 result: {r}")

        # S1: setpoint write IMMEDIATELY after the mode write (no delay) —
        # it should land inside the settle window
        st = await get_status(session, dev_id, target)
        s1 = f"{float(st['mtemp']) + 2.0:.1f}"
        log(f"S1 POST {{'stemp': '{s1}', 'units': 'C'}} (immediate, in-window)")
        r = await post_status(
            session, dev_id, target, {"stemp": s1, "units": "C"}
        )
        log(f"S1 result: {r}")
        await asyncio.sleep(6)
        st = await get_status(session, dev_id, target)
        log(
            f"S1 verify GET: mode={st['mode']!r} stemp={st['stemp']!r} "
            f"(expect {s1!r} if the in-window write was applied)"
        )

        # S2 control: same setpoint again, now that the window has closed
        log(f"S2 POST {{'stemp': '{s1}', 'units': 'C'}} (control, post-window)")
        r = await post_status(session, dev_id, target, {"stemp": s1, "units": "C"})
        log(f"S2 result: {r}")
        await asyncio.sleep(6)
        st = await get_status(session, dev_id, target)
        log(
            f"S2 verify GET: mode={st['mode']!r} stemp={st['stemp']!r} "
            f"(expect {s1!r})"
        )

        # Restore: one key per POST
        for payload in (
            {"stemp": base["stemp"], "units": "C"},
            {"mode": base["mode"]},
        ):
            log(f"RESTORE POST {payload}")
            r = await post_status(session, dev_id, target, payload)
            log(f"RESTORE result: {r}")
            await asyncio.sleep(3)
        await asyncio.sleep(3)

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
    return asyncio.run(run_probe(args.addr, args.confirm))


if __name__ == "__main__":
    sys.exit(main())
