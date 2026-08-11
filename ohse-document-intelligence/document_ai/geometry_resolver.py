"""Resolve table cell bounding boxes by matching Document AI text to PDF geometry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import fitz

from ingestion.pdf_geometry import (
    PageGeometryIndex,
    PdfWord,
    normalize_match_text,
    rect_to_bbox,
    tokenize_match_text,
    union_word_bbox,
)

CAS_PATTERN = re.compile(r"\[\d+-\d+-\d+\]")
ENGLISH_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9\- ]{3,}")
PERSIAN_SUFFIX_TOKENS = {"ها", "های", "تر", "ترین"}


def compact_match_text(value: str) -> str:
    return normalize_match_text(value).replace(" ", "")


def significant_tokens(value: str) -> list[str]:
    tokens = tokenize_match_text(value)
    return [token for token in tokens if token not in PERSIAN_SUFFIX_TOKENS and len(token) > 1]

BBOX_SOURCE_DOCUMENT_AI = "document_ai"
BBOX_SOURCE_PYMUPDF = "pymupdf"
BBOX_SOURCE_OCR_OVERLAY = "ocr_overlay"

MATCH_EXACT = "exact"
MATCH_LINE = "line"
MATCH_TOKENS = "tokens"
MATCH_PARTIAL = "partial"
MATCH_CAS = "cas"
MATCH_TOKEN = "token"
MATCH_NONE = "none"


@dataclass(frozen=True)
class GeometryCellInput:
    page_number: int
    cell_text: str
    table_id: str
    row_index: int | None = None
    column_index: int | None = None


@dataclass
class GeometryMatchResult:
    bbox: dict[str, float] | None
    match_confidence: float
    bbox_source: str = BBOX_SOURCE_PYMUPDF
    match_method: str = MATCH_NONE
    source_reference: dict[str, Any] = field(default_factory=dict)
    matched_words: list[str] = field(default_factory=list)


def is_valid_bbox(bbox: dict[str, Any] | None) -> bool:
    if not bbox:
        return False
    if any(key in bbox for key in ("bounding_poly", "normalized_vertices", "bounding_box")):
        return True
    width = bbox.get("width")
    height = bbox.get("height")
    return (
        bbox.get("x") is not None
        and bbox.get("y") is not None
        and width is not None
        and height is not None
        and float(width) > 0
        and float(height) > 0
    )


def document_ai_bbox_to_xywh(bbox: dict[str, Any]) -> dict[str, float] | None:
    bounding_box = bbox.get("bounding_box") or bbox.get("boundingBox")
    if bounding_box:
        return {
            "x": float(bounding_box.get("x") or bounding_box.get("left") or 0),
            "y": float(bounding_box.get("y") or bounding_box.get("top") or 0),
            "width": float(bounding_box.get("width") or 0),
            "height": float(bounding_box.get("height") or 0),
        }
    return None


class GeometryResolver:
    """Match normalized Document AI cell text to PyMuPDF word geometry."""

    def __init__(
        self,
        pdf_path: Path,
        *,
        document_path: str | None = None,
        min_token_length: int = 4,
    ) -> None:
        self.pdf_path = pdf_path
        self.document_path = document_path or str(pdf_path)
        self.min_token_length = min_token_length
        self._pdf: fitz.Document | None = None
        self._page_cache: dict[int, PageGeometryIndex] = {}

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None
        self._page_cache.clear()

    def __enter__(self) -> GeometryResolver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get_page_index(self, page_number: int) -> PageGeometryIndex:
        if page_number not in self._page_cache:
            if self._pdf is None:
                self._pdf = fitz.open(self.pdf_path)
            page = self._pdf.load_page(page_number - 1)
            self._page_cache[page_number] = PageGeometryIndex(page)
        return self._page_cache[page_number]

    def resolve(
        self,
        cell: GeometryCellInput,
        *,
        document_ai_bbox: dict[str, Any] | None = None,
    ) -> GeometryMatchResult:
        if document_ai_bbox and is_valid_bbox(document_ai_bbox):
            converted = document_ai_bbox_to_xywh(document_ai_bbox) or document_ai_bbox
            if is_valid_bbox(converted):
                return GeometryMatchResult(
                    bbox=converted,
                    match_confidence=1.0,
                    bbox_source=BBOX_SOURCE_DOCUMENT_AI,
                    match_method=MATCH_EXACT,
                    source_reference=self._source_reference(
                        cell,
                        match_method=MATCH_EXACT,
                        matched_text=cell.cell_text,
                    ),
                    matched_words=[cell.cell_text] if cell.cell_text else [],
                )

        text = cell.cell_text.strip()
        if not text:
            return GeometryMatchResult(
                bbox=None,
                match_confidence=0.0,
                match_method=MATCH_NONE,
                source_reference=self._source_reference(cell, match_method=MATCH_NONE),
            )

        page_index = self._get_page_index(cell.page_number)
        match = self._match_on_page(page_index, text)
        match.source_reference = self._source_reference(
            cell,
            match_method=match.match_method,
            matched_text=text,
            matched_words=match.matched_words,
            page_width=page_index.page_width,
            page_height=page_index.page_height,
        )
        return match

    def _source_reference(
        self,
        cell: GeometryCellInput,
        *,
        match_method: str,
        matched_text: str | None = None,
        matched_words: list[str] | None = None,
        page_width: float | None = None,
        page_height: float | None = None,
    ) -> dict[str, Any]:
        reference: dict[str, Any] = {
            "document_path": self.document_path,
            "page_number": cell.page_number,
            "table_id": cell.table_id,
            "match_method": match_method,
        }
        if cell.row_index is not None:
            reference["row_index"] = cell.row_index
        if cell.column_index is not None:
            reference["column_index"] = cell.column_index
        if matched_text is not None:
            reference["matched_text"] = matched_text
        if matched_words:
            reference["matched_words"] = matched_words
        if page_width is not None:
            reference["page_width"] = page_width
        if page_height is not None:
            reference["page_height"] = page_height
        return reference

    def _match_on_page(self, page_index: PageGeometryIndex, text: str) -> GeometryMatchResult:
        page = page_index.page

        for candidate in (text, normalize_match_text(text), compact_match_text(text)):
            if not candidate:
                continue
            rects = page.search_for(candidate)
            if rects:
                bbox = rect_to_bbox(rects[0])
                if len(rects) > 1:
                    combined = fitz.Rect(rects[0])
                    for rect in rects[1:]:
                        combined |= rect
                    bbox = rect_to_bbox(combined)
                return GeometryMatchResult(
                    bbox=bbox,
                    match_confidence=0.98,
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_EXACT,
                    matched_words=[candidate],
                )

        cas_numbers = CAS_PATTERN.findall(text)
        if len(cas_numbers) > 1:
            rects: list[fitz.Rect] = []
            for cas_number in cas_numbers:
                rects.extend(page.search_for(cas_number))
            if rects:
                combined = fitz.Rect(rects[0])
                for rect in rects[1:]:
                    combined |= rect
                return GeometryMatchResult(
                    bbox=rect_to_bbox(combined),
                    match_confidence=0.95,
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_CAS,
                    matched_words=cas_numbers,
                )

        for english_name in ENGLISH_NAME_PATTERN.findall(text):
            name = english_name.strip()
            if len(name) < 4:
                continue
            rects = page.search_for(name)
            if rects:
                return GeometryMatchResult(
                    bbox=rect_to_bbox(rects[0]),
                    match_confidence=0.92,
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_EXACT,
                    matched_words=[name],
                )

        normalized = normalize_match_text(text)
        if len(normalized) >= 3:
            for line in page_index.lines:
                if normalized in line.norm or line.norm in normalized:
                    bbox = line.bbox
                    return GeometryMatchResult(
                        bbox={
                            "x": bbox[0],
                            "y": bbox[1],
                            "width": bbox[2] - bbox[0],
                            "height": bbox[3] - bbox[1],
                        },
                        match_confidence=0.92,
                        bbox_source=BBOX_SOURCE_PYMUPDF,
                        match_method=MATCH_LINE,
                        matched_words=[line.text],
                    )

        tokens = significant_tokens(text)
        token_match = self._match_token_sequence(page_index, tokens)
        if token_match is not None:
            return token_match

        cas_match = CAS_PATTERN.search(text)
        if cas_match:
            rects = page.search_for(cas_match.group())
            if rects:
                return GeometryMatchResult(
                    bbox=rect_to_bbox(rects[0]),
                    match_confidence=0.95,
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_CAS,
                    matched_words=[cas_match.group()],
                )

        compact = compact_match_text(text)
        if len(compact) >= 4:
            best_ratio = 0.0
            best_word = None
            for word in page_index.words:
                word_compact = compact_match_text(word.text)
                if not word_compact:
                    continue
                ratio = SequenceMatcher(None, compact, word_compact).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_word = word
            if best_word is not None and best_ratio >= 0.82:
                return GeometryMatchResult(
                    bbox={
                        "x": best_word.x0,
                        "y": best_word.y0,
                        "width": best_word.x1 - best_word.x0,
                        "height": best_word.y1 - best_word.y0,
                    },
                    match_confidence=round(min(0.92, 0.85 + (best_ratio - 0.82) * 0.5), 3),
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_TOKEN,
                    matched_words=[best_word.text],
                )
            for word in page_index.words:
                word_compact = compact_match_text(word.text)
                if compact in word_compact or word_compact in compact:
                    return GeometryMatchResult(
                        bbox={
                            "x": word.x0,
                            "y": word.y0,
                            "width": word.x1 - word.x0,
                            "height": word.y1 - word.y0,
                        },
                        match_confidence=0.88,
                        bbox_source=BBOX_SOURCE_PYMUPDF,
                        match_method=MATCH_TOKEN,
                        matched_words=[word.text],
                    )

        for token in sorted(tokens, key=len, reverse=True):
            if len(token) < 4:
                continue
            hits = [
                word
                for word in page_index.words
                if word.norm == token
                or token in word.norm
                or word.norm in token
                or compact_match_text(word.text) == compact_match_text(token)
            ]
            if hits:
                return GeometryMatchResult(
                    bbox=union_word_bbox(hits[:5]),
                    match_confidence=0.85,
                    bbox_source=BBOX_SOURCE_PYMUPDF,
                    match_method=MATCH_TOKEN,
                    matched_words=[word.text for word in hits[:5]],
                )

        return GeometryMatchResult(
            bbox=None,
            match_confidence=0.0,
            bbox_source=BBOX_SOURCE_PYMUPDF,
            match_method=MATCH_NONE,
        )

    def _match_token_sequence(
        self,
        page_index: PageGeometryIndex,
        tokens: list[str],
    ) -> GeometryMatchResult | None:
        if not tokens:
            return None

        best: GeometryMatchResult | None = None
        for line_words in page_index.words_by_line().values():
            ordered = sorted(line_words, key=lambda word: -word.x0)
            norms = [word.norm for word in ordered]
            token_count = len(tokens)

            for start in range(len(norms)):
                matched_words: list[PdfWord] = []
                token_index = 0
                for word_index in range(start, len(norms)):
                    if token_index >= token_count:
                        break
                    left = norms[word_index]
                    right = tokens[token_index]
                    left_compact = left.replace(" ", "")
                    right_compact = right.replace(" ", "")
                    if (
                        left == right
                        or right in left
                        or left in right
                        or left_compact == right_compact
                        or right_compact in left_compact
                        or left_compact in right_compact
                    ):
                        matched_words.append(ordered[word_index])
                        token_index += 1

                if token_index == token_count and matched_words:
                    confidence = 0.98 if token_count >= 2 else 0.9
                    candidate = GeometryMatchResult(
                        bbox=union_word_bbox(matched_words),
                        match_confidence=confidence,
                        bbox_source=BBOX_SOURCE_PYMUPDF,
                        match_method=MATCH_TOKENS,
                        matched_words=[word.text for word in matched_words],
                    )
                    if best is None or candidate.match_confidence > best.match_confidence:
                        best = candidate
                elif matched_words and token_index >= max(1, int(token_count * 0.6)):
                    confidence = 0.85 * (token_index / token_count)
                    candidate = GeometryMatchResult(
                        bbox=union_word_bbox(matched_words),
                        match_confidence=confidence,
                        bbox_source=BBOX_SOURCE_PYMUPDF,
                        match_method=MATCH_PARTIAL,
                        matched_words=[word.text for word in matched_words],
                    )
                    if best is None or candidate.match_confidence > best.match_confidence:
                        best = candidate

        return best
