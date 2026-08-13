"""Pre-persistence promotion validation for all OEL table pages. Read-only."""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from config.settings import get_settings
from document_ai.bbox_provenance import resolve_bbox_inputs_from_cell
from document_ai.geometry_cell_ordering import geometry_resolve_sort_key
from document_ai.geometry_promotion import (
    PromotionCellInput,
    bbox_y_center,
    resolve_cells_with_row_anchor_retry,
    search_for_hit_count,
)
from document_ai.geometry_search import search_query_variants
from document_ai.geometry_resolver import GeometryResolver
from document_ai.geometry_validation_checks import (
    ROW_Y_MISMATCH_THRESHOLD,
    TALL_BBOX_THRESHOLD,
    has_cross_row_bbox_union,
    is_numeric_wrong_row_match,
    should_flag_row_y_mismatch,
)
from ingestion.pdf_geometry import normalize_match_text

PDF_PATH = Path(r"e:\cursor projects\HSE6 AI Agent\OHE6.pdf")
INTERMEDIATE = PROJECT / "data/intermediate"
LAYER_A_VS = Path(r"E:\temp\validated_structure_46-55_layer_a.json")
OUT_JSON = Path(r"E:\temp\ohse_geometry_promotion_report.json")
OUT_MD = Path(r"E:\temp\ohse_geometry_promotion_report.md")

STRUCTURE_BATCHES = [
    INTERMEDIATE / "validated_structure_46-70.json",
    INTERMEDIATE / "validated_structure_71-95.json",
    INTERMEDIATE / "validated_structure_96-110.json",
    INTERMEDIATE / "validated_structure_140-140.json",
    INTERMEDIATE / "validated_structure_144-144.json",
]

NUMERIC_ONLY_RE = re.compile(r"^[\d۰-۹٠-٩\s./,-]+$")
TWA_RE = re.compile(r"\bTWA\b", re.I)
STEL_RE = re.compile(r"\bSTEL\b", re.I)
CEIL_RE = re.compile(r"\bCeiling\b|\bC\b", re.I)
LAYER_A_PAGES = set(range(46, 56))


@dataclass
class SuspiciousCase:
    category: str
    page_number: int
    table_id: str
    row_index: int
    column_index: int
    cell_text: str
    selected_bbox: dict | None
    match_method: str
    confidence: float
    why: str
    gate: str
    persist: bool
    bbox_height: float | None = None


@dataclass
class CellRecord:
    page_number: int
    table_id: str
    cell_id: str
    row_index: int
    column_index: int
    cell_text: str
    normalized_text: str
    resolved_bbox: dict | None
    bbox_source: str | None
    input_bbox_source: str | None
    match_method: str
    bbox_confidence: float
    gate: str
    persist: bool
    disposition: str
    rejection_reason: str
    review_flags: list[str] = field(default_factory=list)
    search_for_hit_count: int = 0
    bbox_y_center: float | None = None
    bbox_height: float | None = None
    has_traceable_evidence: bool = False
    suspicious: list[str] = field(default_factory=list)
    cell_source: str | None = None


def all_search_rects(page: fitz.Page, text: str) -> list[fitz.Rect]:
    for candidate in search_query_variants(text):
        rects = page.search_for(candidate)
        if rects:
            return list(rects)
    return []


def search_hits_span_row_bands(
    page: fitz.Page,
    text: str,
    selected_y: float | None,
    *,
    tolerance: float = ROW_Y_MISMATCH_THRESHOLD,
) -> bool:
    """True when search_for hits span distinct row bands from the selected bbox."""
    rects = all_search_rects(page, text)
    if len(rects) <= 1:
        return False

    y_centers = sorted({round(rect.y0 + rect.height / 2.0, 0) for rect in rects})
    bands: list[float] = []
    for y_center in y_centers:
        if not any(abs(y_center - band) <= tolerance for band in bands):
            bands.append(y_center)
    if len(bands) <= 1:
        return False
    if selected_y is None:
        return True
    return any(abs(y_center - selected_y) > tolerance for y_center in bands)


