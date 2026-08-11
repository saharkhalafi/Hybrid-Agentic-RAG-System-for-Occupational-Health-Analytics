"""Semantic chunking from pre-structured page evidence — no inline section discovery."""

from __future__ import annotations

import re
from typing import Any

from goldset_generator.page_structure_detector import (
    PageStructure,
    detect_page_structure,
    section_for_paragraph,
)
from goldset_generator.persian_text_repair import (
    assess_fragmentation,
    build_raw_text,
    build_repaired_text,
    build_repair_audit,
    has_unresolved_ocr,
    normalize_repaired_text,
    repair_ocr_text,
)
from goldset_generator.persian_text_reconstructor import (
    looks_like_table_header,
    looks_like_table_row,
    sort_blocks_reading_order,
)
from normalization.persian_normalizer import contains_persian

RUNNING_HEADER_PATTERN = re.compile(
    r"حدود\s+مجاز|وزارت\s+بهداشت|درمان\s+و.?آموزش\s+پزشکی",
    re.I,
)
PAGE_NUMBER_ONLY = re.compile(r"^\d{1,4}$")
ROW_NUMBER_ONLY = re.compile(r"^\d{1,4}$")
FORMULA_REF_PATTERN = re.compile(r"رابطه\s*\d+|معادله|equation\s*\d+", re.I)
SENTENCE_END = re.compile(r"[.؟!؛»\"]\s*$")

MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1400
TOPIC_Y_GAP = 28.0
OVERLAP_THRESHOLD = 0.30


def _is_running_header(text: str) -> bool:
    stripped = (text or "").strip()
    if PAGE_NUMBER_ONLY.fullmatch(stripped):
        return True
    if ROW_NUMBER_ONLY.fullmatch(stripped):
        return True
    if len(stripped) > 100:
        return False
    head = stripped[:100]
    if RUNNING_HEADER_PATTERN.search(head) and (
        ("حدود" in head and "وزارت" in head)
        or head.startswith("حدود")
        and "شغلی" in head
        and len(stripped) < 80
    ):
        return True
    return False


def _bbox_area(bbox: dict[str, Any] | None) -> float:
    if not bbox:
        return 0.0
    return max(float(bbox.get("width") or 0), 0) * max(float(bbox.get("height") or 0), 0)


