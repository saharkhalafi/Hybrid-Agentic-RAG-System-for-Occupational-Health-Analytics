"""Geometry + pattern validated heading/section detection before semantic chunking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from goldset_generator.persian_text_repair import repair_ocr_text

VALID_HEADING = re.compile(
    r"^(?:"
    r"بخش\s+"
    r"|فصل\s+"
    r"|مقدمه\s*"
    r"|منابع\s+اصلی"
    r"|تعریف\s+"
    r"|مبحث\s+"
    r"|پیوست\s+(?:\d+|[A-Za-z\u0600-\u06FF]{1,4})\s*$"
    r")",
    re.I,
)
SENTENCE_LIKE = re.compile(
    r"(?:^\.|^\،|^:|"
    r"\sکه\s|\sاگر\s|\sمی\s|\sمی‌|\sمی\u200c|\sمی‌کند|\sمی\s+کند|\sمیشود|\sباشد|\sباشند|\sگردد|\sشود|\sدارند|\sدارد|\sباید|\sنباید|"
    r"منتقل\s+شده|سعی\s+شده|قرار\s+گرفته|،{1,})",
    re.I,
)
TOPIC_BOUNDARY = re.compile(
    r"^(?:"
    r"منابع\s+اصلی"
    r"|تعریف\s+"
    r"|پیوست\s+(?:\d+|[A-Za-z\u0600-\u06FF]{1,4})\s*$"
    r"|بخش\s+"
    r"|فصل\s+"
    r"|مقدمه\s*"
    r")",
    re.I,
)


@dataclass
class SectionNode:
    section_id: str
    title: str
    level: int
    path: list[str] = field(default_factory=list)


@dataclass
class PageStructure:
    sections: list[SectionNode]
    paragraph_sections: dict[int, SectionNode]
    topic_boundaries: set[int]


def _leading_indent(raw: str) -> int:
    match = re.match(r"^(\s+)", raw or "")
    return len(match.group(1)) if match else 0


def _is_valid_heading(text: str, *, leading_indent: int = 0, max_font: float = 0) -> bool:
    line = repair_ocr_text(text).split("\n")[0].strip()
    if not line:
        return False
    if SENTENCE_LIKE.search(line) and not VALID_HEADING.match(line.split(" که ")[0]):
        return False
    if len(line.split()) > 12 and not line.endswith(":"):
        return False
    if VALID_HEADING.match(line):
        return True
    if leading_indent >= 6 and TOPIC_BOUNDARY.match(line):
        return True
    if len(line) > 85:
        return False
    if len(line) < 50 and not SENTENCE_LIKE.search(line):
        if line.endswith((":", "؟", "!")) and len(line.split()) <= 8:
            return True
    return False


def _heading_level(title: str, leading_indent: int) -> int:
    if re.match(r"^بخش\s+", title, re.I):
        return 1
    if re.match(r"^(?:فصل|مقدمه)\s*", title, re.I):
        return 2
    if leading_indent >= 6:
        return 3
    return 2


def detect_page_structure(
    paragraphs: list[dict[str, Any]],
    *,
    page_number: int,
) -> PageStructure:
    sections: list[SectionNode] = []
    paragraph_sections: dict[int, SectionNode] = {}
    topic_boundaries: set[int] = set()
    current = SectionNode(
        section_id=f"section_{page_number:03d}_000",
        title="",
        level=0,
        path=[],
    )
    path_stack: list[str] = []

    for idx, paragraph in enumerate(paragraphs):
        raw = paragraph.get("text") or ""
        repaired = repair_ocr_text(raw)
        leading = _leading_indent(raw)
        max_font = float(paragraph.get("max_font_size") or 0)

        stripped = repaired.strip()
        if TOPIC_BOUNDARY.match(stripped):
            topic_boundaries.add(idx)

        if _is_valid_heading(raw, leading_indent=leading, max_font=max_font):
            title = repaired.split("\n")[0].strip()
            if SENTENCE_LIKE.search(title) and not VALID_HEADING.match(title.split(" که ")[0]):
                paragraph_sections[idx] = current
                continue
            if " که " in title:
                title = title.split(" که ")[0].strip()

            level = _heading_level(title, leading)
            if level == 1:
                path_stack = [title]
            elif level == 2:
                path_stack = path_stack[:1] + [title] if path_stack else [title]
            else:
                path_stack = path_stack[:2] + [title]

            section_id = f"section_{page_number:03d}_{len(sections)+1:02d}"
            current = SectionNode(
                section_id=section_id,
                title=title,
                level=level,
                path=list(path_stack),
            )
            sections.append(current)
            topic_boundaries.add(idx)
            paragraph_sections[idx] = current
            continue

        paragraph_sections[idx] = current

    return PageStructure(
        sections=sections,
        paragraph_sections=paragraph_sections,
        topic_boundaries=topic_boundaries,
    )


def section_for_paragraph(structure: PageStructure, paragraph_index: int) -> SectionNode:
    return structure.paragraph_sections.get(
        paragraph_index,
        SectionNode(section_id="", title="", level=0, path=[]),
    )
