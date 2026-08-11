"""Base adapter protocol for Document AI response normalization."""

from __future__ import annotations

from typing import Any, Protocol

from document_ai.schema import DocumentAIExtractionResult


class DocumentAIAdapter(Protocol):
    def adapt(self, raw_json: dict[str, Any]) -> DocumentAIExtractionResult:
        ...
