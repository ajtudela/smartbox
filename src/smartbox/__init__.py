"""Expose submodules."""

import importlib.metadata

from .error import (
    APIUnavailableError,
    InvalidAuthError,
    ResellerNotExistError,
    SmartboxError,
)
from .models import (
    AcmNodeStatus,
    DefaultNodeStatus,
    DeviceVersion,
    Guests,
    GuestUser,
    HtrModNodeStatus,
    HtrNodeStatus,
    HtrSystemSetup,
    NodeExtraOptions,
    NodeFactoryOptions,
    NodeProg,
    NodeSetup,
    NodeStatus,
    SmartboxNodeType,
)
from .reseller import AvailableResellers, SmartboxReseller
from .session import AsyncSmartboxSession, Session
from .socket import SocketSession
from .update_manager import UpdateManager

__version__ = importlib.metadata.version("smartbox")


__all__ = [
    "APIUnavailableError",
    "AcmNodeStatus",
    "AsyncSmartboxSession",
    "AvailableResellers",
    "DefaultNodeStatus",
    "DeviceVersion",
    "GuestUser",
    "Guests",
    "HtrModNodeStatus",
    "HtrNodeStatus",
    "HtrSystemSetup",
    "InvalidAuthError",
    "NodeExtraOptions",
    "NodeFactoryOptions",
    "NodeProg",
    "NodeSetup",
    "NodeStatus",
    "ResellerNotExistError",
    "Session",
    "SmartboxError",
    "SmartboxNodeType",
    "SmartboxReseller",
    "SocketSession",
    "UpdateManager",
]
