"""Smartbox specific Errors."""


class SmartboxError(Exception):
    """General errors from smartbox API."""


class InvalidAuthError(Exception):
    """Authentication failed."""


class APIUnavailableError(Exception):
    """API is unavailable."""


class ResailerNotExistError(Exception):
    """Resailer is not known."""
