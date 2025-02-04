"""Expose submodules."""

from .error import (
    APIUnavailableError,
    InvalidAuthError,
    ResailerNotExistError,
    SmartboxError,
)
from .resailer import AvailableResailers, SmartboxResailer
from .session import AsyncSmartboxSession, Session
from .socket import SocketSession
from .update_manager import UpdateManager

__version__ = "2.1.0-beta.1"


__all__ = [
    "APIUnavailableError",
    "AsyncSmartboxSession",
    "AvailableResailers",
    "InvalidAuthError",
    "ResailerNotExistError",
    "Session",
    "SmartboxError",
    "SmartboxResailer",
    "SocketSession",
    "UpdateManager",
]