def estimate_row_y_centers(cells: list[CellRecord]) -> dict[tuple[str, int], float]:
    groups: dict[tuple[str, int], list[float]] = defaultdict(list)
    for cell in cells:
        if cell.bbox_y_center is not None and cell.persist:
            groups[(cell.table_id, cell.row_index)].append(cell.bbox_y_center)
    return {key: statistics.median(values) for key, values in groups.items() if values}


def _append_table_cells(cells: list[dict[str, Any]], table: dict[str, Any]) -> None:
    page = int(table["page_number"])
    table_id = str(table["table_id"])
    for row in table.get("rows", []):
        for cell in row:
            da_bbox, bbox_provenance = resolve_bbox_inputs_from_cell(cell)
            cells.append(
                {
                    "page_number": page,
                    "table_id": table_id,
                    "cell_id": cell.get("cell_id") or f"cell_{table_id}_{cell.get('row')}_{cell.get('column')}",
                    "row_index": int(cell.get("row", 0)),
                    "column_index": int(cell.get("column", 0)),
                    "text": (cell.get("text") or "").strip(),
                    "document_ai_bbox": da_bbox,
                    "bbox_provenance": bbox_provenance,
                    "input_bbox_source": cell.get("bbox_source"),
                    "cell_source": cell.get("source"),
                }
            )


def load_structure_cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    layer_a_loaded: set[int] = set()

    if LAYER_A_VS.exists():
        data = json.loads(LAYER_A_VS.read_text(encoding="utf-8"))
        for table in data.get("tables", []):
            page = int(table["page_number"])
            if page in LAYER_A_PAGES:
                _append_table_cells(cells, table)
                layer_a_loaded.add(page)

    for batch_path in STRUCTURE_BATCHES:
        if not batch_path.exists():
            continue
        data = json.loads(batch_path.read_text(encoding="utf-8"))
        for table in data.get("tables", []):
            page = int(table["page_number"])
            if page in layer_a_loaded:
                continue
            _append_table_cells(cells, table)

    cells.sort(
        key=lambda cell: geometry_resolve_sort_key(
            page_number=cell["page_number"],
            table_id=cell["table_id"],
            row_index=cell["row_index"],
            column_index=cell["column_index"],
        )
    )
    return cells


def conf_bucket(confidence: float) -> str:
    if confidence < 0.85:
        return "<0.85"
    if confidence < 0.90:
        return "0.85-0.90"
    if confidence < 0.95:
        return "0.90-0.95"
    if confidence < 0.98:
        return "0.95-0.98"
    return "0.98-1.00"


def aggregate_scope(records: list[CellRecord]) -> dict[str, Any]:
    total = len(records)
    accepted = [record for record in records if record.disposition == "accept"]
    review = [record for record in records if record.disposition == "review"]
    rejected = [record for record in records if record.disposition == "reject"]
    return {
        "total_cells": total,
        "accepted": len(accepted),
        "review": len(review),
        "rejected": len(rejected),
        "acceptance_pct": round(100.0 * len(accepted) / total, 2) if total else 0.0,
        "rejection_reasons": dict(Counter(record.rejection_reason for record in rejected)),
        "review_flags": dict(Counter(flag for record in review for flag in record.review_flags)),
        "match_method_all": dict(Counter(record.match_method for record in records)),
        "match_method_accepted": dict(Counter(record.match_method for record in accepted)),
        "confidence_buckets": dict(Counter(conf_bucket(record.bbox_confidence) for record in records)),
        "bbox_source": dict(Counter(record.bbox_source or "none" for record in records)),
        "input_bbox_source": dict(Counter(record.input_bbox_source or "none" for record in records)),
    }


