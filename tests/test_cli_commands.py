"""Command wiring tests for the CLI read and write surfaces."""

from __future__ import annotations

import json
from unittest.mock import patch

from asyncclick.testing import CliRunner

from smartbox.cli import smartbox

NODES = [
    {"type": "htr", "addr": 2, "name": "Heater"},
    {"type": "acm", "addr": 1, "name": "Storage"},
    {"type": "thm", "addr": 3, "name": "Thermostat"},
    {"type": "htr_mod", "addr": 4, "name": "Module"},
    {"type": "acm", "addr": 5, "name": "Helki storage"},
    {"type": "htr", "addr": 6, "name": "Trio"},
    {"type": "htr", "addr": 7, "name": "WiFi"},
    {"type": "pmo", "addr": 8, "name": "Meter"},
    {"type": "htr", "addr": 9, "name": "Unknown"},
]
PIDS = {
    ("htr", "2"): "04020000",
    ("acm", "1"): "08140000",
    ("thm", "3"): "08040000",
    ("htr_mod", "4"): "080A0000",
    ("acm", "5"): "081C0000",
    ("htr", "6"): "081A0000",
    ("htr", "7"): "08340000",
    ("pmo", "8"): "0A030000",
    ("htr", "9"): "08990000",
}
STATUS = {
    "htr": {
        "units": "C",
        "mode": "auto",
        "stemp": "21.0",
        "sync_status": "ok",
        "boost": False,
        "active": True,
    },
    "acm": {
        "units": "C",
        "charging": True,
        "charge_level": 50,
        "target_charge_per": 80,
    },
    "thm": {"units": "C", "mtemp": "19.0", "stemp": "21.0"},
    "pmo": {"units": "C", "power": "100"},
    "htr_mod": {
        "units": "C",
        "mode": "manual",
        "selected_temp": "comfort",
        "comfort_temp": "21.0",
        "eco_offset": "-2.0",
        "on": True,
    },
}
SETUP = {
    "units": "C",
    "control_mode": 5,
    "max_stemp_limit": "23.0",
    "factory_options": {"ui_mode": 2, "boost_config": 3},
    "extra_options": {"user_duty_limit": 25},
    "charging_conf": {
        "active_days": [1] * 7,
        "slot_1": {"start": 420, "end": 540},
        "slot_2": {"start": 0, "end": 0},
    },
}


class FakeClient:
    last: FakeClient

    def __init__(self, _config, **_kwargs):
        self.posts: list[tuple[str, dict]] = []
        FakeClient.last = self

    async def get_devices(self):
        return {"devs": [{"dev_id": "dev1", "name": "Home"}], "invited_to": []}

    async def get_nodes(self, _dev_id):
        return NODES

    async def get_node_status(self, _dev_id, node):
        return dict(STATUS[node.node_type])

    async def get_node_setup(self, _dev_id, node):
        setup = json.loads(json.dumps(SETUP))
        if node.addr == "6":
            setup["power_limit"] = "1000"
        return setup

    async def get_node_prog(self, _dev_id, _node):
        return {"prog": {"0": [2] * 24}}

    async def get_node_version(self, _dev_id, node):
        return {
            "pid": PIDS[(node.node_type, node.addr)],
            "fw_version": "1.9",
            "hw_version": "1.0",
        }

    async def get_node_power(self, _dev_id, _node):
        return 123

    async def get_node_samples(self, _dev_id, _node, _start, _end):
        return [{"t": 1, "temp": "20.0"}, {"t": 2, "power": 100}]

    async def get_away_status(self, _dev_id):
        return {"away": False}

    async def get_connected(self, _dev_id):
        return {"connected": True}

    async def get_power_limit(self, _dev_id):
        return {"power_limit": "2000"}

    async def get_rtc_time(self, _dev_id):
        return {"time": "2026-11-24T00:00:00Z", "n": 10}

    async def get_discovery(self, _dev_id):
        return {"discovery": {}}

    async def get_dev_data(self, _dev_id):
        return {"dev_id": "dev1"}

    async def health_check(self):
        return {"status": "ok"}

    async def get_api_version(self):
        return {"version": "test"}

    async def post(self, path, body):
        self.posts.append((path, dict(body)))

    async def close(self):
        return None


