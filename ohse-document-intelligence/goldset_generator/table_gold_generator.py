"""Table gold generation with chemical OEL column mapping."""

from __future__ import annotations

import re
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
        return header_mapping[column_index]
    resolved = resolve_field_name(column_index, column_name, header_mapping)
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


class TableGoldGenerator:
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

                val, unit = _parse_exposure_value(text)
                status = "extracted" if val else "extraction_uncertain"
                _set_field(
                    row_data,
                    field,
                    _field_payload(table, cell, field, value=val or text, unit=unit, value_status=status),
                )

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