def _bbox_overlap_ratio(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float:
    if not a or not b:
        return 0.0
    ax1, ay1 = float(a["x"]), float(a["y"])
    ax2, ay2 = ax1 + float(a["width"]), ay1 + float(a["height"])
    bx1, by1 = float(b["x"]), float(b["y"])
    bx2, by2 = bx1 + float(b["width"]), by1 + float(b["height"])
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    smaller = min(_bbox_area(a), _bbox_area(b))
    return inter / smaller if smaller else 0.0


def _paragraph_in_table_region(
    bbox: dict[str, Any] | None,
    table_bboxes: list[dict[str, Any]],
    cell_bboxes: list[dict[str, Any]],
) -> bool:
    if not bbox:
        return False
    for region in table_bboxes + cell_bboxes:
        if _bbox_overlap_ratio(bbox, region) >= OVERLAP_THRESHOLD:
            return True
    return False


def _paragraph_in_table_evidence(text: str, cell_texts: set[str]) -> bool:
    repaired = repair_ocr_text(text)
    if len(repaired) < 12:
        return False
    for cell in cell_texts:
        cell_norm = repair_ocr_text(cell)
        if not cell_norm:
            continue
        if repaired == cell_norm:
            return True
        if len(repaired) > 40 and repaired in cell_norm:
            return True
        if len(cell_norm) > 40 and cell_norm in repaired:
            return True
    return False


def _ends_complete_sentence(text: str) -> bool:
    return bool(SENTENCE_END.search((text or "").strip()))


def _related_entity_ids(
    chunk_bbox: dict[str, Any] | None,
    entities: list[dict[str, Any]],
) -> list[str]:
    related: list[str] = []
    for entity in entities:
        ref = entity.get("source_reference") or {}
        bbox = ref.get("bbox")
        if chunk_bbox and bbox and _bbox_overlap_ratio(chunk_bbox, bbox) >= 0.15:
            for cell_id in entity.get("linked_cell_ids") or []:
                if cell_id and cell_id not in related:
                    related.append(cell_id)
    return related


def _related_table_ids(
    chunk_text: str,
    table_ids: list[str],
    chunk_bbox: dict[str, Any] | None,
    table_bboxes: dict[str, dict[str, Any]],
) -> list[str]:
    if not table_ids:
        return []
    if "جدول" in chunk_text or "table" in chunk_text.lower():
        return list(table_ids)
    for bbox in table_bboxes.values():
        if chunk_bbox and bbox and _bbox_overlap_ratio(chunk_bbox, bbox) >= 0.05:
            return []
    return []


def _related_formula_ids(chunk_text: str, formula_ids: list[str]) -> list[str]:
    if not formula_ids or not FORMULA_REF_PATTERN.search(chunk_text):
        return []
    return list(formula_ids)


def _union_bbox(boxes: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    valid = [b for b in boxes if b and all(k in b for k in ("x", "y", "width", "height"))]
    if not valid:
        return None
    x0 = min(float(b["x"]) for b in valid)
    y0 = min(float(b["y"]) for b in valid)
    x1 = max(float(b["x"]) + float(b["width"]) for b in valid)
    y1 = max(float(b["y"]) + float(b["height"]) for b in valid)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _is_continuation_paragraph(prev_text: str, next_text: str) -> bool:
    if _ends_complete_sentence(prev_text):
        return False
    prev_words = prev_text.strip().split()
    next_words = next_text.strip().split()
    if not prev_words or not next_words:
        return False
    prev_tail = prev_words[-1]
    next_head = next_words[0]
    known_splits = {
        ("مواجهه", "شغلی"),
        ("مواجه", "ه"),
        ("نمی", "تواند"),
        ("نمی‌تواند", "به"),
        ("می", "تواند"),
        ("می", "شود"),
        ("می", "باشند"),
        ("در", "برابر"),
    }
    if (prev_tail, next_head) in known_splits:
        return True
    if next_head in {
        "شغلی", "شده", "شوند", "گردد", "باشند", "تواند", "شود", "کرد", "کند", "باشد",
    }:
        return True
    return False


def _should_split_before(
    item: dict[str, Any],
    buffer: list[dict[str, Any]],
    structure: PageStructure,
) -> bool:
    if not buffer:
        return False
    idx = int(item["paragraph_index"])
    prev_text = build_repaired_text([p["raw_text"] for p in buffer])
    incomplete = not _ends_complete_sentence(prev_text)
    next_text = repair_ocr_text(item.get("raw_text") or "").strip()
    if incomplete and _is_continuation_paragraph(prev_text, next_text):
        return False
    if incomplete and re.match(r"^(?:که|و|یا|همچنین|اما|لذا|از|در|با|به|این|آن|شغلی)\s", next_text):
        return False
    if idx in structure.topic_boundaries:
        return True
    prev = buffer[-1]
    prev_section = prev.get("section_id")
    curr_section = item.get("section_id")
    if prev_section and curr_section and prev_section != curr_section:
        return True
    prev_y = (prev.get("bbox") or {}).get("y")
    curr_y = (item.get("bbox") or {}).get("y")
    if (
        not incomplete
        and prev_y is not None
        and curr_y is not None
        and (float(curr_y) - float(prev_y)) > TOPIC_Y_GAP
    ):
        return True
    if len(prev_text) >= MIN_CHUNK_CHARS and _ends_complete_sentence(prev_text):
        if next_text.startswith(("منابع", "تعریف", "بخش", "فصل", "مقدمه")):
            return True
    return False


class SemanticTextGenerator:
    """Build traceable narrative chunks — sections come from structure layer, not chunker."""

    def generate_for_page(
        self,
        *,
        page_number: int,
        printed_page_number: int | None,
        paragraphs: list[dict[str, Any]],
        document_id: str,
        source_pdf: str,
        table_ids: list[str] | None = None,
        formula_ids: list[str] | None = None,
        entities: list[dict[str, Any]] | None = None,
        cell_texts: set[str] | None = None,
        table_bboxes: list[dict[str, Any]] | None = None,
        cell_bboxes: list[dict[str, Any]] | None = None,
        table_bbox_by_id: dict[str, dict[str, Any]] | None = None,
        page_structure: PageStructure | None = None,
    ) -> list[dict[str, Any]]:
        table_ids = table_ids or []
        formula_ids = formula_ids or []
        entities = entities or []
        cell_texts = cell_texts or set()
        table_bboxes = table_bboxes or []
        cell_bboxes = cell_bboxes or []
        table_bbox_by_id = table_bbox_by_id or {}

        structure = page_structure or detect_page_structure(paragraphs, page_number=page_number)

        usable: list[dict[str, Any]] = []
        ordered = sort_blocks_reading_order(
            [{"paragraph_index": idx, **paragraph} for idx, paragraph in enumerate(paragraphs)]
        )
        for item in ordered:
            index = int(item["paragraph_index"])
            raw_text = (item.get("text") or "").strip()
            if not raw_text or _is_running_header(raw_text):
                continue
            if index in structure.topic_boundaries and _is_valid_heading_block(raw_text, item):
                repaired = repair_ocr_text(raw_text)
                if not re.search(r"(?:\sکه\s|\sکه\S|،)", repaired):
                    continue
            bbox = item.get("bbox")
            if _paragraph_in_table_region(bbox, table_bboxes, cell_bboxes):
                continue
            if _paragraph_in_table_evidence(raw_text, cell_texts):
                continue
            if looks_like_table_row(repair_ocr_text(raw_text)) or looks_like_table_header(repair_ocr_text(raw_text)):
                continue

            section = section_for_paragraph(structure, index)
            usable.append(
                {
                    "paragraph_index": index,
                    "raw_text": raw_text,
                    "bbox": bbox,
                    "text_evidence_id": f"text_evidence_{page_number:03d}_{index:03d}",
                    "section_id": section.section_id,
                    "section_title": section.title,
                    "section_path": list(section.path),
                }
            )

        if not usable:
            return []

        chunks: list[dict[str, Any]] = []
        buffer: list[dict[str, Any]] = []

        def flush_buffer(*, force: bool = False) -> None:
            nonlocal buffer
            if not buffer:
                return
            raw = build_raw_text([item["raw_text"] for item in buffer])
            repaired = build_repaired_text([item["raw_text"] for item in buffer])
            normalized = normalize_repaired_text(repaired)
            ocr_audit = build_repair_audit([item["raw_text"] for item in buffer])
            if len(normalized) < MIN_CHUNK_CHARS and not force:
                return
            if not force and not _ends_complete_sentence(repaired) and len(repaired) < MAX_CHUNK_CHARS:
                return

            chunk_index = len(chunks) + 1
            chunk_id = f"semantic_{page_number:03d}_{chunk_index:02d}"
            chunk_bbox = _union_bbox([item.get("bbox") for item in buffer])
            section_path = buffer[-1].get("section_path") or []
            section_title = buffer[-1].get("section_title") or ""
            evidence_ids = [item["text_evidence_id"] for item in buffer]
            review_status = "pending"
            validation_issues: list[str] = []
            frag = assess_fragmentation(normalized)
            if frag.has_confirmed:
                review_status = "review_required"
                validation_issues.append("confirmed_ocr_fragmentation")
            elif frag.has_possible or ocr_audit.medium_confidence_count > 0:
                review_status = "review_required"
                validation_issues.append("possible_ocr_fragmentation")
            if not _ends_complete_sentence(repaired):
                review_status = "review_required"
                validation_issues.append("chunk ends mid-sentence")

            chunk = {
                "chunk_id": chunk_id,
                "chunk_type": "semantic_text",
                "raw_text": raw,
                "text": repaired,
                "normalized_text": normalized,
                "semantic_text": None,
                "ocr_repair": ocr_audit.to_dict(),
                "page_numbers": [page_number],
                "pdf_page_number": page_number,
                "printed_page_number": printed_page_number,
                "document_id": document_id,
                "source_pdf": source_pdf,
                "section": {"title": section_title, "path": section_path[-4:]},
                "source_references": {
                    "page_ids": [f"page_{page_number:03d}"],
                    "text_evidence_ids": evidence_ids,
                    "entity_ids": _related_entity_ids(chunk_bbox, entities),
                    "table_ids": _related_table_ids(repaired, table_ids, chunk_bbox, table_bbox_by_id),
                    "formula_ids": _related_formula_ids(repaired, formula_ids),
                },
                "bbox_references": [item["bbox"] for item in buffer if item.get("bbox")],
                "bbox": chunk_bbox,
                "provenance": {
                    "source": "pymupdf_paragraph",
                    "derivation": "evidence_preserving_line_join_and_ocr_repair",
                    "llm_rewritten": False,
                    "document_id": document_id,
                    "source_pdf": source_pdf,
                    "page_number": page_number,
                    "printed_page_number": printed_page_number,
                    "text_evidence_ids": evidence_ids,
                    "bbox": chunk_bbox,
                },
                "semantic_enrichment": None,
                "retrieval_mode": "semantic",
                "embedding_status": "pending",
                "review_status": review_status,
            }
            if validation_issues:
                chunk["validation_issues"] = validation_issues
            chunks.append(chunk)
            buffer = []

        for i, item in enumerate(usable):
            if _should_split_before(item, buffer, structure):
                flush_buffer(force=True)

            buffer.append(item)
            repaired = build_repaired_text([p["raw_text"] for p in buffer])

            if len(repaired) > MAX_CHUNK_CHARS and len(buffer) > 1:
                if not _ends_complete_sentence(build_repaired_text([p["raw_text"] for p in buffer[:-1]])):
                    if _is_continuation_paragraph(
                        build_repaired_text([p["raw_text"] for p in buffer[:-1]]),
                        item.get("raw_text") or "",
                    ):
                        pass
                    else:
                        overflow = buffer.pop()
                        flush_buffer(force=True)
                        buffer = [overflow]
                else:
                    overflow = buffer.pop()
                    flush_buffer(force=True)
                    buffer = [overflow]

            if i + 1 < len(usable) and _should_split_before(usable[i + 1], buffer, structure):
                flush_buffer(force=True)
            elif i + 1 == len(usable):
                flush_buffer(force=True)
            elif _ends_complete_sentence(repaired) and len(repaired) >= MIN_CHUNK_CHARS:
                next_item = usable[i + 1]
                if _should_split_before(next_item, buffer, structure):
                    flush_buffer(force=True)

        flush_buffer(force=True)
        return chunks


def _is_valid_heading_block(raw_text: str, item: dict[str, Any]) -> bool:
    from goldset_generator.page_structure_detector import _is_valid_heading

    return _is_valid_heading(
        raw_text,
        leading_indent=int(item.get("leading_indent") or 0),
        max_font=float(item.get("max_font_size") or 0),
    )
