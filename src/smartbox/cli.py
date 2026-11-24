"""Command line interaction with the Smartbox API.

The CLI is a debugging and troubleshooting tool: it prints the raw wire
payloads, exercises the named writes and can follow the ws_user feed. It is
deliberately separate from the Home Assistant integration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import csv
from dataclasses import asdict
from datetime import UTC, datetime
import json
import logging
import os
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

from . import __version__
from .device.capabilities import (
    Check,
    NodeFacts,
    Reason,
    check_boost,
    check_boost_mode,
    check_charging_schedule,
    check_control_mode,
    check_easy,
    check_htr_mod_status,
    check_legacy_boost,
    check_max_stemp_limit,
    check_node_power_limit,
    check_prog_temps,
    check_runback,
    check_storage_lock,
    check_storage_mode,
    check_storage_ui_mode,
    check_target_charge,
    check_thermostat_prog,
)
from .device.codes import PRODUCT_CODES, ProductCode, code_info, product_code
from .device.control_modes import control_mode_choices, control_mode_label
from .device.profile import resolve_profile, resolve_stored_profile
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
from .protocol.writes import VALID_BOOST_MODES, VALID_MODES, VALID_PRIORITIES
from .token_cache import FileTokenCache, account_stamp, default_cache_path

_LOGGER = logging.getLogger(__name__)


def _pretty_print(data: object) -> None:
    """Pretty print a raw wire value."""
    if isinstance(data, (dict, list)):
        click.echo(json.dumps(data, indent=4, sort_keys=True, default=str))
    else:
        click.echo(repr(data))


def _load_json_arg(value: str, label: str) -> object:
    """Parse a JSON argument, with ``@path`` / ``@-`` file input."""
    if value.startswith("@"):
        source = value[1:]
        try:
            text = (
                sys.stdin.read()
                if source == "-"
                else Path(source).read_text(encoding="utf-8")
            )
        except OSError as ex:
            raise click.BadParameter(
                f"cannot read {source}: {ex}", param_hint=label
            ) from ex
    else:
        text = value
    try:
        return json.loads(text)
    except json.JSONDecodeError as ex:
        raise click.BadParameter(f"invalid JSON: {ex}", param_hint=label) from ex


def _load_json_object(value: str, label: str) -> dict[str, Any]:
    parsed = _load_json_arg(value, label)
    if not isinstance(parsed, dict):
        raise click.BadParameter("must be a JSON object", param_hint=label)
    return parsed


def _parse_time(value: str | None, default: int) -> int:
    """Parse a Unix timestamp or an ISO-8601 value; None uses the default."""
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as ex:
        raise click.BadParameter(
            f"expected Unix seconds or ISO-8601: {value!r}"
        ) from ex
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _parse_days(value: str) -> list[int]:
    """Parse ``1110111`` or ``1,1,1,0,1,1,1`` into a 0/1 list."""
    text = value.replace(",", "").replace(" ", "")
    if len(text) != 7 or set(text) - {"0", "1"}:
        raise click.BadParameter("expected seven 0/1 entries")
    return [int(day) for day in text]


def _parse_minutes(value: str) -> int:
    try:
        if ":" in value:
            hours, minutes = value.split(":", 1)
            return int(hours) * 60 + int(minutes)
        return int(value)
    except ValueError as ex:
        raise click.BadParameter(f"expected HH:MM or minutes: {value!r}") from ex


def _parse_slot(value: str) -> dict[str, int]:
    start, sep, end = value.partition("-")
    if not sep:
        raise click.BadParameter("expected START-END")
    return {"start": _parse_minutes(start), "end": _parse_minutes(end)}


def _mask_username(value: str | None) -> str:
    if not value:
        return "-"
    local, sep, domain = value.partition("@")
    if not sep:
        return _mask_identifier(value, 4)
    return f"{local[:2]}…@{domain}" if len(local) > 2 else f"{local}…@{domain}"


class _DryRunHost:
    """A WriteHost that records POSTs instead of sending them."""

    def __init__(self, client: SmartboxClient) -> None:
        self.client = client
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, body: Mapping[str, object]) -> None:
        self.posts.append((path, dict(body)))

    async def get_node_status(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_status(dev_id, node)

    async def get_node_setup(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_setup(dev_id, node)

    async def get_node_prog(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_prog(dev_id, node)


def _write_api(ctx: click.Context, client: SmartboxClient) -> WriteApi:
    """Build a WriteApi, using a recording host under --dry-run."""
    host: Any = client
    if ctx.obj["dry_run"]:
        host = _DryRunHost(client)
    return WriteApi(host)


def _show_write(ctx: click.Context, result: tuple[str, dict[str, Any]]) -> None:
    path, body = result
    _pretty_print(
        {
            "dry_run": bool(ctx.obj["dry_run"]),
            "method": "POST",
            "path": path,
            "body": body,
        }
    )


def _confirm(ctx: click.Context, action: str) -> None:
    """Ask before an interactive write; --dry-run and --yes skip the prompt."""
    if ctx.obj["dry_run"] or ctx.obj["yes"]:
        return
    if sys.stdin.isatty() and not click.confirm(f"{action}?", default=False):
        raise click.Abort


def _product_name(pid: str | None) -> str:
    info = code_info(pid)
    code = product_code(pid) or "unknown"
    return f"product {code} ({info.label})" if info is not None else f"product {code}"


async def _node_facts(client: SmartboxClient, dev_id: str, node: NodeRef) -> NodeFacts:
    version = await client.get_node_version(dev_id, node)
    setup = await client.get_node_setup(dev_id, node)
    pid = version.get("pid") if isinstance(version, Mapping) else None
    fw_version = version.get("fw_version") if isinstance(version, Mapping) else None
    return NodeFacts(
        node_type=node.node_type,
        pid=pid if isinstance(pid, str) else None,
        fw_version=fw_version if isinstance(fw_version, str) else None,
        setup=setup if isinstance(setup, Mapping) else {},
    )


def _refusal_message(
    check: Check,
    facts: NodeFacts,
    node: NodeRef,
    requirement: str,
    unavailable: str | None = None,
) -> str:
    name = _product_name(facts.pid)
    if check.reason is Reason.VALUE_NOT_OFFERED:
        offered = check.detail.get("offered", [])
        parts: list[str] = []
        for item in offered if isinstance(offered, list) else []:
            if isinstance(item, Mapping):
                label = str(item.get("label"))
                value = item.get("value")
                parts.append(f"{value} = {label}" if str(value) != label else label)
            else:
                parts.append(str(item))
        labels = ", ".join(parts)
        return (
            f"{name} offers {labels}; request "
            f"{check.detail.get('requested')!r} is not valid"
        )
    if check.reason is Reason.CURRENT_MODE_HIDES_SETTING:
        return "the current factory ui_mode does not expose the UI-mode setting"
    if check.reason is Reason.WRONG_MODE:
        return "target charge is only writable by a classic-accumulator Elnur storage controller"
    if check.reason is Reason.NOT_AVAILABLE and unavailable is not None:
        return f"{name} {unavailable}"
    if check.reason is Reason.NOT_AVAILABLE:
        return f"{name} does not support this setting"
    return f"node {node.node_type}/{node.addr} is {name}; {requirement}"


def _require_check(
    check: Check,
    facts: NodeFacts,
    node: NodeRef,
    requirement: str,
    unavailable: str | None = None,
) -> None:
    if not check.ok:
        raise click.ClickException(
            _refusal_message(check, facts, node, requirement, unavailable)
        )


async def _iter_devices(
    client: SmartboxClient, device_ref: str | None = None
) -> AsyncIterator[tuple[dict[str, Any], str]]:
    """Yield (device, dev_id) for the owned and invited devices."""
    for device in _dev_list(await client.get_devices()):
        dev_id = device.get("dev_id")
        if dev_id is None:
            continue
        if device_ref is not None and device_ref not in (
            str(dev_id),
            device.get("name"),
        ):
            continue
        yield device, str(dev_id)


async def _targets(
    client: SmartboxClient,
    device_ref: str | None = None,
    node_addr: str | None = None,
) -> AsyncIterator[tuple[dict[str, Any], str, dict[str, Any], NodeRef]]:
    """Yield (device, dev_id, node_info, NodeRef), optionally filtered."""
    async for device, dev_id in _iter_devices(client, device_ref):
        for node in await client.get_nodes(dev_id):
            if node_addr is not None and str(node.get("addr")) != str(node_addr):
                continue
            if "type" not in node or "addr" not in node:
                continue
            yield device, dev_id, node, _node(node)


async def _read_nodes(
    ctx: click.Context,
    client: SmartboxClient,
    method: str,
    device_ref: str | None,
    node_addr: str | None,
) -> None:
    """Read one node method across the filtered targets."""
    payloads: dict[str, Any] = {}
    found = False
    async for device, dev_id, node, node_ref in _targets(client, device_ref, node_addr):
        found = True
        data = await getattr(client, method)(dev_id, node_ref)
        if ctx.obj["json"]:
            bucket = payloads.setdefault(
                dev_id, {"name": device.get("name"), "nodes": {}}
            )
            bucket["nodes"][str(node.get("addr"))] = data
        else:
            click.echo(
                f"{device.get('name')} ({dev_id}) "
                f"{node.get('name')} ({node.get('addr')})"
            )
            _pretty_print(data)
    if (device_ref or node_addr) and not found:
        raise click.ClickException(
            f"no nodes matched device={device_ref!r} node={node_addr!r}"
        )
    if ctx.obj["json"]:
        _pretty_print(payloads)


async def _read_devices(
    ctx: click.Context,
    client: SmartboxClient,
    method: str,
    device_ref: str | None,
) -> None:
    payloads: dict[str, Any] = {}
    found = False
    async for device, dev_id in _iter_devices(client, device_ref):
        found = True
        data = await getattr(client, method)(dev_id)
        if ctx.obj["json"]:
            payloads[dev_id] = {"name": device.get("name"), "data": data}
        else:
            click.echo(f"{device.get('name')} ({dev_id})")
            _pretty_print(data)
    if device_ref and not found:
        raise click.ClickException(f"no device matched {device_ref!r}")
    if ctx.obj["json"]:
        _pretty_print(payloads)


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
    """Flatten the owned and invited devices in a ``devices`` response.

    The API returns ``{"devs": [...], "invited_to": [...]}``. An invited
    home's device is only under ``invited_to`` while ``devs`` stays empty, so
    the CLI commands must read both lists. Duplicate dev_ids (a device can
    appear in both) keep the first entry.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in ("devs", "invited_to"):
            values = payload.get(key)
            if isinstance(values, list):
                entries.extend(entry for entry in values if isinstance(entry, dict))
    elif isinstance(payload, list):
        entries.extend(entry for entry in payload if isinstance(entry, dict))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        dev_id = entry.get("dev_id")
        if dev_id is not None:
            key = str(dev_id)
            if key in seen:
                continue
            seen.add(key)
        unique.append(entry)
    return unique


