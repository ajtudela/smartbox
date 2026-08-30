# API notes
Notes on the REST and socket.io endpoints used by this library, one entry per path: what it represents, its GET payload, and what POST does.

The GET payloads and POST behaviour below were exercised against a live device on 2026-08-30 (the `api-hjm` reseller, one device with a single `htr` node). Other resellers, firmwares and node types may differ. "untested" marks a request that was not made from here. Observed error bodies: `{"error": {"code": 2}}` means the route/method does not exist, `{"error": {"code": 5}}` means the route exists but rejected the body, and an Express-style `{"statusCode": 404, "message": "Cannot POST ..."}` means the path has no POST handler at all.

# REST API

## Auth

### Basic Auth Credential
Initial authentication to the smartbox REST API is protected by HTTP Basic Auth, in addition to the user's username and password which are then used to obtain an access token. In order not to undermine the security layer it provides, and also because it might change over time or vary between implementations, **the token is not provided here and system owners need to find it themselves**.

#### Capturing it from the reseller's web app

This is the easiest route and needs no extra tools.

1. Open your reseller's web app (its `web_url`, e.g. `https://app.helki.com/`) in a desktop browser and open the developer tools (`F12`) on the **Network** tab. Tick "Preserve log".
2. Log in with your normal username and password.
3. In the network list find the `POST` request to `.../api/v2/client/token` (filter by `token`). Select it and look at **Request Headers**:
   * `authorization: Basic <base64string>` — everything after `Basic ` (the `<base64string>`) is the credential. Pass it **verbatim**, still base64-encoded, as `-b`/`--basic-auth-creds` or `SMARTBOX_BASIC_AUTH_CREDS`. (It decodes to `client_id:client_secret`, but the library does not need it decoded.)
   * `x-serialid: <number>` — the value for `-i`/`--x-serial-id`.
   * `x-referer: <url>` — the value for `-r`/`--x-referer` (often the web app URL itself).
4. The request URL host is `api-<name>.helki.com`; the `api-<name>` part is your `-a`/`--api-name` (e.g. host `api-foo.helki.com` -> `api-foo`, host `api.helki.com` -> `api`). The same value appears as "API Host" under the app's *Version* menu.

#### Capturing it from the mobile app

Route the phone through an intercepting HTTPS proxy (mitmproxy, Charles, HTTP Toolkit) with its CA certificate trusted on the device, then trigger a login and read the same `client/token` request as above. These apps generally do not pin certificates, but a proxy that the OS/app rejects will show nothing — the web-app method is more reliable.

Treat all of these as secrets: the Basic Auth credential and a live `x-serialid` identify your reseller integration, and a captured `access_token` is valid for ~4 hours.

### /api/v2/client/token
Obtain (and refresh) the bearer access token.

POST: needs the basic auth token in the `Authorization` header plus the OAuth-style grant body (`grant_type=password` with `username`/`password`, or `grant_type=refresh_token`). Returns `{access_token, refresh_token, expires_in, token_type}`; the access token lasts about four hours. All the endpoints below need `Authorization: Bearer <access_token>`.

## Devices

### /api/v2/devs
List the devices the account can see.

GET: `{devs: [{dev_id, name, product_id, fw_version, serial_id}], invited_to: [...]}` (`invited_to` holds devices shared with the account by someone else).

### /api/v2/grouped_devs
List devices grouped into "homes".

GET: `[{id, name, devs: [{dev_id, name, product_id, fw_version, serial_id}], owner: bool, extraData}]`. `extraData` was `null` in the sample.

POST: not accepted (`Cannot POST /api/v2/grouped_devs`).

### /api/v2/devs/<dev_id>/dev_data
The whole device state in one payload: geolocation, away status, per-node `status`/`setup`/`version`, `htr_system` and `pmo_system`. This is what the socket `dev_data` event returns too.

GET: top-level keys `away_status`, `discovery`, `geoData`, `geo_data` (both the camelCase and snake_case copies are present), `htr_system` (`{power_limit, setup}`), `nodes` (list), `pmo_system`.

POST: not accepted (`Cannot POST /...`).

### /api/v2/devs/<dev_id>/geo_data
The device's geolocation (used for weather/timezone).

