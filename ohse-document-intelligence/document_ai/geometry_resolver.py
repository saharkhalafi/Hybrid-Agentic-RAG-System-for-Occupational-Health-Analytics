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
    PdfLine,
    PdfWord,
    bbox_y_center,
    extract_cas_numbers,
    is_numeric_only_text,
    is_short_limit_token,
    line_y_center,
    normalize_match_text,
    rect_to_bbox,
    tokenize_match_text,
    union_word_bbox,
)

from document_ai.geometry_search import search_query_variants

from document_ai.geometry_cell_ordering import OEL_ROW_NUMBER_COLUMN

# NOTE: tolerates internal spacing around the hyphens too (e.g.
# "[ 100 - 42 - 5 ]"), consistent with the CAS_PATTERN used in
# goldset_generator.structural_resolver and
# ingestion.merged_row_splitter. OCR/PDF extraction can introduce
# whitespace anywhere inside the bracketed number, not just next to
# the brackets.
CAS_PATTERN = re.compile(
    r"\[\s*\d{2,7}\s*-\s*\d{2}\s*-\s*\d\s*\]"
)
ENGLISH_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9\- ]{3,}")
PERSIAN_SUFFIX_TOKENS = {"ها", "های", "تر", "ترین"}

FUZZY_LINE_MIN_CONFIDENCE = 0.85
FUZZY_WORD_MIN_RATIO = 0.82

ROW_Y_TOLERANCE = 18.0
ROW_Y_SOFT_TOLERANCE = 28.0
MAX_ROW_BBOX_HEIGHT = 25.0
AMBIGUITY_SCORE_MARGIN = 0.08
HEADER_ROW_INDEX = 0
SEARCH_FOR_MAX_CONFIDENCE = 0.97
PAGE_TOP_EXCLUDE_RATIO = 0.04
HEADER_BAND_PADDING = 12.0

# ----------------------------------------------------------------------
# CAS-vs-token priority tuning.
#
# A cell like "Styrene [ 100-42-5 ]" must resolve against the CAS
# number's own geometry, not against a generic token-sequence match on
# the digit fragments "100", "42", "5". Token-sequence candidates can
# reach base_confidence 0.98 (see _candidates_token_sequence), which
# used to beat CAS candidates outright (0.94-0.95). CAS_PRIORITY_BONUS
# / TOKEN_CAS_PENALTY are applied in _score_candidate() whenever the
# cell text itself contains a CAS number, so CAS-generator candidates
# reliably win that comparison.
# ----------------------------------------------------------------------

CAS_PRIORITY_BONUS = 0.06
TOKEN_CAS_PENALTY = 0.06

HEADER_STRUCTURE_TOKENS = frozenset(
    {
        "twa",
        "stel",
        "ceiling",
        "ppm",
        "ردیف",
        "مواجهه",
        "وزن",
        "ملکولی",
        "mg/m3",
        "mg/m³",
        "symbol",
        "نماد",
        "scientific",
        "علمی",
    }
)

WEAK_ANCHOR_GENERATORS = frozenset({"fuzzy_word", "substring_word", "partial_token_sequence"})

CAS_GENERATORS = frozenset({"cas_line_words", "cas_line", "cas_search_for"})
ENGLISH_NAME_GENERATORS = frozenset({"english_name"})

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


def compact_match_text(value: str) -> str:
    return normalize_match_text(value).replace(" ", "")


def significant_tokens(value: str) -> list[str]:
    tokens = tokenize_match_text(value)
    return [token for token in tokens if token not in PERSIAN_SUFFIX_TOKENS and len(token) > 1]


def _text_has_cas(text: str) -> bool:
    """
    Single check for "does this cell text contain a CAS number", used to
    decide whether to apply CAS-vs-token priority scoring. Uses the same
    extraction path as candidate generation (extract_cas_numbers with a
    CAS_PATTERN fallback) so the two never disagree.
    """

    return bool(
        extract_cas_numbers(text) or CAS_PATTERN.findall(text)
    )


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


@dataclass(frozen=True)
class _RowAnchor:
    column_index: int | None
    y_center: float


@dataclass(frozen=True)
class _GeometryCandidate:
    bbox: dict[str, float]
    match_method: str
    base_confidence: float
    matched_words: list[str]
    y_center: float
    generator: str


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


def document_ai_bbox_to_xywh(
    bbox: dict[str, Any],
) -> dict[str, float] | None:
    """Convert supported Document AI bbox representations to xywh."""

    if not bbox:
        return None

    # Already in our canonical xywh format
    if all(
        bbox.get(key) is not None
        for key in ("x", "y", "width", "height")
    ):
        width = float(bbox["width"])
        height = float(bbox["height"])

        if width > 0 and height > 0:
            return {
                "x": float(bbox["x"]),
                "y": float(bbox["y"]),
                "width": width,
                "height": height,
            }

    # Document AI / API bounding box object
    bounding_box = bbox.get("bounding_box") or bbox.get("boundingBox")

    if bounding_box:
        x = bounding_box.get("x", bounding_box.get("left"))
        y = bounding_box.get("y", bounding_box.get("top"))
        width = bounding_box.get("width")
        height = bounding_box.get("height")

        if (
            x is not None
            and y is not None
            and width is not None
            and height is not None
        ):
            width = float(width)
            height = float(height)

            if width > 0 and height > 0:
                return {
                    "x": float(x),
                    "y": float(y),
                    "width": width,
                    "height": height,
                }

    # Polygon / vertices
    vertices = (
        bbox.get("normalized_vertices")
        or bbox.get("normalizedVertices")
        or bbox.get("vertices")
    )

    if vertices:
        xs = []
        ys = []

        for vertex in vertices:
            if vertex.get("x") is not None:
                xs.append(float(vertex["x"]))
            if vertex.get("y") is not None:
                ys.append(float(vertex["y"]))

        if xs and ys:
            x0 = min(xs)
            y0 = min(ys)
            x1 = max(xs)
            y1 = max(ys)

            if x1 > x0 and y1 > y0:
                return {
                    "x": x0,
                    "y": y0,
                    "width": x1 - x0,
                    "height": y1 - y0,
                }

    return None


