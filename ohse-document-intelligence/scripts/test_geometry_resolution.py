"""Integration test for PDF geometry resolution."""

from __future__ import annotations

from pathlib import Path

from document_ai.client import DocumentAIClient
from document_ai.parser import parse_document_ai_response
from ingestion.geometry_resolver import PdfGeometryResolver


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PDF_PATH = (
    PROJECT_ROOT
    / "data"
    / "intermediate"
    / "document_ai_endpoint_test.pdf"
)


def main() -> None:
    print("=" * 80)
    print("PDF GEOMETRY RESOLUTION TEST")
    print("=" * 80)

    if not PDF_PATH.exists():
        raise FileNotFoundError(
            f"Test PDF not found:\n{PDF_PATH}"
        )

    print(f"PDF: {PDF_PATH}")
    print()

    # ------------------------------------------------------------------
    # 1. Document AI
    # ------------------------------------------------------------------

    client = DocumentAIClient()

    content = PDF_PATH.read_bytes()

    document = client.process_document(content)

    raw_json = client.document_to_dict(document)

    extraction = parse_document_ai_response(raw_json)

    print(f"Processor format : {extraction.processor_format}")
    print(f"Pages            : {len(extraction.pages)}")
    print(f"Tables           : {len(extraction.tables)}")
    print(f"Full text length : {len(extraction.full_text)}")
    print()

    # ------------------------------------------------------------------
    # 2. Geometry resolution
    # ------------------------------------------------------------------

    resolver = PdfGeometryResolver(
        pdf_path=PDF_PATH,
    )

    report = resolver.resolve(extraction)

    # ------------------------------------------------------------------
    # 3. Report
    # ------------------------------------------------------------------

    print("=" * 80)
    print("GEOMETRY RESOLUTION REPORT")
    print("=" * 80)

    print()
    print(report)

    print()
    print("=" * 80)
    print("REPORT ATTRIBUTES")
    print("=" * 80)

    print(report.__dict__)

    # ------------------------------------------------------------------
    # 4. Generic statistics
    # ------------------------------------------------------------------

    data = report.__dict__

    for key, value in data.items():
        if isinstance(value, (int, float, str, bool)):
            print(f"{key}: {value}")

    print()
    print("=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()