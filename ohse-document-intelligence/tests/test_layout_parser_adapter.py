"""Tests for Layout Parser adapter."""

import json
from pathlib import Path

from document_ai.adapters.layout_parser_adapter import LayoutParserAdapter
from document_ai.adapters.registry import adapt_document_ai_response, detect_processor_format


FIXTURE = Path(__file__).resolve().parents[1] / "data" / "processed" / "8b9e2240e437_document_ai.json"


def test_detect_layout_parser_format():
    raw = {"documentLayout": {"blocks": []}}
    assert detect_processor_format(raw) == "layout_parser"


def test_layout_parser_extracts_tables_from_fixture():
    if not FIXTURE.exists():
        return
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = LayoutParserAdapter().adapt(raw)
    assert len(result.tables) == 3
    assert sum(len(table.flat_cells()) for table in result.tables) == 103
    assert result.processor_format == "layout_parser"


def test_registry_adapts_fixture():
    if not FIXTURE.exists():
        return
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = adapt_document_ai_response(raw)
    assert result.pages
    assert result.tables
