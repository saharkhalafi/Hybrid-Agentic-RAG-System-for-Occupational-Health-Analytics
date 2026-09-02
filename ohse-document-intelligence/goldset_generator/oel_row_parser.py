"""Deterministic parser for chemical OEL table rows and merged OCR cells."""

from __future__ import annotations

import re
from typing import Any

PERSIAN_DIGIT = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
CAS_PATTERN = re.compile(r"\[(\d{2,7}-\d{2}-\d)\s*\]")
CAS_STRIP = re.compile(r"\[\d{2,7}-\d{2}-\d\s*\]")
ENGLISH_NAME_PATTERN = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9\-,./\s]{2,}?)\s*/?\s*\[(\d{2,7}-\d{2}-\d)\s*\]"
)
MOL_WEIGHT_PATTERN = re.compile(r"(\d{1,3})\s*/\s*(\d{2,4})")
EXPOSURE_PATTERN = re.compile(
    r"([\d۰-۹]+(?:[./][\d۰-۹]+)?)\s*(ppm|mg/m³|mg/m3|f/ml)\b",
    re.IGNORECASE,
)
CEILING_PATTERN = re.compile(
    r"(?:C\s*=|Ceiling\s*=)\s*([\d./۰-۹]+)\s*(ppm|mg/m³|mg/m3)?",
    re.IGNORECASE,
)
SYMBOL_PATTERN = re.compile(
    r"پوست|SKIN|BEI|DSEN|RSEN|OTO|\bA[1-4]\b|\(R\)|\(IFV\)|\(IV\)",
    re.IGNORECASE,
)
ASPHYXIANT_PATTERN = re.compile(r"خفگی\s*آور\s*ساده\s*\(?\s*D\s*\)?", re.IGNORECASE)
HEALTH_KEYWORDS = ("تحریک", "ادم", "سقط", "خفگی", "سوزش", "آسیب", "سرطان", "تنفس", "جنین")
EMPTY_LIMIT = frozenset({"", "-", "—", "–", "null", "none"})

STANDARD_OEL_COLUMN_MAP: dict[int, str] = {
    0: "health_effect",
    1: "symbols",
    2: "STEL",
    3: "TWA",
    4: "molecular_weight",
    5: "chemical_name",
    6: "row_number",
}


def _normalize_digits(text: str) -> str:
    return (text or "").translate(PERSIAN_DIGIT)


def _looks_like_health_effect(text: str | None) -> bool:
    if not text:
        return False
    return any(keyword in text for keyword in HEALTH_KEYWORDS)


def _looks_like_persian_name(text: str | None) -> bool:
    if not text or not re.search(r"[\u0600-\u06FF]{3,}", text or ""):
        return False
    if _looks_like_health_effect(text) and not re.search(r"اتان|فلورن|آسپیرین|آکرول", text):
        return False
    return True


def _normalize_persian_name_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip("() \n،؛")).strip()


def _persian_name_from_paren_line(line: str) -> str | None:
    if not line.startswith(")"):
        return None
    persian = _normalize_persian_name_line(line.lstrip(")"))
    if "(" in persian and not persian.endswith(")"):
        persian = f"{persian})"
    if len(persian) < 4:
        return None
    return persian if _looks_like_persian_name(persian) else persian


def extract_persian_names_from_row_number(text: str | None) -> list[str]:
    """Parse one or more Persian chemical names from row-number column OCR."""
    if not text:
        return []

    body = text.strip()
    match = re.match(r"^[\d۰-۹]+\s*[\n\s]+(.+)$", body, re.DOTALL)
    if match:
        body = match.group(1)

    lines = [line.strip() for line in body.split("\n") if line.strip()]
    names: list[str] = []
    name_lines: list[str] = []

    def flush_name_lines() -> None:
        if not name_lines:
            return
        primary = re.sub(r"\s+", " ", " ".join(name_lines)).strip("() ")
        if primary:
            names.append(primary)
        name_lines.clear()

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        paren_name = _persian_name_from_paren_line(line)
        if paren_name:
            flush_name_lines()
            names.append(paren_name)
            idx += 1
            continue
        if idx + 1 < len(lines) and lines[idx + 1].startswith("-"):
            flush_name_lines()
            if re.fullmatch(r"[\d۰-۹]+", line):
                name_lines.append(f"{line}{lines[idx + 1]}")
                idx += 2
                continue
            name_lines.append(line)
            idx += 1
            continue
        if re.fullmatch(r"[\d۰-۹]+", line):
            idx += 1
            continue
        if line.startswith("-") or name_lines:
            name_lines.append(line)
            idx += 1
            continue
        if re.search(r"[\u0600-\u06FF]{3,}", line):
            flush_name_lines()
            names.append(_normalize_persian_name_line(line))
        idx += 1

    flush_name_lines()

    deduped: list[str] = []
    for name in names:
        cleaned = re.sub(r"\s+", " ", name).strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return deduped


def extract_persian_from_row_number(text: str | None, *, index: int = 0) -> str | None:
    """Parse Persian chemical name from row-number column OCR."""
    names = extract_persian_names_from_row_number(text)
    if not names:
        return None
    if index < 0:
        index = len(names) + index
    if index >= len(names):
        return names[-1]
    persian = names[index]
    return persian if _looks_like_persian_name(persian) else persian if len(persian) >= 4 else None


def _is_rtl_reversed_mw(left: str, right: str) -> bool:
    """True for visual RTL MW tokens: fraction / integer (08/71, 14/146)."""
    if len(right) > len(left):
        return True
    return (
        len(left) <= 2
        and left.startswith("0")
        and not right.startswith("0")
        and len(right) >= 2
    )


def parse_molecular_weight(text: str) -> str | None:
    """Parse OHE6-style molecular weight tokens like 10/85 -> 85.10."""
    match = MOL_WEIGHT_PATTERN.search(_normalize_digits(text))
    if not match:
        return None
    left, right = match.groups()
    if _is_rtl_reversed_mw(left, right):
        return f"{right}.{left}"
    if len(right) == 2 and right.startswith("0"):
        return f"{left}.{right}"
    if len(left) <= 2 and len(right) >= 2:
        return f"{right}.{left}"
    if len(right) <= 2:
        return f"{left}.{right}"
    return f"{right}.{left}"


def parse_layer2_molecular_weight(text: str) -> str | None:
    """Parse a Layer-2 MW cell. Reverse only clearly RTL frac/integer slashes."""
    match = MOL_WEIGHT_PATTERN.search(_normalize_digits(text))
    if not match:
        return None
    left, right = match.groups()
    if _is_rtl_reversed_mw(left, right):
        return f"{right}.{left}"
    return None


def _parse_slash_decimal(numerator: str, denominator: str) -> str | None:
    """Parse OCR slash-decimal notation used in HSE6 OEL tables.

    Examples: ``3/0`` → ``0.3``, ``0009/0`` → ``0.0009``, ``0/0009`` → ``0.0009``.
    """
    num = numerator.strip()
    den = denominator.strip()
    if den == "0":
        return f"0.{num}" if num else None
    if num == "0" and den.isdigit():
        return f"0.{den}"
    if len(num) <= 2 and len(den) == 1:
        return f"{num}.{den}"
    return None


