"""Tests for the optional file-backed token cache."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import stat

import pytest

from smartbox.protocol.auth import TokenBundle
from smartbox.token_cache import FileTokenCache, account_stamp, default_cache_path


def _bundle(access: str = "access", refresh: str = "refresh") -> TokenBundle:
    return TokenBundle(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )


async def test_roundtrip_is_owner_only_and_clears(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    cache = FileTokenCache(path, account="api:17:user@example.test")
    assert await cache.async_load() is None

    bundle = _bundle()
    await cache.async_save(bundle)
    loaded = await cache.async_load()
    assert loaded == bundle

    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # The atomic write leaves no temp file behind.
    names = await asyncio.to_thread(lambda: sorted(p.name for p in tmp_path.iterdir()))
    assert names == ["tokens.json"]

    await cache.async_clear()
    assert not path.exists()
    assert await cache.async_load() is None
    await cache.async_clear()  # missing file is not an error


async def test_another_account_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    await FileTokenCache(path, account="api:17:user@example.test").async_save(_bundle())

    assert (
        await FileTokenCache(path, account="api:17:other@example.test").async_load()
        is None
    )


async def test_malformed_records_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    cache = FileTokenCache(path, account="api:17:user@example.test")

    path.write_text("{not json", encoding="utf-8")
    assert await cache.async_load() is None

    record = {
        "version": 99,
        "account": "api:17:user@example.test",
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    assert await cache.async_load() is None

    record["version"] = 1
    record["access_token"] = ""
    path.write_text(json.dumps(record), encoding="utf-8")
    assert await cache.async_load() is None


async def test_unreadable_path_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.mkdir()  # reading a directory raises OSError

    assert (
        await FileTokenCache(path, account="api:17:user@example.test").async_load()
        is None
    )


def test_default_path_is_per_account_and_honours_xdg(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    first = default_cache_path("api", "17", "User@Example.test")
    assert first.parent == tmp_path / "smartbox"
    assert first == default_cache_path("api", "17", "user@example.test")
    assert first != default_cache_path("api", "18", "user@example.test")
    assert first != default_cache_path("api-lhz", "17", "user@example.test")
    assert first != default_cache_path(
        "api", "17", "user@example.test", "https://api-elnur.helki.com"
    )


def test_account_stamp_identifies_the_session() -> None:
    assert (
        account_stamp("api-lhz", "2", "User@Example.test")
        == "api-lhz:2:user@example.test"
    )
    assert (
        account_stamp(
            "api-lhz",
            "2",
            "User@Example.test",
            "https://users-api.helki.com",
        )
        == "api-lhz:2:user@example.test:https://users-api.helki.com"
    )


async def test_invalid_utf8_is_treated_as_no_cache(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_bytes(b"\xff\xfe\x00bad")

    assert (
        await FileTokenCache(path, account="api:17:user@example.test").async_load()
        is None
    )


async def test_save_errors_are_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "tokens.json"
    path.mkdir()  # replacing a directory raises OSError
    cache = FileTokenCache(path, account="api:17:user@example.test")
    with caplog.at_level("WARNING"):
        await cache.async_save(_bundle())
    assert "Cannot persist token cache" in caplog.text


async def test_clear_reports_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "tokens.json"
    path.mkdir()
    cache = FileTokenCache(path, account="api:17:user@example.test")
    with caplog.at_level("WARNING"):
        assert await cache.async_clear() is False
    assert "Cannot remove token cache" in caplog.text
