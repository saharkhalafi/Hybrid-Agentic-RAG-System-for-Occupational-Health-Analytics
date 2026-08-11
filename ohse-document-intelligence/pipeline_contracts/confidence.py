"""Layered confidence — separate scores per pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LayeredConfidence:
    layout: float | None = None
    geometry: float | None = None
    mapping: float | None = None
    validation: float | None = None

    @property
    def overall(self) -> float:
        values = [v for v in (self.layout, self.geometry, self.mapping, self.validation) if v is not None]
        if not values:
            return 0.0
        return min(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_confidence": self.layout,
            "geometry_confidence": self.geometry,
            "mapping_confidence": self.mapping,
            "validation_confidence": self.validation,
            "overall": self.overall,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayeredConfidence:
        return cls(
            layout=_float_or_none(data.get("layout_confidence") or data.get("layout")),
            geometry=_float_or_none(data.get("geometry_confidence") or data.get("geometry")),
            mapping=_float_or_none(data.get("mapping_confidence") or data.get("mapping")),
            validation=_float_or_none(data.get("validation_confidence") or data.get("validation")),
        )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
