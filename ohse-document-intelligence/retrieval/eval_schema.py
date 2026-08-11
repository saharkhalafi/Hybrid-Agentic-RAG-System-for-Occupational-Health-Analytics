"""Pydantic schema for production retrieval evaluation records."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ReviewStatus(str, Enum):
    GENERATED = "generated"
    MACHINE_VALIDATED = "machine_validated"
    HUMAN_REVIEWED = "human_reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class RelevanceLevel(str, Enum):
    REQUIRED = "REQUIRED"
    SUPPORTING = "SUPPORTING"
    OPTIONAL = "OPTIONAL"
    IRRELEVANT = "IRRELEVANT"


class RelevanceItem(BaseModel):
    source_type: Literal["structured", "semantic_text", "formula", "document"]
    relevance: RelevanceLevel
    table: str | None = None
    record_id: str | None = None
    chunk_id: str | None = None
    formula_id: str | None = None
    field: str | None = None
    chemical: str | None = None
    cas: str | None = None
    page_number: int | None = None
    printed_page_number: int | None = None
    cell_id: str | None = None
    bbox: dict[str, float] | None = None
    document_id: str | None = None
    gold_artifact_path: str | None = None
    notes: str | None = None


class NumericGroundTruth(BaseModel):
    value: str | float | None = None
    normalized_value: float | None = None
    unit: str | None = None
    source_table: str | None = None
    source_record_id: str | None = None
    source_row_key: str | None = None
    source_cell_id: str | None = None
    original_value: str | None = None
    page_number: int | None = None
    bbox: dict[str, float] | None = None
    field: str | None = None


class CitationRequirement(BaseModel):
    page_number: int | None = None
    printed_page_number: int | None = None
    chunk_id: str | None = None
    source_row_key: str | None = None
    formula_id: str | None = None
    cell_id: str | None = None
    source_type: str | None = None
    authority: Literal["postgresql", "formula_engine", "semantic", "none"] = "none"


class FormulaGroundTruth(BaseModel):
    formula_id: str
    normalized_expression: str | None = None
    calculation_inputs: dict[str, float] | None = None
    calculation_output: float | None = None
    valid: bool | None = None


class GroundTruth(BaseModel):
    relevance: list[RelevanceItem] = Field(default_factory=list)
    required_sources: list[str] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    numeric_values: list[NumericGroundTruth] = Field(default_factory=list)
    formula: FormulaGroundTruth | None = None


class MetadataRequirements(BaseModel):
    document_id: str | None = None
    page_number: int | None = None
    printed_page_number: int | None = None
    section: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    chunk_id: str | None = None
    record_id: str | None = None
    source_type: str | None = None
    language: str = "fa"
    table_name: str | None = None
    provenance_required: bool = True
    cell_id: str | None = None
    bbox: dict[str, float] | None = None


class SourceTraceability(BaseModel):
    gold_artifact_path: str | None = None
    evidence_path: str | None = None
    builder_version: str = "1.0.0"
    built_from: Literal["gold_file", "postgresql", "gold_file+postgresql"] = "gold_file"


class RetrievalEvalRecord(BaseModel):
    query_id: str
    session_id: str | None = None
    turn_id: int = 1
    previous_turn_ids: list[str] = Field(default_factory=list)

    query: str
    standalone_query: str | None = None
    resolved_query: str | None = None
    language: str = "fa"

    intent: str
    domain: str
    category: str
    difficulty: Literal["easy", "medium", "hard", "adversarial"] = "medium"

    expected_agents: list[str] = Field(default_factory=list)
    expected_route: str | None = None

    requires_context: bool = False
    requires_clarification: bool = False
    requires_session_context: bool = False

    expected_context: dict[str, Any] = Field(default_factory=dict)
    slots: dict[str, Any] = Field(default_factory=dict)
    expected_slots: dict[str, Any] = Field(default_factory=dict)
    inherited_slots: dict[str, Any] = Field(default_factory=dict)
    previous_turns: list[dict[str, Any]] = Field(default_factory=list)

    expected_answer: str | None = None
    answer_type: Literal[
        "numeric", "text", "boolean", "formula", "clarification", "refusal", "multi"
    ] = "text"
    answer_language: str = "fa"
    legacy_reference_en: str | None = None

    ground_truth: GroundTruth = Field(default_factory=GroundTruth)
    citations: list[CitationRequirement] = Field(default_factory=list)
    metadata_requirements: MetadataRequirements = Field(default_factory=MetadataRequirements)
    source_traceability: SourceTraceability = Field(default_factory=SourceTraceability)

    confidence_requirement: Literal["high", "medium", "low"] = "high"
    review_status: ReviewStatus = ReviewStatus.GENERATED
    tags: list[str] = Field(default_factory=list)

    @field_validator("language", "answer_language")
    @classmethod
    def _must_be_fa(cls, v: str) -> str:
        if v != "fa":
            raise ValueError("production eval records must use language=fa")
        return v

    def to_jsonl_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DatasetStatistics(BaseModel):
    total_records: int = 0
    by_intent: dict[str, int] = Field(default_factory=dict)
    by_agent: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    numerical_queries: int = 0
    formula_queries: int = 0
    conversational_turns: int = 0
    conversational_sessions: int = 0
    negative_adversarial: int = 0
    unique_chemicals: int = 0
    unique_chunks: int = 0
    unique_formulas: int = 0
    unique_pages: int = 0
    rejected_records: int = 0
