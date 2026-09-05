"""
Layer 2 — deterministic structural resolver for table reconstruction.

Layer 2 responsibilities
------------------------

1. Preserve Layer 1 Document AI evidence.
2. Apply logical-row reconstruction only to supported table types.
3. Resolve missing cell geometry using the existing geometry resolver.
4. Detect genuinely corrupted / under-columned tables.
5. Recover tables with PyMuPDF only when Document AI structure is unreliable.
6. Keep non-OEL tables generic and untouched.
7. Preserve provenance for every structural decision.
8. Emit explicit OEL schema diagnostics.

Important design rule
---------------------

The PDF contains multiple table schemas.

The chemical OEL table contains:

    ردیف
    نام علمی ماده شیمیایی
    وزن ملکولی
    حد مجاز مواجهه شغلی
        ├── TWA
        └── STEL/C
    نمادها
    مبنای تعیین حد مواجهه مجاز

Therefore:

- "حد مجاز مواجهه شغلی" is a parent/header group.
- TWA and STEL/C are two independent physical columns.
- The OEL table therefore has 7 physical columns.
- We NEVER apply 7 columns globally.
- Non-OEL tables remain generic.

Important implementation rule
-----------------------------

"oel_schema" is a diagnostic/provenance object.

It is NOT a database table.

It is written into:

    validated_structure_<start>-<end>.json

under:

    page_detection[page]["oel_schema"]

and also, where applicable, into each table's structural diagnostics.

Final architecture (authoritative)
-----------------------------------

    Geometry decides physical rows. Semantic reconstruction decides
    logical rows. Neither is allowed to invent numeric values.

    Document AI evidence
            |
            v
    geometry alignment (PyMuPDF + Document AI bbox)
            |
            v
    physical visual rows
            |
            v
    OEL multi-CAS geometry split          <- physical row boundary
    (geometry only; a CAS count is a trigger to *check* geometry,
     never a trigger to split by itself — two CAS values in one
     cell do not necessarily mean two physical rows)
            |
            v
    corrected visual rows
            |
            v
    OEL logical-row reconstruction        <- semantic continuation merge
            |
            v
    logical chemical rows
            |
            v
    OEL column normalization (6->7 limits, basis/symbol expansion)
            |
            v
    merged-cell marking
            |
            v
    PyMuPDF recovery (only when Document AI structure is unreliable)
            |
            v
    validation / goldset output

    Two counters that must never be conflated:

        multi_cas_visual_row_split_count
            physical rows created by _split_oel_multi_cas_rows()
            (geometry-driven).

        logical_row_reconstruction_count
            continuation-row merges created by
            _reconstruct_oel_logical_rows() (semantic), plus OEL
            column-expansion events.

    A page with, say, 10 Document AI visual rows where geometry proves
    2 of them each contain 2 physical CAS rows, and logical
    reconstruction later re-merges 2 continuation fragments back down,
    should read:

        initial visual rows       10
        CAS visual splits         +2   (multi_cas_visual_row_split_count)
        logical merges            -2   (logical_row_reconstruction_count)
        final logical rows        10

    That is the audit trail this module exists to keep intact.

raw_markdown vs rows
---------------------

    `table.raw_markdown` is preserved, untouched, Document AI evidence.
    `table.rows` is the Layer 2 *corrected* structure and will diverge
    from raw_markdown once geometry / logical / column corrections are
    applied. Downstream consumers must not treat raw_markdown as a
    rendering of the final `rows` structure — they represent two
    different things on purpose.

"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import copy
import fitz

from config.logging import get_logger
from config.settings import get_settings

from document_ai.bbox_provenance import (
    BBOX_PROVENANCE_DOCUMENT_AI,
    BBOX_PROVENANCE_PYMUPDF_ALIGNED,
    BBOX_PROVENANCE_PYMUPDF_RECOVERY,
    is_trusted_document_ai_bbox,
    resolve_bbox_inputs,
)

from document_ai.geometry_resolver import (
    GeometryResolver,
    is_valid_bbox,
)

from document_ai.geometry_promotion import (
    PromotionCellInput,
    resolve_cells_with_row_anchor_retry,
)

from goldset_generator.document_processor import (
    ExtractedCellRecord,
    ExtractedTableRecord,
    ProcessedDocument,
    _cell_id,
    _table_id,
)

from goldset_generator.row_visual_band import (
    refine_table_visual_rows,
)

from goldset_generator.table_detection_gate import (
    evaluate_table_detection,
    has_chemical_oel_signatures,
    looks_like_oel_table_page,
)

from ingestion.table_recovery import (
    recover_tables_for_page,
)

from normalization.persian_normalizer import (
    normalize_persian_text,
)

from validation.structural_confidence import (
    compute_structural_confidence,
)
from ingestion.merged_row_splitter import (
    extract_cas_geometry,
    repair_oel_numeric_from_pdf_headers,
    split_table_rows_by_cas_geometry,
)


logger = get_logger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

CAS_PATTERN = re.compile(
    r"\[\s*\d{2,7}\s*-\s*\d{2}\s*-\s*\d\s*\]"
)

LIMIT_MARKERS = re.compile(
    r"\b(ppm|mg/m³|mg/m3|TWA|STEL|STEL/C|Ceiling|C)\b",
    re.IGNORECASE,
)

TWA_PATTERN = re.compile(
    r"\bTWA\b",
    re.IGNORECASE,
)

STEL_PATTERN = re.compile(
    r"\bSTEL\s*/?\s*C\b",
    re.IGNORECASE,
)

ENGLISH_TOKEN_PATTERN = re.compile(
    r"[A-Za-z]{4,}"
)


# ============================================================================
# CAS EXTRACTION — SINGLE SOURCE OF TRUTH
# ============================================================================
#
# CAS_PATTERN above is a *candidate* extractor only. It matches bracketed
# numeric triplets and tolerates PDF/OCR spacing artifacts such as
# "[135410 - 20 - 7]" or "[135410-20 -7]". It is NOT checksum validation —
# that would be a separate, later step if it is ever needed.
#
# Every caller in this module MUST go through extract_cas_values() rather
# than calling CAS_PATTERN.findall()/.search() directly. That keeps
# spacing tolerance and normalization consistent everywhere, and leaves
# exactly one place to change if the CAS matching rule ever changes.
#
# A CAS count is never, by itself, a split decision. It is only ever a
# trigger to *check* physical geometry (see _split_oel_multi_cas_rows()
# below and split_table_rows_by_cas_geometry() in
# ingestion.merged_row_splitter). Two CAS values inside one Document AI
# cell do not necessarily mean two physical PDF rows — a single chemical
# entry can legitimately carry more than one CAS number.
# ============================================================================


def normalize_cas(value: str) -> str:
    """
    Normalize a raw CAS_PATTERN match into the canonical bare-digit form
    used for CAS identity comparisons everywhere in the pipeline.

    Strips whitespace introduced by OCR/PDF spacing artifacts AND strips
    the surrounding brackets, so "[135410 - 20 - 7]", "[135410-20-7]",
    and "135410-20-7" all normalize to the same value: "135410-20-7".

    IMPORTANT: this must stay in sync with
    ingestion.merged_row_splitter._normalize_cas(), which already strips
    brackets. structural_resolver.py and merged_row_splitter.py anchor
    rows to each other by CAS identity (see _reconstruct_oel_logical_rows
    and _split_row_by_cas_geometry) — a bracket-stripping mismatch
    between the two modules silently breaks that anchor and was part of
    the page-54 "row 72 not found" failure.
    """

    return re.sub(
        r"\s+",
        "",
        value,
    ).strip("[]")


def extract_cas_values(
    text: str | None,
) -> list[str]:
    """
    Single centralized CAS candidate extractor.

    Returns normalized CAS strings (brackets included, internal
    whitespace stripped). This is candidate extraction, not validation.

    Every other function in this module that needs CAS values must call
    this instead of touching CAS_PATTERN directly.
    """

    if not text:
        return []

    return [
        normalize_cas(match)
        for match in CAS_PATTERN.findall(text)
    ]


# ============================================================================
# OEL SEMANTIC SCHEMA
# ============================================================================

OEL_HEADER_TERMS = {
    "ردیف",
    "نام علمی",
    "ماده شیمیایی",
    "وزن ملکولی",
    "حد مجاز",
    "مواجهه شغلی",
    "نمادها",
    "مبنای تعیین",
}

OEL_REQUIRED_LIMIT_COLUMNS = {
    "TWA",
    "STEL/C",
}

EXPECTED_OEL_PHYSICAL_COLUMNS = 7

MOLECULAR_WEIGHT_PATTERN = re.compile(
    r"وزن\s*ملکولی|molecular\s*weight",
    re.IGNORECASE,
)

EXPOSURE_VALUE_PATTERN = re.compile(
    r"([\d۰-۹٠-٩]+(?:[./][\d۰-۹٠-٩]+)?)\s*(ppm|mg/m³|mg/m3|f/ml)\b",
    re.IGNORECASE,
)

LATEX_ARTIFACT_PATTERN = re.compile(
    r"\\[A-Za-z]+",
)


# ============================================================================
# TEXT NORMALIZATION
# ============================================================================

def _normalize_for_matching(text: str | None) -> str:
    """
    Normalize Persian/Unicode text for deterministic matching.
    """

    if not text:
        return ""

    normalized = normalize_persian_text(text)

    if normalized:
        return normalized.normalized.strip()

    return text.strip()


ROW_NUMBER_LABEL_PATTERN = re.compile(r"^\D*(\d+)\D*$")


def _normalize_row_number_label(text: str | None) -> str | None:
    """
    Extract a bare row-number label ("72") from an OEL "ردیف" column
    cell, for use as a secondary row-identity anchor in
    _reconstruct_oel_logical_rows(). Returns None for anything that
    isn't a single bare integer, so this never accidentally anchors on
    a chemical name or a multi-value cell — it only ever fires for the
    dedicated row-number column.
    """

    if not text:
        return None

    normalized = _normalize_for_matching(text)

    match = ROW_NUMBER_LABEL_PATTERN.match(normalized)

    if not match:
        return None

    return match.group(1)


# ============================================================================
# TABLE TYPE NORMALIZATION
# ============================================================================

def _table_type_value(table: ExtractedTableRecord) -> str:
    """
    Safely convert table_type enum/string into a comparable string.

    This prevents bugs when table_type is an Enum rather than a raw string.
    """

    value = getattr(table, "table_type", None)

    if value is None:
        return ""

    if hasattr(value, "value"):
        return str(value.value).strip().lower()

    return str(value).strip().lower()


def _is_oel_table_type(table: ExtractedTableRecord) -> bool:
    """
    Explicit table-type check.

    Supports both:

        chemical_oel

    and enum-like values whose .value is chemical_oel.
    """

    value = _table_type_value(table)

    return value in {
        "chemical_oel",
        "oel",
        "chemical-oel",
        "chemical oel",
    }


# ============================================================================
# SOURCE NORMALIZATION
# ============================================================================

def _is_document_ai_cell(cell: ExtractedCellRecord) -> bool:
    """
    Determine whether a cell should be considered Layer 1 evidence.

    IMPORTANT:

    Older pipeline outputs may contain:

        source=None

    even though the cell originated from Document AI.

    We therefore do NOT reject source=None.

    Explicit non-Document-AI sources are rejected.

    Accepted:
        document_ai
        document-ai
        None
        ""

    Rejected:
        pymupdf_recovery
        structural_resolver
        logical_reconstruction
        other explicit recovery sources
    """

    source = getattr(cell, "source", None)

    if source is None:
        return True

    value = str(source).strip().lower()

    if not value:
        return True

    return value in {
        "document_ai",
        "document-ai",
        "documentai",
        "layer1",
        "layer_1",
    }


# ============================================================================
# HEADER EXTRACTION
# ============================================================================

def _table_header_rows(
    table: ExtractedTableRecord,
    max_rows: int = 3,
) -> list[list[ExtractedCellRecord]]:
    """
    Return first few rows.

    OEL uses hierarchical headers, therefore looking only at row 0
    is unsafe.
    """

    if not table.rows:
        return []

    return table.rows[:max_rows]


def _table_header_text(
    table: ExtractedTableRecord,
) -> str:
    """
    Return normalized text from first few header rows.
    """

    rows = _table_header_rows(table)

    parts: list[str] = []

    for row in rows:
        for cell in row:

            text = (cell.text or "").strip()

            if text:
                parts.append(text)

    return " ".join(parts)


def _oel_header_score(
    table: ExtractedTableRecord,
) -> float:
    """
    Semantic confidence that a table is the chemical OEL schema.
    """

    header = _normalize_for_matching(
        _table_header_text(table)
    )

    if not header:
        return 0.0

    matches = 0

    for term in OEL_HEADER_TERMS:

        normalized_term = _normalize_for_matching(term)

        if (
            normalized_term
            and normalized_term in header
        ):
            matches += 1

    return matches / len(OEL_HEADER_TERMS)


def _oel_limit_header_score(
    table: ExtractedTableRecord,
) -> float:
    """
    Detect TWA and STEL/C independently.

    TWA and STEL/C are separate physical columns.
    """

    header = _normalize_for_matching(
        _table_header_text(table)
    )

    if not header:
        return 0.0

    twa = bool(
        TWA_PATTERN.search(header)
    )

    stel = bool(
        STEL_PATTERN.search(header)
    )

    return (
        int(twa) + int(stel)
    ) / 2.0


def _is_chemical_oel_table(
    table: ExtractedTableRecord,
    page_text: str,
) -> bool:
    """
    Determine whether this specific table is the chemical OEL table.
    """

    if _is_oel_table_type(table):
        return True

    if not has_chemical_oel_signatures(
        page_text or ""
    ):
        return False

    header_score = _oel_header_score(table)

    limit_score = _oel_limit_header_score(table)

    return (
        header_score >= 0.30
        and limit_score >= 0.50
    )


# ============================================================================
# PHYSICAL COLUMN ANALYSIS
# ============================================================================

def _physical_columns(
    table: ExtractedTableRecord,
) -> list[int]:
    """
    Return actual physical column indexes.

    This is intentionally table-specific.
    """

    columns: set[int] = set()

    for row in table.rows or []:
        for cell in row:
            columns.add(int(cell.column))

    return sorted(columns)


def _physical_column_count(
    table: ExtractedTableRecord,
) -> int:
    return len(
        _physical_columns(table)
    )


def _max_physical_column_index(
    table: ExtractedTableRecord,
) -> int:
    columns = _physical_columns(table)

    if not columns:
        return -1

    return max(columns)


# ============================================================================
# OEL SCHEMA DIAGNOSTICS
# ============================================================================

def _find_header_columns(
    table: ExtractedTableRecord,
) -> dict[str, list[int]]:
    """
    Find physical columns containing semantic OEL headers.

    This is evidence-based.

    We do NOT assume:

        TWA = column 3
        STEL/C = column 4

    Instead we inspect the actual header cells.
    """

    result: dict[str, list[int]] = {
        "TWA": [],
        "STEL/C": [],
    }

    for row in _table_header_rows(
        table,
        max_rows=3,
    ):

        for cell in row:

            text = cell.text or ""

            if TWA_PATTERN.search(text):
                result["TWA"].append(
                    cell.column
                )

            if STEL_PATTERN.search(text):
                result["STEL/C"].append(
                    cell.column
                )

    result["TWA"] = sorted(
        set(result["TWA"])
    )

    result["STEL/C"] = sorted(
        set(result["STEL/C"])
    )

    return result


def _oel_schema_signals(
    table: ExtractedTableRecord,
) -> dict[str, Any]:
    """
    Build explicit OEL physical schema diagnostics.

    Expected structure:

        0 = ردیف
        1 = نام علمی ماده شیمیایی
        2 = وزن ملکولی
        3 = TWA
        4 = STEL/C
        5 = نمادها
        6 = مبنای تعیین حد مواجهه مجاز

    However, the resolver does NOT force these positions.

    It reports what was actually detected.
    """

    physical_columns = _physical_columns(table)

    header_text = _normalize_for_matching(
        _table_header_text(table)
    )

    header_columns = _find_header_columns(
        table
    )

    twa_columns = header_columns["TWA"]
    stel_columns = header_columns["STEL/C"]

    has_twa = bool(twa_columns)
    has_stel = bool(stel_columns)

    return {
        "schema_name": "chemical_oel",
        "schema_version": "oel_v1",

        "expected_physical_column_count": (
            EXPECTED_OEL_PHYSICAL_COLUMNS
        ),

        "physical_column_count": (
            len(physical_columns)
        ),

        "physical_column_indexes": (
            physical_columns
        ),

        "max_column_index": (
            max(physical_columns)
            if physical_columns
            else -1
        ),

        "has_twa_header": has_twa,

        "has_stel_c_header": has_stel,

        "has_both_limit_headers": (
            has_twa
            and has_stel
        ),

        "twa_physical_columns": (
            twa_columns
        ),

        "stel_c_physical_columns": (
            stel_columns
        ),

        "twa_and_stel_are_distinct_columns": (
            bool(
                set(twa_columns).isdisjoint(
                    set(stel_columns)
                )
            )
            if has_twa and has_stel
            else False
        ),

        "header_score": (
            _oel_header_score(table)
        ),

        "limit_header_score": (
            _oel_limit_header_score(table)
        ),

        "header_text_excerpt": (
            header_text[:2000]
        ),

        "column_count_status": (
            "expected"
            if len(physical_columns)
            == EXPECTED_OEL_PHYSICAL_COLUMNS
            else (
                "under_columned"
                if len(physical_columns)
                < EXPECTED_OEL_PHYSICAL_COLUMNS
                else "over_columned"
            )
        ),

        "schema_validity": (
            "valid"
            if (
                len(physical_columns)
                == EXPECTED_OEL_PHYSICAL_COLUMNS
                and has_twa
                and has_stel
                and bool(
                    set(twa_columns).isdisjoint(
                        set(stel_columns)
                    )
                )
            )
            else "needs_review"
        ),
    }


# ============================================================================
# ROW QUALITY
# ============================================================================

def _row_non_empty_count(
    row: list[ExtractedCellRecord],
) -> int:
    return sum(
        1
        for cell in row
        if (cell.text or "").strip()
    )


def _chemical_rows_with_cas(
    table: ExtractedTableRecord,
) -> int:
    count = 0

    for row in table.rows or []:

        row_text = " ".join(
            cell.text or ""
            for cell in row
        )

        if extract_cas_values(row_text):
            count += 1

    return count


def _cas_count_in_page(
    page_text: str,
) -> int:
    return len(
        extract_cas_values(page_text)
    )


# ============================================================================
# MERGED CELL DETECTION
# ============================================================================

def _is_merged_cell(
    text: str,
    row_span: int = 1,
    column_span: int = 1,
) -> bool:

    if row_span > 1 or column_span > 1:
        return True

    if not text:
        return False

    # Multiple CAS candidates in one cell is a signal of a merged cell,
    # but note this is a *signal*, not a split decision — actual row
    # splitting only ever happens downstream in
    # _split_oel_multi_cas_rows(), and only when PDF geometry proves
    # the CAS values sit at different physical Y positions.
    cas_values = extract_cas_values(text)

    if len(cas_values) > 1:
        return True

    has_cas = bool(cas_values)

    has_limit = bool(
        LIMIT_MARKERS.search(text)
    )

    has_english = bool(
        ENGLISH_TOKEN_PATTERN.search(text)
    )

    return (
        has_cas
        and has_limit
        and has_english
    )


# ============================================================================
# DOCUMENT AI QUALITY
# ============================================================================

def _document_ai_table_quality_poor(
    page_text: str,
    table: ExtractedTableRecord,
) -> bool:
    """
    Determine whether a confirmed OEL table is structurally unreliable.

    IMPORTANT:

    This function NEVER applies the 7-column rule to non-OEL tables.
    """

    if not _is_chemical_oel_table(
        table,
        page_text,
    ):
        return False

    cas_in_page = _cas_count_in_page(
        page_text
    )

    if cas_in_page < 2:
        return False

    schema = _oel_schema_signals(
        table
    )

    physical_columns = schema[
        "physical_column_count"
    ]

    has_both_limit_headers = schema[
        "has_both_limit_headers"
    ]

    # ------------------------------------------------------------
    # Physical column structure
    # ------------------------------------------------------------

    if physical_columns < 6:
        return True

    if physical_columns == 6:

        if not has_both_limit_headers:
            return True

        cas_in_table = (
            _chemical_rows_with_cas(table)
        )

        if (
            cas_in_page >= 4
            and cas_in_table
            < max(
                2,
                cas_in_page // 2,
            )
        ):
            return True

    # 7+ columns are NOT automatically bad.
    # 7 is the expected OEL physical schema.
    # >7 may require review, but recovery should not be automatic
    # based only on column count.

    # ------------------------------------------------------------
    # Header CAS pollution — multiple CAS in the primary header row
    # signals transposed / corrupt Document AI structure.
    #
    # Only inspect row 0. Data rows legitimately contain CAS numbers.
    # ------------------------------------------------------------

    if table.rows:
        header_text = " ".join(
            (cell.text or "")
            for cell in table.rows[0]
        )

        if len(
            extract_cas_values(
                header_text,
            )
        ) >= 2:
            return True

    # ------------------------------------------------------------
    # Header quality
    # ------------------------------------------------------------

    header_score = schema[
        "header_score"
    ]

    limit_header_score = schema[
        "limit_header_score"
    ]

    if (
        header_score < 0.30
        and cas_in_page >= 3
    ):
        return True

    if (
        limit_header_score == 0.0
        and cas_in_page >= 4
    ):
        return True

    # ------------------------------------------------------------
    # CAS distribution
    # ------------------------------------------------------------

    cas_in_table = (
        _chemical_rows_with_cas(table)
    )

    if cas_in_page >= 4:

        if cas_in_table < max(
            2,
            cas_in_page // 2,
        ):
            return True

    # ------------------------------------------------------------
    # Row structure
    # ------------------------------------------------------------

    data_rows = (
        table.rows[1:]
        if len(table.rows) > 1
        else []
    )

    if not data_rows:
        return True

    meaningful_rows = sum(
        1
        for row in data_rows
        if _row_non_empty_count(row) >= 2
    )

    if meaningful_rows == 0:
        return True

    if len(data_rows) >= 4:

        meaningful_ratio = (
            meaningful_rows
            / len(data_rows)
        )

        if meaningful_ratio < 0.30:
            return True

    # ------------------------------------------------------------
    # Merged cells
    # ------------------------------------------------------------

    all_cells = table.flat_cells()

    if all_cells:

        merged_count = sum(
            1
            for cell in all_cells
            if _is_merged_cell(
                cell.text,
                getattr(
                    cell,
                    "row_span",
                    1,
                ),
                getattr(
                    cell,
                    "column_span",
                    1,
                ),
            )
        )

        merged_ratio = (
            merged_count
            / len(all_cells)
        )

        if (
            merged_ratio > 0.55
            and cas_in_table < cas_in_page
        ):
            return True

    return False


def _dai_oel_grid_repairable_in_place(
    table: ExtractedTableRecord,
) -> bool:
    """True when Document AI already has an OEL grid overlay can repair.

    Header CAS pollution or incomplete CAS-in-table counts still mark
    quality as poor, but replacing a 6+/7-column STEL/TWA grid with a
    PyMuPDF rebuild destroys cloned/spanned limit geometry that
    TableGoldGenerator._overlay_pdf_stel_twa is designed to fix.
    Collapsed tables (<6 columns, no limit headers, no data rows) are
    not repairable in place.
    """

    schema = _oel_schema_signals(table)

    if schema.get("physical_column_count", 0) < 6:
        return False

    if float(schema.get("header_score") or 0) < 0.30:
        return False

    has_limit_structure = bool(
        schema.get("has_both_limit_headers")
        or _needs_oel_limit_column_expansion(table)
        or _needs_oel_basis_symbols_expansion(table)
    )
    if not has_limit_structure:
        return False

    data_rows = table.rows[1:] if len(table.rows or []) > 1 else []
    if not data_rows:
        return False

    if not any(_row_non_empty_count(row) >= 2 for row in data_rows):
        return False

    nonempty = [
        cell
        for row in data_rows
        for cell in row
        if str(getattr(cell, "text", "") or "").strip()
    ]
    if not nonempty:
        return False
    boxed = sum(1 for cell in nonempty if is_valid_bbox(getattr(cell, "bbox", None)))
    # Overlay repairs cloned/spanned limit geometry; it cannot invent boxes.
    return boxed / len(nonempty) >= 0.5


def _needs_pymupdf_recovery(
    page_text: str,
    tables: list[ExtractedTableRecord],
) -> bool:
    """
    Determine whether PyMuPDF recovery is necessary.

    Only OEL tables can trigger OEL-specific recovery.
    Do not replace a usable DAI OEL grid solely because quality is poor.
    """

    if not tables:
        return False

    oel_tables = [
        table
        for table in tables
        if _is_chemical_oel_table(
            table,
            page_text,
        )
    ]

    if not oel_tables:
        return False

    return any(
        _document_ai_table_quality_poor(
            page_text,
            table,
        )
        and not _needs_oel_limit_column_expansion(
            table,
        )
        and not _needs_oel_basis_symbols_expansion(
            table,
        )
        and not _dai_oel_grid_repairable_in_place(
            table,
        )
        for table in oel_tables
    )


def _needs_oel_basis_symbols_expansion(
    table: ExtractedTableRecord,
) -> bool:
    schema = _oel_schema_signals(table)

    if schema.get("physical_column_count") != 6:
        return False

    if not schema.get(
        "twa_and_stel_are_distinct_columns",
    ):
        return False

    if not table.rows:
        return False

    first_header = next(
        (
            cell
            for cell in table.rows[0]
            if int(cell.column) == 0
        ),
        None,
    )

    if first_header is None:
        return False

    header_text = normalize_persian_text(
        first_header.text or "",
    ).normalized

    return (
        "مبنای" in header_text
        and "نماد" in header_text
    )


# ============================================================================
# RESULT
# ============================================================================

@dataclass
class StructuralResolverResult:

    tables: list[
        ExtractedTableRecord
    ] = field(
        default_factory=list
    )

    cells: list[
        ExtractedCellRecord
    ] = field(
        default_factory=list
    )

    page_detection: dict[
        int,
        dict[str, Any],
    ] = field(
        default_factory=dict
    )

    merged_cell_count: int = 0

    recovered_table_count: int = 0

    document_ai_table_count: int = 0

    logical_row_reconstruction_count: int = 0

    geometry_aligned_cell_count: int = 0

    # ------------------------------------------------------------
    # Number of additional physical rows created because PDF
    # geometry proved that one Document AI visual row contained
    # multiple CAS identities occupying different physical PDF rows.
    #
    # This is a geometry-driven counter. It must never be added to
    # logical_row_reconstruction_count (semantic/continuation-merge
    # counter) — see module docstring "Final architecture".
    # ------------------------------------------------------------

    multi_cas_visual_row_split_count: int = 0


# ============================================================================
# CELL REPLACEMENT
# ============================================================================

def _replace_table_cells(
    table: ExtractedTableRecord,
    replacement_by_id: dict[
        str,
        ExtractedCellRecord,
    ],
) -> ExtractedTableRecord:

    new_rows: list[
        list[ExtractedCellRecord]
    ] = []

    for row in table.rows:

        new_row: list[
            ExtractedCellRecord
        ] = []

        for cell in row:

            replacement = (
                replacement_by_id.get(
                    cell.cell_id
                )
            )

            if replacement is not None:
                new_row.append(
                    replacement
                )
            else:
                new_row.append(
                    cell
                )

        new_rows.append(
            new_row
        )

    # NOTE: raw_markdown is deliberately preserved as-is here. It stays
    # Document AI evidence; `rows` above is the Layer 2 corrected
    # structure. See module docstring "raw_markdown vs rows".
    return ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=new_rows,
        structural_confidence=(
            table.structural_confidence
        ),
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )


# ============================================================================
# GEOMETRY ALIGNMENT
# ============================================================================

def _align_evidence_cells(
    evidence_cells: list[
        ExtractedCellRecord
    ],
    pdf_path: Path,
) -> tuple[
    list[ExtractedCellRecord],
    int,
]:

    inputs: list[
        PromotionCellInput
    ] = []

    for cell in evidence_cells:

        # ------------------------------------------------------------
        # Existing valid Document AI bbox is immutable.
        # ------------------------------------------------------------

        if is_trusted_document_ai_bbox(
            cell.bbox,
            cell.bbox_source,
        ):
            continue

        da_bbox, bbox_provenance = (
            resolve_bbox_inputs(
                cell.bbox,
                cell.bbox_source,
            )
        )

        inputs.append(
            PromotionCellInput(
                page_number=cell.page_number,
                table_id=cell.table_id,
                cell_id=cell.cell_id,
                row_index=cell.row,
                column_index=cell.column,
                text=cell.text,
                document_ai_bbox=da_bbox,
                bbox_provenance=bbox_provenance,
                input_bbox_source=(
                    cell.bbox_source
                ),
                cell_source=cell.source,
            )
        )

    if not inputs:
        return (
            evidence_cells,
            0,
        )

    settings = get_settings()

    threshold = (
        settings.bbox_confidence_threshold
    )

    with (
        fitz.open(pdf_path) as doc,
        GeometryResolver(
            pdf_path,
            document_path=str(pdf_path),
        ) as resolver,
    ):

        page_numbers = {
            cell.page_number
            for cell in inputs
            if (
                1
                <= cell.page_number
                <= len(doc)
            )
        }

        page_cache = {
            page_number: doc[
                page_number - 1
            ]
            for page_number in page_numbers
        }

        def page_lookup(
            page_number: int,
        ) -> fitz.Page:

            return page_cache[
                page_number
            ]

        resolved = (
            resolve_cells_with_row_anchor_retry(
                resolver,
                inputs,
                threshold=threshold,
                page_lookup=page_lookup,
            )
        )

    output: list[
        ExtractedCellRecord
    ] = []

    aligned_count = 0

    for original in evidence_cells:

        geometry = resolved.get(
            original.cell_id
        )

        if geometry is None:

            output.append(
                original
            )

            continue

        if not is_valid_bbox(
            geometry.resolved_bbox
        ):

            output.append(
                original
            )

            continue

        if (
            geometry.bbox_source
            == BBOX_PROVENANCE_DOCUMENT_AI
        ):

            provenance = (
                BBOX_PROVENANCE_DOCUMENT_AI
            )

        else:

            provenance = (
                BBOX_PROVENANCE_PYMUPDF_ALIGNED
            )

        output.append(
            ExtractedCellRecord(
                cell_id=original.cell_id,
                table_id=original.table_id,
                page_number=original.page_number,
                row=original.row,
                column=original.column,
                text=original.text,
                bbox=geometry.resolved_bbox,
                confidence=original.confidence,
                bbox_confidence=(
                    geometry.bbox_confidence
                ),
                bbox_source=provenance,
                source=original.source,
                normalized_value=(
                    original.normalized_value
                ),
                source_reference={
                    **(
                        original.source_reference
                        or {}
                    ),
                    "bbox": (
                        geometry.resolved_bbox
                    ),
                    "bbox_source": provenance,
                    "alignment_layer": (
                        "structural_resolver"
                    ),
                    "geometry_resolved_pass": (
                        geometry.resolved_pass
                    ),
                },
            )
        )

        aligned_count += 1

    return (
        output,
        aligned_count,
    )


# ============================================================================
# OEL LIMIT COLUMN EXPANSION (6 physical -> 7 logical)
# ============================================================================

def _needs_oel_limit_column_expansion(
    table: ExtractedTableRecord,
) -> bool:
    """
    Detect the known six-column OEL layout where TWA and STEL/C share one
    physical header/limit column.
    """

    if not _is_oel_table_type(table):
        return False

    schema = _oel_schema_signals(table)

    if schema["physical_column_count"] != 6:
        return False

    if not schema["has_both_limit_headers"]:
        return False

    if schema["twa_and_stel_are_distinct_columns"]:
        return False

    merged_col = (
        schema["twa_physical_columns"][0]
        if schema["twa_physical_columns"]
        else None
    )

    if merged_col is None:
        return False

    molecular_weight_column: int | None = None

    for row in _table_header_rows(table):
        for cell in row:
            if MOLECULAR_WEIGHT_PATTERN.search(
                cell.text or "",
            ):
                molecular_weight_column = int(
                    cell.column,
                )

    return molecular_weight_column == merged_col + 1


def _split_merged_limit_header_text(
    text: str,
) -> tuple[str, str]:
    joined = (text or "").strip()

    if not joined:
        return "", ""

    if (
        TWA_PATTERN.search(joined)
        and STEL_PATTERN.search(joined)
    ):
        if (
            "حد مجاز" in joined
            or "مواجهه" in joined
        ):
            return (
                "حد مجاز مواجهه شغلی STEL/C",
                "TWA",
            )

        return "STEL/C", "TWA"

    return joined, ""


def _limit_substrings_from_merged_cell(
    text: str,
) -> tuple[str, str, bool]:
    """
    Split one merged exposure-limit cell into STEL/C and TWA substrings.

    Ordering follows the existing gold parser:
      - two regular values -> STEL then TWA
      - one regular value -> TWA
      - explicit C value -> STEL/C side
    """

    joined = (text or "").strip()

    if not joined:
        return "", "", False

    value_text = re.sub(
        r"m\s*\^\s*\{\s*3(?:\s*\([^)]*\))*\s*\}",
        "m³",
        joined,
        flags=re.IGNORECASE,
    )

    matches = list(
        EXPOSURE_VALUE_PATTERN.finditer(
            value_text,
        )
    )

    if not matches:
        needs_review = bool(
            LATEX_ARTIFACT_PATTERN.search(
                value_text,
            )
            or LIMIT_MARKERS.search(
                value_text,
            )
        )
        return joined, "", needs_review

    regular: list[str] = []
    ceiling: str | None = None

    for index, match in enumerate(matches):
        previous_end = (
            matches[index - 1].end()
            if index
            else 0
        )
        next_start = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(value_text)
        )
        prefix = value_text[
            previous_end : match.start()
        ]
        suffix = value_text[
            match.end() : next_start
        ]
        is_ceiling = bool(
            re.search(
                r"(?:^|[^A-Za-z])C\s*$",
                prefix,
                re.IGNORECASE,
            )
            or (
                index + 1 == len(matches)
                and re.match(
                    r"\s*C(?:[^A-Za-z]|$)",
                    suffix,
                    re.IGNORECASE,
                )
            )
        )
        segment = value_text[
            match.start() : match.end()
        ].strip()

        if is_ceiling:
            start = match.start()
            for pos in range(
                match.start() - 1,
                previous_end - 1,
                -1,
            ):
                if value_text[pos].upper() == "C":
                    start = pos
                    break
            ceiling = value_text[
                start : match.end()
            ].strip()
        else:
            regular.append(segment)

    if len(regular) >= 2:
        return regular[0], regular[1], False

    if ceiling and not regular:
        return ceiling, "", False

    if regular:
        return "", regular[0], False

    unit_occurrences = len(
        LIMIT_MARKERS.findall(value_text)
    )
    needs_review = bool(
        LATEX_ARTIFACT_PATTERN.search(
            value_text,
        )
    ) or unit_occurrences > len(matches)

    return joined, "", needs_review


def _row_looks_like_oel_data(
    by_col: dict[int, ExtractedCellRecord],
) -> bool:
    chemical_cell = by_col.get(4)

    if chemical_cell is None:
        return False

    text = chemical_cell.text or ""

    return bool(
        extract_cas_values(text)
        or ENGLISH_TOKEN_PATTERN.search(
            text,
        )
    )


def _clone_expanded_limit_cell(
    source: ExtractedCellRecord,
    *,
    table_id: str,
    row_index: int,
    column_index: int,
    text: str,
    limit_field: str,
    needs_review: bool,
) -> ExtractedCellRecord:
    normalized = (
        normalize_persian_text(text)
        if text
        else None
    )

    source_reference = {
        **(source.source_reference or {}),
        "limit_column_expansion": True,
        "source_merged_column": source.column,
        "source_cell_id": source.cell_id,
        "limit_field": limit_field,
    }

    if needs_review:
        source_reference["value_status"] = (
            "extraction_uncertain"
        )

    attach_bbox = bool(text)

    return ExtractedCellRecord(
        cell_id=_cell_id(
            table_id,
            row_index,
            column_index,
        ),
        table_id=table_id,
        page_number=source.page_number,
        row=row_index,
        column=column_index,
        text=text,
        bbox=source.bbox if attach_bbox else None,
        confidence=source.confidence,
        bbox_confidence=(
            source.bbox_confidence
            if attach_bbox
            else None
        ),
        bbox_source=(
            source.bbox_source
            if attach_bbox
            else None
        ),
        source=source.source,
        normalized_value=(
            normalized.normalized
            if normalized
            else None
        ),
        source_reference=source_reference,
    )


def _shift_expanded_cell(
    source: ExtractedCellRecord,
    *,
    row_index: int,
    column_index: int,
) -> ExtractedCellRecord:
    return ExtractedCellRecord(
        cell_id=_cell_id(
            source.table_id,
            row_index,
            column_index,
        ),
        table_id=source.table_id,
        page_number=source.page_number,
        row=row_index,
        column=column_index,
        text=source.text,
        bbox=source.bbox,
        confidence=source.confidence,
        bbox_confidence=source.bbox_confidence,
        bbox_source=source.bbox_source,
        source=source.source,
        normalized_value=source.normalized_value,
        source_reference={
            **(source.source_reference or {}),
            "limit_column_expansion_shift": True,
            "source_column": source.column,
        },
    )


def _expand_oel_limit_columns(
    table: ExtractedTableRecord,
) -> tuple[
    ExtractedTableRecord,
    int,
]:
    """
    Expand a six-column OEL table with a merged TWA/STEL limit column into
    seven logical physical columns.
    """

    if not _needs_oel_limit_column_expansion(
        table,
    ):
        return table, 0

    schema = _oel_schema_signals(table)
    merged_col = schema["twa_physical_columns"][0]

    new_rows: list[
        list[ExtractedCellRecord]
    ] = []

    for row_index, row in enumerate(
        table.rows,
    ):
        by_col = {
            int(cell.column): cell
            for cell in row
        }

        is_header = (
            row_index == 0
            or not _row_looks_like_oel_data(
                by_col,
            )
        )

        expanded_by_col: dict[
            int,
            ExtractedCellRecord,
        ] = {}

        for col, cell in sorted(
            by_col.items(),
        ):
            if col < merged_col:
                expanded_by_col[col] = cell
                continue

            if col == merged_col:
                if is_header:
                    stel_text, twa_text = (
                        _split_merged_limit_header_text(
                            cell.text or "",
                        )
                    )
                    needs_review = False
                else:
                    (
                        stel_text,
                        twa_text,
                        needs_review,
                    ) = _limit_substrings_from_merged_cell(
                        cell.text or "",
                    )

                expanded_by_col[
                    merged_col
                ] = _clone_expanded_limit_cell(
                    cell,
                    table_id=table.table_id,
                    row_index=row_index,
                    column_index=merged_col,
                    text=stel_text,
                    limit_field="STEL",
                    needs_review=needs_review,
                )
                expanded_by_col[
                    merged_col + 1
                ] = _clone_expanded_limit_cell(
                    cell,
                    table_id=table.table_id,
                    row_index=row_index,
                    column_index=merged_col + 1,
                    text=twa_text,
                    limit_field="TWA",
                    needs_review=needs_review,
                )
                continue

            expanded_by_col[
                col + 1
            ] = _shift_expanded_cell(
                cell,
                row_index=row_index,
                column_index=col + 1,
            )

        new_rows.append(
            [
                expanded_by_col[col]
                for col in sorted(
                    expanded_by_col,
                )
            ]
        )

    # NOTE: raw_markdown is intentionally left pointing at the original
    # Document AI evidence, not the expanded 7-column structure. See
    # module docstring "raw_markdown vs rows".
    expanded_table = ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=new_rows,
        structural_confidence=table.structural_confidence,
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )

    return expanded_table, 1


def _expand_oel_basis_symbols_column(
    table: ExtractedTableRecord,
) -> tuple[ExtractedTableRecord, int]:
    """
    Expand a six-column OEL table where column 0 merges basis + symbols.
    """

    if not _needs_oel_basis_symbols_expansion(
        table,
    ):
        return table, 0

    rows = table.rows
    new_rows: list[list[ExtractedCellRecord]] = []

    for row_index, row in enumerate(rows):
        by_col = {
            int(cell.column): cell
            for cell in row
        }

        first_cell = by_col.get(0)

        if first_cell is None:
            shifted = [
                _shift_expanded_cell(
                    cell,
                    row_index=row_index,
                    column_index=int(cell.column) + 1,
                )
                for cell in row
            ]
            new_rows.append(
                sorted(
                    shifted,
                    key=lambda cell: cell.column,
                )
            )
            continue

        raw_text = first_cell.text or ""

        if row_index == 0:
            basis_text = "مبنای تعیین حد مجاز مواجهه"
            symbols_text = "نمادها"
            basis_review = False
        else:
            normalized_text = normalize_persian_text(
                raw_text,
            ).normalized.strip()

            symbol_match = re.search(
                r"(?<!\w)(?:A[1-5]|B[1-2]|[123]A|[123]B)(?!\w)",
                normalized_text,
                flags=re.IGNORECASE,
            )

            if symbol_match:
                symbols_text = symbol_match.group(0).strip()
                basis_text = (
                    normalized_text[:symbol_match.start()]
                    + normalized_text[symbol_match.end():]
                ).strip()
                basis_text = re.sub(
                    r"^[؛;,\s]+|[؛;,\s]+$",
                    "",
                    basis_text,
                ).strip()
                basis_review = False
            else:
                basis_text = normalized_text
                symbols_text = ""
                basis_review = False

        basis_cell = _clone_expanded_basis_symbol_cell(
            first_cell,
            table_id=table.table_id,
            row_index=row_index,
            column_index=0,
            text=basis_text,
            field="basis",
            needs_review=basis_review,
        )
        symbols_cell = _clone_expanded_basis_symbol_cell(
            first_cell,
            table_id=table.table_id,
            row_index=row_index,
            column_index=1,
            text=symbols_text,
            field="symbols",
            needs_review=basis_review,
        )

        new_row: list[ExtractedCellRecord] = [
            basis_cell,
            symbols_cell,
        ]

        for col in sorted(by_col):
            if col == 0:
                continue
            source = by_col[col]
            new_row.append(
                _shift_expanded_cell(
                    source,
                    row_index=row_index,
                    column_index=col + 1,
                )
            )

        new_rows.append(
            sorted(
                new_row,
                key=lambda cell: cell.column,
            )
        )

    # NOTE: raw_markdown preserved as original Document AI evidence.
    expanded_table = ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=new_rows,
        structural_confidence=table.structural_confidence,
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )

    return expanded_table, 1


def _clone_expanded_basis_symbol_cell(
    source: ExtractedCellRecord,
    *,
    table_id: str,
    row_index: int,
    column_index: int,
    text: str,
    field: str,
    needs_review: bool,
) -> ExtractedCellRecord:
    normalized = (
        normalize_persian_text(text)
        if text
        else None
    )

    source_reference = {
        **(source.source_reference or {}),
        "basis_symbols_column_expansion": True,
        "source_merged_column": source.column,
        "source_cell_id": source.cell_id,
        "field": field,
    }

    if needs_review:
        source_reference["value_status"] = (
            "extraction_uncertain"
        )

    # Empty cells must not inherit bbox.
    has_text = bool(text.strip())

    return ExtractedCellRecord(
        cell_id=_cell_id(
            table_id,
            row_index,
            column_index,
        ),
        table_id=table_id,
        page_number=source.page_number,
        row=row_index,
        column=column_index,
        text=text,
        bbox=source.bbox if has_text else None,
        confidence=source.confidence,
        bbox_confidence=(
            source.bbox_confidence
            if has_text
            else None
        ),
        bbox_source=(
            source.bbox_source
            if has_text
            else None
        ),
        source=source.source,
        normalized_value=(
            normalized.normalized
            if normalized
            else None
        ),
        source_reference=source_reference,
    )




def _split_oel_multi_cas_rows(
    table: ExtractedTableRecord,
    pdf_page: Any,
) -> tuple[
    ExtractedTableRecord,
    int,
]:
    """
    Geometry-based correction of OEL visual rows.

    Responsibility:
        Split one Document AI visual row into multiple physical
        PDF rows only when multiple CAS identities are proven
        to occupy different Y positions in the source PDF.

    This function MUST NOT:
        - reconstruct logical continuation rows
        - infer numeric values
        - copy numeric values between rows
        - normalize OEL columns
        - split rows based only on text position

    This is the ONLY place in Layer 2 that is allowed to create new
    physical rows out of OEL table data, and it is only ever allowed to
    do so on PDF geometry evidence (see split_table_rows_by_cas_geometry
    in ingestion.merged_row_splitter). A raw CAS count is a trigger to
    *check* that geometry, never a trigger to split by itself.
    """

    if not _is_oel_table_type(table):
        return table, 0

    if not table.rows:
        return table, 0

    if pdf_page is None:
        logger.warning(
            "oel_multi_cas_split_skipped_no_pdf_page",
            table_id=str(table.table_id),
            page_number=table.page_number,
        )
        return table, 0

    # --------------------------------------------------------------
    # Extract physical CAS evidence from the PDF.
    # --------------------------------------------------------------

    try:
        cas_geometry = extract_cas_geometry(pdf_page)

    except Exception as exc:
        logger.warning(
            "oel_multi_cas_cas_geometry_failed",
            table_id=str(table.table_id),
            page_number=table.page_number,
            error=str(exc),
        )
        return table, 0

    if not cas_geometry:
        return table, 0

    # --------------------------------------------------------------
    # Split only when geometry proves that multiple CAS values
    # occupy different physical rows.
    # --------------------------------------------------------------

    try:
        corrected_rows, split_count = (
            split_table_rows_by_cas_geometry(
                table.rows,
                cas_geometry,
                y_threshold=8.0,
                # IMPORTANT: page must be forwarded here. Without it,
                # ingestion.merged_row_splitter's word-geometry lookup
                # (_group_words_by_physical_y) receives page=None and
                # immediately returns empty, so every non-chemical
                # column in a split row falls back to being cleared
                # instead of being assigned by PDF word geometry. That
                # was the actual cause of merged-row artifacts
                # surviving on pages like 47/49/51/52/55 even though
                # the splitter itself was correct.
                page=pdf_page,
            )
        )

    except Exception as exc:
        logger.warning(
            "oel_multi_cas_visual_split_failed",
            table_id=str(table.table_id),
            page_number=table.page_number,
            error=str(exc),
        )
        return table, 0

    if not corrected_rows:
        return table, 0

    # Always keep splitter output: PDF STEL/TWA/MW reconstruction
    # runs for unsplit rows as well. split_count only counts extra
    # physical rows created from multi-CAS geometry.

    final_rows: list[list[ExtractedCellRecord]] = []

    for row_index, row in enumerate(corrected_rows):

        final_row: list[ExtractedCellRecord] = []

        for column_index, cell in enumerate(row):

            new_cell = copy.deepcopy(cell)

            new_cell.row = row_index
            original_column = getattr(cell, "column", None)
            if original_column is not None:
                new_cell.column = int(original_column)
            else:
                new_cell.column = column_index

            previous_reference = (
                new_cell.source_reference or {}
            )

            new_cell.source_reference = {
                **previous_reference,
            }
            if split_count > 0:
                new_cell.source_reference.update(
                    {
                        "multi_cas_visual_row_split": True,
                        "split_basis": "pymupdf_cas_y_geometry",
                        "cas_y_threshold": 8.0,
                    }
                )

            final_row.append(new_cell)

        final_rows.append(final_row)

    # NOTE: raw_markdown preserved as original Document AI evidence —
    # it will not reflect this geometry-driven row split.
    corrected_table = ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=final_rows,
        structural_confidence=table.structural_confidence,
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )

    if split_count:
        logger.info(
            "oel_multi_cas_visual_rows_split",
            table_id=str(table.table_id),
            page_number=table.page_number,
            split_count=split_count,
            original_row_count=len(table.rows),
            corrected_row_count=len(final_rows),
        )

    return corrected_table, split_count

# ============================================================================
# LOGICAL ROW RECONSTRUCTION
# ============================================================================

_UNBRACKETED_CAS = re.compile(r"\b\d{2,7}-\d{2}-\d\b")


def _row_contains_cas(
    row: list[ExtractedCellRecord],
) -> bool:
    for cell in row:
        text = cell.text or ""
        if extract_cas_values(text):
            return True
        if _UNBRACKETED_CAS.search(text):
            return True
    return False


def _merge_visual_row_cells(
    upper: list[ExtractedCellRecord],
    lower: list[ExtractedCellRecord],
    row_index: int,
) -> list[ExtractedCellRecord]:
    """Join two visual fragments of one logical chemical. Source text only."""

    by_upper = {
        int(cell.column): cell
        for cell in upper
    }
    by_lower = {
        int(cell.column): cell
        for cell in lower
    }
    merged: list[ExtractedCellRecord] = []

    for column in sorted(set(by_upper) | set(by_lower)):
        left = by_upper.get(column)
        right = by_lower.get(column)

        if left is None:
            cell = copy.deepcopy(right)
        elif right is None:
            cell = copy.deepcopy(left)
        else:
            left_text = str(left.text or "").strip()
            right_text = str(right.text or "").strip()
            if not left_text:
                cell = copy.deepcopy(right)
            elif not right_text:
                cell = copy.deepcopy(left)
            elif left_text == right_text:
                cell = copy.deepcopy(right)
            elif _UNBRACKETED_CAS.search(left_text) or _UNBRACKETED_CAS.search(right_text):
                cell = copy.deepcopy(right)
                cell.text = f"{left_text} {right_text}".strip()
                cell.normalized_value = cell.text
            elif re.search(r"\d|[۰-۹٠-٩]|ppm|mg/m|\\cdot", left_text) and re.search(
                r"\d|[۰-۹٠-٩]|ppm|mg/m|\\cdot",
                right_text,
            ):
                # Limits/MW already present on the CAS-less name row.
                cell = copy.deepcopy(left)
            else:
                cell = copy.deepcopy(left)
                cell.text = f"{left_text} {right_text}".strip()
                cell.normalized_value = cell.text

        cell.row = row_index
        merged.append(cell)

    return merged


def _merge_casless_name_rows(
    table: ExtractedTableRecord,
) -> tuple[ExtractedTableRecord, int]:
    """
    Document AI often stores the Persian name + MW/limits on one visual
    row and the English name + CAS on the next. After geometry split,
    attach a CAS-less fragment to the immediately following CAS row.
    """

    if not table.rows or len(table.rows) < 2:
        return table, 0

    header = table.rows[0]
    body = table.rows[1:]
    new_body: list[list[ExtractedCellRecord]] = []
    merge_count = 0
    index = 0

    while index < len(body):
        if (
            index + 1 < len(body)
            and not _row_contains_cas(body[index])
            and _row_contains_cas(body[index + 1])
        ):
            new_body.append(
                _merge_visual_row_cells(
                    body[index],
                    body[index + 1],
                    0,
                )
            )
            merge_count += 1
            index += 2
            continue

        new_body.append(body[index])
        index += 1

    if merge_count == 0:
        return table, 0

    reindexed: list[list[ExtractedCellRecord]] = [header]
    for row_index, row in enumerate(new_body, start=1):
        new_row: list[ExtractedCellRecord] = []
        for cell in row:
            new_cell = copy.deepcopy(cell)
            new_cell.row = row_index
            new_row.append(new_cell)
        reindexed.append(new_row)

    return (
        ExtractedTableRecord(
            table_id=table.table_id,
            page_number=table.page_number,
            table_type=table.table_type,
            rows=reindexed,
            structural_confidence=table.structural_confidence,
            raw_markdown=table.raw_markdown,
            bbox=table.bbox,
        ),
        merge_count,
    )


def _reconstruct_oel_logical_rows(
    table: ExtractedTableRecord,
) -> tuple[
    ExtractedTableRecord,
    int,
]:
    """
    Reconstruct semantic/logical OEL rows from already corrected
    visual rows.

    IMPORTANT
    ---------
    Multi-CAS geometry splitting MUST already have happened
    (_split_oel_multi_cas_rows). This function operates purely on
    already-correct physical rows and performs a semantic operation:
    continuation-row merging. It never touches physical row boundaries
    and never re-derives them from a CAS count.

    This function:
        - reconstructs continuation rows
        - preserves source evidence
        - preserves geometry/provenance
        - does not infer numeric values
        - does not perform CAS-based physical splitting
        - does not expand OEL columns

    Returns:
        (reconstructed_table, reconstruction_count)
    """

    if not _is_oel_table_type(table):
        return table, 0

    if not table.rows:
        return table, 0

    # Document AI sometimes emits a fully blank visual row between
    # chemicals. Seven-column OEL tables skip the 6-column logical
    # merger, so those spacer rows would otherwise survive into Layer 2.
    nonempty_rows: list[list[ExtractedCellRecord]] = []
    for row_index, row in enumerate(table.rows):
        if row_index > 0:
            has_content = False
            for cell in row:
                if str(cell.text or "").strip() or str(
                    cell.normalized_value or ""
                ).strip():
                    has_content = True
                    break
            if not has_content:
                continue
        nonempty_rows.append(row)

    if len(nonempty_rows) != len(table.rows):
        reindexed_rows: list[list[ExtractedCellRecord]] = []
        for row_index, row in enumerate(nonempty_rows):
            new_row: list[ExtractedCellRecord] = []
            for cell in row:
                new_cell = copy.deepcopy(cell)
                new_cell.row = row_index
                new_row.append(new_cell)
            reindexed_rows.append(new_row)
        table = ExtractedTableRecord(
            table_id=table.table_id,
            page_number=table.page_number,
            table_type=table.table_type,
            rows=reindexed_rows,
            structural_confidence=table.structural_confidence,
            raw_markdown=table.raw_markdown,
            bbox=table.bbox,
        )

    table, casless_merge_count = _merge_casless_name_rows(table)

    from document_ai.logical_row_reconstructor import (
        reconstruct_logical_rows,
    )

    # --------------------------------------------------------------
    # Convert to lightweight logical-row representation.
    # --------------------------------------------------------------

    raw_rows: list[dict[str, Any]] = []

    for row in table.rows:

        raw_cells: list[dict[str, Any]] = []

        for cell in row:

            raw_cells.append(
                {
                    "blocks": (
                        [
                            {
                                "textBlock": {
                                    "text": cell.text or "",
                                }
                            }
                        ]
                        if cell.text
                        else []
                    ),
                    "rowSpan": getattr(
                        cell,
                        "row_span",
                        1,
                    ),
                    "columnSpan": getattr(
                        cell,
                        "column_span",
                        1,
                    ),
                }
            )

        raw_rows.append(
            {
                "cells": raw_cells,
            }
        )

    original_row_count = len(raw_rows)

    reconstructed_rows = reconstruct_logical_rows(
        raw_rows
    )

    reconstructed_row_count = len(
        reconstructed_rows
    )

    row_count_reduction = max(
        0,
        original_row_count
        - reconstructed_row_count,
    )

    # --------------------------------------------------------------
    # Detect actual textual structural change.
    # --------------------------------------------------------------

    def row_text(
        row: dict[str, Any],
    ) -> str:

        parts: list[str] = []

        for cell in row.get("cells") or []:

            for block in cell.get("blocks") or []:

                text_block = (
                    block.get("textBlock")
                    or {}
                )

                value = text_block.get(
                    "text",
                    "",
                )

                if value:
                    parts.append(str(value))

        return " ".join(parts).strip()

    original_text = [
        row_text(row)
        for row in raw_rows
    ]

    reconstructed_text = [
        row_text(row)
        for row in reconstructed_rows
    ]

    structural_changed = (
        original_text != reconstructed_text
    )

    if not structural_changed:
        return table, casless_merge_count

    # --------------------------------------------------------------
    # Reconstruction count.
    #
    # A reduction from N visual rows to M logical rows means
    # N-M continuation merges.
    #
    # If the representation changed without row reduction,
    # conservatively report one event.
    # --------------------------------------------------------------

    reconstruction_count = (
        row_count_reduction
        if row_count_reduction > 0
        else 1
    )

    # --------------------------------------------------------------
    # Convert reconstructed representation back to records.
    #
    # The mapping strategy MUST anchor CAS-bearing rows to their
    # original source row. CAS is used here purely as an identity
    # anchor for re-attaching provenance/evidence to the right row —
    # this is not a row-splitting decision, that already happened in
    # _split_oel_multi_cas_rows().
    # --------------------------------------------------------------

    original_rows = table.rows

    source_cas_rows: dict[
        str,
        list[ExtractedCellRecord],
    ] = {}

    for source_row in original_rows:

        for cell in source_row:

            for cas in extract_cas_values(
                cell.text or ""
            ):
                source_cas_rows.setdefault(
                    cas,
                    source_row,
                )

    # ----------------------------------------------------------------
    # Second-strongest identity anchor: the OEL "ردیف" row-number label
    # (physical column 0). This is used when a logical row carries no
    # CAS of its own (e.g. a continuation row) or its CAS couldn't be
    # matched. It preserves source/physical row identity instead of
    # falling straight to a positional guess, which silently drifts
    # once visual and logical row counts diverge — this was the root
    # cause behind "row 72 not found" on page 54.
    # ----------------------------------------------------------------

    source_by_row_number: dict[
        str,
        list[ExtractedCellRecord],
    ] = {}

    for source_row in original_rows:

        if not source_row:
            continue

        label_cell = next(
            (
                cell
                for cell in source_row
                if int(cell.column) == 0
            ),
            None,
        )

        if label_cell is None:
            continue

        label = _normalize_row_number_label(
            label_cell.text
        )

        if label is not None:
            source_by_row_number.setdefault(
                label,
                source_row,
            )

    def logical_cell_text(
        logical_cell: dict[str, Any],
    ) -> str:

        values: list[str] = []

        for block in (
            logical_cell.get("blocks") or []
        ):

            text_block = (
                block.get("textBlock")
                or {}
            )

            value = text_block.get(
                "text",
                "",
            )

            if value:
                values.append(str(value))

        return " ".join(values).strip()

    def logical_row_cas(
        logical_row: dict[str, Any],
    ) -> list[str]:

        return extract_cas_values(
            row_text(logical_row)
        )

    def clone_with_text(
        source_cell: ExtractedCellRecord | None,
        logical_cell: dict[str, Any],
        row_index: int,
        column_index: int,
    ) -> ExtractedCellRecord:

        text = logical_cell_text(
            logical_cell
        )

        if source_cell is not None:

            # Only text is replaced here. bbox, confidence, and
            # normalized_value are intentionally carried over
            # unchanged from the source cell — this function must
            # never guess or synthesize evidence for those fields.
            new_cell = copy.deepcopy(
                source_cell
            )

            new_cell.text = text

            new_cell.source_reference = {
                **(new_cell.source_reference or {}),
                "logical_row_reconstructed": True,
                "reconstruction_basis": (
                    "document_ai_logical_row_reconstructor"
                ),
            }

            return new_cell

        return ExtractedCellRecord(
            cell_id=_cell_id(
                table.table_id,
                row_index,
                column_index,
            ),
            table_id=table.table_id,
            row_index=row_index,
            column_index=column_index,
            text=text,
            normalized_text=None,
            bbox=None,
            confidence=None,
            row_span=logical_cell.get(
                "rowSpan",
                1,
            ),
            column_span=logical_cell.get(
                "columnSpan",
                1,
            ),
        )

    new_rows: list[
        list[ExtractedCellRecord]
    ] = []

    for new_row_index, logical_row in enumerate(
        reconstructed_rows
    ):

        logical_cells = (
            logical_row.get("cells") or []
        )

        source_row = None

        # ----------------------------------------------------------
        # CAS is the strongest identity anchor.
        # ----------------------------------------------------------

        cas_values = logical_row_cas(
            logical_row
        )

        if cas_values:

            for cas in cas_values:

                candidate = source_cas_rows.get(
                    cas
                )

                if candidate is not None:
                    source_row = candidate
                    break

        # ----------------------------------------------------------
        # Second-strongest anchor: the ردیف row-number label, when the
        # CAS anchor above found nothing.
        # ----------------------------------------------------------

        if source_row is None and logical_cells:

            row_number_label = (
                _normalize_row_number_label(
                    logical_cell_text(
                        logical_cells[0]
                    )
                )
            )

            if row_number_label is not None:

                source_row = (
                    source_by_row_number.get(
                        row_number_label
                    )
                )

        # ----------------------------------------------------------
        # Weakest fallback: same positional index. Only reached when
        # neither CAS nor the row-number label could anchor this
        # logical row to a specific source row.
        # ----------------------------------------------------------

        if source_row is None:

            if new_row_index < len(
                original_rows
            ):
                source_row = original_rows[
                    new_row_index
                ]

        converted_row: list[
            ExtractedCellRecord
        ] = []

        for column_index, logical_cell in enumerate(
            logical_cells
        ):

            source_cell = None

            if (
                source_row is not None
                and column_index < len(source_row)
            ):
                source_cell = source_row[
                    column_index
                ]

            converted_row.append(
                clone_with_text(
                    source_cell,
                    logical_cell,
                    new_row_index,
                    column_index,
                )
            )

        new_rows.append(
            converted_row
        )

    # NOTE: raw_markdown preserved as original Document AI evidence —
    # it will not reflect these logical continuation merges. See
    # module docstring "raw_markdown vs rows".
    reconstructed_table = ExtractedTableRecord(
        table_id=table.table_id,
        page_number=table.page_number,
        table_type=table.table_type,
        rows=new_rows,
        structural_confidence=table.structural_confidence,
        raw_markdown=table.raw_markdown,
        bbox=table.bbox,
    )

    return (
        reconstructed_table,
        reconstruction_count + casless_merge_count,
    )
# ============================================================================
# PYMUPDF RECOVERY
# ============================================================================

def _recovered_to_records(
    recovered: Any,
    table_id: str,
) -> tuple[
    ExtractedTableRecord,
    list[ExtractedCellRecord],
    int,
]:

    page_number = recovered.page_number

    row_records: list[
        list[ExtractedCellRecord]
    ] = []

    all_cells: list[
        ExtractedCellRecord
    ] = []

    merged_count = 0

    for row in recovered.rows:

        cell_row: list[
            ExtractedCellRecord
        ] = []

        for cell in row:

            merged = _is_merged_cell(
                cell.text
            )

            if merged:
                merged_count += 1

            normalized = (
                normalize_persian_text(
                    cell.text
                )
                if cell.text
                else None
            )

            cid = _cell_id(
                table_id,
                cell.row,
                cell.column,
            )

            record = ExtractedCellRecord(
                cell_id=cid,
                table_id=table_id,
                page_number=page_number,
                row=cell.row,
                column=cell.column,
                text=cell.text,
                bbox=cell.bbox,
                confidence=cell.confidence,
                bbox_confidence=(
                    cell.confidence
                    if cell.bbox
                    else None
                ),
                bbox_source=(
                    BBOX_PROVENANCE_PYMUPDF_RECOVERY
                    if cell.bbox
                    else None
                ),
                source="structural_resolver",
                normalized_value=(
                    normalized.normalized
                    if normalized
                    else None
                ),
                source_reference={
                    "page_number": page_number,
                    "cell_ids": [cid],
                    "bbox": cell.bbox,
                    "recovery_method": (
                        recovered.recovery_method
                    ),
                    "column_name": (
                        cell.column_name
                    ),
                    "value_status": (
                        "merged_cell"
                        if merged
                        else "extracted"
                    ),
                    "source_words": (
                        getattr(
                            cell,
                            "source_words",
                            None,
                        )
                        or []
                    ),
                    "alignment_layer": (
                        "structural_resolver_recovery"
                    ),
                },
            )

            cell_row.append(
                record
            )

            all_cells.append(
                record
            )

        row_records.append(
            cell_row
        )

    table_record = (
        ExtractedTableRecord(
            table_id=table_id,
            page_number=page_number,
            table_type=recovered.table_type,
            rows=row_records,
            structural_confidence=(
                recovered.structural_confidence
            ),
            raw_markdown=recovered.raw_markdown,
            bbox=recovered.bbox,
        )
    )

    return (
        table_record,
        all_cells,
        merged_count,
    )


def _apply_recovered_oel_structure(
    table: ExtractedTableRecord,
    pdf_page,
    page_text: str,
) -> tuple[ExtractedTableRecord, int]:
    """Run the same OEL geometry/numeric repair used for Document AI tables."""
    if pdf_page is None:
        return table, 0
    if not _is_chemical_oel_table(table, page_text):
        return table, 0
    table, split_count = _split_oel_multi_cas_rows(table, pdf_page)
    table, _ = _reconstruct_oel_logical_rows(table)
    table.rows = repair_oel_numeric_from_pdf_headers(table.rows, pdf_page)
    return table, split_count


# ============================================================================
# OEL DIAGNOSTIC ATTACHMENT
# ============================================================================

def _attach_oel_schema_diagnostics(
    detection: dict[str, Any],
    tables: list[ExtractedTableRecord],
    page_text: str,
) -> None:
    """
    Add oel_schema diagnostics to page detection.

    This is deliberately called BEFORE recovery/acceptance decisions.

    Therefore the JSON tells us:

        what Document AI saw
        what schema it appeared to have
        whether recovery was required
    """

    oel_tables = [
        table
        for table in tables
        if _is_chemical_oel_table(
            table,
            page_text,
        )
    ]

    if not oel_tables:
        detection[
            "oel_schema"
        ] = {
            "detected": False,
            "reason": (
                "No chemical OEL table "
                "identified in Document AI evidence."
            ),
        }

        return

    detection[
        "oel_schema"
    ] = {
        "detected": True,
        "table_count": len(
            oel_tables
        ),
        "tables": [
            {
                "table_id": str(
                    table.table_id
                ),
                "page_number": (
                    table.page_number
                ),
                "signals": (
                    _oel_schema_signals(
                        table
                    )
                ),
            }
            for table in oel_tables
        ],
    }


# ============================================================================
# MAIN RESOLVER
# ============================================================================

def resolve_structure(
    processed: ProcessedDocument,
    pdf_path: Path,
) -> StructuralResolverResult:
    """
    Run deterministic Layer 2 structural resolution.

    Processing order
    -----------------

    Layer 1
        Document AI evidence

    Layer 2
        1. Geometry alignment
        2. Page/table detection
        3. Visual row refinement
        4. OEL multi-CAS geometry-based row splitting
        5. OEL logical-row reconstruction
        6. OEL column expansion
        7. Merged-cell marking
        8. PyMuPDF recovery when Document AI structure is unreliable

    Important
    ---------

    _split_oel_multi_cas_rows() must run BEFORE
    _reconstruct_oel_logical_rows().

    The reason is that multi-CAS splitting is based on actual PDF
    geometry. Logical-row reconstruction should operate on the already
    corrected visual-row structure.

    No numeric values are invented by Layer 2.

    Metric ownership (do not blur these):

        multi_cas_visual_row_split_count  -> geometry split only
        logical_row_reconstruction_count  -> semantic merge + column
                                              expansion events only
    """

    result = StructuralResolverResult()

    evidence_tables_by_page: dict[
        int,
        list[ExtractedTableRecord],
    ] = {}

    # ========================================================================
    # 1. COLLECT DOCUMENT AI EVIDENCE
    # ========================================================================

    for table in processed.tables:

        if not table.rows:
            continue

        # --------------------------------------------------------------------
        # Do NOT require source == "document_ai".
        #
        # Older/current process_document.py output may contain:
        #
        #     source=None
        #
        # and this is still accepted as Layer 1 evidence.
        # --------------------------------------------------------------------

        if not all(
            _is_document_ai_cell(cell)
            for cell in table.flat_cells()
        ):
            logger.debug(
                "table_skipped_not_layer1_evidence",
                table_id=str(
                    table.table_id
                ),
                page_number=(
                    table.page_number
                ),
            )

            continue

        evidence_tables_by_page.setdefault(
            table.page_number,
            [],
        ).append(
            table
        )

        result.document_ai_table_count += 1

    # ========================================================================
    # 2. GEOMETRY ALIGNMENT
    # ========================================================================

    if processed.cells:

        (
            aligned_evidence,
            geometry_aligned_count,
        ) = _align_evidence_cells(
            processed.cells,
            pdf_path,
        )

        result.geometry_aligned_cell_count = (
            geometry_aligned_count
        )

    else:

        aligned_evidence = []

    aligned_by_id = {
        cell.cell_id: cell
        for cell in aligned_evidence
    }

    # ========================================================================
    # 3. OPEN PDF ONCE
    #
    # _split_oel_multi_cas_rows() needs the actual PyMuPDF page object.
    #
    # Do not repeatedly open/close the PDF inside the page loop.
    # ========================================================================

    with fitz.open(pdf_path) as pdf_doc:

        # ====================================================================
        # 4. PAGE-BY-PAGE STRUCTURAL RESOLUTION
        # ====================================================================

        for page in processed.pages:

            page_num = page.page_number

            page_text = page.text or ""

            page_tables = (
                evidence_tables_by_page.get(
                    page_num,
                    [],
                )
            )

            # ---------------------------------------------------------------
            # Resolve actual PDF page.
            #
            # ProcessedDocument page numbers are assumed 1-based.
            # ---------------------------------------------------------------

            pdf_page = None

            if (
                1
                <= page_num
                <= len(pdf_doc)
            ):
                pdf_page = pdf_doc[
                    page_num - 1
                ]

            is_oel_page = (
                looks_like_oel_table_page(
                    page_text,
                    page_num,
                )
            )

            detection = evaluate_table_detection(
                document_type=(
                    "chemical_oel_table"
                    if is_oel_page
                    else "unknown"
                ),
                page_text=page_text,
                document_ai_tables=[
                    table.to_dict()
                    for table in page_tables
                ],
            )

            # ----------------------------------------------------------------
            # ALWAYS emit OEL schema diagnostics.
            # ----------------------------------------------------------------

            _attach_oel_schema_diagnostics(
                detection,
                page_tables,
                page_text,
            )

            # =================================================================
            # 5. NO DOCUMENT AI TABLE
            # =================================================================

            if not page_tables:

                if detection.get(
                    "needs_recovery"
                ):

                    logger.info(
                        "pymupdf_recovery_triggered_no_document_ai_table",
                        page_number=page_num,
                    )

                    recovered_list = (
                        recover_tables_for_page(
                            pdf_path,
                            page_num,
                            page_text,
                        )
                    )

                    for recovery_index, recovered in enumerate(
                        recovered_list,
                        start=1,
                    ):

                        tid = _table_id(
                            page_num,
                            100 + recovery_index,
                        )

                        (
                            table_record,
                            new_cells,
                            merged,
                        ) = _recovered_to_records(
                            recovered,
                            tid,
                        )

                        (
                            table_record,
                            recovered_split_count,
                        ) = _apply_recovered_oel_structure(
                            table_record,
                            pdf_page,
                            page_text,
                        )
                        if recovered_split_count:
                            result.multi_cas_visual_row_split_count += (
                                recovered_split_count
                            )

                        result.tables.append(
                            table_record
                        )

                        result.cells.extend(
                            new_cells
                        )

                        result.merged_cell_count += (
                            merged
                        )

                        result.recovered_table_count += (
                            1
                        )

                    if recovered_list:

                        detection[
                            "table_detection_status"
                        ] = "recovered"

                        detection[
                            "recovery_table_count"
                        ] = len(
                            recovered_list
                        )

                    else:

                        detection[
                            "table_detection_status"
                        ] = (
                            "missed_no_recovery"
                        )

                else:

                    detection[
                        "table_detection_status"
                    ] = "not_detected"

                result.page_detection[
                    page_num
                ] = detection

                continue

            # =================================================================
            # 6. DOCUMENT AI TABLES EXIST
            # =================================================================

            page_needs_recovery = (
                _needs_pymupdf_recovery(
                    page_text,
                    page_tables,
                )
            )

            detection[
                "document_ai_table_count"
            ] = len(page_tables)

            detection[
                "needs_recovery"
            ] = page_needs_recovery

            # =================================================================
            # 7. DOCUMENT AI ACCEPTED
            # =================================================================

            if not page_needs_recovery:

                for table in page_tables:

                    # ========================================================
                    # 7.1 Geometry
                    # ========================================================

                    table = _replace_table_cells(
                        table,
                        aligned_by_id,
                    )

                    # ========================================================
                    # 7.2 Visual row refinement
                    #
                    # This separates rows based on visual bands.
                    # ========================================================

                    (
                        new_rows,
                        visual_split_count,
                    ) = refine_table_visual_rows(
                        table.table_id,
                        table.rows,
                    )

                    if visual_split_count:

                        logger.info(
                            "visual_row_band_split",
                            page_number=page_num,
                            table_id=(
                                str(
                                    table.table_id
                                )
                            ),
                            split_rows=(
                                visual_split_count
                            ),
                        )

                        table = (
                            ExtractedTableRecord(
                                table_id=(
                                    table.table_id
                                ),
                                page_number=(
                                    table.page_number
                                ),
                                table_type=(
                                    table.table_type
                                ),
                                rows=new_rows,
                                structural_confidence=(
                                    table.structural_confidence
                                ),
                                raw_markdown=(
                                    table.raw_markdown
                                ),
                                bbox=table.bbox,
                            )
                        )

                    # ========================================================
                    # 7.3 OEL STRUCTURAL CORRECTIONS
                    #
                    # IMPORTANT ORDER:
                    #
                    #     visual rows
                    #          ↓
                    #     multi-CAS geometry split      (physical rows)
                    #          ↓
                    #     logical-row reconstruction     (semantic merge)
                    #          ↓
                    #     column expansion
                    #
                    # _split_oel_multi_cas_rows() is deliberately here,
                    # BEFORE _reconstruct_oel_logical_rows().
                    # ========================================================

                    if (
                        pdf_page is not None
                        and _is_chemical_oel_table(
                            table,
                            page_text,
                        )
                    ):

                        # ----------------------------------------------------
                        # MULTI-CAS VISUAL ROW SPLIT (geometry)
                        # ----------------------------------------------------

                        (
                            table,
                            multi_cas_split_count,
                        ) = _split_oel_multi_cas_rows(
                            table,
                            pdf_page,
                        )

                        if multi_cas_split_count:

                            logger.info(
                                "oel_multi_cas_visual_row_split",
                                page_number=page_num,
                                table_id=(
                                    str(
                                        table.table_id
                                    )
                                ),
                                split_rows=(
                                    multi_cas_split_count
                                ),
                            )

                            # IMPORTANT: this is a geometry-driven
                            # physical-row split, not a logical/semantic
                            # reconstruction event. It accumulates into
                            # multi_cas_visual_row_split_count only —
                            # never into logical_row_reconstruction_count.
                            # Conflating the two breaks the audit trail
                            # described in the module docstring.
                            result.multi_cas_visual_row_split_count += (
                                multi_cas_split_count
                            )

                            detection[
                                "multi_cas_visual_row_split_count"
                            ] = (
                                detection.get(
                                    "multi_cas_visual_row_split_count",
                                    0,
                                )
                                + multi_cas_split_count
                            )

                    # ========================================================
                    # 7.4 OEL logical-row reconstruction (semantic)
                    #
                    # This now receives the already geometry-corrected
                    # multi-CAS structure.
                    # ========================================================

                    if _is_chemical_oel_table(
                        table,
                        page_text,
                    ):

                        (
                            table,
                            logical_count,
                        ) = (
                            _reconstruct_oel_logical_rows(
                                table
                            )
                        )

                        result.logical_row_reconstruction_count += (
                            logical_count
                        )

                        if logical_count:

                            detection[
                                "logical_row_reconstruction_count"
                            ] = (
                                detection.get(
                                    "logical_row_reconstruction_count",
                                    0,
                                )
                                + logical_count
                            )

                        # ====================================================
                        # 7.5 OEL 6 -> 7 limit-column expansion
                        # ====================================================

                        (
                            table,
                            expansion_count,
                        ) = _expand_oel_limit_columns(
                            table
                        )

                        if expansion_count:

                            detection[
                                "limit_column_expansion_count"
                            ] = (
                                detection.get(
                                    "limit_column_expansion_count",
                                    0,
                                )
                                + expansion_count
                            )

                        # ====================================================
                        # 7.6 OEL basis/symbols expansion
                        # ====================================================

                        (
                            table,
                            basis_symbols_expansion_count,
                        ) = _expand_oel_basis_symbols_column(
                            table
                        )

                        if basis_symbols_expansion_count:

                            detection[
                                "basis_symbols_column_expansion_count"
                            ] = (
                                detection.get(
                                    "basis_symbols_column_expansion_count",
                                    0,
                                )
                                + basis_symbols_expansion_count
                            )

                        result.logical_row_reconstruction_count += (
                            expansion_count
                            + basis_symbols_expansion_count
                        )

                        if pdf_page is not None:
                            table.rows = repair_oel_numeric_from_pdf_headers(
                                table.rows,
                                pdf_page,
                            )

                    # ========================================================
                    # 7.7 Mark merged cells
                    # ========================================================

                    merged = 0

                    for cell in table.flat_cells():

                        if _is_merged_cell(
                            cell.text,
                            getattr(
                                cell,
                                "row_span",
                                1,
                            ),
                            getattr(
                                cell,
                                "column_span",
                                1,
                            ),
                        ):

                            merged += 1

                            cell.source_reference = {
                                **(
                                    cell.source_reference
                                    or {}
                                ),
                                "value_status": (
                                    "merged_cell"
                                ),
                            }

                    result.merged_cell_count += (
                        merged
                    )

                    # ========================================================
                    # 7.8 Store final table
                    # ========================================================

                    result.tables.append(
                        table
                    )

                    result.cells.extend(
                        table.flat_cells()
                    )

                detection[
                    "table_detection_status"
                ] = "detected"

                detection[
                    "document_ai_structure"
                ] = "accepted"

                # ------------------------------------------------------------
                # Recalculate OEL schema AFTER all structural operations.
                #
                # This is important because the original Document AI table
                # may have had 6 columns while the final Layer 2 structure
                # has 7.
                # ------------------------------------------------------------

                final_oel_tables = [
                    table
                    for table in result.tables
                    if (
                        table.page_number
                        == page_num
                        and _is_chemical_oel_table(
                            table,
                            page_text,
                        )
                    )
                ]

                _attach_oel_schema_diagnostics(
                    detection,
                    final_oel_tables,
                    page_text,
                )

                result.page_detection[
                    page_num
                ] = detection

                continue

            # =================================================================
            # 8. DOCUMENT AI STRUCTURE POOR
            # =================================================================

            logger.info(
                "pymupdf_recovery_triggered",
                page_number=page_num,
                cas_count=_cas_count_in_page(
                    page_text
                ),
                document_ai_tables=len(
                    page_tables
                ),
            )

            detection[
                "needs_recovery"
            ] = True

            detection[
                "table_detection_status"
            ] = (
                "under_columned_or_corrupt"
            )

            recovered_list = (
                recover_tables_for_page(
                    pdf_path,
                    page_num,
                    page_text,
                )
            )

            if recovered_list:

                for recovery_index, recovered in enumerate(
                    recovered_list,
                    start=1,
                ):

                    tid = _table_id(
                        page_num,
                        100 + recovery_index,
                    )

                    (
                        table_record,
                        new_cells,
                        merged,
                    ) = _recovered_to_records(
                        recovered,
                        tid,
                    )

                    (
                        table_record,
                        recovered_split_count,
                    ) = _apply_recovered_oel_structure(
                        table_record,
                        pdf_page,
                        page_text,
                    )
                    if recovered_split_count:
                        result.multi_cas_visual_row_split_count += (
                            recovered_split_count
                        )

                    result.tables.append(
                        table_record
                    )

                    result.cells.extend(
                        new_cells
                    )

                    result.merged_cell_count += (
                        merged
                    )

                    result.recovered_table_count += (
                        1
                    )

                detection[
                    "table_detection_status"
                ] = "recovered"

                detection[
                    "recovery_method"
                ] = (
                    recovered_list[
                        0
                    ].recovery_method
                )

                detection[
                    "recovered_table_count"
                ] = len(
                    recovered_list
                )

                # ------------------------------------------------------------
                # Preserve original Document AI evidence.
                # ------------------------------------------------------------

                detection[
                    "document_ai_structure"
                ] = "recovery_recommended"

                detection[
                    "document_ai_evidence_preserved"
                ] = True

            else:

                # ------------------------------------------------------------
                # CRITICAL:
                #
                # Never discard Document AI evidence if recovery fails.
                # ------------------------------------------------------------

                logger.warning(
                    "pymupdf_recovery_failed_preserving_document_ai",
                    page_number=page_num,
                )

                detection[
                    "table_detection_status"
                ] = (
                    "recovery_failed_document_ai_preserved"
                )

                detection[
                    "document_ai_evidence_preserved"
                ] = True

                for table in page_tables:

                    table = _replace_table_cells(
                        table,
                        aligned_by_id,
                    )

                    result.tables.append(
                        table
                    )

                    result.cells.extend(
                        table.flat_cells()
                    )

            # ----------------------------------------------------------------
            # OEL diagnostics remain present after recovery decision.
            # ----------------------------------------------------------------

            _attach_oel_schema_diagnostics(
                detection,
                page_tables,
                page_text,
            )

            result.page_detection[
                page_num
            ] = detection

    # ========================================================================
    # 9. FINAL RESULT
    # ========================================================================

    return result


# ============================================================================
# WRITE VALIDATED STRUCTURE
# ============================================================================

def write_validated_structure(
    structural: StructuralResolverResult,
    processed: ProcessedDocument,
    *,
    start_page: int,
    end_page: int,
) -> Path:
    """
    Persist deterministic Layer 2 output.

    The generated JSON contains:

        page_detection
            └── oel_schema

    whenever an OEL table is detected.
    """

    settings = get_settings()

    path = (
        settings.data_intermediate_dir
        / (
            f"validated_structure_"
            f"{start_page}-{end_page}.json"
        )
    )

    payload = {
        "schema_version": "layer2_validated_structure_v2",

        "source_pdf": processed.source_pdf,

        "content_hash": (
            processed.content_hash
        ),

        "pages_range": {
            "start": start_page,
            "end": end_page,
        },

        "document_ai_table_count": (
            structural.document_ai_table_count
        ),

        "recovered_table_count": (
            structural.recovered_table_count
        ),

        "merged_cell_count": (
            structural.merged_cell_count
        ),

        "logical_row_reconstruction_count": (
            structural.logical_row_reconstruction_count
        ),
        "multi_cas_visual_row_split_count": (
            structural.multi_cas_visual_row_split_count
        ),

        "geometry_aligned_cell_count": (
            structural.geometry_aligned_cell_count
        ),

        "page_detection": (
            structural.page_detection
        ),

        "tables": [
            table.to_dict()
            for table in structural.tables
        ],

        "cells": [
            cell.to_dict()
            for cell in structural.cells
        ],
    }

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "wrote_validated_structure",
        path=str(path),
        tables=len(
            structural.tables
        ),
        cells=len(
            structural.cells
        ),
    )

    return path