GET: `{country, state, city, tz_code, zip, coarsePosition: {latitude, longitude}}`.

POST: accepted (HTTP 201). Re-posting the current payload is a no-op; the fields to set a new location were not explored.

### /api/v2/devs/<dev_id>/connected
Whether the device is currently reachable by the cloud.

GET: `{connected: bool}`.

POST: not accepted (`Cannot POST /...`).

### /api/v2/devs/<dev_id>/mgr/away_status
Away mode for the whole device. `enabled`: away scheduling is on; `away`: currently in away mode; `forced`: away is held on manually rather than by schedule.

GET: `{enabled: bool, away: bool, forced: bool}`.

POST: accepted (HTTP 201), returns an empty body. Send only the fields being changed (this is what `set_device_away_status` does). `forced` is sticky: clearing it required posting `enabled: true` together with `forced: false`, a plain `{"away": false}` left `forced` set.

### /api/v2/devs/<dev_id>/mgr/discovery
Node discovery on the device (pairing new nodes).

GET: `{discovery: str}` — a short status string.

POST: route exists but an empty body is rejected with `{"error": {"code": 5}}`; the body that starts a discovery scan was not explored.

### /api/v2/devs/<dev_id>/htr_system/power_limit
The whole-device electrical power limit, in watts, as a string.

GET: `{power_limit: str}`.

POST: accepted (HTTP 201). Body `{"power_limit": "<watts>"}` (string), as sent by `set_device_power_limit`.

### /api/v2/devs/<dev_id>/htr_system/setup
The heater-system configuration: the same `power_limit` (here as an integer), the sampling `refresh_period` (seconds) and the extra-energy configuration. The library does not use this endpoint.

GET: `{power_limit: int, refresh_period: int, extra_nrg_conf: {enabled: bool}}`.

POST: untested.

### /api/v2/devs/<dev_id>/mgr/rtc/time
The device's real-time clock.

GET: `{y, n, d, h, m, s, w}`, all integers — year, month (`n`), day, hour, minute, second, weekday.

POST: not accepted (`{"error": {"code": 2}}`).

