"""Document page identity for retrieval metadata (PDF index vs printed folio)."""

from __future__ import annotations

from typing import Any


def canonical_document_page(
    page_number: Any = None,
    printed_page_number: Any = None,
) -> Any:
    """Prefer the printed folio when present; otherwise the 1-based PDF page_number.

    Chunking stores both: ``page_number`` is the PDF index, ``printed_page_number``
    is the header/footer number when extraction found one (often PDF − 1).
    """
    if printed_page_number is not None:
        return printed_page_number
    return page_number


def retrieval_page_fields(
    *,
    page_number: Any = None,
    printed_page_number: Any = None,
) -> dict[str, Any]:
    """Both identities plus the canonical document page for citations/eval."""
    return {
        "page_number": page_number,
        "printed_page_number": printed_page_number,
        "document_page": canonical_document_page(page_number, printed_page_number),
    }


def candidate_page_values(candidate: dict[str, Any]) -> set[Any]:
    """Pages a retrieval candidate should match for page_hint / eval identity."""
    pages: set[Any] = set()
    meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    for source in (candidate, meta):
        if not isinstance(source, dict):
            continue
        for key in ("page_number", "printed_page_number", "document_page"):
            value = source.get(key)
            if value is not None:
                pages.add(value)
    return pages
