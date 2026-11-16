"""Command line interaction with the Smartbox API.

The CLI is a debugging and troubleshooting tool: it prints the raw wire
payloads, exercises the named writes and can follow the ws_user feed. It is
deliberately separate from the Home Assistant integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

try:
    import asyncclick as click
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by users only
    raise SystemExit(
        "The smartbox CLI needs the optional dependency: pip install 'smartbox[cli]'"
    ) from exc

from .protocol import (
    AuthConfig,
    AvailableResellers,
    ManagedTokenSource,
    NodeRef,
    SmartboxClient,
    SmartboxReseller,
    WriteApi,
    WsUserTransport,
    api_base_url,
)
from .protocol.errors import ResellerNotExistError, SmartboxError
from .protocol.events import Event, Snapshot, SocketClosed, Update
from .protocol.reseller import SMARTBOX_GENERIC_BASIC_AUTH
from .token_cache import FileTokenCache, account_stamp, default_cache_path

_LOGGER = logging.getLogger(__name__)


def _pretty_print(data: object) -> None:
    """Pretty print a raw wire value."""
    if isinstance(data, (dict, list)):
        click.echo(json.dumps(data, indent=4, sort_keys=True, default=str))
    else:
        click.echo(repr(data))


def _api_base_url(reseller: SmartboxReseller, override: str | None) -> str:
    """Return the reseller base URL, or a normalized --api-host override."""
    if not override:
        return api_base_url(reseller)
    override = override.rstrip("/")
    return override if "://" in override else f"https://{override}"


def _find_dotenv() -> Path | None:
    """Return the nearest ``.env`` file, searching upward from the CWD."""
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _load_env_file(path: Path) -> None:
    """Load simple ``KEY=VALUE`` lines without overriding the environment."""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_dotenv() -> None:
    path = _find_dotenv()
    if path is not None:
        _load_env_file(path)


def _dev_list(payload: object) -> list[dict[str, Any]]:
    """Accept the raw ``{devs: [...]}`` envelope or a bare list."""
    if isinstance(payload, dict):
        devs = payload.get("devs")
        if isinstance(devs, list):
            return [entry for entry in devs if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


async def _client(ctx: click.Context) -> SmartboxClient:
    """Return this invocation's client, constructing it on first use."""
    if "client" in ctx.obj:
        return ctx.obj["client"]
    username = ctx.obj["username"]
    password = ctx.obj["password"]
    if not username or not password:
        raise click.ClickException(
            "username and password are required (or SMARTBOX_USERNAME / "
            "SMARTBOX_PASSWORD)"
        )
    api_name = ctx.obj["api_name"]
    try:
        reseller = AvailableResellers(
            api_name,
            basic_auth=ctx.obj["basic_auth_creds"],
            web_url=ctx.obj["x_referer"],
            serial_id=ctx.obj["x_serial_id"],
            name=api_name,
        ).reseller
    except ResellerNotExistError as err:
        raise click.ClickException(str(err)) from err
    base_url = _api_base_url(reseller, ctx.obj["api_host"])
    ctx.obj["api_base_url"] = base_url
    config = AuthConfig(
        api_host=base_url,
        basic_auth_creds=ctx.obj["basic_auth_creds"]
        or reseller.basic_auth
        or SMARTBOX_GENERIC_BASIC_AUTH,
        serial_id=str(
            ctx.obj["x_serial_id"]
            if ctx.obj["x_serial_id"] is not None
            else reseller.serial_id
        ),
        username=username,
        password=password,
        referer=ctx.obj["x_referer"] or reseller.web_url or None,
    )
    cache: FileTokenCache | None = None
    stored = None
    cache_enabled = bool(ctx.obj["token_cache"])
    if cache_enabled or ctx.obj["forget_tokens"]:
        cache_path = (
            Path(ctx.obj["token_cache_path"])
            if ctx.obj["token_cache_path"]
            else default_cache_path(api_name, config.serial_id, username, base_url)
        )
        cache = FileTokenCache(
            cache_path,
            account=account_stamp(api_name, config.serial_id, username, base_url),
        )
    if ctx.obj["forget_tokens"] and cache is not None and not await cache.async_clear():
        click.echo(f"warning: could not remove {cache.path}", err=True)
    if cache_enabled and cache is not None:
        stored = await cache.async_load()
        if stored is not None:
            _LOGGER.debug(
                "Resuming the cached session; the password grant is skipped "
                "(use --forget-tokens to force a login)."
            )
    client = SmartboxClient(
        config,
        initial_tokens=stored,
        on_grant=cache.async_save if cache_enabled and cache is not None else None,
    )
    ctx.obj["client"] = client
    return client


