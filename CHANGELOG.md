# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/) and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.7.0] - 2026-08-30

Large hardening pass over resource lifecycle, the exception hierarchy, model tolerance and the socket loop, plus a `.env`-driven CLI and new read endpoints.

### Added
- CLI: every auth option resolves from a `SMARTBOX_*` environment variable and from a `.env` file (searched upward from the working directory). New `smartbox.cmd.cli` entry point (`smartbox` console script now points at it) and a `.env.example`.
- `AsyncSmartboxSession.get_node_prog()`, `get_device_version()` and `get_htr_system_setup()` with the models `NodeProg`, `DeviceVersion`, `HtrSystemSetup`, and CLI commands `node-prog`, `device-version`, `htr-system-setup`.
- `AsyncSession` `timeout` constructor parameter (default: `aiohttp.ClientTimeout(total=30)`).
- Transient-failure retry in `_api_request`/`_api_post`: `retry_attempts` and `backoff_factor` are now honoured (connection errors and `429`/`5xx` retried with exponential backoff, numeric `Retry-After` respected).
- `AsyncSession.close()` as a public counterpart to the async context manager.
- `tests/test_socket.py`: first tests for `socket.py` (`on_update` gating, reconnect backoff, re-entry guard, idempotent shutdown).
- `network` pytest marker; `test_all_resellers` is deselected by default (`pytest -m network` to run it).
- README "Library usage" section with `AsyncSmartboxSession` and `UpdateManager` examples.

### Changed
- `get_node_status`/`get_node_setup` pick the model from `node["type"]` (via `_STATUS_MODELS`) instead of by the response shape, and return the concrete type-specific model (`HtrNodeStatus`, `HtrModNodeStatus`, `AcmNodeStatus`, `PmoSetup`, `DefaultNodeSetup`).
- All status/setup models are lenient: every field optional, `model_config = ConfigDict(extra="allow")`, and `DefaultNodeStatus.error_code` accepts `str | int`.
- `Node.type` is `SmartboxNodeType | str`: an unrecognised node type is kept verbatim instead of raising.
- Exception hierarchy: `InvalidAuthError`, `APIUnavailableError` and `ResellerNotExistError` now derive from `SmartboxError`; a `401`/`403` on a data request raises `InvalidAuthError` instead of a generic `SmartboxError`. `APIUnavailableError` keeps `aiohttp.ClientConnectionError` as a secondary base for now.
- `check_refresh_auth` is serialised with an `asyncio.Lock` and re-checks the condition inside it; a rejected refresh token falls back to the password grant.
- `set_node_setup` reads the current setup with a raw request, so fields the models do not declare survive the read-modify-write and are not wiped on the device.
- `get_device_power_limit` reads a `pmo` node's limit from `power_limit` (was `power`, mismatching the write path) and parses it with `int(float(...))`.
- `get_node_samples` `start_time`/`end_time` default to `None`; the window is computed per call instead of being frozen at import.
- `__aexit__` only closes the `ClientSession` when the session created it; an injected `websession` is left open.
- `AvailableResellers` resolves the reseller in `__init__`, so a bad configuration fails at construction.
- `SocketSession`: `_ping_task`/`_loop_should_exit`/`_running` initialised in `__init__`; `shutdown()` is the single idempotent teardown and `cancel()` is an alias; `run()` refuses a concurrent re-entry; a connection that ends sooner than `_MIN_SESSION_SECONDS` backs off instead of reconnecting immediately.
- `UpdateManager`: `subscribe_to_node_status`/`_setup`/`_version` share `_subscribe_to_node_field`.
- README CLI section rewritten (correct option names, `set-status`/`set-setup` shown with their named options, previously undocumented commands listed, `node-samples` moved under a read heading, prerequisites aligned to Python 3.14.2). `api-notes.md` rewritten from live probing of the REST API, including POST behaviour and one newly documented endpoint (`htr_system/setup`).
- CI runs `ruff` and `mypy` over `tests/` as well as `src/smartbox`; `codecov.yml` no longer ignores `src/smartbox/socket.py`.
- Dependencies pinned: `aiohttp>=3,<4`, `pydantic>=2,<3`, `python-socketio>=5,<6`. `ruff` `target-version` set to `py314`.

### Removed
- `requests` and `websocket-client` runtime dependencies (imported nowhere).
- Dead `[tool.setuptools.package-data]`, `[tool.setuptools.packages.find]` and `[tool.uv.workspace]` sections from `pyproject.toml`; the unused `person` test fixture.
- The one-tuple wrapper idiom and its `# type: ignore` in `update_manager.py`; the dead `_socket` branch in `AsyncSession.__aexit__`.

### Fixed
- `SmartboxAPIV2Namespace._dev_data` no longer emits `dev_data` on a disconnected namespace (missing `return`).
- The reconnect loop used `_LOGGER.exception` with no active exception, appending a bogus `NoneType: None` traceback; it now logs a warning.
- The CLI closes its `ClientSession` on context teardown (`ctx.call_on_close`); an unknown `-d`/`-n` raises `click.BadParameter` with the offending value instead of an unhandled `StopIteration`.
- `UpdateManager._update_cb` logs a missing `path` once per message, not once per subscription.
- `node-samples` / `set-status` / `set-setup` CLI commands annotate `node_addr` as `int` to match the `-n` option.
- Swapped `Sample` / `PmoSample` docstrings; `NodeSetup`/`NodeStatus.__getattr__` annotated `-> Any`.

### Security
- The access token and the reseller Basic Auth credential are no longer written to the DEBUG logs.

## [2.6.0]

Superseded by 2.7.0 before release (the `.env` / environment-variable CLI support landed here and is described above).

## Earlier releases

For versions up to and including 2.5.2, see the [GitHub releases](https://github.com/ajtudela/smartbox/releases) and tags.

## [0.0.5] - alpha

### Features
- Rename `away_status` to `device_away_status`
- Update API docs
- Add tox and flake8 on GitHub Actions

### Bug Fixes
- Pin dependency of `python-socketio` to match server

## [0.0.4] - alpha

### Features
- Refactor socket session and implement reconnect
- Add note on basic auth credentials

## [0.0.3] - alpha

### Features
- Fixed packaging

### Bug Fixes
- Fixed disconnect handling on token refresh

## [0.0.2] - alpha

### Features
- Added `get_api_name` function
