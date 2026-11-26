"""Profile, diagnostic and account commands."""

from __future__ import annotations

import asyncio
import csv
from dataclasses import asdict
from pathlib import Path
import sys
import time
from typing import Any

try:
    import asyncclick as click
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by users only
    raise SystemExit(
        "The smartbox CLI needs the optional dependency: pip install 'smartbox[cli]'"
    ) from exc

from smartbox import __version__
from smartbox.device.codes import PRODUCT_CODES, ProductCode, code_info, product_code
from smartbox.device.profile import resolve_profile, resolve_stored_profile
from smartbox.device.setup import SetupView
from smartbox.protocol import (
    AvailableResellers,
    ManagedTokenSource,
    NodeRef,
    SmartboxClient,
    WsUserTransport,
)
from smartbox.protocol.events import Event, Snapshot, SocketClosed, Update
from smartbox.protocol.reseller import SMARTBOX_GENERIC_BASIC_AUTH
from smartbox.token_cache import default_cache_path

from .common import (
    _client,
    _dev_list,
    _find_device,
    _find_node,
    _find_node_info,
    _load_json_object,
    _mask_username,
    _parse_time,
    _pretty_print,
    _resolve_reseller,
    _targets,
    dotenv_path,
    smartbox,
)


@smartbox.command(help="Resolve a controller profile from identity facts")
@click.option("--node-type", default="htr", show_default=True, help="Wire node type.")
@click.option(
    "--pid",
    required=True,
    help="Wire product id: version.pid (code = pid[2:4]) or a bare 2-character code.",
)
@click.option("--fw-version", default=None, help="Firmware version, when known.")
@click.option(
    "--setup",
    "setup_json",
    default="{}",
    show_default=True,
    help="Node setup JSON object (factory/extra options included).",
)
@click.option(
    "--max-limit-c",
    type=float,
    default=None,
    help="Override the setup max_stemp_limit in Celsius (0 = unclamped catalogue).",
)
def profile(
    node_type: str,
    pid: str,
    fw_version: str | None,
    setup_json: str,
    max_limit_c: float | None,
) -> None:
    """Print the neutral controller profile without contacting the API."""
    setup = _load_json_object(setup_json, "--setup")
    code = product_code(pid)
    if code is None and len(pid.strip()) == 2:
        code = pid.strip().upper()
        pid = f"00{code}0000"
    if code not in PRODUCT_CODES:
        raise click.BadParameter(
            "expected a 4+ character version.pid (code = pid[2:4]) or a 2-character code",
            param_hint="--pid",
        )
    if max_limit_c is None:
        resolved = resolve_stored_profile(node_type, pid, fw_version, setup)
    else:
        resolved = resolve_profile(
            node_type,
            pid,
            fw_version,
            setup,
            max_limit_c=max_limit_c,
        )
    _pretty_print(asdict(resolved))