async def _find_device(client: SmartboxClient, ref: str) -> dict[str, Any]:
    devices = _dev_list(await client.get_devices())
    for device in devices:
        if ref in (device.get("dev_id"), device.get("name")):
            return device
    available = ", ".join(
        f"{device.get('name')} ({device.get('dev_id')})" for device in devices
    )
    raise click.ClickException(
        f"device {ref!r} not found; available: {available or 'none'}"
    )


async def _find_node(
    client: SmartboxClient, dev_id: str, addr: str
) -> tuple[str, NodeRef]:
    device = await _find_device(client, dev_id)
    real_id = str(device["dev_id"])
    nodes = await client.get_nodes(real_id)
    for node in nodes:
        if str(node.get("addr")) == str(addr):
            return real_id, NodeRef(node_type=str(node["type"]), addr=str(node["addr"]))
    available = ", ".join(f"{node.get('type')}/{node.get('addr')}" for node in nodes)
    raise click.ClickException(
        f"node {addr!r} not found on device {real_id!r}; "
        f"available: {available or 'none'}"
    )


class _SmartboxGroup(click.Group):
    """Close the lazily created client on success and on command errors."""

    async def invoke(self, ctx: click.Context) -> Any:
        try:
            return await super().invoke(ctx)
        finally:
            client: SmartboxClient | None = ctx.obj.get("client") if ctx.obj else None
            if client is not None:
                await client.close()


@click.group(cls=_SmartboxGroup, chain=True)
@click.option("-a", "--api-name", envvar="SMARTBOX_API_NAME", default="api")
@click.option("-b", "--basic-auth-creds", envvar="SMARTBOX_BASIC_AUTH_CREDS")
@click.option("-u", "--username", envvar="SMARTBOX_USERNAME")
@click.option("-p", "--password", envvar="SMARTBOX_PASSWORD", hide_input=True)
@click.option("-r", "--x-referer", envvar="SMARTBOX_X_REFERER")
@click.option("-i", "--x-serial-id", envvar="SMARTBOX_X_SERIAL_ID", type=int)
@click.option("--api-host", envvar="SMARTBOX_API_HOST")
@click.option(
    "--token-cache/--no-token-cache",
    default=True,
    help="Persist the refresh token between invocations.",
)
@click.option(
    "--token-cache-path",
    envvar="SMARTBOX_TOKEN_CACHE",
    help="Per-account cache file under $XDG_CACHE_HOME/smartbox (default).",
)
@click.option(
    "--forget-tokens",
    is_flag=True,
    help=(
        "Delete the cached session before running. The new grant is cached "
        "again unless --no-token-cache is also given."
    ),
)
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
async def smartbox(
    ctx: click.Context,
    api_name: str,
    basic_auth_creds: str | None,
    username: str | None,
    password: str | None,
    x_referer: str | None,
    x_serial_id: int | None,
    api_host: str | None,
    token_cache: bool,
    token_cache_path: str | None,
    forget_tokens: bool,
    verbose: bool,
) -> None:
    """Smartbox API command line."""
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
    )
    ctx.obj = {
        "api_name": api_name,
        "basic_auth_creds": basic_auth_creds,
        "username": username,
        "password": password,
        "x_referer": x_referer,
        "x_serial_id": x_serial_id,
        "api_host": api_host,
        "token_cache": token_cache,
        "token_cache_path": token_cache_path,
        "forget_tokens": forget_tokens,
        "verbose": verbose,
    }


@smartbox.command(help="Show devices")
@click.pass_context
async def devices(ctx: click.Context) -> None:
    _pretty_print(await (await _client(ctx)).get_devices())


@smartbox.command(help="Show homes")
@click.pass_context
async def homes(ctx: click.Context) -> None:
    _pretty_print(await (await _client(ctx)).get_grouped_devices())


