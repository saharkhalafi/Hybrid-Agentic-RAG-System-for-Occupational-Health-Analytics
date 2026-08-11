"""Processor and pipeline provenance — attached to every artifact."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class PipelineStage(str, Enum):
    EVIDENCE = "evidence"
    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    VALIDATION = "validation"
    DOMAIN = "domain"


@dataclass
class ProcessorProvenance:
    """Forensic traceability for comparing extraction runs over time."""

    processor: str
    processor_version: str
    pipeline_version: str
    schema_id: str | None = None
    schema_version: str | None = None
    confidence_source: str | None = None
    pipeline_stage: str = PipelineStage.EVIDENCE.value
    evidence_ref: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "processor": self.processor,
            "processor_version": self.processor_version,
            "pipeline": self.pipeline_version,
            "pipeline_stage": self.pipeline_stage,
            "created_at": self.created_at,
        }
        if self.schema_id:
            payload["schema"] = self.schema_id
        if self.schema_version:
            payload["schema_version"] = self.schema_version
        if self.confidence_source:
            payload["confidence_source"] = self.confidence_source
        if self.evidence_ref:
            payload["evidence_ref"] = self.evidence_ref
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessorProvenance:
        return cls(
            processor=str(data.get("processor") or "unknown"),
            processor_version=str(data.get("processor_version") or "unknown"),
            pipeline_version=str(data.get("pipeline") or data.get("pipeline_version") or "unknown"),
            schema_id=data.get("schema"),
            schema_version=data.get("schema_version"),
            confidence_source=data.get("confidence_source"),
            pipeline_stage=str(data.get("pipeline_stage") or PipelineStage.EVIDENCE.value),
            evidence_ref=data.get("evidence_ref"),
            created_at=str(data.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )
