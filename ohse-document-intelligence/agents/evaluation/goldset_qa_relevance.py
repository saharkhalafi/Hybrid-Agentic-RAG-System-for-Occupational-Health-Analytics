"""Gold → retrieved relevance at evaluation time (page + source_text; no Gold IDs)."""

from __future__ import annotations

import re
from typing import Any

from agents.evaluation.goldset_qa_loader import GoldsetQAItem
from normalization.persian_normalizer import normalize_persian_text
from retrieval.eval_metrics import exact_numeric_match

CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")
LATIN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")
NUMBER_RE = re.compile(r"-?\d+(?:[./]\d+)?")
DEFAULT_GOLD_TOKEN_COVERAGE = 0.45
FIELD_ALIASES = {
    "twa": "twa",
    "stel": "stel",
    "ceiling": "ceiling",
    "c": "ceiling",
    "stel-c": "stel",
}


def normalize_eval_text(value: Any) -> str:
    if value is None:
        return ""
    normalized = normalize_persian_text(str(value)).normalized
    return " ".join(normalized.lower().split())


def parse_page(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def pages_match(retrieved: dict[str, Any], gold_page: int) -> bool:
    page = parse_page(retrieved.get("page_number"))
    printed = parse_page(retrieved.get("printed_page_number"))
    meta = retrieved.get("metadata") if isinstance(retrieved.get("metadata"), dict) else {}
    if page is None:
        page = parse_page(meta.get("page_number"))
    if printed is None:
        printed = parse_page(meta.get("printed_page_number"))
    return gold_page in {p for p in (page, printed) if p is not None}


def text_overlaps(
    content: Any,
    source_text: str,
    *,
    min_token_coverage: float = DEFAULT_GOLD_TOKEN_COVERAGE,
) -> bool:
    gold = normalize_eval_text(source_text)
    hay = normalize_eval_text(content)
    if not gold or not hay:
        return False
    if gold in hay or hay in gold:
        return True
    gold_tokens = [t for t in gold.split() if t]
    hay_tokens = set(hay.split())
    if not gold_tokens:
        return False
    covered = sum(1 for t in gold_tokens if t in hay_tokens)
    return (covered / len(gold_tokens)) >= min_token_coverage


def extract_cas_values(*texts: Any) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if text is None:
            continue
        found.update(CAS_RE.findall(str(text)))
    return found


def extract_numbers(value: Any) -> list[float]:
    if value is None or value == "":
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    text = normalize_eval_text(value).replace(",", "")
    out: list[float] = []
    for token in NUMBER_RE.findall(text):
        if "/" in token:
            parts = token.split("/")
            try:
                if len(parts) == 2 and float(parts[1]) != 0:
                    out.append(float(parts[0]) / float(parts[1]))
                    continue
            except ValueError:
                continue
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def numeric_matches_reference(retrieved_value: Any, reference_answer: str) -> bool | None:
    """Return True/False when Gold has a number; None when numeric check is not applicable."""
    expected = extract_numbers(reference_answer)
    if not expected:
        return None
    actual = extract_numbers(retrieved_value)
    if not actual:
        return False
    for exp in expected:
        for act in actual:
            if exact_numeric_match(exp, act) == 1.0:
                return True
    return False


def _latin_tokens(text: str) -> set[str]:
    skip = {
        "twa",
        "stel",
        "oel",
        "cas",
        "mw",
        "ppm",
        "ipm",
        "niosh",
        "osha",
        "acgih",
        "the",
        "and",
        "for",
        "of",
    }
    return {t.lower() for t in LATIN_NAME_RE.findall(text or "") if t.lower() not in skip and len(t) > 1}


def gold_limit_fields(gold: GoldsetQAItem) -> set[str]:
    blob = " ".join(
        [
            normalize_eval_text(gold.question),
            normalize_eval_text(gold.source_text),
            normalize_eval_text(gold.reference_answer),
        ]
    )
    found: set[str] = set()
    for token in LATIN_NAME_RE.findall(blob):
        mapped = FIELD_ALIASES.get(token.lower())
        if mapped:
            found.add(mapped)
    return found


def normalize_record_field(value: Any) -> str | None:
    if value is None or value == "":
        return None
    token = str(value).strip().lower()
    return FIELD_ALIASES.get(token, token)


def field_aligns(retrieved: dict[str, Any], gold: GoldsetQAItem) -> bool:
    rec_field = normalize_record_field(retrieved.get("field"))
    if rec_field is None:
        return True
    expected = gold_limit_fields(gold)
    if not expected:
        return True
    return rec_field in expected


def chemical_aligns(retrieved: dict[str, Any], gold: GoldsetQAItem) -> bool:
    rec_cas = extract_cas_values(retrieved.get("cas"))
    gold_cas = extract_cas_values(gold.question, gold.source_text, gold.reference_answer)
    if rec_cas and gold_cas and rec_cas.isdisjoint(gold_cas):
        return False
    if rec_cas and gold_cas and rec_cas & gold_cas:
        return True

    names = [
        retrieved.get("chemical_name"),
        retrieved.get("english_name"),
        retrieved.get("persian_name"),
    ]
    q_norm = normalize_eval_text(gold.question)
    src_norm = normalize_eval_text(gold.source_text)
    for name in names:
        n = normalize_eval_text(name)
        if n and (n in q_norm or n in src_norm):
            return True

    q_latin = _latin_tokens(gold.question)
    rec_latin = set()
    for name in names:
        rec_latin |= _latin_tokens(str(name or ""))
    if q_latin and rec_latin and rec_latin.isdisjoint(q_latin):
        return False
    if q_latin and rec_latin and q_latin & rec_latin:
        return True
    if rec_cas and not gold_cas and not q_latin:
        return True
    if not rec_cas and not rec_latin:
        return True
    return not (gold_cas or q_latin)


def semantic_content(chunk: dict[str, Any]) -> str:
    return str(chunk.get("content") or chunk.get("text") or chunk.get("enriched_content") or "")


def formula_text(record: dict[str, Any]) -> str:
    parts = [
        record.get("original_expression"),
        record.get("normalized_expression"),
        record.get("description"),
        record.get("formula_id"),
        record.get("content"),
        record.get("expression"),
    ]
    return " ".join(str(p) for p in parts if p)


def structured_value_payload(record: dict[str, Any]) -> Any:
    if record.get("value") is not None:
        return record.get("value")
    if record.get("molecular_weight") is not None:
        return record.get("molecular_weight")
    for key in ("twa", "stel", "ceiling", "normalized_value", "original_value"):
        if record.get(key) is not None:
            return record.get(key)
    return None


def is_semantic_chunk_relevant(chunk: dict[str, Any], gold: GoldsetQAItem) -> bool:
    if not pages_match(chunk, gold.source_page):
        return False
    return text_overlaps(semantic_content(chunk), gold.source_text)


def is_structured_record_relevant(
    record: dict[str, Any],
    gold: GoldsetQAItem,
    *,
    require_numeric_when_present: bool = True,
) -> bool:
    if record.get("success") is False:
        return False
    if not pages_match(record, gold.source_page):
        return False
    if not chemical_aligns(record, gold):
        return False
    if not field_aligns(record, gold):
        return False
    if require_numeric_when_present:
        match = numeric_matches_reference(structured_value_payload(record), gold.reference_answer)
        if match is False:
            return False
    return True


def is_formula_record_relevant(record: dict[str, Any], gold: GoldsetQAItem) -> bool:
    if record.get("success") is False:
        return False
    if not pages_match(record, gold.source_page):
        return False
    return text_overlaps(formula_text(record), gold.source_text)
