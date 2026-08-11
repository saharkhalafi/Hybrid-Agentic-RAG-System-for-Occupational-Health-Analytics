"""Versioned metadata contract for structured records, semantic chunks, and formulas.

Traceability chain (required for production knowledge):

    answer → knowledge record → Gold artifact → table/chunk/formula
           → cell/text → PDF page → immutable evidence
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

KnowledgeRecordType = Literal["structured", "semantic", "formula", "entity"]
ValidationStatus = Literal["accepted", "review_required", "rejected", "legacy_reference"]


@dataclass
class SourceReference:
    document_id: str | None = None
    page_number: int | None = None
    printed_page_number: int | None = None
    table_id: str | None = None
    row_id: str | None = None
    cell_id: str | None = None
    chunk_id: str | None = None
    formula_id: str | None = None
    evidence_path: str | None = None
    bbox: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class FieldProvenance:
    """Distinguishes source, normalized, and accepted values for one field."""

    field_name: str
    original_value: str | None = None
    normalized_value: str | None = None
    accepted_value: str | None = None
    unit: str | None = None
    cell_id: str | None = None
    bbox: dict[str, Any] | None = None
    validation_status: ValidationStatus = "accepted"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeMetadata:
    """Shared metadata envelope for all knowledge-layer records."""

    record_type: KnowledgeRecordType
    record_id: str
    document_id: str
    language: str = "fa"
    validation_status: ValidationStatus = "accepted"
    gold_artifact_path: str | None = None
    gold_version: str | None = None
    pipeline_version: str | None = None
    source_reference: SourceReference = field(default_factory=SourceReference)
    field_provenance: list[FieldProvenance] = field(default_factory=list)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedding_version: str | None = None
    content_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_reference"] = self.source_reference.to_dict()
        payload["field_provenance"] = [fp.to_dict() for fp in self.field_provenance]
        return payload


@dataclass
class ProvenanceChain:
    """End-to-end traceability link for audits and agent responses."""

    fact: str
    knowledge_record_id: str
    knowledge_record_type: KnowledgeRecordType
    gold_artifact_path: str | None
    table_id: str | None
    cell_id: str | None
    chunk_id: str | None
    formula_id: str | None
    page_number: int | None
    evidence_path: str | None
    original_value: str | None
    normalized_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_knowledge_metadata(meta: KnowledgeMetadata) -> list[str]:
    issues: list[str] = []
    if not meta.record_id:
        issues.append("record_id required")
    if not meta.document_id:
        issues.append("document_id required")
    if meta.validation_status not in {"accepted", "legacy_reference"}:
        issues.append(f"production knowledge requires accepted status, got {meta.validation_status}")
    if meta.record_type == "structured" and not meta.source_reference.table_id:
        issues.append("structured record missing table_id")
    if meta.record_type == "semantic" and not meta.source_reference.chunk_id:
        issues.append("semantic record missing chunk_id")
    if meta.record_type == "formula" and not meta.source_reference.formula_id:
        issues.append("formula record missing formula_id")
    return issues


def build_provenance_chain(
    *,
    fact: str,
    metadata: KnowledgeMetadata,
    original_value: str | None = None,
    normalized_value: str | None = None,
) -> ProvenanceChain:
    ref = metadata.source_reference
    return ProvenanceChain(
        fact=fact,
        knowledge_record_id=metadata.record_id,
        knowledge_record_type=metadata.record_type,
        gold_artifact_path=metadata.gold_artifact_path,
        table_id=ref.table_id,
        cell_id=ref.cell_id,
        chunk_id=ref.chunk_id,
        formula_id=ref.formula_id,
        page_number=ref.page_number,
        evidence_path=ref.evidence_path,
        original_value=original_value,
        normalized_value=normalized_value,
    )
