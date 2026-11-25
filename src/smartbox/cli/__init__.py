"""Smartbox API command line.

The command group and its helpers live in :mod:`smartbox.cli.common`; the
modules below register their commands when imported.
"""

from __future__ import annotations

from smartbox.protocol import SmartboxError

from . import (
    diagnostics as _diagnostics,  # noqa: F401 - registers commands
    read as _read,  # noqa: F401 - registers commands
    write as _write,  # noqa: F401 - registers commands
)
from .common import _load_dotenv, smartbox

__all__ = ["cli", "smartbox"]


def cli() -> None:
    """Console-script entry point: load ``.env`` then run the CLI."""
    _load_dotenv()
    try:
        smartbox()
    except SmartboxError as err:
        raise SystemExit(f"Error: {err}") from err
