"""Validate semantic text chunks against evidence and gold cross-references."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from goldset_generator.persian_text_repair import (
    assess_fragmentation,
    build_raw_text,
    build_repaired_text,
    build_repair_audit,
    has_possible_ocr,
    has_unresolved_ocr,
    normalize_repaired_text,
    repair_ocr_text,
)
from goldset_generator.persian_text_reconstructor import looks_like_table_row

NUMERIC_LIMIT_PATTERN = re.compile(
    r"\b\d+(?:[./]\d+)?\s*(?:ppm|mg/m³|mg/m3|dBA|mg/L|f/ml|%)\b",
    re.I,
)
CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
FORMULA_BODY = re.compile(r"(?:=|√|∑|∫|×|\^|_\{|\bsqrt\b)", re.I)
OCR_NOISE = re.compile(r"(?:\b[اآی]\s+[اآی]\b|\bمی\s+ن\s+توان|\bش\s+یمی|\bمواج\s+ه)")


class SemanticTextValidator:
    def __init__(
        self,
        *,
        gold_dir: Path,
        paragraph_evidence: dict[str, str],
        table_cell_texts: set[str] | None = None,
    ) -> None:
        self.gold_dir = gold_dir
        self.paragraph_evidence = paragraph_evidence
        self.table_cell_texts = table_cell_texts or set()

    def validate_chunk(self, chunk: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        text = chunk.get("text") or ""
        normalized = chunk.get("normalized_text") or ""

        if chunk.get("retrieval_mode") != "semantic":
            issues.append("retrieval_mode must be semantic")

        if chunk.get("embedding_status") != "pending":
            issues.append("embedding_status must be pending until embedding pipeline runs")

        if chunk.get("provenance", {}).get("llm_rewritten"):
            issues.append("chunk text must not be LLM-rewritten")

        if chunk.get("semantic_enrichment") not in (None, {}):
            enrichment = chunk.get("semantic_enrichment") or {}
            if enrichment.get("semantic_summary") and enrichment.get("semantic_summary") == text:
                issues.append("semantic_enrichment must not duplicate or replace source text")

        if not text.strip():
            issues.append("empty chunk text")
            return issues

        if not normalized.strip():
            issues.append("missing normalized_text")

        page_number = chunk.get("pdf_page_number")
        if not page_number:
            issues.append("missing pdf_page_number")

        if not chunk.get("bbox") and not (chunk.get("bbox_references") or []):
            issues.append("missing bbox/source evidence")

        provenance = chunk.get("provenance") or {}
        if not provenance.get("text_evidence_ids"):
            issues.append("orphan chunk: missing provenance.text_evidence_ids")

        evidence_ids = (chunk.get("source_references") or {}).get("text_evidence_ids") or []
        if not evidence_ids:
            issues.append("missing text_evidence_ids")
        else:
            combined_evidence = "\n".join(
                self.paragraph_evidence.get(eid, "") for eid in evidence_ids
            )
            if combined_evidence:
                norm_text = normalize_repaired_text(chunk.get("text") or "")
                norm_evidence = normalize_repaired_text(
                    build_repaired_text(
                        [self.paragraph_evidence.get(eid, "") for eid in evidence_ids]
                    )
                )
                if norm_text not in norm_evidence and norm_evidence not in norm_text:
                    if not _token_subset(norm_text, norm_evidence):
                        issues.append("chunk text not traceable to text evidence")

        refs = chunk.get("source_references") or {}
        for table_id in refs.get("table_ids") or []:
            if not (self.gold_dir / "tables" / f"{table_id}.json").exists():
                issues.append(f"orphan table reference: {table_id}")

        for formula_id in refs.get("formula_ids") or []:
            formula_path = self.gold_dir / "formulas" / f"{formula_id}.json"
            candidate_path = self.gold_dir / "candidates" / "formulas" / f"{formula_id}.json"
            if not formula_path.exists() and not candidate_path.exists():
                issues.append(f"orphan formula reference: {formula_id}")

        page_ids = refs.get("page_ids") or []
        for page_id in page_ids:
            page_num = _page_id_number(page_id)
            if page_num is not None and not (self.gold_dir / "pages" / f"page_{page_num:03d}.json").exists():
                issues.append(f"orphan page reference: {page_id}")

        if looks_like_table_row(text):
            issues.append("chunk appears to contain structured table row content")

        if FORMULA_BODY.search(text) and not refs.get("formula_ids"):
            issues.append("chunk contains formula body but no formula reference")

        for cell_text in self.table_cell_texts:
            cell_norm = normalize_repaired_text(cell_text)
            chunk_norm = normalize_repaired_text(text)
            if len(cell_norm) > 40 and cell_norm in chunk_norm:
                issues.append("chunk duplicates structured table cell content")
                break

        for match in NUMERIC_LIMIT_PATTERN.findall(text):
            if CAS_PATTERN.search(text) and match not in " ".join(
                self.paragraph_evidence.get(eid, "") for eid in evidence_ids
            ):
                issues.append(f"structured numeric limit may belong in tables/: {match!r}")

        frag = assess_fragmentation(normalized)
        if frag.has_confirmed:
            issues.append("confirmed_ocr_fragmentation")
        elif frag.has_possible:
            issues.append("possible_ocr_fragmentation")
        elif has_possible_ocr(normalized):
            issues.append("possible_ocr_fragmentation")

        if OCR_NOISE.search(normalized):
            issues.append("unresolved OCR artifact detected")

        return issues

    def validate_ocr_repair(self, chunk: dict[str, Any]) -> dict[str, Any]:
        normalized = chunk.get("normalized_text") or ""
        ocr_meta = chunk.get("ocr_repair") or {}
        frag = assess_fragmentation(normalized)
        return {
            "original_fragmentation_count": ocr_meta.get("fragmentation_before", frag.confirmed_count),
            "remaining_fragmentation_count": frag.confirmed_count,
            "possible_fragmentation_count": frag.possible_count,
            "high_confidence_repairs": ocr_meta.get("high_confidence_repairs", 0),
            "medium_confidence_repairs": ocr_meta.get("medium_confidence_repairs", 0),
            "low_confidence_repairs": ocr_meta.get("low_confidence_repairs", 0),
            "protected_tokens": ocr_meta.get("protected_tokens", []),
        }

    @staticmethod
    def validate_corpus(chunks: list[dict[str, Any]]) -> dict[str, Any]:
        seen_ids: set[str] = set()
        seen_text: dict[str, str] = {}
        duplicates: list[str] = []
        empty = 0
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id") or ""
            if not (chunk.get("text") or "").strip():
                empty += 1
            if chunk_id in seen_ids:
                duplicates.append(chunk_id)
            seen_ids.add(chunk_id)
            norm = normalize_repaired_text(chunk.get("text") or "")
            if norm:
                prior = seen_text.get(norm)
                if prior and prior != chunk_id:
                    duplicates.append(f"{prior}|{chunk_id}")
                seen_text[norm] = chunk_id
        return {
            "total_chunks": len(chunks),
            "empty_chunks": empty,
            "duplicate_chunks": len(duplicates),
            "duplicate_ids": duplicates[:50],
        }


def _token_subset(chunk: str, evidence: str) -> bool:
    chunk_tokens = chunk.split()
    evidence_tokens = set(evidence.split())
    if not chunk_tokens:
        return False
    matched = sum(1 for token in chunk_tokens if token in evidence_tokens)
    return matched / len(chunk_tokens) >= 0.85


def _page_id_number(page_id: str) -> int | None:
    match = re.search(r"page_(\d+)", page_id or "")
    return int(match.group(1)) if match else None