def _resolve_reseller(ctx: click.Context) -> tuple[SmartboxReseller, str]:
    """Resolve the tenant and base URL, with no credential requirement."""
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
    ctx.obj["reseller_name"] = reseller.name
    ctx.obj["serial_id"] = str(
        ctx.obj["x_serial_id"]
        if ctx.obj["x_serial_id"] is not None
        else reseller.serial_id
    )
    return reseller, base_url


async def _client(ctx: click.Context) -> SmartboxClient:
    """Return this invocation's client, constructing it on first use."""
    if "client" in ctx.obj:
        return ctx.obj["client"]
    username = ctx.obj["username"]
    password = ctx.obj["password"]
    if not username or not password:
        raise click.ClickException(
            "username and password are required (or SMARTBOX_USERNAME / "
            "SMARTBOX_PASSWORD). Copy .env.example to .env and fill it in, "
            "or pass -u/-p."
        )
    reseller, base_url = _resolve_reseller(ctx)
    config = AuthConfig(
        api_host=base_url,
        basic_auth_creds=ctx.obj["basic_auth_creds"]
        or reseller.basic_auth
        or SMARTBOX_GENERIC_BASIC_AUTH,
        serial_id=ctx.obj["serial_id"],
        username=username,
        password=password,
        referer=ctx.obj["x_referer"] or reseller.web_url or None,
    )
    ctx.obj["serial_id"] = config.serial_id
    cache: FileTokenCache | None = None
    stored = None
    cache_enabled = bool(ctx.obj["token_cache"])
    if cache_enabled or ctx.obj["forget_tokens"]:
        cache_path = (
            Path(ctx.obj["token_cache_path"])
            if ctx.obj["token_cache_path"]
            else default_cache_path(
                ctx.obj["api_name"], config.serial_id, username, base_url
            )
        )
        cache = FileTokenCache(
            cache_path,
            account=account_stamp(
                ctx.obj["api_name"], config.serial_id, username, base_url
            ),
        )
        ctx.obj["token_cache_path"] = str(cache.path)
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


