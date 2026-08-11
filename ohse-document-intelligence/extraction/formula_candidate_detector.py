"""Detect formula candidates from page text and layout — never emit final formulas."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

EQUATION_REF_PATTERN = re.compile(r"رابطه\s*(\d+)", re.I)
MATH_TOKEN_PATTERN = re.compile(
    r"[=√×+\-*/()]|A\(\d+\)|\b[a-z]{1,5}\d*\b|\bT\d*\b|\bt\d*\b",
    re.I,
)
FORMULA_ASSIGNMENT_PATTERN = re.compile(
    r"([A-Za-z]+\(\d+\)|[a-z]{2,5})\s*=\s*",
    re.I,
)
SQRT_PATTERN = re.compile(r"√|sqrt", re.I)


@dataclass
class FormulaCandidate:
    candidate_id: str
    page: int
    candidate: bool = True
    reason: str = "mathematical_tokens_detected"
    document_equation_reference: str | None = None
    text_before: str = ""
    candidate_text: str = ""
    text_after: str = ""
    nearby_math_tokens: list[str] = field(default_factory=list)
    bbox: dict[str, Any] | None = None
    source: str = "pymupdf"
    paragraph_indices: list[int] = field(default_factory=list)
    evidence_text_fragments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "page": self.page,
            "candidate": self.candidate,
            "reason": self.reason,
            "document_equation_reference": self.document_equation_reference,
            "text_before": self.text_before,
            "candidate_text": self.candidate_text,
            "text_after": self.text_after,
            "nearby_math_tokens": self.nearby_math_tokens,
            "bbox": self.bbox,
            "source": self.source,
            "paragraph_indices": self.paragraph_indices,
            "evidence_text_fragments": self.evidence_text_fragments,
        }


def _union_bbox(boxes: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    valid = [b for b in boxes if b and all(k in b for k in ("x", "y", "width", "height"))]
    if not valid:
        return None
    x0 = min(b["x"] for b in valid)
    y0 = min(b["y"] for b in valid)
    x1 = max(b["x"] + b["width"] for b in valid)
    y1 = max(b["y"] + b["height"] for b in valid)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _is_primary_equation_anchor(text: str, eq_num: str) -> bool:
    """Prefer display-style equation labels over inline prose references."""
    if re.search(rf"\(\s*رابطه\s*{eq_num}\s*\)", text or ""):
        return True
    stripped = (text or "").strip()
    if re.search(rf"رابطه\s*{eq_num}", stripped) and ("=" in stripped or "√" in stripped):
        return True
    return False


def _select_unique_anchors(
    paragraphs: list[dict[str, Any]],
    anchors: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """Keep one anchor per equation reference — the display label closest to math tokens."""
    by_ref: dict[str, tuple[int, str, int]] = {}
    for para_index, eq_ref in anchors:
        text = paragraphs[para_index].get("text") or ""
        score = 0
        if _is_primary_equation_anchor(text, eq_ref):
            score += 10
        if "=" in text or "√" in text:
            score += 5
        if eq_ref not in by_ref or score > by_ref[eq_ref][2]:
            by_ref[eq_ref] = (para_index, eq_ref, score)
    return sorted(((item[0], item[1]) for item in by_ref.values()), key=lambda x: x[0])


def _extract_math_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in MATH_TOKEN_PATTERN.finditer(text or ""):
        token = match.group(0).strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _paragraph_is_formula_fragment(text: str) -> bool:
    """True when a paragraph belongs to displayed equation math, not variable definitions."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if re.fullmatch(r"\d+", stripped):
        return True
    if re.search(r"رابطه\s*\d", stripped) and ("√" in stripped or "=" in stripped):
        return True
    if re.match(r"^T[\s(]", stripped):
        return True
    if "A(8)" in stripped and ("=" in stripped or "√" in stripped):
        return True
    if stripped in {"Tv", "T0", "To"}:
        return True
    if "\n" in stripped:
        parts = [part.strip() for part in stripped.split("\n") if part.strip()]
        if len(parts) == 2 and all(len(part) <= 4 for part in parts):
            return True
    return bool(FORMULA_ASSIGNMENT_PATTERN.search(stripped) or SQRT_PATTERN.search(stripped))


def _paragraph_has_math(text: str) -> bool:
    if _paragraph_is_formula_fragment(text):
        return True
    if not text:
        return False
    if EQUATION_REF_PATTERN.search(text):
        return True
    return len(_extract_math_tokens(text)) >= 2


