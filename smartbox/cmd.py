import asyncio
import click
import json
import logging

from smartbox.session import Session
from smartbox.socket import SocketSession

_LOGGER = logging.getLogger(__name__)


def _pretty_print(data):
    print(json.dumps(data, indent=4, sort_keys=True))


@click.group(chain=True)
@click.option("-a", "--api-name", required=True, help="API name")
@click.option(
    "-b", "--basic-auth-creds", required=True, help="API basic auth credentials"
)
@click.option("-u", "--username", required=True, help="API username")
@click.option("-p", "--password", required=True, help="API password")
@click.option(
    "-v", "--verbose/--no-verbose", default=False, help="Enable verbose logging"
)
@click.pass_context
def smartbox(ctx, api_name, basic_auth_creds, username, password, verbose):
    ctx.ensure_object(dict)
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s "
        "[%(name)s.%(funcName)s:%(lineno)d] %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    session = Session(api_name, basic_auth_creds, username, password)
    ctx.obj["session"] = session
    ctx.obj["verbose"] = verbose


@smartbox.command(help="Show devices")
@click.pass_context
def devices(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()
    _pretty_print(devices)


@smartbox.command(help="Show nodes")
@click.pass_context
def nodes(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = session.get_nodes(device["dev_id"])
        _pretty_print(nodes)


@smartbox.command(help="Show node status")
@click.pass_context
def status(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = session.get_nodes(device["dev_id"])

        for node in nodes:
            print(f"{node['name']} (addr: {node['addr']})")
            status = session.get_status(device["dev_id"], node)
            _pretty_print(status)


@smartbox.command(help="Set node status (pass settings as extra args, e.g. mode=auto)")
@click.option(
    "-d", "--device-id", required=True, help="Device ID for node to set status on"
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
# TODO: other options
@click.pass_context
def set_status(ctx, device_id, node_addr, **kwargs):
    session = ctx.obj["session"]
    devices = session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)
    nodes = session.get_nodes(device["dev_id"])
    node = next(n for n in nodes if n["addr"] == node_addr)

    session.set_status(device["dev_id"], node, kwargs)


@smartbox.command(help="Show node setup")
@click.pass_context
def setup(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        nodes = session.get_nodes(device["dev_id"])

        for node in nodes:
            print(f"{node['name']} (addr: {node['addr']})")
            setup = session.get_setup(device["dev_id"], node)
            _pretty_print(setup)


@smartbox.command(help="Set node setup options")
@click.option(
    "-d", "--device-id", required=True, help="Device ID for node to set setup on"
)
@click.option(
    "-n", "--node-addr", type=int, required=True, help="Address of node to set setup on"
)
@click.option("--control-mode", type=int, default=None)
@click.option("--offset", type=str, default=None)
@click.option("--priority", type=str, default=None)
@click.option("--true-radiant-enabled", type=bool, default=None)
@click.option("--units", type=str, default=None)
@click.option("--window-mode-enabled", type=bool, default=None)
@click.pass_context
def set_setup(ctx, device_id, node_addr, **kwargs):
    session = ctx.obj["session"]
    devices = session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)
    nodes = session.get_nodes(device["dev_id"])
    node = next(n for n in nodes if n["addr"] == node_addr)

    # Only pass specified options
    setup_kwargs = {k: v for k, v in kwargs.items() if v is not None}
    session.set_setup(device["dev_id"], node, setup_kwargs)


@smartbox.command(help="Show device away_status")
@click.pass_context
def device_away_status(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        device_away_status = session.get_device_away_status(device["dev_id"])
        _pretty_print(device_away_status)


@smartbox.command(
    help="Set device away_status (pass settings as extra args, e.g. away=true)"
)
@click.option(
    "-d", "--device-id", required=True, help="Device ID to set away_status on"
)
@click.option("--away", type=bool)
@click.option("--enabled", type=bool)
@click.option("--forced", type=bool)
@click.pass_context
def set_device_away_status(ctx, device_id, **kwargs):
    session = ctx.obj["session"]
    devices = session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)

    session.set_device_away_status(device["dev_id"], kwargs)


@smartbox.command(help="Show device power_limit")
@click.pass_context
def device_power_limit(ctx):
    session = ctx.obj["session"]
    devices = session.get_devices()

    for device in devices:
        print(f"{device['name']} (dev_id: {device['dev_id']})")
        device_power_limit = session.get_device_power_limit(device["dev_id"])
        _pretty_print(device_power_limit)


@smartbox.command(help="Set device power_limit")
@click.option(
    "-d", "--device-id", required=True, help="Device ID to set power_limit on"
)
@click.argument("power-limit", type=int)
@click.pass_context
def set_device_power_limit(ctx, device_id, power_limit):
    session = ctx.obj["session"]
    devices = session.get_devices()
    device = next(d for d in devices if d["dev_id"] == device_id)

    session.set_device_power_limit(device["dev_id"], power_limit)


@smartbox.command(help="Open socket.io connection to device.")
@click.option("-d", "--device-id", required=True, help="Device ID to open socket for")
@click.pass_context
def socket(ctx, device_id):
    session = ctx.obj["session"]
    verbose = ctx.obj["verbose"]

    def on_dev_data(data):
        _LOGGER.info("Received dev_data:")
        _pretty_print(data)

    def on_update(data):
        _LOGGER.info("Received update:")
        _pretty_print(data)

    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)
    socket_session = SocketSession(
        session, device_id, on_dev_data, on_update, verbose, add_sigint_handler=True
    )
    task = event_loop.create_task(socket_session.run())
    event_loop.run_until_complete(task)


# For debuggging
if __name__ == "__main__":
    smartbox()