async def _invoke(*args: str) -> tuple[object, FakeClient]:
    with patch("smartbox.cli.common.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--no-token-cache",
                "--yes",
                *args,
            ],
        )
    return result, FakeClient.last


def _json(result) -> object:
    return json.loads(result.output)


async def test_help_lists_the_new_surfaces():
    result = await CliRunner().invoke(smartbox, ["--help"])
    assert result.exit_code == 0
    for command in (
        "codes",
        "diagnose",
        "doctor",
        "node-profile",
        "node-version",
        "rtc",
        "set-easy",
        "set-storage-ui-mode",
        "set-thermostat-prog",
    ):
        assert command in result.output


async def test_status_filters_one_node_and_emits_one_json_document():
    result, _client = await _invoke("--json", "status", "-d", "dev1", "-n", "2")
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["dev1"]["name"] == "Home"
    assert payload["dev1"]["nodes"]["2"]["stemp"] == "21.0"
    assert "1" not in payload["dev1"]["nodes"]


async def test_node_version_and_power_reads():
    version, _client = await _invoke("node-version", "-d", "dev1", "-n", "2")
    assert version.exit_code == 0, version.output
    assert "04020000" in version.output
    power, _client = await _invoke("node-power", "-d", "dev1", "-n", "8")
    assert power.exit_code == 0, power.output
    assert "123" in power.output
    rejected, client = await _invoke("node-power", "-d", "dev1", "-n", "2")
    assert rejected.exit_code != 0
    assert "pmo-only" in rejected.output
    assert client.posts == []


async def test_device_reads_accept_a_device_filter():
    for command, needle in (
        ("device-away-status", "away"),
        ("device-connected-status", "connected"),
        ("device-power-limit", "power_limit"),
        ("rtc", "2026-11-24"),
        ("discovery", "discovery"),
        ("dev-data", "dev1"),
    ):
        result, _client = await _invoke(command, "-d", "dev1")
        assert result.exit_code == 0, (command, result.output)
        assert needle in result.output


async def test_node_profile_resolves_the_live_identity():
    result, _client = await _invoke("node-profile", "-d", "dev1", "-n", "2")
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["product"]["code"] == "02"
    assert payload["product"]["label"] == "Relay heater"
    assert payload["profile"]["climate_bounds"] == [1.0, 23.0]
    assert payload["status"]["units"] == "C"
    assert payload["issues"] == []


async def test_diagnose_covers_every_node_and_reports_issues():
    result, _client = await _invoke("--json", "diagnose", "-d", "dev1")
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert [row["node"]["addr"] for row in payload] == [2, 1, 3, 4, 5, 6, 7, 8, 9]
    assert all(row["product"] is not None for row in payload[:-1])
    assert payload[-1]["issues"] == ["product code 99 is not reviewed"]


async def test_codes_is_an_offline_table():
    result = await CliRunner().invoke(smartbox, ["codes", "--relay"])
    assert result.exit_code == 0, result.output
    assert "28" in result.output
    assert "01" not in result.output
    raw = await CliRunner().invoke(smartbox, ["codes", "--json", "--family", "storage"])
    assert raw.exit_code == 0, raw.output
    assert {row["code"] for row in _json(raw)} == {"1C", "2E"}
    elnur = await CliRunner().invoke(
        smartbox, ["codes", "--json", "--limits-family", "storage_elnur"]
    )
    assert elnur.exit_code == 0, elnur.output
    assert {row["code"] for row in _json(elnur)} == {"14", "20", "25"}
    thm = await CliRunner().invoke(smartbox, ["codes", "--json", "--thm"])
    assert thm.exit_code == 0, thm.output
    assert {row["code"] for row in _json(thm)} == {"28"}
    search = await CliRunner().invoke(
        smartbox, ["codes", "--json", "--search", "thermostatic"]
    )
    assert search.exit_code == 0, search.output
    assert {row["code"] for row in _json(search)} == {"0F"}
    renamed = await CliRunner().invoke(smartbox, ["profiles"])
    assert renamed.exit_code != 0
    assert "renamed to `codes`" in renamed.output
    config = await CliRunner().invoke(smartbox, ["config", "--help"])
    assert config.exit_code == 0, config.output


