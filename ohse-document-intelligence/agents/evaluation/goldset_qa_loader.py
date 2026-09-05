"""Load the frozen Goldset QA workbook. Never writes or mutates the file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

GOLDSET_QA_FILENAME = "Goldset_QA.xlsx"
DEFAULT_GOLDSET_QA_PATH = PROJECT_ROOT / GOLDSET_QA_FILENAME
REQUIRED_COLUMNS = (
    "id",
    "question",
    "reference_answer",
    "source_page",
    "source_text",
    "question_type",
)
EXPECTED_ROW_COUNT = 50
SUPPORTED_QUESTION_TYPES = frozenset(
    {"definition", "narrative", "exception", "condition", "numeric", "table", "formula"}
)


@dataclass(frozen=True)
class GoldsetQAItem:
    id: str
    question: str
    reference_answer: str
    source_page: int
    source_text: str
    question_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "reference_answer": self.reference_answer,
            "source_page": self.source_page,
            "source_text": self.source_text,
            "question_type": self.question_type,
        }


def default_goldset_qa_path() -> Path:
    return DEFAULT_GOLDSET_QA_PATH


def _cell_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_source_page(value: Any, *, row_id: str) -> int:
    if value is None or _cell_str(value) == "":
        raise ValueError(f"Goldset_QA row {row_id} is missing source_page")
    if isinstance(value, bool):
        raise ValueError(f"Goldset_QA row {row_id} has invalid source_page")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"Goldset_QA row {row_id} has non-integer source_page={value}")
    text = _cell_str(value)
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"Goldset_QA row {row_id} has invalid source_page={value!r}") from exc


def load_goldset_qa(path: Path | None = None) -> list[GoldsetQAItem]:
    """Read Goldset_QA.xlsx as evaluation ground truth. Read-only."""
    import openpyxl

    xlsx = path or default_goldset_qa_path()
    if not xlsx.is_file():
        raise FileNotFoundError(f"Frozen Goldset QA not found: {xlsx}")

    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()

    if not rows:
        raise ValueError(f"Goldset_QA is empty: {xlsx}")

    headers = [_cell_str(c) for c in rows[0]]
    missing = [col for col in REQUIRED_COLUMNS if col not in headers]
    if missing:
        raise ValueError(f"Goldset_QA missing columns {missing}; found {headers}")
    idx = {name: headers.index(name) for name in REQUIRED_COLUMNS}

    items: list[GoldsetQAItem] = []
    for raw in rows[1:]:
        if raw is None or all(c is None or _cell_str(c) == "" for c in raw):
            continue
        item_id = _cell_str(raw[idx["id"]])
        question = _cell_str(raw[idx["question"]])
        question_type = _cell_str(raw[idx["question_type"]]).lower()
        if not item_id or not question or not question_type:
            raise ValueError(f"Goldset_QA has an incomplete row near {item_id or 'unknown id'}")
        if question_type not in SUPPORTED_QUESTION_TYPES:
            raise ValueError(f"Goldset_QA row {item_id} has unsupported question_type={question_type}")
        items.append(
            GoldsetQAItem(
                id=item_id,
                question=question,
                reference_answer=_cell_str(raw[idx["reference_answer"]]),
                source_page=_parse_source_page(raw[idx["source_page"]], row_id=item_id),
                source_text=_cell_str(raw[idx["source_text"]]),
                question_type=question_type,
            )
        )

    if len(items) != EXPECTED_ROW_COUNT:
        raise ValueError(
            f"Goldset_QA expected {EXPECTED_ROW_COUNT} rows, found {len(items)} in {xlsx}"
        )
    return items
