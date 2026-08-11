"""Parse Document AI responses into normalized extraction artifacts."""

from __future__ import annotations

from typing import Any

from document_ai.adapters.registry import adapt_document_ai_response
from document_ai.schema import DocumentAIExtractionResult

# Backward-compatible aliases used by older imports
ParsedDocument = DocumentAIExtractionResult


def parse_document_ai_response(raw_json: dict[str, Any]) -> DocumentAIExtractionResult:
    return adapt_document_ai_response(raw_json)
