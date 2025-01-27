class SmartboxError(Exception):
    """General errors from smartbox API"""

    pass


class InvalidAuth(Exception):
    """Authentication failed"""

    pass


class APIUnavailable(Exception):
    """API is unavailable"""

    pass
