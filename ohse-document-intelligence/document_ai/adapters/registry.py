"""Detect Document AI response format and route to the correct adapter."""

from __future__ import annotations

from typing import Any

from config.logging import get_logger
from document_ai.adapters.classic_adapter import ClassicDocumentAIAdapter
from document_ai.adapters.layout_parser_adapter import LayoutParserAdapter
from document_ai.schema import DocumentAIExtractionResult

logger = get_logger(__name__)


def detect_processor_format(raw_json: dict[str, Any]) -> str:
    if raw_json.get("documentLayout"):
        return "layout_parser"
    if raw_json.get("pages"):
        return "classic"
    return "unknown"


def adapt_document_ai_response(raw_json: dict[str, Any]) -> DocumentAIExtractionResult:
    processor_format = detect_processor_format(raw_json)
    if processor_format == "layout_parser":
        result = LayoutParserAdapter().adapt(raw_json)
    elif processor_format == "classic":
        result = ClassicDocumentAIAdapter().adapt(raw_json)
    else:
        logger.warning("unknown_document_ai_format", keys=list(raw_json.keys()))
        result = DocumentAIExtractionResult(
            pages=[],
            full_text=raw_json.get("text", ""),
            raw_json=raw_json,
            processor_format="unknown",
        )

    logger.info(
        "adapted_document_ai_response",
        format=result.processor_format,
        pages=len(result.pages),
        tables=len(result.tables),
        text_length=len(result.full_text),
    )
    return result
