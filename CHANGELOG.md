# Changelog

## Unreleased

### New
* `Samples.counter_increments()`: per-interval consumption increments
  with counter-reset detection (a drop is a reset, never negative
  consumption); the raw `counter` values are untouched.
* `smartbox.retry.jittered()`: shared jitter helper for retry delays.

### Changed
* REST GET retries now use capped (10 s) exponential backoff with up to
  +20 % jitter, so clients do not retry in lockstep after a reseller
  outage. `ws_user` reuses the same helper.
* `Node`, `NodeVersion`, `Device`, `Home` and `DeviceAwayStatus` keep
  unknown keys (`extra="allow"`) so new reseller fields are not lost.

### Docs
* README documents why the OAuth password grant is used and how
  credentials are handled.

## 2.7.0

### New
* Per-user `ws_user` websocket transport (`WsUserSocketSession`,
  `check_ws_user_support`): one connection serves every device of the
  account, replacing N per-device `socket_io` sessions. Wire protocol
  and every failure mode verified live (api-notes.md, "Transports —
  socket_io vs ws_user"). Selected via the new `ws_user_socket=` kwarg
  on `UpdateManager`; the default transport is unchanged.
* `WsUserUnsupportedError` + the fallback doctrine: only deterministic
  handshake rejections (HTTP 401/403/404/410 in the one-shot probe on a
  fresh token; 404/410 in the run loop, or 401/403 that keep rejecting
  freshly minted tokens) signal "endpoint unsupported"; transient
  trouble is retried and never demotes.
* In `ws_user` mode `UpdateManager` ignores the SocketSession
  pass-through kwargs (warned at construction) — the shared socket's
  liveness is heartbeat-driven, not knob-driven.

### Breaking
* `UpdateManager.socket_session` now returns `None` when the manager
  runs in `ws_user` mode (previously always a `SocketSession`); use the
  new `ws_user_socket` accessor to distinguish modes.

### Features
* CLI: every authentication option also resolves from a `SMARTBOX_*`
  environment variable, and the `smartbox` console script loads a `.env`
  file (searched upward from the working directory) before parsing
  options. Precedence: command line > shell environment > `.env`. See
  `.env.example`.

### Fixes
* CLI: `.env` parsing now follows python-dotenv semantics — an inline
  `#` starts a comment only when it is preceded by whitespace and lies
  outside quotes, and quoted values unquote even when a trailing
  comment follows them. Previously inline comments silently became
  part of the value and quote-stripping was defeated, corrupting
  credentials written in the conventional quoted style.

### Refactor
* `UpdateManager`: `subscribe_to_node_status`/`_setup`/`_version`/`_prog`
  share one `_subscribe_to_node_key` implementation.

### Tests & CI
* Socket tests for the namespace callbacks, websocket cleanup, SIGINT
  handler and cancellation during connect; coverage floor of 95%
  (`fail_under`) enforced.
* ruff and mypy run over `tests/` in CI; `socket.py` is no longer
  excluded from codecov.

### Docs
* README rewritten: library usage, command table, `.env` section and an
  actionable Support section. api-notes documents how to capture the
  Basic Auth credential and the new-reseller issue template points to it.

## 2.6.2

Port round of upstream PR #64 (ajtudela, `improvements/fix_bugs`, 19
commits) — full per-commit audit, verdicts and evidence in
ajtudela-pr-64-audit.md. Ten commits were already fixed by the 2.6.x
round (independent convergence); of the remainder, seven were ported
(each commit references the upstream source hash), one was declined by
decision (#7: typed-mode sparse statuses keep the rich
`SmartboxValidationError` rejection — the bare `{"sync_status": "lost"}`
dead-node frame is first-class there), and one is upstream-only (#18,
version bump).

### Bug Fixes
* `set_node_setup` never serializes the read-modify-write GET through
  the Pydantic models: the internal read is always raw, so keys the
  setup models do not declare (e.g. `counter_offset` on pmo nodes) can
  no longer be wiped on the device by a setup write (upstream `5618aa3`).
  Completed by `extra="allow"` on `DefaultNodeSetup`/`PmoSetup`, so
  typed (`raw_response=False`) reads keep unknown keys and round-trip
  losslessly (upstream `5618aa3`, completion by decision).
