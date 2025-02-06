"""Command line interaction with Smartbox system."""

import json
import logging
from typing import Any

from aiohttp import ClientSession
import asyncclick as click

from smartbox.resailer import AvailableResailers
from smartbox.session import AsyncSmartboxSession
from smartbox.socket import SocketSession

_LOGGER = logging.getLogger(__name__)


def _pretty_print(data: dict[str, Any]) -> None:
    """Pretty print json."""
    print(json.dumps(data, indent=4, sort_keys=True))


@click.group(chain=True)
@click.option("-a", "--api-name", required=False, help="API name")
@click.option(
    "-b",
    "--basic-auth-creds",
    required=False,
    help="API basic auth credentials",
)
@click.option("-u", "--username", required=True, help="API username")
@click.option("-p", "--password", required=True, help="API password")
@click.option(
    "-v",
    "--verbose/--no-verbose",
    default=False,
    help="Enable verbose logging",
)
@click.option("-r", "--x-referer", required=False, help="Refere of API")
@click.option("-i", "--x-serial-id", required=False, help="Serial id of API")
@click.pass_context
async def smartbox(
    ctx,
    api_name: str,
    basic_auth_creds: str,
    username: str,
    password: str,
    verbose: bool,
    x_serial_id: int,
    x_referer: str,
) -> None:
    """Set default options for smartbox."""
    ctx.ensure_object(dict)
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s "
        "[%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    session = AsyncSmartboxSession(
        api_name=api_name,
        basic_auth_credentials=basic_auth_creds,
        username=username,
        password=password,
        websession=ClientSession(),
        x_referer=x_referer,
        x_serial_id=x_serial_id,
    )
    ctx.obj["session"] = session
    ctx.obj["verbose"] = verbose


@smartbox.command(help="Show devices")
@click.pass_context
async def devices(ctx) -> None:
    """Show devices."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    _pretty_print(devices)


@smartbox.command(help="Show Homes")
@click.pass_context
async def homes(ctx) -> None:
    """Show homes."""
    session = ctx.obj["session"]
    devices = await session.get_homes()
    _pretty_print(devices)


@smartbox.command(help="Show nodes")
@click.pass_context
async def nodes(ctx) -> None:
    """Show nodes."""
    session = ctx.obj["session"]
    devices = await session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = await session.get_nodes(device["dev_id"])
        _pretty_print(nodes)


@smartbox.command(help="Show node status")
@click.pass_context
async def status(ctx) -> None:
    """Show node status."""
    session = ctx.obj["session"]
    devices = await session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = await session.get_nodes(device["dev_id"])

        for node in nodes:
            print(f"{node['name']} (addr: {node['addr']})")
            status = await session.get_node_status(device["dev_id"], node)
            _pretty_print(status)


@smartbox.command(help="Show node power and temperature records (aka samples)")
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID for node to set status on",
)
@click.option(
    "-n",
    "--node-addr",
    type=int,
    required=True,
    help="Address of node to set status on",
)
@click.option(
    "-s",
    "--start-time",
    type=int,
    required=False,
    help="Default now - 1 hour",
)
@click.option(
    "-e",
    "--end-time",
    type=int,
    required=False,
    help="Default now + 1 hour",
)
@click.pass_context
async def node_samples(
    ctx,
    device_id: str,
    node_addr: str,
    start_time: int,
    end_time: int,
) -> None:
    """Show node temperatures and consumption history."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)
    nodes = await session.get_nodes(device["dev_id"])
    node = next(n for n in nodes if n["addr"] == node_addr)

    node_samples = await session.get_node_samples(
        device_id,
        node,
        start_time,
        end_time,
    )
    _pretty_print(node_samples)


