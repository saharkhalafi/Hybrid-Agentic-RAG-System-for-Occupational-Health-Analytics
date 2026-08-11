"""Orchestrate goldset generation pipeline — 3-layer architecture."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import configure_logging, get_logger
from config.settings import PROJECT_ROOT, get_settings
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.entity_extractor import EntityExtractor
from goldset_generator.formula_generator import FormulaGoldGenerator
from goldset_generator.gemini_client import GeminiClient
from goldset_generator.knowledge_generator import KnowledgeGenerator
from goldset_generator.metadata_generator import generate_page_metadata
from goldset_generator.provenance import GenerationInfo
from goldset_generator.qa_generator import QAGenerator
from goldset_generator.review_manager import ReviewManager
from goldset_generator.semantic_text_generator import SemanticTextGenerator
from goldset_generator.semantic_text_validator import SemanticTextValidator
from goldset_generator.structural_resolver import resolve_structure, write_validated_structure
from goldset_generator.table_gold_generator import TableGoldGenerator
from goldset_generator.validator import GoldsetValidator
from pipeline_contracts.canonical_grid import CanonicalGridBuilder
from pipeline_contracts.canonical_table import CanonicalTableBuilder
from pipeline_contracts.evidence import EvidenceStore
from pipeline_contracts.table_family_classifier import TableFamilyClassifier
from pipeline_contracts.validation_codes import ValidationIssue
from goldset_generator.validation_report import build_page_validation_report, build_table_validation_report
from validation_engine.engine import ValidationEngine

logger = get_logger(__name__)


@dataclass
class PipelineRunStats:
    pages_processed: int = 0
    pages_skipped: int = 0
    tables_detected: int = 0
    tables_recovered: int = 0
    merged_cells: int = 0
    entities_generated: int = 0
    triples_generated: int = 0
    qa_generated: int = 0
    numeric_validation_failures: int = 0
    bbox_missing_count: int = 0
    review_items: int = 0
    overwrite_prevented: int = 0
    semantic_text_chunks: int = 0
    semantic_chunks_review: int = 0
    pages_failed: int = 0
    document_ai_cells: int = 0
    structural_cells: int = 0
    evidence_manifest: str | None = None
    canonical_grids: int = 0
    canonical_tables: int = 0


class GoldsetPipeline:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gemini = GeminiClient()
        self.review_manager = ReviewManager(self.settings.gold_dir)
        self.entity_extractor = EntityExtractor()
        self.knowledge_generator = KnowledgeGenerator()
        self.table_gold_generator = TableGoldGenerator()
        self.formula_generator = FormulaGoldGenerator()
        self.qa_generator = QAGenerator()
        self.semantic_text_generator = SemanticTextGenerator()
        self.stats = PipelineRunStats()

    def run(
        self,
        pdf_path: Path,
        *,
        start_page: int,
        end_page: int,
        force: bool = False,
        skip_document_ai: bool = True,
    ) -> dict[str, Any]:
        processor = DocumentProcessor()
        evidence = processor.process(
            pdf_path,
            start_page=start_page,
            end_page=end_page,
            skip_document_ai=skip_document_ai,
        )
        self.stats.document_ai_cells = sum(1 for c in evidence.cells if c.source == "document_ai")

        evidence_manifest = EvidenceStore().persist(
            evidence,
            pipeline_version=self.settings.goldset_pipeline_version,
            processor_version=self.settings.processing_version,
            force=force,
        )
        self.stats.evidence_manifest = evidence_manifest.evidence_dir
        evidence_refs = list(evidence_manifest.artifacts.values())

        structural = resolve_structure(evidence, pdf_path)
        validated_path = write_validated_structure(structural, evidence, start_page=start_page, end_page=end_page)
        self.stats.tables_detected = structural.document_ai_table_count
        self.stats.tables_recovered = structural.recovered_table_count
        self.stats.merged_cells = structural.merged_cell_count
        self.stats.structural_cells = len(structural.cells)

        structural_tables = [t.to_dict() for t in structural.tables]
        grid_builder = CanonicalGridBuilder(
            pipeline_version=self.settings.goldset_pipeline_version,
            processor_version=self.settings.processing_version,
            evidence_refs=evidence_refs,
        )
        canonical_grids = grid_builder.build_all(structural_tables)
        grid_paths = CanonicalGridBuilder.write_all(
            canonical_grids,
            content_hash=evidence.content_hash,
            start_page=start_page,
            end_page=end_page,
        )
        self.stats.canonical_grids = len(grid_paths)
        grid_ref_by_table = {g.table_id: p for g, p in zip(canonical_grids, grid_paths)}

        table_classifier = TableFamilyClassifier(gemini_client=self.gemini)
        domain_validator = ValidationEngine()

        generation_info = GenerationInfo(
            source_pdf=pdf_path.name,
            pipeline_version=self.settings.goldset_pipeline_version,
            document_ai_processor=self.settings.document_ai_processor_name,
            gemini_model=self.settings.llm_model,
            start_page=start_page,
            end_page=end_page,
        )

        cell_by_id = {cell.cell_id: cell.to_dict() for cell in structural.cells}
        valid_cell_ids = list(cell_by_id.keys())
        validator = GoldsetValidator(list(cell_by_id.values()))

        all_review_items: list[dict[str, Any]] = []
        all_table_golds: list[dict[str, Any]] = []
        all_canonical_tables: list[Any] = []
        all_qa: list[dict[str, Any]] = []
        all_semantic_chunks: list[dict[str, Any]] = []
        pages_generated = 0

        for page in evidence.pages:
            page_number = page.page_number
            if self.review_manager.should_skip_page(page_number, force=force):
                self.stats.pages_skipped += 1
                self.stats.overwrite_prevented += 1
                logger.info("skipping_protected_page", page_number=page_number)
                continue

            page_tables = [t.to_dict() for t in structural.tables if t.page_number == page_number]
            page_cells = [c.to_dict() for c in structural.cells if c.page_number == page_number]
            table_detection = structural.page_detection.get(page_number, {})

            metadata = generate_page_metadata(
                page_number,
                page.text,
                page_tables,
                pdf_page_number=page_number,
                printed_page_number=page.printed_page_number,
                table_detection=table_detection,
            )

            page_cell_ids = [c["cell_id"] for c in page_cells if c.get("cell_id")]
            page_cell_by_id = {cid: cell_by_id[cid] for cid in page_cell_ids if cid in cell_by_id}
            page_validator = GoldsetValidator(list(page_cell_by_id.values()))
            table_schemas = []
            page_entity_issues: list[dict[str, Any]] = []
            page_qa_issues: list[dict[str, Any]] = []
            page_table_reports: list[dict[str, Any]] = []
            page_numeric_failures = 0

            table_golds: list[dict[str, Any]] = []
            for table in page_tables:
                classification = table_classifier.classify_table_dict(table)
                table_gold = self.table_gold_generator.generate(table)
                table_gold["schema_id"] = classification.schema_id
                table_gold["classification"] = classification.to_dict()
                row_validation_issues: list[ValidationIssue] = []
                rows_needing_review = 0
                for row in table_gold.get("rows", []):
                    for fname, fval in row.items():
                        if isinstance(fval, dict):
                            issues = page_validator.validate_table_field(fname, fval)
                            if issues:
                                page_numeric_failures += len(issues)
                                self.stats.numeric_validation_failures += len(issues)
                                typed = [
                                    ValidationIssue.from_legacy_string(issue, field=fname)
                                    for issue in issues
                                ]
                                row_validation_issues.extend(typed)
                                all_review_items.append(
                                    {
                                        "page_number": page_number,
                                        "item_type": "table_field",
                                        "issues": [i.to_dict() for i in typed],
                                        "source_reference": fval.get("source_reference"),
                                    }
                                )
                            if fval.get("value_status") in {"extraction_uncertain", "merged_cell"}:
                                rows_needing_review += 1
                            if fval.get("value_status") == "extracted" and not fval.get("bbox"):
                                self.stats.bbox_missing_count += 1

                    domain_result = domain_validator.validate_table_row(
                        table_gold.get("table_type", "chemical_oel"),
                        row,
                        schema_id=classification.schema_id,
                    )
                    row_validation_issues.extend(domain_result.issues)

                page_table_reports.append(
                    build_table_validation_report(
                        table_id=table_gold["table_id"],
                        page_number=page_number,
                        mapping_meta={
                            "mapping_confidence": table_gold.get("mapping_confidence"),
                            "mapping_status": table_gold.get("mapping_status"),
                            "mapping_issues": table_gold.get("mapping_issues"),
                        },
                        row_review_count=rows_needing_review,
                    )
                )

                canonical_table = CanonicalTableBuilder.from_table_gold(
                    table_gold,
                    classification,
                    pipeline_version=self.settings.goldset_pipeline_version,
                    processor_version=self.settings.processing_version,
                    validation_issues=row_validation_issues,
                    canonical_grid_ref=grid_ref_by_table.get(table_gold["table_id"]),
                    evidence_refs=evidence_refs,
                    mapping_confidence=classification.confidence,
                )
                all_canonical_tables.append(canonical_table)

                path = self.review_manager.write_table_gold(table_gold["table_id"], table_gold, force=force)
                table_golds.append(table_gold)
                all_table_golds.append(table_gold)
                logger.info("wrote_table_gold", path=str(path))

            from goldset_generator.table_schema_builder import build_page_schemas

            table_schemas = build_page_schemas(table_golds)

            entities = self.entity_extractor.extract(
                page_number,
                page.text,
                page_cells,
                page_tables,
                table_golds=table_golds,
                valid_cell_ids=page_cell_ids,
                document=pdf_path.name,
            )
            valid_entities: list[dict[str, Any]] = []
            for entity in entities:
                issues = page_validator.validate_entity(entity, page_number=page_number)
                if issues:
                    page_numeric_failures += len(issues)
                    self.stats.numeric_validation_failures += len(issues)
                    page_entity_issues.append({"issues": issues, "entity": entity.get("type")})
                    all_review_items.append(
                        {
                            "page_number": page_number,
                            "item_type": "entity",
                            "issues": issues,
                            "source_reference": entity.get("source_reference"),
                        }
                    )
                    continue
                valid_entities.append(entity)
            self.stats.entities_generated += len(valid_entities)

            triples = self.knowledge_generator.generate(
                page_number,
                page.text,
                page_cells,
                valid_entities,
                table_golds=table_golds,
                valid_cell_ids=page_cell_ids,
            )
            resolved_triples = self.knowledge_generator.resolve_triples(triples, page_cell_by_id)
            valid_triples: list[dict[str, Any]] = []
            for triple in resolved_triples:
                issues = page_validator.validate_triple(triple, page_number=page_number)
                if issues:
                    self.stats.numeric_validation_failures += len(issues)
                    all_review_items.append(
                        {
                            "page_number": page_number,
                            "item_type": "knowledge_triple",
                            "issues": issues,
                            "source_reference": triple.get("source_reference"),
                        }
                    )
                    continue
                valid_triples.append(triple)
            self.stats.triples_generated += len(valid_triples)

            approved_formulas, formula_review_items, all_formula_records = (
                self.formula_generator.generate_for_page(
                    page_number,
                    page.text,
                    page.paragraphs,
                    page_cells,
                )
            )
            for record in all_formula_records:
                formula_id = record["formula_id"]
                if record.get("status") == "approved":
                    self.review_manager.write_formula_gold(formula_id, record)
                else:
                    self.review_manager.write_formula_review(formula_id, record)
                    self.review_manager.write_formula_candidate(formula_id, record)
            all_review_items.extend(formula_review_items)

            qa_items = self.qa_generator.generate(
                page_number,
                page.text,
                table_golds,
                valid_entities,
                approved_formulas,
                valid_cell_ids=page_cell_ids,
            )
            resolved_qa = self.qa_generator.resolve_answers(qa_items, page_cell_by_id)
            valid_qa: list[dict[str, Any]] = []
            for qa in resolved_qa:
                issues = page_validator.validate_qa(qa, page_number=page_number)
                if issues:
                    page_numeric_failures += len(issues)
                    self.stats.numeric_validation_failures += len(issues)
                    page_qa_issues.append({"issues": issues, "question": qa.get("question")})
                    all_review_items.append(
                        {
                            "page_number": page_number,
                            "item_type": "qa",
                            "issues": issues,
                            "source_reference": qa.get("target_reference"),
                        }
                    )
                    continue
                valid_qa.append(qa)
            all_qa.extend(valid_qa)
            self.stats.qa_generated += len(valid_qa)

            ocr_conf = _min_score([c.get("confidence") for c in page_cells])
            bbox_conf = _min_score([c.get("bbox_confidence") for c in page_cells])
            structural_conf = _min_score([t.get("structural_confidence") for t in page_tables])
            semantic_conf = _min_score([e.get("confidence") for e in valid_entities])

            final_confidence = validator.compute_final_confidence(
                ocr_confidence=ocr_conf,
                structural_confidence=structural_conf,
                semantic_confidence=semantic_conf,
                bbox_confidence=bbox_conf,
            )

            validation_report = build_page_validation_report(
                page_number=page_number,
                table_reports=page_table_reports,
                entity_issues=page_entity_issues,
                qa_issues=page_qa_issues,
                numeric_failures=page_numeric_failures,
            )

            page_index = {
                "page_number": page_number,
                "pdf_page_number": page_number,
                "printed_page_number": page.printed_page_number,
                "generation_info": generation_info.to_dict(),
                "tables": [t["table_id"] for t in table_golds],
                "evidence_manifest": evidence_manifest.evidence_dir,
                "entities_file": f"entities/entities_{page_number:03d}.json",
                "qa_file": f"qa/qa_{page_number:03d}.json",
                "semantic_candidate": f"candidates/pages/page_{page_number:03d}.json",
                "validation_report": f"review/validation_page_{page_number:03d}.json",
                "review_status": validation_report["overall_status"],
                "confidence": {
                    "layout": ocr_conf,
                    "geometry": structural_conf,
                    "semantic": semantic_conf,
                    "bbox": bbox_conf,
                    "final": final_confidence,
                },
            }

            semantic_page = {
                "page_number": page_number,
                "pdf_page_number": page_number,
                "printed_page_number": page.printed_page_number,
                "generation_info": generation_info.to_dict(),
                "metadata": metadata,
                "summary": _build_summary(page, metadata, valid_entities, table_golds),
                "section": {
                    "title": metadata.get("document_type"),
                    "page_number": page_number,
                    "pdf_page_number": page_number,
                    "printed_page_number": page.printed_page_number,
                },
                "entities": valid_entities,
                "knowledge_triples": valid_triples,
                "table_schema": table_schemas,
                "tables": [t["table_id"] for t in table_golds],
                "formulas": [f["formula_id"] for f in approved_formulas],
                "qa": valid_qa,
                "validation": validation_report,
                "confidence": page_index["confidence"],
                "review_status": "pending",
            }

            self.review_manager.write_page_index(page_number, page_index, force=force)
            semantic_path = self.review_manager.write_semantic_page(page_number, semantic_page, force=force)
            self.review_manager.write_entities(page_number, valid_entities)
            self.review_manager.write_qa(page_number, valid_qa)
            self.review_manager.write_validation_report(page_number, validation_report)

            cell_texts = {
                (cell.get("text") or "").strip()
                for cell in page_cells
                if (cell.get("text") or "").strip()
            }
            cell_bboxes = [cell["bbox"] for cell in page_cells if cell.get("bbox")]
            table_bboxes: list[dict[str, Any]] = []
            table_bbox_by_id: dict[str, dict[str, Any]] = {}
            for table in page_tables:
                table_id = table.get("table_id")
                cells = [c for c in page_cells if c.get("table_id") == table_id and c.get("bbox")]
                if not cells:
                    continue
                xs = [float(c["bbox"]["x"]) for c in cells]
                ys = [float(c["bbox"]["y"]) for c in cells]
                x2 = [float(c["bbox"]["x"]) + float(c["bbox"]["width"]) for c in cells]
                y2 = [float(c["bbox"]["y"]) + float(c["bbox"]["height"]) for c in cells]
                bbox = {
                    "x": min(xs),
                    "y": min(ys),
                    "width": max(x2) - min(xs),
                    "height": max(y2) - min(ys),
                }
                table_bboxes.append(bbox)
                if table_id:
                    table_bbox_by_id[table_id] = bbox
            paragraph_evidence = {
                f"text_evidence_{page_number:03d}_{idx:03d}": (p.get("text") or "")
                for idx, p in enumerate(page.paragraphs)
            }
            page_semantic = self.semantic_text_generator.generate_for_page(
                page_number=page_number,
                printed_page_number=page.printed_page_number,
                paragraphs=page.paragraphs,
                document_id=evidence.content_hash,
                source_pdf=pdf_path.name,
                table_ids=[t["table_id"] for t in table_golds],
                formula_ids=[f["formula_id"] for f in approved_formulas],
                entities=valid_entities,
                cell_texts=cell_texts,
                table_bboxes=table_bboxes,
                cell_bboxes=cell_bboxes,
                table_bbox_by_id=table_bbox_by_id,
            )
            semantic_validator = SemanticTextValidator(
                gold_dir=self.settings.gold_dir,
                paragraph_evidence=paragraph_evidence,
                table_cell_texts=cell_texts,
            )
            for chunk in page_semantic:
                issues = semantic_validator.validate_chunk(chunk)
                if issues:
                    chunk["review_status"] = "review_required"
                    chunk["validation_issues"] = issues
                    self.stats.semantic_chunks_review += 1
                    all_review_items.append(
                        {
                            "page_number": page_number,
                            "item_type": "semantic_text",
                            "chunk_id": chunk.get("chunk_id"),
                            "issues": issues,
                            "source_reference": chunk.get("source_references"),
                        }
                    )
                all_semantic_chunks.append(chunk)

            pages_generated += 1
            self.stats.pages_processed += 1
            logger.info("wrote_page_gold", page_number=page_number, path=str(semantic_path))

            if isinstance(final_confidence, (int, float)) and final_confidence < 0.85:
                all_review_items.append(
                    {
                        "page_number": page_number,
                        "item_type": "page",
                        "confidence": final_confidence,
                        "issues": [f"final confidence below threshold: {final_confidence:.3f}"],
                        "source_reference": {"page_number": page_number, "cell_ids": []},
                        "review_priority": validator.review_priority(final_confidence),
                    }
                )

        self.stats.review_items = len(all_review_items)
        self.review_manager.append_review_items(all_review_items)
        self._write_semantic_text_corpus(all_semantic_chunks, start_page, end_page)

        if all_canonical_tables:
            table_paths = CanonicalTableBuilder.write_all(
                all_canonical_tables,
                content_hash=evidence.content_hash,
                start_page=start_page,
                end_page=end_page,
            )
            self.stats.canonical_tables = len(table_paths)

        report_path = _write_generation_report(self.stats, start_page, end_page, validated_path)

        return {
            "pages_generated": pages_generated,
            "pages_skipped": self.stats.pages_skipped,
            "tables_generated": len(all_table_golds),
            "tables_detected": self.stats.tables_detected,
            "tables_recovered": self.stats.tables_recovered,
            "merged_cells": self.stats.merged_cells,
            "entities_generated": self.stats.entities_generated,
            "triples_generated": self.stats.triples_generated,
            "qa_generated": self.stats.qa_generated,
            "numeric_validation_failures": self.stats.numeric_validation_failures,
            "review_items": self.stats.review_items,
            "semantic_text_chunks": self.stats.semantic_text_chunks,
            "semantic_chunks_review": self.stats.semantic_chunks_review,
            "pages_failed": self.stats.pages_failed,
            "evidence_manifest": self.stats.evidence_manifest,
            "canonical_grids": self.stats.canonical_grids,
            "canonical_tables": self.stats.canonical_tables,
            "validated_structure": str(validated_path),
            "generation_report": str(report_path),
            "gold_dir": str(self.settings.gold_dir),
        }

    def _write_semantic_text_corpus(
        self,
        chunks: list[dict[str, Any]],
        start_page: int,
        end_page: int,
    ) -> Path:
        rag_dir = self.settings.gold_dir / "rag"
        rag_dir.mkdir(parents=True, exist_ok=True)
        out_path = rag_dir / "semantic_text.jsonl"

        existing: list[dict[str, Any]] = []
        if out_path.exists():
            for line in out_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    existing.append(json.loads(line))

        merged = _merge_jsonl_records(
            existing,
            chunks,
            page_key="pdf_page_number",
            start_page=start_page,
            end_page=end_page,
        )
        with out_path.open("w", encoding="utf-8") as handle:
            for record in merged:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        self.stats.semantic_text_chunks = len(chunks)
        return out_path


def _min_score(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    return min(nums) if nums else None


def _build_summary(page, metadata: dict[str, Any], entities: list[dict], table_golds: list[dict]) -> str:
    printed = page.printed_page_number
    printed_note = f", printed page {printed}" if printed is not None else ""
    recovery = metadata.get("table_detection_status")
    return (
        f"PDF page {page.page_number}{printed_note}: {metadata.get('document_type')} with "
        f"{len(table_golds)} table(s), {len(entities)} entities, recovery={recovery}."
    )


def _write_generation_report(
    stats: PipelineRunStats,
    start_page: int,
    end_page: int,
    validated_path: Path,
) -> Path:
    report_path = PROJECT_ROOT / "docs" / "reports" / "goldset_generation_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Goldset Generation Report",
        "",
        f"Pages processed: **{start_page}–{end_page}**",
        "",
        "## Summary",
        "",
        f"- Pages processed: {stats.pages_processed}",
        f"- Pages skipped (review protection): {stats.pages_skipped}",
        f"- Tables detected (Document AI): {stats.tables_detected}",
        f"- Tables recovered (structural resolver): {stats.tables_recovered}",
        f"- Merged cells flagged: {stats.merged_cells}",
        f"- Document AI evidence cells: {stats.document_ai_cells}",
        f"- Structural layer cells: {stats.structural_cells}",
        f"- Entities accepted: {stats.entities_generated}",
        f"- Knowledge triples accepted: {stats.triples_generated}",
        f"- QA items accepted: {stats.qa_generated}",
        f"- Numeric validation failures: {stats.numeric_validation_failures}",
        f"- Cells missing bbox (extracted fields): {stats.bbox_missing_count}",
        f"- Review queue items: {stats.review_items}",
        f"- Semantic text chunks: {stats.semantic_text_chunks}",
        f"- Semantic chunks requiring review: {stats.semantic_chunks_review}",
        f"- Overwrite prevented: {stats.overwrite_prevented}",
        f"- Evidence manifest: `{stats.evidence_manifest}`",
        f"- Canonical grids written: {stats.canonical_grids}",
        f"- Canonical tables written: {stats.canonical_tables}",
        "",
        f"Validated structure: `{validated_path}`",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _merge_jsonl_records(
    existing: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    *,
    page_key: str,
    start_page: int,
    end_page: int,
) -> list[dict[str, Any]]:
    """Keep records outside processed page range; replace records inside range."""
    preserved = []
    for record in existing:
        page_num = record.get(page_key)
        if page_num is None:
            page_nums = record.get("page_numbers") or []
            page_num = page_nums[0] if page_nums else None
        if page_num is not None and start_page <= int(page_num) <= end_page:
            continue
        preserved.append(record)
    return preserved + new_records