* `InvalidAuthError`, `APIUnavailableError` and `ResellerNotExistError`
  now derive from `SmartboxError` — one `except SmartboxError` catches
  every library failure. `APIUnavailableError` keeps its
  `aiohttp.ClientConnectionError` base for backwards compatibility; the
  data-path 403 → `SmartboxError` mapping is kept deliberately
  (upstream `62efcf8`).
* CLI: unknown `-d`/`-n` values raise `click.BadParameter` with a
  `param_hint` (exit code 2, usage line, offending option named)
  instead of `ClickException`; malformed wire node payloads stay
  `ClickException` (upstream `6ad2014`).

### Build & Docs
* Core dependencies bounded to their current major: `aiohttp>=3,<4`,
  `pydantic>=2,<3`, `python-socketio>=5,<6` (upstream `da3bb9c`; the
  commit's unused-dependency removal was already done in 2.6.0).
* `.vscode/settings.json`: pylint/flake8 editor settings replaced with
  the ruff formatter/code-actions setup (upstream `a78bc3a`; its
  pyproject/ruff halves were already done in 2.6.0).
* CODEOWNERS co-owner and README badge reflow adopted from upstream
  `2ec1ef0`.

### Tests
* `test_all_resellers` (real HTTP calls to twelve reseller servers)
  moved behind a `network` marker, deselected by default; run on demand
  with `pytest -m network`. Sessions in it are closed (upstream
  `0bef64c`).

## 2.6.1

### Features
* Node reachability / write-confirmation tracking on `UpdateManager`:
  `subscribe_to_node_availability` / `get_node_availability` report a
  node Available/Unavailable from its status/setup frames — a bare
  `{"sync_status": "lost"}` frame arms a uniform 6 s grace timer
  (`unavailable_delay` knob, matches the vendor app's unreachable
  window); any ok frame reports Available again. `expect_write_confirmation`
  (knob `write_confirm_timeout`, 6 s) arms a window after an accepted
  write: if no ok frame arrives, a one-shot discriminating GET decides
  "applied (node alive)" vs "silently dropped (node unreachable)" —
  writes to unreachable nodes are ACKed OK with no device ack (observed
  live). Frame taps register lazily; both features share them, each
  enables them independently. Backed by live probes 2026-09-26/30
  (api-notes.md, "Node reachability").

## 2.6.0

### Features
* Full node programme (schedule) support: `get_node_prog` / `set_node_prog`
  (+ typed `NodeProg` model, sync `Session.get_prog` / `set_prog` wrappers,
  `subscribe_to_node_prog` websocket helper and `prog` / `set-prog` CLI
  commands). Wire note: GET and POST both use the day-keyed object shape;
  the array shape is rejected with 400 (observed live).
* Full management/fleet surface per webapi-spec §2–3: device discovery
  GET/POST, device rtc/time, device/node/group rename, device/node delete,
  device group move, node select/power, group geo_data/extra_data, invite
  flows (invite/revoke/confirm — confirm is unauthenticated), quiet-home
  notification config GET/POST + test, encrypted-wifi-credentials and
  geocoding helpers. Read-only-verified paths pinned from the official
  vendor app; all writes marked **unverified on live hardware** in docstrings.
  New base helpers: `_api_delete`, host-relative `_api_get` (params not
  logged — may carry secrets) and `_api_post_path` (auth optional).
* Away-status writes default `enabled: true` to match the vendor app shape
  (`{away, enabled}`); the server also honours `{away}` alone (D1 verified
  live).
* Setup models widened for the fw-1.9 htr family: optional
  `user_duty_factor` / `flash_version` / `extra_options`; new
  `max_stemp_limit` / `priority` / `revision` and
  `factory_options.operating_mode` / `power_factor` / `super_lock_available`.
  Its `factory_options` carries no `prog_resolution` (schedule resolution
  not readable from setup on this family).

### Bug Fixes
* `SocketSession.cancel()` is now bounded by a timeout (no more unbounded
  hangs on a wedged websocket) and disconnects exactly once (cancel +
  shutdown no longer double-disconnect).
* The websocket loop checks the exit flag before touching the REST API, so
  cancel is no longer delayed by a slow auth refresh.
* Dangling background cleanup tasks are awaited (bounded) on shutdown
  instead of dying as "Task was destroyed but it is pending" noise.
* Routine retry log downgraded from `_LOGGER.exception` to `_LOGGER.warning`.
* Status models widened for the newer htr firmware family (`error_code`
  str|int; optional `act_duty` / `power_pcb_temp`); optional fields
  `easy` / `runback` / `version` tolerated.

* Code-review round 3 (2026-09-27): request-timeouts and HTTP 5xx now map
  to `APIUnavailableError` (aiohttp's total-timeout raises a plain
  `TimeoutError` that previously escaped all error handling; 5xx GETs are
  retried via the existing retry wrapper — 4xx still raises
  `SmartboxError`); request bodies are redacted in debug logs (the
  invite-confirmation `pass` leaked in plaintext); subscription user
  callbacks are isolated — one raising callback can no longer starve the
  other subscriptions or propagate into the socket loop, and jq-emitted
  `null` node payloads no longer fire callbacks; the socket reconnect
  backoff is capped at 30s per attempt (previously up to ~8.5 min of
  futile retries before the token refresh that usually fixes the
  disconnect); `_attempt_connection` also tolerates transient
  `TimeoutError`/`OSError` from the websocket transport instead of
  killing the run loop; the ping task is cancelled **and** reaped
  (bounded) on cancel/shutdown; `get_device_power_limit` raises
  `SmartboxError` instead of `KeyError` on an unexpected payload;
  sync `Session` annotations aligned with the async surface (`set_setup`
  → `None`, `get_status`/`get_setup`/`get_prog` may return models);
  CLI: non-numeric node addresses give a clean error, `resellers` prints
  a readable list without credentials, and `-u`/`-p` accept the
  `SMARTBOX_USERNAME`/`SMARTBOX_PASSWORD` env vars; `__version__` no
  longer explodes when imported uninstalled.

* Code-review round 4 (2026-09-27): sync `Session` no longer fails on the
  second consecutive call with "RuntimeError: Session is closed" (the
  per-call cleanup now forgets the closed client session so the next
  `asyncio.run` recreates it — regression test drives the real client
  through a local HTTP server); sync `get_device_power_limit` /
  `set_device_power_limit` regain the async `node` parameter (pmo devices
  were unreachable from the sync API) and sync wrappers were added for
  `health_check`, `api_version`, `get_device_connected`,
  `get_node_samples` and `get_node_version`; CLI `-a/--api-name` defaults
  to `api` and session/API failures (auth, network, unknown reseller)
  surface as clean `ClickException` errors instead of tracebacks;
  `set_node_prog` rejects array-shape / `prog`-less payloads with a clear
  error instead of an `AttributeError` traceback or a silent no-op POST
  (CLI guard included); a token endpoint 200 with a non-JSON body raises
  `SmartboxError` instead of `InvalidAuthError` (it must not trigger the
  consumer's reauth flow); `SocketSession.run` authenticates before the
  first connect attempt (a cold CLI session no longer burns its
  reconnect budget on an empty token); the CLI SIGINT handler is removed
  at teardown and platforms without signal-handler support no longer die
  on connect; package exports completed (`NodeVersion` and the remaining
  public model types).

* Code-review round 5 (2026-09-27) — **contract changes**:
  * `raw_response=True` no longer validates responses: `get_devices`,
    `get_homes`, `get_grouped_devices`, `get_home_guests`,
    `get_device_connected` and `get_device_away_status` return the wire
    payload (with only the documented list extraction, e.g. `devs` +
    `invited_to` flattened) instead of model-serialised dicts. Unknown
    keys are kept (live: `grouped_devs` carries `extraData`, previously
    dropped); a payload too malformed to extract from raises
    `SmartboxError`. All raw getters now follow the same rule.
  * New `SmartboxValidationError(SmartboxError)`, exported: with
    `raw_response=False`, payload drift raises it (chained from the
    pydantic error) instead of a bare `pydantic.ValidationError`.
  * Every library call is bounded by a 30s whole-call deadline covering
    token refresh, retries and backoff — also on caller-provided
    websessions (previously aiohttp's 300s default applied there, and 5
    retries could stretch one call to minutes). Expiry raises
    `APIUnavailableError`; a backoff that would overrun it is skipped.
  * A rejected refresh token falls back to one password login before
    `InvalidAuthError` is raised; a mid-session 401 invalidates the
    token and retries the request once after re-authenticating.
* Code-review round 5 — reliability fixes:
  * The websocket loop survives transient auth-refresh failures
    (`APIUnavailableError`/`SmartboxError` are waited out with capped
    backoff; previously one network blip ended `run()` for good); only
    rejected credentials end it.
  * `cancel()` during reconnect backoff stops the reconnect loop
    immediately (it used to keep attempting), and a connect completing
    after `cancel()` is disconnected instead of leaking a live socket
    that kept `run()` alive.
  * The keepalive ping task survives a failed send (it died for the
    rest of the session on one `BadNamespaceError`).
  * Secrets: the INFO "Successfully connected" log no longer prints the
    access token; error messages and logs no longer embed request query
    strings (aiohttp's exception text leaked the wifi password of
    `get_encrypted_wifi_credentials`).
  * Truncated response bodies (`ClientPayloadError`) map to
    `APIUnavailableError` (retried on GETs) and undecodable JSON to
    `SmartboxError` — both previously escaped the library's error types.
  * `retry_attempts=0` makes one attempt instead of raising `TypeError`.
  * Update subscriptions anchor their paths (`/prog` no longer matches
    `/prog_temps` frames, likewise status/setup/version).
  * Hygiene: 45 unneeded `type: ignore`s removed, sync
    `Session.set_device_away_status` annotated `-> None`, dead
    `_basic_auth_credentials` attribute removed, `socket.py` imports
    `urllib.parse` explicitly.

* Code-review round 6 (2026-09-27, every finding reproduced by a probe
  before fixing): the token endpoint (`/client/token`) maps truncated
  bodies (`ClientPayloadError`) and undecodable-JSON 200s to
  `APIUnavailableError` / `SmartboxError` like the REST path — a
  truncated token body previously escaped raw and **ended the websocket
  loop for good** (end-to-end reproduced); `SocketSession` rejects
  `ping_interval <= 0` (hot ping loop) and `reconnect_attempts < 1`
  (endless refresh churn) with a `ValueError` at construction; the CLI
  `set-status` command gives a clean error for `--stemp` without
  `--units` instead of a raw `ValueError` traceback; the token endpoint
  treats HTTP 429 as transient (`APIUnavailableError`) instead of bad
  credentials — rate limiting no longer triggers the password-login
  fallback, the consumer's reauth flow, or the end of the websocket
  loop (400/401 rejections still surface as `InvalidAuthError`).

* Code-review round 6 follow-up (2026-09-27, **breaking** — 2.6.0 is the
  alignment window; every fix probe-verified): `set_node_prog` raises
  `ValueError` for a `prog`-less / array-shaped payload (was `TypeError`
  — now aligned with `set_node_status`'s input validation); the sync
  `Session` wrapper return annotations reflect `raw_response=False`
  (e.g. `get_devices` may return a `Devices` model, `get_nodes` a
  `list[Node]`); the sync `Session` no longer leaks "coroutine was never
  awaited" `RuntimeWarning`s when misused inside a running event loop
  (the pre-built coroutines are closed before re-raising); socket
  teardown reaps cancelled disconnect tasks and cancelled pending
  background tasks (bounded) — no more "Task was destroyed but it is
  pending" noise in edge teardown timing.

* Dependency refresh (2026-09-27, all gates + live socket round green):
  runtime aiohttp 3.14.0→3.14.3, pydantic 2.13.4→2.13.5,
  python-socketio 5.16.2→5.17.0 (+engineio 4.14.0, bidict), asyncclick
  8.3.0.7→8.4.2.1, jq 1.11.0→1.12.0; dev pytest 9.1.1, ruff 0.16.9,
  mypy 2.3.1, time-machine 3.5.1, tox 4.64.3, pytest-randomly 5.0.0.
* Removed dead dependencies `requests` and `websocket-client` (imported
  nowhere; legacy from the pre-asyncio era — the websocket path uses
  python-socketio/simple-websocket, HTTP uses aiohttp).
* Lint config: ruff `target-version` aligned to the runtime floor
  (py313 → py314); disabled CPY001 (no copyright headers in this
  project) and PLR0917 (positional variant of the already-ignored
  PLR0913); dropped dead mypy override modules (`freezegun`, `test`);
  typing-only imports moved behind `TYPE_CHECKING` (ruff TC rules).

* Code-review round (2026-09-27, all gates + contract tests + live smoke
  green): caller-provided websessions are no longer closed on context
  exit (session-ownership model, `aclose_owned_session()`); REST requests
  get bounded timeouts (30s total / 10s connect) on library-created
  sessions; GETs retry on transient connection failures (uses the
  previously dead `retry_attempts`/`backoff_factor` constructor config);
  mid-session HTTP 401 now raises `InvalidAuthError` (consumers can
  trigger reauth); access token is masked in debug logs; sync `Session`
  closes its lazily-created client session per call (no more "Unclosed
  client session" warnings); CLI: clean `ClickException` errors for
  unknown device/node ids and invalid prog JSON, and fixed a latent bug
  where set-* commands could never resolve a node (string vs int address
  comparison); sample-window defaults are computed at call time (they
  were frozen at import and drifted in long-lived processes); docstring
  fixes (`Sample`/`PmoSample` were swapped, `subscribe_to_device_connected`
  claimed to be a power-limit hook); removed redundant field
  redeclarations in status subclasses and 1-tuple call hacks in the
  update manager; dropped dead `[tool.uv.workspace]` and
  `[tool.setuptools.*]` config.

* Code-review round 7 — final pre-release pass (2026-09-27, gates +
  contract tests green): `SocketSession` can now be constructed outside
  a running event loop (the REST websession binding moved from
  `__init__` to `run()`, with `external_http=True` so engineio never
  closes the session shared with REST traffic); the websocket run loop
  no longer dies when the first `dev_data` emit races the server drop
  (`BadNamespaceError` is a *sibling* of `ConnectionError` under
  `SocketIOError` — the catch family was widened to `SocketIOError`);
  a token-endpoint 200 with a wrong-shaped body raises `SmartboxError`
  instead of `InvalidAuthError` (consistent with the undecodable-JSON /
  non-JSON mapping: a server-side drift is not "credentials rejected");
  `AsyncSession` rejects a negative `backoff_factor` at construction
  (it previously silently disabled backoff); CLI node resolution gives
  a clean error on malformed wire payloads (missing/non-numeric `addr`)
  instead of a raw `KeyError`/`ValueError` traceback.

## 0.0.5 (alpha)

### Features
* Rename `away_status` to `device_away_status`
* Update API docs
* Add tox, tox and flake8 on github action

### Bug Fixes
* Pin dependency of python-socketio to match server

## 0.0.4 (alpha)

### Features
* Refactor socket session and implement reconnect
* Add note on basic auth credentials

## 0.0.3 (alpha)

### Features
* Fixed packaging

### Bug Fixes
* Fixed disconnect handling on token refresh

## 0.0.2 (alpha)

### Features
* Added `get_api_name` function
* Added basic tests for REST interactions
* Added token refresh support
* Added socket.io interface via `open_socket` function (no tests as yet)
* Added documentation for known REST and websocket endpoints

## 0.0.1 (alpha)

### Features
* Initial version supporting some REST endpoints
