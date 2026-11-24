"""Smoke tests for the optional CLI surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
from unittest.mock import AsyncMock, patch

from asyncclick.testing import CliRunner

from smartbox.cli import _api_base_url, _dev_list, smartbox
from smartbox.protocol import AvailableResellers
from smartbox.protocol.auth import TokenBundle
from smartbox.protocol.client import SmartboxClient
from smartbox.protocol.reseller import SMARTBOX_GENERIC_BASIC_AUTH
from smartbox.token_cache import FileTokenCache

_ACCOUNT = "api:1:user@example.test:https://users-api.helki.com"


async def test_cli_help_lists_commands():
    result = await CliRunner().invoke(smartbox, ["--help"])
    assert result.exit_code == 0
    assert "listen" in result.output
    assert "set-boost-mode" in result.output
    assert "--api-host" in result.output


async def test_cli_resellers_does_not_need_credentials():
    result = await CliRunner().invoke(smartbox, ["resellers"])
    assert result.exit_code == 0
    assert "API host" in result.output
    assert "users-api.helki.com" in result.output
    assert "api-elnur.helki.com" in result.output
    assert "api-ehc.helki.com" in result.output
    assert "brand key" in result.output
    assert "generic" in result.output
    assert "custom" in result.output
    assert "Electric Heating Company" in result.output
    assert "Haverland" in result.output
    assert SMARTBOX_GENERIC_BASIC_AUTH not in result.output


def test_cli_api_host_override_normalizes():
    """--api-host accepts a bare host or a full HTTPS URL."""
    helki = AvailableResellers("api").reseller
    assert _api_base_url(helki, None) == "https://users-api.helki.com"
    assert _api_base_url(helki, "api.test") == "https://api.test"
    assert _api_base_url(helki, "https://api.test/") == "https://api.test"

    elnur = AvailableResellers("api-elnur").reseller
    assert _api_base_url(elnur, None) == "https://api-elnur.helki.com"


async def test_help_lists_the_token_cache_options():
    result = await CliRunner().invoke(smartbox, ["--help"])
    assert result.exit_code == 0
    assert "--token-cache" in result.output
    assert "--forget-tokens" in result.output


async def test_forget_tokens_deletes_the_cached_session(tmp_path):
    path = tmp_path / "tokens.json"
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    await FileTokenCache(path, account=_ACCOUNT).async_save(bundle)

    with patch.object(
        SmartboxClient, "get_devices", AsyncMock(return_value={"devs": []})
    ):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--token-cache-path",
                str(path),
                "--forget-tokens",
                "devices",
            ],
        )

    assert result.exit_code == 0, result.output
    assert not path.exists()


async def test_no_token_cache_ignores_a_preexisting_record(tmp_path):
    path = tmp_path / "tokens.json"
    await FileTokenCache(path, account=_ACCOUNT).async_save(
        TokenBundle(
            access_token="cached-access",
            refresh_token="cached-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=4),
        )
    )
    seen: dict = {}

    class FakeClient:
        def __init__(self, _config, **kwargs):
            seen.update(kwargs)

        async def get_devices(self):
            return {"devs": []}

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--token-cache-path",
                str(path),
                "--no-token-cache",
                "devices",
            ],
        )

    assert result.exit_code == 0, result.output
    assert seen["initial_tokens"] is None
    assert seen["on_grant"] is None


async def test_cli_seeds_the_client_from_the_cache_and_persists_grants(tmp_path):
    path = tmp_path / "tokens.json"
    cached = TokenBundle(
        access_token="cached-access",
        refresh_token="cached-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    await FileTokenCache(path, account=_ACCOUNT).async_save(cached)
    seen: dict = {}

    class FakeClient:
        def __init__(self, _config, **kwargs):
            seen.update(kwargs)

        async def get_devices(self):
            return {"devs": []}

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--token-cache-path",
                str(path),
                "devices",
            ],
        )

    assert result.exit_code == 0, result.output
    assert seen["initial_tokens"] == cached
    granted = TokenBundle(
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    await seen["on_grant"](granted)
    loaded = await FileTokenCache(path, account=_ACCOUNT).async_load()
    assert loaded == granted


async def test_cached_session_notice_is_debug_only(tmp_path, caplog):
    path = tmp_path / "tokens.json"
    await FileTokenCache(path, account=_ACCOUNT).async_save(
        TokenBundle(
            access_token="cached-access",
            refresh_token="cached-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=4),
        )
    )

    with patch.object(
        SmartboxClient, "get_devices", AsyncMock(return_value={"devs": []})
    ):
        with caplog.at_level(logging.INFO, logger="smartbox.cli"):
            quiet = await CliRunner().invoke(
                smartbox,
                [
                    "-u",
                    "user@example.test",
                    "-p",
                    "pw",
                    "--token-cache-path",
                    str(path),
                    "devices",
                ],
            )
        assert "Resuming the cached session" not in caplog.text
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="smartbox.cli"):
            verbose = await CliRunner().invoke(
                smartbox,
                [
                    "-u",
                    "user@example.test",
                    "-p",
                    "pw",
                    "--token-cache-path",
                    str(path),
                    "-v",
                    "devices",
                ],
            )

    assert quiet.exit_code == 0, quiet.output
    assert verbose.exit_code == 0, verbose.output
    assert "Resuming the cached session" not in quiet.output
    assert "Resuming the cached session" in caplog.text


async def test_sessions_masks_sids_and_marks_the_current_row():
    rows = [
        {
            "sid": "d19b4d4c1234",
            "createdAt": "2026-11-14T19:01:03.678Z",
            "lastUsedAt": "2026-11-14T19:01:03.678Z",
            "userAgent": "HomeAssistant/2026.11.1 aiohttp/3.14.5 Python/3.14",
            "current": True,
        }
    ]

    class FakeClient:
        def __init__(self, _config, **kwargs):
            pass

        async def get_sessions(self):
            return rows

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--no-token-cache",
                "sessions",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "d19b4d4c…" in result.output
    assert "d19b4d4c1234" not in result.output
    assert result.output.rstrip().endswith("*")


async def test_sessions_json_prints_the_raw_payload():
    rows = [{"sid": "d19b4d4c1234", "current": True}]

    class FakeClient:
        def __init__(self, _config, **kwargs):
            pass

        async def get_sessions(self):
            return rows

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox,
            [
                "-u",
                "user@example.test",
                "-p",
                "pw",
                "--no-token-cache",
                "sessions",
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "d19b4d4c1234" in result.output


def test_dev_list_flattens_invited_devices_and_dedupes():
    """An invited home's device is under invited_to, not devs."""
    payload = {
        "devs": [{"dev_id": "own", "name": "Own"}],
        "invited_to": [
            {"dev_id": "guest", "name": "Guest"},
            {"dev_id": "own", "name": "Own duplicate"},
        ],
    }
    assert [entry["dev_id"] for entry in _dev_list(payload)] == ["own", "guest"]
    assert _dev_list([{"dev_id": "bare"}]) == [{"dev_id": "bare"}]
    assert _dev_list({"devs": "junk"}) == []