def _node_profile_payload(
    device: dict[str, Any],
    dev_id: str,
    node: dict[str, Any],
    version: dict[str, Any],
    setup: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    pid = version.get("pid")
    fw_version = version.get("fw_version")
    node_type = str(node.get("type"))
    profile = resolve_stored_profile(
        node_type,
        pid if isinstance(pid, str) else None,
        fw_version if isinstance(fw_version, str) else None,
        setup,
    )
    info = code_info(pid if isinstance(pid, str) else None)
    issues: list[str] = []
    if not pid:
        issues.append("version.pid missing")
    elif info is None:
        issues.append(f"product code {(product_code(pid) or '?'):>2} is not reviewed")
    if not fw_version:
        issues.append("fw_version missing")
    units = status.get("units")
    if units not in ("C", "F"):
        issues.append("status.units missing or invalid")
    view = SetupView(setup)
    if "max_stemp_limit" in view and view.max_stemp_limit is None:
        issues.append("max_stemp_limit is the disabled sentinel")
    return {
        "device": {"dev_id": dev_id, "name": device.get("name")},
        "node": {
            "type": node_type,
            "addr": node.get("addr"),
            "name": node.get("name"),
        },
        "version": version,
        "product": asdict(info) if info is not None else None,
        "profile": asdict(profile),
        "status": {
            "units": units,
            "mode": status.get("mode"),
            "sync_status": status.get("sync_status"),
            "boost": status.get("boost"),
            "active": status.get("active"),
        },
        "issues": issues,
    }


async def _profile_payload_for(
    client: SmartboxClient,
    device: dict[str, Any],
    dev_id: str,
    node: dict[str, Any],
    node_ref: NodeRef,
) -> dict[str, Any]:
    version = await client.get_node_version(dev_id, node_ref)
    setup = await client.get_node_setup(dev_id, node_ref)
    status = await client.get_node_status(dev_id, node_ref)
    return _node_profile_payload(device, dev_id, node, version, setup, status)


@smartbox.command(help="Resolve the live profile of one node")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.pass_context
async def node_profile(ctx: click.Context, device_id: str, node_addr: str) -> None:
    client = await _client(ctx)
    device = await _find_device(client, device_id)
    real_id, node_ref, node = await _find_node_info(
        client, device_id, node_addr, device
    )
    _pretty_print(await _profile_payload_for(client, device, real_id, node, node_ref))


@smartbox.command(help="Summarize every node's profile and discovery state")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def diagnose(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    client = await _client(ctx)
    if device_id is not None:
        device = await _find_device(client, device_id)
        device_id = str(device["dev_id"])
    payloads: list[dict[str, Any]] = []
    async for device, dev_id, node, node_ref in _targets(client, device_id, node_addr):
        payload = await _profile_payload_for(client, device, dev_id, node, node_ref)
        payloads.append(payload)
        if ctx.obj["json"]:
            continue
        profile = payload["profile"]
        limits = profile["limits"]
        product = payload["product"]
        label = product["label"] if product else "unknown"
        click.echo(
            f"{dev_id}/{node.get('addr')} {node.get('name')}: "
            f"{profile['code'] or '?'} {label} | "
            f"{product['family'] if product else '?'}/"
            f"{product['actuation'] if product else '?'} | "
            f"limits {limits['min_c']}-{limits['max_c']} "
            f"{','.join(limits['profiles'])} | "
            f"climate {profile['climate_bounds'][0]}-{profile['climate_bounds'][1]} | "
            f"sync {payload['status'].get('sync_status') or '?'}"
        )
        for issue in payload["issues"]:
            click.echo(f"  issue: {issue}")
    if not payloads:
        raise click.ClickException("no nodes matched the device/node filter")
    if ctx.obj["json"]:
        _pretty_print(payloads)


@smartbox.command(help="Show node samples (power/temperature history)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option(
    "-s",
    "--start-time",
    help="Unix seconds or ISO-8601; default one hour ago.",
)
@click.option(
    "-e",
    "--end-time",
    help="Unix seconds or ISO-8601; default one hour from now.",
)
@click.option("--csv", "as_csv", is_flag=True, help="Print the rows as CSV.")
@click.pass_context
async def node_samples(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    start_time: str | None,
    end_time: str | None,
    as_csv: bool,
) -> None:
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    now = int(time.time())
    rows = await client.get_node_samples(
        device_id,
        node,
        _parse_time(start_time, now - 3600),
        _parse_time(end_time, now + 3600),
    )
    if not as_csv:
        _pretty_print(rows)
        return
    if not rows:
        return
    fields = sorted({key for row in rows for key in row})
    writer = csv.DictWriter(sys.stdout, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)


def _code_rows(
    *,
    family: str | None = None,
    limits_family: str | None = None,
    surface: str | None = None,
    relay: bool = False,
    thm: bool = False,
    search: str | None = None,
) -> list[ProductCode]:
    rows = []
    needle = search.lower() if search else None
    for info in PRODUCT_CODES.values():
        if family is not None and info.family != family:
            continue
        if limits_family is not None and info.limits_family != limits_family:
            continue
        if surface is not None and info.surface != surface:
            continue
        if relay and info.actuation != "RELAY":
            continue
        if thm and not info.thm_heater:
            continue
        if needle is not None and needle not in f"{info.code} {info.label}".lower():
            continue
        rows.append(info)
    return rows


@smartbox.command(name="codes", help="List the reviewed product-code table")
@click.option(
    "--family", type=click.Choice(sorted({i.family for i in PRODUCT_CODES.values()}))
)
@click.option(
    "--limits-family",
    type=click.Choice(sorted({i.limits_family for i in PRODUCT_CODES.values()})),
)
@click.option(
    "--surface", type=click.Choice(sorted({i.surface for i in PRODUCT_CODES.values()}))
)
@click.option("--relay", is_flag=True, help="Only relay-actuated products.")
@click.option("--thm", "thm_heater", is_flag=True, help="Only THM heater products.")
@click.option("--search", help="Match the code or label.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
async def codes(
    ctx: click.Context,
    family: str | None,
    limits_family: str | None,
    surface: str | None,
    relay: bool,
    thm_heater: bool,
    search: str | None,
    as_json: bool,
) -> None:
    rows = _code_rows(
        family=family,
        limits_family=limits_family,
        surface=surface,
        relay=relay,
        thm=thm_heater,
        search=search,
    )
    if as_json or ctx.obj["json"]:
        _pretty_print([asdict(row) for row in rows])
        return
    header = (
        f"{'code':4} {'family':10} {'actuation':11} {'limits':14} {'surface':10} label"
    )
    click.echo(header)
    click.echo("-" * len(header))
    click.echo(
        "flags: L=lhz_duty T=thm_heater F=fixed_time_boost W=water S=stepped_charge"
    )
    for row in rows:
        flags = "".join(
            flag
            for flag, enabled in (
                ("L", row.lhz_duty),
                ("T", row.thm_heater),
                ("F", row.fixed_time_boost),
                ("W", row.water),
                ("S", row.stepped_charge),
            )
            if enabled
        )
        click.echo(
            f"{row.code:4} {row.family:10} {row.actuation:11} "
            f"{row.limits_family:14} {row.surface:10} {row.label}"
            + (f"  [{flags}]" if flags else "")
        )


@smartbox.command(
    name="profiles",
    hidden=True,
    help="Renamed to `codes`; kept only to explain the move.",
)
@click.option("--json", "as_json", is_flag=True, hidden=True)
def _profiles_alias(as_json: bool) -> None:  # noqa: ARG001
    """Give upgrading users the renamed command."""
    raise click.ClickException("`profiles` was renamed to `codes`")


@smartbox.command(help="Show the library version")
def version() -> None:
    click.echo(f"smartbox {__version__}")


@smartbox.command(help="Show CLI configuration and optional API checks")
@click.option(
    "--check", is_flag=True, help="Call health-check, api-version and devices."
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.pass_context
async def doctor(ctx: click.Context, check: bool, as_json: bool) -> None:
    reseller, base_url = _resolve_reseller(ctx)
    username = ctx.obj.get("username")
    cache_path = ctx.obj.get("token_cache_path")
    if cache_path is None and username:
        cache_path = str(
            default_cache_path(
                ctx.obj["api_name"], ctx.obj["serial_id"], username, base_url
            )
        )
    payload: dict[str, Any] = {
        "version": __version__,
        "api_name": ctx.obj["api_name"],
        "reseller": reseller.name,
        "base_url": base_url,
        "serial_id": ctx.obj["serial_id"],
        "username": _mask_username(username),
        "dotenv": str(dotenv_path()) if dotenv_path() is not None else None,
        "token_cache": cache_path,
        "token_cache_exists": bool(
            cache_path and await asyncio.to_thread(Path(cache_path).is_file)
        ),
    }
    if check:
        client = await _client(ctx)
        payload["health"] = await client.health_check()
        payload["api_version"] = await client.get_api_version()
        payload["devices"] = len(_dev_list(await client.get_devices()))
    if as_json or ctx.obj["json"]:
        _pretty_print(payload)
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            click.echo(f"{key}:")
            _pretty_print(value)
        else:
            click.echo(f"{key}: {value}")


smartbox.add_command(doctor, "config")


@smartbox.command(help="Get the API health status")
@click.pass_context
async def health_check(ctx: click.Context) -> None:
    _pretty_print(await (await _client(ctx)).health_check())


@smartbox.command(help="Get the API version")
@click.pass_context
async def api_version(ctx: click.Context) -> None:
    _pretty_print(await (await _client(ctx)).get_api_version())


@smartbox.command(help="List known resellers and their connection details")
def resellers() -> None:
    rows = list(AvailableResellers.resellers.items())
    key_w = max(len("key"), *(len(key) for key, _ in rows))
    name_w = max(len("brand"), *(len(r.name) for _, r in rows))
    brand_w = max(len("brand key"), *(len(r.api_url) for _, r in rows))
    host_w = max(len("API host"), *(len(r.api_host) for _, r in rows))
    serial_w = max(len("serial"), *(len(str(r.serial_id)) for _, r in rows))
    basic_w = len("generic")
    header = (
        f"  {'key':<{key_w}}  {'brand':<{name_w}}  "
        f"{'brand key':<{brand_w}}  {'API host':<{host_w}}  "
        f"{'serial':>{serial_w}}  {'basic':<{basic_w}}  referer"
    )
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))
    for key, reseller in rows:
        basic = (
            "generic"
            if reseller.basic_auth == SMARTBOX_GENERIC_BASIC_AUTH
            else "custom"
        )
        click.echo(
            f"  {key:<{key_w}}  {reseller.name:<{name_w}}  "
            f"{reseller.api_url:<{brand_w}}  {reseller.api_host:<{host_w}}  "
            f"{reseller.serial_id:>{serial_w}}  {basic:<{basic_w}}  "
            f"{reseller.web_url or '-'}"
        )

    click.echo()
    click.echo(
        "API hosts are HTTPS. Basic is 'generic' for the shared app credential "
        "or 'custom' for a tenant-specific one; the credential value is not "
        "printed."
    )


@smartbox.command(help="Show the guests of a home")
@click.option("-h", "--home-id", required=True)
@click.pass_context
async def guests(ctx: click.Context, home_id: str) -> None:
    _pretty_print(await (await _client(ctx)).get_home_guests(home_id))


@smartbox.command(help="Follow the shared ws_user feed")
@click.option("-d", "--device-id", help="only print updates for this device")
@click.option("-n", "--node-addr", help="only print updates for this node address")
@click.option("--path", help="only print updates whose path contains this text")
@click.pass_context
async def listen(
    ctx: click.Context,
    device_id: str | None,
    node_addr: str | None,
    path: str | None,
) -> None:
    client = await _client(ctx)
    if device_id is not None:
        device = await _find_device(client, device_id)
        device_id = str(device["dev_id"])
    if node_addr is not None:
        nodes = await client.get_nodes(device_id) if device_id else []
        if device_id and not any(
            str(node.get("addr")) == str(node_addr) for node in nodes
        ):
            raise click.ClickException(
                f"node {node_addr!r} not found on device {device_id!r}"
            )

    async def on_event(event: Event) -> None:
        if isinstance(event, Snapshot):
            click.echo("snapshot")
            _pretty_print(event.payload)
        elif isinstance(event, Update):
            if device_id is not None and event.dev_id != device_id:
                return
            if node_addr is not None and f"/{node_addr}/" not in f"/{event.path}/":
                return
            if path is not None and path not in event.path:
                return
            sync = (
                event.body.get("sync_status") if isinstance(event.body, dict) else None
            )
            click.echo(f"update {event.dev_id} {event.path} sync={sync or '?'}")
            _pretty_print(event.body)
        elif isinstance(event, SocketClosed):
            click.echo(f"closed code={event.code} expected={event.expected}")

    transport = WsUserTransport(
        ManagedTokenSource(client.auth), ctx.obj["api_base_url"]
    )
    transport.subscribe(on_event)
    try:
        await transport.run()
    except asyncio.CancelledError:
        pass
    finally:
        await transport.close()
        await client.close()
