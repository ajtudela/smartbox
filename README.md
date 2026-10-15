# smartbox
![build](https://github.com/ajtudela/smartbox/workflows/Python%20package/badge.svg)
[![PyPI version](https://badge.fury.io/py/smartbox.svg)](https://badge.fury.io/py/smartbox)
[![codecov](https://codecov.io/gh/ajtudela/smartbox/branch/main/graph/badge.svg?token=ghNZOGVzVv)](https://codecov.io/gh/ajtudela/smartbox)
[![PyPI license](https://img.shields.io/pypi/l/smartbox.svg)](https://pypi.python.org/pypi/smartbox/)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/smartbox.svg)](https://pypi.python.org/pypi/smartbox/)

Python API to control heating 'smart boxes' (Helki and its resellers: Elnur, Haverland, Climastar, Technotherm, HJM, ...). It wraps the REST API and the socket.io channel, and ships a `smartbox` command line tool.

## Installation

    pip install smartbox

The package is importable as `smartbox` and ships `py.typed`.

## Library usage

`AsyncSmartboxSession` is the entry point. Use it as an async context manager so its HTTP client is closed for you; when you already have an `aiohttp.ClientSession` (for example Home Assistant's shared one) pass it as `websession=` and it will not be closed on exit.

```python
import asyncio
from smartbox import AsyncSmartboxSession

async def main() -> None:
    async with AsyncSmartboxSession(
        api_name="api",            # or SMARTBOX_API_NAME
        username="you@example.com",
        password="secret",
        raw_response=False,        # return Pydantic models instead of dicts
    ) as session:
        for device in await session.get_devices():
            for node in await session.get_nodes(device.dev_id):
                status = await session.get_node_status(device.dev_id, node.model_dump())
                print(device.name, node.name, status.mtemp, status.stemp)

asyncio.run(main())
```

With `raw_response=True` (the default) every method returns the raw `dict`/`list` from the API. With `raw_response=False` they return models from `smartbox.models` (`HtrNodeStatus`, `DefaultNodeSetup`, ...); the models are lenient (unknown keys kept, fields optional) because the API is undocumented and varies by reseller.

Constructor options worth knowing: `retry_attempts`/`backoff_factor` (transient 5xx / connection errors are retried with exponential backoff), `x_referer` / `x_serial_id` / `basic_auth_credentials` for a reseller that is not built in.

For live updates over socket.io use `UpdateManager`:

```python
from smartbox import AsyncSmartboxSession, UpdateManager

async with AsyncSmartboxSession(...) as session:
    manager = UpdateManager(session, device_id)
    manager.subscribe_to_node_status(
        lambda node_type, addr, status: print(node_type, addr, status)
    )
    await manager.run()   # runs until cancelled
```

## Authentication

The reseller APIs only offer the OAuth *password* grant (username and password exchanged for an access/refresh token pair), so that is the flow `smartbox` uses; no other flow is available. The grant is deprecated in current OAuth guidance, which is why the package keeps the exposure small: the password is only used to log in and as a fallback when the refresh token is rejected, and `token`, `pass`, `password` and `user_id` are redacted from debug logs and logged URLs. Even so, review any log before pasting it in an issue.

## Websocket transports

Status updates arrive over one of two transports; both are implemented
in this library and verified live (api-notes.md, "Transports —
socket_io vs ws_user"):

* **`socket_io`** (default): a per-device socket.io connection. The
  `UpdateManager` owns it; `run()` runs the loop, `cancel()` is
  terminal.
* **`ws_user`** (the vendor web apps' channel): ONE plain-WebSocket
  connection (`WsUserSocketSession`) serves every device of the
  account. Its server-side proxy PINGs keep the session supervised, and
  sockets are cut by the server exactly at the access token's 4 h
  expiry (close code 1006) — the reconnect with a fresh token is
  mandatory and part of the steady state.

Caller contract for the shared ws_user socket:

    socket_ = WsUserSocketSession(session)
    # The caller owns run() — and must SUPERVISE it: run() exits on any
    # failure (it is re-runnable; only cancel() is terminal), and a
    # parked UpdateManager never observes a shared-socket death, so
    # without a supervisor the transport dies silently. Exceptions
    # surface when the task is awaited, so keep and await it.
    task = asyncio.create_task(supervise(socket_.run()))
    socket_.add_device(dev_id, dev_data_cb, update_cb)   # register (any time)
    socket_.remove_device(dev_id)                        # unregister
    await socket_.cancel()          # TERMINAL: later run() calls no-op

`run()` raises `InvalidAuthError` (rejected credentials — start a
reauthentication flow) and `WsUserUnsupportedError` (deterministic
404/410 — the host does not serve ws_user; fall back to socket_io).

`UpdateManager(session, dev_id, ws_user_socket=socket_)` switches an
individual manager onto the shared connection: `run()` registers the
manager's device and parks until `cancel()` (the shared socket's own
restarts are handled by its supervisor, not by per-device consumers).
Pass-through SocketSession kwargs are ignored in this mode (warned).

## `smartbox` command line tool

Use the `smartbox` tool to read status information from your heaters (nodes) and change settings.

### Options

`-u`/`--username` and `-p`/`--password` (your mobile-app / web-app credentials) are required for every command except `resellers`. `-v`/`--verbose` enables debug logging.

These are only needed when your reseller is not built into the package:

* `-a`/`--api-name`: the API name for your heater vendor, from the 'API Host' entry under the 'Version' menu of the mobile/web app. For a host `api-foo.xxxx` or `api.xxxx` use `api-foo` or `api`.
* `-b`/`--basic-auth-creds`: the HTTP Basic Auth credential used for the initial authentication, as a base64 string. See [Basic Auth Credential](./api-notes.md#basic-auth-credential) in api-notes.md for how to capture it.
* `-r`/`--x-referer`: the `x-referer` header value.
* `-i`/`--x-serial-id`: the `x-serialid` header value.

### Configuration via environment variables / `.env`

Every option above can be supplied through an environment variable instead of being typed on each invocation. The command line always takes precedence over the environment.

| Option                    | Environment variable        |
| ------------------------- | --------------------------- |
| `-a`/`--api-name`         | `SMARTBOX_API_NAME`         |
| `-b`/`--basic-auth-creds` | `SMARTBOX_BASIC_AUTH_CREDS` |
| `-u`/`--username`         | `SMARTBOX_USERNAME`         |
| `-p`/`--password`         | `SMARTBOX_PASSWORD`         |
| `-r`/`--x-referer`        | `SMARTBOX_X_REFERER`        |
| `-i`/`--x-serial-id`      | `SMARTBOX_X_SERIAL_ID`      |

The `smartbox` command also reads a `.env` file (searched for in the working directory and its parents) before parsing options, so the usual workflow is to copy [`.env.example`](.env.example) to `.env`, fill it in once, and then run commands without any auth flags:

    cp .env.example .env
    # edit .env
    smartbox devices

### Commands

In the examples below `<auth options...>` stands for the options above (or nothing, when they come from `.env`).

Read-only, across every device/node:

| Command | What it shows |
| --- | --- |
| `devices` | the devices the account can see |
| `homes` | devices grouped into homes |
| `nodes` | the nodes of each device |
| `status` | live status of every node |
| `setup` | configuration of every node |
| `prog` | weekly heating schedule of every node |
| `device-away-status` | away status of every device |
| `device-connected-status` | whether each device is online |
| `device-power-limit` | power limit (watts) of every device |
| `health-check` | whether the API is alive |
| `api-version` | API build info |
| `resellers` | resellers with a built-in configuration |

```
smartbox <auth options...> status
smartbox <auth options...> prog
```

`guests` needs a home id:

    smartbox <auth options...> guests -h <home id>

`node-samples` reads the temperature/consumption history of one node (`-s`/`-e` default to one hour before/after now):

    smartbox <auth options...> node-samples -d <device id> -n <node addr> [-s <start unix ts>] [-e <end unix ts>]

Writes take named options, one per field to change:

    smartbox <auth options...> set-status -d <device id> -n <node addr> [--mode auto] [--stemp 21.5 --units C] [--locked false]
    smartbox <auth options...> set-setup  -d <device id> -n <node addr> [--control-mode 1] [--offset 0.0] [--units C] [--true-radiant-enabled true] [--window-mode-enabled false] [--priority low]
    smartbox <auth options...> set-prog -d <device id> -n <node addr> '{"prog": {"0": [2, 2, ...], ...}}'
    smartbox <auth options...> set-device-away-status -d <device id> [--away true] [--enabled true] [--forced false]
    smartbox <auth options...> set-device-power-limit -d <device id> <watts>

`socket` opens a long-lived socket.io connection and prints `dev_data` and `update` events until interrupted:

    smartbox <auth options...> socket -d <device id>

See [api-notes.md](./api-notes.md) for notes on the REST and socket.io endpoints.

## Development

Prerequisites:

    uv
    python >=3.14.2

Clone the repo, install dependencies and install pre-commit hooks:

    git clone https://github.com/ajtudela/smartbox
    cd smartbox
    uv sync
    pre-commit install

## Testing

Run the full suite:

    uv run pytest

Generate a coverage XML (e.g. for use in an editor):

    uv run pytest --cov-report xml:cov.xml --cov smartbox --cov-append tests/

`tox` runs the tests against the installed package:

    uv run tox
    uv run tox -e py           # the single environment declared in pyproject.toml

## Changelog

Release notes are kept in [CHANGELOG.md](./CHANGELOG.md).

## Support

This is a community project, maintained on a best-effort basis and provided without warranty.

### Getting help and reporting problems

Open an issue at <https://github.com/ajtudela/smartbox/issues>. Templates are provided for the common cases:

* **Bug report** — a command or method misbehaves. Include the `smartbox` command you ran (or the code), the full output with `-v`/`--verbose`, and your reseller. **Redact the access token and any Basic Auth credential** before pasting logs.
* **Feature request** — a missing endpoint or option.
* **New reseller** — your heater vendor is not in the built-in list. The template asks for `api-name`, `x-referer`, `x-serial-id` and the Basic Auth credential; see ["Capturing it from the reseller's web app"](./api-notes.md#capturing-it-from-the-resellers-web-app) in api-notes.md for how to read them from the login request. Once added, everyone using that vendor benefits.

For questions about the API itself rather than this library, see [api-notes.md](./api-notes.md).

### Supporting the maintainers

If this library is useful to you, you can buy the maintainers a coffee:

[![Buy a coffee to ajtudela][buymeacoffee-shield]][buymeacoffee-ajtudela]
[![Buy a coffee to delmael][buymeacoffee-shield]][buymeacoffee-delmael]

[buymeacoffee-ajtudela]: https://www.buymeacoffee.com/ajtudela
[buymeacoffee-delmael]: https://www.buymeacoffee.com/delmael

[buymeacoffee-shield]: https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png
