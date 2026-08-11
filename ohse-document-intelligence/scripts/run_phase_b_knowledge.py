"""Run Phase B knowledge engineering: structured sync + semantic RAG pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from database.session import session_scope
from persistence.formula_registry import sync_gold_formulas
from persistence.knowledge_pipeline import sync_gold_tables
from persistence.semantic_gold_promotion import promote_semantic_gold_for_production
from persistence.semantic_store import (
    embed_semantic_chunks,
    sync_semantic_production_corpus,
)
from retrieval.embeddings import EmbeddingService

logger = get_logger(__name__)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Phase B — Knowledge Engineering orchestrator")
    parser.add_argument(
        "--skip-structured",
        action="store_true",
        help="Skip gold table + formula sync (semantic-only run)",
    )
    parser.add_argument(
        "--skip-embed",
        action="store_true",
        help="Promote + sync semantic chunks but skip Gemini embedding",
    )
    parser.add_argument(
        "--force-promote",
        action="store_true",
        help="Rebuild semantic_text_production.jsonl from review-pending gold",
    )
    parser.add_argument(
        "--semantic-only",
        action="store_true",
        help="Only promote + sync + embed Persian semantic_text (skip structured sync)",
    )
    parser.add_argument("--batch-size", type=int, default=16, help="Embedding batch size")
    args = parser.parse_args()

    settings = get_settings()
    report: dict = {
        "phase": "B",
        "pipeline_version": settings.goldset_pipeline_version,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.vector_dimension,
    }

    promotion_stats = promote_semantic_gold_for_production(force=args.force_promote)
    report["semantic_promotion"] = promotion_stats.to_dict()

    with session_scope() as session:
        if not args.semantic_only and not args.skip_structured:
            report["gold_tables"] = sync_gold_tables(session).to_dict()
            report["formulas"] = sync_gold_formulas(session).to_dict()

        semantic_stats = sync_semantic_production_corpus(session)
        report["semantic_sync"] = semantic_stats.to_dict()

        if not args.skip_embed:
            embedder = EmbeddingService()
            if embedder.available():
                embed_stats = embed_semantic_chunks(
                    session,
                    batch_size=max(1, args.batch_size),
                )
                report["semantic_embeddings"] = embed_stats.to_dict()
            else:
                logger.warning("embedding_skipped_client_unavailable")
                report["semantic_embeddings"] = {"error": "gcp_client_unavailable"}

    out_path = PROJECT_ROOT / "docs" / "reports" / "phase_b_sync_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Phase B knowledge sync complete")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
