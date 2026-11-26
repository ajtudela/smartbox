"""Public surface of the protocol layer."""

from __future__ import annotations

from .auth import (
    AuthConfig,
    GrantCallback,
    ManagedTokenSource,
    TokenBundle,
    TokenManager,
)
from .client import NodeRef, SmartboxClient
from .errors import (
    AuthError,
    ResellerNotExistError,
    SmartboxError,
    TransportUnavailableError,
    TransportUnsupportedError,
)
from .events import Event, Snapshot, SocketClosed, Update
from .jwt import jwt_account_matches, jwt_expiry, jwt_user_id
from .reseller import API_HOST, AvailableResellers, SmartboxReseller, api_base_url
from .retry import backoff_delay
from .transport import EventHandler, Transport
from .values import bounded_repr, format_setpoint, is_lost, parse_number
from .writes import (
    VALID_BOOST_MODES,
    VALID_MODES,
    VALID_PRIORITIES,
    WriteApi,
    WriteHost,
)
from .ws_user import (
    TokenSource,
    WsCredentials,
    WsUserTransport,
    build_ws_url,
    check_ws_user_support,
)

__all__ = [
    "API_HOST",
    "VALID_BOOST_MODES",
    "VALID_MODES",
    "VALID_PRIORITIES",
    "AuthConfig",
    "AuthError",
    "AvailableResellers",
    "Event",
    "EventHandler",
    "GrantCallback",
    "ManagedTokenSource",
    "NodeRef",
    "ResellerNotExistError",
    "SmartboxClient",
    "SmartboxError",
    "SmartboxReseller",
    "Snapshot",
    "SocketClosed",
    "TokenBundle",
    "TokenManager",
    "TokenSource",
    "Transport",
    "TransportUnavailableError",
    "TransportUnsupportedError",
    "Update",
    "WriteApi",
    "WriteHost",
    "WsCredentials",
    "WsUserTransport",
    "api_base_url",
    "backoff_delay",
    "bounded_repr",
    "build_ws_url",
    "check_ws_user_support",
    "format_setpoint",
    "is_lost",
    "jwt_account_matches",
    "jwt_expiry",
    "jwt_user_id",
    "parse_number",
]
