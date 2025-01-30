class SmartboxError(Exception):
    """General errors from smartbox API"""

    pass


class InvalidAuth(Exception):
    """Authentication failed"""

    pass


class APIUnavailable(Exception):
    """API is unavailable"""

    pass


class ResailerNotExist(Exception):
    """Resailer is not known."""

    pass
