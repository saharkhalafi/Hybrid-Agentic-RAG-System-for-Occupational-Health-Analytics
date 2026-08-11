"""Repair Persian OCR line breaks and normalize spacing for semantic retrieval.

Policy: all text is derived deterministically from PyMuPDF/Document AI evidence.
Never rewrite wording with LLMs — only join lines and normalize characters/spacing.
"""

from __future__ import annotations

import re

from normalization.persian_normalizer import contains_persian, normalize_persian_text

PERSIAN_LETTER = re.compile(r"[\u0600-\u06FF]")
LATIN_LETTER = re.compile(r"[A-Za-z]")
SENTENCE_END = re.compile(r"[.؟!؛:»)\]]\s*$")
LIST_ITEM = re.compile(r"^\d+[\).\-]\s")
TABLE_ROW_SIGNATURE = re.compile(
    r"\[\d{2,7}-\d{2}-\d\s*\]"
    r"|(?:^|\s)\d{1,3}\s*/\s*\d{2,4}\s"
    r"|(?:ppm|mg/m|mg/m³|mg/m3|f/ml)\b"
    r"|\bA[1-4]\b"
    r"|\b(IFV|IV|SKIN|BEI|DSEN|RSEN|OTO)\b",
    re.I,
)
OCR_ARTIFACT = re.compile(
    r"(?:\b[اآی]\s+[اآی]\b|\bمی\s+ن\s+توان|\bش\s+یمی|\bمواج\s+ه|\bاحت\s+یاط|"
    r"\bتعن\s+یی|\bارائه\s*می\b|\bای\s*مورد\b|\bسطح\s*کنترل\b)"
)
BROKEN_WORD = re.compile(r"\b[\u0600-\u06FF](?:\s+[\u0600-\u06FF]){1,4}\b")
INCOMPLETE_HEADING = re.compile(r"(?:ارائه\s*می|ارائهمی|مورد\s*استفاده|ای\s*مورد)$", re.I)


def reconstruct_block_text(raw: str) -> str:
    """Join OCR line breaks inside a PyMuPDF block — evidence-preserving only."""
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    if not lines:
        return ""

    parts: list[str] = [lines[0]]
    for line in lines[1:]:
        prev = parts[-1]
        if _should_join_lines(prev, line):
            parts[-1] = _join_lines(prev, line)
        else:
            parts.append(line)
    return "\n".join(parts)


def build_chunk_text_from_evidence(evidence_texts: list[str]) -> str:
    """Build chunk text strictly from linked paragraph evidence strings."""
    blocks = [reconstruct_block_text(text) for text in evidence_texts if (text or "").strip()]
    return "\n\n".join(blocks).strip()


def normalize_semantic_text(raw: str) -> str:
    """Normalize Persian/Latin spacing for embedding prep without changing meaning."""
    text = reconstruct_block_text(raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([،؛:.!?])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    text = re.sub(r"(?<=[\u0600-\u06FF])\s+(?=[A-Za-z0-9])", " ", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u0600-\u06FF])", " ", text)
    return normalize_persian_text(text).normalized.strip()


def sort_blocks_reading_order(blocks: list[dict]) -> list[dict]:
    """Sort paragraph blocks top-to-bottom, then right-to-left for Persian pages."""

    def sort_key(item: dict) -> tuple[float, float]:
        bbox = item.get("bbox") or {}
        y = float(bbox.get("y") or 0.0)
        x = float(bbox.get("x") or 0.0)
        return (round(y / 8.0), -x)

    return sorted(blocks, key=sort_key)


def looks_like_table_row(text: str) -> bool:
    if not text:
        return False
    hits = len(TABLE_ROW_SIGNATURE.findall(text))
    if hits >= 2:
        return True
    if re.search(r"\[\d{2,7}-\d{2}-\d\s*\]", text) and re.search(r"mg/m|ppm", text, re.I):
        return True
    return False


def looks_like_table_header(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    header_tokens = (
        "ردیف",
        "نام علمی",
        "وزن",
        "ملکولی",
        "TWA",
        "STEL",
        "نماد",
        "حد مجاز",
        "مبنای تعیین",
    )
    hits = sum(1 for token in header_tokens if token in stripped)
    return hits >= 2 and len(stripped) < 180


def looks_like_incomplete_heading(text: str) -> bool:
    line = reconstruct_block_text(text).split("\n")[0].strip()
    if not line or len(line) < 8:
        return True
    if INCOMPLETE_HEADING.search(line):
        return True
    if len(line) > 95 and not line.endswith((".", ":", "؟", "!")):
        return True
    return False


def has_broken_persian(text: str) -> bool:
    if not contains_persian(text):
        return False
    if OCR_ARTIFACT.search(text):
        return True
    for match in BROKEN_WORD.findall(text):
        compact = match.replace(" ", "")
        if len(compact) >= 4 and " " in match:
            return True
    return False


def _should_join_lines(prev: str, nxt: str) -> bool:
    if not prev or not nxt:
        return False
    prev_st = prev.strip()
    nxt_st = nxt.strip()
    if SENTENCE_END.search(prev_st):
        return False
    if LIST_ITEM.match(nxt_st):
        return False
    if prev_st.endswith("-") and LATIN_LETTER.search(nxt_st):
        return True
    if prev_st[-1].isdigit() and nxt_st[0].isalpha():
        return True
    if not (prev_st[-1].isalpha() and nxt_st[0].isalpha()):
        return False
    return True


def _join_lines(prev: str, nxt: str) -> str:
    prev_st = prev.strip()
    nxt_st = nxt.strip()
    if _join_without_space(prev_st, nxt_st):
        return prev_st + nxt_st
    return f"{prev_st} {nxt_st}"


def _join_without_space(prev: str, nxt: str) -> bool:
    """Join only when OCR split a single token across lines."""
    if len(prev) <= 3 or len(nxt) <= 3:
        return True
    if " " not in prev and len(prev) <= 5:
        return True
    if " " in prev:
        return len(nxt) <= 2
    prev_tail = prev.split()[-1] if prev.split() else prev
    if len(prev_tail) <= 2 and len(nxt) <= 6:
        return True
    return False
