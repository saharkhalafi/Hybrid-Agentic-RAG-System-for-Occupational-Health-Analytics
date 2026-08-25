"""Scoped embed preflight for Phase 7 — validates retry logic before full rebuild."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from config.settings import get_settings
from database.models import Document, DocumentChunk
from database.session import SessionLocal
from persistence.canonical_chunk_store import CANONICAL_CHUNK_PREFIX
from persistence.domain_reconciliation import GOLD_ARTIFACT_PATH, OHE6_CONTENT_HASH
from persistence.semantic_store import (
    embed_target_chunks,
    verify_chunk_embeddings,
)
from retrieval.embeddings import EmbeddingService, get_embedding_service

PREFLIGHT_PAGE_MIN = 46
PREFLIGHT_PAGE_MAX = 49
PREFLIGHT_TABLE_PATTERN = re.compile(r"table_04[6-9]_\d+")


def find_preflight_embed_chunks(session, *, document_id) -> list[DocumentChunk]:
    """Return canonical row_knowledge chunks from pages 46–49 (prior failure batch)."""
    rows = session.scalars(
        select(DocumentChunk)
        .where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.source_type == "row_knowledge",
            DocumentChunk.validation_status == "accepted",
            DocumentChunk.gold_artifact_path == GOLD_ARTIFACT_PATH,
            or_(
                DocumentChunk.page_number.between(PREFLIGHT_PAGE_MIN, PREFLIGHT_PAGE_MAX),
                DocumentChunk.chunk_id.like(f"{CANONICAL_CHUNK_PREFIX}table_04%"),
            ),
        )
        .order_by(DocumentChunk.chunk_id)
    ).all()
    filtered = [
        chunk
        for chunk in rows
        if (
            chunk.page_number is not None
            and PREFLIGHT_PAGE_MIN <= chunk.page_number <= PREFLIGHT_PAGE_MAX
        )
        or (
            chunk.chunk_id
            and PREFLIGHT_TABLE_PATTERN.search(
                chunk.chunk_id.removeprefix(CANONICAL_CHUNK_PREFIX)
            )
        )
    ]
    return filtered or list(rows)


def execute_embed_preflight(
    *,
    content_hash: str = OHE6_CONTENT_HASH,
    batch_size: int = 16,
    reset_embeddings: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Embed only pages 46–49 chunks to validate batch-retry before full Phase 7."""
    session = SessionLocal()
    result: dict[str, Any] = {
        "phase": "7_preflight_embed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_hash": content_hash,
        "page_range": [PREFLIGHT_PAGE_MIN, PREFLIGHT_PAGE_MAX],
        "commit": commit,
    }

    try:
        document = session.scalar(select(Document).where(Document.content_hash == content_hash))
        if not document:
            raise RuntimeError(f"Canonical document not found for content_hash={content_hash}")

        chunks = find_preflight_embed_chunks(session, document_id=document.id)
        result["target_chunk_ids"] = [c.chunk_id for c in chunks]
        result["target_count"] = len(chunks)

        if not chunks:
            result["success"] = False
            result["error"] = "no_preflight_chunks_found"
            return result

        content_snapshot = {c.chunk_id: len((c.content or "").strip()) for c in chunks}

        if reset_embeddings:
            for chunk in chunks:
                chunk.embedding = None
                chunk.embedding_model = None
                chunk.embedding_version = None
                chunk.embedding_dimension = None
            session.flush()

        embed_stats = embed_target_chunks(session, chunks, batch_size=batch_size)
        result["embeddings"] = embed_stats.to_dict()

        embedder = get_embedding_service()
        verification = verify_chunk_embeddings(chunks, embedder=embedder)
        result["embedding_verification"] = verification

        truncated: list[str] = []
        for chunk in chunks:
            before = content_snapshot.get(chunk.chunk_id, 0)
            after = len((chunk.content or "").strip())
            if before != after:
                truncated.append(f"{chunk.chunk_id}: content_len {before}->{after}")
        result["content_integrity"] = {"passed": not truncated, "failures": truncated}

        passed = (
            embed_stats.embed_failed == 0
            and verification["passed"]
            and not truncated
        )
        result["success"] = passed

        if passed and commit:
            session.commit()
            result["transaction_committed"] = True
        else:
            session.rollback()
            result["transaction_committed"] = False
            result["rollback_performed"] = True
    except Exception as exc:
        session.rollback()
        result["success"] = False
        result["rollback_performed"] = True
        result["error"] = str(exc)
        raise
    finally:
        session.close()

    return result
