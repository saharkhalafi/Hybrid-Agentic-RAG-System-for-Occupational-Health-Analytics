"""PDF loading and page extraction via PyMuPDF."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

from config.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PageContent:
    page_number: int
    text: str
    has_digital_text: bool
    width: float
    height: float
    image_bytes: bytes | None = None


@dataclass(frozen=True)
class LoadedDocument:
    path: Path
    content_hash: str
    page_count: int
    pages: list[PageContent]


def compute_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_pdf(
    path: Path,
    page_limit: int | None = None,
    page_start: int = 1,
    page_end: int | None = None,
    render_dpi: int = 300,
) -> LoadedDocument:
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    content_hash = compute_file_hash(path)
    pages: list[PageContent] = []

    with fitz.open(path) as doc:
        total_pages = doc.page_count
        start_index = max(page_start, 1) - 1

        if page_end is not None:
            end_index = min(page_end, total_pages) - 1
        elif page_limit is not None:
            end_index = min(start_index + page_limit - 1, total_pages - 1)
        else:
            end_index = total_pages - 1

        if start_index > end_index:
            raise ValueError(f"Invalid page range: {page_start}-{page_end or page_limit}")

        logger.info(
            "loading_pdf",
            path=str(path),
            page_start=start_index + 1,
            page_end=end_index + 1,
            total=total_pages,
        )

        for index in range(start_index, end_index + 1):
            page = doc.load_page(index)
            text = page.get_text("text").strip()
            has_digital_text = len(text) > 20
            image_bytes = None

            if not has_digital_text:
                matrix = fitz.Matrix(render_dpi / 72, render_dpi / 72)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                image_bytes = pix.tobytes("png")

            pages.append(
                PageContent(
                    page_number=index + 1,
                    text=text,
                    has_digital_text=has_digital_text,
                    width=page.rect.width,
                    height=page.rect.height,
                    image_bytes=image_bytes,
                )
            )

    return LoadedDocument(
        path=path,
        content_hash=content_hash,
        page_count=len(pages),
        pages=pages,
    )