def detect_formula_candidates(
    page_number: int,
    page_text: str,
    paragraphs: list[dict[str, Any]],
    *,
    source: str = "pymupdf",
) -> list[FormulaCandidate]:
    """Return formula candidates anchored by document equation references or math token clusters."""
    if not paragraphs:
        return _detect_from_text_only(page_number, page_text, source=source)

    anchors: list[tuple[int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        text = paragraph.get("text") or ""
        for match in EQUATION_REF_PATTERN.finditer(text):
            anchors.append((index, match.group(1)))

    if not anchors:
        return _detect_from_text_only(page_number, page_text, source=source)

    anchors = _select_unique_anchors(paragraphs, anchors)

    candidates: list[FormulaCandidate] = []
    for anchor_pos, (para_index, eq_ref) in enumerate(anchors):
        next_para_index = anchors[anchor_pos + 1][0] if anchor_pos + 1 < len(anchors) else len(paragraphs)
        region_indices = [para_index]
        scan_idx = para_index + 1
        while scan_idx < next_para_index:
            fragment_text = paragraphs[scan_idx].get("text") or ""
            if _paragraph_is_formula_fragment(fragment_text):
                region_indices.append(scan_idx)
                scan_idx += 1
                continue
            break

        # Include adjacent single-digit numerators near the anchor (fraction numerator)
        anchor_bbox = paragraphs[para_index].get("bbox")
        for back_idx in range(max(0, para_index - 2), para_index):
            token = (paragraphs[back_idx].get("text") or "").strip()
            back_bbox = paragraphs[back_idx].get("bbox")
            if not re.fullmatch(r"\d+", token) or back_idx in region_indices:
                continue
            if anchor_bbox and back_bbox and abs(back_bbox["y"] - anchor_bbox["y"]) <= 25:
                region_indices.insert(0, back_idx)

        region_paragraphs = [paragraphs[i] for i in region_indices]

        candidate_text = "\n".join(p.get("text", "") for p in region_paragraphs).strip()
        math_tokens = _extract_math_tokens(candidate_text)
        if not math_tokens and not FORMULA_ASSIGNMENT_PATTERN.search(candidate_text):
            continue

        text_before = paragraphs[para_index - 1]["text"] if para_index > 0 else ""
        after_index = region_indices[-1] + 1 if region_indices else para_index + 1
        text_after = paragraphs[after_index]["text"] if after_index < len(paragraphs) else ""

        fragments = [
            {
                "text": p.get("text", ""),
                "bbox": p.get("bbox"),
                "paragraph_index": region_indices[idx] if idx < len(region_indices) else None,
            }
            for idx, p in enumerate(region_paragraphs)
        ]

        candidates.append(
            FormulaCandidate(
                candidate_id=f"formula_candidate_{page_number:03d}_{len(candidates) + 1:02d}",
                page=page_number,
                reason="equation_reference_anchor",
                document_equation_reference=eq_ref,
                text_before=text_before.strip(),
                candidate_text=candidate_text,
                text_after=text_after.strip(),
                nearby_math_tokens=math_tokens,
                bbox=_union_bbox([p.get("bbox") for p in region_paragraphs]),
                source=source,
                paragraph_indices=region_indices,
                evidence_text_fragments=fragments,
            )
        )

    return candidates


def _detect_from_text_only(
    page_number: int,
    page_text: str,
    *,
    source: str = "pymupdf",
) -> list[FormulaCandidate]:
    """Fallback when layout paragraphs are unavailable."""
    candidates: list[FormulaCandidate] = []
    for match in FORMULA_ASSIGNMENT_PATTERN.finditer(page_text or ""):
        start = max(0, match.start() - 120)
        end = min(len(page_text), match.end() + 200)
        window = page_text[start:end]
        candidates.append(
            FormulaCandidate(
                candidate_id=f"formula_candidate_{page_number:03d}_{len(candidates) + 1:02d}",
                page=page_number,
                reason="mathematical_tokens_detected",
                text_before=page_text[max(0, start - 80) : start].strip(),
                candidate_text=window.strip(),
                text_after=page_text[end : end + 80].strip(),
                nearby_math_tokens=_extract_math_tokens(window),
                source=source,
            )
        )
    return candidates