def document_ai_y_hint(bbox: dict[str, Any] | None) -> float | None:
    if not bbox:
        return None

    converted = document_ai_bbox_to_xywh(bbox)

    if not converted:
        return None

    height = float(converted.get("height", 0))

    if height <= 0:
        return None

    return float(converted["y"]) + height / 2.0


class GeometryResolver:
    """Match normalized Document AI cell text to PyMuPDF word geometry."""

    def __init__(
        self,
        pdf_path: Path,
        *,
        document_path: str | None = None,
        min_token_length: int = 4,
        fuzzy_line_threshold: float = FUZZY_LINE_MIN_CONFIDENCE,
    ) -> None:
        self.pdf_path = pdf_path
        self.document_path = document_path or str(pdf_path)
        self.min_token_length = min_token_length
        self.fuzzy_line_threshold = fuzzy_line_threshold
        self._pdf: fitz.Document | None = None
        self._page_cache: dict[int, PageGeometryIndex] = {}
        self._row_anchors: dict[tuple[int, str, int], list[_RowAnchor]] = {}

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None
        self._page_cache.clear()
        self._row_anchors.clear()

    def __enter__(self) -> GeometryResolver:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def clear_table_context(self, page_number: int, table_id: str) -> None:
        prefix = (page_number, table_id)
        for key in list(self._row_anchors):
            if key[:2] == prefix:
                del self._row_anchors[key]

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
        bbox_provenance: str | None = None,
    ) -> GeometryMatchResult:
        trusted_document_ai = (
            bbox_provenance == BBOX_SOURCE_DOCUMENT_AI
            and document_ai_bbox
            and is_valid_bbox(document_ai_bbox)
        )
        if trusted_document_ai:
            converted = document_ai_bbox_to_xywh(document_ai_bbox)
            if is_valid_bbox(converted):
                result = GeometryMatchResult(
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
                self._record_row_anchor(cell, converted)
                return result

        text = cell.cell_text.strip()
        if not text:
            return GeometryMatchResult(
                bbox=None,
                match_confidence=0.0,
                match_method=MATCH_NONE,
                source_reference=self._source_reference(cell, match_method=MATCH_NONE),
            )

        page_index = self._get_page_index(cell.page_number)
        da_y_hint = (
            document_ai_y_hint(document_ai_bbox)
            if bbox_provenance == BBOX_SOURCE_DOCUMENT_AI
            else None
        )
        header_band = self._effective_header_band(cell.page_number, cell.table_id, page_index)
        row_y_target = self._row_y_target(cell, da_y_hint)

        candidates = self._generate_candidates(
            page_index,
            cell,
            text,
            row_y_target=row_y_target,
            header_band=header_band,
            page_height=page_index.page_height,
        )
        selected, final_confidence, scored_candidates = self._select_candidate(
            candidates,
            cell,
            text,
            row_y_target=row_y_target,
            header_band=header_band,
            page_height=page_index.page_height,
        )

        if selected is None:
            match = GeometryMatchResult(
                bbox=None,
                match_confidence=0.0,
                bbox_source=BBOX_SOURCE_PYMUPDF,
                match_method=MATCH_NONE,
            )
        else:
            match = GeometryMatchResult(
                bbox=selected.bbox,
                match_confidence=round(final_confidence, 4),
                bbox_source=BBOX_SOURCE_PYMUPDF,
                match_method=selected.match_method,
                matched_words=selected.matched_words,
            )
            if self._can_record_row_anchor(
                cell,
                selected.bbox,
                selected,
                text,
                row_y_target=row_y_target,
                header_band=header_band,
                scored_candidates=scored_candidates,
                page_height=page_index.page_height,
            ):
                self._record_row_anchor(cell, selected.bbox)

        match.source_reference = self._source_reference(
            cell,
            match_method=match.match_method,
            matched_text=text,
            matched_words=match.matched_words,
            page_width=page_index.page_width,
            page_height=page_index.page_height,
            extra={
                "candidate_generator": selected.generator if selected else None,
                "row_y_target": row_y_target,
            },
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
        extra: dict[str, Any] | None = None,
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
        if extra:
            reference.update({key: value for key, value in extra.items() if value is not None})
        return reference

    def _record_row_anchor(self, cell: GeometryCellInput, bbox: dict[str, float] | None) -> None:
        if bbox is None or cell.row_index is None:
            return
        key = (cell.page_number, cell.table_id, cell.row_index)
        anchor = _RowAnchor(column_index=cell.column_index, y_center=bbox_y_center(bbox))
        anchors = self._row_anchors.setdefault(key, [])
        if cell.column_index is not None:
            anchors[:] = [item for item in anchors if item.column_index != cell.column_index]
        anchors.append(anchor)

    def _has_same_row_peer_anchors(self, cell: GeometryCellInput) -> bool:
        if cell.row_index is None:
            return False
        key = (cell.page_number, cell.table_id, cell.row_index)
        return bool(self._row_anchors.get(key))

    @staticmethod
    def _line_looks_like_table_header(line: PdfLine) -> bool:
        line_tokens = set(tokenize_match_text(line.norm))
        if line_tokens & HEADER_STRUCTURE_TOKENS:
            return True
        return ("mg/m" in line.norm
    or "مواجه" in line.norm
    or "symbol" in line.norm)

    def _infer_structural_header_band(self, page_index: PageGeometryIndex) -> tuple[float, float] | None:
        header_lines = [
            (line_y_center(line), line)
            for line in page_index.lines
            if self._line_looks_like_table_header(line)
        ]
        if not header_lines:
            return None

        header_lines.sort(key=lambda item: item[0])
        clusters: list[list[tuple[float, PdfLine]]] = []
        for y, line in header_lines:
            placed = False
            for cluster in clusters:
                cluster_y = sum(item[0] for item in cluster) / len(cluster)
                if abs(y - cluster_y) <= ROW_Y_SOFT_TOLERANCE:
                    cluster.append((y, line))
                    placed = True
                    break
            if not placed:
                clusters.append([(y, line)])

        def cluster_score(cluster: list[tuple[float, PdfLine]]) -> float:
            ys = [y for y, _ in cluster]
            lines = [line for _, line in cluster]
            avg_y = sum(ys) / len(ys)
            score = len(cluster) * 10.0 + sum(len(line.norm) for line in lines)
            if avg_y <= page_index.page_height * PAGE_TOP_EXCLUDE_RATIO:
                if len(cluster) <= 2 and all(len(line.norm) <= 12 for line in lines):
                    score -= 100.0
                if any(len(line.norm) >= 20 for line in lines):
                    score += 25.0
            return score

        best_cluster = max(clusters, key=cluster_score)
        ys = [y for y, _ in best_cluster]
        band_min = min(ys) - HEADER_BAND_PADDING
        band_max = max(ys) + HEADER_BAND_PADDING
        return band_min, band_max

    def _header_y_band_from_anchors(self, page_number: int, table_id: str) -> tuple[float, float] | None:
        header_anchors = self._row_anchors.get((page_number, table_id, HEADER_ROW_INDEX), [])
        if not header_anchors:
            return None
        ys = [anchor.y_center for anchor in header_anchors]
        return min(ys) - HEADER_BAND_PADDING, max(ys) + HEADER_BAND_PADDING

    def _effective_header_band(
        self,
        page_number: int,
        table_id: str,
        page_index: PageGeometryIndex,
    ) -> tuple[float, float] | None:
        anchor_band = self._header_y_band_from_anchors(page_number, table_id)
        structural_band = self._infer_structural_header_band(page_index)
        if anchor_band and structural_band:
            anchor_mid = (anchor_band[0] + anchor_band[1]) / 2.0
            structural_mid = (structural_band[0] + structural_band[1]) / 2.0
            if abs(anchor_mid - structural_mid) > ROW_Y_SOFT_TOLERANCE:
                return structural_band
            band_min = max(anchor_band[0], structural_band[0])
            band_max = min(anchor_band[1], structural_band[1])
            if band_min <= band_max:
                return band_min, band_max
            return structural_band
        return anchor_band or structural_band

    @staticmethod
    def _is_below_page_top_fragment(y_center: float, page_height: float) -> bool:
        return y_center > page_height * PAGE_TOP_EXCLUDE_RATIO

    @staticmethod
    def _is_in_band(y_center: float, band: tuple[float, float] | None) -> bool:
        if band is None:
            return True
        return band[0] <= y_center <= band[1]

    def _is_page_top_fragment(
        self,
        y_center: float,
        page_height: float,
        header_band: tuple[float, float] | None,
    ) -> bool:
        if header_band is not None:
            if self._is_in_band(y_center, header_band):
                return False
        return y_center <= page_height * 0.02

    def _row_y_target(self, cell: GeometryCellInput, da_y_hint: float | None) -> float | None:
        if cell.row_index is None:
            return da_y_hint

        key = (cell.page_number, cell.table_id, cell.row_index)
        peer_anchors = self._row_anchors.get(key, [])
        if peer_anchors:
            return sum(anchor.y_center for anchor in peer_anchors) / len(peer_anchors)

        if da_y_hint is not None:
            return da_y_hint

        table_rows = self._table_row_medians(cell.page_number, cell.table_id)
        if not table_rows:
            return None

        if cell.row_index >= 1:
            data_rows = {row: y for row, y in table_rows.items() if row >= 1}
            if data_rows:
                return self._extrapolate_row_y(cell.row_index, data_rows)
            return None

        return self._extrapolate_row_y(cell.row_index, table_rows)

    def _table_row_medians(self, page_number: int, table_id: str) -> dict[int, float]:
        medians: dict[int, float] = {}
        prefix = (page_number, table_id)
        for (p, t, row_index), anchors in self._row_anchors.items():
            if (p, t) != prefix or not anchors:
                continue
            reliable = [
                anchor for anchor in anchors if anchor.column_index != OEL_ROW_NUMBER_COLUMN
            ]
            if not reliable:
                continue
            medians[row_index] = sum(item.y_center for item in reliable) / len(reliable)
        return medians

    @staticmethod
    def _extrapolate_row_y(row_index: int, row_medians: dict[int, float]) -> float | None:
        if row_index in row_medians:
            return row_medians[row_index]

        known = sorted(row_medians.items())
        if len(known) == 1:
            only_row, only_y = known[0]
            return only_y + (row_index - only_row) * 30.0

        best_pair: tuple[int, int] | None = None
        for left, right in zip(known, known[1:]):
            if left[0] <= row_index <= right[0]:
                best_pair = (left[0], right[0])
                break
        if best_pair is None:
            first_row, first_y = known[0]
            second_row, second_y = known[1]
            spacing = (second_y - first_y) / max(1, second_row - first_row)
            return first_y + (row_index - first_row) * spacing

        left_row, left_y = next(item for item in known if item[0] == best_pair[0])
        right_row, right_y = next(item for item in known if item[0] == best_pair[1])
        if right_row == left_row:
            return left_y
        ratio = (row_index - left_row) / (right_row - left_row)
        return left_y + ratio * (right_y - left_y)

    def _requires_row_context(self, cell: GeometryCellInput, text: str, normalized: str) -> bool:
        if cell.row_index is None or cell.row_index <= HEADER_ROW_INDEX:
            return False
        if is_short_limit_token(normalized):
            return True
        if is_numeric_only_text(text):
            return True
        compact = compact_match_text(text)
        if compact.isdigit() and len(compact) <= 2:
            return True
        return False

    def _generate_candidates(
        self,
        page_index: PageGeometryIndex,
        cell: GeometryCellInput,
        text: str,
        *,
        row_y_target: float | None,
        header_band: tuple[float, float] | None,
        page_height: float,
    ) -> list[_GeometryCandidate]:
        page = page_index.page
        normalized = normalize_match_text(text)
        compact = compact_match_text(text)
        require_row = self._requires_row_context(cell, text, normalized)

        candidates: list[_GeometryCandidate] = []

        candidates.extend(
            self._candidates_exact_lines(page_index, normalized, row_y_target, require_row)
        )

        # ------------------------------------------------------------
        # CAS matching runs BEFORE token/substring matching.
        #
        # A cell like "Styrene [ 100-42-5 ]" must resolve against the
        # CAS number's own geometry, not against a generic token-
        # sequence match on the digit fragments "100", "42", "5" --
        # that token match could previously score higher (up to 0.98)
        # than a CAS match (0.94-0.95) and win outright. Generating
        # CAS candidates first, and boosting them in _score_candidate()
        # below whenever the cell text contains a CAS number, fixes
        # that.
        # ------------------------------------------------------------

        cas_numbers = extract_cas_numbers(text) or CAS_PATTERN.findall(text)
        if cas_numbers:
            candidates.extend(
                self._candidates_cas(page_index, page, cas_numbers, row_y_target, require_row)
            )

        if not require_row or row_y_target is not None:
            candidates.extend(
                self._candidates_search_for(page, text, normalized, compact, row_y_target, require_row)
            )
            candidates.extend(
                self._candidates_english_names(page, text, row_y_target, require_row)
            )

        candidates.extend(
            self._candidates_substring_lines(page_index, normalized, row_y_target, require_row)
        )
        candidates.extend(
            self._candidates_token_sequence(page_index, significant_tokens(text), row_y_target, require_row)
        )

        if len(compact) >= 4:
            candidates.extend(
                self._candidates_fuzzy_words(page_index, compact, row_y_target, require_row)
            )

        candidates.extend(
            self._candidates_fuzzy_lines(page_index, normalized, row_y_target, require_row)
        )

        candidates = self._dedupe_candidates(candidates)
        return self._filter_candidates_for_row_context(
            candidates,
            cell,
            text,
            normalized,
            row_y_target=row_y_target,
            header_band=header_band,
            page_height=page_height,
        )

    def _filter_candidates_for_row_context(
        self,
        candidates: list[_GeometryCandidate],
        cell: GeometryCellInput,
        text: str,
        normalized: str,
        *,
        row_y_target: float | None,
        header_band: tuple[float, float] | None,
        page_height: float,
    ) -> list[_GeometryCandidate]:
        filtered: list[_GeometryCandidate] = []
        for candidate in candidates:
            if self._is_page_top_fragment(candidate.y_center, page_height, header_band):
                continue

            if cell.row_index == HEADER_ROW_INDEX:
                if header_band is not None and not self._is_in_band(candidate.y_center, header_band):
                    continue
                if candidate.generator in WEAK_ANCHOR_GENERATORS and header_band is None:
                    continue

            if cell.row_index is not None and cell.row_index > HEADER_ROW_INDEX and header_band is not None:
                if self._is_in_band(candidate.y_center, header_band):
                    continue

            filtered.append(candidate)
        return filtered

    @staticmethod
    def _distinct_y_bands(candidates: list[_GeometryCandidate], *, tolerance: float = ROW_Y_TOLERANCE) -> list[float]:
        bands: list[float] = []
        for candidate in sorted(candidates, key=lambda item: item.y_center):
            if not any(abs(candidate.y_center - band) <= tolerance for band in bands):
                bands.append(candidate.y_center)
        return bands

    def _is_numeric_like(self, text: str, normalized: str) -> bool:
        compact = compact_match_text(text)
        return is_numeric_only_text(text) or (compact.isdigit() and len(compact) <= 2)

    def _can_record_row_anchor(
        self,
        cell: GeometryCellInput,
        bbox: dict[str, float],
        candidate: _GeometryCandidate,
        text: str,
        *,
        row_y_target: float | None,
        header_band: tuple[float, float] | None,
        scored_candidates: list[tuple[_GeometryCandidate, float]],
        page_height: float,
    ) -> bool:
        y_center = bbox_y_center(bbox)
        if self._is_page_top_fragment(y_center, page_height, header_band):
            return False

        if float(bbox.get("height", 0)) > MAX_ROW_BBOX_HEIGHT:
            return False

        if cell.row_index == HEADER_ROW_INDEX:
            if header_band is not None and not self._is_in_band(y_center, header_band):
                return False
            if candidate.generator in WEAK_ANCHOR_GENERATORS:
                return False

        normalized = normalize_match_text(text)
        if self._is_numeric_like(text, normalized):
            if row_y_target is not None and abs(y_center - row_y_target) > ROW_Y_TOLERANCE:
                return False
            if not self._has_same_row_peer_anchors(cell):
                distinct = self._distinct_y_bands([item[0] for item in scored_candidates])
                if len(distinct) > 1:
                    return False
        elif cell.column_index == OEL_ROW_NUMBER_COLUMN:
            if row_y_target is None or abs(y_center - row_y_target) > ROW_Y_TOLERANCE:
                return False
        elif self._has_same_row_peer_anchors(cell) and row_y_target is not None:
            if abs(y_center - row_y_target) > ROW_Y_TOLERANCE:
                return False

        return True

    @staticmethod
    def _dedupe_candidates(candidates: list[_GeometryCandidate]) -> list[_GeometryCandidate]:
        best_by_key: dict[tuple[float, str, str], _GeometryCandidate] = {}
        for candidate in candidates:
            key = (round(candidate.y_center / 4.0), candidate.match_method, candidate.generator)
            existing = best_by_key.get(key)
            if existing is None or candidate.base_confidence > existing.base_confidence:
                best_by_key[key] = candidate
        return list(best_by_key.values())

    def _line_in_row_scope(
        self,
        line: PdfLine,
        *,
        row_y_target: float | None,
        require_row: bool,
    ) -> bool:
        if not require_row:
            return True
        if row_y_target is None:
            return False
        return abs(line_y_center(line) - row_y_target) <= ROW_Y_SOFT_TOLERANCE

    def _rect_in_row_scope(
        self,
        rect: fitz.Rect,
        *,
        row_y_target: float | None,
        require_row: bool,
    ) -> bool:
        if not require_row:
            return True
        if row_y_target is None:
            return False
        y_center = rect.y0 + rect.height / 2.0
        return abs(y_center - row_y_target) <= ROW_Y_SOFT_TOLERANCE

    def _candidates_exact_lines(
        self,
        page_index: PageGeometryIndex,
        normalized: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        if not normalized:
            return []

        matches: list[_GeometryCandidate] = []
        for line in page_index.lines:
            if line.norm != normalized:
                continue
            if not self._line_in_row_scope(line, row_y_target=row_y_target, require_row=require_row):
                continue
            matches.append(
                _GeometryCandidate(
                    bbox=self._line_bbox(line),
                    match_method=MATCH_EXACT,
                    base_confidence=0.98,
                    matched_words=[line.text],
                    y_center=line_y_center(line),
                    generator="exact_line",
                )
            )
        return matches

    def _cluster_rects_by_row(self, rects: list[fitz.Rect]) -> list[list[fitz.Rect]]:
        if not rects:
            return []
        sorted_rects = sorted(rects, key=lambda rect: (rect.y0, rect.x0))
        clusters: list[list[fitz.Rect]] = []
        for rect in sorted_rects:
            y_center = rect.y0 + rect.height / 2.0
            placed = False
            for cluster in clusters:
                cluster_y = cluster[0].y0 + cluster[0].height / 2.0
                if abs(y_center - cluster_y) <= 6.0:
                    cluster.append(rect)
                    placed = True
                    break
            if not placed:
                clusters.append([rect])
        return clusters

    @staticmethod
    def _union_cluster_bbox(rects: list[fitz.Rect]) -> dict[str, float]:
        combined = fitz.Rect(rects[0])
        for rect in rects[1:]:
            combined |= rect
        return rect_to_bbox(combined)

    def _candidates_search_for(
        self,
        page: fitz.Page,
        text: str,
        normalized: str,
        compact: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        candidates: list[_GeometryCandidate] = []
        seen_cluster_keys: set[tuple[float, float]] = set()

        query_pairs = [
            (query, max(0.94, 0.97 - index * 0.01))
            for index, query in enumerate(search_query_variants(text))
        ]

        for query, quality in query_pairs:
            if not query:
                continue
            rects = page.search_for(query)
            for cluster in self._cluster_rects_by_row(list(rects)):
                bbox = self._union_cluster_bbox(cluster)
                y_center = bbox_y_center(bbox)
                cluster_key = (round(y_center, 1), round(bbox["x"], 0))
                if cluster_key in seen_cluster_keys:
                    continue
                if bbox["height"] > MAX_ROW_BBOX_HEIGHT:
                    continue
                if not self._rect_in_row_scope(
                    fitz.Rect(bbox["x"], bbox["y"], bbox["x"] + bbox["width"], bbox["y"] + bbox["height"]),
                    row_y_target=row_y_target,
                    require_row=require_row,
                ):
                    continue
                seen_cluster_keys.add(cluster_key)
                candidates.append(
                    _GeometryCandidate(
                        bbox=bbox,
                        match_method=MATCH_EXACT,
                        base_confidence=min(SEARCH_FOR_MAX_CONFIDENCE, quality),
                        matched_words=[query],
                        y_center=y_center,
                        generator="search_for",
                    )
                )
        return candidates

    def _candidates_english_names(
        self,
        page: fitz.Page,
        text: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        candidates: list[_GeometryCandidate] = []
        for english_name in ENGLISH_NAME_PATTERN.findall(text):
            name = english_name.strip()
            if len(name) < 4:
                continue
            for rect in page.search_for(name):
                if not self._rect_in_row_scope(rect, row_y_target=row_y_target, require_row=require_row):
                    continue
                bbox = rect_to_bbox(rect)
                candidates.append(
                    _GeometryCandidate(
                        bbox=bbox,
                        match_method=MATCH_EXACT,
                        base_confidence=0.94,
                        matched_words=[name],
                        y_center=bbox_y_center(bbox),
                        generator="english_name",
                    )
                )
        return candidates

    @staticmethod
    def _line_matches_substring(normalized: str, line_norm: str) -> bool:
        if not normalized or not line_norm:
            return False
        if normalized == line_norm:
            return True
        if normalized in line_norm:
            return len(normalized) >= 3
        if line_norm in normalized:
            min_chars = max(8, int(len(normalized) * 0.45))
            return len(line_norm) >= min_chars
        return False

    def _candidates_substring_lines(
        self,
        page_index: PageGeometryIndex,
        normalized: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        if len(normalized) < 3:
            return []

        if require_row and row_y_target is None:
            return []

        scoped_lines = [
            line
            for line in page_index.lines
            if self._line_in_row_scope(line, row_y_target=row_y_target, require_row=require_row)
            and self._line_matches_substring(normalized, line.norm)
        ]

        candidates: list[_GeometryCandidate] = []
        for line in scoped_lines:
            candidates.append(
                _GeometryCandidate(
                    bbox=self._line_bbox(line),
                    match_method=MATCH_LINE,
                    base_confidence=0.92,
                    matched_words=[line.text],
                    y_center=line_y_center(line),
                    generator="substring_line",
                )
            )
        return candidates

    def _candidates_token_sequence(
        self,
        page_index: PageGeometryIndex,
        tokens: list[str],
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        if not tokens:
            return []

        candidates: list[_GeometryCandidate] = []
        for line_words in page_index.words_by_line().values():
            if not line_words:
                continue
            line_y = (min(word.y0 for word in line_words) + max(word.y1 for word in line_words)) / 2.0
            if require_row and row_y_target is not None and abs(line_y - row_y_target) > ROW_Y_SOFT_TOLERANCE:
                continue
            if require_row and row_y_target is None:
                continue

            ordered = sorted(line_words,key=lambda word: word.x0,)
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
                    bbox = union_word_bbox(matched_words)
                    confidence = 0.98 if token_count >= 2 else 0.9
                    candidates.append(
                        _GeometryCandidate(
                            bbox=bbox,
                            match_method=MATCH_TOKENS,
                            base_confidence=confidence,
                            matched_words=[word.text for word in matched_words],
                            y_center=bbox_y_center(bbox),
                            generator="token_sequence",
                    )
                    )
                elif matched_words and token_index >= max(1, int(token_count * 0.6)):
                    bbox = union_word_bbox(matched_words)
                    confidence = 0.85 * (token_index / token_count)
                    candidates.append(
                        _GeometryCandidate(
                            bbox=bbox,
                            match_method=MATCH_PARTIAL,
                            base_confidence=confidence,
                            matched_words=[word.text for word in matched_words],
                            y_center=bbox_y_center(bbox),
                            generator="partial_token_sequence",
                        )
                    )
        return candidates

    def _candidates_cas(
        self,
        page_index: PageGeometryIndex,
        page: fitz.Page,
        cas_numbers: list[str],
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        candidates: list[_GeometryCandidate] = []

        for cas in cas_numbers:
            cas_normalized = normalize_match_text(cas)

            for line in page_index.lines:
                if not self._line_in_row_scope(line, row_y_target=row_y_target, require_row=require_row):
                    continue
                if cas_normalized not in line.norm and not any(
                    self._cas_digits_match(word.norm, cas_normalized) for word in line.words
                ):
                    continue

                matched_words = [
                    word
                    for word in line.words
                    if cas_normalized in word.norm
                    or self._cas_digits_match(word.norm, cas_normalized)
                ]
                if matched_words:
                    bbox = union_word_bbox(matched_words)
                    candidates.append(
                        _GeometryCandidate(
                            bbox=bbox,
                            match_method=MATCH_CAS,
                            # Bumped from 0.95: this is a direct
                            # word-level CAS match, the strongest CAS
                            # signal available. Combined with the
                            # CAS_PRIORITY_BONUS applied in
                            # _score_candidate(), this reliably beats
                            # token_sequence's 0.98 ceiling.
                            base_confidence=0.97,
                            matched_words=[word.text for word in matched_words],
                            y_center=bbox_y_center(bbox),
                            generator="cas_line_words",
                        )
                    )
                    continue

                candidates.append(
                    _GeometryCandidate(
                        bbox=self._line_bbox(line),
                        match_method=MATCH_CAS,
                        base_confidence=0.95,
                        matched_words=[line.text],
                        y_center=line_y_center(line),
                        generator="cas_line",
                    )
                )

            for rect in page.search_for(cas):
                if not self._rect_in_row_scope(rect, row_y_target=row_y_target, require_row=require_row):
                    continue
                bbox = rect_to_bbox(rect)
                candidates.append(
                    _GeometryCandidate(
                        bbox=bbox,
                        match_method=MATCH_CAS,
                        base_confidence=0.95,
                        matched_words=[cas],
                        y_center=bbox_y_center(bbox),
                        generator="cas_search_for",
                    )
                )

        return candidates

    def _candidates_fuzzy_words(
        self,
        page_index: PageGeometryIndex,
        compact: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        candidates: list[_GeometryCandidate] = []
        best_ratio = 0.0
        best_word: PdfWord | None = None

        for word in page_index.words:
            word_y = (word.y0 + word.y1) / 2.0
            if require_row and row_y_target is not None and abs(word_y - row_y_target) > ROW_Y_SOFT_TOLERANCE:
                continue
            if require_row and row_y_target is None:
                continue

            word_compact = compact_match_text(word.text)
            if not word_compact:
                continue
            ratio = SequenceMatcher(None, compact, word_compact).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_word = word

        if best_word is not None and best_ratio >= FUZZY_WORD_MIN_RATIO:
            candidates.append(
                _GeometryCandidate(
                    bbox=best_word.bbox,
                    match_method=MATCH_TOKEN,
                    base_confidence=round(min(0.92, 0.85 + (best_ratio - FUZZY_WORD_MIN_RATIO) * 0.5), 3),
                    matched_words=[best_word.text],
                    y_center=(best_word.y0 + best_word.y1) / 2.0,
                    generator="fuzzy_word",
                )
            )
            return candidates

        for word in page_index.words:
            word_y = (word.y0 + word.y1) / 2.0
            if require_row and row_y_target is not None and abs(word_y - row_y_target) > ROW_Y_SOFT_TOLERANCE:
                continue
            if require_row and row_y_target is None:
                continue

            word_compact = compact_match_text(word.text)
            if compact in word_compact or word_compact in compact:
                candidates.append(
                    _GeometryCandidate(
                        bbox=word.bbox,
                        match_method=MATCH_TOKEN,
                        base_confidence=0.88,
                        matched_words=[word.text],
                        y_center=word_y,
                        generator="substring_word",
                    )
                )
                break

        return candidates

    def _candidates_fuzzy_lines(
        self,
        page_index: PageGeometryIndex,
        normalized: str,
        row_y_target: float | None,
        require_row: bool,
    ) -> list[_GeometryCandidate]:
        if not normalized:
            return []

        best_ratio = 0.0
        best_line: PdfLine | None = None

        for line in page_index.lines:
            if not self._line_in_row_scope(line, row_y_target=row_y_target, require_row=require_row):
                continue
            ratio = SequenceMatcher(None, normalized, line.norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_line = line

        if best_line is None or best_ratio < self.fuzzy_line_threshold:
            return []

        return [
            _GeometryCandidate(
                bbox=self._line_bbox(best_line),
                match_method=MATCH_LINE,
                base_confidence=round(best_ratio, 4),
                matched_words=[best_line.text],
                y_center=line_y_center(best_line),
                generator="fuzzy_line",
            )
        ]

    def _score_candidate(
        self,
        candidate: _GeometryCandidate,
        cell: GeometryCellInput,
        *,
        row_y_target: float | None,
        header_band: tuple[float, float] | None,
        text_has_cas: bool = False,
    ) -> float:
        score = candidate.base_confidence

        # ------------------------------------------------------------
        # CAS matching takes priority over generic token matching.
        #
        # When the cell text itself contains a CAS number, a candidate
        # produced by CAS-specific matching (CAS_GENERATORS) is the
        # more specific, more trustworthy signal and gets a bonus;
        # a plain token/partial-token-sequence candidate for the same
        # cell — which may just be matching the CAS's own digit
        # fragments as generic tokens — gets a matching penalty. This
        # is what fixes cases like "Styrene [ 100-42-5 ]" resolving to
        # a token match instead of the actual CAS geometry.
        # ------------------------------------------------------------

        if text_has_cas:
            if candidate.generator in CAS_GENERATORS:
                score += CAS_PRIORITY_BONUS
            elif candidate.generator in {"token_sequence", "partial_token_sequence"}:
                score -= TOKEN_CAS_PENALTY

        skip_row_penalty = (
            candidate.generator in CAS_GENERATORS | ENGLISH_NAME_GENERATORS
            and not self._has_same_row_peer_anchors(cell)
        )
        if row_y_target is not None and not skip_row_penalty:
            delta = abs(candidate.y_center - row_y_target)
            if delta <= ROW_Y_TOLERANCE:
                score += 0.12
            elif delta <= ROW_Y_SOFT_TOLERANCE:
                score -= 0.08
            else:
                score -= 0.45

        if header_band is not None and cell.row_index is not None:
            band_min, band_max = header_band
            in_header = band_min <= candidate.y_center <= band_max
            if cell.row_index == HEADER_ROW_INDEX and not in_header:
                score -= 0.25
            if cell.row_index > HEADER_ROW_INDEX and in_header:
                score -= 0.55

        if candidate.bbox["height"] > MAX_ROW_BBOX_HEIGHT:
            score -= 0.4

        if cell.column_index is not None and cell.row_index is not None:
            peer_key = (cell.page_number, cell.table_id, cell.row_index)
            peer_anchors = self._row_anchors.get(peer_key, [])
            if peer_anchors:
                peer_delta = min(abs(candidate.y_center - anchor.y_center) for anchor in peer_anchors)
                if peer_delta <= ROW_Y_TOLERANCE:
                    score += 0.08

        return round(score, 4)

    def _select_candidate(
        self,
        candidates: list[_GeometryCandidate],
        cell: GeometryCellInput,
        text: str,
        *,
        row_y_target: float | None,
        header_band: tuple[float, float] | None,
        page_height: float,
    ) -> tuple[_GeometryCandidate | None, float, list[tuple[_GeometryCandidate, float]]]:
        if not candidates:
            return None, 0.0, []

        normalized = normalize_match_text(text)
        require_row = self._requires_row_context(cell, text, normalized)
        numeric_like = self._is_numeric_like(text, normalized)
        text_has_cas = _text_has_cas(text)

        if require_row and row_y_target is None:
            return None, 0.0, []

        if numeric_like and not self._has_same_row_peer_anchors(cell):
            distinct = self._distinct_y_bands(candidates)
            if len(distinct) > 1:
                return None, 0.0, []

        if cell.row_index == HEADER_ROW_INDEX and header_band is not None:
            candidates = [c for c in candidates if self._is_in_band(c.y_center, header_band)]
            if not candidates:
                return None, 0.0, []

        scored = [
            (
                candidate,
                self._score_candidate(
                    candidate,
                    cell,
                    row_y_target=row_y_target,
                    header_band=header_band,
                    text_has_cas=text_has_cas,
                ),
            )
            for candidate in candidates
        ]
        scored.sort(key=lambda item: (-item[1], -item[0].base_confidence))

        if not scored:
            return None, 0.0, []

        best_candidate, best_score = scored[0]
        if best_score < FUZZY_LINE_MIN_CONFIDENCE:
            return None, 0.0, scored

        if best_candidate.bbox["height"] > MAX_ROW_BBOX_HEIGHT:
            return None, 0.0, scored

        if self._is_page_top_fragment(best_candidate.y_center, page_height, header_band):
            return None, 0.0, scored

        if cell.row_index == HEADER_ROW_INDEX:
            if header_band is not None and not self._is_in_band(best_candidate.y_center, header_band):
                return None, 0.0, scored
            if best_candidate.generator in WEAK_ANCHOR_GENERATORS:
                return None, 0.0, scored

        if len(scored) > 1:
            second_score = scored[1][1]
            score_gap = best_score - second_score
            if score_gap <= AMBIGUITY_SCORE_MARGIN:
                close = [
                    item
                    for item in scored
                    if item[1] >= best_score - AMBIGUITY_SCORE_MARGIN
                    and abs(item[0].y_center - best_candidate.y_center) > ROW_Y_TOLERANCE
                ]
                if close:
                    return None, 0.0, scored
            elif score_gap <= 0.15:
                y_close = [
                    item
                    for item in scored[:3]
                    if abs(item[0].y_center - best_candidate.y_center) > ROW_Y_TOLERANCE
                    and item[1] >= best_score - AMBIGUITY_SCORE_MARGIN
                ]
                if y_close:
                    return None, 0.0, scored

        if numeric_like:
            if row_y_target is not None and abs(best_candidate.y_center - row_y_target) > ROW_Y_TOLERANCE:
                return None, 0.0, scored

            in_band = [
                item
                for item in scored
                if row_y_target is not None and abs(item[0].y_center - row_y_target) <= ROW_Y_TOLERANCE
            ]
            distinct_band_ys = {round(item[0].y_center, 0) for item in in_band}
            if len(distinct_band_ys) > 1:
                band_scored = sorted(in_band, key=lambda item: -item[1])
                if len(band_scored) > 1 and band_scored[1][1] >= band_scored[0][1] - AMBIGUITY_SCORE_MARGIN:
                    return None, 0.0, scored

            if row_y_target is None and len(self._distinct_y_bands([item[0] for item in scored[:5]])) > 1:
                return None, 0.0, scored

            if not self._has_same_row_peer_anchors(cell) and len(self._distinct_y_bands(candidates)) > 1:
                return None, 0.0, scored

        if (
            self._has_same_row_peer_anchors(cell)
            and row_y_target is not None
            and cell.row_index is not None
            and cell.row_index > HEADER_ROW_INDEX
            and best_candidate.generator not in CAS_GENERATORS
        ):
            if abs(best_candidate.y_center - row_y_target) > ROW_Y_TOLERANCE:
                in_band = [
                    item
                    for item in scored
                    if abs(item[0].y_center - row_y_target) <= ROW_Y_TOLERANCE
                ]
                if not in_band:
                    return None, 0.0, scored
                band_scored = sorted(in_band, key=lambda item: (-item[1], -item[0].base_confidence))
                best_candidate, best_score = band_scored[0]
                if best_score < FUZZY_LINE_MIN_CONFIDENCE:
                    return None, 0.0, scored

        if is_short_limit_token(normalized) and cell.row_index is not None and cell.row_index > HEADER_ROW_INDEX:
            if header_band is not None and header_band[0] <= best_candidate.y_center <= header_band[1]:
                return None, 0.0, scored

        if cell.row_index is not None and cell.row_index > HEADER_ROW_INDEX and header_band is not None:
            if header_band[0] <= best_candidate.y_center <= header_band[1]:
                return None, 0.0, scored

        return best_candidate, min(0.99, best_score), scored

    @staticmethod
    def _line_bbox(line: PdfLine) -> dict[str, float]:
        bbox = line.bbox
        return {
            "x": bbox[0],
            "y": bbox[1],
            "width": bbox[2] - bbox[0],
            "height": bbox[3] - bbox[1],
        }

    @staticmethod
    def _cas_digits_match(value: str, cas: str) -> bool:
        value_digits = re.sub(r"\D", "", value)
        cas_digits = re.sub(r"\D", "", cas)
        if not value_digits or not cas_digits:
            return False
        return value_digits == cas_digits