"""Typed read-only view of a node's ``setup`` payload.

The wire setup is a nested mapping with optional keys and several sentinel
conventions. The device modules need the same interpretation of
``factory_options``, ``extra_options``, ``extra_nrg_conf`` and unit fields;
this view keeps that parsing in one place. It implements ``Mapping`` so the
existing helpers can keep accepting a raw mapping. Enum accessors accept
integral JSON numbers (the app's JS `2.0 === 2`); strings and fractional
numbers are not coerced.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from smartbox.core.merging import parse_max_stemp_limit


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int | None:
    """Return an integral wire enum; strings and fractional numbers are not."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


@dataclass(frozen=True)
class SetupView(Mapping[str, object]):
    """Read-only accessors over one node setup mapping."""

    raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalize a non-mapping payload to an empty setup."""
        if not isinstance(self.raw, Mapping):
            object.__setattr__(self, "raw", {})

    def __getitem__(self, key: str) -> object:
        """Return a raw setup value."""
        return self.raw[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate the raw setup keys."""
        return iter(self.raw)

    def __len__(self) -> int:
        """Return the number of raw setup keys."""
        return len(self.raw)

    @property
    def factory_options(self) -> Mapping[str, object]:
        """The node's ``factory_options`` object."""
        return _mapping(self.raw.get("factory_options"))

    @property
    def extra_options(self) -> Mapping[str, object]:
        """The node's ``extra_options`` object."""
        return _mapping(self.raw.get("extra_options"))

    @property
    def extra_nrg_conf(self) -> Mapping[str, object]:
        """The node's ``extra_nrg_conf`` object."""
        return _mapping(self.raw.get("extra_nrg_conf"))

    @property
    def extra_nrg_enabled(self) -> bool:
        """Whether the extra-energy configuration is enabled."""
        return bool(self.extra_nrg_conf.get("enabled"))

    @property
    def extra_nrg_mode(self) -> int | None:
        """Return ``extra_nrg_conf.mode`` when present."""
        return _integer(self.extra_nrg_conf.get("mode"))

    @property
    def ui_mode(self) -> int | None:
        """Return ``factory_options.ui_mode`` (0/1/2) when present."""
        return _integer(self.factory_options.get("ui_mode"))

    @property
    def elnur_model(self) -> int | None:
        """Return ``factory_options.model`` (ECOMBI/PRO/E2/ARC) when present."""
        return _integer(self.factory_options.get("model"))

    @property
    def operational_mode(self) -> int | None:
        """Return the top-level ``operational_mode`` when present."""
        return _integer(self.raw.get("operational_mode"))

    @property
    def sensor_mode(self) -> int | None:
        """Return ``extra_options.sensor_mode`` when present."""
        return _integer(self.extra_options.get("sensor_mode"))

    @property
    def control_mode(self) -> int | None:
        """Return ``control_mode`` when present."""
        return _integer(self.raw.get("control_mode"))

    @property
    def max_stemp_limit(self) -> float | None:
        """Return the stored max-stemp Celsius ceiling, or None when disabled."""
        return parse_max_stemp_limit(self.raw.get("max_stemp_limit"))

    @property
    def power(self) -> object:
        """Return the raw setup ``power`` value."""
        return self.raw.get("power")


__all__ = ["SetupView"]
