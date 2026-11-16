"""Smartbox protocol and domain layers.

The package is Home Assistant-agnostic: ``smartbox.protocol`` owns the
wire and the ``smartbox.core`` package owns the domain state. Both are
usable without the HA test harness.
"""

from importlib.metadata import PackageNotFoundError, version

from . import core, protocol, token_cache

try:
    __version__ = version("smartbox")
except PackageNotFoundError:  # pragma: no cover - vendored/undist import
    __version__ = "0.0.0"

__all__ = ["__version__", "core", "protocol", "token_cache"]
