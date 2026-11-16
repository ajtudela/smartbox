"""Optional file-backed token cache for command-line callers.

The Home Assistant integration owns its own storage; this module serves the
CLI and local tools that want the refresh token to survive a process
restart, so repeated invocations continue one vendor session instead of
minting a login each time.

The file holds live credentials. It is written owner-only (``0600``) inside a
per-user cache directory (created ``0700``), replaced atomically, and keyed
per account. Any read problem is treated as "no cache": a disposable cache
must never block a run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from .protocol.auth import TokenBundle

__all__ = ["FileTokenCache", "account_stamp", "default_cache_path"]

_LOGGER = logging.getLogger(__name__)

_RECORD_VERSION = 1


def account_stamp(
    api_name: str, serial_id: str, username: str, api_host: str = ""
) -> str:
    """Return the cache record's account identity.

    The reseller, case-folded login and effective API host make the record
    meaningless for a different account or endpoint, mirroring the claim check
    the token manager applies. ``api_host`` is optional for callers that keep
    the pre-host cache format.
    """
    stamp = f"{api_name}:{serial_id}:{username.casefold()}"
    return f"{stamp}:{api_host}" if api_host else stamp


def default_cache_path(
    api_name: str, serial_id: str, username: str, api_host: str = ""
) -> Path:
    """Return the per-account/per-host cache path under ``$XDG_CACHE_HOME``."""
    digest = hashlib.sha256(
        account_stamp(api_name, serial_id, username, api_host).encode()
    ).hexdigest()[:16]
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "smartbox" / f"{digest}.json"


class FileTokenCache:
    """One account's token bundle in a JSON file."""

    def __init__(self, path: Path, *, account: str) -> None:
        """Bind the cache to a file and the account it belongs to."""
        self.path = Path(path)
        self._account = account

    async def async_load(self) -> TokenBundle | None:
        """Return the cached bundle, or ``None`` when absent or unusable."""
        try:
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as err:
            _LOGGER.debug(
                "Cannot read token cache %s (%s)", self.path, type(err).__name__
            )
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring malformed token cache %s", self.path)
            return None
        if (
            not isinstance(record, dict)
            or record.get("version") != _RECORD_VERSION
            or record.get("account") != self._account
        ):
            _LOGGER.debug(
                "Ignoring token cache %s for another record/account", self.path
            )
            return None
        try:
            return TokenBundle.from_record(record)
        except ValueError:
            _LOGGER.debug("Ignoring unusable token cache %s", self.path)
            return None

    async def async_save(self, tokens: TokenBundle) -> None:
        """Write a newly granted bundle (suitable as ``on_grant``).

        Failures are logged and swallowed: the cache is best-effort and the
        granted session is already live in the caller.
        """
        record = {
            "version": _RECORD_VERSION,
            "account": self._account,
            **tokens.to_record(),
        }
        payload = json.dumps(record, sort_keys=True)
        try:
            await asyncio.to_thread(self._write_atomic, payload)
        except OSError as err:
            _LOGGER.warning(
                "Cannot persist token cache %s (%s)", self.path, type(err).__name__
            )

    async def async_clear(self) -> bool:
        """Remove the cached bundle; return whether it is gone."""
        try:
            await asyncio.to_thread(self.path.unlink)
        except FileNotFoundError:
            return True
        except OSError as err:
            _LOGGER.warning(
                "Cannot remove token cache %s (%s)", self.path, type(err).__name__
            )
            return False
        return True

    def _write_atomic(self, payload: str) -> None:
        """Write ``payload`` via a private temp file and an atomic rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        tmp = Path(tmp_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
