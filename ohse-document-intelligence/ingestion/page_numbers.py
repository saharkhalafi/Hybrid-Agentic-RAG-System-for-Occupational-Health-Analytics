"""Extract printed page numbers from PDF header/footer regions."""

from __future__ import annotations

import re

import fitz

PRINTED_PAGE_PATTERN = re.compile(r"^\d{1,4}$")


def extract_printed_page_number(page: fitz.Page) -> int | None:
    """Return the page number printed in the document header/footer, if found."""
    candidates: list[tuple[float, int]] = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("words"):
        cleaned = text.strip()
        if not PRINTED_PAGE_PATTERN.fullmatch(cleaned):
            continue
        if y0 > 55 or x0 < 300:
            continue
        candidates.append((x0, int(cleaned)))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]
