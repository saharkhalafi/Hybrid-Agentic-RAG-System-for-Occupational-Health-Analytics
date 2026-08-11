"""Production Persian semantic retrieval — constants and result contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# Hard production scope for the Semantic Agent (enforced in SQL, not by callers).
SEMANTIC_SOURCE_TYPE = "semantic_text"
SEMANTIC_VALIDATION_STATUS = "accepted"
SEMANTIC_LANGUAGE = "fa"
SEMANTIC_SIMILARITY_METRIC = "cosine_distance"  # pgvector `<=>` operator


@dataclass(frozen=True)
class SemanticRetrievalResult:
    chunk_id: str
    content: str
    score: float
    document_id: str
    page_number: int | None
    printed_page_number: int | None
    section_id: str | None
    section_title: str | None
    chunk_type: str | None
    language: str | None
    source_type: str
    validation_status: str
    embedding_model: str | None
    embedding_version: str | None
    embedding_dimension: int | None
    content_version: str | None
    provenance: dict[str, Any] | None
    source_reference: dict[str, Any] | None
    gold_artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REQUIRED_RESULT_FIELDS = frozenset(SemanticRetrievalResult.__dataclass_fields__.keys())
