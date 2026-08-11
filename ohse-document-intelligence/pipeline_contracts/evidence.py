"""Immutable Evidence Layer — Document AI output is forensic source, never mutated."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import get_logger
from config.settings import PROJECT_ROOT, get_settings
from pipeline_contracts.provenance import PipelineStage, ProcessorProvenance

logger = get_logger(__name__)

EVIDENCE_ROOT = PROJECT_ROOT / "data" / "evidence"


@dataclass
class EvidenceManifest:
    content_hash: str
    source_pdf: str
    start_page: int
    end_page: int
    processor: str
    processor_version: str
    pipeline_version: str
    evidence_dir: str
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "source_pdf": self.source_pdf,
            "page_range": {"start": self.start_page, "end": self.end_page},
            "provenance": ProcessorProvenance(
                processor=self.processor,
                processor_version=self.processor_version,
                pipeline_version=self.pipeline_version,
                pipeline_stage=PipelineStage.EVIDENCE.value,
            ).to_dict(),
            "evidence_dir": self.evidence_dir,
            "artifacts": self.artifacts,
            "immutable": True,
        }


class EvidenceStore:
    """Write-once evidence snapshots referenced by all downstream stages."""

    def __init__(self, root: Path | None = None) -> None:
        self.settings = get_settings()
        self.root = root or EVIDENCE_ROOT

    def _run_dir(self, content_hash: str, start_page: int, end_page: int) -> Path:
        return self.root / f"{content_hash[:12]}_{start_page}-{end_page}"

    def persist(
        self,
        processed: Any,
        *,
        pipeline_version: str,
        processor_version: str | None = None,
        force: bool = False,
    ) -> EvidenceManifest:
        run_dir = self._run_dir(processed.content_hash, processed.start_page, processed.end_page)
        run_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists() and not force:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            logger.info("evidence_manifest_exists", path=str(manifest_path))
            return EvidenceManifest(
                content_hash=existing["content_hash"],
                source_pdf=existing["source_pdf"],
                start_page=existing["page_range"]["start"],
                end_page=existing["page_range"]["end"],
                processor=existing["provenance"]["processor"],
                processor_version=existing["provenance"]["processor_version"],
                pipeline_version=existing["provenance"]["pipeline"],
                evidence_dir=str(run_dir),
                artifacts=existing.get("artifacts", {}),
            )

        processor = processed.processor_format or "document_ai"
        proc_version = processor_version or self.settings.processing_version
        artifacts: dict[str, str] = {}

        raw_path = run_dir / "document_ai_raw.json"
        if processed.raw_document_ai:
            if raw_path.exists() and not force:
                logger.warning("evidence_raw_exists_skipped", path=str(raw_path))
            else:
                raw_path.write_text(
                    json.dumps(processed.raw_document_ai, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            artifacts["document_ai_raw"] = str(raw_path.relative_to(PROJECT_ROOT))

        pages_dir = run_dir / "pages"
        pages_dir.mkdir(exist_ok=True)
        layout_entities: list[dict[str, Any]] = []

        for page in processed.pages:
            page_payload = {
                "page_number": page.page_number,
                "printed_page_number": page.printed_page_number,
                "text": page.text,
                "has_digital_text": page.has_digital_text,
                "paragraphs": page.paragraphs,
                "provenance": ProcessorProvenance(
                    processor=processor,
                    processor_version=proc_version,
                    pipeline_version=pipeline_version,
                    pipeline_stage=PipelineStage.EVIDENCE.value,
                    evidence_ref=str(raw_path.relative_to(PROJECT_ROOT)) if raw_path.exists() else None,
                ).to_dict(),
            }
            page_file = pages_dir / f"page_{page.page_number:03d}.json"
            page_file.write_text(json.dumps(page_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts[f"page_{page.page_number:03d}"] = str(page_file.relative_to(PROJECT_ROOT))
            layout_entities.append(
                {
                    "type": "page",
                    "page_number": page.page_number,
                    "artifact": artifacts[f"page_{page.page_number:03d}"],
                }
            )

        tables_dir = run_dir / "tables"
        tables_dir.mkdir(exist_ok=True)
        for table in processed.tables:
            table_dict = table.to_dict() if hasattr(table, "to_dict") else table
            table_id = table_dict["table_id"]
            table_file = tables_dir / f"{table_id}.json"
            table_payload = {
                **table_dict,
                "provenance": ProcessorProvenance(
                    processor=processor,
                    processor_version=proc_version,
                    pipeline_version=pipeline_version,
                    pipeline_stage=PipelineStage.EVIDENCE.value,
                    evidence_ref=str(raw_path.relative_to(PROJECT_ROOT)) if raw_path.exists() else None,
                ).to_dict(),
            }
            table_file.write_text(json.dumps(table_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            rel = str(table_file.relative_to(PROJECT_ROOT))
            artifacts[table_id] = rel
            layout_entities.append(
                {
                    "type": "table",
                    "table_id": table_id,
                    "page_number": table_dict.get("page_number"),
                    "artifact": rel,
                }
            )

        layout_path = run_dir / "layout_entities.json"
        layout_path.write_text(
            json.dumps(
                {
                    "content_hash": processed.content_hash,
                    "entities": layout_entities,
                    "provenance": ProcessorProvenance(
                        processor=processor,
                        processor_version=proc_version,
                        pipeline_version=pipeline_version,
                        pipeline_stage=PipelineStage.EVIDENCE.value,
                    ).to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts["layout_entities"] = str(layout_path.relative_to(PROJECT_ROOT))

        manifest = EvidenceManifest(
            content_hash=processed.content_hash,
            source_pdf=processed.source_pdf,
            start_page=processed.start_page,
            end_page=processed.end_page,
            processor=processor,
            processor_version=proc_version,
            pipeline_version=pipeline_version,
            evidence_dir=str(run_dir.relative_to(PROJECT_ROOT)),
            artifacts=artifacts,
        )
        manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("evidence_layer_persisted", path=str(run_dir), artifacts=len(artifacts))
        return manifest

    @staticmethod
    def copy_cached_raw(source: Path, destination: Path) -> None:
        """Copy Document AI cache into evidence dir without modifying content."""
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