def _promotion_result_to_cell_record(result) -> CellRecord:
    return CellRecord(
        page_number=result.page_number,
        table_id=result.table_id,
        cell_id=result.cell_id,
        row_index=result.row_index,
        column_index=result.column_index,
        cell_text=result.cell_text,
        normalized_text=result.normalized_text,
        resolved_bbox=result.resolved_bbox,
        bbox_source=result.bbox_source,
        input_bbox_source=result.input_bbox_source,
        match_method=result.match_method,
        bbox_confidence=result.bbox_confidence,
        gate=result.gate,
        persist=result.persist,
        disposition=result.disposition,
        rejection_reason=result.rejection_reason,
        search_for_hit_count=result.search_for_hit_count,
        bbox_y_center=result.bbox_y_center,
        bbox_height=result.bbox_height,
        has_traceable_evidence=result.has_traceable_evidence,
        cell_source=result.cell_source,
    )


def main() -> None:
    settings = get_settings()
    threshold = settings.bbox_confidence_threshold
    input_cells = load_structure_cells()
    all_pages = sorted({cell["page_number"] for cell in input_cells})

    page_fitz: dict[int, fitz.Page] = {}

    promotion_inputs = [
        PromotionCellInput(
            page_number=item["page_number"],
            table_id=item["table_id"],
            cell_id=item["cell_id"],
            row_index=item["row_index"],
            column_index=item["column_index"],
            text=item["text"],
            document_ai_bbox=item.get("document_ai_bbox"),
            bbox_provenance=item.get("bbox_provenance"),
            input_bbox_source=item.get("input_bbox_source"),
            cell_source=item.get("cell_source"),
        )
        for item in input_cells
    ]

    with fitz.open(PDF_PATH) as doc, GeometryResolver(PDF_PATH) as resolver:
        for page_number in all_pages:
            page_fitz[page_number] = doc[page_number - 1]

        resolved = resolve_cells_with_row_anchor_retry(
            resolver,
            promotion_inputs,
            threshold=threshold,
            page_lookup=lambda page_number: page_fitz[page_number],
        )

    records_by_id = {
        cell_id: _promotion_result_to_cell_record(result)
        for cell_id, result in resolved.items()
    }

    records = list(records_by_id.values())
    records.sort(
        key=lambda record: geometry_resolve_sort_key(
            page_number=record.page_number,
            table_id=record.table_id,
            row_index=record.row_index,
            column_index=record.column_index,
        )
    )
    row_y = estimate_row_y_centers(records)
    suspicious: list[SuspiciousCase] = []
    numeric_wrong_row_accepted = 0
    cross_row_union_accepted = 0

    with fitz.open(PDF_PATH) as doc:
        page_fitz = {page_number: doc[page_number - 1] for page_number in all_pages}

        for rec in records:
            text = rec.cell_text
            key = (rec.table_id, rec.row_index)

            if rec.search_for_hit_count > 1 and rec.match_method == "exact" and rec.persist:
                row_aligned = (
                    key in row_y
                    and rec.bbox_y_center is not None
                    and abs(rec.bbox_y_center - row_y[key]) <= ROW_Y_MISMATCH_THRESHOLD
                )
                if search_hits_span_row_bands(page_fitz[rec.page_number], text, rec.bbox_y_center) and not row_aligned:
                    suspicious.append(
                        SuspiciousCase(
                            category="A_multi_search_for",
                            page_number=rec.page_number,
                            table_id=rec.table_id,
                            row_index=rec.row_index,
                            column_index=rec.column_index,
                            cell_text=text[:120],
                            selected_bbox=rec.resolved_bbox,
                            match_method=rec.match_method,
                            confidence=rec.bbox_confidence,
                            why=f"search_for returned {rec.search_for_hit_count} rects across row bands",
                            gate=rec.gate,
                            persist=rec.persist,
                            bbox_height=rec.bbox_height,
                        )
                    )
                    rec.suspicious.append("A")
                    if rec.disposition == "accept":
                        rec.disposition = "review"
                        rec.review_flags.append("A_multi_search_for")

            if rec.bbox_y_center is not None and key in row_y and rec.persist:
                expected = row_y[key]
                if should_flag_row_y_mismatch(
                    cell_text=text,
                    bbox_y_center=rec.bbox_y_center,
                    expected_row_y=expected,
                    bbox_height=rec.bbox_height,
                ):
                    suspicious.append(
                        SuspiciousCase(
                            category="B_row_y_mismatch",
                            page_number=rec.page_number,
                            table_id=rec.table_id,
                            row_index=rec.row_index,
                            column_index=rec.column_index,
                            cell_text=text[:120],
                            selected_bbox=rec.resolved_bbox,
                            match_method=rec.match_method,
                            confidence=rec.bbox_confidence,
                            why=f"y_center={rec.bbox_y_center:.1f} vs row median={expected:.1f}",
                            gate=rec.gate,
                            persist=rec.persist,
                            bbox_height=rec.bbox_height,
                        )
                    )
                    rec.suspicious.append("B")

            if rec.persist and has_cross_row_bbox_union(rec.bbox_height):
                cross_row_union_accepted += 1
                suspicious.append(
                    SuspiciousCase(
                        category="C_tall_bbox",
                        page_number=rec.page_number,
                        table_id=rec.table_id,
                        row_index=rec.row_index,
                        column_index=rec.column_index,
                        cell_text=text[:120],
                        selected_bbox=rec.resolved_bbox,
                        match_method=rec.match_method,
                        confidence=rec.bbox_confidence,
                        why=f"bbox height={rec.bbox_height:.1f}pt",
                        gate=rec.gate,
                        persist=rec.persist,
                        bbox_height=rec.bbox_height,
                    )
                )
                rec.suspicious.append("C")

            if (
                text
                and NUMERIC_ONLY_RE.match(rec.normalized_text.replace(" ", ""))
                and rec.search_for_hit_count > 1
            ):
                suspicious.append(
                    SuspiciousCase(
                        category="D_numeric_duplicate",
                        page_number=rec.page_number,
                        table_id=rec.table_id,
                        row_index=rec.row_index,
                        column_index=rec.column_index,
                        cell_text=text[:120],
                        selected_bbox=rec.resolved_bbox,
                        match_method=rec.match_method,
                        confidence=rec.bbox_confidence,
                        why="numeric-only cell with multiple page occurrences",
                        gate=rec.gate,
                        persist=rec.persist,
                        bbox_height=rec.bbox_height,
                    )
                )
                rec.suspicious.append("D")

            if rec.persist and key in row_y:
                if is_numeric_wrong_row_match(
                    normalized_text=rec.normalized_text,
                    bbox_y_center=rec.bbox_y_center,
                    expected_row_y=row_y[key],
                    persist=True,
                ):
                    numeric_wrong_row_accepted += 1
                    rec.suspicious.append("D_wrong_row")
                    if rec.disposition == "accept":
                        rec.disposition = "review"
                        rec.review_flags.append("numeric_wrong_row")

            if rec.row_index >= 2 and (TWA_RE.search(text) or STEL_RE.search(text) or CEIL_RE.search(text)):
                header_ys = [row_y.get((rec.table_id, row)) for row in (0, 1) if (rec.table_id, row) in row_y]
                if header_ys and rec.bbox_y_center is not None and rec.persist:
                    nearest_header = min(header_ys, key=lambda header_y: abs(header_y - rec.bbox_y_center))
                    if abs(rec.bbox_y_center - nearest_header) < 8:
                        suspicious.append(
                            SuspiciousCase(
                                category="E_header_label_in_data_row",
                                page_number=rec.page_number,
                                table_id=rec.table_id,
                                row_index=rec.row_index,
                                column_index=rec.column_index,
                                cell_text=text[:120],
                                selected_bbox=rec.resolved_bbox,
                                match_method=rec.match_method,
                                confidence=rec.bbox_confidence,
                                why="limit label bbox near header Y",
                                gate=rec.gate,
                                persist=rec.persist,
                                bbox_height=rec.bbox_height,
                            )
                        )
                        rec.suspicious.append("E")

    global_stats = aggregate_scope(records)
    per_page = {str(page): aggregate_scope([record for record in records if record.page_number == page]) for page in all_pages}
    per_table: dict[str, Any] = {}
    for table_id in sorted({record.table_id for record in records}):
        table_cells = [record for record in records if record.table_id == table_id]
        per_table[table_id] = {**aggregate_scope(table_cells), "page_number": table_cells[0].page_number if table_cells else None}
    per_column = {
        str(column): aggregate_scope([record for record in records if record.column_index == column])
        for column in sorted({record.column_index for record in records})
    }

    accepted_suspicious = [case for case in suspicious if case.persist]
    accepted_suspicious_by_cat = Counter(case.category for case in accepted_suspicious)

    missing_rejection_reason = [record for record in records if record.disposition == "reject" and not record.rejection_reason]
    missing_evidence = [record for record in records if record.disposition == "accept" and not record.has_traceable_evidence]

    safety_gates = {
        "accepted_row_y_mismatches": accepted_suspicious_by_cat.get("B_row_y_mismatch", 0),
        "accepted_tall_bbox": accepted_suspicious_by_cat.get("C_tall_bbox", 0),
        "accepted_header_contamination": accepted_suspicious_by_cat.get("E_header_label_in_data_row", 0),
        "numeric_wrong_row_accepted": numeric_wrong_row_accepted,
        "cross_row_bbox_union_accepted": cross_row_union_accepted,
        "accepted_without_traceable_evidence": len(missing_evidence),
        "rejected_without_explicit_reason": len(missing_rejection_reason),
    }
    promotion_passed = all(
        safety_gates[key] == 0
        for key in (
            "accepted_row_y_mismatches",
            "accepted_tall_bbox",
            "accepted_header_contamination",
            "numeric_wrong_row_accepted",
            "cross_row_bbox_union_accepted",
            "accepted_without_traceable_evidence",
            "rejected_without_explicit_reason",
        )
    )

    layer_a_loaded = sorted(LAYER_A_PAGES & set(all_pages))
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "promotion_passed": promotion_passed,
        "source_pdf": str(PDF_PATH),
        "scope": {
            "description": "All OEL table pages (Layer-A VS for 46-55, legacy VS elsewhere)",
            "table_pages": len(all_pages),
            "page_range": [min(all_pages), max(all_pages)] if all_pages else [],
            "layer_a_pages": layer_a_loaded,
            "structure_batches": [path.name for path in STRUCTURE_BATCHES],
        },
        "threshold": threshold,
        "totals": {
            "total_cells": global_stats["total_cells"],
            "accepted": global_stats["accepted"],
            "review": global_stats["review"],
            "rejected": global_stats["rejected"],
        },
        "global": global_stats,
        "safety_gates": safety_gates,
        "suspicious_count_by_category": dict(Counter(case.category for case in suspicious)),
        "accepted_suspicious_count_by_category": dict(accepted_suspicious_by_cat),
        "per_page": per_page,
        "per_table": per_table,
        "per_column": per_column,
        "evidence_bbox_provenance": {
            "resolved_bbox_source": global_stats["bbox_source"],
            "input_bbox_source": global_stats["input_bbox_source"],
        },
        "confidence_distribution": global_stats["confidence_buckets"],
        "suspicious_accepted_cells": [asdict(case) for case in accepted_suspicious],
        "cells": [asdict(record) for record in records],
    }

    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(out)
    print(json.dumps({"promotion_passed": promotion_passed, "safety_gates": safety_gates, "totals": out["totals"]}, indent=2))


def write_markdown(out: dict[str, Any]) -> None:
    totals = out["totals"]
    safety = out["safety_gates"]
    lines = [
        "# OHE6 Geometry Promotion Report",
        "",
        f"Generated: {out['generated_at']}",
        f"Promotion passed: **{out['promotion_passed']}**",
        "",
        "## Totals",
        f"- Total cells: {totals['total_cells']}",
        f"- Accepted: {totals['accepted']}",
        f"- Review: {totals['review']}",
        f"- Rejected: {totals['rejected']}",
        "",
        "## Safety Gates",
    ]
    for key, value in safety.items():
        lines.append(f"- {key}: **{value}**")
    lines.extend(["", "## Rejection Reasons"])
    for reason, count in sorted(out["global"]["rejection_reasons"].items(), key=lambda item: -item[1]):
        lines.append(f"- {reason}: {count}")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
