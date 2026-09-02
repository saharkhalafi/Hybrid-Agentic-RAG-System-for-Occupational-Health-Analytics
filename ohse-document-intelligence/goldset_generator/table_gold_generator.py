"""Table gold generation with chemical OEL column mapping."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from goldset_generator.chemical_entity_extractor import (
    ChemicalEntity,
    extract_chemical_entities_from_cell,
)
from goldset_generator.oel_row_parser import (
    STANDARD_OEL_COLUMN_MAP,
    enrich_oel_rows,
    parse_molecular_weight,
    resolve_field_name,
    split_chemical_segments,
)

from pipeline_contracts.header_reconstruction import reconstruct_header_structure
from pipeline_contracts.numeric_integrity import (
    extract_ceiling_from_stel_c_cell,
    parse_numeric_cell,
    validate_row_number_cell,
)
from pipeline_contracts.table_quality_gate import evaluate_table_quality

from goldset_generator.header_mapping import analyze_header_mapping
from goldset_generator.validator import GoldsetValidator

_EMPTY_LIMIT = frozenset({"", "-", "—", "–"})
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAX_PDF_ROW_BAND = 32.0
_PDF_Y_PAD = 5.0
_PDF_ROW_ANCHOR_Y_PAD = 8.0

CAS_PATTERN = re.compile(r"\[(\d{2,7}-\d{2}-\d)\s*\]")
CAS_STRIP = re.compile(r"\[\d{2,7}-\d{2}-\d\]")
UNIT_PATTERN = re.compile(r"\b(ppm|mg/m³|mg/m3|f/ml)\b", re.IGNORECASE)
NUMERIC_PATTERN = re.compile(r"[\d۰-۹]+(?:[./][\d۰-۹]+)?")
EXPOSURE_VALUE_PATTERN = re.compile(
    r"([\d۰-۹٠-٩]+(?:[./][\d۰-۹٠-٩]+)?)\s*(ppm|mg/m³|mg/m3|f/ml)\b",
    re.IGNORECASE,
)
LIMIT_MARKERS = re.compile(r"\b(ppm|mg/m³|mg/m3|TWA|STEL|Ceiling|C\b)\b", re.IGNORECASE)
# Document AI occasionally OCRs Persian decimal digits in the exposure-limit
# columns as LaTeX math commands (e.g. "\cdot", "\Delta", "\pi", "\tau",
# "\wedge") instead of real digit characters. This is a source-level OCR
# defect, not something we can safely reconstruct — but we must never treat
# it as "no value present" (which would silently drop the reading and skip
# human review). Detecting it lets us flag the field for mandatory review
# instead of losing the value.
LATEX_ARTIFACT_PATTERN = re.compile(r"\\[A-Za-z]+")
SYMBOL_TOKEN = re.compile(
    r"^(?:A[1-4]|DSEN|BEI|SKIN|STEL|TWA|CEILING|\(R\)|\(IFV\)|\(IV\)|پوست)(?:\s*[؛;]\s*(?:BEI|DSEN|A[1-4]|پوست))*$",
    re.IGNORECASE,
)
ROW_PERSIAN_PATTERN = re.compile(
    r"^([\d۰-۹]+)\s*[\n\s]+([\u0600-\u06FF][\u0600-\u06FF\s\-،0-9]+)$",
    re.DOTALL,
)

CHEMICAL_HEADER_MAP = {
    "chemical_name": ["نام علمی", "نام", "chemical", "material", "ماده شیمیایی"],
    "CAS": ["cas", "cas number", "شماره cas"],
    "molecular_weight": ["وزن ملکولی", "molecular weight", "mw"],
    "TWA": ["twa"],
    "STEL": ["stel", "stel/c"],
    "ceiling": ["ceiling", "c "],
    "symbols": ["نماد", "symbol", "a1", "a2", "skin", "bei", "dsen"],
    "health_effect": ["مبنای", "health", "effect", "تعیین حد"],
    "row_number": ["ردیف", "row", "no", "#"],
}


def _match_header(header_text: str) -> str | None:
    normalized = header_text.lower().strip()
    for field, keywords in CHEMICAL_HEADER_MAP.items():
        for keyword in keywords:
            if keyword in normalized:
                return field
    return None


def _build_header_mapping(header_row: list[dict[str, Any]], table_type: str) -> dict[int, str]:
    header_mapping: dict[int, str] = {}
    for cell in header_row:
        if not isinstance(cell, dict):
            continue
        field = _match_header(cell.get("text", ""))
        if field:
            header_mapping[cell.get("column", 0)] = field

    if table_type != "chemical_oel":
        return header_mapping

    cells_by_column = {
        int(cell.get("column", 0)): cell
        for cell in header_row
        if isinstance(cell, dict)
    }

    # The Persian OEL header often visually spans two subcolumns (STEL/C and
    # TWA), but Document AI returns one merged label. Determine whether the
    # evidence still contains two physical columns by locating the molecular
    # weight column immediately to its left.
    for column, cell in cells_by_column.items():
        header_text = str(cell.get("text") or "").lower()
        if "twa" not in header_text or "stel" not in header_text:
            continue
        molecular_weight_column = next(
            (
                other_column
                for other_column, other_cell in cells_by_column.items()
                if _match_header(str(other_cell.get("text") or "")) == "molecular_weight"
            ),
            None,
        )
        if molecular_weight_column == column + 2:
            # Two source columns: STEL/C first, then TWA.
            header_mapping[column] = "STEL"
            header_mapping[column + 1] = "TWA"
        else:
            # One physical source cell contains TWA, STEL and possibly C.
            header_mapping[column] = "TWA_STEL"

    # Only apply positional fallback when header detection found almost nothing.
    # Never invent phantom columns: merged Persian OEL headers often arrive as
    # just 2–3 physical cells (health/symbols block, name/weight block,
    # TWA+STEL/C sub-header). Blindly filling STANDARD_OEL_COLUMN_MAP created
    # duplicate TWA mappings at columns that do not exist in the evidence.
    if len(header_mapping) < 2:
        for col_idx in sorted(cells_by_column):
            if col_idx in STANDARD_OEL_COLUMN_MAP:
                header_mapping.setdefault(col_idx, STANDARD_OEL_COLUMN_MAP[col_idx])
    return header_mapping


def _is_merged_cell(cell: dict[str, Any]) -> bool:
    ref = cell.get("source_reference") or {}
    if ref.get("value_status") == "merged_cell":
        return True
    text = cell.get("text") or ""
    if CAS_PATTERN.search(text) and LIMIT_MARKERS.search(text):
        return True
    return len(CAS_PATTERN.findall(text)) > 1


def _clean_chemical_name(text: str) -> str:
    cleaned = CAS_STRIP.sub("", text)
    cleaned = UNIT_PATTERN.sub("", cleaned)
    cleaned = NUMERIC_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -/\n")
    eng = re.search(r"[A-Za-z][A-Za-z0-9\-\[\]α-ωΑ-Ω ]{2,}", cleaned)
    if eng:
        return eng.group().strip(" -")
    fa = re.search(r"[\u0600-\u06FF]{3,}", cleaned)
    return fa.group().strip() if fa else cleaned[:80]


def _parse_exposure_value(text: str) -> tuple[str | None, str | None]:
    if not text or not text.strip() or text.strip() in {"-", "—"}:
        return None, None
    # Normalize LaTeX-like cubic-metre notation before searching for a value.
    # Otherwise the exponent in a unit-only cell (e.g. ``mg/m^{3}``) is
    # incorrectly extracted as the exposure value 3.
    value_text = re.sub(
        r"m\s*\^\s*\{\s*3(?:\s*\([^)]*\))*\s*\}",
        "m³",
        text,
        flags=re.IGNORECASE,
    )
    unit_match = UNIT_PATTERN.search(value_text)
    unit = unit_match.group(1) if unit_match else None
    num_match = NUMERIC_PATTERN.search(value_text)
    if not num_match:
        return None, unit
    return num_match.group(0), unit


def _parse_combined_exposure_values(
    text: str,
) -> tuple[dict[str, tuple[str, str | None]], bool]:
    """Split one merged OEL cell into TWA, STEL and ceiling values.

    In the source layout, two unlabelled regular values are ordered STEL then
    TWA. A value explicitly adjacent to ``C`` is a ceiling value. A single
    unlabelled value is conservatively treated as TWA.

    Returns ``(parsed, has_unparsed_segment)``. ``has_unparsed_segment`` is
    True when the cell text contains evidence of an additional limit value
    (e.g. a second unit marker, or a LaTeX-style OCR artifact such as
    ``\\cdot``/``\\Delta``) that could not be resolved to a clean number. This
    tells the caller the missing field is genuinely present-but-unreadable
    (``extraction_uncertain``) rather than legitimately absent from the
    source table.
    """
    if not text or not text.strip():
        return {}, False
    value_text = re.sub(
        r"m\s*\^\s*\{\s*3(?:\s*\([^)]*\))*\s*\}",
        "m³",
        text,
        flags=re.IGNORECASE,
    )
    regular: list[tuple[str, str | None]] = []
    ceiling: tuple[str, str | None] | None = None

    matches = list(EXPOSURE_VALUE_PATTERN.finditer(value_text))
    for index, match in enumerate(matches):
        previous_end = matches[index - 1].end() if index else 0
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(value_text)
        prefix = value_text[previous_end : match.start()]
        suffix = value_text[match.end() : next_start]
        is_ceiling = bool(
            re.search(r"(?:^|[^A-Za-z])C\s*$", prefix, re.IGNORECASE)
            or (
                index + 1 == len(matches)
                and re.match(r"\s*C(?:[^A-Za-z]|$)", suffix, re.IGNORECASE)
            )
        )
        pair = (match.group(1), match.group(2))
        if is_ceiling:
            ceiling = pair
        else:
            regular.append(pair)

    parsed: dict[str, tuple[str, str | None]] = {}
    if len(regular) >= 2:
        parsed["STEL"] = regular[0]
        parsed["TWA"] = regular[1]
    elif regular:
        parsed["TWA"] = regular[0]
    if ceiling:
        parsed["ceiling"] = ceiling

    unit_occurrences = len(UNIT_PATTERN.findall(value_text))
    has_unparsed_segment = bool(LATEX_ARTIFACT_PATTERN.search(value_text)) or unit_occurrences > len(matches)
    return parsed, has_unparsed_segment


def _parse_limits(text: str) -> tuple[str | None, str | None]:
    """Parse numeric value and unit only. Does not assign STEL vs TWA vs MW."""
    return _parse_exposure_value(text)


def _looks_like_symbol(text: str) -> bool:
    compact = re.sub(r"\s+", " ", (text or "").strip())
    if not compact:
        return False
    if SYMBOL_TOKEN.match(compact):
        return True
    upper = compact.upper()
    return upper in {"DSEN", "BEI", "SKIN"} or bool(re.fullmatch(r"A[1-4](?:\s*[؛;]\s*A[1-4])?", upper))


def _field_from_cell(
    text: str,
    column_index: int,
    header_mapping: dict[int, str],
    *,
    column_name: str | None = None,
    use_content_heuristics: bool = True,
) -> str:
    if column_index in header_mapping:
        mapped = header_mapping[column_index]
        if mapped and not mapped.startswith("column_"):
            return mapped
    resolved = resolve_field_name(column_index, column_name, header_mapping)
    if resolved in {"STEL", "TWA", "TWA_STEL", "molecular_weight", "ceiling"}:
        return resolved
    if not use_content_heuristics or not text:
        return resolved
    if ROW_PERSIAN_PATTERN.match(text):
        return "row_number"
    if _looks_like_symbol(text):
        return "symbols"
    return resolved


def _should_keep_existing_field(existing: dict[str, Any] | None, new_status: str) -> bool:
    if not existing:
        return False
    if existing.get("value_status") == "merged_cell" and new_status != "extracted":
        return True
    if new_status == "absent":
        return True
    return False


def _set_field(row_data: dict[str, Any], field: str, payload: dict[str, Any]) -> None:
    if _should_keep_existing_field(row_data.get(field), payload.get("value_status", "")):
        return
    row_data[field] = payload


def _field_payload(
    table: dict[str, Any],
    cell: dict[str, Any],
    field: str,
    *,
    value: str | None = None,
    unit: str | None = None,
    value_status: str = "extracted",
    numeric_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "value": value,
        "unit": unit,
        "cell_id": cell.get("cell_id"),
        "bbox": cell.get("bbox"),
        "original_value": cell.get("text"),
        "normalized_value": cell.get("normalized_value"),
        "value_status": value_status,
        "source_reference": {
            "page_number": table.get("page_number"),
            "cell_ids": [cell.get("cell_id")] if cell.get("cell_id") else [],
            "bbox": cell.get("bbox"),
            "column_name": (cell.get("source_reference") or {}).get("column_name"),
            "source_words": (cell.get("source_reference") or {}).get("source_words") or [],
        },
    }
    if numeric_result:
        if isinstance(numeric_result, dict):
            payload["normalized_value"] = numeric_result.get("normalized_value")
            payload["numeric_parse_status"] = numeric_result.get("numeric_parse_status")
            payload["numeric_parse_method"] = numeric_result.get("numeric_parse_method")
            if value is None and numeric_result.get("normalized_value") is not None:
                payload["value"] = numeric_result.get("normalized_value")
        else:
            payload["normalized_value"] = numeric_result.normalized_value
            payload["numeric_parse_status"] = numeric_result.numeric_parse_status
            payload["numeric_parse_method"] = numeric_result.numeric_parse_method
            if numeric_result.unit:
                payload["unit"] = numeric_result.unit
            if value is None:
                if numeric_result.parsed_token is not None:
                    payload["value"] = numeric_result.parsed_token
                elif numeric_result.numeric_parse_status == "NOT_NUMERIC":
                    payload["value"] = cell.get("text")
    return payload


def _apply_parsed_segment(
    row_data: dict[str, Any],
    table: dict[str, Any],
    cell: dict[str, Any],
    parsed: dict[str, Any],
) -> None:
    _set_field(
        row_data,
        "chemical_name",
        _field_payload(table, cell, "chemical_name", value=parsed.get("chemical_name"), value_status="extracted"),
    )
    _set_field(
        row_data,
        "CAS",
        _field_payload(table, cell, "CAS", value=parsed.get("CAS"), value_status="extracted"),
    )
    if parsed.get("molecular_weight"):
        _set_field(
            row_data,
            "molecular_weight",
            _field_payload(
                table,
                cell,
                "molecular_weight",
                value=parsed.get("molecular_weight"),
                value_status="extracted",
            ),
        )
    for limit_field, unit_key in (("TWA", "TWA_unit"), ("STEL", "STEL_unit"), ("ceiling", "ceiling_unit")):
        if parsed.get(limit_field):
            _set_field(
                row_data,
                limit_field,
                _field_payload(
                    table,
                    cell,
                    limit_field,
                    value=parsed.get(limit_field),
                    unit=parsed.get(unit_key),
                    value_status="extracted",
                ),
            )
    if parsed.get("symbols"):
        _set_field(
            row_data,
            "symbols",
            _field_payload(table, cell, "symbols", value=parsed.get("symbols"), value_status="extracted"),
        )
    if parsed.get("health_effect"):
        _set_field(
            row_data,
            "health_effect",
            _field_payload(
                table,
                cell,
                "health_effect",
                value=parsed.get("health_effect"),
                value_status="extracted",
            ),
        )


def _apply_chemical_entity(
    row_data: dict[str, Any],
    table: dict[str, Any],
    cell: dict[str, Any],
    entity: ChemicalEntity,
) -> None:
    """Apply one semantic entity from a physical cell without splitting structure."""
    status = "review_required" if entity.review_required else "extracted"
    cell_id = cell.get("cell_id")
    _set_field(
        row_data,
        "chemical_name",
        _field_payload(
            table,
            cell,
            "chemical_name",
            value=entity.chemical_name,
            value_status=status,
        ),
    )
    if entity.persian_chemical_name:
        _set_field(
            row_data,
            "persian_chemical_name",
            _field_payload(
                table,
                cell,
                "persian_chemical_name",
                value=entity.persian_chemical_name,
                value_status=status,
            ),
        )
    row_data["cas_numbers"] = [c.to_dict(source_cell_id=cell_id) for c in entity.cas_numbers]
    if entity.primary_cas:
        _set_field(
            row_data,
            "CAS",
            _field_payload(
                table,
                cell,
                "CAS",
                value=entity.primary_cas,
                value_status=status,
            ),
        )


def _valid_xywh(bbox: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, dict):
        return None
    try:
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox.get("width") or 0)
        height = float(bbox.get("height") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _bbox_iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    if union <= 0:
        return 0.0
    return inter / union


def _bboxes_cloned(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    if all(abs(a[i] - b[i]) <= 1.0 for i in range(4)):
        return True
    return _bbox_iou(a, b) >= 0.8


def _bbox_spans_stel_and_twa(
    box: tuple[float, float, float, float],
    page_width: float,
) -> bool:
    """True when one physical cell covers both STEL (col 2) and TWA (col 3) x-bands."""
    from ingestion.table_recovery import _column_boundaries

    bounds = _column_boundaries(page_width)
    split_x = bounds[2]
    x, _, width, _ = box
    return x < split_x - 8.0 and (x + width) > split_x + 8.0


def _cells_by_column(row: list[Any]) -> dict[int, dict[str, Any]]:
    by_col: dict[int, dict[str, Any]] = {}
    for cell in row:
        if not isinstance(cell, dict) or cell.get("column") is None:
            continue
        by_col[int(cell["column"])] = cell
    return by_col


def _limit_text_unreadable(text: str) -> bool:
    compact = (text or "").strip()
    if not compact or compact in _EMPTY_LIMIT:
        return False
    if LATEX_ARTIFACT_PATTERN.search(compact):
        return True
    numeric = parse_numeric_cell(compact, field_type="STEL")
    if numeric.parsed_token:
        return False
    return bool(LIMIT_MARKERS.search(compact) or "~" in compact or "mg/m" in compact.lower())


def _physical_column_of_bbox(
    box: tuple[float, float, float, float],
    page_width: float,
) -> int:
    from ingestion.table_recovery import _assign_column_by_boundary

    x, _, width, _ = box
    return _assign_column_by_boundary(x + width / 2, page_width)


def _decimal_token(text: str | None, field_type: str) -> Any:
    compact = (text or "").strip()
    if not compact or compact in _EMPTY_LIMIT:
        return None
    numeric = parse_numeric_cell(compact, field_type=field_type)
    value = numeric.normalized_value if numeric.normalized_value is not None else numeric.parsed_token
    if value is None:
        return None
    try:
        from decimal import Decimal, InvalidOperation

        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)


def pdf_overrides_complete_dai_limits(
    dai_stel: Any,
    dai_twa: Any,
    pdf_stel: Any,
    pdf_twa: Any,
) -> bool:
    """True only when PDF x-bands are a column-swap of the same two DAI numbers.

    If Document AI already parsed both limits, incomplete or different PDF
    numbers must not replace them (cloned bboxes often sit in one column).
    """
    if dai_stel is None or dai_twa is None:
        return True
    if pdf_stel is None or pdf_twa is None:
        return False
    return {dai_stel, dai_twa} == {pdf_stel, pdf_twa} and (pdf_stel, pdf_twa) != (dai_stel, dai_twa)


def pdf_geometry_untrusted_fields(row: list[Any], page_width: float) -> set[str]:
    """STEL/TWA untrusted from limit-cell geometry (cloned, spanned, wrong x-band, OCR)."""
    by_col = _cells_by_column(row)
    cell_stel = by_col.get(2)
    cell_twa = by_col.get(3)
    text_stel = ((cell_stel or {}).get("text") or "").strip()
    text_twa = ((cell_twa or {}).get("text") or "").strip()
    box_stel = _valid_xywh((cell_stel or {}).get("bbox"))
    box_twa = _valid_xywh((cell_twa or {}).get("bbox"))
    empty_stel = text_stel in _EMPTY_LIMIT
    empty_twa = text_twa in _EMPTY_LIMIT
    fields: set[str] = set()

    if (
        box_stel
        and box_twa
        and not empty_stel
        and not empty_twa
        and _bboxes_cloned(box_stel, box_twa)
    ):
        fields.update({"STEL", "TWA"})

    if box_stel and _bbox_spans_stel_and_twa(box_stel, page_width):
        fields.update({"STEL", "TWA"})
    if box_twa and _bbox_spans_stel_and_twa(box_twa, page_width):
        fields.update({"STEL", "TWA"})

    if box_stel and _physical_column_of_bbox(box_stel, page_width) == 3:
        fields.update({"STEL", "TWA"})

    if _limit_text_unreadable(text_stel) or _limit_text_unreadable(text_twa):
        fields.update({"STEL", "TWA"})

    return fields


def pdf_identity_fill_fields(row: list[Any], page_width: float) -> set[str]:
    """Fill STEL/TWA from PDF only when this row's y-band identity can be proven."""
    by_col = _cells_by_column(row)
    cell_stel = by_col.get(2)
    cell_twa = by_col.get(3)
    text_stel = ((cell_stel or {}).get("text") or "").strip()
    text_twa = ((cell_twa or {}).get("text") or "").strip()
    box_stel = _valid_xywh((cell_stel or {}).get("bbox"))
    box_twa = _valid_xywh((cell_twa or {}).get("bbox"))
    empty_stel = text_stel in _EMPTY_LIMIT
    empty_twa = text_twa in _EMPTY_LIMIT
    fields: set[str] = set()
    if empty_stel and empty_twa:
        fields.update({"STEL", "TWA"})
    if box_stel and _physical_column_of_bbox(box_stel, page_width) == 4:
        fields.update({"STEL", "TWA"})
    if box_twa and _physical_column_of_bbox(box_twa, page_width) == 4:
        fields.update({"STEL", "TWA"})
    return fields