def _find_slash_decimal_after_unit(text: str, unit_marker: str) -> str | None:
    """Find limit values that appear after a unit marker such as ``mg/m``."""
    marker_pos = text.lower().find(unit_marker.lower())
    if marker_pos < 0:
        return None
    tail = text[marker_pos + len(unit_marker) :]
    for pattern in (
        r"(\d+)\s*/\s*0\b",
        r"0\s*/\s*(\d+)\b",
    ):
        match = re.search(pattern, tail)
        if not match:
            continue
        if pattern.startswith(r"(\d+)"):
            parsed = _parse_slash_decimal(match.group(1), "0")
        else:
            parsed = _parse_slash_decimal("0", match.group(1))
        if parsed:
            return parsed
    plain = re.search(r"[\s\n]*([\d./۰-۹]+)", tail)
    if plain:
        val = _normalize_digits(plain.group(1))
        if val.replace(".", "").isdigit():
            return val.replace("/", ".")
    return None


def _find_slash_decimal_ceiling(text: str) -> str | None:
    for pattern in (
        r"(\d+)\s*/\s*0\s*C\b",
        r"0\s*/\s*(\d+)\s*C\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        if pattern.startswith(r"(\d+)"):
            parsed = _parse_slash_decimal(match.group(1), "0")
        else:
            parsed = _parse_slash_decimal("0", match.group(1))
        if parsed:
            return parsed
    return None


def _extract_symbols(text: str) -> str | None:
    tokens = SYMBOL_PATTERN.findall(text or "")
    if not tokens:
        return None
    seen: list[str] = []
    for token in tokens:
        normalized = token.strip()
        if normalized not in seen:
            seen.append(normalized)
    return "؛ ".join(seen)


def _extract_persian_chemical_name(chunk: str, english_name: str) -> str | None:
    idx = chunk.find(english_name)
    prefix = chunk[:idx] if idx > 0 else ""
    candidates = re.findall(r"[\u0600-\u06FF][\u0600-\u06FF\s()\-،0-9]{2,}", prefix)
    candidates = [
        re.sub(r"\s+", " ", c.strip("() \n،؛"))
        for c in candidates
        if len(c.strip()) >= 3 and not SYMBOL_PATTERN.search(c) and _looks_like_persian_name(c)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _extract_health_effect(text: str) -> str | None:
    if not text:
        return None
    persian_chunks = re.findall(r"[\u0600-\u06FF][\u0600-\u06FF\s؛،\-]{3,}", text)
    if not persian_chunks:
        return None
    candidates = [c.strip() for c in persian_chunks if len(c.strip()) >= 4]
    candidates = [c for c in candidates if not SYMBOL_PATTERN.fullmatch(c.strip())]
    candidates = [c for c in candidates if _looks_like_health_effect(c)]
    if not candidates:
        return None
    return max(candidates, key=len)


def _strip_molecular_weight_token(segment: str) -> str:
    match = MOL_WEIGHT_PATTERN.search(_normalize_digits(segment))
    if not match:
        return segment
    start, end = match.span()
    tail = segment[end:]
    unit_tail = re.match(r"\s*(ppm|mg/m³|mg/m3)\b", tail, re.I)
    if unit_tail:
        end += unit_tail.end()
    return segment[:start] + segment[end:]


def _parse_limits(
    segment: str,
    *,
    full_segment: str | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    segment = _normalize_digits(segment)
    full_text = _normalize_digits(full_segment or segment)
    segment = _strip_molecular_weight_token(segment)
    value_text = _strip_molecular_weight_token(full_text)
    result: dict[str, Any] = {
        "TWA": None,
        "TWA_unit": None,
        "STEL": None,
        "STEL_unit": None,
        "ceiling": None,
        "ceiling_unit": None,
    }

    if ASPHYXIANT_PATTERN.search(segment):
        return result

    if re.search(r"mg/m", value_text, re.I):
        mg_pair = re.search(
            r"([\d./۰-۹]+)\s*mg/m[^\d]*([\d./۰-۹\s]+?)\s*(?:-|؛|;|\bA[1-4]\b|\bDSEN\b|\bBEI\b|$)",
            value_text,
            re.IGNORECASE | re.DOTALL,
        )
        if mg_pair:
            first = mg_pair.group(1).replace("/", ".")
            second_raw = re.sub(r"\s+", "", mg_pair.group(2))
            if "/" in second_raw and second_raw.split("/", 1)[1].strip() in {"0", "۰"}:
                num, den = second_raw.split("/", 1)
                twa_val = _parse_slash_decimal(num, den)
                result["STEL"] = first
                result["STEL_unit"] = "mg/m³"
                if twa_val:
                    result["TWA"] = twa_val
                    result["TWA_unit"] = "mg/m³"
            else:
                second = second_raw.replace("/", ".")
                result["TWA"] = first
                result["TWA_unit"] = "mg/m³"
                result["STEL"] = second
                result["STEL_unit"] = "mg/m³"
        else:
            slash_twa = _find_slash_decimal_after_unit(value_text, "mg/m")
            before_mg = re.search(r"([\d./]+)\s*mg/m", value_text, re.I)
            if slash_twa:
                result["TWA"] = slash_twa
                result["TWA_unit"] = "mg/m³"
                if before_mg:
                    stel_val = before_mg.group(1).replace("/", ".")
                    if stel_val != slash_twa:
                        result["STEL"] = stel_val
                        result["STEL_unit"] = "mg/m³"
            elif before_mg:
                result["TWA"] = before_mg.group(1).replace("/", ".")
                result["TWA_unit"] = "mg/m³"
            else:
                after_mg = re.search(r"mg/m\s*([\d./]+)", value_text, re.I)
                if after_mg:
                    result["TWA"] = after_mg.group(1).replace("/", ".")
                    result["TWA_unit"] = "mg/m³"

    slash_c_val = _find_slash_decimal_ceiling(segment)
    if slash_c_val:
        result["ceiling"] = slash_c_val
        result["ceiling_unit"] = "ppm"

    ceiling = CEILING_PATTERN.search(segment)
    if ceiling and not result["ceiling"]:
        result["ceiling"] = ceiling.group(1).replace("/", ".")
        result["ceiling_unit"] = ceiling.group(2) or "ppm"
    elif not result["ceiling"]:
        c_match = re.search(r"([\d.]+)\s*C\b", segment)
        if c_match:
            result["ceiling"] = c_match.group(1)
            result["ceiling_unit"] = "ppm" if "ppm" in full_text.lower() else "mg/m³"

    pairs = EXPOSURE_PATTERN.findall(segment)
    cleaned_pairs: list[tuple[str, str]] = []
    for value, unit in pairs:
        val = value.replace("/", ".")
        pair = (val, unit)
        if pair in cleaned_pairs:
            continue
        cleaned_pairs.append(pair)

    if len(cleaned_pairs) < 2:
        twin = re.search(
            r"([\d.]+)\s+([\d.]+)\s*(ppm|mg/m³|mg/m3|f/ml)\b",
            segment,
            re.IGNORECASE,
        )
        if twin:
            cleaned_pairs = [(twin.group(1), twin.group(3)), (twin.group(2), twin.group(3))]

    if not result["TWA"]:
        lone_twa = re.search(r"(?:ppm\s*)?(\d+(?:\.\d+)?)\s*(?:\n\s*)+-", full_text)
        if lone_twa and "ppm" in full_text.lower():
            result["TWA"] = lone_twa.group(1)
            result["TWA_unit"] = "ppm"

    if not result["TWA"]:
        point_one = re.search(r"\b0\s*[/.]\s*1\b", full_text)
        if point_one and "ppm" in full_text.lower() and not result["ceiling"]:
            result["TWA"] = "0.1"
            result["TWA_unit"] = "ppm"

    if len(cleaned_pairs) >= 2:
        # Persian OEL layout lists STEL/C before TWA in the merged exposure cell.
        if not result["STEL"]:
            result["STEL"] = cleaned_pairs[0][0]
            result["STEL_unit"] = cleaned_pairs[0][1]
        if not result["TWA"]:
            result["TWA"] = cleaned_pairs[1][0]
            result["TWA_unit"] = cleaned_pairs[1][1]
    elif len(cleaned_pairs) == 1 and not result["TWA"]:
        val, unit = cleaned_pairs[0]
        mw = parse_molecular_weight(full_text)
        if mw and val in {mw.split(".")[0], mw.split(".")[1] if "." in mw else ""}:
            pass
        else:
            result["TWA"] = val
            result["TWA_unit"] = unit

    mw_val = parse_molecular_weight(full_text)
    if mw_val and result.get("TWA"):
        twa = str(result["TWA"])
        mw_parts = mw_val.split(".")
        if twa in {mw_parts[0], mw_parts[1] if len(mw_parts) > 1 else "", mw_val.replace(".", "")}:
            result["TWA"] = None
            result["TWA_unit"] = None

    if field in {"STEL", "TWA", "ceiling"}:
        primary_val = result.get(field)
        primary_unit = result.get(f"{field}_unit")
        if not primary_val and cleaned_pairs:
            primary_val, primary_unit = cleaned_pairs[0]
        if not primary_val:
            fallback = result.get("TWA") or result.get("STEL")
            fallback_unit = result.get("TWA_unit") if result.get("TWA") else result.get("STEL_unit")
            primary_val = fallback
            primary_unit = fallback_unit
        ceiling_val = result.get("ceiling")
        ceiling_unit = result.get("ceiling_unit")
        result = {
            "TWA": None,
            "TWA_unit": None,
            "STEL": None,
            "STEL_unit": None,
            "ceiling": None,
            "ceiling_unit": None,
        }
        result[field] = primary_val
        result[f"{field}_unit"] = primary_unit
        if field == "STEL" and ceiling_val:
            result["ceiling"] = ceiling_val
            result["ceiling_unit"] = ceiling_unit

    return result


def split_chemical_segments(text: str) -> list[dict[str, Any]]:
    """Split a merged OCR cell into one record per chemical CAS block."""
    if not text or not CAS_PATTERN.search(text):
        return []

    segments: list[dict[str, Any]] = []
    for match in ENGLISH_NAME_PATTERN.finditer(text):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" ,;/")
        name = re.sub(r"^(OTO|BEI|DSEN|RSEN|SKIN|A[1-4])\s+", "", name, flags=re.I)
        cas = match.group(2)
        start = match.start()
        next_start = None
        for nxt in ENGLISH_NAME_PATTERN.finditer(text):
            if nxt.start() > start:
                next_start = nxt.start()
                break
        chunk = text[start:next_start if next_start is not None else len(text)]

        limits = _parse_limits(chunk, full_segment=chunk)
        mw = parse_molecular_weight(chunk)
        symbols = _extract_symbols(chunk)
        health = _extract_health_effect(chunk)
        if ASPHYXIANT_PATTERN.search(chunk):
            health = health or "خفگی‌آور ساده (D)"
        persian_name = _extract_persian_chemical_name(chunk, name)
        if not persian_name:
            paren_match = re.search(
                r"\)([\u0600-\u06FF][\u0600-\u06FF\s()\-،0-9]{2,})",
                chunk,
            )
            if paren_match:
                candidate = _normalize_persian_name_line(paren_match.group(1))
                if _looks_like_persian_name(candidate):
                    persian_name = candidate

        segments.append(
            {
                "chemical_name": name,
                "persian_chemical_name": persian_name,
                "CAS": cas,
                "molecular_weight": mw,
                "TWA": limits.get("TWA"),
                "TWA_unit": limits.get("TWA_unit"),
                "STEL": limits.get("STEL"),
                "STEL_unit": limits.get("STEL_unit"),
                "ceiling": limits.get("ceiling"),
                "ceiling_unit": limits.get("ceiling_unit"),
                "symbols": symbols,
                "health_effect": health,
                "source_chunk": chunk.strip(),
            }
        )

    return segments


def resolve_field_name(column_index: int, column_name: str | None, header_mapping: dict[int, str]) -> str:
    """Resolve semantic field. Valid header_mapping always beats positional fallback."""
    if column_index in header_mapping:
        mapped = header_mapping[column_index]
        if mapped and not mapped.startswith("column_"):
            return mapped
    if column_name and column_name in STANDARD_OEL_COLUMN_MAP.values():
        return column_name
    if column_index in STANDARD_OEL_COLUMN_MAP:
        return STANDARD_OEL_COLUMN_MAP[column_index]
    if column_index in header_mapping:
        return header_mapping[column_index]
    return f"column_{column_index}"


def _is_row_number_value(text: str) -> bool:
    return bool(re.fullmatch(r"[\d۰-۹]+", (text or "").strip()))


def _merge_parsed_into_row(row: dict[str, Any], parsed: dict[str, Any], *, overwrite: bool = False) -> None:
    def _set(field: str, value: str | None, unit: str | None = None) -> None:
        if value is None or str(value).strip() in EMPTY_LIMIT:
            return
        existing = row.get(field)
        if existing and not overwrite:
            if existing.get("value") and existing.get("value_status") != "absent":
                return
        row[field] = {
            **(existing or {}),
            "value": value,
            "unit": unit,
            "value_status": "extracted",
        }

    _set("chemical_name", parsed.get("chemical_name"))
    _set("CAS", parsed.get("CAS"))
    _set("molecular_weight", parsed.get("molecular_weight"))
    _set("TWA", parsed.get("TWA"), parsed.get("TWA_unit"))
    _set("STEL", parsed.get("STEL"), parsed.get("STEL_unit"))
    _set("ceiling", parsed.get("ceiling"), parsed.get("ceiling_unit"))
    _set("symbols", parsed.get("symbols"))
    _set("health_effect", parsed.get("health_effect"))
    if parsed.get("persian_chemical_name"):
        _set("persian_chemical_name", parsed.get("persian_chemical_name"))


def _rebalance_symbols_and_health(row: dict[str, Any]) -> None:
    symbols = row.get("symbols") or {}
    sym_val = str(symbols.get("value") or "").strip()
    if not sym_val:
        return
    if _extract_symbols(sym_val):
        return
    if _looks_like_health_effect(sym_val):
        health = row.get("health_effect") or {}
        if not health.get("value"):
            row["health_effect"] = {**symbols, "value": sym_val, "value_status": "extracted"}
        row.pop("symbols", None)


def _row_number_persian_index(row: dict[str, Any]) -> int:
    rn_field = row.get("row_number") or {}
    base = rn_field.get("value")
    original = rn_field.get("original_value") or ""
    if not base or not original:
        return 0
    base_num = _normalize_digits(str(base))
    first_num = re.match(r"^([\d۰-۹]+)", original.strip())
    if not first_num:
        return -1
    first = _normalize_digits(first_num.group(1))
    if base_num == first:
        return 0
    return -1


def _apply_persian_from_row_number(row: dict[str, Any]) -> None:
    rn_field = row.get("row_number") or {}
    original = rn_field.get("original_value") or ""
    persian_index = _row_number_persian_index(row)
    persian = extract_persian_from_row_number(original, index=persian_index)
    if not persian:
        return
    current = (row.get("persian_chemical_name") or {}).get("value")
    en_name = str((row.get("chemical_name") or {}).get("value") or "").lower()
    if current and _looks_like_persian_name(current) and not _looks_like_health_effect(current):
        if persian_index >= 0 and current != persian:
            if "acetylsalicylic" in en_name or "aspirin" in en_name:
                pass
            elif "acrolein" in en_name and "آکرول" in current:
                return
            elif persian_index == 0:
                return
        elif persian_index < 0:
            return
        elif current == persian:
            return
    row["persian_chemical_name"] = {
        **(row.get("persian_chemical_name") or rn_field),
        "value": persian,
        "value_status": "extracted",
        "original_value": original,
    }


def _sanitize_limit_fields(row: dict[str, Any]) -> None:
    for field in ("TWA", "STEL", "ceiling"):
        val = str((row.get(field) or {}).get("value") or "")
        if not val:
            continue
        if ASPHYXIANT_PATTERN.search(val) or _looks_like_health_effect(val):
            row.pop(field, None)

    mw = str((row.get("molecular_weight") or {}).get("value") or "")
    twa_field = row.get("TWA") or {}
    twa = str(twa_field.get("value") or "")
    if mw and twa:
        mw_parts = mw.split(".")
        if twa in {mw_parts[0], mw_parts[1] if len(mw_parts) > 1 else "", mw.replace(".", "")}:
            row.pop("TWA", None)


def _enrich_from_chemical_cell(row: dict[str, Any]) -> None:
    chem = row.get("chemical_name") or row.get("CAS") or {}
    original = chem.get("original_value") or chem.get("value") or ""
    if not original:
        return
    original_text = str(original)
    if CAS_PATTERN.search(original_text):
        segments = split_chemical_segments(original_text)
        if segments:
            _merge_parsed_into_row(row, segments[0], overwrite=False)
            _sanitize_limit_fields(row)
            return
    # OCR often breaks CAS brackets across lines; still recover exposure limits
    # when the merged cell clearly contains ppm/mg/m³ values.
    if re.search(r"(?:ppm|mg/m|f/ml)", original_text, re.IGNORECASE):
        limits = _parse_limits(original_text, full_segment=original_text)
        _merge_parsed_into_row(row, limits, overwrite=False)
    _sanitize_limit_fields(row)


def _cell_stub_from_field(field_data: dict[str, Any]) -> dict[str, Any]:
    original = field_data.get("original_value") or ""
    return {
        "cell_id": field_data.get("cell_id"),
        "text": original,
        "bbox": field_data.get("bbox"),
        "source_reference": field_data.get("source_reference") or {},
    }


def _physical_row_number_field(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve row_number exactly from the physical source cell."""
    rn_field = dict(row.get("row_number") or {})
    original = rn_field.get("original_value") or rn_field.get("value") or ""
    if original:
        token = _normalize_digits(str(original))
        rn_field["value"] = token
        rn_field["original_value"] = str(original)
        rn_field.setdefault("value_status", "extracted")
    return rn_field


def _apply_entity_fields(
    target: dict[str, Any],
    entity: Any,
    source_field: dict[str, Any],
) -> None:
    from goldset_generator.chemical_entity_extractor import apply_entity_to_row_fields

    apply_entity_to_row_fields(target, entity, source_field)


def enrich_oel_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Post-process table gold rows: parse merged cells and split multi-chemical rows."""
    enriched: list[dict[str, Any]] = []

    for row in rows:
        health = row.get("health_effect") or {}
        health_val = str(health.get("value") or "").strip()
        if _is_row_number_value(health_val) and "row_number" not in row:
            row["row_number"] = {
                **health,
                "value": _normalize_digits(health_val),
                "value_status": "extracted",
            }
            row.pop("health_effect", None)

        _apply_persian_from_row_number(row)
        _rebalance_symbols_and_health(row)
        _enrich_from_chemical_cell(row)

        if row.get("cas_numbers"):
            _sanitize_limit_fields(row)
            enriched.append(row)
            continue

        merged_sources: list[tuple[str, dict[str, Any]]] = []
        for field in ("chemical_name", "CAS"):
            field_data = row.get(field) or {}
            original = field_data.get("original_value") or ""
            if field_data.get("value_status") == "merged_cell" or len(CAS_PATTERN.findall(original)) > 1:
                merged_sources.append((field, field_data))

        if not merged_sources:
            _sanitize_limit_fields(row)
            enriched.append(row)
            continue

        _, source_field = merged_sources[0]
        original = source_field.get("original_value") or ""
        from goldset_generator.chemical_entity_extractor import extract_chemical_entities_from_cell

        entities = extract_chemical_entities_from_cell(_cell_stub_from_field(source_field))

        if len(entities) == 1:
            _apply_entity_fields(row, entities[0], source_field)
            _sanitize_limit_fields(row)
            enriched.append(row)
            continue

        if len(entities) > 1:
            preserved_rn = _physical_row_number_field(row)
            for entity in entities:
                new_row = {
                    k: v
                    for k, v in row.items()
                    if k
                    not in {
                        "chemical_name",
                        "CAS",
                        "cas_numbers",
                        "molecular_weight",
                        "TWA",
                        "STEL",
                        "ceiling",
                        "symbols",
                        "health_effect",
                        "persian_chemical_name",
                    }
                }
                new_row["row_number"] = dict(preserved_rn)
                _apply_entity_fields(new_row, entity, source_field)
                _rebalance_symbols_and_health(new_row)
                _sanitize_limit_fields(new_row)
                enriched.append(new_row)
            continue

        segments = split_chemical_segments(original)
        if len(segments) <= 1:
            _sanitize_limit_fields(row)
            enriched.append(row)
            continue

        preserved_rn = _physical_row_number_field(row)
        row_number_original = (row.get("row_number") or {}).get("original_value") or ""
        row_number_names = extract_persian_names_from_row_number(row_number_original)

        for offset, parsed in enumerate(segments):
            new_row = {
                k: v
                for k, v in row.items()
                if k
                not in {
                    "chemical_name",
                    "CAS",
                    "cas_numbers",
                    "molecular_weight",
                    "TWA",
                    "STEL",
                    "ceiling",
                    "symbols",
                    "health_effect",
                    "persian_chemical_name",
                }
            }

            new_row["row_number"] = dict(preserved_rn)

            persian = parsed.get("persian_chemical_name")
            if not persian and offset < len(row_number_names):
                persian = row_number_names[offset]
            elif not persian and offset > 0:
                persian = extract_persian_from_row_number(row_number_original, index=-1)
            if not persian and offset == 0:
                persian = extract_persian_from_row_number(row_number_original, index=0)
            if persian:
                persian_field = dict(row.get("persian_chemical_name") or source_field)
                persian_field.update({"value": persian, "value_status": "extracted"})
                new_row["persian_chemical_name"] = persian_field

            for field in (
                "TWA",
                "STEL",
                "ceiling",
            ):
                new_row.pop(field, None)

            for field in (
                "chemical_name",
                "CAS",
                "molecular_weight",
                "TWA",
                "STEL",
                "ceiling",
                "symbols",
                "health_effect",
            ):
                val = parsed.get(field)
                unit = parsed.get(f"{field}_unit") if field in {"TWA", "STEL", "ceiling"} else None
                if val is None:
                    continue
                payload = dict(source_field)
                payload.update(
                    {
                        "value": val,
                        "unit": unit,
                        "value_status": "extracted",
                        "original_value": parsed.get("source_chunk") or original,
                    }
                )
                new_row[field] = payload

            _merge_parsed_into_row(new_row, parsed, overwrite=True)
            _rebalance_symbols_and_health(new_row)
            _sanitize_limit_fields(new_row)
            enriched.append(new_row)

    for row in enriched:
        if not (row.get("persian_chemical_name") or {}).get("value"):
            _apply_persian_from_row_number(row)
        _rebalance_symbols_and_health(row)
        _sanitize_limit_fields(row)

    deduped: list[dict[str, Any]] = []
    seen_cas: set[str] = set()
    for row in enriched:
        cas = str((row.get("CAS") or {}).get("value") or "")
        if cas and cas in seen_cas:
            continue
        if cas:
            seen_cas.add(cas)
        deduped.append(row)

    return deduped
