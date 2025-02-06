"""Smartbox specific Errors."""

import aiohttp


class SmartboxError(Exception):
    """General errors from smartbox API."""


class InvalidAuthError(Exception):
    """Authentication failed."""


class APIUnavailableError(aiohttp.ClientConnectionError):
    """API is unavailable."""


class ResailerNotExistError(Exception):
    """Resailer is not known."""