async def test_nodes_lists_nodes_from_an_invited_home():
    """The node commands must not stop at the empty owned-device list."""

    class FakeClient:
        def __init__(self, _config, **kwargs):
            pass

        async def get_devices(self):
            return {
                "devs": [],
                "invited_to": [{"dev_id": "guest", "name": "Guest home"}],
            }

        async def get_nodes(self, dev_id):
            assert dev_id == "guest"
            return [{"type": "htr", "addr": 2, "name": "Radiateur"}]

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox, ["-u", "user@example.test", "-p", "pw", "nodes"]
        )

    assert result.exit_code == 0, result.output
    assert "Guest home" in result.output
    assert "Radiateur" in result.output
    assert '"addr": 2' in result.output


async def test_devices_flattens_an_invited_home():
    """The raw envelope is flattened so an invited device is visible."""

    class FakeClient:
        def __init__(self, _config, **kwargs):
            pass

        async def get_devices(self):
            return {
                "devs": [],
                "invited_to": [{"dev_id": "guest", "name": "Guest home"}],
            }

        async def close(self):
            return None

    with patch("smartbox.cli.SmartboxClient", FakeClient):
        result = await CliRunner().invoke(
            smartbox, ["-u", "user@example.test", "-p", "pw", "devices"]
        )

    assert result.exit_code == 0, result.output
    assert "Guest home" in result.output
    assert "invited_to" not in result.output


async def test_cli_profile_resolves_without_credentials():
    """The profile command is a second consumer of the neutral resolver."""
    result = await CliRunner().invoke(
        smartbox,
        [
            "profile",
            "--pid",
            "08320000",
            "--node-type",
            "htr",
            "--fw-version",
            "1.9",
            "--setup",
            '{"factory_options": {"boost_config": 2}}',
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["code"] == "32"
    assert payload["limits"]["profiles"] == ["frost", "eco", "comfort", "off"]
    assert payload["climate_bounds"] == [1.0, 30.0]


async def test_cli_profile_rejects_bad_setup_json():
    result = await CliRunner().invoke(
        smartbox, ["profile", "--pid", "08320000", "--setup", "not-json"]
    )
    assert result.exit_code != 0
    assert "invalid JSON" in result.output
