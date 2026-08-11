"""Run evidence pipeline (pages 44-49) and persist to PostgreSQL."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from config.settings import get_settings
from database.session import session_scope
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.structural_resolver import resolve_structure, write_validated_structure
from goldset_generator.table_gold_generator import TableGoldGenerator
from persistence.evidence_pipeline import (
    _content_hash,
    persist_evidence_snapshot,
    persist_universal_tables,
    persist_validated_rows_and_domain,
    upsert_document,
)

logger = get_logger(__name__)


def run(pdf_path: Path, *, start_page: int, end_page: int) -> dict:
    settings = get_settings()
    processor = DocumentProcessor()
    evidence = processor.process(
        pdf_path,
        start_page=start_page,
        end_page=end_page,
        skip_document_ai=True,
    )
    structural = resolve_structure(evidence, pdf_path)
    write_validated_structure(structural, evidence, start_page=start_page, end_page=end_page)

    table_gold_gen = TableGoldGenerator()
    table_golds = [table_gold_gen.generate(t.to_dict()) for t in structural.tables]

    content_hash = _content_hash(pdf_path.name, start_page, end_page, evidence.content_hash)
    raw_path = settings.data_intermediate_dir / f"document_ai_raw_{start_page}-{end_page}.json"

    with session_scope() as session:
        document = upsert_document(
            session,
            filename=pdf_path.name,
            content_hash=content_hash,
            page_count=end_page - start_page + 1,
            processing_version=settings.goldset_pipeline_version,
        )

        persist_evidence_snapshot(
            session,
            document,
            page_start=start_page,
            page_end=end_page,
            raw_json_path=raw_path,
            processor_format=evidence.processor_format,
        )

        stable_map = persist_universal_tables(
            session,
            document,
            structural,
            structural.page_detection,
        )
        domain_stats = persist_validated_rows_and_domain(
            session,
            document,
            table_golds,
            stable_map,
        )

        return {
            "document_id": str(document.id),
            "tables": len(stable_map),
            "cells": len(structural.cells),
            **domain_stats,
        }


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Evidence pipeline → PostgreSQL (pages 44-49)")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--start-page", type=int, default=44)
    parser.add_argument("--end-page", type=int, default=49)
    args = parser.parse_args()

    result = run(args.file, start_page=args.start_page, end_page=args.end_page)
    print("Evidence pipeline persisted:")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