def pdf_limit_replace_fields(row: list[Any], page_width: float) -> set[str]:
    """Fields whose DAI STEL/TWA assignment is geometrically untrusted.

    Does not look at Gold. Triggers are physical: cloned merged-cell bboxes,
    a bbox that spans both limit columns, a STEL cell whose x-center sits in
    the TWA band, a limit bbox parked in the MW x-band, both limit cells empty,
    or unreadable OCR in a limit cell.
    """
    return pdf_geometry_untrusted_fields(row, page_width) | pdf_identity_fill_fields(
        row, page_width
    )


def pdf_row_identity_matches(
    words: list[Any],
    page_width: float,
    y_min: float,
    y_max: float,
    row_number: int | None,
) -> bool:
    """True when the y-band contains exactly this row's PDF row-number token."""
    from ingestion.table_recovery import _assign_column_by_boundary, _is_header_word

    if row_number is None:
        return False
    found: set[int] = set()
    for word in words:
        if not (y_min <= word.y_center <= y_max):
            continue
        if _is_header_word(word):
            continue
        if _assign_column_by_boundary(word.x_center, page_width) != 6:
            continue
        parsed = parse_numeric_cell(word.text, field_type="row_number")
        token = parsed.normalized_value or parsed.parsed_token
        if not token:
            continue
        try:
            found.add(int(float(token)))
        except (TypeError, ValueError):
            continue
    return found == {row_number}


