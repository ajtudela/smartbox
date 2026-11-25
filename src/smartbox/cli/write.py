"""CLI write commands."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

try:
    import asyncclick as click
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by users only
    raise SystemExit(
        "The smartbox CLI needs the optional dependency: pip install 'smartbox[cli]'"
    ) from exc

from smartbox.device.capabilities import (
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
from smartbox.device.control_modes import control_mode_choices, control_mode_label
from smartbox.protocol.writes import VALID_BOOST_MODES, VALID_MODES, VALID_PRIORITIES

from .common import (
    _client,
    _confirm,
    _find_device,
    _find_node,
    _load_json_object,
    _node_facts,
    _parse_days,
    _parse_slot,
    _require_check,
    _show_write,
    _write_api,
    smartbox,
)


def _resolve_control_mode(mode: str, choices: tuple[tuple[str, int], ...]) -> int:
    raw = mode.strip()
    if re.fullmatch(r"[+-]?\d+", raw):
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
    offered = ", ".join(f"{index} = {label}" for label, index in choices)
    if not matches:
        raise click.ClickException(
            f"control mode {mode!r} not found; choices: {offered}"
        )
    if len(matches) > 1:
        match_list = ", ".join(f"{index} = {label}" for label, index in matches)
        raise click.ClickException(
            f"control mode {mode!r} is ambiguous; matches: {match_list}"
        )
    return matches[0][1]


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


@smartbox.command(
    help="Set node status (lock/mode/setpoint; htr_mod fields use set-htr-mod-status)",
    short_help="Set node status",
)
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--locked", type=bool)
@click.option("--mode", type=click.Choice(VALID_MODES))
@click.option("--stemp", type=float)
@click.option("--units", type=click.Choice(("C", "F")))
@click.option("--on", type=bool, hidden=True)
@click.option("--selected-temp", hidden=True)
@click.option("--comfort-temp", type=float, hidden=True)
@click.option("--eco-offset", hidden=True)
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
    if any(
        value is not None for value in (on, selected_temp, comfort_temp, eco_offset)
    ):
        raise click.ClickException(
            "htr_mod fields moved to set-htr-mod-status "
            "(--on/--selected-temp/--comfort-temp/--eco-offset)"
        )
    client = await _client(ctx)
    writes = _write_api(ctx, client)
    device_id, node = await _find_node(client, device_id, node_addr)
    if stemp is not None and units is None:
        raise click.ClickException("must supply --units with --stemp")
    if units is not None and stemp is None:
        raise click.ClickException("--units is only used with --stemp")
    if all(value is None for value in (locked, mode, stemp, units)):
        raise click.ClickException("nothing to set: use --locked, --mode or --stemp")
    _confirm(ctx, f"set node status on {device_id}/{node_addr}")
    if locked is not None:
        _show_write(ctx, await writes.set_lock(device_id, node, locked))
    if mode is not None:
        _show_write(ctx, await writes.set_mode(device_id, node, mode))
    if stemp is not None and units is not None:
        _show_write(ctx, await writes.set_setpoint(device_id, node, stemp, units))


@smartbox.command(help="Set node setup options (control mode uses set-control-mode)")
@click.option("-d", "--device-id", required=True)
@click.option("-n", "--node-addr", required=True)
@click.option("--control-mode", type=str, hidden=True)
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
    control_mode: str | None,
    offset: float | None,
    priority: str | None,
    true_radiant_enabled: bool | None,
    units: str | None,
    window_mode_enabled: bool | None,
    extra_json: str | None,
) -> None:
    if control_mode is not None:
        raise click.ClickException(
            "--control-mode moved to set-control-mode (label or wire index)"
        )
    client = await _client(ctx)
    writes = _write_api(ctx, client)
    device_id, node = await _find_node(client, device_id, node_addr)
    extra = (
        _load_json_object(extra_json, "--extra-json")
        if extra_json is not None
        else None
    )
    if all(
        value is None
        for value in (
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
    _confirm(ctx, f"set node setup on {device_id}/{node_addr}")
    if offset is not None:
        _show_write(ctx, await writes.set_away_offset(device_id, node, offset))
    if priority is not None:
        _show_write(ctx, await writes.set_priority(device_id, node, priority))
    if true_radiant_enabled is not None:
        _show_write(
            ctx,
            await writes.set_true_radiant(device_id, node, true_radiant_enabled),
        )
    if units is not None:
        _show_write(ctx, await writes.set_setup_value(device_id, node, "units", units))
    if window_mode_enabled is not None:
        _show_write(
            ctx,
            await writes.set_window_mode(device_id, node, window_mode_enabled),
        )
    if extra is not None:
        _show_write(ctx, await writes.set_extra_options(device_id, node, extra))


@smartbox.command(help="Set node prog from JSON or @file")
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
    _confirm(ctx, f"set away {'on' if away else 'off'} on {device['dev_id']}")
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
    _confirm(ctx, f"set boost {'on' if on else 'off'} on {device_id}/{node_addr}")
    _show_write(ctx, await _write_api(ctx, client).set_boost(device_id, node, on))


@smartbox.command(help="Set full-boost mode")
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
    _confirm(ctx, f"set EASY {'on' if enabled else 'off'} on {dev_id}/{node_addr}")
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
    _confirm(ctx, f"set RUNBACK {'on' if enabled else 'off'} on {dev_id}/{node_addr}")
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
    if off and limit is not None:
        raise click.ClickException("use LIMIT or --off")
    if not off and limit is None:
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
@click.option("--selected-temp", help="App label, e.g. comfort/eco/off.")
@click.option("--comfort-temp", type=float)
@click.option("--eco-offset", help="Signed offset in the node unit.")
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
    if comfort_temp is not None and units is None:
        raise click.ClickException("--comfort-temp requires --units")
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
