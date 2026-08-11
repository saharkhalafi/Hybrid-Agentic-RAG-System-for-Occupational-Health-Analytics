"""Regenerate semantic gold (entities, triples, QA) from existing table gold."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from goldset_generator.entity_extractor import EntityExtractor
from goldset_generator.formula_generator import FormulaGoldGenerator
from goldset_generator.knowledge_generator import KnowledgeGenerator
from goldset_generator.qa_generator import QAGenerator
from goldset_generator.review_manager import ReviewManager
from goldset_generator.validator import GoldsetValidator
from goldset_generator.structural_resolver import resolve_structure
from goldset_generator.document_processor import DocumentProcessor
from goldset_generator.table_gold_generator import TableGoldGenerator


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--start-page", type=int, required=True)
    parser.add_argument("--end-page", type=int, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    pdf_path = Path(args.file)
    settings = get_settings()
    review_manager = ReviewManager(settings.gold_dir)
    entity_extractor = EntityExtractor()
    knowledge_generator = KnowledgeGenerator()
    qa_generator = QAGenerator()
    formula_generator = FormulaGoldGenerator()
    table_gold_generator = TableGoldGenerator()

    processor = DocumentProcessor()
    evidence = processor.process(
        pdf_path,
        start_page=args.start_page,
        end_page=args.end_page,
        skip_document_ai=True,
    )
    structural = resolve_structure(evidence, pdf_path)

    for page in evidence.pages:
        page_number = page.page_number
        if page_number < args.start_page or page_number > args.end_page:
            continue
        if review_manager.should_skip_page(page_number, force=args.force):
            continue

        page_tables = [t.to_dict() for t in structural.tables if t.page_number == page_number]
        page_cells = [c.to_dict() for c in structural.cells if c.page_number == page_number]
        page_cell_ids = [c["cell_id"] for c in page_cells if c.get("cell_id")]
        page_cell_by_id = {c["cell_id"]: c for c in page_cells if c.get("cell_id")}
        page_validator = GoldsetValidator(list(page_cell_by_id.values()))

        table_golds = [table_gold_generator.generate(table) for table in page_tables]
        for table_gold in table_golds:
            review_manager.write_table_gold(table_gold["table_id"], table_gold, force=args.force)

        from goldset_generator.table_schema_builder import build_page_schemas

        table_schemas = build_page_schemas(table_golds)
        entities = entity_extractor.extract(
            page_number, page.text, page_cells, page_tables,
            table_golds=table_golds, valid_cell_ids=page_cell_ids,
        )
        valid_entities = [e for e in entities if not page_validator.validate_entity(e, page_number=page_number)]

        triples = knowledge_generator.generate(
            page_number, page.text, page_cells, valid_entities,
            table_golds=table_golds, valid_cell_ids=page_cell_ids,
        )
        resolved_triples = knowledge_generator.resolve_triples(triples, page_cell_by_id)
        valid_triples = [
            t for t in resolved_triples if not page_validator.validate_triple(t, page_number=page_number)
        ]

        formulas = formula_generator.generate_for_page(page_number, page.text, page_cells)
        qa_items = qa_generator.generate(
            page_number, page.text, table_golds, valid_entities, formulas,
            valid_cell_ids=page_cell_ids,
        )
        resolved_qa = qa_generator.resolve_answers(qa_items, page_cell_by_id)
        valid_qa = [q for q in resolved_qa if not page_validator.validate_qa(q, page_number=page_number)]

        page_gold_path = settings.gold_dir / "pages" / f"page_{page_number:03d}.json"
        if page_gold_path.exists():
            page_gold = json.loads(page_gold_path.read_text(encoding="utf-8"))
        else:
            page_gold = {"page_number": page_number}

        page_gold["entities"] = valid_entities
        page_gold["knowledge_triples"] = valid_triples
        page_gold["qa"] = valid_qa
        page_gold["table_schema"] = table_schemas
        page_gold["tables"] = [t["table_id"] for t in table_golds]
        review_manager.write_page_gold(page_number, page_gold, force=args.force)
        print(f"page {page_number}: entities={len(valid_entities)} triples={len(valid_triples)} qa={len(valid_qa)}")


if __name__ == "__main__":
    main()
