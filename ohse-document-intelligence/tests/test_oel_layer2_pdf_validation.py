"""Validate Layer 2 STEL/TWA/MW against PDF geometry for pages 46-162.

Does not encode page/CAS/chemical expected values in production code.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goldset_generator.table_detection_gate import has_chemical_oel_signatures
from pipeline_contracts.numeric_integrity import parse_slash_decimal
from ingestion.merged_row_splitter import (
    extract_cas_geometry,
    group_cas_by_pdf_row,
    reconstruct_limit_expression_from_pdf_words,
    _ascii_digits,
    _cluster_words_by_x,
    _group_y_bands,
    _limit_header_anchors,
    _pdf_words,
    _pick_limit_cluster,
    _reconstructed_limit_has_unit,
)

PDF = ROOT.parent / "OHE6.pdf"
PENDING = ROOT / "data" / "intermediate" / "validated_structure_46-162.pending.json"
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
CDOT_RE = re.compile(r"\\cdot|\\pi")
DIGIT_RE = re.compile(r"\d|[۰-۹٠-٩]")
RANGES = (
    (46, 55),
    (56, 80),
    (81, 100),
    (101, 120),
    (121, 140),
    (141, 162),
)


def _cell_text(cell: dict | None) -> str:
    if not cell:
        return ""
    return str(cell.get("text") or cell.get("normalized_value") or "").strip()


def _col_map(row: list) -> dict[int, dict]:
    return {
        int(c["column"]): c
        for c in row
        if isinstance(c, dict) and c.get("column") is not None
    }


def _l2_logical_rows(payload: dict) -> list[dict]:
    rows = []
    for table in payload.get("tables") or []:
        page = int(table.get("page_number") or 0)
        body = (table.get("rows") or [])[1:]
        for index, row in enumerate(body, start=1):
            cols = _col_map(row)
            blob = " ".join(
                _cell_text(c) for c in row if isinstance(c, dict)
            )
            cas = CAS_RE.findall(blob)
            if not cas and not blob.strip():
                continue
            stel_cell = cols.get(2) or {}
            twa_cell = cols.get(3) or {}
            mw_cell = cols.get(4) or {}
            stel_sr = stel_cell.get("source_reference") or {}
            twa_sr = twa_cell.get("source_reference") or {}
            rows.append(
                {
                    "page": page,
                    "table_id": table.get("table_id"),
                    "row_index": index,
                    "cas": cas,
                    "stel": _cell_text(stel_cell),
                    "twa": _cell_text(twa_cell),
                    "mw": _cell_text(mw_cell),
                    "stel_unresolved": bool(stel_sr.get("physical_value_unresolved")),
                    "twa_unresolved": bool(twa_sr.get("physical_value_unresolved")),
                    "stel_latex": bool(CDOT_RE.search(_cell_text(stel_cell))),
                    "twa_latex": bool(CDOT_RE.search(_cell_text(twa_cell))),
                    "stel_da": str(stel_sr.get("da_original_text") or ""),
                    "twa_da": str(twa_sr.get("da_original_text") or ""),
                }
            )
    return rows


def _norm(value: str) -> str:
    return _ascii_digits(value).lower().replace(" ", "")


def _is_limit_text(value: str) -> bool:
    lowered = _norm(value)
    return (
        "mg/m" in lowered
        or "ppm" in lowered
        or "ppb" in lowered
        or "f/ml" in lowered
    )


def _pdf_limits_for_page(page, cas_y_override=None):
    geometry = extract_cas_geometry(page)
    if not geometry:
        return []
    cas_values = [item.cas for item in geometry]
    groups = group_cas_by_pdf_row(cas_values, geometry)
    cas_y = cas_y_override or {item.cas: item.y for item in geometry}
    bands = _group_y_bands(groups, cas_y)
    anchors = _limit_header_anchors(page)
    words = _pdf_words(page)
    result = []
    for band, group in zip(bands, groups):
        if band is None:
            continue
        y0, y1 = band
        in_band = [
            word
            for word in words
            if y0 <= (word[1] + word[3]) / 2.0 <= y1
        ]
        clusters = _cluster_words_by_x(in_band)
        slots = {}
        for slot in (0, 1):
            cluster = _pick_limit_cluster(
                clusters,
                limit_slot=slot,
                limit_slot_count=2,
                anchors=anchors,
            )
            reconstructed = (
                reconstruct_limit_expression_from_pdf_words(cluster)
                if cluster
                else None
            )
            slots[slot] = reconstructed
        mw_cluster = None
        for cluster in clusters:
            rec = reconstruct_limit_expression_from_pdf_words(cluster)
            if rec and _reconstructed_limit_has_unit(rec):
                continue
            tokens = [w[4] for w in cluster]
            if any(DIGIT_RE.search(t) for t in tokens) and any(
                "/" in t or t == "/" for t in tokens
            ):
                mw_cluster = " ".join(tokens)
                break
        result.append({"group": group, "stel": slots[0], "twa": slots[1], "mw": mw_cluster})
    return result


def _primary_number(value: str) -> str:
    text = _ascii_digits(value or "")
    slash = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if slash:
        parsed = parse_slash_decimal(slash.group(1), slash.group(2))
        if parsed:
            return parsed
    match = re.search(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def _same_number(left: str, right: str) -> bool:
    a = _primary_number(left)
    b = _primary_number(right)
    return bool(a) and a == b


def validate_payload(payload: dict, pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    logical = _l2_logical_rows(payload)
    tables = [
        t
        for t in (payload.get("tables") or [])
        if t.get("table_type") == "chemical_oel"
    ]
    pdf_supported = 0
    correct = 0
    wrong_column = 0
    mw_to_limit = 0
    limit_to_mw = 0
    pdf_missing = 0
    unresolved = 0
    latex = 0
    guessed = 0

    by_page: dict[int, list] = {}
    for row in logical:
        by_page.setdefault(row["page"], []).append(row)

    for page_number, rows in by_page.items():
        page = doc[page_number - 1]
        pdf_rows = _pdf_limits_for_page(page)
        pdf_by_cas = {}
        for pdf_row in pdf_rows:
            for cas in pdf_row["group"]:
                pdf_by_cas[cas] = pdf_row

        for row in rows:
            if row["stel_latex"]:
                latex += 1
            if row["twa_latex"]:
                latex += 1
            if row["stel_unresolved"]:
                unresolved += 1
            if row["twa_unresolved"]:
                unresolved += 1
            if _is_limit_text(row["mw"]):
                limit_to_mw += 1

            pdf_row = None
            for cas in row["cas"]:
                pdf_row = pdf_by_cas.get(cas)
                if pdf_row:
                    break
            if pdf_row is None:
                continue

            mw_blob = _ascii_digits(pdf_row["mw"] or "")
            for slot, l2_val, pdf_val, other in (
                ("stel", row["stel"], pdf_row["stel"], pdf_row["twa"]),
                ("twa", row["twa"], pdf_row["twa"], pdf_row["stel"]),
            ):
                if pdf_val:
                    pdf_supported += 1
                    if row[f"{slot}_latex"] or not l2_val or row[f"{slot}_unresolved"]:
                        pdf_missing += 1
                        continue
                    if _same_number(l2_val, pdf_val):
                        correct += 1
                    elif other and _same_number(l2_val, other) and not _same_number(pdf_val, other):
                        wrong_column += 1
                    elif mw_blob and not _is_limit_text(l2_val) and _ascii_digits(l2_val).replace(" ", "")[:3] == mw_blob.replace(" ", "")[:3]:
                        mw_to_limit += 1
                    else:
                        pdf_missing += 1
                else:
                    da = row["stel_da"] if slot == "stel" else row["twa_da"]
                    if (
                        l2_val
                        and _is_limit_text(l2_val)
                        and not CDOT_RE.search(l2_val)
                        and CDOT_RE.search(da)
                    ):
                        guessed += 1

    doc.close()
    return {
        "tables": len(tables),
        "logical_rows": len(logical),
        "stel_twa_mw_cells": len(logical) * 3,
        "pdf_supported_numeric_cells": pdf_supported,
        "correct_assignments": correct,
        "wrong_column_assignments": wrong_column,
        "mw_to_stel_twa_errors": mw_to_limit,
        "stel_twa_to_mw_errors": limit_to_mw,
        "pdf_supported_but_missing": pdf_missing,
        "unresolved_values": unresolved,
        "remaining_latex_artifacts": latex,
        "guessed_from_latex": guessed,
    }


@pytest.fixture(scope="module")
def pending_payload():
    if not PENDING.exists():
        pytest.skip("pending Layer 2 artifact not present")
    return json.loads(PENDING.read_text(encoding="utf-8"))


@pytest.mark.parametrize("start,end", RANGES)
def test_representative_oel_table_in_range(pending_payload, start, end):
    if not PDF.exists():
        pytest.skip("OHE6.pdf not available")
    tables = [
        t
        for t in pending_payload.get("tables") or []
        if start <= int(t.get("page_number") or 0) <= end
        and t.get("table_type") == "chemical_oel"
    ]
    if not tables:
        pytest.skip(f"no OEL table in {start}-{end}")
    subset = {
        "tables": [tables[0]],
        "cells": [],
    }
    report = validate_payload(subset, PDF)
    assert report["wrong_column_assignments"] == 0
    assert report["mw_to_stel_twa_errors"] == 0
    assert report["stel_twa_to_mw_errors"] == 0
    assert report["guessed_from_latex"] == 0


def test_full_46_162_layer2_column_integrity(pending_payload):
    if not PDF.exists():
        pytest.skip("OHE6.pdf not available")
    report = validate_payload(pending_payload, PDF)
    assert report["wrong_column_assignments"] == 0
    assert report["mw_to_stel_twa_errors"] == 0
    assert report["stel_twa_to_mw_errors"] == 0
    assert report["guessed_from_latex"] == 0
