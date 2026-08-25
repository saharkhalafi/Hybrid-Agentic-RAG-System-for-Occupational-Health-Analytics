"""Extract word/line geometry from digital PDF pages via PyMuPDF.

This module is intentionally independent from Document AI matching logic.
Document AI provides table structure; PyMuPDF provides reliable coordinates
for digital PDFs. Geometry matching lives in ``document_ai.geometry_resolver``.
"""

from __future__ import annotations

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

LATEX_CDOT_OVER_GREEK = re.compile(r"\\cdot\s*/\s*\\(?:Delta|pi)\b", re.IGNORECASE)
LATEX_MG_M3_BRACE = re.compile(r"mg\s*/\s*m\s*\^\{\s*3(?:\([^)]*\))?\s*\}", re.IGNORECASE)
F_ML_FRAGMENT = re.compile(r"\bf\s*/\s*ml\b", re.IGNORECASE)

CAS_PATTERN = re.compile(r"\[\s*\d{2,7}-\d{2}-\d\s*\]")
REVERSED_CAS_OCR_PATTERN = re.compile(r"(\d)\]\s*-\s*(\d{2,7})\s*-\s*\[(\d{2,7})")


def repair_reversed_cas_brackets(value: str) -> str:
    """Repair OCR-reversed CAS brackets such as ``9] - 89 - [58`` -> ``[58-89-9]``."""

    def _replace(match: re.Match[str]) -> str:
        return f"[{match.group(3)}-{match.group(2)}-{match.group(1)}]"

    return REVERSED_CAS_OCR_PATTERN.sub(_replace, value)


def normalize_latex_limit_search_text(value: str) -> str:
    """Normalize LaTeX limit fragments for geometry search only."""
    if not value:
        return ""
    text = value
    text = LATEX_CDOT_OVER_GREEK.sub(" ", text)
    text = LATEX_MG_M3_BRACE.sub("mg/m3", text)
    text = re.sub(r"~+", " ", text)
    text = F_ML_FRAGMENT.sub("f/ml", text)
    text = re.sub(r"(\d)\s*mg/m3(?:\([^)]*\))?", r"\1 mg/m3", text, flags=re.IGNORECASE)
    return text


def normalize_match_text(value: str) -> str:
    """Normalize text for geometry matching.

    This is NOT the final domain normalization.
    It is only used to determine whether PDF text and extracted text
    refer to the same visual content.
    """

    if not value:
        return ""

    normalized = normalize_persian_text(value).normalized

    normalized = normalize_latex_limit_search_text(normalized)
    normalized = MATH_NOISE.sub(" ", normalized)
    normalized = re.sub(r"mg\s*/\s*m\s*3", "mg/m3", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(\d)\s*mg/m3", r"\1 mg/m3", normalized, flags=re.IGNORECASE)

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

    repaired = repair_reversed_cas_brackets(value)
    return [
        re.sub(r"\s+", "", match)
        for match in CAS_PATTERN.findall(repaired)
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


def bbox_y_center(bbox: dict[str, float]) -> float:
    return float(bbox["y"]) + float(bbox["height"]) / 2.0


def line_y_center(line: PdfLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2.0


NUMERIC_ONLY_PATTERN = re.compile(r"^[\d\s./\\\-،,٫٪%]+$", re.IGNORECASE)

SHORT_LIMIT_TOKENS = frozenset(
    {
        "twa",
        "stel",
        "c",
        "ppm",
        "ceiling",
        "mg/m3",
        "mg/m³",
        "ifv",
        "iv",
        "r",
    }
)


def is_numeric_only_text(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    if not NUMERIC_ONLY_PATTERN.match(text):
        return False
    return any(char.isdigit() for char in text)


def is_short_limit_token(value: str) -> bool:
    normalized = normalize_match_text(value)
    if not normalized:
        return False
    tokens = [token for token in re.split(r"[\s/]+", normalized) if token]
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in SHORT_LIMIT_TOKENS
    return all(token in SHORT_LIMIT_TOKENS for token in tokens)


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

                x0, y0, x1, y1 = map(float, bbox)

                line_center_y = (y0 + y1) / 2.0

                # Only attach words whose vertical center belongs
                # to this actual PDF line.
                line_words = tuple(
                    sorted(
                        (
                            word
                            for word in self.words
                            if (
                                y0 - 2.0
                                <= (word.y0 + word.y1) / 2.0
                                <= y1 + 2.0
                            )
                        ),
                        key=lambda word: word.x0,
                    )
                )

                lines.append(
                    PdfLine(
                        y_key=round(line_center_y / 2) * 2,
                        text=text,
                        norm=norm,
                        bbox=(x0, y0, x1, y1),
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