def pdf_unique_row_number_y_span(
    words: list[Any],
    page_width: float,
    row_number: int | None,
) -> tuple[float, float] | None:
    """Tight y-span around the unique PDF row-number token in column 6.

    Used when the DAI-derived limit band sits on a neighbor row. Duplicate
    or missing row-number glyphs are refused so a neighbor is never guessed.
    """
    from ingestion.table_recovery import _assign_column_by_boundary, _is_header_word

    if row_number is None:
        return None
    matches: list[Any] = []
    for word in words:
        if _is_header_word(word):
            continue
        if _assign_column_by_boundary(word.x_center, page_width) != 6:
            continue
        parsed = parse_numeric_cell(word.text, field_type="row_number")
        token = parsed.normalized_value or parsed.parsed_token
        if not token:
            continue
        try:
            if int(float(token)) != row_number:
                continue
        except (TypeError, ValueError):
            continue
        matches.append(word)
    if len(matches) != 1:
        return None
    word = matches[0]
    y_min = word.y0 - _PDF_ROW_ANCHOR_Y_PAD
    y_max = word.y1 + _PDF_ROW_ANCHOR_Y_PAD
    if y_max - y_min > _MAX_PDF_ROW_BAND:
        return None
    return y_min, y_max


def _row_limit_y_band(row: list[Any], page_width: float) -> tuple[float, float] | None:
    """Vertical band for STEL/TWA words: limit cells, then MW/row-number x-bands."""
    from ingestion.table_recovery import _assign_column_by_boundary

    limit_spans: list[tuple[float, float]] = []
    mw_spans: list[tuple[float, float]] = []
    rownum_spans: list[tuple[float, float]] = []
    name_spans: list[tuple[float, float]] = []
    for cell in row:
        if not isinstance(cell, dict):
            continue
        box = _valid_xywh(cell.get("bbox"))
        if not box:
            continue
        x, y, width, height = box
        x_center = x + width / 2
        column = cell.get("column")
        geom_col = _assign_column_by_boundary(x_center, page_width)
        span = (y, y + height)
        if column in {2, 3}:
            limit_spans.append(span)
        elif column == 4 and geom_col == 4:
            mw_spans.append(span)
        elif column == 6 and geom_col == 6:
            rownum_spans.append(span)
        elif column == 5 and geom_col == 5:
            name_spans.append(span)
    use = limit_spans or mw_spans or rownum_spans or name_spans
    if not use:
        return None
    y_min = min(item[0] for item in use) - _PDF_Y_PAD
    y_max = max(item[1] for item in use) + _PDF_Y_PAD
    if y_max - y_min > _MAX_PDF_ROW_BAND:
        use = limit_spans or mw_spans or rownum_spans
        if not use:
            return None
        y_min = min(item[0] for item in use) - _PDF_Y_PAD
        y_max = max(item[1] for item in use) + _PDF_Y_PAD
    if y_max - y_min > _MAX_PDF_ROW_BAND:
        return None
    return y_min, y_max


