"""Build RAG corpus JSONL from gold artifacts (works without PostgreSQL)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from goldset_generator.fact_resolver import resolve_field_display


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _merge_jsonl_records(
    existing: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    *,
    page_key: str,
    start_page: int,
    end_page: int,
) -> list[dict[str, Any]]:
    preserved: list[dict[str, Any]] = []
    for record in existing:
        page_num = record.get(page_key)
        if page_num is None:
            page_nums = record.get("page_numbers") or []
            page_num = page_nums[0] if page_nums else None
        if page_num is not None and start_page <= int(page_num) <= end_page:
            continue
        preserved.append(record)
    return preserved + new_records


def _load_page_artifacts(gold_dir: Path, page_num: int) -> dict:
    index_path = gold_dir / "pages" / f"page_{page_num:03d}.json"
    candidate_path = gold_dir / "candidates" / "pages" / f"page_{page_num:03d}.json"
    qa_path = gold_dir / "qa" / f"qa_{page_num:03d}.json"

    page_index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    semantic = (
        json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.exists() else page_index
    )
    qa_payload = json.loads(qa_path.read_text(encoding="utf-8")) if qa_path.exists() else {}
    qa_items = qa_payload.get("qa") or semantic.get("qa") or []
    triples = semantic.get("knowledge_triples") or []
    tables = page_index.get("tables") or semantic.get("tables") or []
    source_pdf = (page_index.get("generation_info") or semantic.get("generation_info") or {}).get(
        "source_pdf", "OHE6.pdf"
    )
    processor = (page_index.get("generation_info") or {}).get("document_ai_processor", "document_ai")
    return {
        "tables": tables,
        "qa": qa_items,
        "triples": triples,
        "source_pdf": source_pdf,
        "processor": processor,
    }


def _resolve_expected_value(
    expected: dict,
    table_rows: list[dict],
    cell_lookup: dict[str, dict],
) -> str | None:
    if not expected:
        return None
    if expected.get("source") == "formula_engine":
        return None
    cell_id = expected.get("cell_id")
    field = expected.get("field")
    if cell_id and field:
        for row in table_rows:
            field_data = row.get(field) or {}
            if field_data.get("cell_id") == cell_id:
                return resolve_field_display(field, field_data)
        cell = cell_lookup.get(cell_id)
        if cell:
            return cell.get("text")
    return None


def build_corpus(
    gold_dir: Path,
    start_page: int,
    end_page: int,
    *,
    processor_version: str = "1.0.0",
    merge_existing: bool = True,
) -> dict[str, int]:
    evidence: list[dict] = []
    row_knowledge: list[dict] = []
    regulatory: list[dict] = []
    sql_benchmarks: list[dict] = []

    for page_num in range(start_page, end_page + 1):
        artifacts = _load_page_artifacts(gold_dir, page_num)
        if not artifacts["tables"]:
            continue

        for table_id in artifacts["tables"]:
            table_path = gold_dir / "tables" / f"{table_id}.json"
            if not table_path.exists():
                continue
            table = json.loads(table_path.read_text(encoding="utf-8"))
            cell_lookup: dict[str, dict] = {}

            for row in table.get("rows") or []:
                parts = []
                provenance = {}
                for field, data in row.items():
                    if not isinstance(data, dict):
                        continue
                    if data.get("cell_id"):
                        cell_lookup[data["cell_id"]] = data
                        provenance[field] = {
                            "cell_id": data["cell_id"],
                            "bbox": data.get("bbox"),
                            "value_status": data.get("value_status"),
                        }
                        evidence.append(
                            {
                                "chunk_type": "evidence_cell",
                                "page_number": page_num,
                                "table_id": table_id,
                                "field": field,
                                "cell_id": data["cell_id"],
                                "text": data.get("original_value") or data.get("value") or "",
                                "bbox": data.get("bbox"),
                                "value_status": data.get("value_status"),
                                "source_pdf": artifacts["source_pdf"],
                                "source": {
                                    "processor": "document_ai",
                                    "processor_version": processor_version,
                                    "page_number": page_num,
                                    "table_id": table_id,
                                    "cell_id": data["cell_id"],
                                    "field": field,
                                },
                            }
                        )
                    if data.get("value") and data.get("value_status") == "extracted":
                        parts.append(f"{field}: {data['value']}")

                subject = (
                    (row.get("persian_chemical_name") or {}).get("value")
                    or (row.get("chemical_name") or {}).get("value")
                )
                if subject and parts:
                    row_knowledge.append(
                        {
                            "chunk_type": "row_knowledge",
                            "page_number": page_num,
                            "table_id": table_id,
                            "subject": subject,
                            "content": " | ".join(parts),
                            "provenance": provenance,
                            "source_pdf": artifacts["source_pdf"],
                        }
                    )

            for qa in artifacts["qa"]:
                expected = qa.get("expected_value") or {}
                resolved = qa.get("derived_answer") or _resolve_expected_value(
                    expected, table.get("rows") or [], cell_lookup
                )
                regulatory.append(
                    {
                        "chunk_type": "regulatory_qa",
                        "page_number": page_num,
                        "question": qa.get("question"),
                        "expected_value": expected,
                        "expected_cell_ids": qa.get("expected_cell_ids"),
                        "derived_answer": resolved,
                        "answer_source": qa.get("answer_source"),
                        "target_reference": qa.get("target_reference"),
                        "review_status": qa.get("review_status", "pending"),
                    }
                )
                if qa.get("target_reference"):
                    sql_benchmarks.append(
                        {
                            "question": qa.get("question"),
                            "expected_value": expected,
                            "expected_cell_ids": qa.get("expected_cell_ids"),
                            "target_reference": qa.get("target_reference"),
                            "derived_answer": resolved,
                            "review_status": qa.get("review_status", "pending"),
                        }
                    )

        for triple in artifacts["triples"]:
            row_knowledge.append(
                {
                    "chunk_type": "knowledge_triple",
                    "page_number": page_num,
                    "subject": triple.get("subject"),
                    "predicate": triple.get("predicate"),
                    "object": triple.get("object"),
                    "source_reference": triple.get("source_reference"),
                }
            )

    out_dir = gold_dir / "rag"
    semantic_existing = _read_jsonl(out_dir / "semantic_text.jsonl")

    if merge_existing:
        evidence = _merge_jsonl_records(
            _read_jsonl(out_dir / "evidence_chunks.jsonl"),
            evidence,
            page_key="page_number",
            start_page=start_page,
            end_page=end_page,
        )
        row_knowledge = _merge_jsonl_records(
            _read_jsonl(out_dir / "row_knowledge.jsonl"),
            row_knowledge,
            page_key="page_number",
            start_page=start_page,
            end_page=end_page,
        )
        regulatory = _merge_jsonl_records(
            _read_jsonl(out_dir / "regulatory_qa.jsonl"),
            regulatory,
            page_key="page_number",
            start_page=start_page,
            end_page=end_page,
        )

    _write_jsonl(out_dir / "evidence_chunks.jsonl", evidence)
    _write_jsonl(out_dir / "row_knowledge.jsonl", row_knowledge)
    _write_jsonl(out_dir / "regulatory_qa.jsonl", regulatory)
    _write_jsonl(out_dir / "semantic_text.jsonl", semantic_existing)

    benchmarks_dir = gold_dir / "benchmarks"
    benchmarks_dir.mkdir(parents=True, exist_ok=True)
    (benchmarks_dir / "sql_gold.json").write_text(
        json.dumps({"items": sql_benchmarks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "evidence_chunks": len(evidence),
        "row_knowledge": len(row_knowledge),
        "regulatory_qa": len(regulatory),
        "semantic_text": len(semantic_existing),
        "sql_benchmarks": len(sql_benchmarks),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-page", type=int, default=44)
    parser.add_argument("--end-page", type=int, default=49)
    parser.add_argument("--no-merge", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    stats = build_corpus(
        settings.gold_dir,
        args.start_page,
        args.end_page,
        processor_version=settings.goldset_pipeline_version,
        merge_existing=not args.no_merge,
    )
    print("RAG corpus built:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  output: {settings.gold_dir / 'rag'}")


if __name__ == "__main__":
    main()
