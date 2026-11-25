"""Read-only CLI commands."""

from __future__ import annotations

from typing import Any

try:
    import asyncclick as click
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by users only
    raise SystemExit(
        "The smartbox CLI needs the optional dependency: pip install 'smartbox[cli]'"
    ) from exc

from smartbox.device.capabilities import check_control_mode
from smartbox.device.control_modes import control_mode_choices, control_mode_label

from .common import (
    _client,
    _find_device,
    _find_node,
    _find_node_info,
    _iter_devices,
    _mask_identifier,
    _node_facts,
    _pretty_print,
    _read_devices,
    _read_nodes,
    _require_check,
    smartbox,
)


@smartbox.command(help="Show devices")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def devices(ctx: click.Context, device_id: str | None) -> None:
    client = await _client(ctx)
    rows = [device async for device, _dev_id in _iter_devices(client, device_id)]
    if device_id and not rows:
        raise click.ClickException(f"no device matched {device_id!r}")
    _pretty_print(rows)


@smartbox.command(help="Show homes")
@click.pass_context
async def homes(ctx: click.Context) -> None:
    _pretty_print(await (await _client(ctx)).get_grouped_devices())


def _session_rows(payload: object) -> list[dict[str, Any]]:
    """Accept the raw list or ``{sessions: [...]}`` envelope."""
    if isinstance(payload, dict):
        rows = payload.get("sessions")
        return (
            [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


@smartbox.command(help="Show active account sessions")
@click.option("--json", "as_json", is_flag=True, help="Print the raw payload.")
@click.option(
    "--limit", type=int, default=20, show_default=True, help="Rows to show; 0 = all."
)
@click.pass_context
async def sessions(ctx: click.Context, as_json: bool, limit: int) -> None:
    payload = await (await _client(ctx)).get_sessions()
    if as_json or ctx.obj["json"]:
        _pretty_print(payload)
        return
    rows = _session_rows(payload)
    rows.sort(key=lambda row: str(row.get("createdAt") or ""), reverse=True)
    if limit > 0:
        rows = rows[:limit]
    click.echo(f"{'sid':10} {'created':26} {'last used':26} {'device':30} current")
    for row in rows:
        click.echo(
            f"{_mask_identifier(row.get('sid'), 8):10} "
            f"{row.get('createdAt') or '-'!s:26} "
            f"{row.get('lastUsedAt') or '-'!s:26} "
            f"{str(row.get('userAgent') or '-')[:30]:30} "
            f"{'*' if row.get('current') else ''}"
        )


@smartbox.command(help="Show nodes")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def nodes(ctx: click.Context, device_id: str | None) -> None:
    client = await _client(ctx)
    payloads: dict[str, Any] = {}
    found = False
    async for device, dev_id in _iter_devices(client, device_id):
        found = True
        data = await client.get_nodes(dev_id)
        if ctx.obj["json"]:
            payloads[dev_id] = {
                "name": device.get("name"),
                "nodes": {str(item.get("addr")): item for item in data},
            }
        else:
            click.echo(f"{device.get('name')} (dev_id: {dev_id})")
            _pretty_print(data)
    if device_id and not found:
        raise click.ClickException(f"no device matched {device_id!r}")
    if ctx.obj["json"]:
        _pretty_print(payloads)


@smartbox.command(help="Show node status")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def status(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    await _read_nodes(ctx, await _client(ctx), "get_node_status", device_id, node_addr)


@smartbox.command(help="Show node setup")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def setup(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    await _read_nodes(ctx, await _client(ctx), "get_node_setup", device_id, node_addr)


@smartbox.command(help="Show the node's control-mode label and choices")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.pass_context
async def control_modes(ctx: click.Context, device_id: str, node_addr: str) -> None:
    client = await _client(ctx)
    device = await _find_device(client, device_id)
    dev_id, node, node_row = await _find_node_info(client, device_id, node_addr, device)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_control_mode(facts),
        facts,
        node,
        "this node does not expose a control-mode setting",
        unavailable="does not expose a control-mode setting",
    )
    choices = tuple(
        sorted(
            control_mode_choices(facts.pid, facts.node_type, facts.setup) or (),
            key=lambda item: item[1],
        )
    )
    current = facts.setup.control_mode
    payload = {
        "device": {"dev_id": dev_id, "name": device.get("name")},
        "node": {
            "type": node.node_type,
            "addr": node.addr,
            "name": node_row.get("name"),
        },
        "current": current,
        "current_label": control_mode_label(choices, current),
        "choices": [{"index": index, "label": label} for label, index in choices],
    }
    if ctx.obj["json"]:
        _pretty_print(payload)
        return
    if current is None:
        click.echo("current: not set")
    elif payload["current_label"]:
        click.echo(f"current: {payload['current_label']} (wire index {current})")
    else:
        click.echo(f"current: not in this product's UI list (wire index {current})")
    click.echo(f"{'index':5} label")
    for label, index in choices:
        click.echo(f"{index:<5} {label}")


@smartbox.command(help="Show node prog")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def prog(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    await _read_nodes(ctx, await _client(ctx), "get_node_prog", device_id, node_addr)


@smartbox.command(help="Show node version")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def node_version(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    await _read_nodes(ctx, await _client(ctx), "get_node_version", device_id, node_addr)


@smartbox.command(help="Show node power (pmo nodes)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.pass_context
async def node_power(ctx: click.Context, device_id: str, node_addr: str) -> None:
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    if node.node_type != "pmo":
        raise click.ClickException(
            f"node power is a pmo-only read; {dev_id}/{node.node_type}/{node.addr} "
            "is not a pmo. Heaters report power in status; the box limit is "
            "device-power-limit."
        )
    _pretty_print(await client.get_node_power(dev_id, node))


@smartbox.command(help="Show device away_status")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def device_away_status(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_away_status", device_id)


@smartbox.command(help="Show device connected status")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def device_connected_status(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_connected", device_id)


@smartbox.command(help="Show device power_limit")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def device_power_limit(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_power_limit", device_id)


@smartbox.command(help="Show the box RTC time")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def rtc(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_rtc_time", device_id)


@smartbox.command(help="Show the box discovery payload")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def discovery(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_discovery", device_id)


@smartbox.command(help="Show the raw dev_data payload")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.pass_context
async def dev_data(ctx: click.Context, device_id: str | None) -> None:
    await _read_devices(ctx, await _client(ctx), "get_dev_data", device_id)
