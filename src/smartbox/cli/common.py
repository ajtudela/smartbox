"""Shared CLI context, helpers and the command group."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

try:
    import asyncclick as click
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by users only
    raise SystemExit(
        "The smartbox CLI needs the optional dependency: pip install 'smartbox[cli]'"
    ) from exc

from smartbox.device.capabilities import (
    Check,
    NodeFacts,
    OperationRefusedError,
    Reason,
    require,
)
from smartbox.device.codes import code_info, product_code
from smartbox.protocol import (
    AuthConfig,
    AvailableResellers,
    NodeRef,
    SmartboxClient,
    SmartboxReseller,
    WriteApi,
    api_base_url,
)
from smartbox.protocol.errors import ResellerNotExistError
from smartbox.protocol.reseller import SMARTBOX_GENERIC_BASIC_AUTH
from smartbox.token_cache import FileTokenCache, account_stamp, default_cache_path

_LOGGER = logging.getLogger(__name__)


def _mask_identifier(value: object, keep: int = 8) -> str:
    """Shorten a session identifier so a row stays readable without the full sid."""
    text = str(value or "")
    return text if len(text) <= keep else text[:keep] + "…"


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
    """A WriteHost that discards POSTs while delegating the reads."""

    def __init__(self, client: SmartboxClient) -> None:
        self.client = client

    async def post(self, path: str, body: Mapping[str, object]) -> None:
        """Discard the write; the caller prints WriteApi's returned tuple."""

    async def get_node_status(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_status(dev_id, node)

    async def get_node_setup(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_setup(dev_id, node)

    async def get_node_prog(self, dev_id: str, node: NodeRef) -> dict[str, Any]:
        return await self.client.get_node_prog(dev_id, node)


class _VersionStore:
    """One-node version cache for the setup omission rule."""

    def __init__(self, dev_id: str, node: NodeRef, facts: NodeFacts) -> None:
        self._dev_id = dev_id
        self._node = node
        self._version = {"pid": facts.pid, "fw_version": facts.fw_version}

    def version(self, dev_id: str, node: NodeRef) -> dict[str, str | None] | None:
        """Return the cached version for this node, else None."""
        if dev_id == self._dev_id and node == self._node:
            return self._version
        return None

    def setup(self, dev_id: str, node: NodeRef) -> None:  # noqa: ARG002
        """No setup cache; merges fall back to REST."""
        return

    def prog(self, dev_id: str, node: NodeRef) -> None:  # noqa: ARG002
        """No prog cache; merges fall back to REST."""
        return


def _write_api(
    ctx: click.Context,
    client: SmartboxClient,
    *,
    facts: NodeFacts | None = None,
    dev_id: str | None = None,
    node: NodeRef | None = None,
) -> WriteApi:
    """Build a WriteApi, using a recording host under --dry-run."""
    host: Any = client
    if ctx.obj["dry_run"]:
        host = _DryRunHost(client)
    store = (
        _VersionStore(dev_id, node, facts)
        if facts is not None and dev_id is not None and node is not None
        else None
    )
    return WriteApi(host, store=store)


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
    if not sys.stdin.isatty():
        click.echo(
            f"warning: stdin is not a tty; proceeding without confirmation for {action}",
            err=True,
        )
        return
    if not click.confirm(f"{action}?", default=False):
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
    try:
        require(check)
    except OperationRefusedError as ex:
        raise click.ClickException(
            _refusal_message(ex.check, facts, node, requirement, unavailable)
        ) from ex


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


_DOTENV: dict[str, Path] = {}


def dotenv_path() -> Path | None:
    """Return the ``.env`` file loaded at entry, if any."""
    return _DOTENV.get("path")


def _load_env_file(path: Path) -> None:
    """Load simple ``KEY=VALUE`` lines without overriding the environment."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as ex:
        _LOGGER.warning("Ignoring unreadable .env file %s: %s", path, ex)
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _load_dotenv() -> None:
    path = _find_dotenv()
    if path is not None:
        _DOTENV["path"] = path
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
    ctx.obj["forget_tokens"] = False
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
    client: SmartboxClient,
    dev_id: str,
    addr: str,
    device: dict[str, Any] | None = None,
) -> tuple[str, NodeRef, dict[str, Any]]:
    device = device if device is not None else await _find_device(client, dev_id)
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


@click.group(
    cls=_SmartboxGroup,
    chain=True,
    epilog="Global options (--json, --dry-run, --yes) go before the command.",
)
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
    if forget_tokens:
        await _clear_forgotten_tokens(ctx)


async def _clear_forgotten_tokens(ctx: click.Context) -> None:
    """Clear the account's cached session before a command that may skip it."""
    path = ctx.obj.get("token_cache_path")
    account = ""
    if path is None:
        username = ctx.obj.get("username")
        if not username:
            click.echo(
                "warning: --forget-tokens needs -u or --token-cache-path; "
                "nothing was cleared",
                err=True,
            )
            return
        _reseller, base_url = _resolve_reseller(ctx)
        path = str(
            default_cache_path(
                ctx.obj["api_name"], ctx.obj["serial_id"], username, base_url
            )
        )
        account = account_stamp(
            ctx.obj["api_name"], ctx.obj["serial_id"], username, base_url
        )
    cache = FileTokenCache(Path(path), account=account)
    if not await cache.async_clear():
        click.echo(f"warning: could not remove {cache.path}", err=True)
    ctx.obj["forget_tokens"] = False


def _node(node: dict[str, Any]) -> NodeRef:
    return NodeRef(node_type=str(node["type"]), addr=str(node["addr"]))