def _mask_identifier(value: object, keep: int = 8) -> str:
    """Shorten a session identifier so a row stays readable without the full sid."""
    text = str(value or "")
    return text if len(text) <= keep else text[:keep] + "…"


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
    if as_json:
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
@click.pass_context
async def nodes(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        _pretty_print(await client.get_nodes(device["dev_id"]))


@smartbox.command(help="Show node status")
@click.pass_context
async def status(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        for node in await client.get_nodes(device["dev_id"]):
            click.echo(f"{node.get('name')} (addr: {node.get('addr')})")
            _pretty_print(await client.get_node_status(device["dev_id"], _node(node)))


def _node(node: dict[str, Any]) -> NodeRef:
    return NodeRef(node_type=str(node["type"]), addr=str(node["addr"]))


@smartbox.command(help="Show node setup")
@click.pass_context
async def setup(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        for node in await client.get_nodes(device["dev_id"]):
            click.echo(f"{node.get('name')} (addr: {node.get('addr')})")
            _pretty_print(await client.get_node_setup(device["dev_id"], _node(node)))


@smartbox.command(help="Show node prog")
@click.pass_context
async def prog(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        for node in await client.get_nodes(device["dev_id"]):
            click.echo(f"{node.get('name')} (addr: {node.get('addr')})")
            _pretty_print(await client.get_node_prog(device["dev_id"], _node(node)))


@smartbox.command(help="Show node samples (power/temperature history)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("-s", "--start-time", type=int)
@click.option("-e", "--end-time", type=int)
@click.pass_context
async def node_samples(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    start_time: int | None,
    end_time: int | None,
) -> None:
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    now = int(time.time())
    _pretty_print(
        await client.get_node_samples(
            device_id, node, start_time or now - 3600, end_time or now + 3600
        )
    )


@smartbox.command(help="Set node status")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--locked", type=bool)
@click.option("--mode")
@click.option("--stemp", type=float)
@click.option("--units")
@click.option("--on", type=bool)
@click.option("--selected-temp")
@click.option("--comfort-temp", type=float)
@click.option("--eco-offset")
@click.pass_context
async def set_status(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    locked: bool | None,
    mode: str | None,
    stemp: float | None,
    units: str | None,
    on: bool | None,
    selected_temp: str | None,
    comfort_temp: float | None,
    eco_offset: str | None,
) -> None:
    client = await _client(ctx)
    writes = WriteApi(client)
    device_id, node = await _find_node(client, device_id, node_addr)
    htr_mod = any(
        value is not None for value in (on, selected_temp, comfort_temp, eco_offset)
    )
    results: list[object] = []
    if htr_mod:
        results.append(
            await writes.set_htr_mod_status(
                device_id,
                node,
                on=on,
                mode=mode,
                selected_temp=selected_temp,
                comfort_temp=comfort_temp,
                eco_offset=eco_offset,
                units=units,
            )
        )
    else:
        if locked is not None:
            results.append(await writes.set_lock(device_id, node, locked))
        if mode is not None:
            results.append(await writes.set_mode(device_id, node, mode))
        if stemp is not None:
            if units is None:
                raise click.ClickException("must supply --units with --stemp")
            results.append(await writes.set_setpoint(device_id, node, stemp, units))
        if not results:
            raise click.ClickException(
                "nothing to set: use --locked, --mode, --stemp or an htr_mod field"
            )
    for result in results:
        _pretty_print(result)


@smartbox.command(help="Set node setup options")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--control-mode", type=int)
@click.option("--offset", type=float)
@click.option("--priority")
@click.option("--true-radiant-enabled", type=bool)
@click.option("--units")
@click.option("--window-mode-enabled", type=bool)
@click.option("--extra-json", help="JSON object merged into extra_options")
@click.pass_context
async def set_setup(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    control_mode: int | None,
    offset: float | None,
    priority: str | None,
    true_radiant_enabled: bool | None,
    units: str | None,
    window_mode_enabled: bool | None,
    extra_json: str | None,
) -> None:
    client = await _client(ctx)
    writes = WriteApi(client)
    device_id, node = await _find_node(client, device_id, node_addr)
    results: list[object] = []
    if control_mode is not None:
        results.append(
            await writes.set_setup_value(device_id, node, "control_mode", control_mode)
        )
    if offset is not None:
        results.append(await writes.set_away_offset(device_id, node, offset))
    if priority is not None:
        results.append(await writes.set_priority(device_id, node, priority))
    if true_radiant_enabled is not None:
        results.append(
            await writes.set_true_radiant(device_id, node, true_radiant_enabled)
        )
    if units is not None:
        results.append(await writes.set_setup_value(device_id, node, "units", units))
    if window_mode_enabled is not None:
        results.append(
            await writes.set_window_mode(device_id, node, window_mode_enabled)
        )
    if extra_json is not None:
        try:
            extra = json.loads(extra_json)
        except json.JSONDecodeError as err:
            raise click.ClickException(f"invalid extra JSON: {err}") from err
        if not isinstance(extra, dict):
            raise click.ClickException("--extra-json must be a JSON object")
        results.append(await writes.set_extra_options(device_id, node, extra))
    if not results:
        raise click.ClickException("nothing to set")
    for result in results:
        _pretty_print(result)


@smartbox.command(help='Set node prog from JSON, e.g. \'{"prog": {"0": [2, ...]}}\'')
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("prog-json")
@click.pass_context
async def set_prog(
    ctx: click.Context, device_id: str, node_addr: str, prog_json: str
) -> None:
    client = await _client(ctx)
    writes = WriteApi(client)
    device_id, node = await _find_node(client, device_id, node_addr)
    try:
        parsed = json.loads(prog_json)
    except json.JSONDecodeError as err:
        raise click.ClickException(f"invalid prog JSON: {err}") from err
    days = parsed.get("prog") if isinstance(parsed, dict) else None
    if not isinstance(days, dict):
        raise click.ClickException(
            'prog payload must be a JSON object with a "prog" object'
        )
    _pretty_print(await writes.set_prog(device_id, node, days))


@smartbox.command(help="Show device away_status")
@click.pass_context
async def device_away_status(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        _pretty_print(await client.get_away_status(device["dev_id"]))


@smartbox.command(help="Show device connected status")
@click.pass_context
async def device_connected_status(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        _pretty_print(await client.get_connected(device["dev_id"]))


@smartbox.command(help="Show device power_limit")
@click.pass_context
async def device_power_limit(ctx: click.Context) -> None:
    client = await _client(ctx)
    for device in _dev_list(await client.get_devices()):
        click.echo(f"{device.get('name')} (dev_id: {device.get('dev_id')})")
        _pretty_print(await client.get_power_limit(device["dev_id"]))


@smartbox.command(help="Set device away_status")
@click.option("-d", "--device-id", required=True)
@click.option("--away/--no-away", default=None)
@click.pass_context
async def set_device_away_status(
    ctx: click.Context, device_id: str, away: bool | None
) -> None:
    if away is None:
        raise click.ClickException("use --away or --no-away")
    client = await _client(ctx)
    device = await _find_device(client, device_id)
    _pretty_print(await WriteApi(client).set_away(device["dev_id"], away))


@smartbox.command(help="Set device power_limit")
@click.option("-d", "--device-id", required=True)
@click.argument("power-limit", type=int)
@click.pass_context
async def set_device_power_limit(
    ctx: click.Context, device_id: str, power_limit: int
) -> None:
    client = await _client(ctx)
    device = await _find_device(client, device_id)
    _pretty_print(await WriteApi(client).set_power_limit(device["dev_id"], power_limit))


@smartbox.command(help="Set node power_limit")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("power-limit", type=int)
@click.pass_context
async def set_node_power_limit(
    ctx: click.Context, device_id: str, node_addr: str, power_limit: int
) -> None:
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    _pretty_print(
        await WriteApi(client).set_node_power_limit(device_id, node, power_limit)
    )


@smartbox.command(help="Turn node boost on or off")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--on/--off", default=None)
@click.pass_context
async def set_boost(
    ctx: click.Context, device_id: str, node_addr: str, on: bool | None
) -> None:
    if on is None:
        raise click.ClickException("use --on or --off")
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    _pretty_print(await WriteApi(client).set_boost(device_id, node, on))


@smartbox.command(help="Set full-boost mode (active, mode, temperature, time)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--active/--inactive", default=None)
@click.option("--mode")
@click.option("--temperature", type=float)
@click.option("--time-minutes", type=int)
@click.pass_context
async def set_boost_mode(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    active: bool | None,
    mode: str | None,
    temperature: float | None,
    time_minutes: int | None,
) -> None:
    if active is None:
        raise click.ClickException("use --active or --inactive")
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    _pretty_print(
        await WriteApi(client).set_boost_mode(
            device_id,
            node,
            active=active,
            mode=mode,
            temperature=temperature,
            time_minutes=time_minutes,
        )
    )


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
@click.pass_context
async def listen(ctx: click.Context, device_id: str | None) -> None:
    client = await _client(ctx)

    async def on_event(event: Event) -> None:
        if isinstance(event, Snapshot):
            click.echo("snapshot")
            _pretty_print(event.payload)
        elif isinstance(event, Update):
            if device_id is not None and event.dev_id != device_id:
                return
            click.echo(f"update {event.dev_id} {event.path}")
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


def cli() -> None:
    """Console-script entry point: load ``.env`` then run the CLI."""
    _load_dotenv()
    try:
        smartbox()
    except SmartboxError as err:
        raise SystemExit(f"Error: {err}") from err


if __name__ == "__main__":
    cli()
