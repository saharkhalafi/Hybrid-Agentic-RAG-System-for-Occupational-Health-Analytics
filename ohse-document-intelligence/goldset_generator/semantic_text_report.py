"""Write semantic-text generation and validation reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_semantic_text_report(
    *,
    start_page: int,
    end_page: int,
    chunks: list[dict[str, Any]],
    page_stats: list[dict[str, Any]],
    corpus_validation: dict[str, Any],
    review_items: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    approved = sum(1 for c in chunks if c.get("review_status") == "pending" and not c.get("validation_issues"))
    needs_review = sum(1 for c in chunks if c.get("review_status") == "review_required")

    payload = {
        "generated_at": now,
        "page_range": {"start": start_page, "end": end_page},
        "summary": {
            "total_chunks": len(chunks),
            "pages_with_chunks": len({c.get("pdf_page_number") for c in chunks}),
            "chunks_pending_review": needs_review,
            "chunks_ready": approved,
            "embedding_status": "pending",
        },
        "corpus_validation": corpus_validation,
        "page_stats": page_stats,
        "review_items": review_items[:200],
    }

    json_path = output_dir / "semantic_text_report.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        "# Semantic Text Pipeline Report",
        "",
        f"Range: **{start_page}–{end_page}**",
        f"Generated: {now}",
        "",
        "## Summary",
        "",
        f"- Total semantic chunks: {len(chunks)}",
        f"- Pages with narrative chunks: {payload['summary']['pages_with_chunks']}",
        f"- Chunks ready (no validation issues): {approved}",
        f"- Chunks requiring review: {needs_review}",
        f"- Duplicate chunks detected: {corpus_validation.get('duplicate_chunks', 0)}",
        f"- Empty chunks detected: {corpus_validation.get('empty_chunks', 0)}",
        "",
        "## Retrieval routing",
        "",
        "- Narrative / semantic questions → `gold/rag/semantic_text.jsonl`",
        "- Structured table / numeric questions → `gold/tables/`",
        "- Formula / calculation questions → `gold/formulas/`",
        "- Entity lookup → `gold/entities/`",
        "",
        "## Validation",
        "",
        "Checks applied: empty chunks, duplicates, broken Persian, OCR artifacts, missing provenance, "
        "table-row leakage, formula-body without reference, orphan cross-links.",
        "",
        f"JSON report: `{json_path}`",
    ]
    md_path = output_dir / "semantic_text_report.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path
