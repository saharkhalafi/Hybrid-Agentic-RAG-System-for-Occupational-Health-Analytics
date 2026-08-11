"""Dedicated Persian semantic RAG pipeline: promote → sync → embed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging
from config.settings import get_settings
from database.session import session_scope
from persistence.semantic_gold_promotion import promote_semantic_gold_for_production
from persistence.semantic_store import embed_semantic_chunks, sync_semantic_production_corpus
from retrieval.embeddings import EmbeddingService


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Persian semantic Gold → pgvector pipeline")
    parser.add_argument("--force-promote", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    settings = get_settings()
    promotion = promote_semantic_gold_for_production(force=args.force_promote)

    with session_scope() as session:
        sync_stats = sync_semantic_production_corpus(session)
        embed_stats = None
        if not args.skip_embed and EmbeddingService().available():
            embed_stats = embed_semantic_chunks(session, batch_size=args.batch_size)

    report = {
        "promotion": promotion.to_dict(),
        "sync": sync_stats.to_dict(),
        "embeddings": embed_stats.to_dict() if embed_stats else None,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.vector_dimension,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