class TableGoldGenerator:
    def __init__(self, pdf_path: Path | str | None = None) -> None:
        self._pdf_path = Path(pdf_path) if pdf_path else None
        self._pdf_doc: Any = None
        self._pdf_unavailable = False

    def _pdf_page(self, page_number: int) -> Any:
        if self._pdf_unavailable or not page_number:
            return None
        if self._pdf_path is None:
            candidate = _REPO_ROOT / "OHE6.pdf"
            if not candidate.exists():
                self._pdf_unavailable = True
                return None
            self._pdf_path = candidate
        try:
            import fitz
        except ImportError:
            self._pdf_unavailable = True
            return None
        try:
            if self._pdf_doc is None:
                self._pdf_doc = fitz.open(self._pdf_path)
            if page_number < 1 or page_number > len(self._pdf_doc):
                return None
            return self._pdf_doc[page_number - 1]
        except Exception:
            self._pdf_unavailable = True
            return None

    def _overlay_pdf_stel_twa(
        self,
        row_data: dict[str, Any],
        table: dict[str, Any],
        row: list[Any],
    ) -> None:
        page = self._pdf_page(int(table.get("page_number") or 0))
        if page is None:
            return
        stel_meta = row_data.get("STEL") or {}
        twa_meta = row_data.get("TWA") or {}
        if stel_meta.get("cell_id") and stel_meta.get("cell_id") == twa_meta.get("cell_id"):
            return
        page_width = float(page.rect.width)
        geometry_replace = pdf_geometry_untrusted_fields(row, page_width)
        identity_replace = pdf_identity_fill_fields(row, page_width)
        replace = geometry_replace | identity_replace
        if not replace:
            return
        from ingestion.table_recovery import WordToken, recover_stel_twa_from_words

        page_number = int(table.get("page_number") or 0)
        words = [
            WordToken(
                text=str(item[4]),
                x0=float(item[0]),
                y0=float(item[1]),
                x1=float(item[2]),
                y1=float(item[3]),
                page_number=page_number,
            )
            for item in page.get_text("words")
            if len(item) >= 5
        ]
        row_no_meta = row_data.get("row_number") or {}
        row_no_token = row_no_meta.get("value") or row_no_meta.get("original_value")
        row_no: int | None = None
        if row_no_token is not None:
            try:
                row_no = int(float(str(row_no_token).strip().replace(",", "")))
            except (TypeError, ValueError):
                row_no = None
        dai_stel = _decimal_token(
            (row_data.get("STEL") or {}).get("value")
            or (row_data.get("STEL") or {}).get("original_value"),
            "STEL",
        )
        dai_twa = _decimal_token(
            (row_data.get("TWA") or {}).get("value")
            or (row_data.get("TWA") or {}).get("original_value"),
            "TWA",
        )
        dai_complete = dai_stel is not None and dai_twa is not None
        band = _row_limit_y_band(row, page_width)
        identity_ok = bool(band) and pdf_row_identity_matches(
            words, page_width, band[0], band[1], row_no
        )
        if not identity_ok:
            anchored = pdf_unique_row_number_y_span(words, page_width, row_no)
            if anchored is not None and (identity_replace or not dai_complete):
                band = anchored
            elif band is None or not geometry_replace or dai_complete:
                return
            else:
                replace = geometry_replace
        recovered = recover_stel_twa_from_words(words, page_width, band[0], band[1])
        pdf_stel = _decimal_token(recovered.get("STEL"), "STEL")
        pdf_twa = _decimal_token(recovered.get("TWA"), "TWA")
        if dai_complete:
            if not pdf_overrides_complete_dai_limits(dai_stel, dai_twa, pdf_stel, pdf_twa):
                return
        for field in ("STEL", "TWA"):
            if field not in replace:
                continue
            raw = recovered.get(field)
            if raw is None:
                continue
            source_cell = {
                "text": raw,
                "bbox": None,
                "cell_id": None,
                "source_reference": {"geometry_source": "pymupdf_column_band"},
            }
            if raw.strip() in _EMPTY_LIMIT:
                row_data[field] = _field_payload(
                    table,
                    source_cell,
                    field,
                    value=None,
                    value_status="absent",
                )
                continue
            numeric = parse_numeric_cell(raw, field_type=field)
            if numeric.parsed_token:
                row_data[field] = _field_payload(
                    table,
                    source_cell,
                    field,
                    value=numeric.parsed_token,
                    unit=numeric.unit,
                    value_status="extracted",
                    numeric_result=numeric,
                )
            else:
                row_data[field] = _field_payload(
                    table,
                    source_cell,
                    field,
                    value=None,
                    value_status="absent" if not raw.strip() else "extraction_uncertain",
                )

    def generate(self, table: dict[str, Any]) -> dict[str, Any]:
        rows = table.get("rows") or []
        if not rows:
            return self._empty_table_gold(table)

        table_type = table.get("table_type", "")
        structure = reconstruct_header_structure(rows, table_type=table_type)
        header_mapping = structure.header_mapping
        header_rows = [rows[i] for i in structure.header_row_indices if i < len(rows)]
        header_row = header_rows[0] if header_rows else rows[0]
        data_rows = rows[structure.data_row_start :]

        gold_rows: list[dict[str, Any]] = []
        numeric_issues: list[dict[str, Any]] = []

        for row in data_rows:
            if not row:
                continue
            row_data: dict[str, Any] = {}
            for cell in row:
                if not isinstance(cell, dict):
                    continue
                text = (cell.get("text") or "").strip()
                col = cell.get("column", 0)
                column_name = (cell.get("source_reference") or {}).get("column_name")
                field = _field_from_cell(
                    text,
                    col,
                    header_mapping,
                    column_name=column_name,
                    use_content_heuristics=col not in header_mapping,
                )

                if _is_merged_cell(cell):
                    entities = extract_chemical_entities_from_cell(cell)
                    if len(entities) == 1:
                        _apply_chemical_entity(row_data, table, cell, entities[0])
                        continue
                    if len(entities) > 1:
                        _set_field(
                            row_data,
                            field,
                            _field_payload(table, cell, field, value=None, value_status="merged_cell"),
                        )
                        continue
                    segments = split_chemical_segments(text)
                    if len(segments) == 1:
                        _apply_parsed_segment(row_data, table, cell, segments[0])
                    else:
                        _set_field(
                            row_data,
                            field,
                            _field_payload(table, cell, field, value=None, value_status="merged_cell"),
                        )
                    continue

                if field == "TWA_STEL":
                    # Merged exposure cell: positional STEL-then-TWA is the fallback
                    # because column identity cannot separate the two limits.
                    parsed_limits, has_unparsed_segment = _parse_combined_exposure_values(text)
                    for limit_field in ("TWA", "STEL"):
                        value, unit = parsed_limits.get(limit_field, (None, None))
                        if value:
                            numeric = parse_numeric_cell(str(value), field_type=limit_field)
                            value = numeric.parsed_token or value
                            status = "extracted"
                            num_meta = numeric
                        elif not text:
                            status = "absent"
                            num_meta = None
                        elif has_unparsed_segment:
                            status = "extraction_uncertain"
                            num_meta = None
                        else:
                            status = "absent" if parsed_limits else "extraction_uncertain"
                            num_meta = None
                        _set_field(
                            row_data,
                            limit_field,
                            _field_payload(
                                table,
                                cell,
                                limit_field,
                                value=value,
                                unit=unit,
                                value_status=status,
                                numeric_result=num_meta,
                            ),
                        )
                    if "ceiling" in parsed_limits:
                        value, unit = parsed_limits["ceiling"]
                        ceil_numeric = parse_numeric_cell(str(value), field_type="ceiling")
                        _set_field(
                            row_data,
                            "ceiling",
                            _field_payload(
                                table,
                                cell,
                                "ceiling",
                                value=value,
                                unit=unit,
                                value_status="extracted",
                                numeric_result=ceil_numeric,
                            ),
                        )
                    continue

                if not text or text == "-":
                    _set_field(
                        row_data,
                        field,
                        _field_payload(table, cell, field, value=None, value_status="absent"),
                    )
                    continue

                row_persian = ROW_PERSIAN_PATTERN.match(text)
                if row_persian:
                    row_num, persian_name = row_persian.groups()
                    lines = [line.strip() for line in persian_name.split("\n") if line.strip()]
                    name_lines: list[str] = []
                    for line in lines:
                        if line.startswith(")"):
                            break
                        name_lines.append(line)
                    persian_clean = re.sub(r"\s+", " ", " ".join(name_lines)).strip("() ")
                    rn_numeric = parse_numeric_cell(row_num, field_type="row_number")
                    _set_field(
                        row_data,
                        "row_number",
                        _field_payload(
                            table,
                            cell,
                            "row_number",
                            value=rn_numeric.parsed_token or row_num.strip(),
                            value_status="extracted",
                            numeric_result=rn_numeric,
                        ),
                    )
                    _set_field(
                        row_data,
                        "persian_chemical_name",
                        _field_payload(
                            table,
                            cell,
                            "persian_chemical_name",
                            value=persian_clean,
                            value_status="extracted",
                        ),
                    )
                    continue

                cas = CAS_PATTERN.search(text)
                if field == "CAS" and cas:
                    _set_field(
                        row_data,
                        "CAS",
                        _field_payload(table, cell, "CAS", value=cas.group(1), value_status="extracted"),
                    )
                    continue

                if field == "chemical_name":
                    segments = split_chemical_segments(text)
                    if segments:
                        _apply_parsed_segment(row_data, table, cell, segments[0])
                    else:
                        _set_field(
                            row_data,
                            "CAS",
                            _field_payload(
                                table,
                                cell,
                                "CAS",
                                value=cas.group(1) if cas else None,
                                value_status="extracted" if cas else "absent",
                            ),
                        )
                        _set_field(
                            row_data,
                            "chemical_name",
                            _field_payload(
                                table,
                                cell,
                                "chemical_name",
                                value=_clean_chemical_name(text),
                                value_status="extracted",
                            ),
                        )
                    continue

                if field == "molecular_weight":
                    numeric = parse_numeric_cell(text, field_type="molecular_weight")
                    status = (
                        "extracted"
                        if numeric.parsed_token
                        else ("absent" if not text else "extraction_uncertain")
                    )
                    _set_field(
                        row_data,
                        field,
                        _field_payload(
                            table,
                            cell,
                            field,
                            value=numeric.parsed_token,
                            value_status=status,
                            numeric_result=numeric,
                        ),
                    )
                    continue

                if field in ("TWA", "STEL", "ceiling"):
                    numeric = parse_numeric_cell(text, field_type=field)
                    if numeric.numeric_parse_status == "NOT_NUMERIC":
                        status = "extraction_uncertain" if text.strip() not in {"", "-", "—", "–"} else "absent"
                        parsed_value = text if status == "extraction_uncertain" else None
                    elif numeric.parsed_token:
                        status = "extracted"
                        parsed_value = numeric.parsed_token
                    else:
                        status = "absent"
                        parsed_value = None
                    _set_field(
                        row_data,
                        field,
                        _field_payload(
                            table,
                            cell,
                            field,
                            value=parsed_value,
                            unit=numeric.unit,
                            value_status=status,
                            numeric_result=numeric,
                        ),
                    )
                    if field == "STEL":
                        ceiling_text = extract_ceiling_from_stel_c_cell(text)
                        if ceiling_text and not row_data.get("ceiling", {}).get("value"):
                            ceil_numeric = parse_numeric_cell(text, field_type="ceiling")
                            _set_field(
                                row_data,
                                "ceiling",
                                _field_payload(
                                    table,
                                    cell,
                                    "ceiling",
                                    value=ceiling_text,
                                    unit=ceil_numeric.unit,
                                    value_status="extracted",
                                    numeric_result=ceil_numeric,
                                ),
                            )
                    continue

                if field == "row_number":
                    for msg in validate_row_number_cell(text):
                        numeric_issues.append(
                            {
                                "type": "ROW_NUMBER_MULTI_VALUE",
                                "severity": "critical",
                                "message": msg,
                                "field": "row_number",
                            }
                        )
                    numeric = parse_numeric_cell(text, field_type="row_number")
                    status = "extracted" if numeric.parsed_token else "extraction_uncertain"
                    _set_field(
                        row_data,
                        field,
                        _field_payload(
                            table,
                            cell,
                            field,
                            value=numeric.parsed_token,
                            value_status=status,
                            numeric_result=numeric,
                        ),
                    )
                    continue

                if field in ("symbols", "health_effect", "exposure_basis"):
                    status = "extracted" if text else "absent"
                    _set_field(
                        row_data,
                        field,
                        _field_payload(table, cell, field, value=text, value_status=status),
                    )
                    continue

                val, unit = _parse_limits(text)
                status = "extracted" if val else "extraction_uncertain"
                _set_field(
                    row_data,
                    field,
                    _field_payload(table, cell, field, value=val or text, unit=unit, value_status=status),
                )

            if row_data and table.get("table_type") == "chemical_oel":
                self._overlay_pdf_stel_twa(row_data, table, row)

            if row_data:
                gold_rows.append(row_data)

        mapping_meta = analyze_header_mapping(
            header_mapping,
            physical_column_count=structure.physical_column_count,
            structure_issues=structure.issues,
        )

        if table.get("table_type") == "chemical_oel":
            gold_rows = enrich_oel_rows(gold_rows)

        result = {
            "table_id": table.get("table_id"),
            "page_number": table.get("page_number"),
            "table_type": table.get("table_type"),
            "headers": [cell.get("text") for cell in header_row if isinstance(cell, dict)],
            "physical_columns": [col.to_dict() for col in structure.physical_columns],
            "header_row_indices": structure.header_row_indices,
            "data_row_start": structure.data_row_start,
            **mapping_meta,
            "rows": gold_rows,
            "structural_confidence": table.get("structural_confidence"),
            "bbox": table.get("bbox"),
            "structure_issues": structure.issues,
            "review_status": "pending"
            if mapping_meta.get("mapping_status") == "review_required"
            else "pending",
        }

        evidence_cells = [c for row in rows for c in row if isinstance(c, dict)]
        field_validator = GoldsetValidator(evidence_cells)
        for row in gold_rows:
            for field_name, field_data in row.items():
                if not isinstance(field_data, dict):
                    continue
                for issue in field_validator.validate_table_field(field_name, field_data):
                    if "CRITICAL" in issue or "NUMERIC_VALUE_MISMATCH" in issue:
                        numeric_issues.append(
                            {
                                "type": "NUMERIC_VALUE_MISMATCH",
                                "severity": "critical",
                                "message": issue,
                                "field": field_name,
                            }
                        )

        quality = evaluate_table_quality(
            header_structure=structure,
            table_gold=result,
            numeric_issues=numeric_issues,
        )
        result["table_quality"] = quality.to_dict()
        result["gold_allowed"] = quality.gold_allowed
        if not quality.gold_allowed:
            result["review_status"] = "review_required"
            result["mapping_status"] = "review_required"

        return result

    def _empty_table_gold(self, table: dict[str, Any]) -> dict[str, Any]:
        return {
            "table_id": table.get("table_id"),
            "page_number": table.get("page_number"),
            "table_type": table.get("table_type"),
            "headers": [],
            "header_mapping": {},
            "rows": [],
            "structural_confidence": table.get("structural_confidence"),
        }
