"""Audit remaining OEL blockers and legacy_reference composition."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from ingestion.pdf_geometry import extract_cas_numbers
from persistence.domain_reconciliation import OEL_COLUMN_MAP

PROMOTION_REPORT = Path(r"E:\temp\ohse_geometry_promotion_report.json")
BLOCKER_REPORT = Path(r"E:\temp\ohse_oel_review_blockers_report.json")
OUT_JSON = Path(r"E:\temp\ohse_f02_blocker_audit.json")

LIMIT_COLS = {1, 2, 3}
NAME_COL = OEL_COLUMN_MAP["chemical_name"]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def promotion_cells_by_row(promotion: dict[str, Any]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for cell in promotion.get("cells", []):
        grouped[(cell["table_id"], cell["row_index"])].append(cell)
    return grouped


def classify_no_accept_limit(row_cells: list[dict[str, Any]]) -> str:
    name_cell = next((c for c in row_cells if c["column_index"] == NAME_COL), None)
    limit_cells = [c for c in row_cells if c["column_index"] in LIMIT_COLS and c.get("cell_text", "").strip()]
    if not limit_cells:
        return "genuine_no_limit_text"
    rejected_limits = [c for c in limit_cells if c.get("disposition") != "accept"]
    if not rejected_limits:
        return "resolver_bug_name_or_cas"
    reasons = Counter(c.get("rejection_reason", "unknown") for c in rejected_limits)
    if all(r == "missing_bbox" for r in reasons):
        if name_cell and name_cell.get("disposition") == "accept":
            return "processing_order_or_geometry_retry_candidate"
        return "genuine_missing_limit_evidence"
    if any(r in {"empty_cell"} for r in reasons):
        return "genuine_no_limit_text"
    return f"genuine_limit_reject:{dict(reasons)}"


def classify_name_not_accepted(row_cells: list[dict[str, Any]]) -> str:
    name_cell = next((c for c in row_cells if c["column_index"] == NAME_COL), None)
    if not name_cell:
        return "genuine_missing_name_cell"
    text = (name_cell.get("cell_text") or "").strip()
    if not text:
        return "genuine_empty_name"
    reason = name_cell.get("rejection_reason", "unknown")
    if reason == "missing_bbox" and name_cell.get("search_for_hit_count", 0) > 0:
        return "resolver_promotion_bug"
    if reason == "low_bbox_confidence":
        return "genuine_ambiguous_evidence"
    if reason == "missing_bbox":
        return "genuine_missing_evidence"
    return f"genuine_reject:{reason}"


def classify_missing_cas(name_text: str) -> str:
    if not name_text.strip():
        return "genuine_empty_name"
    cas = extract_cas_numbers(name_text)
    if cas:
        return "resolver_promotion_bug_cas_extractable"
    compact = name_text.replace(" ", "")
    if "[" in compact or "]" in compact or "-" in compact:
        return "genuine_corrupted_cas_ocr"
    return "genuine_no_cas_in_source"


def audit_blockers(blockers: dict[str, Any], promotion: dict[str, Any]) -> dict[str, Any]:
    by_row = promotion_cells_by_row(promotion)
    result: dict[str, Any] = {
        "no_accept_limit": {"counts": Counter(), "rows": []},
        "chemical_name_not_accepted": {"counts": Counter(), "rows": []},
        "missing_cas": {"counts": Counter(), "rows": []},
    }

    for reason_key in ("no_accept_limit", "chemical_name_not_accepted", "missing_cas"):
        rows = blockers.get("by_reason", {}).get(reason_key, [])
        for entry in rows:
            table_id = entry.get("stable_table_id") or entry.get("table_id") or entry.get("source_table_id")
            row_index = entry.get("row_index") or entry.get("source_row_index")
            if row_index is None and entry.get("source_row_key"):
                row_index = int(str(entry["source_row_key"]).split(":row_")[-1])
            key = (table_id, row_index)
            row_cells = by_row.get(key, [])
            name_cell = next((c for c in row_cells if c["column_index"] == NAME_COL), None)
            name_text = (name_cell or {}).get("cell_text") or entry.get("chemical_name") or ""

            if reason_key == "no_accept_limit":
                classification = classify_no_accept_limit(row_cells)
            elif reason_key == "chemical_name_not_accepted":
                classification = classify_name_not_accepted(row_cells)
            else:
                classification = classify_missing_cas(name_text)

            result[reason_key]["counts"][classification] += 1
            result[reason_key]["rows"].append(
                {
                    "table_id": table_id,
                    "row_index": row_index,
                    "classification": classification,
                    "chemical_name": name_text[:120],
                    "limit_cells": [
                        {
                            "col": c["column_index"],
                            "text": c.get("cell_text"),
                            "disposition": c.get("disposition"),
                            "rejection_reason": c.get("rejection_reason"),
                        }
                        for c in row_cells
                        if c["column_index"] in LIMIT_COLS
                    ],
                    "name_disposition": (name_cell or {}).get("disposition"),
                }
            )

    for key in result:
        result[key]["counts"] = dict(result[key]["counts"])
    return result


_ROW_INDEX_EXPR = "CAST(substring(l.source_row_key from 'row_([0-9]+)$') AS INT)"


def audit_legacy_from_db() -> dict[str, Any]:
    from config.settings import get_settings
    from sqlalchemy import create_engine, text

    settings = get_settings()
    engine = create_engine(str(settings.database_url))
    with engine.connect() as conn:
        total = conn.execute(
            text(
                "SELECT COUNT(*) FROM oel_chemical_limits "
                "WHERE validation_status = 'legacy_reference'"
            )
        ).scalar_one()
        duplicate_keys = conn.execute(
            text(
                "SELECT source_row_key, COUNT(*) AS cnt "
                "FROM oel_chemical_limits "
                "WHERE source_row_key IS NOT NULL "
                "GROUP BY source_row_key HAVING COUNT(*) > 1"
            )
        ).all()
        dup_retired = sum(row.cnt - 1 for row in duplicate_keys)
        active_legacy = total
        with_accepted_name = conn.execute(
            text(
                "SELECT COUNT(DISTINCT l.source_row_key) FROM oel_chemical_limits l "
                "JOIN extracted_tables t ON t.stable_table_id = split_part(l.source_row_key, ':', 1) "
                "JOIN table_cells tc ON tc.table_id = t.id "
                f"AND tc.row_index = {_ROW_INDEX_EXPR} "
                "AND tc.column_index = 5 "
                "WHERE l.validation_status = 'legacy_reference' "
                "AND tc.source_reference->'validation'->>'disposition' = 'ACCEPT'"
            )
        ).scalar_one()
        with_accepted_limit = conn.execute(
            text(
                "SELECT COUNT(DISTINCT l.source_row_key) FROM oel_chemical_limits l "
                "JOIN extracted_tables t ON t.stable_table_id = split_part(l.source_row_key, ':', 1) "
                "JOIN table_cells tc ON tc.table_id = t.id "
                f"AND tc.row_index = {_ROW_INDEX_EXPR} "
                "AND tc.column_index IN (1, 2, 3) "
                "WHERE l.validation_status = 'legacy_reference' "
                "AND tc.source_reference->'validation'->>'disposition' = 'ACCEPT'"
            )
        ).scalar_one()
        potential_canonical = conn.execute(
            text(
                "SELECT COUNT(*) FROM oel_chemical_limits l "
                "WHERE l.validation_status = 'legacy_reference' "
                "AND EXISTS ("
                "  SELECT 1 FROM extracted_tables t "
                "  JOIN table_cells name_cell ON name_cell.table_id = t.id "
                f"  AND name_cell.row_index = {_ROW_INDEX_EXPR} "
                "  AND name_cell.column_index = 5 "
                "  AND name_cell.source_reference->'validation'->>'disposition' = 'ACCEPT' "
                "  JOIN table_cells limit_cell ON limit_cell.table_id = t.id "
                "  AND limit_cell.row_index = name_cell.row_index "
                "  AND limit_cell.column_index IN (1, 2, 3) "
                "  AND limit_cell.source_reference->'validation'->>'disposition' = 'ACCEPT' "
                "  WHERE t.stable_table_id = split_part(l.source_row_key, ':', 1)"
                ")"
            )
        ).scalar_one()
        blocked = max(0, active_legacy - potential_canonical)

    return {
        "total_legacy_reference": total,
        "duplicate_source_row_keys": len(duplicate_keys),
        "retired_duplicates_estimate": dup_retired,
        "active_legacy_reference": active_legacy,
        "legacy_with_accepted_name_cell": with_accepted_name,
        "legacy_with_accepted_limit_cell": with_accepted_limit,
        "potential_canonical_evidence_sufficient": potential_canonical,
        "blocked_missing_ambiguous_evidence": blocked,
        "outside_canonical_scope_estimate": 0,
    }


def main() -> None:
    promotion = load_json(PROMOTION_REPORT)
    blockers = load_json(BLOCKER_REPORT)
    blocker_audit = audit_blockers(blockers, promotion)
    legacy_audit = audit_legacy_from_db()
    payload = {
        "blocker_audit": blocker_audit,
        "legacy_reference_audit": legacy_audit,
        "promotion_summary": promotion.get("summary", {}),
        "blocker_summary": blockers.get("summary", {}),
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
