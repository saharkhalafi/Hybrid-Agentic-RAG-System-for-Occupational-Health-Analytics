"""Load RAG corpus into PostgreSQL document_chunks (when DB available)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from database.models import ChunkType, Document, DocumentChunk
from database.session import session_scope
from sqlalchemy import select

logger = get_logger(__name__)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def load_rag_corpus(document_id: str | None, rag_dir: Path) -> dict[str, int]:
    stats = {"evidence": 0, "row_knowledge": 0, "regulatory": 0, "semantic_text": 0}

    with session_scope() as session:
        if document_id:
            doc = session.get(Document, uuid.UUID(document_id))
        else:
            doc = session.scalars(select(Document).order_by(Document.created_at.desc()).limit(1)).first()
        if not doc:
            raise RuntimeError("No document found in database — run persist_evidence_pipeline.py first")

        for record in _read_jsonl(rag_dir / "evidence_chunks.jsonl"):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    section=f"table:{record.get('table_id')}",
                    topic=record.get("field"),
                    content=record.get("text") or "",
                    chunk_type=ChunkType.TABLE_CONTEXT,
                    page_number=record.get("page_number"),
                    language="fa,en",
                    source_type="evidence_cell",
                    confidence=0.95 if record.get("value_status") == "extracted" else 0.7,
                    metadata_={
                        "cell_id": record.get("cell_id"),
                        "bbox": record.get("bbox"),
                        "chunk_layer": "evidence",
                        **record,
                    },
                )
            )
            stats["evidence"] += 1

        for record in _read_jsonl(rag_dir / "row_knowledge.jsonl"):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    section=record.get("subject"),
                    topic=record.get("chunk_type"),
                    content=record.get("content") or json.dumps(record, ensure_ascii=False),
                    chunk_type=ChunkType.TABLE_CONTEXT,
                    page_number=record.get("page_number"),
                    language="fa,en",
                    source_type="row_knowledge",
                    confidence=0.9,
                    metadata_={"chunk_layer": "row_knowledge", **record},
                )
            )
            stats["row_knowledge"] += 1

        for record in _read_jsonl(rag_dir / "regulatory_qa.jsonl"):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    section="qa",
                    topic=record.get("target_reference", {}).get("field") if record.get("target_reference") else "qa",
                    content=f"Q: {record.get('question')}\nA: {record.get('answer')}",
                    chunk_type=ChunkType.REGULATORY_NOTE,
                    page_number=record.get("page_number"),
                    language="fa",
                    source_type="regulatory_qa",
                    confidence=0.88,
                    metadata_={"chunk_layer": "regulatory", **record},
                )
            )
            stats["regulatory"] += 1

        for record in _read_jsonl(rag_dir / "semantic_text.jsonl"):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    section=record.get("section", {}).get("title") or "narrative",
                    topic=record.get("chunk_type"),
                    content=record.get("text") or "",
                    chunk_type=ChunkType.PARAGRAPH,
                    page_number=(record.get("page_numbers") or [None])[0],
                    language="fa,en",
                    source_type="semantic_text",
                    confidence=0.85 if record.get("review_status") != "review_required" else 0.6,
                    metadata_={"chunk_layer": "semantic", **record},
                )
            )
            stats["semantic_text"] += 1

    return stats


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-id", default=None)
    args = parser.parse_args()

    settings = get_settings()
    rag_dir = settings.gold_dir / "rag"
    stats = load_rag_corpus(args.document_id, rag_dir)
    print("Loaded RAG corpus to PostgreSQL:")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
