"""Semantic knowledge store — Persian production RAG only."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from config.logging import get_logger
from config.settings import Settings, get_settings
from database.models import ChunkType, Document, DocumentChunk, KnowledgeSyncRun
from knowledge.metadata_contract import KnowledgeMetadata, SourceReference
from persistence.knowledge_pipeline import ensure_document, resolve_document_content_hash
from retrieval.embeddings import EmbeddingService, get_embedding_service
from retrieval.semantic_retrieval import (
    SEMANTIC_LANGUAGE,
    SEMANTIC_SOURCE_TYPE,
    SEMANTIC_VALIDATION_STATUS,
    SemanticRetrievalResult,
)

logger = get_logger(__name__)

EMBEDDING_VERSION = "1"
CONTENT_VERSION = "1"
PRODUCTION_JSONL = "semantic_text_production.jsonl"


@dataclass
class SemanticSyncStats:
    chunks_processed: int = 0
    chunks_synced: int = 0
    chunks_skipped: int = 0
    chunks_rejected: int = 0
    duplicates_prevented: int = 0
    embedded: int = 0
    embed_failed: int = 0
    embed_batches: int = 0
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: (v if not isinstance(v, list) else list(v)) for k, v in self.__dict__.items()}


def _chunk_content(record: dict[str, Any]) -> str:
    """Prefer Persian retrieval text without translating."""
    for key in ("text", "normalized_text", "semantic_text", "raw_text"):
        value = record.get(key)
        if value and isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _content_hash(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def is_production_semantic_record(record: dict[str, Any]) -> tuple[bool, str]:
    if record.get("chunk_type") != SEMANTIC_SOURCE_TYPE:
        return False, "not_semantic_text"
    if record.get("review_status") != "accepted":
        return False, f"review_status={record.get('review_status')}"
    if not _chunk_content(record):
        return False, "empty_content"
    return True, "accepted"


def upsert_semantic_chunk(
    session: Session,
    document: Document,
    record: dict[str, Any],
    *,
    gold_path: str,
    pipeline_version: str,
) -> tuple[DocumentChunk, bool]:
    """Upsert one semantic chunk. Returns (chunk, content_changed)."""
    chunk_id = record.get("chunk_id")
    if not chunk_id:
        raise ValueError("semantic chunk missing chunk_id")

    content = _chunk_content(record)
    content_fingerprint = _content_hash(content)
    existing = session.scalar(
        select(DocumentChunk).where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.chunk_id == chunk_id,
        )
    )

    section = record.get("section") or {}
    page_numbers = record.get("page_numbers") or []
    page_number = page_numbers[0] if page_numbers else record.get("pdf_page_number")

    metadata = KnowledgeMetadata(
        record_type="semantic",
        record_id=chunk_id,
        document_id=str(document.id),
        language="fa",
        validation_status="accepted",
        gold_artifact_path=gold_path,
        gold_version=pipeline_version,
        source_reference=SourceReference(
            document_id=str(document.id),
            page_number=page_number,
            printed_page_number=record.get("printed_page_number"),
            chunk_id=chunk_id,
            evidence_path=f"evidence/pages/page_{page_number:03d}.json" if page_number else None,
            bbox=record.get("bbox"),
        ),
        extra={
            "table_refs": (record.get("source_references") or {}).get("table_ids", []),
            "formula_refs": (record.get("source_references") or {}).get("formula_ids", []),
            "entity_refs": (record.get("source_references") or {}).get("entity_ids", []),
        },
    )

    content_changed = False
    chunk = existing or DocumentChunk(
        id=uuid.uuid4(),
        document_id=document.id,
        chunk_id=chunk_id,
    )

    if existing and (existing.content or "") != content:
        content_changed = True
    elif not existing:
        content_changed = True

    chunk.section = section.get("title") if isinstance(section, dict) else str(section) if section else None
    chunk.section_title = chunk.section
    chunk.section_id = "/".join(section.get("path", [])) if isinstance(section, dict) else None
    chunk.topic = record.get("chunk_type")
    chunk.content = content
    chunk.chunk_type = ChunkType.PARAGRAPH
    chunk.page_number = page_number
    chunk.printed_page_number = record.get("printed_page_number")
    chunk.language = "fa"
    chunk.source_type = SEMANTIC_SOURCE_TYPE
    chunk.validation_status = "accepted"
    chunk.content_version = f"{CONTENT_VERSION}:{content_fingerprint}"
    chunk.provenance = record.get("provenance") or metadata.source_reference.to_dict()
    chunk.gold_artifact_path = gold_path
    chunk.metadata_ = {"knowledge_metadata": metadata.to_dict(), **record}
    chunk.confidence = 0.95

    if content_changed:
        chunk.embedding = None
        chunk.embedding_model = None
        chunk.embedding_version = None
        chunk.embedding_dimension = None

    if not existing:
        session.add(chunk)
    session.flush()
    return chunk, content_changed


def sync_semantic_production_corpus(
    session: Session,
    *,
    rag_dir: Path | None = None,
    settings: Settings | None = None,
) -> SemanticSyncStats:
    """Sync ONLY accepted Persian semantic_text_production.jsonl → document_chunks."""
    settings = settings or get_settings()
    rag_dir = rag_dir or (settings.gold_dir / "rag")
    production_path = rag_dir / PRODUCTION_JSONL
    stats = SemanticSyncStats()

    if not production_path.exists():
        raise FileNotFoundError(
            f"Missing {PRODUCTION_JSONL}. Run semantic promotion first."
        )

    content_hash, filename = resolve_document_content_hash(settings)
    document = ensure_document(session, content_hash=content_hash, filename=filename, settings=settings)

    # Remove duplicate semantic rows tied to other OHE6 document records.
    stale_ids = session.scalars(
        select(Document.id).where(
            Document.filename == filename,
            Document.content_hash != content_hash,
        )
    ).all()
    if stale_ids:
        session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id.in_(stale_ids),
                DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
            )
        )
        session.flush()
        logger.info("semantic_stale_chunks_removed", documents=len(stale_ids))

    run = KnowledgeSyncRun(
        sync_type="semantic_production",
        pipeline_version=settings.goldset_pipeline_version,
        status="running",
    )
    session.add(run)
    session.flush()

    for line in production_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        stats.chunks_processed += 1
        record = json.loads(line)
        allowed, reason = is_production_semantic_record(record)
        if not allowed:
            stats.chunks_rejected += 1
            stats.unresolved.append(f"{record.get('chunk_id')}: {reason}")
            continue

        chunk_id = record.get("chunk_id")
        existing = session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.document_id == document.id,
                DocumentChunk.chunk_id == chunk_id,
            )
        )
        if existing:
            stats.duplicates_prevented += 1

        _, changed = upsert_semantic_chunk(
            session,
            document,
            record,
            gold_path=str(production_path),
            pipeline_version=settings.goldset_pipeline_version,
        )
        stats.chunks_synced += 1
        if existing and not changed:
            stats.chunks_skipped += 1

    run.stats = stats.to_dict()
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    return stats


def embed_semantic_chunks(
    session: Session,
    *,
    settings: Settings | None = None,
    batch_size: int = 16,
    commit_every: int = 4,
) -> SemanticSyncStats:
    """Embed ONLY Persian semantic_text chunks (not legacy RAG / QA / evidence)."""
    settings = settings or get_settings()
    stats = SemanticSyncStats()
    embedder = get_embedding_service()

    if not embedder.available():
        raise RuntimeError("Embedding service unavailable — check GCP credentials")

    pending = session.scalars(
        select(DocumentChunk).where(
            DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
            DocumentChunk.validation_status == "accepted",
            DocumentChunk.embedding.is_(None),
        ).order_by(DocumentChunk.chunk_id)
    ).all()

    logger.info("semantic_embed_start", pending=len(pending), batch_size=batch_size)

    batch: list[DocumentChunk] = []
    for chunk in pending:
        batch.append(chunk)
        if len(batch) < batch_size:
            continue
        stats.embed_batches += 1
        _embed_batch(session, batch, embedder, settings, stats)
        batch = []
        if stats.embed_batches % commit_every == 0:
            session.flush()
            logger.info(
                "semantic_embed_progress",
                embedded=stats.embedded,
                failed=stats.embed_failed,
                batches=stats.embed_batches,
            )

    if batch:
        stats.embed_batches += 1
        _embed_batch(session, batch, embedder, settings, stats)

    session.flush()
    logger.info(
        "semantic_embed_complete",
        embedded=stats.embedded,
        failed=stats.embed_failed,
        total_pending=len(pending),
    )
    return stats


def _embed_batch(
    session: Session,
    chunks: list[DocumentChunk],
    embedder: EmbeddingService,
    settings: Settings,
    stats: SemanticSyncStats,
) -> None:
    texts: list[str] = []
    valid: list[DocumentChunk] = []
    for chunk in chunks:
        stats.chunks_processed += 1
        if not (chunk.content or "").strip():
            stats.embed_failed += 1
            stats.unresolved.append(f"{chunk.chunk_id}: empty_content")
            continue
        texts.append(chunk.content)
        valid.append(chunk)

    if not texts:
        return

    try:
        vectors = embedder.embed_texts(texts)
    except Exception as exc:
        for chunk in valid:
            stats.embed_failed += 1
            stats.unresolved.append(f"{chunk.chunk_id}: batch_error:{exc}")
        logger.warning("semantic_embed_batch_failed", error=str(exc), size=len(texts))
        return

    for chunk, vector in zip(valid, vectors, strict=True):
        if len(vector) != embedder.dimension:
            stats.embed_failed += 1
            stats.unresolved.append(
                f"{chunk.chunk_id}: dimension_mismatch:{len(vector)}!={embedder.dimension}"
            )
            continue
        chunk.embedding = vector
        chunk.embedding_model = settings.embedding_model
        chunk.embedding_version = EMBEDDING_VERSION
        chunk.embedding_dimension = embedder.dimension
        stats.embedded += 1


def _chunk_to_retrieval_result(chunk: DocumentChunk, score: float) -> SemanticRetrievalResult:
    meta = chunk.metadata_ or {}
    knowledge_meta = meta.get("knowledge_metadata") or {}
    source_reference = knowledge_meta.get("source_reference") or chunk.provenance or {}
    chunk_type = chunk.chunk_type.value if chunk.chunk_type else chunk.topic
    return SemanticRetrievalResult(
        chunk_id=chunk.chunk_id or "",
        content=chunk.content,
        score=float(score),
        document_id=str(chunk.document_id),
        page_number=chunk.page_number,
        printed_page_number=chunk.printed_page_number,
        section_id=chunk.section_id,
        section_title=chunk.section_title,
        chunk_type=chunk_type,
        language=chunk.language,
        source_type=chunk.source_type or SEMANTIC_SOURCE_TYPE,
        validation_status=chunk.validation_status,
        embedding_model=chunk.embedding_model,
        embedding_version=chunk.embedding_version,
        embedding_dimension=chunk.embedding_dimension,
        content_version=chunk.content_version,
        provenance=chunk.provenance,
        source_reference=source_reference,
        gold_artifact_path=chunk.gold_artifact_path,
    )


def _production_semantic_query(session: Session, *, document_id: uuid.UUID | None = None):
    """Base query scoped to production Persian semantic chunks only."""
    stmt = select(DocumentChunk).where(
        DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
        DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
        DocumentChunk.embedding.is_not(None),
        DocumentChunk.language == SEMANTIC_LANGUAGE,
    )
    if document_id is not None:
        stmt = stmt.where(DocumentChunk.document_id == document_id)
    return stmt


def embed_query_vector(query: str, *, embedder: EmbeddingService | None = None) -> list[float]:
    """Embed query text without holding a DB connection."""
    query = (query or "").strip()
    if not query:
        raise ValueError("empty query")
    svc = embedder or get_embedding_service()
    vectors = svc.embed_texts([query])
    if not vectors:
        raise RuntimeError("embedding returned empty")
    vector = vectors[0]
    if len(vector) != svc.dimension:
        raise ValueError(
            f"Query embedding dimension {len(vector)} != configured {svc.dimension}"
        )
    return vector


def search_by_query_vector(
    session: Session,
    query_vector: list[float],
    *,
    document_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """pgvector search using a pre-computed query vector (short DB hold)."""
    distance_expr = DocumentChunk.embedding.cosine_distance(query_vector)
    score_expr = (1 - distance_expr).label("score")

    stmt = (
        select(DocumentChunk, score_expr)
        .where(
            DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
            DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.language == SEMANTIC_LANGUAGE,
            *([DocumentChunk.document_id == document_id] if document_id else []),
        )
        .order_by(distance_expr)
        .limit(max(1, limit))
    )

    rows = session.execute(stmt).all()
    results: list[dict[str, Any]] = []
    for chunk, score in rows:
        if chunk.source_type != SEMANTIC_SOURCE_TYPE or chunk.validation_status != SEMANTIC_VALIDATION_STATUS:
            logger.error(
                "semantic_scope_violation",
                chunk_id=chunk.chunk_id,
                source_type=chunk.source_type,
                validation_status=chunk.validation_status,
            )
            continue
        results.append(_chunk_to_retrieval_result(chunk, score).to_dict())
    return results


def search_persian_semantic(
    session: Session,
    query: str,
    *,
    document_id: uuid.UUID | None = None,
    limit: int = 5,
    query_vector: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve accepted Persian semantic chunks via Gemini embedding + pgvector cosine search.

    Production scope (always enforced):
        source_type = 'semantic_text'
        validation_status = 'accepted'
        embedding IS NOT NULL
        language = 'fa'

    Query embedding uses the same Gemini model/dimension as stored chunk embeddings.
    Content is returned exactly as stored — numerics are never modified.

    When ``query_vector`` is omitted, embedding happens before any DB query so callers
    can release their session during the Vertex API call.
    """
    query = (query or "").strip()
    if not query:
        return []

    vector = query_vector or embed_query_vector(query)
    return search_by_query_vector(session, vector, document_id=document_id, limit=limit)


def search_persian_semantic_isolated(
    query: str,
    *,
    document_id: uuid.UUID | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Embed + search using a dedicated short-lived DB session (Phase E)."""
    from database.session import SessionLocal

    vector = embed_query_vector(query)
    db = SessionLocal()
    try:
        return search_by_query_vector(db, vector, document_id=document_id, limit=limit)
    finally:
        db.close()


def count_production_semantic_chunks(session: Session) -> dict[str, int]:
    """Return counts for data verification reports."""
    from sqlalchemy import func

    filters = (
        DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
        DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
        DocumentChunk.language == SEMANTIC_LANGUAGE,
    )
    accepted = session.scalar(select(func.count()).select_from(DocumentChunk).where(*filters)) or 0
    embedded = session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(*filters, DocumentChunk.embedding.is_not(None))
    ) or 0
    return {"accepted_semantic": accepted, "embedded_semantic": embedded}