async def test_version_and_doctor():
    version = await CliRunner().invoke(smartbox, ["version"])
    assert version.exit_code == 0
    assert version.output.startswith("smartbox ")
    doctor, _client = await _invoke("doctor", "--json")
    assert doctor.exit_code == 0, doctor.output
    payload = _json(doctor)
    assert payload["username"] == "us…@example.test"
    assert payload["reseller"]
    assert str(payload["token_cache"]).endswith(".json")
    assert payload["token_cache_exists"] is False


async def test_doctor_works_without_credentials():
    result = await CliRunner().invoke(
        smartbox, ["--no-token-cache", "doctor", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["api_name"] == "api"
    assert payload["base_url"]
    assert payload["username"] == "-"


async def test_unknown_read_filters_fail_loudly():
    for args in (
        ("status", "-d", "nope"),
        ("status", "-n", "99"),
        ("devices", "-d", "nope"),
        ("nodes", "-d", "nope"),
        ("rtc", "-d", "nope"),
    ):
        result, _client = await _invoke(*args)
        assert result.exit_code != 0, args
        assert "no " in result.output.lower(), (args, result.output)


async def test_missing_writes_post_the_expected_paths_and_bodies():
    cases = [
        (
            ("set-easy", "-d", "dev1", "-n", "6", "--on"),
            "/htr/6/status",
            {"easy": True},
        ),
        (
            ("set-runback", "-d", "dev1", "-n", "6", "--off"),
            "/htr/6/status",
            {"runback": False},
        ),
        (
            (
                "set-storage-mode",
                "-d",
                "dev1",
                "-n",
                "5",
                "--mode",
                "manual",
                "--stemp",
                "21.0",
                "--units",
                "C",
            ),
            "/acm/5/mode",
            {"mode": "manual", "stemp": "21.0", "units": "C"},
        ),
        (
            ("set-target-charge", "-d", "dev1", "-n", "1", "100"),
            "/acm/1/status",
            {"target_charge_per": 100},
        ),
        (
            ("set-storage-lock", "-d", "dev1", "-n", "1", "2"),
            "/acm/1/lock",
            {"locked": 2},
        ),
        (
            (
                "set-storage-ui-mode",
                "-d",
                "dev1",
                "-n",
                "1",
                "2",
            ),
            "/acm/1/setup",
            {"factory_options": {"ui_mode": 2, "boost_config": 3}},
        ),
        (
            (
                "set-charging-schedule",
                "-d",
                "dev1",
                "-n",
                "5",
                "--active-days",
                "1110111",
                "--slot1",
                "07:00-09:00",
            ),
            "/acm/5/setup",
            {
                "charging_conf": {
                    "active_days": [1, 1, 1, 0, 1, 1, 1],
                    "slot_1": {"start": 420, "end": 540},
                    "slot_2": {"start": 0, "end": 0},
                }
            },
        ),
        (
            (
                "set-boost-legacy",
                "-d",
                "dev1",
                "-n",
                "5",
                "--on",
                "--temperature",
                "21",
                "--time-minutes",
                "60",
                "--units",
                "C",
            ),
            "/acm/5/boost",
            {
                "boost": True,
                "boost_time": 60,
                "stemp": "21.0",
                "units": "C",
            },
        ),
        (
            (
                "set-thermostat-prog",
                "-d",
                "dev1",
                "-n",
                "3",
                "--units",
                "C",
                "--prog",
                json.dumps({str(day): [{"m": 480, "t": "19.0"}] for day in range(7)}),
            ),
            "/thm/3/prog",
            {
                "prog": {str(day): [{"m": 480, "t": "19.0"}] for day in range(7)},
                "units": "C",
            },
        ),
        (
            (
                "set-prog-temps",
                "-d",
                "dev1",
                "-n",
                "2",
                "--comfort",
                "21.5",
                "--units",
                "C",
            ),
            "/htr/2/status",
            {"comf_temp": "21.5", "units": "C"},
        ),
        (
            (
                "set-max-stemp-limit",
                "-d",
                "dev1",
                "-n",
                "2",
                "--units",
                "C",
                "23",
            ),
            "/htr/2/setup",
            {"max_stemp_limit": "23.0"},
        ),
        (
            (
                "set-htr-mod-status",
                "-d",
                "dev1",
                "-n",
                "4",
                "--selected-temp",
                "comfort",
                "--comfort-temp",
                "22.0",
                "--units",
                "C",
            ),
            "/htr_mod/4/status",
            {"selected_temp": "comfort", "comfort_temp": "22.0"},
        ),
    ]
    for args, suffix, expected in cases:
        result, client = await _invoke(*args)
        assert result.exit_code == 0, (args, result.output)
        assert len(client.posts) == 1, args
        path, body = client.posts[0]
        assert path.endswith(suffix), (args, path)
        for key, value in expected.items():
            assert body[key] == value, (args, key, body)
    result, _client = await _invoke(
        "set-max-stemp-limit", "-d", "dev1", "-n", "2", "--off"
    )
    assert result.exit_code == 0, result.output


async def test_storage_and_htr_mod_commands_refuse_the_wrong_product():
    cases = [
        (
            ("set-storage-ui-mode", "-d", "dev1", "-n", "2", "2"),
            "Elnur storage",
        ),
        (
            ("set-storage-mode", "-d", "dev1", "-n", "2", "--mode", "auto"),
            "storage controller",
        ),
        (
            ("set-storage-mode", "-d", "dev1", "-n", "1", "--mode", "auto"),
            "target-charge",
        ),
        (
            ("set-storage-lock", "-d", "dev1", "-n", "2", "1"),
            "storage controller",
        ),
        (
            (
                "set-charging-schedule",
                "-d",
                "dev1",
                "-n",
                "2",
                "--active-days",
                "1111111",
            ),
            "storage controller",
        ),
        (
            (
                "set-boost-legacy",
                "-d",
                "dev1",
                "-n",
                "2",
                "--on",
                "--temperature",
                "21",
                "--time-minutes",
                "60",
                "--units",
                "C",
            ),
            "Helki storage",
        ),
        (
            (
                "set-htr-mod-status",
                "-d",
                "dev1",
                "-n",
                "2",
                "--selected-temp",
                "comfort",
            ),
            "htr_mod",
        ),
        (
            (
                "set-max-stemp-limit",
                "-d",
                "dev1",
                "-n",
                "1",
                "--units",
                "C",
                "23",
            ),
            "max-stemp",
        ),
    ]
    for args, message in cases:
        result, client = await _invoke(*args)
        assert result.exit_code != 0, (args, result.output)
        assert message in result.output, (args, result.output)
        assert client.posts == [], args


async def test_storage_ui_mode_rejects_an_unoffered_value():
    result, client = await _invoke("set-storage-ui-mode", "-d", "dev1", "-n", "1", "0")
    assert result.exit_code != 0
    assert "offers" in result.output
    assert client.posts == []


async def test_dry_run_prints_the_post_without_sending_it():
    result, client = await _invoke(
        "--dry-run", "set-easy", "-d", "dev1", "-n", "6", "--on"
    )
    assert result.exit_code == 0, result.output
    assert client.posts == []
    payload = _json(result)
    assert payload["dry_run"] is True
    assert payload["path"].endswith("/htr/6/status")
    assert payload["body"] == {"easy": True}


async def test_status_and_boost_writes_keep_their_bodies():
    result, client = await _invoke(
        "set-status", "-d", "dev1", "-n", "2", "--locked", "false", "--mode", "auto"
    )
    assert result.exit_code == 0, result.output
    assert [body for _path, body in client.posts] == [
        {"locked": False},
        {"mode": "auto"},
    ]
    result, client = await _invoke(
        "set-boost-mode",
        "-d",
        "dev1",
        "-n",
        "7",
        "--active",
        "--mode",
        "runback",
        "--temperature",
        "21",
        "--time-minutes",
        "60",
    )
    assert result.exit_code == 0, result.output
    assert client.posts[0][0].endswith("/htr/7/boost_mode")
    assert client.posts[0][1] == {
        "active": True,
        "mode": "runback",
        "temp": "21.0",
        "time": 60,
    }
    result, client = await _invoke("set-device-away-status", "-d", "dev1", "--away")
    assert result.exit_code == 0, result.output
    assert client.posts[0][0].endswith("/dev1/mgr/away_status")
    assert client.posts[0][1] == {"away": True, "enabled": True, "forced": False}


async def test_invalid_invocations_send_nothing():
    cases = [
        (
            "set-status",
            "-d",
            "dev1",
            "-n",
            "2",
            "--locked",
            "false",
            "--stemp",
            "20",
        ),
        ("set-easy", "-d", "dev1", "-n", "2", "--on"),
        ("set-runback", "-d", "dev1", "-n", "2", "--on"),
        ("set-boost", "-d", "dev1", "-n", "1", "--on"),
        ("set-boost", "-d", "dev1", "-n", "5", "--on"),
        ("set-boost", "-d", "dev1", "-n", "2", "--on"),
        (
            "set-boost-mode",
            "-d",
            "dev1",
            "-n",
            "6",
            "--active",
            "--mode",
            "runback",
        ),
        (
            "set-boost-mode",
            "-d",
            "dev1",
            "-n",
            "3",
            "--active",
            "--mode",
            "runback",
        ),
        (
            "set-boost-mode",
            "-d",
            "dev1",
            "-n",
            "2",
            "--active",
            "--mode",
            "runback",
        ),
        ("set-status", "-d", "dev1", "-n", "2", "--selected-temp", "comfort"),
        ("set-setup", "-d", "dev1", "-n", "2", "--control-mode", "6"),
        (
            "set-boost-mode",
            "-d",
            "dev1",
            "-n",
            "1",
            "--active",
            "--mode",
            "runback",
        ),
        ("set-node-power-limit", "-d", "dev1", "-n", "2", "1000"),
        ("set-setup", "-d", "dev1", "-n", "2", "--offset", "-1"),
        ("control-modes", "-d", "dev1", "-n", "1"),
        ("set-control-mode", "-d", "dev1", "-n", "1", "--mode", "5"),
        ("set-control-mode", "-d", "dev1", "-n", "4", "--mode", "5"),
        (
            "set-storage-mode",
            "-d",
            "dev1",
            "-n",
            "5",
            "--mode",
            "manual",
            "--stemp",
            "21",
        ),
        (
            "set-boost-mode",
            "-d",
            "dev1",
            "-n",
            "7",
            "--inactive",
            "--temperature",
            "21",
        ),
        (
            "set-boost-legacy",
            "-d",
            "dev1",
            "-n",
            "5",
            "--off",
            "--temperature",
            "21",
        ),
        ("set-control-mode", "-d", "dev1", "-n", "2", "--mode", "PID"),
        (
            "set-setup",
            "-d",
            "dev1",
            "-n",
            "2",
            "--priority",
            "high",
            "--extra-json",
            "{bad",
        ),
        ("set-max-stemp-limit", "-d", "dev1", "-n", "2", "23", "--off"),
        (
            "set-target-charge",
            "-d",
            "dev1",
            "-n",
            "5",
            "100",
        ),
        (
            "set-prog-temps",
            "-d",
            "dev1",
            "-n",
            "1",
            "--comfort",
            "21",
            "--units",
            "C",
        ),
        (
            "set-thermostat-prog",
            "-d",
            "dev1",
            "-n",
            "2",
            "--units",
            "C",
            "--prog",
            json.dumps({str(day): [{"m": 480, "t": "19.0"}] for day in range(7)}),
        ),
    ]
    for args in cases:
        result, client = await _invoke(*args)
        assert result.exit_code != 0, (args, result.output)
        assert client.posts == [], args


async def test_missing_json_file_is_a_click_error():
    result, client = await _invoke(
        "set-prog", "-d", "dev1", "-n", "2", "@/nonexistent/prog.json"
    )
    assert result.exit_code != 0
    assert "cannot read" in result.output
    assert client.posts == []


async def test_json_arguments_accept_at_file(tmp_path):
    prog = tmp_path / "prog.json"
    prog.write_text('{"prog": {"0": [2, 2]}}', encoding="utf-8")
    result, client = await _invoke("set-prog", "-d", "dev1", "-n", "2", f"@{prog}")
    assert result.exit_code == 0, result.output
    assert client.posts[0][0].endswith("/htr/2/prog")
    assert client.posts[0][1] == {"prog": {"0": [2, 2]}}
    extra = tmp_path / "extra.json"
    extra.write_text('{"boost_temp": "21.0"}', encoding="utf-8")
    result, client = await _invoke(
        "set-setup", "-d", "dev1", "-n", "2", f"--extra-json=@{extra}"
    )
    assert result.exit_code == 0, result.output
    assert client.posts[0][1]["extra_options"] == {
        "user_duty_limit": 25,
        "boost_temp": "21.0",
    }


async def test_profile_accepts_a_setup_file(tmp_path):
    setup = tmp_path / "setup.json"
    setup.write_text('{"max_stemp_limit": "23.0"}', encoding="utf-8")
    result = await CliRunner().invoke(
        smartbox,
        ["profile", "--pid", "0402", "--setup", f"@{setup}"],
    )
    assert result.exit_code == 0, result.output
    assert _json(result)["climate_bounds"] == [1.0, 23.0]


async def test_write_confirmation_requires_yes_only_on_a_tty():
    # CliRunner is not a TTY, so the command proceeds without --yes.
    with patch("smartbox.cli.common.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--no-token-cache",
                "set-easy",
                "-d",
                "dev1",
                "-n",
                "6",
                "--on",
            ],
        )
    assert result.exit_code == 0, result.output
    assert FakeClient.last.posts


async def test_samples_accept_iso_times_and_csv():
    result, _client = await _invoke(
        "node-samples",
        "-d",
        "dev1",
        "-n",
        "2",
        "--start-time",
        "2026-11-24T00:00:00Z",
        "--end-time",
        "2026-11-24T01:00:00Z",
        "--csv",
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0] == "power,t,temp"
    assert "20.0" in lines[1]


async def test_boolean_and_power_writes_are_gated_and_sent():
    case_list = [
        (
            ("set-easy", "-d", "dev1", "-n", "6", "--on"),
            "/htr/6/status",
            {"easy": True},
        ),
        (
            ("set-runback", "-d", "dev1", "-n", "6", "--on"),
            "/htr/6/status",
            {"runback": True},
        ),
        (
            ("set-boost", "-d", "dev1", "-n", "7", "--on"),
            "/htr/7/status",
            {"boost": True},
        ),
        (
            (
                "set-boost-mode",
                "-d",
                "dev1",
                "-n",
                "7",
                "--active",
                "--mode",
                "runback",
                "--temperature",
                "21",
                "--time-minutes",
                "60",
            ),
            "/htr/7/boost_mode",
            {"active": True, "mode": "runback", "temp": "21.0", "time": 60},
        ),
        (
            ("set-node-power-limit", "-d", "dev1", "-n", "6", "1000"),
            "/htr/6/power_limit",
            {"power_limit": "1000"},
        ),
        (
            ("set-device-power-limit", "-d", "dev1", "3000"),
            "/dev1/htr_system/power_limit",
            {"power_limit": "3000"},
        ),
    ]
    for args, suffix, expected in case_list:
        result, client = await _invoke(*args)
        assert result.exit_code == 0, (args, result.output)
        assert client.posts[0][0].endswith(suffix), (args, client.posts)
        assert client.posts[0][1] == expected, (args, client.posts)


async def test_control_modes_reads_labels_and_wire_index():
    result, _client = await _invoke("control-modes", "-d", "dev1", "-n", "2")
    assert result.exit_code == 0, result.output
    assert "PID (15 min. period)" in result.output
    assert "wire index 5" in result.output
    raw, _client = await _invoke("--json", "control-modes", "-d", "dev1", "-n", "2")
    payload = _json(raw)
    assert payload["current"] == 5
    assert payload["current_label"] == "PID (15 min. period)"
    assert {"index": 6, "label": "PID (30 min. period)"} in payload["choices"]


async def test_set_control_mode_accepts_labels_and_rejects_unoffered_values():
    result, client = await _invoke(
        "set-control-mode",
        "-d",
        "dev1",
        "-n",
        "2",
        "--mode",
        "PID (30 min. period)",
    )
    assert result.exit_code == 0, result.output
    assert client.posts[0][0].endswith("/htr/2/setup")
    assert client.posts[0][1]["control_mode"] == 6

    result, client = await _invoke(
        "set-control-mode", "-d", "dev1", "-n", "2", "--mode", "4"
    )
    assert result.exit_code != 0
    assert "offers" in result.output
    assert client.posts == []


async def test_set_setup_posts_each_named_field():
    result, client = await _invoke(
        "set-setup",
        "-d",
        "dev1",
        "-n",
        "2",
        "--priority",
        "high",
        "--units",
        "C",
        "--offset",
        "2",
        "--true-radiant-enabled",
        "true",
        "--window-mode-enabled",
        "false",
    )
    assert result.exit_code == 0, result.output
    bodies = [body for _path, body in client.posts]
    assert any(body.get("priority") == "high" for body in bodies)
    assert any(body.get("units") == "C" for body in bodies)
    assert any(body.get("away_offset") == "2.0" for body in bodies)
    assert any(body.get("true_radiant_enabled") is True for body in bodies)
    assert any(body.get("window_mode_enabled") is False for body in bodies)


async def test_dry_run_reads_the_current_setup_through_the_proxy():
    result, client = await _invoke(
        "--dry-run",
        "set-setup",
        "-d",
        "dev1",
        "-n",
        "2",
        "--extra-json",
        '{"boost_temp": "21.0"}',
    )
    assert result.exit_code == 0, result.output
    assert client.posts == []
    payload = _json(result)
    assert payload["dry_run"] is True
    assert payload["body"]["extra_options"] == {
        "user_duty_limit": 25,
        "boost_temp": "21.0",
    }


async def test_doctor_check_reports_the_probe_results():
    result, _client = await _invoke("doctor", "--check", "--json")
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["health"] == {"status": "ok"}
    assert payload["api_version"] == {"version": "test"}
    assert payload["devices"] == 1


async def test_control_mode_migration_hint_accepts_a_label():
    result, client = await _invoke(
        "set-setup", "-d", "dev1", "-n", "2", "--control-mode", "PID"
    )
    assert result.exit_code != 0
    assert "moved to set-control-mode" in result.output
    assert client.posts == []
