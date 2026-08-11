"""Document AI processing orchestration."""

from __future__ import annotations

from pathlib import Path

from config.logging import get_logger
from document_ai.client import DocumentAIClient
from document_ai.parser import parse_document_ai_response
from document_ai.schema import DocumentAIExtractionResult

logger = get_logger(__name__)


class DocumentAIProcessor:
    def __init__(self, client: DocumentAIClient | None = None) -> None:
        self.client = client or DocumentAIClient()

    def process_pdf(self, pdf_path: Path) -> tuple[DocumentAIExtractionResult, dict]:
        content = pdf_path.read_bytes()
        document = self.client.process_document(content=content, mime_type="application/pdf")
        raw_json = self.client.document_to_dict(document)
        parsed = parse_document_ai_response(raw_json)
        return parsed, raw_json