async def _find_node_info(
    client: SmartboxClient, dev_id: str, addr: str
) -> tuple[str, NodeRef, dict[str, Any]]:
    device = await _find_device(client, dev_id)
    real_id = str(device["dev_id"])
    nodes = await client.get_nodes(real_id)
    for node in nodes:
        if str(node.get("addr")) == str(addr):
            ref = NodeRef(node_type=str(node["type"]), addr=str(node["addr"]))
            return real_id, ref, node
    available = ", ".join(f"{node.get('type')}/{node.get('addr')}" for node in nodes)
    raise click.ClickException(
        f"node {addr!r} not found on device {real_id!r}; "
        f"available: {available or 'none'}"
    )


async def _find_node(
    client: SmartboxClient, dev_id: str, addr: str
) -> tuple[str, NodeRef]:
    real_id, ref, _node = await _find_node_info(client, dev_id, addr)
    return real_id, ref


class _SmartboxGroup(click.Group):
    """Close the lazily created client on success and on command errors."""

    async def invoke(self, ctx: click.Context) -> Any:
        try:
            return await super().invoke(ctx)
        except ValueError as ex:
            raise click.ClickException(str(ex)) from ex
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
    "--json",
    "as_json",
    is_flag=True,
    help="Print one JSON document for multi-node/device reads.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run the GET half of writes and print the POST without sending it.",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Do not ask before writes (non-interactive runs already skip the prompt).",
)
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
    as_json: bool,
    dry_run: bool,
    yes: bool,
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
        "json": as_json,
        "dry_run": dry_run,
        "yes": yes,
        "token_cache": token_cache,
        "token_cache_path": token_cache_path,
        "forget_tokens": forget_tokens,
        "verbose": verbose,
    }


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


