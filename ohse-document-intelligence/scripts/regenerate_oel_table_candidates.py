"""Regenerate OEL table candidates from immutable local evidence.

Reads from (priority order):
  1. ``data/intermediate/validated_structure_*.json`` (structural resolver + recovery)
  2. ``data/canonical/grids/*/table_*.json`` (canonical grid artifacts)
  3. ``data/evidence/*/tables/table_*.json`` (raw Document AI — fallback only)

Writes to ``gold/candidates/tables`` by default. Use ``--promote-gold`` to also
refresh ``gold/tables`` (what the HITL bridge script reads first).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings
from goldset_generator.table_gold_generator import TableGoldGenerator


def _cell_from_grid_cell(cell: dict) -> dict:
    return {
        "cell_id": cell.get("cell_id"),
        "column": cell.get("column"),
        "text": cell.get("text"),
        "normalized_value": cell.get("normalized_text"),
        "bbox": cell.get("bbox"),
        "source_reference": {},
    }


def grid_to_table_dict(grid: dict) -> dict:
    """Convert a canonical grid JSON object into TableGoldGenerator input."""
    header_cells = [_cell_from_grid_cell(h) for h in grid.get("headers") or []]
    rows: list[list[dict]] = []
    if header_cells:
        rows.append(header_cells)
    for row in grid.get("rows") or []:
        cells = [_cell_from_grid_cell(c) for c in row.get("cells") or []]
        if cells:
            rows.append(cells)
    return {
        "table_id": grid["table_id"],
        "page_number": grid["page_number"],
        "table_type": "chemical_oel",
        "structural_confidence": 0.7,
        "rows": rows,
    }


def _latest_evidence_tables() -> dict[str, Path]:
    latest: dict[str, Path] = {}
    evidence_root = PROJECT_ROOT / "data" / "evidence"
    for path in evidence_root.glob("*/tables/table_*.json"):
        current = latest.get(path.stem)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            latest[path.stem] = path
    return latest


def _latest_canonical_grids() -> dict[str, Path]:
    latest: dict[str, Path] = {}
    grid_root = PROJECT_ROOT / "data" / "canonical" / "grids"
    for path in grid_root.glob("*/table_*.json"):
        current = latest.get(path.stem)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            latest[path.stem] = path
    return latest


def _load_validated_structure_tables(start_page: int, end_page: int) -> dict[str, dict]:
    """Load structural-resolver tables covering the page range."""
    tables: dict[str, dict] = {}
    intermediate = PROJECT_ROOT / "data" / "intermediate"
    for path in intermediate.glob("validated_structure_*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        page_range = data.get("pages_range") or {}
        range_start = int(page_range.get("start") or 0)
        range_end = int(page_range.get("end") or 0)
        if range_end < start_page or range_start > end_page:
            continue
        for table in data.get("tables") or []:
            page_number = int(table.get("page_number") or 0)
            if not start_page <= page_number <= end_page:
                continue
            table_id = table.get("table_id")
            if table_id:
                tables[str(table_id)] = table
    return tables


def _load_table_source(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "headers" in data and "rows" in data and isinstance(data.get("rows"), list):
        first = data["rows"][0] if data["rows"] else {}
        if isinstance(first, dict) and "cells" in first:
            return grid_to_table_dict(data)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate OEL table candidates from evidence")
    parser.add_argument("--start-page", type=int, required=True)
    parser.add_argument("--end-page", type=int, required=True)
    parser.add_argument("--write", action="store_true", help="Write candidate JSON files")
    parser.add_argument(
        "--promote-gold",
        action="store_true",
        help="Also write refreshed output to gold/tables (used by HITL bridge)",
    )
    parser.add_argument(
        "--include-canonical-grids",
        action="store_true",
        default=True,
        help="Also regenerate from data/canonical/grids when no evidence/tables file exists",
    )
    args = parser.parse_args()

    if args.start_page > args.end_page:
        raise SystemExit("--start-page must be <= --end-page")

    settings = get_settings()
    candidate_dir = settings.gold_dir / "candidates" / "tables"
    gold_table_dir = settings.gold_dir / "tables"
    generator = TableGoldGenerator()
    results: list[dict[str, object]] = []

    # Priority: validated_structure > canonical_grid > document_ai_evidence
    table_sources: dict[str, tuple[str, dict | Path]] = {}

    for table_id, table_dict in _load_validated_structure_tables(args.start_page, args.end_page).items():
        table_sources[table_id] = ("validated_structure", table_dict)

    if args.include_canonical_grids:
        for table_id, path in _latest_canonical_grids().items():
            if table_id not in table_sources:
                table_sources[table_id] = ("canonical_grid", path)

    for table_id, path in _latest_evidence_tables().items():
        if table_id not in table_sources:
            table_sources[table_id] = ("document_ai_evidence", path)

    for table_id, (source_kind, source) in sorted(table_sources.items()):
        if isinstance(source, Path):
            table_input = _load_table_source(source)
            source_label = str(source.relative_to(PROJECT_ROOT))
        else:
            table_input = source
            source_label = f"data/intermediate/validated_structure (page {table_input.get('page_number')})"

        page_number = int(table_input.get("page_number") or 0)
        if not args.start_page <= page_number <= args.end_page:
            continue
        if table_input.get("table_type") != "chemical_oel":
            continue

        candidate = generator.generate(table_input)
        rows = candidate.get("rows") or []
        result = {
            "table_id": table_id,
            "page_number": page_number,
            "source_kind": source_kind,
            "source": source_label,
            "header_mapping": candidate.get("header_mapping"),
            "mapping_status": candidate.get("mapping_status"),
            "gold_allowed": candidate.get("gold_allowed"),
            "physical_column_count": candidate.get("physical_column_count"),
            "row_count": len(rows),
            "stel_fields": sum("STEL" in row for row in rows),
            "stel_values": sum(bool(row.get("STEL", {}).get("value")) for row in rows),
            "twa_values": sum(bool(row.get("TWA", {}).get("value")) for row in rows),
            "ceiling_values": sum(bool(row.get("ceiling", {}).get("value")) for row in rows),
        }
        results.append(result)

        if args.write:
            candidate_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(candidate, ensure_ascii=False, indent=2)
            (candidate_dir / f"{table_id}.json").write_text(payload, encoding="utf-8")
            if args.promote_gold:
                gold_table_dir.mkdir(parents=True, exist_ok=True)
                (gold_table_dir / f"{table_id}.json").write_text(payload, encoding="utf-8")

    print(
        json.dumps(
            {
                "mode": "write" if args.write else "dry_run",
                "promote_gold": args.promote_gold,
                "candidate_directory": str(candidate_dir),
                "gold_table_directory": str(gold_table_dir),
                "tables": results,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
