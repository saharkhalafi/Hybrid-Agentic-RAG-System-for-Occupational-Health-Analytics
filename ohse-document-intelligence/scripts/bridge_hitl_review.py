"""Bridge goldset JSON artifacts into PostgreSQL HITL tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from database.models import Document, ExtractionCandidate
from database.session import session_scope
from review.candidate_store import CandidateStore
from review.revalidation import validate_table_candidate
from review.review_events import log_event
from review.task_factory import create_cases_from_page_validation, upsert_review_case
from database.models import ReviewerType, ReviewEventType, ReviewTargetType, ReviewTask, ReviewTaskStatus

CELL_ID_TABLE_PATTERN = re.compile(r"^cell_(table_\d+_\d+)_")


def _table_id_from_cell_id(cell_id: str) -> str | None:
    match = CELL_ID_TABLE_PATTERN.match(cell_id)
    return match.group(1) if match else None


def _get_or_create_table_candidate(
    session,
    document: Document,
    table_id: str,
    cache: dict[str, ExtractionCandidate | None],
) -> ExtractionCandidate | None:
    """Resolve (or create v1 of) the table candidate referenced by a review item."""
    if table_id in cache:
        return cache[table_id]

    settings = get_settings()
    store = CandidateStore(session)
    existing = store.get_latest(document.id, "table", table_id)
    if existing:
        cache[table_id] = existing
        return existing

    table_path = settings.gold_dir / "tables" / f"{table_id}.json"
    if not table_path.exists():
        table_path = settings.gold_dir / "candidates" / "tables" / f"{table_id}.json"
    if not table_path.exists():
        cache[table_id] = None
        return None

    candidate = store.create_from_file(
        document_id=document.id,
        candidate_type="table",
        stable_id=table_id,
        file_path=table_path,
    )
    cache[table_id] = candidate
    return candidate


def _formula_gold_path(settings, formula_id: str) -> Path | None:
    path = settings.gold_dir / "formulas" / f"{formula_id}.json"
    if not path.exists():
        path = settings.gold_dir / "candidates" / "formulas" / f"{formula_id}.json"
    return path if path.exists() else None


def _formula_is_resolved(settings, formula_id: str) -> bool:
    """True if the current (latest) pipeline run already approved this formula.

    The JSON review_queue.json is append-only across pipeline reruns, so it can
    contain issues from an earlier, broken run that a later run already fixed.
    Importing those as human review tasks would show reviewers stale, already-
    resolved problems."""
    path = _formula_gold_path(settings, formula_id)
    if not path:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("status") == "approved":
        return True
    return (data.get("validation") or {}).get("overall") == "approved"


def _get_or_create_formula_candidate(
    session,
    document: Document,
    formula_id: str,
    cache: dict[str, ExtractionCandidate | None],
) -> ExtractionCandidate | None:
    """Resolve (or create v1 of) the formula candidate referenced by a review item."""
    if formula_id in cache:
        return cache[formula_id]

    store = CandidateStore(session)
    existing = store.get_latest(document.id, "formula", formula_id)
    if existing:
        cache[formula_id] = existing
        return existing

    settings = get_settings()
    formula_path = _formula_gold_path(settings, formula_id)
    if not formula_path:
        cache[formula_id] = None
        return None

    candidate = store.create_from_file(
        document_id=document.id,
        candidate_type="formula",
        stable_id=formula_id,
        file_path=formula_path,
    )
    cache[formula_id] = candidate
    return candidate


def _cancel_stale_page_tasks(session, document: Document, current_keys: set[tuple]) -> int:
    """Cancel generic PAGE-level tasks whose issues have moved into more specific
    (e.g. FORMULA) tasks after regrouping, so reviewers don't see stale duplicates."""
    from sqlalchemy import select as _select

    stale = session.scalars(
        _select(ReviewTask).where(
            ReviewTask.document_id == document.id,
            ReviewTask.target_type == ReviewTargetType.PAGE,
            ReviewTask.status.in_([ReviewTaskStatus.PENDING, ReviewTaskStatus.IN_PROGRESS]),
        )
    ).all()
    cancelled = 0
    for task in stale:
        key = (task.page_number, task.target_type.value, task.target_id)
        if key in current_keys:
            continue
        task.status = ReviewTaskStatus.CANCELLED
        task.resolved_at = datetime.now(timezone.utc)
        task.assigned_to = None
        task.claimed_at = None
        task.claim_expires_at = None
        log_event(
            session,
            review_task_id=task.id,
            event_type=ReviewEventType.TASK_RESOLVED,
            actor_id="system",
            actor_type=ReviewerType.SYSTEM,
            metadata={"reason": "superseded_by_regrouping"},
        )
        cancelled += 1
    return cancelled


