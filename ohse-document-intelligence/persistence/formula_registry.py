"""Formula registry synchronization from approved Gold formulas."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.logging import get_logger
from config.settings import Settings, get_settings
from database.models import Document, Formula, KnowledgeSyncRun
from knowledge.metadata_contract import KnowledgeMetadata, SourceReference
from persistence.knowledge_pipeline import ensure_document, reject_candidate_sync, resolve_document_content_hash

logger = get_logger(__name__)


@dataclass
class FormulaSyncStats:
    processed: int = 0
    synced: int = 0
    rejected: int = 0
    skipped: int = 0
    duplicates_prevented: int = 0
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "synced": self.synced,
            "rejected": self.rejected,
            "skipped": self.skipped,
            "duplicates_prevented": self.duplicates_prevented,
            "unresolved": self.unresolved,
        }


def is_production_formula(payload: dict[str, Any], path: Path) -> tuple[bool, str]:
    if reject_candidate_sync(path):
        return False, "candidate_path_forbidden"
    status = payload.get("status") or (payload.get("validation") or {}).get("overall")
    if status not in {"approved", "accepted"}:
        return False, f"status_not_approved:{status}"
    return True, "accepted"


def sync_formula_file(
    session: Session,
    path: Path,
    document: Document,
    stats: FormulaSyncStats,
    *,
    pipeline_version: str,
) -> None:
    stats.processed += 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed, reason = is_production_formula(payload, path)
    if not allowed:
        stats.rejected += 1
        stats.unresolved.append(f"{path.name}: {reason}")
        return

    stable_id = payload.get("formula_id") or path.stem
    existing = session.scalar(select(Formula).where(Formula.stable_formula_id == stable_id))
    if existing:
        stats.duplicates_prevented += 1

    reconstruction = payload.get("reconstruction") or {}
    semantics = payload.get("semantics") or {}
    evidence = payload.get("evidence") or {}
    page = evidence.get("page") or payload.get("source_reference", {}).get("page") or 0

    variables = semantics.get("variables") or {}
    var_descriptions = {k: v.get("description") for k, v in variables.items() if isinstance(v, dict)}

    persian_desc = None
    for var in variables.values():
        if isinstance(var, dict) and var.get("description"):
            persian_desc = var["description"]
            break

    gold_path = str(path)
    metadata = KnowledgeMetadata(
        record_type="formula",
        record_id=stable_id,
        document_id=str(document.id),
        language="fa",
        validation_status="accepted",
        gold_artifact_path=gold_path,
        gold_version=pipeline_version,
        source_reference=SourceReference(
            document_id=str(document.id),
            page_number=page,
            formula_id=stable_id,
            bbox=evidence.get("bbox"),
            evidence_path=f"evidence/pages/page_{page:03d}.json" if page else None,
        ),
    )

    record = existing or Formula(
        id=uuid.uuid4(),
        stable_formula_id=stable_id,
        document_id=document.id,
    )
    record.formula_name = stable_id
    record.persian_name = persian_desc
    record.domain = semantics.get("formula_type") or "exposure_calculation"
    record.original_expression = payload.get("raw_expression") or reconstruction.get("raw_expression") or ""
    record.normalized_expression = payload.get("normalized_expression") or reconstruction.get("expression") or ""
    record.variables = var_descriptions
    record.unit = (semantics.get("units") or {}).get("result") if isinstance(semantics.get("units"), dict) else None
    record.description = persian_desc
    record.page_number = int(page)
    record.bbox = evidence.get("bbox")
    record.confidence = (payload.get("confidence") or {}).get("overall")
    record.validation_status = "accepted"
    record.formula_version = pipeline_version
    record.source_reference = payload.get("source_reference") or evidence
    record.semantics = semantics
    record.applicability_conditions = payload.get("applicability_conditions")
    record.gold_artifact_path = gold_path
    record.knowledge_metadata = metadata.to_dict()

    if not existing:
        session.add(record)
        stats.synced += 1
    session.flush()


def sync_gold_formulas(
    session: Session,
    *,
    formulas_dir: Path | None = None,
    settings: Settings | None = None,
) -> FormulaSyncStats:
    settings = settings or get_settings()
    formulas_dir = formulas_dir or (settings.gold_dir / "formulas")
    stats = FormulaSyncStats()
    content_hash, filename = resolve_document_content_hash(settings)
    document = ensure_document(session, content_hash=content_hash, filename=filename, settings=settings)

    run = KnowledgeSyncRun(
        sync_type="gold_formulas",
        pipeline_version=settings.goldset_pipeline_version,
        status="running",
    )
    session.add(run)
    session.flush()

    for path in sorted(formulas_dir.glob("*.json")):
        try:
            sync_formula_file(
                session,
                path,
                document,
                stats,
                pipeline_version=settings.goldset_pipeline_version,
            )
        except Exception as exc:
            stats.rejected += 1
            stats.unresolved.append(f"{path.name}: {exc}")
            logger.exception("formula_sync_failed", path=str(path))

    run.stats = stats.to_dict()
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    return stats
