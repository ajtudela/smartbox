# smartbox ![build](https://github.com/graham33/smartbox/workflows/Python%20package/badge.svg) [![PyPI version](https://badge.fury.io/py/smartbox.svg)](https://badge.fury.io/py/smartbox) [![codecov](https://codecov.io/gh/graham33/smartbox/branch/main/graph/badge.svg?token=BMCS2E9OC7)](https://codecov.io/gh/graham33/smartbox) [![PyPI license](https://img.shields.io/pypi/l/smartbox.svg)](https://pypi.python.org/pypi/smartbox/) [![PyPI pyversions](https://img.shields.io/pypi/pyversions/smartbox.svg)](https://pypi.python.org/pypi/smartbox/)

Python API to control heating 'smart boxes'

## Installation

Install using pip from [PyPI](https://pypi.python.org/pypi/smartbox/):

    pip install smartbox

## `smartbox` Command Line Tool
You can use the `smartbox` tool to get status information from your heaters
(nodes) and change settings.

A few common options are required for all commands:
 * `-a`/`--api-name`: The API name for your heater vendor. This is visible in
  the 'API Host' entry in the 'Version' menu item in the mobile app/web app. If
  the host name is of the form `api-foo.xxxx` or `api.xxxx` use the values
  `api-foo` or `api` respectively.
* `-u`/`--username`: Your username as used for the mobile app/web app.
* `-p`/`--password`: Your password as used for the mobile app/web app.
* `-b`/`--base-auth-creds`: An HTTP Basic Auth credential used to do initial
  authentication with the server. Use the base64 encoded string directly. See
  'Basic Auth Credential' section below for more details.

Verbose logging can be enabled with the `-v`/`--verbose` flag.

### Listing smartbox devices

    smartbox <auth options...> devices

### Listing smartbox nodes
The `nodes` command lists nodes across all devices.

    smartbox <auth options...> nodes

### Getting node status
The `status` command lists status across all nodes and devices.

    smartbox <auth options...> status

### Setting node status
The `set-status` command can be used to change a status item on a particular
node.

    smartbox <auth options...> set-status <-d/--device-id> <device id> <-n/--node-addr> <node> <name>=<value> [<name>=<value> ...]

### Getting node setup
The `setup` command lists setup across all nodes and devices.

    smartbox <auth options...> setup

### Setting node setup
The `set-setup` command can be used to change a setup item on a particular
node.

    smartbox <auth options...> set-setup <-d/--device-id> <device id> <-n/--node-addr> <node> <name>=<value> [<name>=<value> ...]

### Getting device away status
The `device-away-status` command lists the away status across all devices.

    smartbox <auth options...> device-away-status

### Setting device away status
The `set-device-away-status` command can be used to change the away status on a
particular device.

    smartbox <auth options...> set-device-away-status <-d/--device-id> <device id> <name>=<value> [<name>=<value> ...]

### Getting device power limit
The `device-power-limit` command lists the power limit (in watts) across all
devices.

    smartbox <auth options...> device-power-limit

### Setting device power limit
The `set-device-power-limit` command can be used to change the power limit (in
watts) on a particular device.

    smartbox <auth options...> set-device-power-limit <-d/--device-id> <device id> <limit>

## Basic Auth Credential
Initial authentication to the smartbox REST API is protected by HTTP Basic Auth,
in addition to the user's username and password which are then used to obtain an
access token. In order not to undermine the security layer it provides, and also
because it might change over time or vary between implementations, **the token
is not provided here and system owners need to find it themselves**.

See [api-notes.md](./api-notes.md) for notes on REST and socket.io endpoints.
