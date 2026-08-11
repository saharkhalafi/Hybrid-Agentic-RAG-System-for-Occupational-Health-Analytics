"""Load and index Gold artifacts for deterministic eval dataset generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT, get_settings
from persistence.knowledge_pipeline import _extract_cas, _field_payload, _row_index
from retrieval.eval_schema import NumericGroundTruth

CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")


@dataclass
class OelRowRecord:
    table_id: str
    source_row_key: str
    page_number: int
    gold_path: str
    english_name: str | None
    persian_name: str | None
    cas: str | None
    twa: dict[str, Any] | None
    stel: dict[str, Any] | None
    ceiling: dict[str, Any] | None
    unit: str | None
    molecular_weight: dict[str, Any] | None
    document_id: str | None = None


@dataclass
class SemanticChunkRecord:
    chunk_id: str
    page_number: int
    printed_page_number: int | None
    text: str
    section_title: str | None
    section_path: list[str]
    bbox: dict[str, float] | None
    document_id: str | None
    gold_path: str
    topic_keywords: list[str] = field(default_factory=list)


@dataclass
class FormulaRecord:
    formula_id: str
    page_number: int
    normalized_expression: str
    raw_expression: str
    variables: dict[str, Any]
    bbox: dict[str, float] | None
    gold_path: str
    status: str
    domain: str | None = None


@dataclass
class GoldCorpus:
    oel_rows: list[OelRowRecord] = field(default_factory=list)
    semantic_chunks: list[SemanticChunkRecord] = field(default_factory=list)
    formulas: list[FormulaRecord] = field(default_factory=list)
    document_id: str | None = None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if "/" in s:
        parts = s.split("/")
        try:
            if len(parts) == 2:
                return float(parts[0]) / float(parts[1])
        except (ValueError, ZeroDivisionError):
            pass
    try:
        return float(s)
    except ValueError:
        return None


def _chemical_name(row: dict[str, Any]) -> tuple[str | None, str | None]:
    en = None
    fa = None
    for key in ("chemical_name", "Chemical", "english_name"):
        fd = row.get(key)
        if isinstance(fd, dict):
            en = fd.get("value") or en
            fa = fd.get("original_value") or fa
    return en, fa


def load_gold_corpus(root: Path | None = None) -> GoldCorpus:
    settings = get_settings()
    root = root or settings.gold_dir
    corpus = GoldCorpus()

    sem_path = root / "rag" / "semantic_text_production.jsonl"
    if sem_path.exists():
        for line in sem_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("review_status") != "accepted":
                continue
            section = rec.get("section") or {}
            title = section.get("title")
            path_list = section.get("path") or []
            text = rec.get("text") or ""
            keywords = []
            if title:
                keywords.append(title)
            if path_list:
                keywords.extend(path_list[-2:])
            corpus.semantic_chunks.append(
                SemanticChunkRecord(
                    chunk_id=rec["chunk_id"],
                    page_number=int(rec.get("pdf_page_number") or rec.get("page_number") or 0),
                    printed_page_number=rec.get("printed_page_number"),
                    text=text,
                    section_title=title,
                    section_path=path_list,
                    bbox=rec.get("bbox"),
                    document_id=rec.get("document_id"),
                    gold_path=str(sem_path.relative_to(PROJECT_ROOT)),
                    topic_keywords=keywords,
                )
            )
            if rec.get("document_id") and not corpus.document_id:
                corpus.document_id = rec["document_id"]

    tables_dir = root / "tables"
    for path in sorted(tables_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("table_type") != "chemical_oel":
            continue
        if not payload.get("gold_allowed", True):
            continue
        table_id = payload.get("table_id") or path.stem
        page = int(payload.get("page_number") or payload.get("pdf_page_number") or 0)
        for idx, row in enumerate(payload.get("rows") or []):
            cas = _extract_cas(row)
            en, fa = _chemical_name(row)
            twa = _field_payload(row.get("TWA"))
            stel = _field_payload(row.get("STEL"))
            ceiling = _field_payload(row.get("CEILING") or row.get("Ceiling") or row.get("STEL/C"))
            mw = _field_payload(row.get("MW") or row.get("molecular_weight"))
            unit = None
            for fp in (twa, stel, ceiling):
                if fp and fp.get("unit"):
                    unit = fp["unit"]
                    break
            row_key = f"{table_id}:row_{_row_index(row, idx)}"
            corpus.oel_rows.append(
                OelRowRecord(
                    table_id=table_id,
                    source_row_key=row_key,
                    page_number=page,
                    gold_path=str(path.relative_to(PROJECT_ROOT)),
                    english_name=en,
                    persian_name=fa,
                    cas=cas,
                    twa=twa,
                    stel=stel,
                    ceiling=ceiling,
                    unit=unit,
                    molecular_weight=mw,
                    document_id=payload.get("document_id") or corpus.document_id,
                )
            )

    formulas_dir = root / "formulas"
    for path in sorted(formulas_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = payload.get("status") or payload.get("validation", {}).get("overall", "")
        if status not in {"approved", "accepted"}:
            continue
        fid = payload.get("formula_id") or path.stem
        evidence = payload.get("evidence") or payload.get("source_reference") or {}
        page = int(evidence.get("page") or payload.get("page_number") or 0)
        semantics = payload.get("semantics") or {}
        corpus.formulas.append(
            FormulaRecord(
                formula_id=fid,
                page_number=page,
                normalized_expression=payload.get("normalized_expression") or "",
                raw_expression=payload.get("raw_expression") or "",
                variables=semantics.get("variables") or {},
                bbox=evidence.get("bbox"),
                gold_path=str(path.relative_to(PROJECT_ROOT)),
                status=status,
                domain=semantics.get("formula_type"),
            )
        )

    return corpus


def numeric_from_field(field: dict[str, Any] | None) -> NumericGroundTruth | None:
    if not field:
        return None
    norm = _parse_float(field.get("normalized_value") or field.get("accepted_value"))
    return NumericGroundTruth(
        value=str(field.get("accepted_value") or field.get("normalized_value") or ""),
        normalized_value=norm,
        unit=field.get("unit"),
        source_cell_id=field.get("cell_id"),
        original_value=str(field.get("original_value") or ""),
        bbox=field.get("bbox"),
    )
