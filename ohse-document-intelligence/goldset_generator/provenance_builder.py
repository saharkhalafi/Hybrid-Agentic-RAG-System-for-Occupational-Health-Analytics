"""Build full PDF → page → table → cell provenance chains."""

from __future__ import annotations

from typing import Any


def build_source_reference(
    *,
    document: str,
    page_number: int,
    table_id: str | None = None,
    cell_id: str | None = None,
    bbox: dict[str, Any] | None = None,
    processor: str = "document_ai",
    processor_version: str | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "document": document,
        "page_number": page_number,
    }
    if table_id:
        ref["table_id"] = table_id
    if cell_id:
        ref["cell_id"] = cell_id
        ref["cell_ids"] = [cell_id]
    if bbox:
        ref["bbox"] = bbox
    if processor:
        ref["processor"] = processor
    if processor_version:
        ref["processor_version"] = processor_version
    return ref


def build_evidence_source(
    *,
    processor: str,
    processor_version: str,
    page_number: int,
    table_id: str,
    cell_id: str,
    field: str | None = None,
) -> dict[str, Any]:
    payload = {
        "processor": processor,
        "processor_version": processor_version,
        "page_number": page_number,
        "table_id": table_id,
        "cell_id": cell_id,
    }
    if field:
        payload["field"] = field
    return payload
