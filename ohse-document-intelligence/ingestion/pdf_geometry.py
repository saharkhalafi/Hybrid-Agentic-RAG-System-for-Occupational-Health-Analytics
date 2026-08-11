
"""Extract word/line geometry from digital PDF pages via PyMuPDF.

This module is intentionally independent from Document AI.

Document AI provides table structure.
PyMuPDF provides reliable coordinates for digital PDFs.

The geometry resolver consumes this index and returns a normalized
GeometryMatch object that is used by ingestion/geometry_resolver.py.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Iterable

import fitz

from normalization.persian_normalizer import normalize_persian_text


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

MATH_NOISE = re.compile(
    r"\\[a-zA-Z]+|[\^{}~]|\(R\)|\(IFV\)|\(IV\)",
    re.IGNORECASE,
)

CAS_PATTERN = re.compile(r"\[\s*\d{2,7}-\d{2}-\d\s*\]")


def normalize_match_text(value: str) -> str:
    """Normalize text for geometry matching.

    This is NOT the final domain normalization.
    It is only used to determine whether PDF text and extracted text
    refer to the same visual content.
    """

    if not value:
        return ""

    normalized = normalize_persian_text(value).normalized

    normalized = MATH_NOISE.sub(" ", normalized)

    # Normalize common Unicode variants.
    normalized = normalized.replace("ي", "ی")
    normalized = normalized.replace("ى", "ی")
    normalized = normalized.replace("ك", "ک")

    # Persian / Arabic punctuation.
    normalized = normalized.replace("،", " ")
    normalized = normalized.replace(",", " ")
    normalized = normalized.replace("؛", " ")
    normalized = normalized.replace(":", " ")

    # Normalize whitespace.
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip().lower()


def tokenize_match_text(value: str) -> list[str]:
    """Tokenize text for ordered-token matching."""

    normalized = normalize_match_text(value)

    return [
        token
        for token in re.split(r"[\s/]+", normalized)
        if token
    ]


def extract_cas_numbers(value: str) -> list[str]:
    """Extract CAS registry numbers."""

    if not value:
        return []

    return [
        re.sub(r"\s+", "", match)
        for match in CAS_PATTERN.findall(value)
    ]


# ---------------------------------------------------------------------------
# Geometry data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PdfWord:
    text: str
    norm: str

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> dict[str, float]:
        return {
            "x": self.x0,
            "y": self.y0,
            "width": self.x1 - self.x0,
            "height": self.y1 - self.y0,
        }


@dataclass(frozen=True)
class PdfLine:
    y_key: float
    text: str
    norm: str

    bbox: tuple[float, float, float, float]

    words: tuple[PdfWord, ...]


@dataclass(frozen=True)
class GeometryMatch:
    """Normalized result returned by GeometryResolver.

    IMPORTANT:
    geometry_resolver.py relies on these exact attributes.
    """

    bbox: dict[str, float]
    confidence: float
    source: str
    matched_text: str
    method: str


# ---------------------------------------------------------------------------
# BBox helpers
# ---------------------------------------------------------------------------

def union_word_bbox(words: Iterable[PdfWord]) -> dict[str, float]:
    words = list(words)

    if not words:
        raise ValueError("Cannot calculate bbox from empty word list")

    return {
        "x": min(word.x0 for word in words),
        "y": min(word.y0 for word in words),
        "width": max(word.x1 for word in words)
        - min(word.x0 for word in words),
        "height": max(word.y1 for word in words)
        - min(word.y0 for word in words),
    }


def rect_to_bbox(rect: fitz.Rect) -> dict[str, float]:
    return {
        "x": float(rect.x0),
        "y": float(rect.y0),
        "width": float(rect.width),
        "height": float(rect.height),
    }


def union_rect_bbox(rects: list[fitz.Rect]) -> dict[str, float]:
    if not rects:
        raise ValueError("Cannot calculate bbox from empty rectangles")

    result = fitz.Rect(rects[0])

    for rect in rects[1:]:
        result |= fitz.Rect(rect)

    return rect_to_bbox(result)


# ---------------------------------------------------------------------------
# Page geometry index
# ---------------------------------------------------------------------------

class PageGeometryIndex:
    """Word and line geometry index for one PDF page."""

    def __init__(self, page: fitz.Page) -> None:
        self.page = page
        self.page_number = page.number + 1

        self.page_width = float(page.rect.width)
        self.page_height = float(page.rect.height)

        self.words = self._extract_words()
        self.lines = self._extract_lines()

    # ------------------------------------------------------------------
    # Word extraction
    # ------------------------------------------------------------------

    def _extract_words(self) -> list[PdfWord]:
        words: list[PdfWord] = []

        raw_words = self.page.get_text("words")

        for item in raw_words:
            if len(item) < 5:
                continue

            x0, y0, x1, y1, text = item[:5]

            if not text:
                continue

            norm = normalize_match_text(text)

            if not norm:
                continue

            words.append(
                PdfWord(
                    text=text,
                    norm=norm,
                    x0=float(x0),
                    y0=float(y0),
                    x1=float(x1),
                    y1=float(y1),
                )
            )

        return words

    # ------------------------------------------------------------------
    # Line extraction
    # ------------------------------------------------------------------

    def _extract_lines(self) -> list[PdfLine]:
        lines: list[PdfLine] = []

        page_dict = self.page.get_text("dict")

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])

                text = "".join(
                    span.get("text", "")
                    for span in spans
                ).strip()

                if not text:
                    continue

                bbox = line.get("bbox")

                if not bbox or len(bbox) != 4:
                    continue

                norm = normalize_match_text(text)

                if not norm:
                    continue

                y0 = float(bbox[1])
                y1 = float(bbox[3])

                y_key = round(y0 / 2) * 2

                line_words = tuple(
                    word
                    for word in self.words
                    if (
                        abs(word.y0 - y0) <= 4
                        or abs(word.y1 - y1) <= 4
                        or (
                            word.y0 <= y1
                            and word.y1 >= y0
                        )
                    )
                )

                line_words = tuple(
                    sorted(
                        line_words,
                        key=lambda word: word.x0,
                    )
                )

                lines.append(
                    PdfLine(
                        y_key=y_key,
                        text=text,
                        norm=norm,
                        bbox=(
                            float(bbox[0]),
                            float(bbox[1]),
                            float(bbox[2]),
                            float(bbox[3]),
                        ),
                        words=line_words,
                    )
                )

        return lines

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def words_by_line(self) -> dict[float, list[PdfWord]]:
        grouped: dict[float, list[PdfWord]] = {}

        for word in self.words:
            y_key = round(word.y0 / 2) * 2

            grouped.setdefault(y_key, []).append(word)

        for words in grouped.values():
            words.sort(key=lambda item: item.x0)

        return grouped


# ---------------------------------------------------------------------------
# Geometry Resolver
# ---------------------------------------------------------------------------

class GeometryResolver:
    """Resolve extracted text to PDF coordinates.

    Matching cascade:

    1. Exact normalized line match
    2. Exact normalized word-sequence match
    3. Ordered token sequence
    4. CAS number match
    5. Fuzzy line match

    Confidence values intentionally represent matching confidence,
    not OCR confidence.
    """

    EXACT_LINE_CONFIDENCE = 0.98
    TOKEN_CONFIDENCE = 0.92
    CAS_CONFIDENCE = 0.95
    FUZZY_MIN_CONFIDENCE = 0.85

    def __init__(
        self,
        page: fitz.Page,
        *,
        fuzzy_threshold: float = FUZZY_MIN_CONFIDENCE,
    ) -> None:
        self.page = page
        self.index = PageGeometryIndex(page)
        self.fuzzy_threshold = fuzzy_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, text: str) -> GeometryMatch | None:
        """Resolve text to a PDF bbox."""

        if not text or not text.strip():
            return None

        normalized = normalize_match_text(text)

        if not normalized:
            return None

        # 1. Exact line
        result = self._match_exact_line(normalized)

        if result:
            return result

        # 2. Token sequence
        result = self._match_token_sequence(normalized)

        if result:
            return result

        # 3. CAS
        result = self._match_cas(text)

        if result:
            return result

        # 4. Fuzzy
        return self._match_fuzzy(normalized)

    # ------------------------------------------------------------------
    # Exact line
    # ------------------------------------------------------------------

    def _match_exact_line(
        self,
        normalized: str,
    ) -> GeometryMatch | None:

        for line in self.index.lines:

            if line.norm == normalized:

                return GeometryMatch(
                    bbox=rect_to_bbox(
                        fitz.Rect(line.bbox)
                    ),
                    confidence=self.EXACT_LINE_CONFIDENCE,
                    source="pymupdf",
                    matched_text=line.text,
                    method="exact_line",
                )

        return None

    # ------------------------------------------------------------------
    # Token sequence
    # ------------------------------------------------------------------

    def _match_token_sequence(
        self,
        normalized: str,
    ) -> GeometryMatch | None:

        target_tokens = tokenize_match_text(normalized)

        if not target_tokens:
            return None

        best: GeometryMatch | None = None

        for line in self.index.lines:

            line_tokens = tokenize_match_text(line.text)

            if not line_tokens:
                continue

            if len(target_tokens) > len(line_tokens):
                continue

            for start in range(
                0,
                len(line_tokens) - len(target_tokens) + 1,
            ):
                window = line_tokens[
                    start:start + len(target_tokens)
                ]

                if window != target_tokens:
                    continue

                words = list(line.words)

                if not words:
                    continue

                matched_words = self._select_words_for_tokens(
                    words,
                    target_tokens,
                )

                if not matched_words:
                    continue

                bbox = union_word_bbox(matched_words)

                candidate = GeometryMatch(
                    bbox=bbox,
                    confidence=self.TOKEN_CONFIDENCE,
                    source="pymupdf",
                    matched_text=line.text,
                    method="ordered_token_sequence",
                )

                best = candidate
                break

            if best:
                break

        return best

    # ------------------------------------------------------------------
    # CAS matching
    # ------------------------------------------------------------------

    def _match_cas(
        self,
        text: str,
    ) -> GeometryMatch | None:

        target_cas = extract_cas_numbers(text)

        if not target_cas:
            return None

        for cas in target_cas:

            cas_normalized = normalize_match_text(cas)

            for line in self.index.lines:

                if cas_normalized not in line.norm:
                    continue

                matched_words = [
                    word
                    for word in line.words
                    if cas_normalized in word.norm
                    or self._cas_digits_match(
                        word.norm,
                        cas_normalized,
                    )
                ]

                if matched_words:

                    return GeometryMatch(
                        bbox=union_word_bbox(matched_words),
                        confidence=self.CAS_CONFIDENCE,
                        source="pymupdf",
                        matched_text=line.text,
                        method="cas_match",
                    )

                return GeometryMatch(
                    bbox=rect_to_bbox(
                        fitz.Rect(line.bbox)
                    ),
                    confidence=self.CAS_CONFIDENCE,
                    source="pymupdf",
                    matched_text=line.text,
                    method="cas_line_match",
                )

        return None

    # ------------------------------------------------------------------
    # Fuzzy
    # ------------------------------------------------------------------

    def _match_fuzzy(
        self,
        normalized: str,
    ) -> GeometryMatch | None:

        best_ratio = 0.0
        best_line: PdfLine | None = None

        for line in self.index.lines:

            ratio = difflib.SequenceMatcher(
                None,
                normalized,
                line.norm,
            ).ratio()

            if ratio > best_ratio:
                best_ratio = ratio
                best_line = line

        if (
            best_line is None
            or best_ratio < self.fuzzy_threshold
        ):
            return None

        return GeometryMatch(
            bbox=rect_to_bbox(
                fitz.Rect(best_line.bbox)
            ),
            confidence=round(best_ratio, 4),
            source="pymupdf",
            matched_text=best_line.text,
            method="fuzzy_line",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _select_words_for_tokens(
        words: list[PdfWord],
        target_tokens: list[str],
    ) -> list[PdfWord] | None:

        if not words:
            return None

        selected: list[PdfWord] = []

        target_index = 0

        for word in words:

            if target_index >= len(target_tokens):
                break

            if word.norm == target_tokens[target_index]:

                selected.append(word)
                target_index += 1

        if target_index != len(target_tokens):
            return None

        return selected

    @staticmethod
    def _cas_digits_match(
        value: str,
        cas: str,
    ) -> bool:

        value_digits = re.sub(r"\D", "", value)
        cas_digits = re.sub(r"\D", "", cas)

        if not value_digits or not cas_digits:
            return False

        return value_digits == cas_digits

