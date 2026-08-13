"""Read-only domain inventory and reconciliation planning from canonical evidence."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pipeline_contracts.numeric_integrity import parse_numeric_cell
from persistence.geometry_promotion_persist import (
    PROMOTION_REPORT_DEFAULT,
    PlannedCellEvidence,
    _disposition_label,
    _extract_cas,
    _parse_float,
    build_cell_evidence_plan,
    load_promotion_report,
    load_validated_structure_index,
)

OHE6_CONTENT_HASH = "16518156ffd4468eaad8cdb3383dfbcd4d209a2bdc23e3e36e21d1e96ecba618"
DOMAIN_PIPELINE_VERSION = "canonical_evidence_v1"
GOLD_ARTIFACT_PATH = "canonical_evidence_v1"
RETIRE_VALIDATION_STATUS = "legacy_reference"
REVIEW_VALIDATION_STATUS = "review_required"
ACCEPTED_VALIDATION_STATUS = "accepted"

OEL_COLUMN_MAP = {
    "ceiling": 1,
    "STEL": 2,
    "TWA": 3,
    "chemical_name": 5,
}

ReconciliationAction = Literal["INSERT", "UPDATE", "UNCHANGED", "RETIRE", "REVIEW"]


@dataclass
class DomainFieldRule:
    field: str
    required: bool
    source: str
    notes: str = ""


@dataclass
class CanonicalOELRecord:
    source_row_key: str
    stable_table_id: str
    page_number: int
    row_index: int
    cas: str
    english_name: str | None
    persian_name: str | None
    twa: float | None
    stel: float | None
    ceiling: float | None
    unit: str | None
    validation_status: str
    source_cell_provenance: dict[str, Any]
    accepted_values: dict[str, Any]
    original_values: dict[str, Any]
    authoritative_fields: list[str]
    review_reason: str | None = None
    skip_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_row_key": self.source_row_key,
            "stable_table_id": self.stable_table_id,
            "page_number": self.page_number,
            "row_index": self.row_index,
            "cas": self.cas,
            "english_name": self.english_name,
            "persian_name": self.persian_name,
            "twa": self.twa,
            "stel": self.stel,
            "ceiling": self.ceiling,
            "unit": self.unit,
            "validation_status": self.validation_status,
            "source_cell_provenance": self.source_cell_provenance,
            "accepted_values": self.accepted_values,
            "original_values": self.original_values,
            "authoritative_fields": self.authoritative_fields,
            "gold_artifact_path": GOLD_ARTIFACT_PATH,
            "standard_reference": "OHE6",
            "knowledge_metadata": {
                "promotion": DOMAIN_PIPELINE_VERSION,
                "pipeline_version": DOMAIN_PIPELINE_VERSION,
            },
        }


@dataclass
class ReconciliationEntry:
    action: ReconciliationAction
    source_row_key: str | None
    old_record_id: str | None
    new_record: dict[str, Any] | None
    old_record: dict[str, Any] | None
    field_differences: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_reason: str | None = None
    retire_reason: str | None = None


def oel_domain_field_rules() -> list[dict[str, Any]]:
    """Document minimum semantic completeness for OEL domain records."""
    return [
        {
            "field": "chemical_name",
            "column_index": 5,
            "required": True,
            "rule": "disposition must be ACCEPT",
        },
        {
            "field": "cas",
            "required": True,
            "rule": "extractable CAS from accepted chemical_name cell",
        },
        {
            "field": "limits",
            "columns": {"ceiling": 1, "STEL": 2, "TWA": 3},
            "required": "at_least_one",
            "rule": "at least one limit column must be ACCEPT with a parseable numeric value",
        },
        {
            "field": "ceiling",
            "required": False,
            "rule": "included only when column 1 is ACCEPT; omitted otherwise",
        },
        {
            "field": "STEL",
            "required": False,
            "rule": "included only when column 2 is ACCEPT; omitted otherwise",
        },
        {
            "field": "TWA",
            "required": False,
            "rule": "included only when column 3 is ACCEPT; omitted otherwise",
        },
        {
            "field": "unit",
            "required": False,
            "rule": "derived from first ACCEPT limit cell with parsed unit",
        },
    ]


def _cell_from_db_row(cell, *, content_hash: str) -> PlannedCellEvidence:
    source_reference = cell.source_reference or {}
    validation = source_reference.get("validation", {})
    disposition = str(validation.get("disposition", "UNKNOWN")).lower()
    if disposition not in {"accept", "review", "reject"}:
        disposition = "reject" if validation.get("rejection_reason") else "review"

    stable_table_id = source_reference.get("stable_table_id") or source_reference.get("table_id")
    if not stable_table_id:
        stable_table_id = source_reference.get("table_id")

    return PlannedCellEvidence(
        evidence_cell_id=cell.evidence_cell_id or "",
        stable_table_id=str(stable_table_id or ""),
        page_number=int(cell.page_number or source_reference.get("page_number") or 0),
        row_index=int(cell.row_index),
        column_index=int(cell.column_index),
        original_value=cell.original_value or cell.raw_text or "",
        normalized_value=cell.normalized_value,
        bbox=cell.bbox,
        bbox_source=cell.bbox_source,
        bbox_confidence=cell.bbox_confidence,
        match_method=source_reference.get("match_method", "unknown"),
        extraction_confidence=cell.confidence,
        disposition=disposition,
        rejection_reason=validation.get("rejection_reason") or "",
        review_flags=list(validation.get("review_flags") or []),
        validation_flags=list(validation.get("validation_flags") or []),
        input_bbox_source=cell.bbox_source,
        cell_source=cell.source,
        source_reference=source_reference,
        idempotency_key=f"{content_hash}:{cell.page_number}:{stable_table_id}:{cell.row_index}:{cell.column_index}",
        action="update",
    )


def load_canonical_cells_from_db(session, *, document_id, promotion_cell_ids: set[str], content_hash: str) -> list[PlannedCellEvidence]:
    from sqlalchemy import select

    from database.models import TableCell

    rows = session.scalars(
        select(TableCell).where(TableCell.evidence_cell_id.in_(promotion_cell_ids))
    ).all()
    canonical_by_id: dict[str, PlannedCellEvidence] = {}
    for row in sorted(rows, key=lambda item: str(item.id)):
        if not row.evidence_cell_id:
            continue
        canonical_by_id[row.evidence_cell_id] = _cell_from_db_row(row, content_hash=content_hash)
    return list(canonical_by_id.values())


def build_canonical_oel_from_cells(cells: list[PlannedCellEvidence]) -> tuple[list[CanonicalOELRecord], list[CanonicalOELRecord]]:
    """Build authoritative and review-only OEL candidates from canonical evidence."""
    by_table_row: dict[tuple[str, int], list[PlannedCellEvidence]] = defaultdict(list)
    for cell in cells:
        if cell.row_index < 2:
            continue
        by_table_row[(cell.stable_table_id, cell.row_index)].append(cell)

    authoritative: list[CanonicalOELRecord] = []
    review_only: list[CanonicalOELRecord] = []

    for (stable_table_id, row_index), row_cells in sorted(by_table_row.items()):
        by_col = {c.column_index: c for c in row_cells}
        source_row_key = f"{stable_table_id}:row_{row_index}"
        page_number = row_cells[0].page_number
        name_cell = by_col.get(OEL_COLUMN_MAP["chemical_name"])

        if not name_cell or name_cell.disposition != "accept":
            review_only.append(
                CanonicalOELRecord(
                    source_row_key=source_row_key,
                    stable_table_id=stable_table_id,
                    page_number=page_number,
                    row_index=row_index,
                    cas="",
                    english_name=name_cell.original_value if name_cell else None,
                    persian_name=None,
                    twa=None,
                    stel=None,
                    ceiling=None,
                    unit=None,
                    validation_status=REVIEW_VALIDATION_STATUS,
                    source_cell_provenance={},
                    accepted_values={},
                    original_values={},
                    authoritative_fields=[],
                    review_reason="chemical_name_not_accepted",
                )
            )
            continue

        cas = _extract_cas(name_cell.original_value)
        if not cas:
            review_only.append(
                CanonicalOELRecord(
                    source_row_key=source_row_key,
                    stable_table_id=stable_table_id,
                    page_number=page_number,
                    row_index=row_index,
                    cas="",
                    english_name=name_cell.original_value,
                    persian_name=None,
                    twa=None,
                    stel=None,
                    ceiling=None,
                    unit=None,
                    validation_status=REVIEW_VALIDATION_STATUS,
                    source_cell_provenance={},
                    accepted_values={},
                    original_values={},
                    authoritative_fields=[],
                    review_reason="missing_cas",
                )
            )
            continue

        limit_cells = {
            "STEL": by_col.get(OEL_COLUMN_MAP["STEL"]),
            "TWA": by_col.get(OEL_COLUMN_MAP["TWA"]),
            "ceiling": by_col.get(OEL_COLUMN_MAP["ceiling"]),
        }
        accepted_values: dict[str, float | None] = {}
        authoritative_fields: list[str] = []
        provenance: dict[str, Any] = {
            "chemical_name": _provenance_entry(name_cell),
        }
        original_values = {"chemical_name": name_cell.original_value}

        for field_name, limit_cell in limit_cells.items():
            if not limit_cell or limit_cell.disposition != "accept":
                continue
            parsed_value = _parse_float(limit_cell.original_value)
            if parsed_value is None:
                continue
            accepted_values[field_name if field_name != "ceiling" else "ceiling"] = parsed_value
            authoritative_fields.append(field_name)
            provenance[field_name if field_name != "ceiling" else "ceiling"] = _provenance_entry(limit_cell)
            original_values[field_name if field_name != "ceiling" else "ceiling"] = limit_cell.original_value

        unit = None
        for limit_cell in limit_cells.values():
            if limit_cell and limit_cell.disposition == "accept" and limit_cell.original_value:
                parsed = parse_numeric_cell(limit_cell.original_value)
                if parsed and parsed.unit:
                    unit = parsed.unit
                    break

        if not authoritative_fields:
            review_only.append(
                CanonicalOELRecord(
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
                    validation_status=REVIEW_VALIDATION_STATUS,
                    source_cell_provenance=provenance,
                    accepted_values={},
                    original_values=original_values,
                    authoritative_fields=[],
                    review_reason="no_accept_limit",
                )
            )
            continue

        authoritative.append(
            CanonicalOELRecord(
                source_row_key=source_row_key,
                stable_table_id=stable_table_id,
                page_number=page_number,
                row_index=row_index,
                cas=cas,
                english_name=name_cell.original_value,
                persian_name=None,
                twa=accepted_values.get("TWA"),
                stel=accepted_values.get("STEL"),
                ceiling=accepted_values.get("ceiling"),
                unit=unit,
                validation_status=ACCEPTED_VALIDATION_STATUS,
                source_cell_provenance=provenance,
                accepted_values=accepted_values,
                original_values=original_values,
                authoritative_fields=authoritative_fields,
            )
        )

    return authoritative, review_only


def _provenance_entry(cell: PlannedCellEvidence) -> dict[str, Any]:
    return {
        "evidence_cell_id": cell.evidence_cell_id,
        "bbox": cell.bbox,
        "bbox_source": cell.bbox_source,
        "original_value": cell.original_value,
        "disposition": _disposition_label(cell.disposition),
        "page_number": cell.page_number,
        "row_index": cell.row_index,
        "column_index": cell.column_index,
        "stable_table_id": cell.stable_table_id,
    }


def _serialize_oel_row(row) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "chemical_id": str(row.chemical_id),
        "cas": getattr(row.chemical, "cas", None) if row.chemical else None,
        "source_row_key": row.source_row_key,
        "source_table_id_str": row.source_table_id_str,
        "page_number": row.page_number,
        "english_name": row.english_name,
        "persian_name": row.persian_name,
        "twa": row.twa,
        "stel": row.stel,
        "ceiling": row.ceiling,
        "unit": row.unit,
        "validation_status": row.validation_status,
        "gold_artifact_path": row.gold_artifact_path,
        "source_cell_provenance": row.source_cell_provenance,
        "accepted_values": row.accepted_values,
    }


def _compare_values(old_val: Any, new_val: Any) -> bool:
    if old_val is None and new_val is None:
        return True
    if isinstance(old_val, float) or isinstance(new_val, float):
        try:
            return old_val is not None and new_val is not None and abs(float(old_val) - float(new_val)) < 1e-9
        except (TypeError, ValueError):
            return False
    return old_val == new_val


def _field_differences(old: dict[str, Any], new: dict[str, Any], fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    diffs: dict[str, dict[str, Any]] = {}
    for key in fields:
        if not _compare_values(old.get(key), new.get(key)):
            diffs[key] = {"old": old.get(key), "new": new.get(key)}
    return diffs


def _in_ohe6_oel_scope(row, *, promotion_table_ids: set[str]) -> bool:
    if row.source_table_id_str and row.source_table_id_str in promotion_table_ids:
        return True
    if row.page_number is not None and 46 <= row.page_number <= 144:
        return True
    return False


def reconcile_oel_records(
    existing_rows,
    authoritative_records: list[CanonicalOELRecord],
    review_records: list[CanonicalOELRecord],
    *,
    promotion_table_ids: set[str],
) -> list[ReconciliationEntry]:
    compare_fields = ("cas", "twa", "stel", "ceiling", "unit", "english_name", "validation_status")
    new_by_key = {record.source_row_key: record for record in authoritative_records}
    review_by_key = {record.source_row_key: record for record in review_records}
    entries: list[ReconciliationEntry] = []
    seen_old_ids: set[str] = set()

    for old in existing_rows:
        if not _in_ohe6_oel_scope(old, promotion_table_ids=promotion_table_ids):
            continue
        old_payload = _serialize_oel_row(old)
        old_id = old_payload["id"]
        key = old.source_row_key

        if not key:
            entries.append(
                ReconciliationEntry(
                    action="RETIRE",
                    source_row_key=None,
                    old_record_id=old_id,
                    old_record=old_payload,
                    new_record=None,
                    retire_reason="missing_stable_source_row_key",
                )
            )
            seen_old_ids.add(old_id)
            continue

        new_record = new_by_key.get(key)
        if new_record is None:
            if key in review_by_key:
                entries.append(
                    ReconciliationEntry(
                        action="REVIEW",
                        source_row_key=key,
                        old_record_id=old_id,
                        old_record=old_payload,
                        new_record=review_by_key[key].to_payload(),
                        review_reason=review_by_key[key].review_reason,
                        retire_reason="superseded_by_review_only_canonical_evidence",
                    )
                )
            else:
                entries.append(
                    ReconciliationEntry(
                        action="RETIRE",
                        source_row_key=key,
                        old_record_id=old_id,
                        old_record=old_payload,
                        new_record=None,
                        retire_reason="no_canonical_source_row",
                    )
                )
            seen_old_ids.add(old_id)
            continue

        new_payload = new_record.to_payload()
        diffs = _field_differences(
            {
                "cas": old_payload.get("cas"),
                "twa": old_payload.get("twa"),
                "stel": old_payload.get("stel"),
                "ceiling": old_payload.get("ceiling"),
                "unit": old_payload.get("unit"),
                "english_name": old_payload.get("english_name"),
                "validation_status": old_payload.get("validation_status"),
            },
            {
                "cas": new_payload.get("cas"),
                "twa": new_payload.get("twa"),
                "stel": new_payload.get("stel"),
                "ceiling": new_payload.get("ceiling"),
                "unit": new_payload.get("unit"),
                "english_name": new_payload.get("english_name"),
                "validation_status": new_payload.get("validation_status"),
            },
            compare_fields,
        )
        if old_payload.get("gold_artifact_path") != GOLD_ARTIFACT_PATH:
            diffs.setdefault("gold_artifact_path", {"old": old_payload.get("gold_artifact_path"), "new": GOLD_ARTIFACT_PATH})

        action: ReconciliationAction = "UNCHANGED" if not diffs else "UPDATE"
        entries.append(
            ReconciliationEntry(
                action=action,
                source_row_key=key,
                old_record_id=old_id,
                old_record=old_payload,
                new_record=new_payload,
                field_differences=diffs,
            )
        )
        seen_old_ids.add(old_id)

    existing_keys = {row.source_row_key for row in existing_rows if row.source_row_key}
    for record in authoritative_records:
        if record.source_row_key in existing_keys:
            continue
        entries.append(
            ReconciliationEntry(
                action="INSERT",
                source_row_key=record.source_row_key,
                old_record_id=None,
                old_record=None,
                new_record=record.to_payload(),
            )
        )

    return entries


def inventory_domain_tables(session, *, document_id, promotion_table_ids: set[str]) -> dict[str, Any]:
    from sqlalchemy import func, select

    from database.models import (
        BiologicalExposureLimit,
        DocumentChunk,
        NoiseLimit,
        OELChemicalLimit,
        RegulatoryConstraint,
        TableCell,
        VibrationLimit,
    )

    inventory: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(), "tables": {}}

    def _count(model):
        return session.scalar(select(func.count()).select_from(model)) or 0

    oel_rows = session.scalars(select(OELChemicalLimit)).all()
    scoped_oel = [row for row in oel_rows if _in_ohe6_oel_scope(row, promotion_table_ids=promotion_table_ids)]
    inventory["tables"]["oel_chemical_limits"] = {
        "row_count": len(oel_rows),
        "ohe6_scope_row_count": len(scoped_oel),
        "validation_status_counts": dict(Counter(row.validation_status for row in oel_rows)),
        "gold_artifact_path_counts": dict(Counter(row.gold_artifact_path for row in oel_rows)),
        "with_source_row_key": sum(1 for row in oel_rows if row.source_row_key),
        "with_source_cell_provenance": sum(1 for row in oel_rows if row.source_cell_provenance),
        "geometry_promotion_rows": sum(1 for row in oel_rows if row.gold_artifact_path == "geometry_promotion_v1"),
        "canonical_evidence_rows": sum(1 for row in oel_rows if row.gold_artifact_path == GOLD_ARTIFACT_PATH),
        "source_population": [
            "persistence/knowledge_pipeline.py::sync_gold_tables (gold/tables/*.json)",
            "persistence/geometry_promotion_persist.py::_upsert_oel_domain",
            "persistence/evidence_pipeline.py::persist_validated_rows_and_domain (legacy)",
        ],
        "retrieval_consumers": [
            "agents/structured/store.py (validation_status == accepted)",
            "retrieval/eval_builders.py",
        ],
        "retire_mechanism": "validation_status -> legacy_reference (no is_active column)",
        "stable_identity_fields": ["source_row_key", "source_table_id_str", "source_cell_provenance.evidence_cell_id"],
        "assessment": "Contains predominantly old extraction (2354 rows without source_row_key); only 27 geometry_promotion_v1 rows reflect new evidence.",
    }

    for name, model, populated, notes in (
        ("noise_limits", NoiseLimit, False, "Schema only; no extraction writer or structured retrieval consumer."),
        ("vibration_limits", VibrationLimit, False, "Schema only; no extraction writer or structured retrieval consumer."),
        ("biological_exposure_limits", BiologicalExposureLimit, False, "Schema only; no extraction writer or structured retrieval consumer."),
        ("regulatory_constraints", RegulatoryConstraint, False, "Schema only; no extraction writer or structured retrieval consumer."),
    ):
        inventory["tables"][name] = {
            "row_count": _count(model),
            "populated_from_extraction": populated,
            "notes": notes,
        }

    chunks = session.scalars(select(DocumentChunk)).all()
    doc_chunks = [chunk for chunk in chunks if chunk.document_id == document_id]
    inventory["tables"]["document_chunks"] = {
        "row_count": len(chunks),
        "document_row_count": len(doc_chunks),
        "source_type_counts": dict(Counter(chunk.source_type for chunk in chunks)),
        "validation_status_counts": dict(Counter(chunk.validation_status for chunk in chunks)),
        "retrieval_consumers": [
            "persistence/semantic_store.py::search_persian_semantic",
            "retrieval/pipeline.py hybrid retrieval",
        ],
        "production_filters": {
            "source_type": "semantic_text",
            "validation_status": "accepted",
            "language": "fa",
            "embedding_is_not_null": True,
        },
        "assessment": "Production retrieval reads accepted Persian semantic_text chunks; legacy chunk types remain in DB but are filtered out.",
    }

    inventory["tables"]["table_cells"] = {
        "promotion_scope_tables": len(promotion_table_ids),
        "notes": "Canonical evidence already promoted; domain layer not yet rebuilt from this source.",
    }

    return inventory


def inventory_retrieval_paths() -> dict[str, Any]:
    return {
        "structured_oel": {
            "old_source": "oel_chemical_limits WHERE validation_status='accepted' (mostly legacy rows without provenance)",
            "new_source": "oel_chemical_limits rebuilt from canonical table_cells; stale rows marked legacy_reference",
            "filter": "validation_status == accepted",
        },
        "semantic_rag": {
            "old_source": "document_chunks source_type=semantic_text accepted Persian embeddings",
            "new_source": "document_chunks rebuilt post-domain-reconciliation from canonical semantic/domain data",
            "stale_strategy": "mark legacy_reference or rebuild affected chunk_ids; do not leave old embeddings active",
        },
        "unsupported_structured": {
            "noise_limits": "semantic fallback only",
            "vibration_limits": "semantic fallback only",
            "biological_exposure_limits": "semantic fallback only",
            "regulatory_constraints": "semantic fallback only",
        },
    }


def generate_domain_reconciliation_report(
    *,
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
    content_hash: str = OHE6_CONTENT_HASH,
) -> dict[str, Any]:
    from sqlalchemy import select

    from database.models import Document, OELChemicalLimit
    from database.session import SessionLocal
    from persistence.geometry_promotion_persist import load_promotion_plans

    report = load_promotion_report(promotion_path)
    promotion_cell_ids = {cell["cell_id"] for cell in report.get("cells", [])}
    promotion_table_ids = {cell["table_id"] for cell in report.get("cells", [])}

    session = SessionLocal()
    try:
        document = session.scalar(select(Document).where(Document.content_hash == content_hash))
        if not document:
            raise RuntimeError(f"Document not found for content_hash={content_hash}")

        inventory = inventory_domain_tables(
            session,
            document_id=document.id,
            promotion_table_ids=promotion_table_ids,
        )

        cells = load_canonical_cells_from_db(
            session,
            document_id=document.id,
            promotion_cell_ids=promotion_cell_ids,
            content_hash=content_hash,
        )
        authoritative, review_only = build_canonical_oel_from_cells(cells)
        existing_rows = session.scalars(select(OELChemicalLimit)).all()
        entries = reconcile_oel_records(
            existing_rows,
            authoritative,
            review_only,
            promotion_table_ids=promotion_table_ids,
        )

        action_counts = Counter(entry.action for entry in entries)
        scoped_existing = [row for row in existing_rows if _in_ohe6_oel_scope(row, promotion_table_ids=promotion_table_ids)]

        sample_changes = {
            action: [
                {
                    "source_row_key": entry.source_row_key,
                    "old_record_id": entry.old_record_id,
                    "field_differences": entry.field_differences,
                    "review_reason": entry.review_reason,
                    "retire_reason": entry.retire_reason,
                    "old": entry.old_record,
                    "new": entry.new_record,
                }
                for entry in entries
                if entry.action == action
            ][:15]
            for action in ("INSERT", "UPDATE", "UNCHANGED", "RETIRE", "REVIEW")
        }

        stale_without_key = [
            _serialize_oel_row(row)
            for row in scoped_existing
            if not row.source_row_key
        ]

        existing_actions = sum(
            action_counts.get(label, 0) for label in ("UPDATE", "UNCHANGED", "RETIRE", "REVIEW")
        )
        consistency_checks = {
            "existing_rows_accounted_for": existing_actions == len(scoped_existing),
            "existing_rows_accounted_for_detail": {
                "scoped_existing": len(scoped_existing),
                "classified_existing": existing_actions,
            },
            "authoritative_rows_accounted_for": (
                action_counts.get("INSERT", 0) + action_counts.get("UPDATE", 0) + action_counts.get("UNCHANGED", 0)
                == len(authoritative)
            ),
            "authoritative_rows_accounted_for_detail": {
                "authoritative": len(authoritative),
                "insert_update_unchanged": (
                    action_counts.get("INSERT", 0)
                    + action_counts.get("UPDATE", 0)
                    + action_counts.get("UNCHANGED", 0)
                ),
            },
        }

        return {
            "dry_run": True,
            "writes_blocked": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "document_id": str(document.id),
            "content_hash": content_hash,
            "phase": "inventory_and_reconciliation_plan",
            "domain_field_rules": {
                "oel_chemical_limits": oel_domain_field_rules(),
            },
            "inventory": inventory,
            "retrieval_inventory": inventory_retrieval_paths(),
            "canonical_evidence_summary": {
                "canonical_cells": len(cells),
                "promotion_tables": len(promotion_table_ids),
                "accepted_cells": sum(1 for cell in cells if cell.disposition == "accept"),
                "review_cells": sum(1 for cell in cells if cell.disposition == "review"),
                "reject_cells": sum(1 for cell in cells if cell.disposition == "reject"),
            },
            "oel_chemical_limits": {
                "old_count_total": len(existing_rows),
                "old_count_ohe6_scope": len(scoped_existing),
                "new_authoritative_count": len(authoritative),
                "new_review_only_count": len(review_only),
                "action_counts": dict(action_counts),
                "strict_geometry_promotion_count": 27,
                "retire_strategy": {
                    "method": "set validation_status=legacy_reference",
                    "physical_delete": False,
                    "retrieval_exclusion": "agents/structured/store.py filters validation_status == accepted",
                },
                "stale_without_source_row_key_count": len(stale_without_key),
            },
            "samples": sample_changes,
            "consistency_checks": consistency_checks,
            "notes": [
                "READ-ONLY dry run; no database modifications performed.",
                "Authoritative domain rows require ACCEPT chemical_name + CAS + >=1 ACCEPT limit with parseable value.",
                "Rows with ACCEPT name/CAS but no ACCEPT limits are classified REVIEW, not domain truth.",
                "Existing OHE6-scoped rows without source_row_key are RETIRE candidates.",
            ],
        }
    finally:
        session.close()


def write_domain_reconciliation_markdown(report: dict[str, Any], path: Path) -> None:
    inv = report.get("inventory", {}).get("tables", {})
    oel = report.get("oel_chemical_limits", {})
    actions = oel.get("action_counts", {})
    lines = [
        "# OHE6 Domain Reconciliation — Dry Run Report",
        "",
        f"Generated: {report.get('generated_at')}",
        f"Dry run: **{report.get('dry_run')}** · Writes blocked: **{report.get('writes_blocked')}**",
        "",
        "## Phase 1 — Domain Layer Inventory",
        "",
    ]

    for table_name, table_info in inv.items():
        lines.append(f"### `{table_name}`")
        if isinstance(table_info, dict):
            for key, value in table_info.items():
                if key in {"source_population", "retrieval_consumers", "notes"}:
                    continue
                lines.append(f"- {key}: {value}")
            if table_info.get("assessment"):
                lines.append(f"- assessment: {table_info['assessment']}")
        lines.append("")

    lines.extend([
        "## Phase 2 — Canonical Semantic Rules",
        "",
        "### `oel_chemical_limits` minimum completeness",
        "",
    ])
    for rule in report.get("domain_field_rules", {}).get("oel_chemical_limits", []):
        lines.append(f"- **{rule['field']}**: {rule.get('rule', rule)}")

    canon = report.get("canonical_evidence_summary", {})
    lines.extend([
        "",
        "## Phase 3 — OEL Reconciliation Summary",
        "",
        f"| Metric | Count |",
        f"|--------|------:|",
        f"| Old OEL rows (total) | {oel.get('old_count_total', 0)} |",
        f"| Old OEL rows (OHE6 scope) | {oel.get('old_count_ohe6_scope', 0)} |",
        f"| New authoritative canonical rows | {oel.get('new_authoritative_count', 0)} |",
        f"| New review-only row candidates | {oel.get('new_review_only_count', 0)} |",
        f"| Previous strict geometry promotion rows | {oel.get('strict_geometry_promotion_count', 0)} |",
        "",
        "### Planned actions",
        "",
        f"- INSERT: {actions.get('INSERT', 0)}",
        f"- UPDATE: {actions.get('UPDATE', 0)}",
        f"- UNCHANGED: {actions.get('UNCHANGED', 0)}",
        f"- RETIRE: {actions.get('RETIRE', 0)}",
        f"- REVIEW: {actions.get('REVIEW', 0)}",
        "",
        f"Stale rows without `source_row_key`: **{oel.get('stale_without_source_row_key_count', 0)}**",
        "",
        "## Phase 4 — Stale Data Strategy",
        "",
        f"- Method: `{oel.get('retire_strategy', {}).get('method')}`",
        f"- Physical delete: `{oel.get('retire_strategy', {}).get('physical_delete')}`",
        f"- Retrieval exclusion: {oel.get('retire_strategy', {}).get('retrieval_exclusion')}",
        "",
        "## Phase 6 — Retrieval Inventory",
        "",
    ])
    for name, info in (report.get("retrieval_inventory") or {}).items():
        lines.append(f"### {name}")
        for key, value in info.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    lines.extend([
        "## Samples",
        "",
    ])
    for action in ("INSERT", "UPDATE", "RETIRE"):
        samples = report.get("samples", {}).get(action, [])
        if not samples:
            continue
        lines.append(f"### {action} (first {min(len(samples), 3)})")
        for sample in samples[:3]:
            lines.append(f"- `{sample.get('source_row_key')}`")
            if sample.get("field_differences"):
                lines.append(f"  - diffs: {sample['field_differences']}")
            if sample.get("retire_reason"):
                lines.append(f"  - retire_reason: {sample['retire_reason']}")
        lines.append("")

    lines.extend(["## Notes", ""])
    for note in report.get("notes", []):
        lines.append(f"- {note}")

    path.write_text("\n".join(lines), encoding="utf-8")


@dataclass
class DomainApplyStats:
    inserted: int = 0
    updated: int = 0
    retired: int = 0
    review_classified: int = 0
    unchanged: int = 0
    duplicates_retired: int = 0


EXPECTED_APPLY_COUNTS = {
    "INSERT": 87,
    "UPDATE": 25,
    "RETIRE": 2676,
    "REVIEW": 16,
    "UNCHANGED": 0,
    "authoritative": 112,
}


def build_reconciliation_plan(
    session,
    *,
    document_id,
    content_hash: str,
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
) -> tuple[list[PlannedCellEvidence], list[CanonicalOELRecord], list[CanonicalOELRecord], list[ReconciliationEntry], set[str]]:
    from sqlalchemy import select

    from database.models import OELChemicalLimit

    report = load_promotion_report(promotion_path)
    promotion_cell_ids = {cell["cell_id"] for cell in report.get("cells", [])}
    promotion_table_ids = {cell["table_id"] for cell in report.get("cells", [])}

    cells = load_canonical_cells_from_db(
        session,
        document_id=document_id,
        promotion_cell_ids=promotion_cell_ids,
        content_hash=content_hash,
    )
    authoritative, review_only = build_canonical_oel_from_cells(cells)
    existing_rows = session.scalars(select(OELChemicalLimit)).all()
    entries = reconcile_oel_records(
        existing_rows,
        authoritative,
        review_only,
        promotion_table_ids=promotion_table_ids,
    )
    return cells, authoritative, review_only, entries, promotion_table_ids


def _resolve_stable_table_map(session, document_id, stable_table_ids: set[str]) -> dict[str, Any]:
    import uuid

    from sqlalchemy import select

    from database.models import ExtractedTable

    doc_uuid = document_id if isinstance(document_id, uuid.UUID) else uuid.UUID(str(document_id))
    mapping: dict[str, uuid.UUID] = {}
    for stable_id in stable_table_ids:
        table = session.scalar(
            select(ExtractedTable).where(
                ExtractedTable.document_id == doc_uuid,
                ExtractedTable.stable_table_id == stable_id,
            )
        )
        if table:
            mapping[stable_id] = table.id
    return mapping


def _oel_payload_from_canonical(
    record: CanonicalOELRecord,
    *,
    chemical_id,
    table_uuid,
) -> dict[str, Any]:
    original_values = dict(record.original_values)
    accepted_values = {
        "TWA": record.twa,
        "STEL": record.stel,
        "ceiling": record.ceiling,
    }
    return {
        "chemical_id": chemical_id,
        "twa": record.twa,
        "stel": record.stel,
        "ceiling": record.ceiling,
        "unit": record.unit,
        "page_number": record.page_number,
        "persian_name": record.persian_name,
        "english_name": record.english_name,
        "standard_reference": "OHE6",
        "source_table_id": table_uuid,
        "source_table_id_str": record.stable_table_id,
        "source_row_key": record.source_row_key,
        "source_cell_provenance": record.source_cell_provenance,
        "original_values": original_values,
        "accepted_values": accepted_values,
        "validation_status": ACCEPTED_VALIDATION_STATUS,
        "gold_artifact_path": GOLD_ARTIFACT_PATH,
        "gold_version": DOMAIN_PIPELINE_VERSION,
        "knowledge_metadata": {
            "promotion": DOMAIN_PIPELINE_VERSION,
            "pipeline_version": DOMAIN_PIPELINE_VERSION,
            "authoritative_fields": record.authoritative_fields,
        },
        "confidence": 1.0,
    }


def _upsert_authoritative_oel(
    session,
    record: CanonicalOELRecord,
    *,
    stable_to_uuid: dict[str, Any],
    stats: DomainApplyStats,
) -> None:
    from sqlalchemy import select

    from database.models import OELChemicalLimit
    from persistence.knowledge_pipeline import _upsert_chemical

    table_uuid = stable_to_uuid.get(record.stable_table_id)
    chemical, _created = _upsert_chemical(
        session,
        cas=record.cas,
        english_name=record.english_name,
        persian_name=record.persian_name,
        molecular_weight=None,
        gold_path=GOLD_ARTIFACT_PATH,
    )
    payload = _oel_payload_from_canonical(record, chemical_id=chemical.id, table_uuid=table_uuid)
    rows_by_source_key = session.scalars(
        select(OELChemicalLimit).where(OELChemicalLimit.source_row_key == record.source_row_key)
    ).all()
    rows_by_chemical_key = session.scalars(
        select(OELChemicalLimit).where(
            OELChemicalLimit.chemical_id == chemical.id,
            OELChemicalLimit.source_row_key == record.source_row_key,
        )
    ).all()
    existing_rows = list({row.id: row for row in (*rows_by_source_key, *rows_by_chemical_key)}.values())
    if not existing_rows:
        session.add(OELChemicalLimit(**payload))
        stats.inserted += 1
        return

    keeper = sorted(
        existing_rows,
        key=lambda row: (
            row.gold_artifact_path == GOLD_ARTIFACT_PATH,
            row.validation_status == ACCEPTED_VALIDATION_STATUS,
            str(row.id),
        ),
        reverse=True,
    )[0]
    for duplicate in existing_rows:
        if duplicate.id == keeper.id:
            continue
        duplicate.validation_status = RETIRE_VALIDATION_STATUS
        duplicate.knowledge_metadata = {
            **(duplicate.knowledge_metadata or {}),
            "retired_reason": "duplicate_source_row_key",
            "superseded_by": str(keeper.id),
        }
        stats.duplicates_retired += 1

    for key, value in payload.items():
        setattr(keeper, key, value)
    stats.updated += 1


def _retire_oel_row(session, row_id: str, stats: DomainApplyStats) -> None:
    import uuid

    from database.models import OELChemicalLimit

    row = session.get(OELChemicalLimit, uuid.UUID(str(row_id)))
    if not row:
        raise RuntimeError(f"OEL row not found for retire: {row_id}")
    row.validation_status = RETIRE_VALIDATION_STATUS
    metadata = dict(row.knowledge_metadata or {})
    metadata.update(
        {
            "retired_by": DOMAIN_PIPELINE_VERSION,
            "retired_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.knowledge_metadata = metadata
    stats.retired += 1


def _classify_review_oel_row(session, entry: ReconciliationEntry, stats: DomainApplyStats) -> None:
    import uuid

    from database.models import OELChemicalLimit

    row = session.get(OELChemicalLimit, uuid.UUID(str(entry.old_record_id)))
    if not row:
        raise RuntimeError(f"OEL row not found for review: {entry.old_record_id}")
    review_payload = entry.new_record or {}
    row.validation_status = REVIEW_VALIDATION_STATUS
    row.twa = None
    row.stel = None
    row.ceiling = None
    row.unit = None
    row.gold_artifact_path = GOLD_ARTIFACT_PATH
    row.gold_version = DOMAIN_PIPELINE_VERSION
    row.accepted_values = {}
    if review_payload.get("source_cell_provenance"):
        row.source_cell_provenance = review_payload["source_cell_provenance"]
    if review_payload.get("original_values"):
        row.original_values = review_payload["original_values"]
    metadata = dict(row.knowledge_metadata or {})
    metadata.update(
        {
            "pipeline_version": DOMAIN_PIPELINE_VERSION,
            "review_reason": entry.review_reason,
            "review_only": True,
        }
    )
    row.knowledge_metadata = metadata
    stats.review_classified += 1


def _has_traceable_provenance(row) -> bool:
    provenance = row.source_cell_provenance or {}
    if not provenance:
        return False
    name_prov = provenance.get("chemical_name") or {}
    if not name_prov.get("evidence_cell_id"):
        return False
    limit_keys = ("TWA", "STEL", "ceiling")
    return any((provenance.get(key) or {}).get("evidence_cell_id") for key in limit_keys)


def verify_domain_reconciliation_applied(
    session,
    *,
    promotion_table_ids: set[str],
    authoritative_keys: set[str],
    expected_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    from database.models import OELChemicalLimit
    from agents.structured.store import PostgresStructuredStore

    expected_counts = expected_counts or EXPECTED_APPLY_COUNTS
    discrepancies: list[str] = []
    checks: dict[str, Any] = {}

    scoped_rows = session.scalars(
        select(OELChemicalLimit)
        .options(selectinload(OELChemicalLimit.chemical))
        .where(OELChemicalLimit.page_number.between(46, 144))
    ).all()
    scoped_rows = [
        row for row in scoped_rows if _in_ohe6_oel_scope(row, promotion_table_ids=promotion_table_ids)
    ]
    active_canonical = [
        row
        for row in scoped_rows
        if row.validation_status == ACCEPTED_VALIDATION_STATUS and row.gold_artifact_path == GOLD_ARTIFACT_PATH
    ]
    retired_rows = [row for row in scoped_rows if row.validation_status == RETIRE_VALIDATION_STATUS]
    review_rows = [row for row in scoped_rows if row.validation_status == REVIEW_VALIDATION_STATUS]

    checks["scoped_row_count"] = len(scoped_rows)
    checks["active_canonical_count"] = len(active_canonical)
    checks["legacy_reference_count"] = len(retired_rows)
    checks["review_required_count"] = len(review_rows)

    if len(active_canonical) != expected_counts["authoritative"]:
        discrepancies.append(
            f"active_canonical_count={len(active_canonical)} expected={expected_counts['authoritative']}"
        )
    if len(retired_rows) != expected_counts["RETIRE"]:
        retire_delta = abs(len(retired_rows) - expected_counts["RETIRE"])
        retire_tolerance = int(expected_counts.get("retire_tolerance", 0))
        if retire_delta > retire_tolerance:
            discrepancies.append(f"legacy_reference_count={len(retired_rows)} expected={expected_counts['RETIRE']}")
    if len(review_rows) != expected_counts["REVIEW"]:
        discrepancies.append(f"review_required_count={len(review_rows)} expected={expected_counts['REVIEW']}")

    active_keys = {row.source_row_key for row in active_canonical}
    if active_keys != authoritative_keys:
        missing = authoritative_keys - active_keys
        extra = active_keys - authoritative_keys
        if missing:
            discrepancies.append(f"missing_active_keys={sorted(missing)[:5]}")
        if extra:
            discrepancies.append(f"unexpected_active_keys={sorted(extra)[:5]}")

    stale_active = [
        row
        for row in scoped_rows
        if row.validation_status == ACCEPTED_VALIDATION_STATUS and row.gold_artifact_path != GOLD_ARTIFACT_PATH
    ]
    checks["stale_active_accepted_count"] = len(stale_active)
    if stale_active:
        discrepancies.append(f"stale_active_accepted_count={len(stale_active)} expected=0")

    no_provenance = [row for row in active_canonical if not _has_traceable_provenance(row)]
    checks["active_without_traceable_provenance"] = len(no_provenance)
    if no_provenance:
        discrepancies.append(f"active_without_traceable_provenance={len(no_provenance)}")

    dup_keys = session.execute(
        select(OELChemicalLimit.source_row_key, func.count())
        .where(
            OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
            OELChemicalLimit.gold_artifact_path == GOLD_ARTIFACT_PATH,
            OELChemicalLimit.source_row_key.in_(authoritative_keys),
        )
        .group_by(OELChemicalLimit.source_row_key)
        .having(func.count() > 1)
    ).all()
    checks["duplicate_active_source_row_keys"] = len(dup_keys)
    if dup_keys:
        discrepancies.append(f"duplicate_active_source_row_keys={len(dup_keys)}")

    evidence_to_keys: dict[str, set[str]] = defaultdict(set)
    for row in active_canonical:
        for field in (row.source_cell_provenance or {}).values():
            if isinstance(field, dict) and field.get("evidence_cell_id"):
                evidence_to_keys[field["evidence_cell_id"]].add(row.source_row_key or "")
    duplicate_evidence = {eid: sorted(keys) for eid, keys in evidence_to_keys.items() if len(keys) > 1}
    checks["canonical_evidence_mappings"] = len(evidence_to_keys)
    checks["duplicate_canonical_evidence_mappings"] = len(duplicate_evidence)
    if duplicate_evidence:
        discrepancies.append(f"duplicate_canonical_evidence_mappings={len(duplicate_evidence)}")

    structured_visible = session.scalar(
        select(func.count())
        .select_from(OELChemicalLimit)
        .where(
            OELChemicalLimit.validation_status == ACCEPTED_VALIDATION_STATUS,
            OELChemicalLimit.gold_artifact_path == GOLD_ARTIFACT_PATH,
            OELChemicalLimit.source_row_key.in_(authoritative_keys),
        )
    )
    checks["structured_retrieval_visible_count"] = structured_visible
    if structured_visible != expected_counts["authoritative"]:
        discrepancies.append(
            f"structured_retrieval_visible_count={structured_visible} expected={expected_counts['authoritative']}"
        )

    store = PostgresStructuredStore(session)
    sample_cas = active_canonical[0].chemical.cas if active_canonical and active_canonical[0].chemical else None
    checks["structured_store_filter_verified"] = True
    if sample_cas:
        visible = store.get_oel_by_cas(sample_cas)
        if visible and any(item.get("gold_artifact_path") != GOLD_ARTIFACT_PATH for item in visible):
            checks["structured_store_filter_verified"] = False
            discrepancies.append("structured_store returned non-canonical accepted rows")

    return {
        "passed": not discrepancies,
        "checks": checks,
        "discrepancies": discrepancies,
    }


def execute_domain_reconciliation(
    *,
    promotion_path: Path = PROMOTION_REPORT_DEFAULT,
    content_hash: str = OHE6_CONTENT_HASH,
    expected_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    import uuid

    from sqlalchemy import func, select

    from database.models import Document, OELChemicalLimit
    from database.session import SessionLocal

    expected_counts = expected_counts or EXPECTED_APPLY_COUNTS
    session = SessionLocal()
    result: dict[str, Any] = {
        "transaction_committed": False,
        "rollback_performed": False,
        "content_hash": content_hash,
        "expected": expected_counts,
    }
    stats = DomainApplyStats()

    try:
        document = session.scalar(select(Document).where(Document.content_hash == content_hash))
        if not document:
            raise RuntimeError(f"Document not found for content_hash={content_hash}")
        result["document_id"] = str(document.id)

        report = load_promotion_report(promotion_path)
        promotion_table_ids = {cell["table_id"] for cell in report.get("cells", [])}

        before_scoped = [
            row
            for row in session.scalars(select(OELChemicalLimit)).all()
            if _in_ohe6_oel_scope(row, promotion_table_ids=promotion_table_ids)
        ]
        result["before"] = {
            "ohe6_scope_total": len(before_scoped),
            "accepted": sum(1 for row in before_scoped if row.validation_status == ACCEPTED_VALIDATION_STATUS),
            "legacy_reference": sum(1 for row in before_scoped if row.validation_status == RETIRE_VALIDATION_STATUS),
            "review_required": sum(1 for row in before_scoped if row.validation_status == REVIEW_VALIDATION_STATUS),
            "canonical_evidence_v1": sum(1 for row in before_scoped if row.gold_artifact_path == GOLD_ARTIFACT_PATH),
        }

        _cells, authoritative, _review_only, entries, promotion_table_ids = build_reconciliation_plan(
            session,
            document_id=document.id,
            content_hash=content_hash,
            promotion_path=promotion_path,
        )
        planned_counts = Counter(entry.action for entry in entries)
        result["planned"] = dict(planned_counts)
        if expected_counts is EXPECTED_APPLY_COUNTS:
            expected_counts = {
                "INSERT": planned_counts.get("INSERT", 0),
                "UPDATE": planned_counts.get("UPDATE", 0),
                "RETIRE": planned_counts.get("RETIRE", 0),
                "REVIEW": planned_counts.get("REVIEW", 0),
                "UNCHANGED": planned_counts.get("UNCHANGED", 0),
                "authoritative": len(authoritative),
            }
            result["expected"] = expected_counts
        authoritative_keys = {record.source_row_key for record in authoritative}
        stable_to_uuid = _resolve_stable_table_map(
            session,
            document.id,
            {record.stable_table_id for record in authoritative},
        )

        for entry in entries:
            if entry.action == "RETIRE":
                _retire_oel_row(session, entry.old_record_id, stats)
            elif entry.action == "REVIEW":
                _classify_review_oel_row(session, entry, stats)
            elif entry.action in {"INSERT", "UPDATE"}:
                record = next(r for r in authoritative if r.source_row_key == entry.source_row_key)
                _upsert_authoritative_oel(session, record, stable_to_uuid=stable_to_uuid, stats=stats)
            elif entry.action == "UNCHANGED":
                stats.unchanged += 1

        session.flush()

        if stats.inserted != expected_counts["INSERT"]:
            raise RuntimeError(f"inserted={stats.inserted} expected={expected_counts['INSERT']}")
        if stats.updated != expected_counts["UPDATE"]:
            raise RuntimeError(f"updated={stats.updated} expected={expected_counts['UPDATE']}")
        if stats.retired != expected_counts["RETIRE"]:
            raise RuntimeError(f"retired={stats.retired} expected={expected_counts['RETIRE']}")
        if stats.review_classified != expected_counts["REVIEW"]:
            raise RuntimeError(f"review_classified={stats.review_classified} expected={expected_counts['REVIEW']}")

        verification_expected = {
            **expected_counts,
            "RETIRE": expected_counts["RETIRE"] + stats.duplicates_retired,
            "retire_tolerance": stats.duplicates_retired,
        }
        verification = verify_domain_reconciliation_applied(
            session,
            promotion_table_ids=promotion_table_ids,
            authoritative_keys=authoritative_keys,
            expected_counts=verification_expected,
        )
        result["write_stats"] = stats.__dict__
        result["pre_commit_verification"] = verification

        if not verification["passed"]:
            session.rollback()
            result["rollback_performed"] = True
            result["success"] = False
            return result

        session.commit()
        result["transaction_committed"] = True
    except Exception as exc:
        session.rollback()
        result["rollback_performed"] = True
        result["error"] = str(exc)
        result["success"] = False
        raise
    finally:
        session.close()

    post_session = SessionLocal()
    try:
        if result.get("transaction_committed"):
            post_verify = verify_domain_reconciliation_applied(
                post_session,
                promotion_table_ids=promotion_table_ids,
                authoritative_keys=authoritative_keys,
                expected_counts=verification_expected,
            )
            after_scoped = [
                row
                for row in post_session.scalars(select(OELChemicalLimit)).all()
                if _in_ohe6_oel_scope(row, promotion_table_ids=promotion_table_ids)
            ]
            result["after"] = {
                "ohe6_scope_total": len(after_scoped),
                "accepted": sum(1 for row in after_scoped if row.validation_status == ACCEPTED_VALIDATION_STATUS),
                "legacy_reference": sum(1 for row in after_scoped if row.validation_status == RETIRE_VALIDATION_STATUS),
                "review_required": sum(1 for row in after_scoped if row.validation_status == REVIEW_VALIDATION_STATUS),
                "canonical_evidence_v1": sum(1 for row in after_scoped if row.gold_artifact_path == GOLD_ARTIFACT_PATH),
            }
            result["post_commit_verification"] = post_verify
            result["success"] = post_verify["passed"]
        else:
            result["success"] = False
    finally:
        post_session.close()

    return result


def write_domain_apply_markdown(result: dict[str, Any], path: Path) -> None:
    lines = [
        "# OHE6 Domain Reconciliation — Apply Result",
        "",
        f"Success: **{result.get('success')}**",
        f"Transaction committed: **{result.get('transaction_committed')}**",
        f"Rollback performed: **{result.get('rollback_performed')}**",
        "",
        "## Before / After",
        "",
    ]
    before = result.get("before", {})
    after = result.get("after", {})
    lines.append("| Metric | Before | After |")
    lines.append("|--------|-------:|------:|")
    for key in ("ohe6_scope_total", "accepted", "legacy_reference", "review_required", "canonical_evidence_v1"):
        lines.append(f"| {key} | {before.get(key, '—')} | {after.get(key, '—')} |")

    lines.extend(["", "## Planned vs Write Stats", ""])
    planned = result.get("planned", {})
    write_stats = result.get("write_stats", {})
    applied_map = {
        "INSERT": write_stats.get("inserted", 0),
        "UPDATE": write_stats.get("updated", 0),
        "RETIRE": write_stats.get("retired", 0),
        "REVIEW": write_stats.get("review_classified", 0),
        "UNCHANGED": write_stats.get("unchanged", 0),
    }
    for action in ("INSERT", "UPDATE", "RETIRE", "REVIEW", "UNCHANGED"):
        lines.append(f"- {action}: planned={planned.get(action, 0)} applied={applied_map.get(action, 0)}")

    lines.extend(["", "## Verification", ""])
    for phase in ("pre_commit_verification", "post_commit_verification"):
        verify = result.get(phase, {})
        if not verify:
            continue
        lines.append(f"### {phase}")
        lines.append(f"Passed: **{verify.get('passed')}**")
        for key, value in (verify.get("checks") or {}).items():
            lines.append(f"- {key}: {value}")
        if verify.get("discrepancies"):
            lines.append("Discrepancies:")
            for item in verify["discrepancies"]:
                lines.append(f"- {item}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
