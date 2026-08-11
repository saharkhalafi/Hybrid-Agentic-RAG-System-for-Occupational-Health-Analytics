"""Semantic chunk quality analysis (Phase C.2) — read-only, no Gold mutation."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from config.settings import PROJECT_ROOT

GOLD_RAG = PROJECT_ROOT / "gold" / "rag" / "semantic_text_production.jsonl"


@dataclass
class ChunkQualityReport:
    total_chunks: int = 0
    avg_length: float = 0.0
    median_length: float = 0.0
    very_short_count: int = 0  # < 50 chars
    very_long_count: int = 0  # > 2000 chars
    fragmentation_rate: float = 0.0
    duplicate_count: int = 0
    near_duplicate_count: int = 0
    malformed_ocr_count: int = 0
    incomplete_sentence_count: int = 0
    section_complete_count: int = 0
    poor_retrieval_units: list[dict[str, Any]] = field(default_factory=list)
    length_distribution: dict[str, int] = field(default_factory=dict)
    issues_by_type: dict[str, int] = field(default_factory=dict)


_OCR_ARTIFACTS = re.compile(r"[\u200c\u200b]|\.{3,}|[^\u0600-\u06FFa-zA-Z0-9\s\.\,\:\;\-\(\)\[\]\/\%]+")
_INCOMPLETE_END = re.compile(r"[^\.!\?؟]\s*$")


def _load_chunks() -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if not GOLD_RAG.exists():
        # fallback: try postgres export path
        alt = PROJECT_ROOT / "data" / "semantic" / "semantic_text_production.jsonl"
        path = alt if alt.exists() else GOLD_RAG
    else:
        path = GOLD_RAG
    if not path.exists():
        return chunks
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(json.loads(line))
    return chunks


def _content(rec: dict[str, Any]) -> str:
    for key in ("text", "normalized_text", "semantic_text", "raw_text"):
        v = rec.get(key)
        if v and isinstance(v, str):
            return v.strip()
    return ""


def _is_fragment(text: str) -> bool:
    if len(text) < 30:
        return True
    if text.count("\n") > 5 and len(text) < 100:
        return True
    if re.search(r"^[a-zA-Z0-9\.\s]{0,20}$", text):
        return True
    return False


def _malformed_ocr(text: str) -> bool:
    if _OCR_ARTIFACTS.search(text):
        return True
    if re.search(r"\b[a-z]\s[a-z]\s[a-z]\b", text.lower()):  # spaced letters
        return True
    if text.count(" ") > len(text) * 0.5 and len(text) < 80:
        return True
    return False


def _incomplete_sentence(text: str) -> bool:
    if len(text) < 20:
        return True
    return bool(_INCOMPLETE_END.search(text.strip()))


def analyze_chunk_quality(chunks: list[dict[str, Any]] | None = None) -> ChunkQualityReport:
    chunks = chunks or _load_chunks()
    report = ChunkQualityReport(total_chunks=len(chunks))
    if not chunks:
        return report

    lengths: list[int] = []
    contents: list[tuple[str, dict[str, Any]]] = []
    seen_exact: set[str] = set()

    for rec in chunks:
        text = _content(rec)
        lengths.append(len(text))
        contents.append((text, rec))

        if len(text) < 50:
            report.very_short_count += 1
        if len(text) > 2000:
            report.very_long_count += 1

        if _is_fragment(text):
            report.fragmentation_rate += 1
            report.issues_by_type["fragment"] = report.issues_by_type.get("fragment", 0) + 1

        if _malformed_ocr(text):
            report.malformed_ocr_count += 1
            report.issues_by_type["malformed_ocr"] = report.issues_by_type.get("malformed_ocr", 0) + 1

        if _incomplete_sentence(text):
            report.incomplete_sentence_count += 1
            report.issues_by_type["incomplete_sentence"] = report.issues_by_type.get("incomplete_sentence", 0) + 1

        section = rec.get("section") or {}
        if section.get("title") or section.get("section_id"):
            report.section_complete_count += 1

        fp = text[:200]
        if fp in seen_exact:
            report.duplicate_count += 1
        seen_exact.add(fp)

        issues: list[str] = []
        if _is_fragment(text):
            issues.append("fragment")
        if _malformed_ocr(text):
            issues.append("malformed_ocr")
        if _incomplete_sentence(text):
            issues.append("incomplete_sentence")
        if len(text) < 50:
            issues.append("very_short")

        if len(issues) >= 2 or "fragment" in issues:
            if len(report.poor_retrieval_units) < 50:
                report.poor_retrieval_units.append({
                    "chunk_id": rec.get("chunk_id"),
                    "page": (rec.get("page_numbers") or [None])[0],
                    "length": len(text),
                    "issues": issues,
                    "preview": text[:120],
                })

    report.avg_length = sum(lengths) / len(lengths) if lengths else 0.0
    sorted_lens = sorted(lengths)
    report.median_length = sorted_lens[len(sorted_lens) // 2]

    report.fragmentation_rate = report.fragmentation_rate / len(chunks) if chunks else 0.0

    # Near-duplicates (sample-based for performance)
    for i in range(min(len(contents), 200)):
        for j in range(i + 1, min(len(contents), 200)):
            if SequenceMatcher(None, contents[i][0][:100], contents[j][0][:100]).ratio() > 0.9:
                report.near_duplicate_count += 1

    buckets = Counter()
    for ln in lengths:
        if ln < 50:
            buckets["0-49"] += 1
        elif ln < 150:
            buckets["50-149"] += 1
        elif ln < 400:
            buckets["150-399"] += 1
        elif ln < 800:
            buckets["400-799"] += 1
        else:
            buckets["800+"] += 1
    report.length_distribution = dict(buckets)

    return report


def render_chunk_quality_markdown(report: ChunkQualityReport) -> str:
    lines = [
        "# Semantic Chunk Quality Analysis",
        "",
        f"**Total chunks analyzed:** {report.total_chunks}",
        "",
        "## Size Metrics",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Average length | {report.avg_length:.1f} chars |",
        f"| Median length | {report.median_length:.0f} chars |",
        f"| Very short (<50) | {report.very_short_count} ({100*report.very_short_count/max(1,report.total_chunks):.1f}%) |",
        f"| Very long (>2000) | {report.very_long_count} |",
        "",
        "## Quality Issues",
        "",
        f"| Issue | Count | Rate |",
        f"|-------|-------|------|",
        f"| Fragmentation | {int(report.fragmentation_rate * report.total_chunks)} | {100*report.fragmentation_rate:.1f}% |",
        f"| Malformed OCR | {report.malformed_ocr_count} | {100*report.malformed_ocr_count/max(1,report.total_chunks):.1f}% |",
        f"| Incomplete sentences | {report.incomplete_sentence_count} | {100*report.incomplete_sentence_count/max(1,report.total_chunks):.1f}% |",
        f"| Exact duplicates | {report.duplicate_count} | |",
        f"| Near-duplicates (sample) | {report.near_duplicate_count} | |",
        f"| Section metadata present | {report.section_complete_count} | |",
        "",
        "## Length Distribution",
        "",
    ]
    for bucket, count in sorted(report.length_distribution.items()):
        lines.append(f"- **{bucket}:** {count}")
    lines.extend(["", "## Poor Retrieval Units (sample)", ""])
    for item in report.poor_retrieval_units[:15]:
        lines.append(f"- `{item['chunk_id']}` p.{item.get('page')} — {item['issues']}: \"{item['preview']}...\"")
    lines.extend([
        "",
        "## Recommendations",
        "",
        "1. Do NOT mutate immutable Gold — use versioned chunk transformation if merging fragments.",
        "2. Filter very short fragments (<50 chars) at retrieval time unless query is exact match.",
        "3. Consider sentence-boundary chunking for future corpus versions.",
        "4. OCR cleanup pass as separate `semantic_text_v2` source_type (not overwriting v1).",
        "",
    ])
    return "\n".join(lines)