def _node(node: dict[str, Any]) -> NodeRef:
    return NodeRef(node_type=str(node["type"]), addr=str(node["addr"]))


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
    dev_id, node, node_row = await _find_node_info(client, device_id, node_addr)
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
    current = _control_index(facts.setup.get("control_mode"))
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
    limit = setup.get("max_stemp_limit")
    if isinstance(limit, (int, float)) and limit <= 0:
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
    real_id, node_ref, node = await _find_node_info(client, device_id, node_addr)
    _pretty_print(await _profile_payload_for(client, device, real_id, node, node_ref))


@smartbox.command(help="Summarize every node's profile and discovery state")
@click.option("-d", "--device-id", help="Only this device id or name.")
@click.option("-n", "--node-addr", help="Only this node address.")
@click.pass_context
async def diagnose(
    ctx: click.Context, device_id: str | None, node_addr: str | None
) -> None:
    client = await _client(ctx)
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
            f"climate {profile['climate_bounds'][0]}-{profile['climate_bounds'][1]}"
        )
        for issue in payload["issues"]:
            click.echo(f"  issue: {issue}")
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


def _control_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _resolve_control_mode(mode: str, choices: tuple[tuple[str, int], ...]) -> int:
    raw = mode.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    matches = [
        (label, index) for label, index in choices if label.casefold() == raw.casefold()
    ]
    if not matches:
        matches = [
            (label, index)
            for label, index in choices
            if label.casefold().startswith(raw.casefold())
        ]
    if len(matches) == 1:
        return matches[0][1]
    offered = ", ".join(label for label, _index in choices)
    raise click.ClickException(f"control mode {mode!r} not found; choices: {offered}")


@smartbox.command(help="Set the node control mode (label or wire index)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option(
    "--mode",
    required=True,
    help="Controller label (e.g. 'PID (15 min. period)') or wire index; see control-modes.",
)
@click.pass_context
async def set_control_mode(
    ctx: click.Context, device_id: str, node_addr: str, mode: str
) -> None:
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_control_mode(facts),
        facts,
        node,
        "this node does not expose a control-mode setting",
        unavailable="does not expose a control-mode setting",
    )
    choices = tuple(control_mode_choices(facts.pid, facts.node_type, facts.setup) or ())
    value = _resolve_control_mode(mode, choices)
    _require_check(
        check_control_mode(facts, value),
        facts,
        node,
        "this control mode is not offered by the product",
    )
    resolved = control_mode_label(choices, value) or str(value)
    _confirm(
        ctx,
        f"set control mode {resolved} (wire index {value}) on {dev_id}/{node_addr}",
    )
    _show_write(
        ctx, await _write_api(ctx, client).set_control_mode(dev_id, node, value)
    )