@smartbox.command(
    help="Set node status (pass settings as extra args, e.g. mode=auto)",
)
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID for node to set status on",
)
@click.option(
    "-n",
    "--node-addr",
    type=int,
    required=True,
    help="Address of node to set status on",
)
@click.option("--locked", type=bool)
@click.option("--mode")
@click.option("--stemp")
@click.option("--units")
@click.pass_context
async def set_status(
    ctx,
    device_id: str,
    node_addr: str,
    **kwargs: dict[str, Any],
) -> None:
    """Set node status."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)
    nodes = await session.get_nodes(device["dev_id"])
    node = next(n for n in nodes if n["addr"] == node_addr)

    await session.set_node_status(device["dev_id"], node, kwargs)


@smartbox.command(help="Show node setup")
@click.pass_context
async def setup(ctx) -> None:
    """Show node setup."""
    session = ctx.obj["session"]
    devices = await session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = await session.get_nodes(device["dev_id"])

        for node in nodes:
            print(f"{node['name']} (addr: {node['addr']})")
            setup = await session.get_node_setup(device["dev_id"], node)
            _pretty_print(setup)


@smartbox.command(help="Set node setup options")
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID for node to set setup on",
)
@click.option(
    "-n",
    "--node-addr",
    type=int,
    required=True,
    help="Address of node to set setup on",
)
@click.option("--control-mode", type=int, default=None)
@click.option("--offset", type=str, default=None)
@click.option("--priority", type=str, default=None)
@click.option("--true-radiant-enabled", type=bool, default=None)
@click.option("--units", type=str, default=None)
@click.option("--window-mode-enabled", type=bool, default=None)
@click.pass_context
async def set_setup(
    ctx,
    device_id: str,
    node_addr: str,
    **kwargs: dict[str, Any],
) -> None:
    """Set node setup options."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)
    nodes = await session.get_nodes(device["dev_id"])
    node = next(n for n in nodes if n["addr"] == node_addr)

    # Only pass specified options
    setup_kwargs = {k: v for k, v in kwargs.items() if v is not None}
    await session.set_node_setup(device["dev_id"], node, setup_kwargs)


@smartbox.command(help="Show device away_status")
@click.pass_context
async def device_away_status(ctx) -> None:
    """Show device away status."""
    session = ctx.obj["session"]
    devices = await session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        device_away_status = await session.get_device_away_status(
            device["dev_id"],
        )
        _pretty_print(device_away_status)


@smartbox.command(
    help="Set device away_status (pass settings as extra args, e.g. away=true)",
)
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID to set away_status on",
)
@click.option("--away", type=bool)
@click.option("--enabled", type=bool)
@click.option("--forced", type=bool)
@click.pass_context
async def set_device_away_status(
    ctx,
    device_id: str,
    **kwargs: dict[str, Any],
) -> None:
    """Set device away status."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)

    await session.set_device_away_status(device["dev_id"], kwargs)


@smartbox.command(help="Show device power_limit")
@click.pass_context
async def device_power_limit(ctx) -> None:
    """Show device power limit."""
    session = ctx.obj["session"]
    devices = await session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        device_power_limit = await session.get_device_power_limit(
            device["dev_id"],
        )
        _pretty_print(device_power_limit)


@smartbox.command(help="Set device power_limit")
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID to set power_limit on",
)
@click.argument("power-limit", type=int)
@click.pass_context
async def set_device_power_limit(ctx, device_id: str, power_limit: int) -> None:
    """Set device power limit."""
    session = ctx.obj["session"]
    devices = await session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)

    await session.set_device_power_limit(device["dev_id"], power_limit)


@smartbox.command(help="Open socket.io connection to device.")
@click.option(
    "-d",
    "--device-id",
    required=True,
    help="Device ID to open socket for",
)
@click.pass_context
async def socket(ctx, device_id: str) -> None:
    """Open socket.io connection to device."""
    session = ctx.obj["session"]
    verbose = ctx.obj["verbose"]

    def on_dev_data(data) -> None:
        """Received dev_data."""
        _LOGGER.info("Received dev_data:")
        _pretty_print(data)

    def on_update(data) -> None:
        """Received update."""
        _LOGGER.info("Received update:")
        _pretty_print(data)

    socket_session = SocketSession(
        session,
        device_id,
        on_dev_data,
        on_update,
        verbose,
        add_sigint_handler=True,
    )
    await socket_session.run()


@smartbox.command(help="Get status of the API.")
@click.pass_context
async def health_check(ctx) -> None:
    """Get the status of the API."""
    session = ctx.obj["session"]
    health = await session.health_check()
    _pretty_print(health)


@smartbox.command(help="Get the availables resailers.")
def resailers() -> None:
    """Get the availables resailers."""
    for item in AvailableResailers.resailers.items():
        print(item)


# For debugging
if __name__ == "__main__":
    smartbox()
