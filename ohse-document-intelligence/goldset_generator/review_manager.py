"""Human review management, candidate vs approved gold separation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTECTED_STATUSES = {"approved", "corrected"}


class ReviewManager:
    def __init__(self, gold_dir: Path) -> None:
        self.gold_dir = gold_dir
        self.pages_dir = gold_dir / "pages"
        self.candidates_dir = gold_dir / "candidates" / "pages"
        self.entities_dir = gold_dir / "entities"
        self.qa_dir = gold_dir / "qa"
        self.rows_dir = gold_dir / "rows"
        self.review_dir = gold_dir / "review"
        self.review_report_path = self.review_dir / "review_queue.json"

        for directory in (
            self.pages_dir,
            self.candidates_dir,
            self.entities_dir,
            self.qa_dir,
            self.rows_dir,
            self.review_dir,
            gold_dir / "candidates" / "tables",
            gold_dir / "candidates" / "formulas",
            gold_dir / "tables",
            gold_dir / "formulas",
            gold_dir / "benchmarks",
            gold_dir / "rag",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def page_gold_path(self, page_number: int) -> Path:
        return self.pages_dir / f"page_{page_number:03d}.json"

    def candidate_page_path(self, page_number: int) -> Path:
        return self.candidates_dir / f"page_{page_number:03d}.json"

    def load_page_gold(self, page_number: int) -> dict[str, Any] | None:
        path = self.page_gold_path(page_number)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def should_skip_page(self, page_number: int, force: bool = False) -> bool:
        if force:
            return False
        existing = self.load_page_gold(page_number)
        if not existing:
            return False
        status = existing.get("review_status", "pending")
        return status in PROTECTED_STATUSES

    def write_page_index(
        self,
        page_number: int,
        index_data: dict[str, Any],
        *,
        force: bool = False,
    ) -> Path:
        """Minimal approved/index page gold — evidence pointers, not semantic interpretation."""
        path = self.page_gold_path(page_number)
        path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_semantic_page(
        self,
        page_number: int,
        semantic_data: dict[str, Any],
        *,
        force: bool = False,
    ) -> Path:
        """Full semantic interpretation — always written to candidates/."""
        path = self.candidate_page_path(page_number)
        path.write_text(json.dumps(semantic_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_page_gold(
        self,
        page_number: int,
        gold_data: dict[str, Any],
        *,
        force: bool = False,
    ) -> Path:
        return self.write_semantic_page(page_number, gold_data, force=force)

    def write_entities(self, page_number: int, entities: list[dict[str, Any]]) -> Path:
        path = self.entities_dir / f"entities_{page_number:03d}.json"
        path.write_text(
            json.dumps(
                {"page_number": page_number, "entities": entities, "review_status": "pending"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def write_qa(self, page_number: int, qa_items: list[dict[str, Any]]) -> Path:
        path = self.qa_dir / f"qa_{page_number:03d}.json"
        path.write_text(
            json.dumps(
                {"page_number": page_number, "qa": qa_items, "review_status": "pending"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def write_validation_report(self, page_number: int, report: dict[str, Any]) -> Path:
        path = self.review_dir / f"validation_page_{page_number:03d}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_table_gold(
        self,
        table_id: str,
        data: dict[str, Any],
        *,
        force: bool = False,
    ) -> Path:
        path = self.gold_dir / "tables" / f"{table_id}.json"
        if path.exists() and not force:
            existing = json.loads(path.read_text(encoding="utf-8"))
            page_number = existing.get("page_number")
            if page_number and self.should_skip_page(page_number, force=False):
                candidate_path = self.gold_dir / "candidates" / "tables" / f"{table_id}.json"
                candidate_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return candidate_path
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_formula_gold(self, formula_id: str, data: dict[str, Any]) -> Path:
        path = self.gold_dir / "formulas" / f"{formula_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_formula_candidate(self, formula_id: str, data: dict[str, Any]) -> Path:
        path = self.gold_dir / "candidates" / "formulas" / f"{formula_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_formula_review(self, formula_id: str, data: dict[str, Any]) -> Path:
        path = self.review_dir / f"formula_{formula_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def append_review_items(self, items: list[dict[str, Any]]) -> None:
        existing: dict[str, Any] = {"items": [], "updated_at": None}
        if self.review_report_path.exists():
            existing = json.loads(self.review_report_path.read_text(encoding="utf-8"))

        existing["items"].extend(items)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.review_report_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_benchmarks(self, benchmarks: dict[str, Any]) -> None:
        bench_dir = self.gold_dir / "benchmarks"
        for name, data in benchmarks.items():
            path = bench_dir / name
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