@smartbox.command(help="Set node status")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--locked", type=bool)
@click.option("--mode", type=click.Choice(VALID_MODES))
@click.option("--stemp", type=float)
@click.option("--units", type=click.Choice(("C", "F")))
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
    writes = _write_api(ctx, client)
    device_id, node = await _find_node(client, device_id, node_addr)
    htr_mod = any(
        value is not None for value in (on, selected_temp, comfort_temp, eco_offset)
    )
    if htr_mod and any(value is not None for value in (locked, stemp)):
        raise click.ClickException(
            "htr_mod fields cannot be combined with --locked/--stemp; use set-htr-mod-status"
        )
    if not htr_mod and stemp is not None and units is None:
        raise click.ClickException("must supply --units with --stemp")
    if not htr_mod and units is not None and stemp is None:
        raise click.ClickException("--units is only used with --stemp")
    if all(
        value is None
        for value in (
            locked,
            mode,
            stemp,
            units,
            on,
            selected_temp,
            comfort_temp,
            eco_offset,
        )
    ):
        raise click.ClickException(
            "nothing to set: use --locked, --mode, --stemp or an htr_mod field"
        )
    if htr_mod:
        facts = await _node_facts(client, device_id, node)
        _require_check(
            check_htr_mod_status(facts),
            facts,
            node,
            "htr_mod fields need code 0A on any wire type, or FF on htr_mod",
        )
    _confirm(ctx, f"set node status on {device_id}/{node_addr}")
    results: list[tuple[str, dict[str, Any]]] = []
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
        if stemp is not None and units is not None:
            results.append(await writes.set_setpoint(device_id, node, stemp, units))
    for result in results:
        _show_write(ctx, result)


@smartbox.command(help="Set node setup options")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--control-mode", type=int, help="Wire index; use control-modes.")
@click.option("--offset", type=float)
@click.option("--priority", type=click.Choice(VALID_PRIORITIES))
@click.option("--true-radiant-enabled", type=bool)
@click.option("--units", type=click.Choice(("C", "F")))
@click.option("--window-mode-enabled", type=bool)
@click.option("--extra-json", help="JSON object (or @file) merged into extra_options")
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
    writes = _write_api(ctx, client)
    device_id, node = await _find_node(client, device_id, node_addr)
    extra = (
        _load_json_object(extra_json, "--extra-json")
        if extra_json is not None
        else None
    )
    results: list[tuple[str, dict[str, Any]]] = []
    if all(
        value is None
        for value in (
            control_mode,
            offset,
            priority,
            true_radiant_enabled,
            units,
            window_mode_enabled,
            extra_json,
        )
    ):
        raise click.ClickException("nothing to set")
    if offset is not None and offset < 0:
        raise click.ClickException("away offset must be >= 0")
    if control_mode is not None:
        facts = await _node_facts(client, device_id, node)
        _require_check(
            check_control_mode(facts, control_mode),
            facts,
            node,
            "this control mode is not offered by the product",
        )
    _confirm(ctx, f"set node setup on {device_id}/{node_addr}")
    if control_mode is not None:
        results.append(await writes.set_control_mode(device_id, node, control_mode))
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
    if extra is not None:
        results.append(await writes.set_extra_options(device_id, node, extra))
    for result in results:
        _show_write(ctx, result)


@smartbox.command(
    help='Set node prog from JSON, e.g. \'{"prog": {"0": [2, ...]}}\' or @file'
)
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("prog-json")
@click.pass_context
async def set_prog(
    ctx: click.Context, device_id: str, node_addr: str, prog_json: str
) -> None:
    client = await _client(ctx)
    writes = _write_api(ctx, client)
    device_id, node = await _find_node(client, device_id, node_addr)
    parsed = _load_json_object(prog_json, "PROG")
    days = parsed.get("prog") if isinstance(parsed, dict) else None
    if not isinstance(days, dict):
        raise click.ClickException(
            'prog payload must be a JSON object with a "prog" object'
        )
    _confirm(ctx, f"set programme on {device_id}/{node_addr}")
    _show_write(ctx, await writes.set_prog(device_id, node, days))


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
    _confirm(ctx, f"set away {away} on {device['dev_id']}")
    _show_write(ctx, await _write_api(ctx, client).set_away(device["dev_id"], away))


