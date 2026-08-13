"""Plan and (optionally) persist geometry-promotion evidence — dry-run capable."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline_contracts.numeric_integrity import parse_numeric_cell
from ingestion.pdf_geometry import extract_cas_numbers

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INTERMEDIATE = PROJECT_ROOT / "data" / "intermediate"
LAYER_A_VS = Path(r"E:\temp\validated_structure_46-55_layer_a.json")
PROMOTION_REPORT_DEFAULT = Path(r"E:\temp\ohse_geometry_promotion_report.json")

STRUCTURE_BATCHES = [
    INTERMEDIATE / "validated_structure_46-70.json",
    INTERMEDIATE / "validated_structure_71-95.json",
    INTERMEDIATE / "validated_structure_96-110.json",
    INTERMEDIATE / "validated_structure_140-140.json",
    INTERMEDIATE / "validated_structure_144-144.json",
]

LAYER_A_PAGES = set(range(46, 56))
CAS_PATTERN = re.compile(r"\[\s*(\d{2,7}-\d{2}-\d)\s*\]")

REQUIRED_EVIDENCE_FIELDS = (
    "evidence_cell_id",
    "page_number",
    "stable_table_id",
    "row_index",
    "column_index",
    "disposition",
)


@dataclass
class PlannedCellEvidence:
    evidence_cell_id: str
    stable_table_id: str
    page_number: int
    row_index: int
    column_index: int
    original_value: str
    normalized_value: str | None
    bbox: dict[str, Any] | None
    bbox_source: str | None
    bbox_confidence: float | None
    match_method: str
    extraction_confidence: float | None
    disposition: str
    rejection_reason: str
    review_flags: list[str]
    validation_flags: list[str]
    input_bbox_source: str | None
    cell_source: str | None
    source_reference: dict[str, Any]
    idempotency_key: str
    action: str  # insert | update
    missing_required: list[str] = field(default_factory=list)


@dataclass
class PlannedOELRow:
    source_row_key: str
    stable_table_id: str
    page_number: int
    row_index: int
    cas: str | None
    english_name: str | None
    persian_name: str | None
    twa: float | None
    stel: float | None
    ceiling: float | None
    unit: str | None
    action: str
    skip_reason: str | None = None
    source_cell_provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannedReviewItem:
    evidence_cell_id: str
    stable_table_id: str
    page_number: int
    row_index: int
    column_index: int
    issue_type: str
    review_flags: list[str]
    idempotency_key: str
    evidence_reference: dict[str, Any]
    action: str


def stable_cell_idempotency_key(
    content_hash: str,
    page_number: int,
    stable_table_id: str,
    row_index: int,
    column_index: int,
) -> str:
    return f"{content_hash}:{page_number}:{stable_table_id}:{row_index}:{column_index}"


def review_task_idempotency_key(document_id: str, pipeline_version: str, evidence_cell_id: str) -> str:
    return f"{document_id}:{pipeline_version}:TABLE_CELL:{evidence_cell_id}"


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    parsed = parse_numeric_cell(value)
    if parsed and parsed.parsed_token:
        try:
            return float(str(parsed.parsed_token).replace("/", "."))
        except ValueError:
            pass
    match = re.search(r"[\d]+(?:[./][\d]+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group().replace("/", "."))
    except ValueError:
        return None


def _extract_cas(text: str | None) -> str | None:
    if not text:
        return None
    nums = extract_cas_numbers(text)
    raw = nums[0] if nums else None
    if not raw:
        match = CAS_PATTERN.search(text)
        raw = match.group(1) if match else None
    return _normalize_cas(raw)


def _normalize_cas(cas: str | None) -> str | None:
    if not cas:
        return None
    cleaned = cas.strip().strip("[]")
    return cleaned or None


PROMOTION_PIPELINE_VERSION = "geometry_promotion_v1"
PROMOTION_PAGES = range(46, 145)


def load_promotion_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_validated_structure_index() -> tuple[str, dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (content_hash, tables_by_id, cells_by_evidence_id)."""
    tables: dict[str, dict[str, Any]] = {}
    cells: dict[str, dict[str, Any]] = {}
    content_hash: str | None = None
    layer_a_pages: set[int] = set()

    sources: list[Path] = []
    if LAYER_A_VS.exists():
        sources.append(LAYER_A_VS)
    sources.extend(STRUCTURE_BATCHES)

    for path in sources:
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not content_hash:
            content_hash = data.get("content_hash")
        for table in data.get("tables", []):
            page = int(table["page_number"])
            tid = str(table["table_id"])
            if path == LAYER_A_VS and page not in LAYER_A_PAGES:
                continue
            if path != LAYER_A_VS and page in layer_a_pages:
                continue
            if path == LAYER_A_VS:
                layer_a_pages.add(page)
            tables[tid] = table
            for row in table.get("rows", []):
                for cell in row:
                    cid = cell.get("cell_id")
                    if cid:
                        cells[str(cid)] = cell

    if not content_hash:
        raise RuntimeError("Could not resolve document content_hash from validated_structure batches")

    return content_hash, tables, cells


