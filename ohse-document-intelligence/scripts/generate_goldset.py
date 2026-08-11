"""CLI for AI-assisted OHSE goldset generation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.logging import configure_logging, get_logger
from goldset_generator.pipeline import GoldsetPipeline

logger = get_logger(__name__)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Generate OHSE document goldset")
    parser.add_argument("--file", required=True, type=Path, help="Path to PDF file")
    parser.add_argument("--start-page", type=int, required=True, help="Start page (1-based)")
    parser.add_argument("--end-page", type=int, required=True, help="End page (1-based, inclusive)")
    parser.add_argument("--force", action="store_true", help="Regenerate even approved/corrected pages")
    parser.add_argument(
        "--skip-document-ai",
        action="store_true",
        help="Skip Document AI API call (requires cached intermediate JSON)",
    )
    args = parser.parse_args()

    if args.start_page > args.end_page:
        raise SystemExit("--start-page must be <= --end-page")

    pipeline = GoldsetPipeline()
    result = pipeline.run(
        args.file,
        start_page=args.start_page,
        end_page=args.end_page,
        force=args.force,
        skip_document_ai=args.skip_document_ai,
    )

    print("Goldset generation complete:")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