@smartbox.command(help="Set device power_limit")
@click.option("-d", "--device-id", required=True)
@click.argument("power-limit", type=int)
@click.pass_context
async def set_device_power_limit(
    ctx: click.Context, device_id: str, power_limit: int
) -> None:
    client = await _client(ctx)
    device = await _find_device(client, device_id)
    _confirm(ctx, f"set power limit {power_limit} on {device['dev_id']}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_power_limit(device["dev_id"], power_limit),
    )


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
    facts = await _node_facts(client, device_id, node)
    _require_check(
        check_node_power_limit(facts),
        facts,
        node,
        "this node has no node-level power-limit setting",
        unavailable="has no node-level power-limit setting",
    )
    _confirm(ctx, f"set node power limit {power_limit} on {device_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_node_power_limit(
            device_id, node, power_limit
        ),
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
    facts = await _node_facts(client, device_id, node)
    _require_check(
        check_boost(facts),
        facts,
        node,
        "storage boost uses set-boost-legacy; the status-flag path is for non-acm heaters",
        unavailable="does not expose a boost mode",
    )
    _confirm(ctx, f"set boost {on} on {device_id}/{node_addr}")
    _show_write(ctx, await _write_api(ctx, client).set_boost(device_id, node, on))


@smartbox.command(help="Set full-boost mode (active, mode, temperature, time)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--active/--inactive", default=None)
@click.option("--mode", type=click.Choice(VALID_BOOST_MODES))
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
    if active and mode is None:
        raise click.ClickException("--mode is required with --active")
    if not active and any(
        value is not None for value in (mode, temperature, time_minutes)
    ):
        raise click.ClickException("an inactive boost mode takes no payload")
    client = await _client(ctx)
    device_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, device_id, node)
    _require_check(
        check_boost_mode(facts, mode),
        facts,
        node,
        "storage boost uses set-boost-legacy; /boost_mode needs a non-acm FULL controller",
        unavailable="does not use the FULL /boost_mode path",
    )
    _confirm(ctx, f"set boost mode on {device_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_boost_mode(
            device_id,
            node,
            active=active,
            mode=mode,
            temperature=temperature,
            time_minutes=time_minutes,
        ),
    )


@smartbox.command(help="Set the storage legacy /boost path")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--on/--off", default=None)
@click.option("--temperature", type=float)
@click.option("--time-minutes", type=int)
@click.option("--units", type=click.Choice(("C", "F")))
@click.pass_context
async def set_boost_legacy(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    on: bool | None,
    temperature: float | None,
    time_minutes: int | None,
    units: str | None,
) -> None:
    if on is None:
        raise click.ClickException("use --on or --off")
    if on and (temperature is None or time_minutes is None or units is None):
        raise click.ClickException(
            "an active legacy boost needs --temperature, --time-minutes and --units"
        )
    if not on and any(
        value is not None for value in (temperature, time_minutes, units)
    ):
        raise click.ClickException("an inactive legacy boost takes no payload")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_legacy_boost(facts),
        facts,
        node,
        "the legacy /boost path needs a Helki storage controller (1C/2E)",
    )
    _confirm(ctx, f"set legacy boost {on} on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_boost_legacy(
            dev_id,
            node,
            on=on,
            temperature=temperature,
            time_minutes=time_minutes,
            units=units,
        ),
    )


@smartbox.command(help="Set the node EASY flag")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--on/--off", "enabled", default=None)
@click.pass_context
async def set_easy(
    ctx: click.Context, device_id: str, node_addr: str, enabled: bool | None
) -> None:
    if enabled is None:
        raise click.ClickException("use --on or --off")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_easy(facts),
        facts,
        node,
        "this product does not expose the EASY flag",
        unavailable="does not expose the EASY flag",
    )
    _confirm(ctx, f"set EASY {enabled} on {dev_id}/{node_addr}")
    _show_write(ctx, await _write_api(ctx, client).set_easy(dev_id, node, enabled))


