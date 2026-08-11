"""Promote Persian semantic Gold for production RAG (Phase B)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import Settings, get_settings

logger = get_logger(__name__)

PRODUCTION_FILENAME = "semantic_text_production.jsonl"
SOURCE_FILENAME = "semantic_text.jsonl"


@dataclass
class PromotionStats:
    source_chunks: int = 0
    promoted: int = 0
    skipped_rejected: int = 0
    already_accepted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_chunks": self.source_chunks,
            "promoted": self.promoted,
            "skipped_rejected": self.skipped_rejected,
            "already_accepted": self.already_accepted,
            "production_file": PRODUCTION_FILENAME,
        }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def promote_semantic_gold_for_production(
    *,
    rag_dir: Path | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> PromotionStats:
    """Accept review-pending semantic chunks into a production JSONL artifact.

    Original ``semantic_text.jsonl`` is preserved (copied to ``.bak`` on first promotion).
    Production sync reads only ``semantic_text_production.jsonl``.
    """
    settings = settings or get_settings()
    rag_dir = rag_dir or (settings.gold_dir / "rag")
    source_path = rag_dir / SOURCE_FILENAME
    production_path = rag_dir / PRODUCTION_FILENAME
    stats = PromotionStats()

    if not source_path.exists():
        raise FileNotFoundError(f"Missing semantic gold: {source_path}")

    if production_path.exists() and not force:
        logger.info("semantic_production_exists", path=str(production_path))
        stats.promoted = len(_read_jsonl(production_path))
        stats.source_chunks = len(_read_jsonl(source_path))
        return stats

    backup_path = rag_dir / f"{SOURCE_FILENAME}.bak"
    if not backup_path.exists():
        shutil.copy2(source_path, backup_path)
        logger.info("semantic_source_backed_up", backup=str(backup_path))

    promoted_records: list[dict[str, Any]] = []
    for record in _read_jsonl(source_path):
        stats.source_chunks += 1
        if record.get("chunk_type") != "semantic_text":
            continue
        status = record.get("review_status")
        if status == "rejected":
            stats.skipped_rejected += 1
            continue
        if status == "accepted" and production_path.exists() and not force:
            stats.already_accepted += 1

        promoted = dict(record)
        prior_status = promoted.get("review_status")
        promoted["review_status"] = "accepted"
        promoted["embedding_status"] = "pending"
        promoted["production_promotion"] = {
            "promoted_at": datetime.now(UTC).isoformat(),
            "prior_review_status": prior_status,
            "promoted_by": "phase_b_semantic_pipeline",
            "pipeline_version": settings.goldset_pipeline_version,
        }
        promoted_records.append(promoted)
        stats.promoted += 1

    _write_jsonl(production_path, promoted_records)
    logger.info(
        "semantic_gold_promoted",
        promoted=stats.promoted,
        production=str(production_path),
    )
    return stats
