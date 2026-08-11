"""Geometry-aware semantic chemical entity extraction from physical table cells.

Physical structure (one cell) is preserved; multiple CAS numbers or chemical
entities are represented semantically with per-token provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from goldset_generator.oel_row_parser import CAS_PATTERN, _extract_persian_chemical_name

ENGLISH_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
CAS_IN_TEXT = re.compile(r"\[(\d{2,7}-\d{2}-\d)\s*\]")


@dataclass
class CasProvenance:
    value: str
    source_text: str
    source_bbox: dict[str, float] | None = None
    source_span: str | None = None

    def to_dict(self, *, source_cell_id: str | None) -> dict[str, Any]:
        return {
            "value": self.value,
            "source_cell_id": source_cell_id,
            "source_bbox": self.source_bbox,
            "source_text": self.source_text,
            "source_span": self.source_span,
            "value_status": "extracted" if self.source_bbox else "review_required",
        }


@dataclass
class ChemicalEntity:
    chemical_name: str | None
    persian_chemical_name: str | None
    cas_numbers: list[CasProvenance] = field(default_factory=list)
    source_text: str = ""
    review_required: bool = False
    ambiguity_reason: str | None = None

    @property
    def primary_cas(self) -> str | None:
        return self.cas_numbers[0].value if self.cas_numbers else None


def apply_entity_to_row_fields(
    target: dict[str, Any],
    entity: ChemicalEntity,
    source_field: dict[str, Any],
) -> None:
    """Populate semantic fields from one entity while retaining cell provenance."""
    status = "review_required" if entity.review_required else "extracted"
    cell_id = source_field.get("cell_id")
    payload_base = dict(source_field)
    target["chemical_name"] = {
        **payload_base,
        "value": entity.chemical_name,
        "value_status": status,
        "original_value": entity.source_text,
    }
    if entity.persian_chemical_name:
        target["persian_chemical_name"] = {
            **payload_base,
            "value": entity.persian_chemical_name,
            "value_status": status,
            "original_value": entity.persian_chemical_name,
        }
    target["cas_numbers"] = [c.to_dict(source_cell_id=cell_id) for c in entity.cas_numbers]
    if entity.primary_cas:
        target["CAS"] = {
            **payload_base,
            "value": entity.primary_cas,
            "value_status": status,
            "original_value": entity.source_text,
        }


def cell_stub_from_field(field_data: dict[str, Any]) -> dict[str, Any]:
    original = field_data.get("original_value") or ""
    return {
        "cell_id": field_data.get("cell_id"),
        "text": original,
        "bbox": field_data.get("bbox"),
        "source_reference": field_data.get("source_reference") or {},
    }


def _bbox_from_word(word: dict[str, Any]) -> dict[str, float] | None:
    bbox = word.get("bbox")
    if isinstance(bbox, dict) and bbox.get("width") is not None:
        return bbox
    return None


def _cas_from_words(source_words: list[dict[str, Any]]) -> list[CasProvenance]:
    hits: list[CasProvenance] = []
    seen: set[str] = set()
    for word in source_words:
        token = str(word.get("word") or "")
        for match in CAS_IN_TEXT.finditer(token):
            value = match.group(1)
            if value in seen:
                continue
            seen.add(value)
            hits.append(
                CasProvenance(
                    value=value,
                    source_text=match.group(0),
                    source_bbox=_bbox_from_word(word),
                    source_span=word.get("source_span"),
                )
            )
    return hits


def _cas_from_text(text: str) -> list[CasProvenance]:
    hits: list[CasProvenance] = []
    seen: set[str] = set()
    for match in CAS_IN_TEXT.finditer(text):
        value = match.group(1)
        if value in seen:
            continue
        seen.add(value)
        hits.append(CasProvenance(value=value, source_text=match.group(0)))
    return hits


def _english_name_tokens(source_words: list[dict[str, Any]], text: str) -> list[tuple[str, dict[str, float] | None, float]]:
    """Return (name, bbox, y) tuples for English chemical name tokens."""
    tokens: list[tuple[str, dict[str, float] | None, float]] = []
    stop = {"dust", "sodium", "and", "compounds", "insoluble", "metal", "fibres", "fiber"}
    if source_words:
        for word in source_words:
            token = str(word.get("word") or "").strip(" ,;/")
            if not ENGLISH_TOKEN.fullmatch(token):
                continue
            if token.lower() in stop:
                continue
            if CAS_IN_TEXT.search(token):
                continue
            bbox = _bbox_from_word(word)
            y = float(bbox.get("y", 0)) if bbox else 0.0
            tokens.append((token, bbox, y))
        if tokens:
            return tokens
    for match in ENGLISH_TOKEN.finditer(text):
        token = match.group(0)
        if token.lower() in stop or len(token) < 4:
            continue
        tokens.append((token, None, 0.0))
    return tokens


def _resolve_english_name(text: str, english_tokens: list[tuple[str, dict[str, float] | None, float]]) -> str | None:
    if not english_tokens and not text:
        return None
    semi_parts = text.split(";")
    if len(semi_parts) > 1:
        tail = semi_parts[-1]
        match = ENGLISH_TOKEN.search(tail)
        if match:
            return match.group(0).strip()
    if english_tokens:
        # Prefer capitalized token (proper chemical name) over lowercase fragments.
        capped = [t for t, _, _ in english_tokens if t[0].isupper()]
        if len(capped) == 1:
            return capped[0]
        if capped:
            return capped[-1]
        return english_tokens[-1][0]
    eng = ENGLISH_TOKEN.findall(text)
    return eng[-1] if eng else None


def _cluster_entities_by_line(
    text: str,
    source_words: list[dict[str, Any]],
    cas_hits: list[CasProvenance],
    english_tokens: list[tuple[str, dict[str, float] | None, float]],
) -> list[ChemicalEntity]:
    """Split into multiple entities when distinct English names sit on separate lines."""
    if len(cas_hits) <= 1:
        return []

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return []

    entities: list[ChemicalEntity] = []
    for line in lines:
        line_cas = _cas_from_text(line)
        if not line_cas:
            continue
        line_eng = [t for t, _, _ in english_tokens if t.lower() in line.lower()]
        name = line_eng[-1] if line_eng else _resolve_english_name(line, [])
        if not name:
            eng_match = ENGLISH_TOKEN.search(line)
            name = eng_match.group(0) if eng_match else None
        entities.append(
            ChemicalEntity(
                chemical_name=name,
                persian_chemical_name=_extract_persian_chemical_name(line, name or ""),
                cas_numbers=line_cas,
                source_text=line,
                review_required=any(c.source_bbox is None for c in line_cas),
            )
        )
    if len(entities) >= 2:
        return entities
    return []


def extract_chemical_entities_from_cell(cell: dict[str, Any]) -> list[ChemicalEntity]:
    """Extract semantic chemical entities from one physical cell."""
    text = str(cell.get("text") or "").strip()
    if not text:
        return []

    source_words = (cell.get("source_reference") or {}).get("source_words") or []
    cas_hits = _cas_from_words(source_words) if source_words else _cas_from_text(text)
    if not cas_hits:
        return []

    english_tokens = _english_name_tokens(source_words, text)
    multi_line = _cluster_entities_by_line(text, source_words, cas_hits, english_tokens)
    if multi_line:
        return multi_line

    english_name = _resolve_english_name(text, english_tokens)
    persian = _extract_persian_chemical_name(text, english_name or "")

    missing_bbox = bool(source_words) and any(c.source_bbox is None for c in cas_hits)
    review = missing_bbox or (len(cas_hits) > 1 and not english_name)

    return [
        ChemicalEntity(
            chemical_name=english_name,
            persian_chemical_name=persian,
            cas_numbers=cas_hits,
            source_text=text,
            review_required=review,
            ambiguity_reason="missing_name_for_multi_cas" if review and len(cas_hits) > 1 else None,
        )
    ]