def _suspicious_index(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in report.get("suspicious_accepted_cells", []):
        key = (
            f"{item['table_id']}:{item['row_index']}:{item['column_index']}"
        )
        index[key].append(item)
    for item in report.get("suspicious", []):
        if not item.get("persist"):
            continue
        key = f"{item['table_id']}:{item['row_index']}:{item['column_index']}"
        index[key].append(item)
    return index


def build_cell_evidence_plan(
    report: dict[str, Any],
    *,
    content_hash: str,
    vs_cells: dict[str, dict[str, Any]],
    vs_tables: dict[str, dict[str, Any]],
    existing_cell_keys: set[str] | None = None,
) -> list[PlannedCellEvidence]:
    existing_cell_keys = existing_cell_keys or set()
    suspicious = _suspicious_index(report)
    planned: list[PlannedCellEvidence] = []

    for cell in report.get("cells", []):
        evidence_cell_id = str(cell["cell_id"])
        stable_table_id = str(cell["table_id"])
        page_number = int(cell["page_number"])
        row_index = int(cell["row_index"])
        column_index = int(cell["column_index"])
        vs_cell = vs_cells.get(evidence_cell_id, {})
        idem = stable_cell_idempotency_key(
            content_hash, page_number, stable_table_id, row_index, column_index
        )
        disposition = str(cell.get("disposition") or ("accept" if cell.get("persist") else "reject"))
        review_flags = list(cell.get("review_flags") or [])
        validation_flags = list(cell.get("suspicious") or [])
        rejection_reason = str(cell.get("rejection_reason") or "none")
        if disposition == "reject" and rejection_reason == "none":
            rejection_reason = "missing_bbox" if not cell.get("resolved_bbox") else "low_bbox_confidence"

        source_reference = {
            "page_number": page_number,
            "table_id": stable_table_id,
            "row_index": row_index,
            "column_index": column_index,
            "match_method": cell.get("match_method"),
            "gate": cell.get("gate"),
            "promotion_generated_at": report.get("generated_at"),
            "validation": {
                "disposition": _disposition_label(disposition),
                "rejection_reason": rejection_reason,
                "review_flags": review_flags,
                "validation_flags": validation_flags,
                "search_for_hit_count": cell.get("search_for_hit_count"),
                "has_traceable_evidence": cell.get("has_traceable_evidence"),
            },
            "input_bbox_source": cell.get("input_bbox_source") or vs_cell.get("bbox_source"),
            "competing_evidence": suspicious.get(
                f"{stable_table_id}:{row_index}:{column_index}", []
            ),
        }
        if disposition == "review" and not source_reference["competing_evidence"]:
            hits = cell.get("search_for_hit_count") or 0
            if hits > 1:
                source_reference["competing_evidence"] = [
                    {
                        "category": "A_multi_search_for",
                        "search_for_hit_count": hits,
                        "selected_bbox": cell.get("resolved_bbox"),
                        "note": "Multiple search_for occurrences; see promotion validation for rects",
                    }
                ]

        record = PlannedCellEvidence(
            evidence_cell_id=evidence_cell_id,
            stable_table_id=stable_table_id,
            page_number=page_number,
            row_index=row_index,
            column_index=column_index,
            original_value=cell.get("cell_text") or "",
            normalized_value=cell.get("normalized_text") or vs_cell.get("normalized_value"),
            bbox=cell.get("resolved_bbox") or vs_cell.get("bbox"),
            bbox_source=cell.get("bbox_source") or vs_cell.get("bbox_source"),
            bbox_confidence=cell.get("bbox_confidence"),
            match_method=str(cell.get("match_method") or "none"),
            extraction_confidence=vs_cell.get("confidence"),
            disposition=disposition,
            rejection_reason=rejection_reason,
            review_flags=review_flags,
            validation_flags=validation_flags,
            input_bbox_source=cell.get("input_bbox_source") or vs_cell.get("bbox_source"),
            cell_source=cell.get("cell_source") or vs_cell.get("source"),
            source_reference=source_reference,
            idempotency_key=idem,
            action="update" if idem in existing_cell_keys else "insert",
        )

        missing = []
        for req in REQUIRED_EVIDENCE_FIELDS:
            val = getattr(record, req, None)
            if val is None:
                missing.append(req)
        if record.disposition == "reject" and not record.rejection_reason:
            missing.append("rejection_reason")
        record.missing_required = missing
        planned.append(record)

    return planned


def build_domain_oel_plan(cells: list[PlannedCellEvidence]) -> list[PlannedOELRow]:
    """Domain truth rows from fully accepted semantic rows only."""
    by_table_row: dict[tuple[str, int], list[PlannedCellEvidence]] = defaultdict(list)
    for cell in cells:
        if cell.row_index < 2:
            continue
        by_table_row[(cell.stable_table_id, cell.row_index)].append(cell)

    planned: list[PlannedOELRow] = []
    for (stable_table_id, row_index), row_cells in sorted(by_table_row.items()):
        by_col = {c.column_index: c for c in row_cells}
        source_row_key = f"{stable_table_id}:row_{row_index}"
        page_number = row_cells[0].page_number

        name_cell = by_col.get(5)
        if not name_cell or name_cell.disposition != "accept":
            planned.append(
                PlannedOELRow(
                    source_row_key=source_row_key,
                    stable_table_id=stable_table_id,
                    page_number=page_number,
                    row_index=row_index,
                    cas=None,
                    english_name=None,
                    persian_name=None,
                    twa=None,
                    stel=None,
                    ceiling=None,
                    unit=None,
                    action="skip",
                    skip_reason="chemical_name_not_accepted",
                )
            )
            continue

        cas = _extract_cas(name_cell.original_value)
        if not cas:
            planned.append(
                PlannedOELRow(
                    source_row_key=source_row_key,
                    stable_table_id=stable_table_id,
                    page_number=page_number,
                    row_index=row_index,
                    cas=None,
                    english_name=name_cell.original_value,
                    persian_name=None,
                    twa=None,
                    stel=None,
                    ceiling=None,
                    unit=None,
                    action="skip",
                    skip_reason="missing_cas",
                )
            )
            continue

        limit_fields: dict[str, PlannedCellEvidence | None] = {
            "STEL": by_col.get(2),
            "TWA": by_col.get(3),
            "ceiling": by_col.get(1),
        }
        if any(c and c.disposition != "accept" for c in limit_fields.values() if c):
            planned.append(
                PlannedOELRow(
                    source_row_key=source_row_key,
                    stable_table_id=stable_table_id,
                    page_number=page_number,
                    row_index=row_index,
                    cas=cas,
                    english_name=name_cell.original_value,
                    persian_name=None,
                    twa=None,
                    stel=None,
                    ceiling=None,
                    unit=None,
                    action="skip",
                    skip_reason="limit_field_not_accepted",
                )
            )
            continue

        stel_cell = limit_fields["STEL"]
        twa_cell = limit_fields["TWA"]
        ceil_cell = limit_fields["ceiling"]
        stel = _parse_float(stel_cell.original_value if stel_cell else None)
        twa = _parse_float(twa_cell.original_value if twa_cell else None)
        ceiling = _parse_float(ceil_cell.original_value if ceil_cell else None)
        unit = None
        for lc in (stel_cell, twa_cell, ceil_cell):
            if lc and lc.original_value:
                parsed = parse_numeric_cell(lc.original_value)
                if parsed and parsed.unit:
                    unit = parsed.unit
                    break

        provenance = {
            field: {
                "evidence_cell_id": c.evidence_cell_id,
                "bbox": c.bbox,
                "bbox_source": c.bbox_source,
                "original_value": c.original_value,
            }
            for field, c in {
                "chemical_name": name_cell,
                "STEL": stel_cell,
                "TWA": twa_cell,
                "ceiling": ceil_cell,
            }.items()
            if c
        }

        planned.append(
            PlannedOELRow(
                source_row_key=source_row_key,
                stable_table_id=stable_table_id,
                page_number=page_number,
                row_index=row_index,
                cas=cas,
                english_name=name_cell.original_value,
                persian_name=None,
                twa=twa,
                stel=stel,
                ceiling=ceiling,
                unit=unit,
                action="upsert",
                source_cell_provenance=provenance,
            )
        )
    return planned


def build_review_plan(
    cells: list[PlannedCellEvidence],
    *,
    document_id_placeholder: str,
    pipeline_version: str,
    existing_review_keys: set[str] | None = None,
) -> list[PlannedReviewItem]:
    existing_review_keys = existing_review_keys or set()
    planned: list[PlannedReviewItem] = []
    for cell in cells:
        if cell.disposition != "review":
            continue
        for flag in cell.review_flags or ["REVIEW_REQUIRED"]:
            issue_type = flag.upper()
            idem = review_task_idempotency_key(document_id_placeholder, pipeline_version, cell.evidence_cell_id)
            planned.append(
                PlannedReviewItem(
                    evidence_cell_id=cell.evidence_cell_id,
                    stable_table_id=cell.stable_table_id,
                    page_number=cell.page_number,
                    row_index=cell.row_index,
                    column_index=cell.column_index,
                    issue_type=issue_type,
                    review_flags=cell.review_flags,
                    idempotency_key=idem,
                    evidence_reference={
                        "evidence_cell_id": cell.evidence_cell_id,
                        "stable_table_id": cell.stable_table_id,
                        "page_number": cell.page_number,
                        "row_index": cell.row_index,
                        "column_index": cell.column_index,
                        "original_value": cell.original_value,
                        "bbox": cell.bbox,
                        "bbox_source": cell.bbox_source,
                        "bbox_confidence": cell.bbox_confidence,
                        "match_method": cell.match_method,
                        "review_flags": cell.review_flags,
                        "validation_flags": cell.validation_flags,
                        "competing_evidence": cell.source_reference.get("competing_evidence", []),
                    },
                    action="update" if idem in existing_review_keys else "insert",
                )
            )
    return planned


def query_existing_db_state(content_hash: str) -> dict[str, Any]:
    """Read-only DB lookup for idempotency conflicts. Never writes."""
    try:
        from sqlalchemy import select

        from config.settings import get_settings
        from database.models import Document, OELChemicalLimit, ReviewTask, TableCell
        from database.session import session_scope
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    settings = get_settings()
    result: dict[str, Any] = {
        "available": True,
        "document_exists": False,
        "document_id": None,
        "existing_cell_keys": set(),
        "existing_evidence_cell_ids": set(),
        "existing_oel_source_row_keys": set(),
        "existing_review_idempotency_keys": set(),
    }
    try:
        with session_scope() as session:
            doc = session.scalar(select(Document).where(Document.content_hash == content_hash))
            if doc:
                result["document_exists"] = True
                result["document_id"] = str(doc.id)
                for cell in session.scalars(
                    select(TableCell).where(TableCell.evidence_cell_id.is_not(None))
                ):
                    result["existing_evidence_cell_ids"].add(cell.evidence_cell_id)
                for row in session.scalars(select(OELChemicalLimit)):
                    if row.source_row_key:
                        result["existing_oel_source_row_keys"].add(row.source_row_key)
                for task in session.scalars(select(ReviewTask)):
                    if task.idempotency_key:
                        result["existing_review_idempotency_keys"].add(task.idempotency_key)
    except Exception as exc:
        result["available"] = False
        result["error"] = str(exc)

    return result


def generate_dry_run_report(
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
) -> dict[str, Any]:
    report = load_promotion_report(promotion_path)
    content_hash, vs_tables, vs_cells = load_validated_structure_index()
    db_state = query_existing_db_state(content_hash)
    document_id_placeholder = db_state.get("document_id") or f"pending:{content_hash[:12]}"

    existing_keys = set()
    if db_state.get("existing_evidence_cell_ids"):
        for cell in report.get("cells", []):
            cid = cell["cell_id"]
            if cid in db_state["existing_evidence_cell_ids"]:
                idem = stable_cell_idempotency_key(
                    content_hash,
                    int(cell["page_number"]),
                    str(cell["table_id"]),
                    int(cell["row_index"]),
                    int(cell["column_index"]),
                )
                existing_keys.add(idem)

    cells = build_cell_evidence_plan(
        report,
        content_hash=content_hash,
        vs_cells=vs_cells,
        vs_tables=vs_tables,
        existing_cell_keys=existing_keys,
    )
    domain_rows = build_domain_oel_plan(cells)
    review_items = build_review_plan(
        cells,
        document_id_placeholder=document_id_placeholder,
        pipeline_version="geometry_promotion_v1",
        existing_review_keys=set(db_state.get("existing_review_idempotency_keys") or []),
    )

    disposition_counts = Counter(c.disposition for c in cells)
    action_counts = Counter(c.action for c in cells)
    missing_any = [c for c in cells if c.missing_required]
    duplicate_keys = [k for k, v in Counter(c.idempotency_key for c in cells).items() if v > 1]

    domain_upsert = [r for r in domain_rows if r.action == "upsert"]
    domain_skip = [r for r in domain_rows if r.action == "skip"]
    oel_conflicts = [
        r.source_row_key
        for r in domain_upsert
        if r.source_row_key in set(db_state.get("existing_oel_source_row_keys") or [])
    ]

    provenance_cov = {
        "bbox_source": dict(Counter(c.bbox_source or "none" for c in cells)),
        "input_bbox_source": dict(Counter(c.input_bbox_source or "none" for c in cells)),
        "match_method": dict(Counter(c.match_method for c in cells)),
        "with_bbox": sum(1 for c in cells if c.bbox),
        "with_traceable_reference": sum(1 for c in cells if c.source_reference),
    }

    tables_plan = {
        tid: {
            "page_number": int(t["page_number"]),
            "action": "upsert",
            "cell_count": sum(1 for c in cells if c.stable_table_id == tid),
        }
        for tid, t in vs_tables.items()
        if any(c.stable_table_id == tid for c in cells)
    }

    return {
        "dry_run": True,
        "writes_blocked": True,
        "promotion_report": str(promotion_path),
        "content_hash": content_hash,
        "document_id": document_id_placeholder,
        "totals": {
            "total_cells": len(cells),
            "accepted": disposition_counts.get("accept", 0),
            "review": disposition_counts.get("review", 0),
            "rejected": disposition_counts.get("reject", 0),
        },
        "evidence_layer": {
            "table_cells_to_insert": action_counts.get("insert", 0),
            "table_cells_to_update": action_counts.get("update", 0),
            "extracted_tables": len(tables_plan),
            "disposition_counts": dict(disposition_counts),
            "rejection_reasons": dict(Counter(c.rejection_reason for c in cells if c.disposition == "reject")),
        },
        "domain_layer": {
            "oel_chemical_limits_upsert": len(domain_upsert),
            "oel_chemical_limits_skip": len(domain_skip),
            "skip_reasons": dict(Counter(r.skip_reason for r in domain_skip if r.skip_reason)),
            "oel_existing_conflicts": oel_conflicts[:50],
            "oel_existing_conflict_count": len(oel_conflicts),
            "sample_upserts": [
                {
                    "source_row_key": r.source_row_key,
                    "cas": r.cas,
                    "page_number": r.page_number,
                    "twa": r.twa,
                    "stel": r.stel,
                }
                for r in domain_upsert[:10]
            ],
        },
        "review_layer": {
            "review_queue_items": len(review_items),
            "review_task_idempotency_keys": len({r.idempotency_key for r in review_items}),
            "by_issue_type": dict(Counter(r.issue_type for r in review_items)),
            "insert": sum(1 for r in review_items if r.action == "insert"),
            "update": sum(1 for r in review_items if r.action == "update"),
        },
        "embeddings_layer": {
            "document_chunks_planned": 0,
            "note": "Deferred until domain persistence validated — no embeddings in this phase",
        },
        "quality_checks": {
            "duplicate_idempotency_keys": duplicate_keys,
            "cells_missing_required_fields": len(missing_any),
            "missing_required_samples": [
                {"evidence_cell_id": c.evidence_cell_id, "missing": c.missing_required}
                for c in missing_any[:10]
            ],
            "provenance_coverage": provenance_cov,
        },
        "db_state": {
            k: (list(v)[:5] if isinstance(v, set) else v)
            for k, v in db_state.items()
            if k
            not in {
                "existing_cell_keys",
                "existing_evidence_cell_ids",
                "existing_oel_source_row_keys",
                "existing_review_idempotency_keys",
            }
        },
        "db_existing_counts": {
            "evidence_cell_ids": len(db_state.get("existing_evidence_cell_ids") or []),
            "oel_source_row_keys": len(db_state.get("existing_oel_source_row_keys") or []),
            "review_idempotency_keys": len(db_state.get("existing_review_idempotency_keys") or []),
        },
        "per_page": {
            str(page): {
                "total": sum(1 for c in cells if c.page_number == page),
                "accepted": sum(1 for c in cells if c.page_number == page and c.disposition == "accept"),
                "review": sum(1 for c in cells if c.page_number == page and c.disposition == "review"),
                "rejected": sum(1 for c in cells if c.page_number == page and c.disposition == "reject"),
            }
            for page in sorted({c.page_number for c in cells})
        },
        "per_table": {
            tid: {
                "page_number": info["page_number"],
                "total": info["cell_count"],
                "accepted": sum(1 for c in cells if c.stable_table_id == tid and c.disposition == "accept"),
                "review": sum(1 for c in cells if c.stable_table_id == tid and c.disposition == "review"),
                "rejected": sum(1 for c in cells if c.stable_table_id == tid and c.disposition == "reject"),
            }
            for tid, info in tables_plan.items()
        },
        "per_column": {
            str(col): {
                "total": sum(1 for c in cells if c.column_index == col),
                "accepted": sum(1 for c in cells if c.column_index == col and c.disposition == "accept"),
                "review": sum(1 for c in cells if c.column_index == col and c.disposition == "review"),
                "rejected": sum(1 for c in cells if c.column_index == col and c.disposition == "reject"),
            }
            for col in sorted({c.column_index for c in cells})
        },
        "idempotency_keys": {
            "cell_key_format": "{content_hash}:{page_number}:{stable_table_id}:{row_index}:{column_index}",
            "review_key_format": "{document_id}:{pipeline_version}:TABLE_CELL:{evidence_cell_id}",
            "oel_row_key_format": "{stable_table_id}:row_{row_index}",
        },
        "rejected_records": {
            "count": disposition_counts.get("reject", 0),
            "reasons": dict(Counter(c.rejection_reason for c in cells if c.disposition == "reject")),
            "note": "All rejected cells persisted in evidence layer with disposition=reject; excluded from domain and embeddings",
        },
    }


@dataclass
class PromotionWriteStats:
    tables_inserted: int = 0
    tables_updated: int = 0
    cells_inserted: int = 0
    cells_updated: int = 0
    oel_inserted: int = 0
    oel_updated: int = 0
    chemicals_created: int = 0
    review_tasks_inserted: int = 0
    review_tasks_updated: int = 0


def _disposition_label(disposition: str) -> str:
    return disposition.upper()


def load_promotion_plans(
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[PlannedCellEvidence], list[PlannedOELRow], list[PlannedReviewItem], uuid.UUID]:
    import uuid

    from sqlalchemy import select

    from database.models import Document, ReviewTask, TableCell
    from database.session import SessionLocal

    report = load_promotion_report(promotion_path)
    content_hash, vs_tables, vs_cells = load_validated_structure_index()

    session = SessionLocal()
    try:
        doc = session.scalar(select(Document).where(Document.content_hash == content_hash))
        if not doc:
            raise RuntimeError(f"Document not found for content_hash={content_hash}")
        document_id = doc.id
        existing_evidence_ids = {
            row
            for row in session.scalars(
                select(TableCell.evidence_cell_id).where(TableCell.evidence_cell_id.is_not(None))
            )
            if row
        }
        existing_review_keys = {
            row
            for row in session.scalars(select(ReviewTask.idempotency_key))
            if row
        }
    finally:
        session.close()

    existing_keys: set[str] = set()
    for cell in report.get("cells", []):
        cid = cell["cell_id"]
        if cid in existing_evidence_ids:
            existing_keys.add(
                stable_cell_idempotency_key(
                    content_hash,
                    int(cell["page_number"]),
                    str(cell["table_id"]),
                    int(cell["row_index"]),
                    int(cell["column_index"]),
                )
            )

    cells = build_cell_evidence_plan(
        report,
        content_hash=content_hash,
        vs_cells=vs_cells,
        vs_tables=vs_tables,
        existing_cell_keys=existing_keys,
    )
    domain_rows = build_domain_oel_plan(cells)
    review_items = build_review_plan(
        cells,
        document_id_placeholder=str(document_id),
        pipeline_version=PROMOTION_PIPELINE_VERSION,
        existing_review_keys=existing_review_keys,
    )
    return report, content_hash, vs_tables, vs_cells, cells, domain_rows, review_items, document_id


def snapshot_db_counts(session, *, promotion_cell_ids: set[str], promotion_table_ids: set[str], document_id) -> dict[str, Any]:
    import uuid

    from sqlalchemy import func, select

    from database.models import Document, ExtractedTable, OELChemicalLimit, ReviewTask, TableCell

    doc_uuid = document_id if isinstance(document_id, uuid.UUID) else uuid.UUID(str(document_id))

    total_cells = session.scalar(select(func.count()).select_from(TableCell)) or 0
    total_tables = session.scalar(select(func.count()).select_from(ExtractedTable)) or 0
    total_oel = session.scalar(select(func.count()).select_from(OELChemicalLimit)) or 0
    total_review = session.scalar(select(func.count()).select_from(ReviewTask)) or 0
    total_documents = session.scalar(select(func.count()).select_from(Document)) or 0

    scope_cells = session.scalar(
        select(func.count())
        .select_from(TableCell)
        .where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
    ) if promotion_cell_ids else 0

    scope_tables = session.scalar(
        select(func.count())
        .select_from(ExtractedTable)
        .where(
            ExtractedTable.document_id == doc_uuid,
            ExtractedTable.stable_table_id.in_(promotion_table_ids),
        )
    ) if promotion_table_ids else 0

    outside_cells = total_cells - scope_cells

    return {
        "total_documents": total_documents,
        "total_table_cells": total_cells,
        "total_extracted_tables": total_tables,
        "total_oel_limits": total_oel,
        "total_review_tasks": total_review,
        "promotion_scope_cells": scope_cells,
        "promotion_scope_tables": scope_tables,
        "cells_outside_promotion_scope": outside_cells,
    }


def _resolve_stable_table_map(
    session,
    document,
    cells: list[PlannedCellEvidence],
    vs_tables: dict[str, dict[str, Any]],
    promotion_table_ids: set[str],
) -> dict[str, Any]:
    """Prefer existing extracted_tables UUIDs discovered via evidence_cell_id."""
    import uuid

    from sqlalchemy import select

    from database.models import ExtractedTable, TableCell

    stable_to_uuid: dict[str, uuid.UUID] = {}
    for planned in cells:
        if planned.stable_table_id in stable_to_uuid:
            continue
        existing_cell = session.scalar(
            select(TableCell).where(TableCell.evidence_cell_id == planned.evidence_cell_id)
        )
        if existing_cell:
            stable_to_uuid[planned.stable_table_id] = existing_cell.table_id

    for stable_id in sorted(promotion_table_ids):
        if stable_id in stable_to_uuid:
            continue
        existing_table = session.scalar(
            select(ExtractedTable).where(
                ExtractedTable.document_id == document.id,
                ExtractedTable.stable_table_id == stable_id,
            )
        )
        if existing_table:
            stable_to_uuid[stable_id] = existing_table.id

    return stable_to_uuid


def _upsert_extracted_tables(
    session,
    document,
    vs_tables: dict[str, dict[str, Any]],
    promotion_table_ids: set[str],
    cells: list[PlannedCellEvidence],
    stats: PromotionWriteStats,
) -> dict[str, Any]:
    import uuid

    from sqlalchemy import select

    from database.models import ExtractedTable, TableType
    from schema_registry.registry import get_schema_registry

    stable_to_uuid = _resolve_stable_table_map(session, document, cells, vs_tables, promotion_table_ids)
    schema = get_schema_registry().for_table_type("chemical_oel")
    schema_id = schema["schema_id"] if schema else None

    for stable_id in sorted(promotion_table_ids):
        table_dict = vs_tables.get(stable_id)
        if not table_dict:
            raise RuntimeError(f"Missing validated_structure table payload for {stable_id}")

        page_number = int(table_dict["page_number"])
        payload = {
            "page_number": page_number,
            "table_type": TableType.CHEMICAL_OEL,
            "stable_table_id": stable_id,
            "schema_id": schema_id,
            "raw_json": table_dict,
            "raw_markdown": table_dict.get("raw_markdown"),
            "confidence": table_dict.get("structural_confidence"),
            "source_processor": "geometry_promotion_v1",
            "bbox": table_dict.get("bbox"),
        }

        table_uuid = stable_to_uuid.get(stable_id)
        existing = session.get(ExtractedTable, table_uuid) if table_uuid else None
        if existing is None:
            existing = session.scalar(
                select(ExtractedTable).where(
                    ExtractedTable.document_id == document.id,
                    ExtractedTable.stable_table_id == stable_id,
                )
            )

        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
            stable_to_uuid[stable_id] = existing.id
            stats.tables_updated += 1
        else:
            db_table = ExtractedTable(document_id=document.id, **payload)
            session.add(db_table)
            session.flush()
            stable_to_uuid[stable_id] = db_table.id
            stats.tables_inserted += 1

    session.flush()
    return stable_to_uuid


def _upsert_table_cells(
    session,
    cells: list[PlannedCellEvidence],
    stable_to_uuid: dict[str, Any],
    stats: PromotionWriteStats,
) -> dict[str, Any]:
    import uuid

    from sqlalchemy import select

    from database.models import TableCell

    cell_uuid_by_evidence_id: dict[str, uuid.UUID] = {}

    for planned in cells:
        table_uuid = stable_to_uuid.get(planned.stable_table_id)
        if not table_uuid:
            raise RuntimeError(f"No extracted_tables UUID for {planned.stable_table_id}")

        source_reference = {
            **planned.source_reference,
            "evidence_cell_id": planned.evidence_cell_id,
            "idempotency_key": planned.idempotency_key,
            "validation": {
                **planned.source_reference.get("validation", {}),
                "disposition": _disposition_label(planned.disposition),
                "rejection_reason": planned.rejection_reason,
                "review_flags": planned.review_flags,
                "validation_flags": planned.validation_flags,
            },
            "match_method": planned.match_method,
            "extraction_confidence": planned.extraction_confidence,
        }

        fields = {
            "table_id": table_uuid,
            "page_number": planned.page_number,
            "row_index": planned.row_index,
            "column_index": planned.column_index,
            "raw_text": planned.original_value,
            "original_value": planned.original_value,
            "normalized_value": planned.normalized_value,
            "bbox": planned.bbox,
            "confidence": planned.extraction_confidence,
            "bbox_source": planned.bbox_source,
            "bbox_confidence": planned.bbox_confidence,
            "source_reference": source_reference,
            "evidence_cell_id": planned.evidence_cell_id,
            "source": planned.cell_source,
        }

        existing_rows = list(
            session.scalars(
                select(TableCell).where(TableCell.evidence_cell_id == planned.evidence_cell_id)
            ).all()
        )
        if not existing_rows:
            existing_rows = [
                session.scalar(
                    select(TableCell).where(
                        TableCell.table_id == table_uuid,
                        TableCell.row_index == planned.row_index,
                        TableCell.column_index == planned.column_index,
                    )
                )
            ]
            existing_rows = [r for r in existing_rows if r is not None]

        if not existing_rows:
            existing_rows = [TableCell(**fields)]
            session.add(existing_rows[0])
            stats.cells_inserted += 1
        else:
            stats.cells_updated += len(existing_rows)

        canonical = None
        for row in existing_rows:
            if (
                row.table_id == table_uuid
                and row.row_index == planned.row_index
                and row.column_index == planned.column_index
            ):
                canonical = row
                break
        if canonical is None:
            for row in existing_rows:
                if row.row_index == planned.row_index and row.column_index == planned.column_index:
                    canonical = row
                    break
        if canonical is None:
            canonical = existing_rows[0]

        position_fields = {"table_id", "row_index", "column_index"}
        occupant = session.scalar(
            select(TableCell).where(
                TableCell.table_id == table_uuid,
                TableCell.row_index == planned.row_index,
                TableCell.column_index == planned.column_index,
                TableCell.id.not_in([row.id for row in existing_rows]),
            )
        )
        skip_position = occupant is not None
        for existing in existing_rows:
            if existing.id == canonical.id:
                update_fields = (
                    {k: v for k, v in fields.items() if k not in position_fields}
                    if skip_position
                    else fields
                )
            else:
                update_fields = {k: v for k, v in fields.items() if k not in position_fields}
            for key, value in update_fields.items():
                setattr(existing, key, value)
            session.flush()
            if existing.id == canonical.id:
                cell_uuid_by_evidence_id[planned.evidence_cell_id] = existing.id

    return cell_uuid_by_evidence_id


def _upsert_oel_domain(
    session,
    document,
    domain_rows: list[PlannedOELRow],
    stable_to_uuid: dict[str, Any],
    stats: PromotionWriteStats,
) -> None:
    from sqlalchemy import select

    from database.models import ChemicalRegistry, OELChemicalLimit
    from persistence.knowledge_pipeline import _upsert_chemical

    upsert_rows = [r for r in domain_rows if r.action == "upsert"]
    for row in upsert_rows:
        if not row.cas:
            continue
        table_uuid = stable_to_uuid.get(row.stable_table_id)
        chemical, created = _upsert_chemical(
            session,
            cas=row.cas,
            english_name=row.english_name,
            persian_name=row.persian_name,
            molecular_weight=None,
            gold_path="geometry_promotion_v1",
        )
        if created:
            stats.chemicals_created += 1

        existing_rows = list(
            session.scalars(
                select(OELChemicalLimit).where(OELChemicalLimit.source_row_key == row.source_row_key)
            ).all()
        )
        if not existing_rows:
            existing_rows = list(
                session.scalars(
                    select(OELChemicalLimit).where(
                        OELChemicalLimit.chemical_id == chemical.id,
                        OELChemicalLimit.source_row_key == row.source_row_key,
                    )
                ).all()
            )
        original_values = {
            k: v.get("original_value")
            for k, v in row.source_cell_provenance.items()
        }
        accepted_values = {
            "TWA": row.twa,
            "STEL": row.stel,
            "ceiling": row.ceiling,
        }
        payload = {
            "chemical_id": chemical.id,
            "twa": row.twa,
            "stel": row.stel,
            "ceiling": row.ceiling,
            "unit": row.unit,
            "page_number": row.page_number,
            "persian_name": row.persian_name,
            "english_name": row.english_name,
            "standard_reference": "OHE6",
            "source_table_id": table_uuid,
            "source_table_id_str": row.stable_table_id,
            "source_row_key": row.source_row_key,
            "source_cell_provenance": row.source_cell_provenance,
            "original_values": original_values,
            "accepted_values": accepted_values,
            "validation_status": "accepted",
            "gold_artifact_path": "geometry_promotion_v1",
            "knowledge_metadata": {"promotion": "geometry_promotion_v1"},
        }
        if existing_rows:
            for existing in existing_rows:
                for key, value in payload.items():
                    setattr(existing, key, value)
            stats.oel_updated += len(existing_rows)
        else:
            session.add(OELChemicalLimit(**payload))
            stats.oel_inserted += 1


def _upsert_review_tasks(
    session,
    document_id,
    review_items: list[PlannedReviewItem],
    cell_uuid_by_evidence_id: dict[str, Any],
    stable_to_uuid: dict[str, Any],
    stats: PromotionWriteStats,
) -> None:
    import uuid

    from sqlalchemy import select

    from database.models import ReviewTask, ReviewTargetType
    from review.task_factory import upsert_review_case

    doc_uuid = document_id if isinstance(document_id, uuid.UUID) else uuid.UUID(str(document_id))
    seen_idempotency: set[str] = set()

    for item in review_items:
        if item.idempotency_key in seen_idempotency:
            continue
        seen_idempotency.add(item.idempotency_key)

        existing = session.scalar(
            select(ReviewTask).where(ReviewTask.idempotency_key == item.idempotency_key)
        )
        table_uuid = stable_to_uuid.get(item.stable_table_id)
        cell_uuid = cell_uuid_by_evidence_id.get(item.evidence_cell_id)

        issues = [
            {
                "code": item.issue_type,
                "type": item.issue_type,
                "severity": "medium",
                "category": item.issue_type.lower(),
            }
        ]
        upsert_review_case(
            session,
            document_id=doc_uuid,
            page_number=item.page_number,
            target_type=ReviewTargetType.TABLE_CELL,
            target_id=item.evidence_cell_id,
            issues=issues,
            stable_table_id=item.stable_table_id,
            evidence_reference=item.evidence_reference,
            pipeline_version=PROMOTION_PIPELINE_VERSION,
        )
        task = session.scalar(
            select(ReviewTask).where(
                ReviewTask.document_id == doc_uuid,
                ReviewTask.pipeline_version == PROMOTION_PIPELINE_VERSION,
                ReviewTask.target_id == item.evidence_cell_id,
            )
        )
        if task is None:
            task = session.scalar(select(ReviewTask).where(ReviewTask.idempotency_key == item.idempotency_key))
        if task:
            task.idempotency_key = item.idempotency_key
            task.table_id = table_uuid
            task.cell_id = cell_uuid
            task.evidence_reference = item.evidence_reference
            task.stable_table_id = item.stable_table_id
        if item.action == "update" or existing:
            stats.review_tasks_updated += 1
        else:
            stats.review_tasks_inserted += 1


def verify_promotion_integrity(
    session,
    *,
    document_id,
    promotion_cell_ids: set[str],
    promotion_table_ids: set[str],
    domain_upsert_keys: set[str],
    review_idempotency_keys: set[str],
    evidence_only: bool = False,
) -> dict[str, Any]:
    import uuid

    from sqlalchemy import func, select

    from database.models import ExtractedTable, OELChemicalLimit, ReviewTask, TableCell
    from document_ai.geometry_resolver import is_valid_bbox

    doc_uuid = document_id if isinstance(document_id, uuid.UUID) else uuid.UUID(str(document_id))
    checks: dict[str, Any] = {}
    discrepancies: list[str] = []

    scope_cell_row_count = session.scalar(
        select(func.count())
        .select_from(TableCell)
        .where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
    )
    scope_cell_distinct_count = session.scalar(
        select(func.count(func.distinct(TableCell.evidence_cell_id)))
        .select_from(TableCell)
        .where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
    )
    checks["promotion_scope_cell_row_count"] = scope_cell_row_count
    checks["promotion_scope_cell_count"] = scope_cell_distinct_count
    if scope_cell_distinct_count != len(promotion_cell_ids):
        discrepancies.append(
            f"promotion_scope_cell_count={scope_cell_distinct_count} expected={len(promotion_cell_ids)}"
        )

    promotion_cells = session.scalars(
        select(TableCell).where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
    ).all()

    unknown_disposition_rows = sum(
        1
        for cell in promotion_cells
        if (cell.source_reference or {}).get("validation", {}).get("disposition", "UNKNOWN") == "UNKNOWN"
    )
    checks["promotion_scope_unknown_disposition_rows"] = unknown_disposition_rows
    if unknown_disposition_rows:
        discrepancies.append(
            f"promotion_scope_unknown_disposition_rows={unknown_disposition_rows} expected=0"
        )

    disposition_counts = Counter(
        (c.source_reference or {}).get("validation", {}).get("disposition", "UNKNOWN")
        for c in promotion_cells
    )
    canonical_disposition_counts = Counter(
        (c.source_reference or {}).get("validation", {}).get("disposition", "UNKNOWN")
        for c in {
            cell.evidence_cell_id: cell
            for cell in sorted(promotion_cells, key=lambda row: str(row.id))
        }.values()
    )
    checks["disposition_counts"] = dict(disposition_counts)
    checks["canonical_disposition_counts"] = dict(canonical_disposition_counts)

    scope_table_count = session.scalar(
        select(func.count())
        .select_from(ExtractedTable)
        .where(
            ExtractedTable.document_id == doc_uuid,
            ExtractedTable.stable_table_id.in_(promotion_table_ids),
        )
    )
    promotion_table_uuids = {
        row
        for row in session.scalars(
            select(TableCell.table_id)
            .where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
            .distinct()
        ).all()
        if row
    }
    distinct_stable_table_ids = {
        row
        for row in session.scalars(
            select(ExtractedTable.stable_table_id)
            .where(
                ExtractedTable.id.in_(promotion_table_uuids),
                ExtractedTable.stable_table_id.is_not(None),
            )
            .distinct()
        ).all()
        if row
    }
    scope_table_count = session.scalar(
        select(func.count())
        .select_from(ExtractedTable)
        .where(
            ExtractedTable.document_id == doc_uuid,
            ExtractedTable.stable_table_id.in_(promotion_table_ids),
        )
    )
    checks["promotion_scope_table_count"] = len(distinct_stable_table_ids)
    checks["promotion_scope_table_ids_from_cells"] = len(promotion_table_uuids)
    checks["promotion_scope_tables_by_document_stable_id"] = scope_table_count or 0
    if len(distinct_stable_table_ids) != len(promotion_table_ids):
        discrepancies.append(
            f"promotion_scope_table_count={len(distinct_stable_table_ids)} expected={len(promotion_table_ids)}"
        )
    tables_missing_stable = session.scalar(
        select(func.count())
        .select_from(ExtractedTable)
        .where(
            ExtractedTable.id.in_(promotion_table_uuids),
            ExtractedTable.stable_table_id.is_(None),
        )
    ) if promotion_table_uuids else 0
    checks["promotion_tables_missing_stable_table_id"] = tables_missing_stable or 0
    if tables_missing_stable:
        discrepancies.append(
            f"promotion_tables_missing_stable_table_id={tables_missing_stable} expected=0"
        )

    dup_positions = session.execute(
        select(TableCell.table_id, TableCell.row_index, TableCell.column_index, func.count())
        .where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
        .group_by(TableCell.table_id, TableCell.row_index, TableCell.column_index)
        .having(func.count() > 1)
    ).all()
    checks["duplicate_table_positions"] = len(dup_positions)
    if dup_positions:
        discrepancies.append(f"duplicate (table_id,row,col) groups={len(dup_positions)}")

    accept_no_bbox = 0
    accept_no_provenance = 0
    review_no_competing = 0
    reject_no_reason = 0
    for cell in promotion_cells:
        validation = (cell.source_reference or {}).get("validation", {})
        disposition = validation.get("disposition", "")
        if disposition == "ACCEPT":
            if not is_valid_bbox(cell.bbox):
                accept_no_bbox += 1
            if not cell.source_reference or not cell.evidence_cell_id:
                accept_no_provenance += 1
        elif disposition == "REVIEW":
            competing = (cell.source_reference or {}).get("competing_evidence") or []
            if not competing:
                review_no_competing += 1
        elif disposition == "REJECT":
            if not validation.get("rejection_reason"):
                reject_no_reason += 1

    checks["accept_without_bbox"] = accept_no_bbox
    checks["accept_without_traceable_provenance"] = accept_no_provenance
    checks["review_without_competing_evidence"] = review_no_competing
    checks["reject_without_reason"] = reject_no_reason

    for key, val, label in (
        ("accept_without_bbox", accept_no_bbox, "ACCEPT without bbox"),
        ("accept_without_traceable_provenance", accept_no_provenance, "ACCEPT without provenance"),
        ("review_without_competing_evidence", review_no_competing, "REVIEW without competing evidence"),
        ("reject_without_reason", reject_no_reason, "REJECT without reason"),
    ):
        if val:
            discrepancies.append(f"{label}: {val}")

    oel_rows = session.scalars(
        select(OELChemicalLimit).where(OELChemicalLimit.source_row_key.in_(domain_upsert_keys))
    ).all()
    distinct_oel_keys = {row.source_row_key for row in oel_rows}
    checks["domain_rows_total"] = len(oel_rows)
    checks["domain_rows_present"] = len(distinct_oel_keys)
    if not evidence_only and len(distinct_oel_keys) != len(domain_upsert_keys):
        missing = domain_upsert_keys - distinct_oel_keys
        discrepancies.append(
            f"domain_rows_present={len(distinct_oel_keys)} expected={len(domain_upsert_keys)} missing={sorted(missing)[:5]}"
        )

    oel_dup_keys = session.execute(
        select(OELChemicalLimit.source_row_key, func.count())
        .where(OELChemicalLimit.source_row_key.in_(domain_upsert_keys))
        .group_by(OELChemicalLimit.source_row_key)
        .having(func.count() > 1)
    ).all()
    checks["duplicate_oel_source_row_keys"] = len(oel_dup_keys)
    if not evidence_only and oel_dup_keys:
        discrepancies.append(f"duplicate OEL source_row_key groups={len(oel_dup_keys)}")

    review_evidence_ids = {key.rsplit(":", 1)[-1] for key in review_idempotency_keys}
    review_tasks = session.scalars(
        select(ReviewTask).where(
            ReviewTask.document_id == doc_uuid,
            ReviewTask.pipeline_version == PROMOTION_PIPELINE_VERSION,
            ReviewTask.target_id.in_(review_evidence_ids),
        )
    ).all()
    checks["promotion_review_tasks"] = len(review_tasks)
    if not evidence_only and len(review_tasks) != len(review_idempotency_keys):
        discrepancies.append(
            f"promotion_review_tasks={len(review_tasks)} expected={len(review_idempotency_keys)}"
        )

    review_dup = session.execute(
        select(ReviewTask.idempotency_key, func.count())
        .where(ReviewTask.idempotency_key.in_(review_idempotency_keys))
        .group_by(ReviewTask.idempotency_key)
        .having(func.count() > 1)
    ).all()
    checks["duplicate_review_idempotency_keys"] = len(review_dup)
    if review_dup:
        discrepancies.append(f"duplicate review idempotency keys={len(review_dup)}")

    known_conflicts = {"table_046_01:row_2", "table_088_01:row_5"}
    for key in known_conflicts & domain_upsert_keys:
        count = session.scalar(
            select(func.count())
            .select_from(OELChemicalLimit)
            .where(OELChemicalLimit.source_row_key == key)
        )
        if count < 1:
            discrepancies.append(f"known conflict {key} count={count} expected>=1")
        elif count > 1:
            checks.setdefault("preexisting_oel_duplicate_keys", []).append(
                {"source_row_key": key, "count": count}
            )

    passed = not discrepancies
    return {
        "passed": passed,
        "checks": checks,
        "discrepancies": discrepancies,
    }


def execute_geometry_promotion(
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
    *,
    evidence_only: bool = False,
) -> dict[str, Any]:
    import uuid

    from sqlalchemy import select

    from database.models import Document, OELChemicalLimit, ReviewTask, TableCell
    from database.session import SessionLocal

    (
        report,
        content_hash,
        vs_tables,
        _vs_cells,
        cells,
        domain_rows,
        review_items,
        document_id,
    ) = load_promotion_plans(promotion_path)

    promotion_cell_ids = {c.evidence_cell_id for c in cells}
    promotion_table_ids = {c.stable_table_id for c in cells}
    domain_upsert = [r for r in domain_rows if r.action == "upsert"]
    domain_upsert_keys = {r.source_row_key for r in domain_upsert}
    review_idempotency_keys = {r.idempotency_key for r in review_items}

    session = SessionLocal()
    result: dict[str, Any] = {
        "transaction_committed": False,
        "rollback_performed": False,
        "promotion_report": str(promotion_path),
        "content_hash": content_hash,
        "document_id": str(document_id),
    }
    stats = PromotionWriteStats()

    try:
        before = snapshot_db_counts(
            session,
            promotion_cell_ids=promotion_cell_ids,
            promotion_table_ids=promotion_table_ids,
            document_id=document_id,
        )
        outside_before = before["cells_outside_promotion_scope"]

        document = session.scalar(select(Document).where(Document.id == document_id))
        if not document:
            raise RuntimeError(f"Document {document_id} not found")

        stable_to_uuid = _upsert_extracted_tables(
            session, document, vs_tables, promotion_table_ids, cells, stats
        )
        cell_uuid_by_evidence_id = _upsert_table_cells(session, cells, stable_to_uuid, stats)
        if not evidence_only:
            _upsert_oel_domain(session, document, domain_rows, stable_to_uuid, stats)
            _upsert_review_tasks(
                session,
                document_id,
                review_items,
                cell_uuid_by_evidence_id,
                stable_to_uuid,
                stats,
            )

        verification = verify_promotion_integrity(
            session,
            document_id=document_id,
            promotion_cell_ids=promotion_cell_ids,
            promotion_table_ids=promotion_table_ids,
            domain_upsert_keys=domain_upsert_keys,
            review_idempotency_keys=review_idempotency_keys,
            evidence_only=evidence_only,
        )

        after = snapshot_db_counts(
            session,
            promotion_cell_ids=promotion_cell_ids,
            promotion_table_ids=promotion_table_ids,
            document_id=document_id,
        )
        outside_after = after["cells_outside_promotion_scope"]
        if outside_after != outside_before:
            verification["discrepancies"].append(
                f"cells_outside_promotion_scope changed {outside_before} -> {outside_after}"
            )
            verification["passed"] = False

        result["before"] = before
        result["after"] = after
        result["write_stats"] = stats.__dict__
        result["verification"] = verification
        result["expected"] = {
            "total_cells": len(cells),
            "accepted": sum(1 for c in cells if c.disposition == "accept"),
            "review": sum(1 for c in cells if c.disposition == "review"),
            "rejected": sum(1 for c in cells if c.disposition == "reject"),
            "tables": len(promotion_table_ids),
            "domain_upserts": len(domain_upsert_keys),
            "review_tasks": len(review_idempotency_keys),
        }

        if verification["passed"]:
            session.commit()
            result["transaction_committed"] = True
        else:
            session.rollback()
            result["rollback_performed"] = True
    except Exception as exc:
        session.rollback()
        result["rollback_performed"] = True
        result["error"] = str(exc)
        raise
    finally:
        session.close()

    if result["transaction_committed"]:
        post_session = SessionLocal()
        try:
            post_verify = verify_promotion_integrity(
                post_session,
                document_id=document_id,
                promotion_cell_ids=promotion_cell_ids,
                promotion_table_ids=promotion_table_ids,
                domain_upsert_keys=domain_upsert_keys,
                review_idempotency_keys=review_idempotency_keys,
            )
            result["post_commit_verification"] = post_verify
            result["success"] = post_verify["passed"]
        finally:
            post_session.close()
    else:
        result["success"] = False

    return result
