"""Provenance tracking for goldset generation — Evidence → Structure → Semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pipeline_contracts.provenance import PipelineStage, ProcessorProvenance

__all__ = [
    "DataLayer",
    "GenerationInfo",
    "PipelineStage",
    "ProcessorProvenance",
    "evidence_source",
    "semantic_source",
    "structural_source",
]


class DataLayer(str, Enum):
    EVIDENCE = PipelineStage.EVIDENCE.value
    STRUCTURAL = PipelineStage.STRUCTURAL.value
    SEMANTIC = PipelineStage.SEMANTIC.value
    VALIDATION = PipelineStage.VALIDATION.value
    DOMAIN = PipelineStage.DOMAIN.value


@dataclass
class GenerationInfo:
    source_pdf: str
    pipeline_version: str
    document_ai_processor: str
    gemini_model: str
    created_date: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    start_page: int | None = None
    end_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "pipeline_version": self.pipeline_version,
            "document_ai_processor": self.document_ai_processor,
            "gemini_model": self.gemini_model,
            "created_date": self.created_date,
            "start_page": self.start_page,
            "end_page": self.end_page,
        }


def evidence_source(source: str = "document_ai") -> dict[str, str]:
    return {"layer": DataLayer.EVIDENCE.value, "source": source}


def structural_source(source: str = "structural_resolver") -> dict[str, str]:
    return {"layer": DataLayer.STRUCTURAL.value, "source": source}


def semantic_source(model: str) -> dict[str, str]:
    return {"layer": DataLayer.SEMANTIC.value, "source": "vertex_ai_gemini", "model": model}