@smartbox.command(help="Set the node RUNBACK flag")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--on/--off", "enabled", default=None)
@click.pass_context
async def set_runback(
    ctx: click.Context, device_id: str, node_addr: str, enabled: bool | None
) -> None:
    if enabled is None:
        raise click.ClickException("use --on or --off")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_runback(facts),
        facts,
        node,
        "this product does not expose the RUNBACK flag",
        unavailable="does not expose the RUNBACK flag",
    )
    _confirm(ctx, f"set RUNBACK {enabled} on {dev_id}/{node_addr}")
    _show_write(ctx, await _write_api(ctx, client).set_runback(dev_id, node, enabled))


@smartbox.command(help="Set the storage controller mode")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--mode", type=click.Choice(VALID_MODES), required=True)
@click.option("--stemp", type=float)
@click.option("--units", type=click.Choice(("C", "F")))
@click.pass_context
async def set_storage_mode(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    mode: str,
    stemp: float | None,
    units: str | None,
) -> None:
    if stemp is not None and units is None:
        raise click.ClickException("must supply --units with --stemp")
    if units is not None and stemp is None:
        raise click.ClickException("--units is only used with --stemp")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_storage_mode(facts, mode),
        facts,
        node,
        "this command needs an acm storage controller",
        unavailable="uses the target-charge path rather than /mode; use set-target-charge",
    )
    _confirm(ctx, f"set storage mode {mode} on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_storage_mode(
            dev_id, node, mode=mode, stemp=stemp, units=units
        ),
    )


@smartbox.command(help="Set the storage target charge (0 or 100)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("charge", type=click.Choice(("0", "100")))
@click.pass_context
async def set_target_charge(
    ctx: click.Context, device_id: str, node_addr: str, charge: str
) -> None:
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_target_charge(facts, int(charge)),
        facts,
        node,
        "target charge needs a classic-accumulator Elnur storage controller",
    )
    _confirm(ctx, f"set target charge {charge} on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_target_charge(dev_id, node, int(charge)),
    )


@smartbox.command(help="Set the storage /lock state (0/1/2)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("locked", type=click.Choice(("0", "1", "2")))
@click.pass_context
async def set_storage_lock(
    ctx: click.Context, device_id: str, node_addr: str, locked: str
) -> None:
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_storage_lock(facts),
        facts,
        node,
        "this command needs an acm storage controller",
    )
    _confirm(ctx, f"set storage lock {locked} on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_lock_endpoint(dev_id, node, int(locked)),
    )


@smartbox.command(help="Set the storage charging days and valley slots")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--active-days", help="Seven 0/1 entries, e.g. 1110111.")
@click.option("--slot1", help="First valley START-END (HH:MM or minutes).")
@click.option("--slot2", help="Second valley START-END (HH:MM or minutes).")
@click.pass_context
async def set_charging_schedule(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    active_days: str | None,
    slot1: str | None,
    slot2: str | None,
) -> None:
    if active_days is None and slot1 is None and slot2 is None:
        raise click.ClickException("use --active-days, --slot1 or --slot2")
    parsed_days = _parse_days(active_days) if active_days else None
    parsed_slot_1 = _parse_slot(slot1) if slot1 else None
    parsed_slot_2 = _parse_slot(slot2) if slot2 else None
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_charging_schedule(facts),
        facts,
        node,
        "this command needs an acm storage controller",
        unavailable="has no charging-schedule setting (water storage uses extra energy)",
    )
    _confirm(ctx, f"set charging schedule on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_charging_conf(
            dev_id,
            node,
            active_days=parsed_days,
            slot_1=parsed_slot_1,
            slot_2=parsed_slot_2,
        ),
    )


@smartbox.command(help="Set the storage UI mode (0/1/2)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.argument("ui_mode", type=click.Choice(("0", "1", "2")))
@click.pass_context
async def set_storage_ui_mode(
    ctx: click.Context, device_id: str, node_addr: str, ui_mode: str
) -> None:
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_storage_ui_mode(facts, int(ui_mode)),
        facts,
        node,
        "this command needs an Elnur storage controller (14/20/25/2A)",
        unavailable="has no UI-mode setting",
    )
    _confirm(ctx, f"set storage UI mode {ui_mode} on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_storage_ui_mode(dev_id, node, int(ui_mode)),
    )


