"""Build row_knowledge retrieval text from stored labels + visible table-cell text."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import DocumentChunk, ExtractedTable, TableCell
from goldset_generator.fact_resolver import resolve_field_display
from retrieval.semantic_retrieval import ROW_KNOWLEDGE_SOURCE_TYPE, SEMANTIC_VALIDATION_STATUS

PRODUCTION_ROW_KNOWLEDGE_GOLD_PATH = "canonical_evidence_v1"


def nonempty_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def already_covered(text: str, blob: str) -> bool:
    needle = _norm_ws(text)
    hay = _norm_ws(blob)
    return bool(needle) and needle in hay


def format_row_knowledge_content(
    *,
    labeled_parts: list[str],
    extra_visible: list[str] | None = None,
) -> str:
    """Keep field labels, then append visible cell/original text not already present."""
    out: list[str] = []
    blob = ""
    for part in [*labeled_parts, *(extra_visible or [])]:
        text = nonempty_text(part)
        if text is None or already_covered(text, blob):
            continue
        out.append(text)
        blob = "\n".join(out)
    return "\n".join(out)


def labeled_and_original_from_gold_field(field_name: str, field_data: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """Labeled canonical display plus unused original/visible cell text."""
    if not isinstance(field_data, dict):
        return [], []
    labeled: list[str] = []
    extra: list[str] = []
    display = resolve_field_display(field_name, field_data)
    if display:
        labeled.append(f"{field_name}: {display}")
    original = nonempty_text(
        field_data.get("original_value")
        or field_data.get("raw_text")
        or field_data.get("text")
    )
    if original is None:
        return labeled, extra
    covered = "\n".join([*labeled, *extra])
    if not already_covered(original, covered):
        extra.append(original)
    return labeled, extra


def gold_row_knowledge_content(row: dict[str, Any]) -> str:
    labeled: list[str] = []
    extra: list[str] = []
    for field_name, field_data in row.items():
        lab, vis = labeled_and_original_from_gold_field(str(field_name), field_data)
        labeled.extend(lab)
        extra.extend(vis)
    return format_row_knowledge_content(labeled_parts=labeled, extra_visible=extra)


def visible_text_from_table_cell(cell: TableCell) -> str | None:
    return (
        nonempty_text(cell.original_value)
        or nonempty_text(cell.raw_text)
        or nonempty_text(cell.normalized_value)
    )


def collect_visible_row_cell_texts(session: Session, chunk: DocumentChunk) -> list[str]:
    """Visible texts for the same extracted table row, from stored cell evidence only."""
    provenance = chunk.provenance if isinstance(chunk.provenance, dict) else {}
    metadata = chunk.metadata_ if isinstance(chunk.metadata_, dict) else {}
    fields = provenance.get("fields") if isinstance(provenance.get("fields"), dict) else {}
    cell_ids = [
        str(payload.get("cell_id"))
        for payload in fields.values()
        if isinstance(payload, dict) and payload.get("cell_id")
    ]
    cells: list[TableCell] = []
    if cell_ids:
        cells = list(
            session.scalars(select(TableCell).where(TableCell.evidence_cell_id.in_(cell_ids))).all()
        )
    if cells:
        table_uuid = cells[0].table_id
        row_index = cells[0].row_index
        cells = list(
            session.scalars(
                select(TableCell)
                .where(TableCell.table_id == table_uuid, TableCell.row_index == row_index)
                .order_by(TableCell.column_index)
            ).all()
        )
    else:
        table_id = metadata.get("table_id") or provenance.get("table_id")
        row_index = metadata.get("row_index")
        if row_index is None:
            row_index = provenance.get("row_index")
        if table_id is not None and row_index is not None:
            table = session.scalar(
                select(ExtractedTable).where(ExtractedTable.stable_table_id == str(table_id))
            )
            if table is not None:
                cells = list(
                    session.scalars(
                        select(TableCell)
                        .where(
                            TableCell.table_id == table.id,
                            TableCell.row_index == int(row_index),
                        )
                        .order_by(TableCell.column_index)
                    ).all()
                )
    texts: list[str] = []
    for cell in cells:
        text = visible_text_from_table_cell(cell)
        if text:
            texts.append(text)
    return texts


@dataclass
class RowKnowledgeEnrichStats:
    scanned: int = 0
    content_changed: int = 0
    unchanged: int = 0
    skipped_empty: int = 0
    changed_chunk_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "content_changed": self.content_changed,
            "unchanged": self.unchanged,
            "skipped_empty": self.skipped_empty,
            "changed_chunk_ids": list(self.changed_chunk_ids),
        }


def enrich_production_row_knowledge(
    session: Session,
    *,
    dry_run: bool = False,
) -> RowKnowledgeEnrichStats:
    """Append stored visible table-cell text to production row_knowledge units."""
    stats = RowKnowledgeEnrichStats()
    chunks = session.scalars(
        select(DocumentChunk).where(
            DocumentChunk.source_type == ROW_KNOWLEDGE_SOURCE_TYPE,
            DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
            DocumentChunk.gold_artifact_path == PRODUCTION_ROW_KNOWLEDGE_GOLD_PATH,
            DocumentChunk.embedding.is_not(None),
        )
    ).all()
    for chunk in chunks:
        stats.scanned += 1
        extra = collect_visible_row_cell_texts(session, chunk)
        existing_parts = [line for line in str(chunk.content or "").splitlines() if line.strip()]
        new_content = format_row_knowledge_content(labeled_parts=existing_parts, extra_visible=extra)
        if not new_content.strip():
            stats.skipped_empty += 1
            continue
        if new_content == (chunk.content or ""):
            stats.unchanged += 1
            continue
        stats.content_changed += 1
        if chunk.chunk_id:
            stats.changed_chunk_ids.append(str(chunk.chunk_id))
        if dry_run:
            continue
        chunk.content = new_content
        chunk.embedding = None
        chunk.embedding_model = None
        chunk.embedding_version = None
        chunk.embedding_dimension = None
        if stats.content_changed % 50 == 0:
            session.flush()
    if not dry_run:
        session.flush()
    return stats
