"""SQLAlchemy ORM models for the OHSE Document Intelligence Platform."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.settings import get_settings
from database.base import Base, TimestampMixin, uuid_pk


class PageType(str, enum.Enum):
    DIGITAL_TEXT = "digital_text"
    SCANNED = "scanned"
    TABLE_HEAVY = "table_heavy"
    TEXT_HEAVY = "text_heavy"
    FORMULA_HEAVY = "formula_heavy"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class TableType(str, enum.Enum):
    CHEMICAL_OEL = "chemical_oel"
    VIBRATION = "vibration"
    NOISE = "noise"
    BIOLOGICAL_MONITORING = "biological_monitoring"
    UNKNOWN = "unknown"


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class ReviewIssueType(str, enum.Enum):
    LOW_OCR_CONFIDENCE = "low_ocr_confidence"
    WRONG_TABLE_STRUCTURE = "wrong_table_structure"
    UNKNOWN_TABLE_TYPE = "unknown_table_type"
    FORMULA_EXTRACTION_FAILURE = "formula_extraction_failure"
    LOW_CELL_CONFIDENCE = "low_cell_confidence"
    STRUCTURAL_MISMATCH = "structural_mismatch"
    RECOVERY_REQUIRED = "recovery_required"
    MISSING_BBOX = "missing_bbox"
    LOW_BBOX_CONFIDENCE = "low_bbox_confidence"


class ChunkType(str, enum.Enum):
    SECTION = "section"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE_CONTEXT = "table_context"
    REGULATORY_NOTE = "regulatory_note"
    FORMULA_CONTEXT = "formula_context"


def _vector_column():
    dimension = get_settings().vector_dimension
    return mapped_column(Vector(dimension), nullable=True)


# ---------------------------------------------------------------------------
# Layer 1 — Evidence / Raw Extraction (immutable)
# ---------------------------------------------------------------------------


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    language: Mapped[str | None] = mapped_column(String(16))
    processing_version: Mapped[str] = mapped_column(String(32), nullable=False)
    gcs_uri: Mapped[str | None] = mapped_column(String(1024))
    page_count: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)

    pages: Mapped[list[DocumentPage]] = relationship(back_populates="document", cascade="all, delete-orphan")
    tables: Mapped[list[ExtractedTable]] = relationship(back_populates="document", cascade="all, delete-orphan")
    chunks: Mapped[list[DocumentChunk]] = relationship(back_populates="document", cascade="all, delete-orphan")
    review_items: Mapped[list[ReviewQueueItem]] = relationship(back_populates="document", cascade="all, delete-orphan")
    extraction_candidates: Mapped[list["ExtractionCandidate"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    review_tasks: Mapped[list["ReviewTask"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(Base, TimestampMixin):
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number", name="uq_document_page"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    ocr_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    layout_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    page_type: Mapped[PageType] = mapped_column(
        Enum(PageType, name="page_type_enum", native_enum=False),
        default=PageType.UNKNOWN,
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    image_path: Mapped[str | None] = mapped_column(String(1024))
    has_digital_text: Mapped[bool] = mapped_column(Boolean, default=False)
    triage_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    document: Mapped[Document] = relationship(back_populates="pages")


# ---------------------------------------------------------------------------
# Universal Table Extraction Layer
# ---------------------------------------------------------------------------


class ExtractedTable(Base, TimestampMixin):
    __tablename__ = "extracted_tables"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    table_type: Mapped[TableType] = mapped_column(
        Enum(TableType, name="table_type_enum", native_enum=False),
        default=TableType.UNKNOWN,
        nullable=False,
    )
    stable_table_id: Mapped[str | None] = mapped_column(String(64), index=True)
    schema_id: Mapped[str | None] = mapped_column(String(64))
    recovery_method: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(512))
    caption: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_markdown: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_processor: Mapped[str] = mapped_column(String(64), default="document_ai")
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    document: Mapped[Document] = relationship(back_populates="tables")
    cells: Mapped[list[TableCell]] = relationship(back_populates="table", cascade="all, delete-orphan")
    formulas: Mapped[list[Formula]] = relationship(back_populates="table", cascade="all, delete-orphan")
    validated_rows: Mapped[list[ValidatedTableRow]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )


class TableCell(Base, TimestampMixin):
    __tablename__ = "table_cells"
    __table_args__ = (
        UniqueConstraint("table_id", "row_index", "column_index", name="uq_table_cell_position"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extracted_tables.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    column_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    original_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(64))
    row_span: Mapped[int] = mapped_column(Integer, default=1)
    column_span: Mapped[int] = mapped_column(Integer, default=1)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    bbox_source: Mapped[str | None] = mapped_column(String(32))
    bbox_confidence: Mapped[float | None] = mapped_column(Float)
    source_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_cell_id: Mapped[str | None] = mapped_column(String(128), index=True)
    merged_cell: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))

    table: Mapped[ExtractedTable] = relationship(back_populates="cells")


class Formula(Base, TimestampMixin):
    __tablename__ = "formulas"

    id: Mapped[uuid.UUID] = uuid_pk()
    stable_formula_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
        index=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    formula_name: Mapped[str | None] = mapped_column(String(256))
    persian_name: Mapped[str | None] = mapped_column(String(512))
    domain: Mapped[str | None] = mapped_column(String(128), index=True)
    original_expression: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_expression: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    unit: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    validation_status: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    formula_version: Mapped[str | None] = mapped_column(String(64))
    source_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    semantics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    applicability_conditions: Mapped[str | None] = mapped_column(Text)
    gold_artifact_path: Mapped[str | None] = mapped_column(String(1024))
    knowledge_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    table: Mapped[ExtractedTable | None] = relationship(back_populates="formulas")


class DocumentEvidenceSnapshot(Base):
    __tablename__ = "document_evidence_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "page_start", "page_end", "snapshot_hash",
            name="uq_evidence_snapshot",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    processor_format: Mapped[str | None] = mapped_column(String(64))
    immutable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ValidatedTableRow(Base):
    __tablename__ = "validated_table_rows"
    __table_args__ = (
        UniqueConstraint("table_id", "row_index", name="uq_validated_row_per_table"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    table_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extracted_tables.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_id: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(512))
    row_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    field_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_issues: Mapped[list[str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    table: Mapped[ExtractedTable] = relationship(back_populates="validated_rows")


# ---------------------------------------------------------------------------
# Human Review System
# ---------------------------------------------------------------------------


class ReviewQueueItem(Base, TimestampMixin):
    __tablename__ = "review_queue"

    id: Mapped[uuid.UUID] = uuid_pk()
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issue_type: Mapped[ReviewIssueType] = mapped_column(
        Enum(ReviewIssueType, name="review_issue_type_enum", native_enum=False),
        nullable=False,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status_enum", native_enum=False),
        default=ReviewStatus.PENDING,
        nullable=False,
    )
    assigned_user: Mapped[str | None] = mapped_column(String(256))
    review_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[Document] = relationship(back_populates="review_items")


# ---------------------------------------------------------------------------
# Domain Knowledge Layer
# ---------------------------------------------------------------------------


class ChemicalRegistry(Base, TimestampMixin):
    __tablename__ = "chemical_registry"

    id: Mapped[uuid.UUID] = uuid_pk()
    cas: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    english_name: Mapped[str | None] = mapped_column(String(512))
    persian_name: Mapped[str | None] = mapped_column(String(512))
    synonyms: Mapped[list[str] | None] = mapped_column(JSONB)
    aliases: Mapped[dict[str, list[str]] | None] = mapped_column(JSONB)
    molecular_weight: Mapped[float | None] = mapped_column(Float)
    hazard_class: Mapped[str | None] = mapped_column(String(128))
    validation_status: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    gold_artifact_path: Mapped[str | None] = mapped_column(String(1024))

    oel_limits: Mapped[list[OELChemicalLimit]] = relationship(back_populates="chemical")
    biological_limits: Mapped[list[BiologicalExposureLimit]] = relationship(back_populates="chemical")


class OELChemicalLimit(Base, TimestampMixin):
    __tablename__ = "oel_chemical_limits"
    __table_args__ = (
        UniqueConstraint("chemical_id", "source_row_key", name="uq_oel_chemical_row"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    chemical_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chemical_registry.id", ondelete="CASCADE"),
        index=True,
    )
    twa: Mapped[float | None] = mapped_column(Float)
    stel: Mapped[float | None] = mapped_column(Float)
    ceiling: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(64))
    page_number: Mapped[int | None] = mapped_column(Integer)
    persian_name: Mapped[str | None] = mapped_column(String(512))
    english_name: Mapped[str | None] = mapped_column(String(512))
    standard_reference: Mapped[str | None] = mapped_column(String(256))
    source_table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
    )
    source_table_id_str: Mapped[str | None] = mapped_column(String(128))
    source_row_key: Mapped[str | None] = mapped_column(String(128), index=True)
    source_cell_provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    original_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    accepted_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_status: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    gold_artifact_path: Mapped[str | None] = mapped_column(String(1024))
    gold_version: Mapped[str | None] = mapped_column(String(64))
    knowledge_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)

    chemical: Mapped[ChemicalRegistry] = relationship(back_populates="oel_limits")


class VibrationLimit(Base, TimestampMixin):
    __tablename__ = "vibration_limits"

    id: Mapped[uuid.UUID] = uuid_pk()
    parameter: Mapped[str | None] = mapped_column(String(128))
    axis: Mapped[str | None] = mapped_column(String(32))
    a8: Mapped[float | None] = mapped_column(Float)
    vdv: Mapped[float | None] = mapped_column(Float)
    action_limit: Mapped[float | None] = mapped_column(Float)
    exposure_limit: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(64))
    standard_reference: Mapped[str | None] = mapped_column(String(256))
    source_table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
    )
    original_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)


class NoiseLimit(Base, TimestampMixin):
    __tablename__ = "noise_limits"

    id: Mapped[uuid.UUID] = uuid_pk()
    laeq: Mapped[float | None] = mapped_column(Float)
    dose: Mapped[float | None] = mapped_column(Float)
    duration: Mapped[float | None] = mapped_column(Float)
    criterion_level: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(64))
    standard_reference: Mapped[str | None] = mapped_column(String(256))
    source_table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
    )
    original_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)


class BiologicalExposureLimit(Base, TimestampMixin):
    __tablename__ = "biological_exposure_limits"

    id: Mapped[uuid.UUID] = uuid_pk()
    chemical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chemical_registry.id", ondelete="SET NULL"),
        index=True,
    )
    indicator: Mapped[str | None] = mapped_column(String(256))
    specimen: Mapped[str | None] = mapped_column(String(128))
    bei_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(64))
    sampling_time: Mapped[str | None] = mapped_column(String(128))
    standard_reference: Mapped[str | None] = mapped_column(String(256))
    source_table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
    )
    original_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)

    chemical: Mapped[ChemicalRegistry | None] = relationship(back_populates="biological_limits")


class RegulatoryConstraint(Base, TimestampMixin):
    __tablename__ = "regulatory_constraints"

    id: Mapped[uuid.UUID] = uuid_pk()
    related_object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    related_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    excluded_context: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(32), default="warning", nullable=False)
    source_page: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)


# ---------------------------------------------------------------------------
# Semantic Layer — Vector chunks
# ---------------------------------------------------------------------------


class DocumentChunk(Base, TimestampMixin):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_id", name="uq_document_chunk_stable"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[str | None] = mapped_column(String(128), index=True)
    section: Mapped[str | None] = mapped_column(String(512))
    section_id: Mapped[str | None] = mapped_column(String(128))
    section_title: Mapped[str | None] = mapped_column(String(512))
    topic: Mapped[str | None] = mapped_column(String(256))
    hazard: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_type: Mapped[ChunkType] = mapped_column(
        Enum(ChunkType, name="chunk_type_enum", native_enum=False),
        default=ChunkType.PARAGRAPH,
        nullable=False,
    )
    page_number: Mapped[int | None] = mapped_column(Integer)
    printed_page_number: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(16))
    source_type: Mapped[str | None] = mapped_column(String(64))
    standard_reference: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[float | None] = mapped_column(Float)
    validation_status: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    content_version: Mapped[str | None] = mapped_column(String(64))
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gold_artifact_path: Mapped[str | None] = mapped_column(String(1024))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    embedding = _vector_column()

    document: Mapped[Document] = relationship(back_populates="chunks")


class KnowledgeSyncRun(Base):
    __tablename__ = "knowledge_sync_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    sync_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)


class NoDataEvent(Base):
    """Demand signal when authoritative data is unavailable (HITL prioritization)."""

    __tablename__ = "no_data_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    reason: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chemical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chemical_registry.id", ondelete="SET NULL"),
        index=True,
    )
    cas: Mapped[str | None] = mapped_column(String(32), index=True)
    query_text: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    intent: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


# ---------------------------------------------------------------------------
# Validation reports
# ---------------------------------------------------------------------------


class ExtractionValidationReport(Base, TimestampMixin):
    __tablename__ = "extraction_validation_reports"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    table_structure_confidence: Mapped[float | None] = mapped_column(Float)
    schema_mapping_confidence: Mapped[float | None] = mapped_column(Float)
    composite_confidence: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Human-in-the-Loop Review System (production workflow)
# ---------------------------------------------------------------------------


class ReviewTaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ReviewDecisionType(str, enum.Enum):
    APPROVE = "approve"
    CORRECT = "correct"
    REJECT = "reject"


class ReviewPriority(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewTargetType(str, enum.Enum):
    TABLE = "table"
    TABLE_CELL = "table_cell"
    HEADER = "header"
    ENTITY = "entity"
    FORMULA = "formula"
    TEXT_CHUNK = "text_chunk"
    PAGE = "page"


class ReviewEventType(str, enum.Enum):
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_CLAIMED = "task_claimed"
    TASK_RELEASED = "task_released"
    DECISION_SUBMITTED = "decision_submitted"
    CORRECTION_CREATED = "correction_created"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_COMPLETED = "validation_completed"
    TASK_RESOLVED = "task_resolved"
    TASK_REJECTED = "task_rejected"
    TASK_CANCELLED = "task_cancelled"


class ReviewerType(str, enum.Enum):
    HUMAN = "human"
    SYSTEM = "system"
    LLM = "llm"


class CandidateStatus(str, enum.Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"


class CorrectionType(str, enum.Enum):
    HEADER_MAPPING = "header_mapping"
    FIELD_VALUE = "field_value"
    ENTITY_LINK = "entity_link"
    STRUCTURE = "structure"
    OTHER = "other"


class ExtractionCandidate(Base):
    __tablename__ = "extraction_candidates"
    __table_args__ = (
        UniqueConstraint("document_id", "candidate_type", "stable_id", "version", name="uq_candidate_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stable_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_candidates.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus, name="candidate_status_enum", native_enum=False),
        default=CandidateStatus.CANDIDATE,
        nullable=False,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, index=True)
    created_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="extraction_candidates")
    validation_runs: Mapped[list[ValidationRun]] = relationship(back_populates="candidate")
    review_tasks: Mapped[list[ReviewTask]] = relationship(back_populates="candidate")


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_candidates.id", ondelete="SET NULL"),
        index=True,
    )
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128), index=True)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    acceptance_contract: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    overall_status: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped[ExtractionCandidate | None] = relationship(back_populates="validation_runs")
    machine_issues: Mapped[list[ValidationIssueRecord]] = relationship(
        back_populates="validation_run",
        cascade="all, delete-orphan",
    )
    review_tasks: Mapped[list[ReviewTask]] = relationship(back_populates="validation_run")


class ValidationIssueRecord(Base):
    """Machine-generated validation issue — separate from human review tasks."""

    __tablename__ = "validation_issues"

    id: Mapped[uuid.UUID] = uuid_pk()
    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        index=True,
    )
    issue_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    field_name: Mapped[str | None] = mapped_column(String(128))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128))
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    validation_run: Mapped[ValidationRun] = relationship(back_populates="machine_issues")


class ReviewTask(Base, TimestampMixin):
    __tablename__ = "review_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_review_task_idempotency"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer, index=True)
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extracted_tables.id", ondelete="SET NULL"),
        index=True,
    )
    cell_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("table_cells.id", ondelete="SET NULL"),
        index=True,
    )
    stable_table_id: Mapped[str | None] = mapped_column(String(128), index=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_candidates.id", ondelete="SET NULL"),
        index=True,
    )
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("validation_runs.id", ondelete="SET NULL"),
        index=True,
    )
    target_type: Mapped[ReviewTargetType] = mapped_column(
        Enum(ReviewTargetType, name="review_target_type_enum", native_enum=False),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    primary_issue_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReviewTaskStatus] = mapped_column(
        Enum(ReviewTaskStatus, name="review_task_status_enum", native_enum=False),
        default=ReviewTaskStatus.PENDING,
        nullable=False,
        index=True,
    )
    priority: Mapped[ReviewPriority] = mapped_column(
        Enum(ReviewPriority, name="review_priority_enum", native_enum=False),
        default=ReviewPriority.MEDIUM,
        nullable=False,
        index=True,
    )
    assigned_to: Mapped[str | None] = mapped_column(String(256), index=True)
    reviewer_type: Mapped[str | None] = mapped_column(String(16))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issue_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    document: Mapped[Document] = relationship(back_populates="review_tasks")
    candidate: Mapped[ExtractionCandidate | None] = relationship(back_populates="review_tasks")
    validation_run: Mapped[ValidationRun | None] = relationship(back_populates="review_tasks")
    decisions: Mapped[list[ReviewDecision]] = relationship(back_populates="review_task", cascade="all, delete-orphan")
    corrections: Mapped[list[ReviewCorrection]] = relationship(back_populates="review_task", cascade="all, delete-orphan")
    events: Mapped[list[ReviewEvent]] = relationship(back_populates="review_task", cascade="all, delete-orphan")


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    reviewer_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_type: Mapped[ReviewerType] = mapped_column(
        Enum(ReviewerType, name="reviewer_type_enum", native_enum=False),
        nullable=False,
    )
    decision: Mapped[ReviewDecisionType] = mapped_column(
        Enum(ReviewDecisionType, name="review_decision_type_enum", native_enum=False),
        nullable=False,
    )
    decision_reason: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("validation_runs.id", ondelete="SET NULL"),
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review_task: Mapped[ReviewTask] = relationship(back_populates="decisions")


class ReviewCorrection(Base):
    __tablename__ = "review_corrections"

    id: Mapped[uuid.UUID] = uuid_pk()
    review_decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_decisions.id", ondelete="CASCADE"),
        index=True,
    )
    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    correction_type: Mapped[CorrectionType] = mapped_column(
        Enum(CorrectionType, name="correction_type_enum", native_enum=False),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    evidence_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review_task: Mapped[ReviewTask] = relationship(back_populates="corrections")


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    review_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[ReviewEventType] = mapped_column(
        Enum(ReviewEventType, name="review_event_type_enum", native_enum=False),
        nullable=False,
    )
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_type: Mapped[ReviewerType] = mapped_column(
        Enum(ReviewerType, name="review_event_actor_type_enum", native_enum=False),
        nullable=False,
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    review_task: Mapped[ReviewTask] = relationship(back_populates="events")