def _ensure_document(session, *, filename: str, content_hash: str) -> Document:
    from sqlalchemy import select

    existing = session.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        return existing
    settings = get_settings()
    doc = Document(
        filename=filename,
        content_hash=content_hash,
        language="fa,en",
        processing_version=settings.goldset_pipeline_version,
        page_count=400,
        metadata_={"source": "hitl_bridge"},
    )
    session.add(doc)
    session.flush()
    return doc


def import_validation_page(
    session,
    document: Document,
    page_number: int,
    *,
    force: bool = False,
) -> dict:
    settings = get_settings()
    review_path = settings.gold_dir / "review" / f"validation_page_{page_number:03d}.json"
    if not review_path.exists():
        return {"page": page_number, "skipped": True, "reason": "no validation report"}

    report = json.loads(review_path.read_text(encoding="utf-8"))
    store = CandidateStore(session)
    candidates_by_table: dict = {}

    for table_info in report.get("tables") or []:
        tid = table_info.get("table_id")
        if not tid:
            continue
        table_path = settings.gold_dir / "tables" / f"{tid}.json"
        if not table_path.exists():
            table_path = settings.gold_dir / "candidates" / "tables" / f"{tid}.json"
        if not table_path.exists():
            continue
        candidate = store.create_from_file(
            document_id=document.id,
            candidate_type="table",
            stable_id=tid,
            file_path=table_path,
            page_number=page_number,
        )
        candidates_by_table[tid] = candidate

    run = None
    if candidates_by_table:
        first = next(iter(candidates_by_table.values()))
        run = validate_table_candidate(session, first, pipeline_version=settings.goldset_pipeline_version)

    tasks = create_cases_from_page_validation(
        session,
        document_id=document.id,
        page_number=page_number,
        validation_report=report,
        candidates_by_table=candidates_by_table,
        validation_run=run,
    )
    return {
        "page": page_number,
        "tasks_created": len(tasks),
        "candidates": list(candidates_by_table.keys()),
        "validation_run_id": str(run.id) if run else None,
    }