def _days_from_json(value: str, label: str) -> Mapping[str, Any]:
    parsed = _load_json_object(value, label)
    days = parsed.get("prog", parsed)
    if not isinstance(days, Mapping):
        raise click.BadParameter(
            'expected a day mapping or {"prog": {...}}', param_hint=label
        )
    return days


@smartbox.command(help="Set the thermostat {m,t} programme")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--units", type=click.Choice(("C", "F")), required=True)
@click.option("--prog", required=True, help="JSON day mapping or @file.")
@click.pass_context
async def set_thermostat_prog(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    units: str,
    prog: str,
) -> None:
    days = _days_from_json(prog, "--prog")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_thermostat_prog(facts),
        facts,
        node,
        "this command needs a thermostat {m,t} surface",
    )
    _confirm(ctx, f"set thermostat programme on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_thermostat_prog(dev_id, node, days, units),
    )


@smartbox.command(help="Set programme temperatures (ice/eco/comfort)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--ice", type=float)
@click.option("--eco", type=float)
@click.option("--comfort", type=float)
@click.option("--units", type=click.Choice(("C", "F")))
@click.pass_context
async def set_prog_temps(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    ice: float | None,
    eco: float | None,
    comfort: float | None,
    units: str | None,
) -> None:
    if ice is None and eco is None and comfort is None:
        raise click.ClickException("use --ice, --eco or --comfort")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_prog_temps(facts),
        facts,
        node,
        "this node does not expose editable programme temperatures",
        unavailable="does not expose editable programme temperatures",
    )
    if units is None:
        status = await client.get_node_status(dev_id, node)
        units = status.get("units") if isinstance(status, Mapping) else None
    if units not in ("C", "F"):
        raise click.ClickException("no --units and no status units on the node")
    _confirm(ctx, f"set programme temperatures on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_prog_temps(
            dev_id,
            node,
            ice_temp=ice,
            eco_temp=eco,
            comf_temp=comfort,
            units=units,
        ),
    )


@smartbox.command(help="Set or disable the max-stemp limit")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--units", type=click.Choice(("C", "F")))
@click.option("--off", is_flag=True, help="Write the disabled 0.0 sentinel.")
@click.argument("limit", type=float, required=False)
@click.pass_context
async def set_max_stemp_limit(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    units: str | None,
    off: bool,
    limit: float | None,
) -> None:
    if off == (limit is not None):
        raise click.ClickException("use LIMIT or --off")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_max_stemp_limit(facts),
        facts,
        node,
        "max-stemp is not editable on this product",
        unavailable="does not expose the max-stemp setting",
    )
    _confirm(ctx, f"set max-stemp limit on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_max_stemp_limit(
            dev_id, node, None if off else limit, units
        ),
    )


@smartbox.command(help="Set an htr_mod on/selected-temp/comfort/eco field")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--on", type=bool)
@click.option("--mode", type=click.Choice(VALID_MODES))
@click.option("--selected-temp")
@click.option("--comfort-temp", type=float)
@click.option("--eco-offset")
@click.option("--units", type=click.Choice(("C", "F")))
@click.pass_context
async def set_htr_mod_status(
    ctx: click.Context,
    device_id: str,
    node_addr: str,
    on: bool | None,
    mode: str | None,
    selected_temp: str | None,
    comfort_temp: float | None,
    eco_offset: str | None,
    units: str | None,
) -> None:
    if all(
        value is None for value in (on, mode, selected_temp, comfort_temp, eco_offset)
    ):
        raise click.ClickException("nothing to set")
    client = await _client(ctx)
    dev_id, node = await _find_node(client, device_id, node_addr)
    facts = await _node_facts(client, dev_id, node)
    _require_check(
        check_htr_mod_status(facts),
        facts,
        node,
        "htr_mod status needs code 0A on any wire type, or FF on htr_mod",
    )
    _confirm(ctx, f"set htr_mod status on {dev_id}/{node_addr}")
    _show_write(
        ctx,
        await _write_api(ctx, client).set_htr_mod_status(
            dev_id,
            node,
            on=on,
            mode=mode,
            selected_temp=selected_temp,
            comfort_temp=comfort_temp,
            eco_offset=eco_offset,
            units=units,
        ),
    )


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


smartbox.add_command(codes, "profiles")


@smartbox.command(help="Show the library version")
def version() -> None:
    click.echo(f"smartbox {__version__}")


@smartbox.command(help="Show the resolved CLI configuration and optional API checks")
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
