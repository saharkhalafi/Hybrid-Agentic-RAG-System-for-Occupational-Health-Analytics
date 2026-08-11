"""Gold → PostgreSQL knowledge synchronization (Phase B).

Only accepted Gold artifacts enter production knowledge tables.
Candidates and non-gold_allowed tables are rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.logging import get_logger
from config.settings import Settings, get_settings
from database.models import ChemicalRegistry, Document, KnowledgeSyncRun, OELChemicalLimit
from knowledge.metadata_contract import FieldProvenance, KnowledgeMetadata, SourceReference

logger = get_logger(__name__)

PERSIAN_DIGIT = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
CAS_PATTERN = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")


@dataclass
class SyncStats:
    tables_processed: int = 0
    tables_synced: int = 0
    tables_skipped: int = 0
    tables_rejected: int = 0
    rows_synced: int = 0
    rows_skipped: int = 0
    rows_rejected: int = 0
    duplicates_prevented: int = 0
    chemicals_upserted: int = 0
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables_processed": self.tables_processed,
            "tables_synced": self.tables_synced,
            "tables_skipped": self.tables_skipped,
            "tables_rejected": self.tables_rejected,
            "rows_synced": self.rows_synced,
            "rows_skipped": self.rows_skipped,
            "rows_rejected": self.rows_rejected,
            "duplicates_prevented": self.duplicates_prevented,
            "chemicals_upserted": self.chemicals_upserted,
            "unresolved": self.unresolved,
        }


def _parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    cleaned = str(value).translate(PERSIAN_DIGIT)
    match = re.search(r"[\d]+(?:[./][\d]+)?", cleaned)
    if not match:
        return None
    token = match.group().replace("/", ".")
    try:
        return float(token)
    except ValueError:
        return None


def _extract_cas(row: dict[str, Any]) -> str | None:
    cas_field = row.get("CAS") or row.get("cas")
    if isinstance(cas_field, dict):
        for candidate in (cas_field.get("value"), cas_field.get("normalized_value"), cas_field.get("original_value")):
            if candidate:
                match = CAS_PATTERN.search(str(candidate))
                if match:
                    return match.group(1)
    for key in ("chemical_name", "Chemical"):
        field_data = row.get(key)
        if isinstance(field_data, dict):
            for candidate in (field_data.get("value"), field_data.get("original_value")):
                if candidate:
                    match = CAS_PATTERN.search(str(candidate))
                    if match:
                        return match.group(1)
    return None


def _field_payload(field_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(field_data, dict):
        return None
    return {
        "original_value": field_data.get("original_value"),
        "normalized_value": field_data.get("normalized_value") or field_data.get("value"),
        "accepted_value": field_data.get("value"),
        "unit": field_data.get("unit"),
        "cell_id": field_data.get("cell_id"),
        "bbox": field_data.get("bbox"),
        "value_status": field_data.get("value_status"),
        "source_reference": field_data.get("source_reference"),
    }


def is_production_gold_table(payload: dict[str, Any], path: Path) -> tuple[bool, str]:
    if "candidates" in path.parts:
        return False, "candidate_path_forbidden"
    if not payload.get("gold_allowed"):
        quality = payload.get("table_quality") or {}
        if not quality.get("gold_allowed"):
            return False, "gold_not_allowed"
    return True, "accepted"


def ensure_document(session: Session, *, content_hash: str, filename: str, settings: Settings) -> Document:
    existing = session.scalar(select(Document).where(Document.content_hash == content_hash))
    if existing:
        return existing
    document = Document(
        filename=filename,
        content_hash=content_hash,
        language="fa,en",
        processing_version=settings.processing_version,
        metadata_={"source": "phase_b_knowledge_sync"},
    )
    session.add(document)
    session.flush()
    return document


def resolve_document_content_hash(settings: Settings) -> tuple[str, str]:
    """Prefer the content_hash embedded in semantic Gold (matches evidence chain)."""
    production = settings.gold_dir / "rag" / "semantic_text_production.jsonl"
    source = settings.gold_dir / "rag" / "semantic_text.jsonl"
    for path in (production, source):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            doc_hash = record.get("document_id")
            filename = record.get("source_pdf") or "OHE6.pdf"
            if doc_hash:
                return doc_hash, filename

    pdf_path = settings.pdf_source_path
    if pdf_path and pdf_path.exists():
        content = pdf_path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        return content_hash, pdf_path.name
    fallback = "435de5ba90aeee0840c749f4d2448c79cb06ac5a3d3cc6e0696a80cfac7acb4f"
    return fallback, "OHE6.pdf"


def _upsert_chemical(
    session: Session,
    *,
    cas: str,
    english_name: str | None,
    persian_name: str | None,
    molecular_weight: float | None,
    gold_path: str,
) -> tuple[ChemicalRegistry, bool]:
    chemical = session.scalar(select(ChemicalRegistry).where(ChemicalRegistry.cas == cas))
    aliases: dict[str, list[str]] = {}
    if english_name:
        aliases["en"] = [english_name]
    if persian_name:
        aliases["fa"] = [persian_name]
    if chemical:
        if english_name and not chemical.english_name:
            chemical.english_name = english_name
        if persian_name and not chemical.persian_name:
            chemical.persian_name = persian_name
        if molecular_weight is not None and chemical.molecular_weight is None:
            chemical.molecular_weight = molecular_weight
        merged = dict(chemical.aliases or {})
        for lang, names in aliases.items():
            merged.setdefault(lang, [])
            for name in names:
                if name not in merged[lang]:
                    merged[lang].append(name)
        chemical.aliases = merged or None
        chemical.validation_status = "accepted"
        chemical.gold_artifact_path = gold_path
        return chemical, False

    chemical = ChemicalRegistry(
        cas=cas,
        english_name=english_name,
        persian_name=persian_name,
        molecular_weight=molecular_weight,
        aliases=aliases or None,
        validation_status="accepted",
        gold_artifact_path=gold_path,
    )
    session.add(chemical)
    session.flush()
    return chemical, True


def _row_index(row: dict[str, Any], idx: int) -> str:
    row_num = row.get("row_number") or row.get("ردیف")
    if isinstance(row_num, dict):
        row_num = row_num.get("value") or row_num.get("normalized_value")
    if row_num:
        return str(row_num)
    return str(idx)


def sync_gold_table_file(
    session: Session,
    path: Path,
    document: Document,
    stats: SyncStats,
    *,
    pipeline_version: str,
) -> None:
    stats.tables_processed += 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed, reason = is_production_gold_table(payload, path)
    if not allowed:
        stats.tables_rejected += 1
        stats.unresolved.append(f"{path.name}: {reason}")
        logger.info("gold_table_rejected", path=str(path), reason=reason)
        return

    table_id = payload.get("table_id") or path.stem
    page_number = payload.get("page_number")
    table_type = payload.get("table_type") or (payload.get("table_family") or {}).get("table_type")
    if table_type != "chemical_oel":
        stats.tables_skipped += 1
        return

    gold_path = str(path)
    rows = payload.get("rows") or []
    rows_written = 0

    for idx, row in enumerate(rows):
        cas = _extract_cas(row)
        if not cas:
            stats.rows_rejected += 1
            stats.unresolved.append(f"{table_id}:row_{idx}: missing_cas")
            continue

        name_field = row.get("chemical_name") or row.get("Chemical") or {}
        english_name = name_field.get("value") if isinstance(name_field, dict) else None
        persian_name = None
        if isinstance(name_field, dict):
            orig = name_field.get("original_value") or ""
            persian_match = re.search(r"[\u0600-\u06FF]+", str(orig))
            if persian_match:
                persian_name = persian_match.group()

        mw_field = row.get("molecular_weight") or row.get("Molecular_weight") or {}
        molecular_weight = _parse_float(mw_field.get("value") if isinstance(mw_field, dict) else None)

        chemical, created = _upsert_chemical(
            session,
            cas=cas,
            english_name=english_name,
            persian_name=persian_name,
            molecular_weight=molecular_weight,
            gold_path=gold_path,
        )
        if created:
            stats.chemicals_upserted += 1

        source_row_key = f"{table_id}:row_{_row_index(row, idx)}"
        existing = session.scalar(
            select(OELChemicalLimit).where(
                OELChemicalLimit.chemical_id == chemical.id,
                OELChemicalLimit.source_row_key == source_row_key,
            )
        )

        twa_field = _field_payload(row.get("TWA"))
        stel_field = _field_payload(row.get("STEL"))
        ceiling_field = _field_payload(row.get("STEL/C") or row.get("ceiling"))

        twa = _parse_float(twa_field.get("accepted_value") if twa_field else None)
        stel = _parse_float(stel_field.get("accepted_value") if stel_field else None)
        ceiling = _parse_float(ceiling_field.get("accepted_value") if ceiling_field else None)

        original_values = {
            k: v for k, v in {
                "TWA": twa_field,
                "STEL": stel_field,
                "ceiling": ceiling_field,
            }.items() if v
        }
        accepted_values = {
            k: v.get("accepted_value") for k, v in original_values.items() if v.get("accepted_value") is not None
        }

        field_provenance = []
        for fname, fdata in original_values.items():
            field_provenance.append(
                FieldProvenance(
                    field_name=fname,
                    original_value=fdata.get("original_value"),
                    normalized_value=fdata.get("normalized_value"),
                    accepted_value=fdata.get("accepted_value"),
                    unit=fdata.get("unit"),
                    cell_id=fdata.get("cell_id"),
                    bbox=fdata.get("bbox"),
                    validation_status="accepted",
                )
            )

        metadata = KnowledgeMetadata(
            record_type="structured",
            record_id=source_row_key,
            document_id=str(document.id),
            language="fa",
            validation_status="accepted",
            gold_artifact_path=gold_path,
            gold_version=pipeline_version,
            pipeline_version=pipeline_version,
            source_reference=SourceReference(
                document_id=str(document.id),
                page_number=page_number,
                table_id=table_id,
                row_id=source_row_key,
                cell_id=(twa_field or stel_field or {}).get("cell_id") if (twa_field or stel_field) else None,
                evidence_path=f"evidence/pages/page_{page_number:03d}.json" if page_number else None,
            ),
            field_provenance=field_provenance,
            aliases={"cas": [cas], "en": [english_name] if english_name else [], "fa": [persian_name] if persian_name else []},
        )

        if existing:
            stats.duplicates_prevented += 1
            existing.twa = twa
            existing.stel = stel
            existing.ceiling = ceiling
            existing.page_number = page_number
            existing.persian_name = persian_name or existing.persian_name
            existing.english_name = english_name or existing.english_name
            existing.source_table_id_str = table_id
            existing.original_values = original_values
            existing.accepted_values = accepted_values
            existing.source_cell_provenance = original_values
            existing.knowledge_metadata = metadata.to_dict()
            existing.gold_artifact_path = gold_path
            existing.gold_version = pipeline_version
            existing.validation_status = "accepted"
        else:
            limit = OELChemicalLimit(
                chemical_id=chemical.id,
                twa=twa,
                stel=stel,
                ceiling=ceiling,
                unit=(twa_field or stel_field or {}).get("unit") if (twa_field or stel_field) else None,
                page_number=page_number,
                persian_name=persian_name,
                english_name=english_name,
                standard_reference="OHE6",
                source_table_id_str=table_id,
                source_row_key=source_row_key,
                source_cell_provenance=original_values,
                original_values=original_values,
                accepted_values=accepted_values,
                validation_status="accepted",
                gold_artifact_path=gold_path,
                gold_version=pipeline_version,
                knowledge_metadata=metadata.to_dict(),
                confidence=1.0,
            )
            session.add(limit)
            rows_written += 1

    stats.rows_synced += rows_written
    stats.tables_synced += 1
    session.flush()


def sync_gold_tables(
    session: Session,
    *,
    gold_tables_dir: Path | None = None,
    settings: Settings | None = None,
) -> SyncStats:
    settings = settings or get_settings()
    gold_tables_dir = gold_tables_dir or (settings.gold_dir / "tables")
    stats = SyncStats()
    content_hash, filename = resolve_document_content_hash(settings)
    document = ensure_document(session, content_hash=content_hash, filename=filename, settings=settings)

    run = KnowledgeSyncRun(
        sync_type="gold_tables",
        pipeline_version=settings.goldset_pipeline_version,
        status="running",
    )
    session.add(run)
    session.flush()

    for path in sorted(gold_tables_dir.glob("*.json")):
        try:
            sync_gold_table_file(
                session,
                path,
                document,
                stats,
                pipeline_version=settings.goldset_pipeline_version,
            )
        except Exception as exc:
            stats.tables_rejected += 1
            stats.unresolved.append(f"{path.name}: {exc}")
            logger.exception("gold_table_sync_failed", path=str(path))

    run.stats = stats.to_dict()
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    return stats


def reject_candidate_sync(path: Path) -> bool:
    """Return True when path must never enter production knowledge."""
    return "candidates" in path.parts