def import_json_review_queue(session, document: Document) -> dict:
    """Import JSON review_queue.json items, grouping table-linked issues under their
    table's candidate so APPROVE/CORRECT can revalidate them. Items with no resolvable
    table (page-level entities, semantic text chunks) are imported as observational
    tasks without a candidate — these resolve directly on APPROVE/REJECT without
    revalidation, since there is no versioned artifact to re-check."""
    settings = get_settings()
    queue_path = settings.gold_dir / "review" / "review_queue.json"
    if not queue_path.exists():
        return {"imported": 0}

    settings = get_settings()
    data = json.loads(queue_path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    grouped: dict[tuple, list] = {}
    table_candidate_cache: dict[str, ExtractionCandidate | None] = {}
    formula_candidate_cache: dict[str, ExtractionCandidate | None] = {}
    skipped_resolved = 0

    for item in items:
        page = item.get("page_number")
        item_type = item.get("item_type", "unknown")
        chunk_id = item.get("chunk_id")
        formula_id = item.get("formula_id")
        ref = item.get("source_reference") or {}
        cell_ids = ref.get("cell_ids") or []
        table_id = next((_table_id_from_cell_id(cid) for cid in cell_ids if _table_id_from_cell_id(cid)), None)

        if item_type == "formula" and formula_id:
            if _formula_is_resolved(settings, formula_id):
                # A later pipeline run already fixed and approved this formula —
                # this queue entry is stale (append-only queue across reruns).
                skipped_resolved += 1
                continue
            target_id = formula_id
            target_type = ReviewTargetType.FORMULA
        elif table_id:
            target_id = table_id
            target_type = ReviewTargetType.TABLE
        elif item_type == "table_field":
            target_id = cell_ids[0] if cell_ids else f"page_{page}"
            target_type = ReviewTargetType.TABLE_CELL
        elif item_type == "semantic_text":
            target_id = chunk_id or f"page_{page}"
            target_type = ReviewTargetType.TEXT_CHUNK
        else:
            target_id = f"page_{page}"
            target_type = ReviewTargetType.PAGE

        key = (page, target_type.value, target_id)
        grouped.setdefault(key, []).extend(item.get("issues") or [{"message": str(item.get("issues"))}])

    created = 0
    linked_to_candidate = 0
    for (page, ttype, tid), issues in grouped.items():
        normalized_issues = []
        for issue in issues:
            if isinstance(issue, dict):
                normalized_issues.append(issue)
            else:
                normalized_issues.append({"message": str(issue), "severity": "medium", "type": "unknown"})

        candidate = None
        if ttype == ReviewTargetType.TABLE.value:
            candidate = _get_or_create_table_candidate(session, document, tid, table_candidate_cache)
            if candidate:
                linked_to_candidate += 1
        elif ttype == ReviewTargetType.FORMULA.value:
            candidate = _get_or_create_formula_candidate(session, document, tid, formula_candidate_cache)
            if candidate:
                linked_to_candidate += 1

        task = upsert_review_case(
            session,
            document_id=document.id,
            page_number=page,
            target_type=ReviewTargetType(ttype),
            target_id=tid,
            issues=normalized_issues,
            candidate_id=candidate.id if candidate else None,
            stable_table_id=tid if ttype == ReviewTargetType.TABLE.value else None,
        )
        if task:
            created += 1

    stale_cancelled = _cancel_stale_page_tasks(session, document, set(grouped.keys()))

    return {
        "imported": created,
        "groups": len(grouped),
        "linked_to_candidate": linked_to_candidate,
        "skipped_already_resolved": skipped_resolved,
        "stale_page_tasks_cancelled": stale_cancelled,
    }


def migrate_legacy_review_queue(session, document: Document) -> dict:
    from sqlalchemy import select
    from database.models import ReviewQueueItem, ReviewTask, ReviewTaskStatus

    legacy_items = session.scalars(
        select(ReviewQueueItem).where(ReviewQueueItem.document_id == document.id)
    ).all()
    migrated = 0
    for item in legacy_items:
        issues = [{"type": item.issue_type.value, "severity": "medium", "message": item.issue_type.value}]
        task = upsert_review_case(
            session,
            document_id=document.id,
            page_number=item.page_number,
            target_type=ReviewTargetType.TABLE if item.object_type == "extracted_table" else ReviewTargetType.TABLE_CELL,
            target_id=str(item.object_id),
            issues=issues,
            evidence_reference={"legacy_review_queue_id": str(item.id), "bbox": item.bbox},
        )
        if task:
            migrated += 1
    return {"legacy_migrated": migrated}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge goldset JSON into PostgreSQL HITL")
    parser.add_argument("--pdf-name", default="OHE6.pdf")
    parser.add_argument("--pages", type=str, default="47", help="Comma-separated page numbers")
    parser.add_argument("--import-queue", action="store_true", help="Import review_queue.json")
    parser.add_argument("--migrate-legacy", action="store_true", help="Migrate old review_queue table rows")
    args = parser.parse_args()

    settings = get_settings()
    content_hash = hashlib.sha256(f"{args.pdf_name}:hitl-bridge".encode()).hexdigest()
    pages = [int(p.strip()) for p in args.pages.split(",") if p.strip()]

    with session_scope() as session:
        document = _ensure_document(session, filename=args.pdf_name, content_hash=content_hash)
        results = {"document_id": str(document.id), "pages": []}

        for page in pages:
            results["pages"].append(import_validation_page(session, document, page))

        if args.import_queue:
            results["queue_import"] = import_json_review_queue(session, document)

        if args.migrate_legacy:
            results["legacy"] = migrate_legacy_review_queue(session, document)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
