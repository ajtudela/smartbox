"""Smartbox specific Errors."""

import aiohttp


class SmartboxError(Exception):
    """Base class for every error raised by this library.

    Consumers can catch ``SmartboxError`` to handle any library failure without
    also catching unrelated ``aiohttp`` exceptions.
    """


class InvalidAuthError(SmartboxError):
    """Authentication failed (bad credentials or rejected/expired token)."""


class APIUnavailableError(SmartboxError, aiohttp.ClientConnectionError):
    """API is unavailable.

    Also inherits from ``aiohttp.ClientConnectionError`` for backwards
    compatibility with consumers that catch that type; this second base is
    expected to be dropped in a future major release.
    """


class ResellerNotExistError(SmartboxError):
    """Reseller is not known."""