### /api/v2/devs/<dev_id>/mgr/version
Manager/system firmware version for the device (distinct from a node's `version`).

GET: `{fw_version, hw_version, product_id}`.

POST: untested.

### /api/v2/devs/<dev_id>/mgr/samples
Device-level history.

GET: not available — returns HTTP 404 with `{"error": {"code": 2}}`. Use the per-node `samples` endpoint instead.

## Nodes
`mgr/nodes` node objects carry `addr`, `type`, `name`, `installed`, `lost`, plus `uid`, `level`, `parent` and a nested `setup` (e.g. `{counter_offset}` for `htr`). Node `type` seen in the wild includes `htr`, `thm`, `acm`, `htr_mod`, `pmo`; only `htr` was exercised here.

### /api/v2/devs/<dev_id>/mgr/nodes
List the nodes attached to a device.

GET: `{nodes: [{addr, type, name, installed, lost, uid, level, parent, setup}]}`.

POST: untested.

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>
The whole state of a single node in one payload (its `status`, `setup`, ...). Per-node counterpart of `dev_data`.

GET: `{status: {...}, setup: {...}, ...}`.

POST: route exists but an empty body is rejected with `{"error": {"code": 5}}`; an aggregate write was not explored.

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/status
The node's live operating state: current/target temperature, mode, duty cycle, boost, lock, error code, presence and window flags.

GET (`htr`): `{sync_status, mode, active, ice_temp, eco_temp, comf_temp, units, stemp, mtemp, power, locked, duty, pcb_temp, presence, window_open, true_radiant_active, easy, runback, boost, boost_end_min, boost_end_day, error_code, version}`. `error_code` came back as an integer, and `act_duty`/`power_pcb_temp` were absent (see "Reseller / firmware variation").

POST: accepted (HTTP 201), returns an empty body. Send only the fields being changed (as `set_node_status` does); `units` must accompany any temperature field. `locked` round-trips cleanly.

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/prog
The node's weekly heating schedule: for each weekday, 24 hourly slots.

GET: `{sync_status, prog: {"0".."6": [24 ints]}}`.

POST: accepted (HTTP 201); re-posting the current schedule is a no-op. The library does not use this endpoint.

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/type
The node type as a bare JSON string (e.g. `"htr"`).

GET: the string, served with `Content-Type: text/html` rather than `application/json`.

POST: not accepted (`{"error": {"code": 2}}`).

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/version
The node's hardware/firmware identifiers.

GET: `{hw_version, fw_version, uid, pid}`.

POST: not accepted (`{"error": {"code": 2}}`).

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/setup
The node's configuration: control mode, units, offsets, away behaviour, window/true-radiant options and `factory_options`.

GET (`htr`): `{revision, sync_status, control_mode, units, power, offset, priority, away_mode, away_offset, modified_auto_span, window_mode_enabled, true_radiant_enabled, max_stemp_limit, factory_options: {duty_limit, operating_mode, power_factor, super_lock_available, temp_compensation_enabled, true_radiant_available, window_mode_available}}`.

POST: accepted (HTTP 201). The whole configuration must be re-sent even for unchanged fields, so `set_node_setup` reads the current setup and re-posts it merged with the changes.

### /api/v2/devs/<dev_id>/<node_type>/<node_addr>/samples
Per-node history of temperature and energy counter samples.

GET: `{samples: [{t, counter, temp}, ...]}`. `start` and `end` unix-timestamp query params are required — without them the response is HTTP 400 `{"statusCode": 400, "message": ["start must be a number string", "end must be a number string"], "error": "Bad Request"}`. An empty list is returned when the window has no data.

POST: not accepted (`{"error": {"code": 2}}`).

### /api/v2/devs/<dev_id>/pmo/<node_addr>/power_limit
The power limit of a `pmo` (power monitor) node — the per-node counterpart of `htr_system/power_limit`. Unverified: the only device available for testing has no `pmo` node. `get_device_power_limit`/`set_device_power_limit` use this path (read and write both on `power_limit`, mirroring `htr_system`); confirm the resource name against a real `pmo` device.

## Reseller / firmware variation
The `api-hjm` `htr` `status` and `setup` payloads above diverge from the fields the Pydantic models declare: `status.error_code` is an integer (not a string), `status` omits `act_duty` and `power_pcb_temp` and adds `easy`, `runback`, `version`; `setup` omits `flash_version`, `user_duty_factor` and `extra_options`, adds `revision`, `priority`, `max_stemp_limit`, and its `factory_options` object has a different, smaller set of keys. Treat every response model as a lower bound on what a given reseller returns.

## Endpoint discovery notes
A ~200-path GET sweep (account, group, device, `mgr/*`, `<x>_system/*` and node subpaths, common names like `energy`, `consumption`, `weather`, `notifications`, `alarms`, `schedule`, `firmware`, `capabilities`, `history`, `stats`, `network`, `wifi`, `me`, `account`, `users`, ...) turned up nothing beyond the paths listed here plus `htr_system/setup`. The REST surface appears to be small and closely mirrors the `dev_data` structure. Two quirks: `OPTIONS` on any path returns `204` with a blanket `Allow: GET,HEAD,PUT,PATCH,POST,DELETE` (a global CORS handler, not a real per-route method map), and path segments after `.../setup` are ignored (`.../setup/anything` returns the full setup), so neither is a reliable discovery signal. `pmo_system`/`acm_system`/`thm_system` URL prefixes 404 on this device, which only has an `htr` node.

## Misc

### /health_check
Liveness probe for the API (no auth needed).

GET: `{message: str}`.

### /version
API build info (no auth needed).

GET: `{major, minor, subminor, commit}`.

# Websocket API
This uses the [socket.io] protocol.

Briefly:
* The socket session is per device
* The access token and device ID must be supplied as query params
* On successful connection, the client should emit a `dev_data` event. The corresponding response from the server is similar to the dev_data REST endpoint above
* The server will send periodic `update` events containing device and node status updates
* The client should send a `ping` message every 20s (in addition to the protocol level ping/pong). Have not tested that this is strictly necessary.

## `update` Messages

### Node Status - `/<node type>/<node addr>/status`
Similar to the node status API endpoints above, one per node.

### Device Away Status - `/mgr/away_status`
Message content is the same structure as the `away_status` device API, or the corresponding field in the `dev_data` message or REST endpoint.

[socket.io]: https://socket.io/
