# smartbox
![build](https://github.com/ajtudela/smartbox/workflows/Python%20package/badge.svg)
[![PyPI version](https://badge.fury.io/py/smartbox.svg)](https://badge.fury.io/py/smartbox)
[![codecov](https://codecov.io/gh/ajtudela/smartbox/branch/main/graph/badge.svg?token=ghNZOGVzVv)](https://codecov.io/gh/ajtudela/smartbox)
[![PyPI license](https://img.shields.io/pypi/l/smartbox.svg)](https://pypi.python.org/pypi/smartbox/)
[![PyPI pyversions](https://img.shields.io/pypi/pyversions/smartbox.svg)](https://pypi.python.org/pypi/smartbox/)

Python API to control heating 'smart boxes' (Helki and its resellers: Elnur, Haverland, Ducasa, Climastar, Technotherm, HJM, ...). It wraps the REST API and the `ws_user` websocket channel, and ships a `smartbox` command line tool.

This is the 3.x line: the protocol and domain layers that the Home Assistant integration uses, packaged Home Assistant-free. `smartbox.protocol` owns the wire (auth, REST client, `ws_user` transport, named writes) and `smartbox.core` owns the domain state (versioned store, write confirmation, availability and feed supervision). The previous session/update-manager API lives on in the `v2.7.0` tag.

## Installation

    pip install smartbox

With the optional `smartbox` command line tool:

    pip install smartbox[cli]

The base package only depends on `aiohttp`; the CLI pulls in `asyncclick`. The package is importable as `smartbox` and ships `py.typed`.

## Library usage

`SmartboxClient` is the REST entry point: it owns the `/api/v2` calls and the token lifecycle (login, refresh, re-auth on expiry). Inject an existing `aiohttp.ClientSession` (for example Home Assistant's shared one) with `session=` and it will not be closed for you; leave it out and the client creates and owns its own.

```python
import asyncio

from smartbox.protocol import (
    AuthConfig,
    AvailableResellers,
    SmartboxClient,
    api_base_url,
)

reseller = AvailableResellers("api").reseller
CONFIG = AuthConfig(
    api_host=api_base_url(reseller),  # honors the row's host (Elnur: api-elnur)
    basic_auth_creds=reseller.basic_auth,
    serial_id=str(reseller.serial_id),
    username="you@example.com",
    password="secret",
    referer=reseller.web_url or None,
)


async def main() -> None:
    client = SmartboxClient(CONFIG)
    try:
        devices = await client.get_devices()
        for device in devices["devs"]:
            nodes = await client.get_nodes(device["dev_id"])
            print(device["name"], [node["name"] for node in nodes])
    finally:
        await client.close()


asyncio.run(main())
```

Methods return the raw `dict`/`list` payloads from the API (raw passthrough): the API is undocumented and varies by reseller, so 3.0 does not model responses with Pydantic models any more. Writes go through `WriteApi`, which exposes named operations (`set_setpoint`, `set_mode`, `set_lock`, `set_setup_value`, ...):

```python
from smartbox.protocol import NodeRef, WriteApi

writes = WriteApi(client)  # the client from the example above
node = NodeRef(node_type="htr_mod", addr="1")
await writes.set_setpoint("mydev", node, 21.5, "C")
```

For a reseller that is not built in, the identity travels in the `AuthConfig` headers (`basic_auth_creds`, `serial_id`, `referer`) against the reseller's `api_host`; see [`smartbox resellers`](#smartbox-command-line-tool) for the built-in list. The built-in table carries the per-reseller host: `api_base_url(reseller)` is the supported way to build `AuthConfig.api_host`. Elnur is the `ws_user` exception to the shared `users-api` host; the legacy EHC row carries `api-ehc.helki.com`, but its `socket.io` transport is not supported by 3.x.

A caller that keeps tokens across process restarts can seed `TokenManager(..., initial_tokens=...)` and pass `on_grant` to receive every rotated bundle; `TokenBundle.to_record()` / `from_record()` cover the stored shape. `smartbox.token_cache.FileTokenCache` is the CLI/tool implementation, keyed by the tenant account and effective API host.

## Live updates over websocket (`ws_user`)

Status updates arrive over the vendor web apps' channel: ONE plain-WebSocket connection (`WsUserTransport`) serves every device of the account. Its server-side proxy PINGs keep the session supervised, and sockets are cut by the server exactly at the access token's 4 h expiry (close code 1006) — the reconnect with a fresh token is mandatory and part of the steady state.

The transport never interprets frames: it owns liveness (heartbeat), the reconnect policy (fresh token per connect, jittered exponential backoff) and dispatches typed events (`Snapshot`, `Update`, `SocketClosed`) to the handler you `subscribe()`:

```python
from smartbox.protocol import (
    AvailableResellers,
    ManagedTokenSource,
    WsUserTransport,
    api_base_url,
)

reseller = AvailableResellers("api").reseller
transport = WsUserTransport(ManagedTokenSource(client.auth), api_base_url(reseller))


async def on_event(event) -> None:
    print(event)


transport.subscribe(on_event)
await transport.run()  # reconnects per policy; CancelledError stops it
await transport.close()  # terminal
```

`run()` raises `AuthError` (credentials keep being rejected — start a reauthentication flow) and `TransportUnsupportedError` (deterministic handshake rejection — the host does not serve `ws_user`). `check_ws_user_support` runs the one-shot probe if you want to know before subscribing. Transient trouble is retried inside `run()` and never surfaces as terminal.

For consumers that want state (the Home Assistant integration, or any dashboard), `smartbox.core.SmartboxEngine` drives a versioned store from those events and supervises the transport for you — restarts with capped backoff on transient exits, marks terminal errors on `unsupported` / `auth_failed`, and confirms writes:

```python
import asyncio

from smartbox.core import SmartboxEngine

engine = SmartboxEngine(client)  # client enables the write path


def on_change(change) -> None:
    print(change)


engine.subscribe(on_change)
engine.supervise(transport)  # wires events in, restarts on failure
...
await engine.stop()  # cancels the supervision task
```

Subscribers receive typed changes (`NodeChange`, `DeviceChange`, `FeedChange`, ...); handler exceptions are isolated log-and-continue. `engine.world` exposes the current `WorldState` at any time, and `NodeState.available` is engine-derived so stale feeds render as unavailable instead of showing stale values.

## `smartbox` command line tool

Use the `smartbox` tool to read status information from your heaters (nodes) and change settings.

### Options

`-u`/`--username` and `-p`/`--password` (your mobile-app / web-app credentials) are required for every API command. The offline commands are `resellers`, `profile`, `codes`, `version` and `doctor`/`config` without `--check`. `-v`/`--verbose` enables debug logging.

These are only needed when your reseller is not built into the package, or when a built-in row's host has moved:

* `-a`/`--api-name`: the brand key for your heater vendor, from the 'API Host' entry under the 'Version' menu of the mobile/web app. The built-in row selects the tenant credentials and host; use `--api-host` if the host differs.
* `-b`/`--basic-auth-creds`: the HTTP Basic Auth credential used for the initial authentication, as a base64 string. To capture it: open your reseller's web app with the browser's network tab, log in, and read the `POST .../api/v2/client/token` request headers.
* `-r`/`--x-referer`: the `x-referer` header value.
* `-i`/`--x-serial-id`: the `x-serialid` header value.
* `--api-host`: override the API host (hostname or full `https://` URL); defaults to the reseller row. Useful when a tenant moves to a different host.
* `--token-cache/--no-token-cache`: persist the refresh token between invocations (enabled by default).
* `--token-cache-path`: select a cache file instead of the per-account default under `$XDG_CACHE_HOME/smartbox`.
* `--forget-tokens`: delete the cached session before running; the next grant is cached.

### Configuration via environment variables / `.env`

Every option above can be supplied through an environment variable instead of being typed on each invocation. The command line always takes precedence over the environment, which takes precedence over `.env`.

| Option                    | Environment variable        |
| ------------------------- | --------------------------- |
| `-a`/`--api-name`         | `SMARTBOX_API_NAME`         |
| `-b`/`--basic-auth-creds` | `SMARTBOX_BASIC_AUTH_CREDS` |
| `-u`/`--username`         | `SMARTBOX_USERNAME`         |
| `-p`/`--password`         | `SMARTBOX_PASSWORD`         |
| `-r`/`--x-referer`        | `SMARTBOX_X_REFERER`        |
| `-i`/`--x-serial-id`      | `SMARTBOX_X_SERIAL_ID`      |
| `--api-host`              | `SMARTBOX_API_HOST`         |
| `--token-cache-path`      | `SMARTBOX_TOKEN_CACHE`      |

The `smartbox` command also reads a `.env` file (searched for in the working directory and its parents) before parsing options, so the usual workflow is to copy [`.env.example`](.env.example) to `.env`, fill it in once, and then run commands without any auth flags:

    cp .env.example .env
    # edit .env
    smartbox devices

### Token cache

Tokens are cached between invocations by default, so repeated commands continue
one vendor session instead of creating a login per run. The cache is
per account and API host under `$XDG_CACHE_HOME/smartbox`, written `0600` and
replaced atomically. `--token-cache-path PATH` (or `SMARTBOX_TOKEN_CACHE`)
selects another file, `--no-token-cache` disables persistence, and
`--forget-tokens` deletes the cached session before running. The cache holds a
live refresh token: treat it like the `.env` credentials. A host override or
different account gets a different cache entry.

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
| `control-modes` | the node’s control-mode label, current wire index and accepted choices |
| `node-version` | version payload of every node |
| `node-power` | the node's own power reading (pmo nodes only; heaters report power in `status`) |
| `device-away-status` | away status of every device |
| `device-connected-status` | whether each device is online |
| `device-power-limit` | power limit (watts) of every device |
| `rtc` | the box RTC time |
| `discovery` | the box discovery payload |
| `dev-data` | the raw `dev_data` payload |
| `health-check` | whether the API is alive |
| `api-version` | API build info |
| `sessions` | the account's active vendor sessions |
| `resellers` | all known reseller connection details |
| `codes` | the reviewed product-code table, with filters |
| `node-profile` | the live product row and resolved profile of one node |
| `diagnose` | a per-node profile/identity summary with issues |
| `doctor` / `config` | resolved reseller, host, serial, cache path; `--check` calls the API |
| `version` | the installed library version |
| `profile` | resolve one controller profile offline from `--node-type`, `--pid`, `--fw-version`, `--setup` JSON and an optional `--max-limit-c` override |

`status`, `setup`, `prog`, `node-version` and `node-power` accept `-d <device>` and `-n <node addr>` filters. The device-level reads accept `-d`. `node-power` requires both and refuses a non-`pmo` node: heater power is in `status.power`, and the box limit is `device-power-limit`. The global options (`--json`, `--dry-run`, `--yes`) must come before the command. Put `--json` there to get one JSON document for a multi-device/multi-node read; a `--json` after the command is accepted only by the commands with a local `--json` option (`sessions`, `codes`, `doctor`/`config`):

    smartbox --json status -d <device id> -n <node addr>

`codes` filters with `--family`, `--limits-family`, `--surface`, `--relay`, `--thm` and `--search`; `codes --json` prints the raw rows.

`smartbox profile --pid 08320000 --node-type htr --fw-version 1.9 --setup '{"factory_options": {"boost_config": 2}, "max_stemp_limit": "23.0"}'` prints the neutral limits, bounds and programme ranges; a stored `max_stemp_limit` shifts the programme ceilings, `--max-limit-c` overrides it (`0` prints the unclamped catalogue), and the command contacts no API and needs no credentials. `--pid` also accepts the bare two-character code (`32`); an unknown code is rejected.

`smartbox resellers` prints each row's key, brand, brand key, API host,
serial id, Basic source and referer in one table. The API hosts are HTTPS;
the Basic source is `generic` or `custom`, and the credential value is not
printed. The brand key is not the endpoint; it selects the tenant credentials,
serial id and referer. `--api-host` affects client commands, not this static
table.

`sessions` lists the account's active sessions (masked `sid`, timestamps,
device and a `*` on the session the current token belongs to). `--json` prints
the raw payload and `--limit` caps the rows; it is read-only.

```
smartbox <auth options...> status
smartbox <auth options...> prog
```

`guests` needs a home id:

    smartbox <auth options...> guests -h <home id>

`node-samples` reads the temperature/consumption history of one node (`-s`/`-e` accept Unix seconds or ISO-8601 and default to one hour around now; `--csv` prints the rows as CSV):

    smartbox <auth options...> node-samples -d <device id> -n <node addr> [-s 2026-11-24T00:00:00Z] [-e 2026-11-24T01:00:00Z] [--csv]

Writes take named options, one per field to change. `--dry-run` performs the GET half and prints the POST it would send; interactive runs ask for confirmation unless `--yes` is given (non-interactive runs skip the prompt):

    smartbox <auth options...> set-status -d <device id> -n <node addr> [--mode auto] [--stemp 21.5 --units C] [--locked false]
    smartbox <auth options...> set-control-mode -d <device id> -n <node addr> --mode "PID (15 min. period)"
    smartbox <auth options...> set-htr-mod-status -d <device id> -n <node addr> [--on true] [--selected-temp comfort] [--comfort-temp 21.5] [--eco-offset -2.0] [--units C]
    smartbox <auth options...> set-setup  -d <device id> -n <node addr> [--offset 0.0] [--units C] [--true-radiant-enabled true] [--window-mode-enabled false] [--priority low] [--extra-json @file.json]
    smartbox <auth options...> set-prog -d <device id> -n <node addr> @prog.json
    smartbox <auth options...> set-device-away-status -d <device id> --away/--no-away
    smartbox <auth options...> set-device-power-limit -d <device id> <watts>

    smartbox <auth options...> set-node-power-limit -d <device id> -n <node addr> <watts>
    smartbox <auth options...> set-boost -d <device id> -n <node addr> --on     # or --off
    smartbox <auth options...> set-boost-legacy -d <device id> -n <node addr> --on --temperature 21 --time-minutes 60 --units C
    smartbox <auth options...> set-boost-mode -d <device id> -n <node addr> --active --mode runback --temperature 21 --time-minutes 60

`runback` and `advanced_slot` are additionally gated by the unit's advertised `boost_modes_available`; the CLI cannot verify that status gate before the write, so a rejected mode remains a server-side possibility. Fixed-time families accept only `max_temp_fixed_time`.

    smartbox <auth options...> set-easy    -d <device id> -n <node addr> --on
    smartbox <auth options...> set-runback -d <device id> -n <node addr> --on
    smartbox <auth options...> set-storage-mode    -d <device id> -n <node addr> --mode auto [--stemp 21.0 --units C]
    smartbox <auth options...> set-target-charge   -d <device id> -n <node addr> {0|100}
    smartbox <auth options...> set-storage-lock    -d <device id> -n <node addr> {0|1|2}
    smartbox <auth options...> set-storage-ui-mode -d <device id> -n <node addr> {0|1|2}
    smartbox <auth options...> set-charging-schedule -d <device id> -n <node addr> [--active-days 1110111] [--slot1 07:00-09:00] [--slot2 22:00-23:00]
    smartbox <auth options...> set-thermostat-prog -d <device id> -n <node addr> --units C --prog @thermostat.json
    smartbox <auth options...> set-prog-temps     -d <device id> -n <node addr> [--ice 5.0] [--eco 18.0] [--comfort 21.0] [--units C]
    smartbox <auth options...> set-max-stemp-limit -d <device id> -n <node addr> --units C 23
    smartbox <auth options...> set-max-stemp-limit -d <device id> -n <node addr> --off

`--prog`, `--extra-json` and the offline `--setup` accept a JSON literal or `@path` (`@-` reads stdin).

`control-modes` shows the vendor label and the wire index the heater stores (`control_mode` is the position in the product’s wire-order array, not the UI list position). `set-control-mode` accepts either the exact label or that index and refuses a value the product does not offer. It is the canonical control-mode command; it also applies the app-visibility gate: the setup must carry `control_mode`, and classic Elnur, water storage, `htr_mod` and NO_PROBES thermostats are refused. Writes print a JSON object with the method, path, body and `dry_run` flag.

The storage and htr_mod commands read `version.pid` first and refuse a product that does not support the write: `set-storage-ui-mode` needs an Elnur storage controller with that value offered and the current mode exposing the setting (the app hides the row once the unit is in Ecombi mode); `set-target-charge` needs an Elnur classic-accumulator mode; `set-storage-mode` needs an `acm` node that is not a classic accumulator, with a mode from `available_modes`; `set-storage-lock` needs an `acm` node; the charging schedule needs an `acm` node except water storage; legacy boost needs a non-Elnur `acm` (Helki storage); `set-htr-mod-status` needs code `0A` on any wire type, or `FF` on `htr_mod`; `set-easy`/`set-runback` use the controller EASY/RUNBACK gates; `set-boost` is the non-`acm` status-flag path (storage uses `set-boost-legacy`); `set-boost-mode` needs a non-`acm`/non-`thm` FULL controller off the legacy path, and fixed-time families only accept `max_temp_fixed_time`; `set-node-power-limit` requires `setup.power_limit`; `set-prog-temps` uses the programme-temperature gate; and `set-max-stemp-limit` uses the decoded max-stemp gate. `--dry-run` performs the same checks before printing the POST.

`set-control-mode` and `set-htr-mod-status` are the canonical commands for those two shapes; the generic `set-status`/`set-setup` commands cover the remaining status/setup fields.

`listen` opens a long-lived `ws_user` connection and prints `snapshot` and `update` events until interrupted (`-d`, `-n` and `--path` filter the updates):

    smartbox <auth options...> listen [-d <device id>] [-n <node addr>] [--path status]

## Package layout

- `smartbox.protocol` — auth, REST client, `ws_user` transport, wire values and named writes.
- `smartbox.core` — versioned state store, merges, write confirmation, deadlines and feed supervision.
- `smartbox.device` — the reviewed product-code table, neutral controller profiles, control modes, lock/boost policy, storage intents, setting gates and the operation-eligibility checks (several entity gates are shared with the HA adapter; the rest back the CLI).
- `smartbox.cli` — the optional debugging CLI.
- `smartbox.token_cache` — the CLI/tool file cache for the token bundle.

The canonical wire record lives in the sibling [`hass-smartbox`](https://github.com/jandado/hass-smartbox) repository under `docs/` (`api-endpoints.md`, `api-contract.md`, `api-ws-user.md`); [`webapi-spec.md`](./webapi-spec.md) in this repo is a pointer to them.

## Development

Prerequisites:

    uv
    python >=3.14.2

Clone the repo, install dependencies and install pre-commit hooks:

    git clone https://github.com/ajtudela/smartbox
    cd smartbox
    uv sync --all-extras --dev
    pre-commit install

Daily checks:

    uv run ruff check src/smartbox tests
    uv run mypy src/smartbox tests

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
* **New reseller** — your heater vendor is not in the built-in list. The template asks for `api-name`, `x-referer`, `x-serial-id` and the Basic Auth credential; to read them, open the reseller's web app with the network tab, log in and inspect the `POST .../api/v2/client/token` request headers. Once added, everyone using that vendor benefits.

For questions about the API itself rather than this library, see the wire record in the `hass-smartbox` repository (linked above).

### Supporting the maintainers

If this library is useful to you, you can buy the maintainers a coffee:

[![Buy a coffee to ajtudela][buymeacoffee-shield]][buymeacoffee-ajtudela]

[buymeacoffee-ajtudela]: https://www.buymeacoffee.com/ajtudela

[buymeacoffee-shield]: https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png

## Credits

The original library and the 2.7.x line are by Alberto Tudela, [Delmael](https://github.com/Delmael), graham33 and the upstream contributors. The 3.0 rewrite is by Jan Dado ([jandado](https://github.com/jandado)); upstream collaboration is welcome.
