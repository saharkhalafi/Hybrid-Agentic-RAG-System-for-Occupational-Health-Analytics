"""Deterministic validator for LLM-proposed OCR word repairs — independent of LLM confidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from goldset_generator.persian_text_repair import PROTECTED_PHRASES

CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
UNIT_PATTERN = re.compile(r"\b(?:ppm|mg/m³|mg/m3|dBA|mg/L|f/ml|%)\b", re.I)
NUMBER_PATTERN = re.compile(r"\d+(?:[./]\d+)?")
ENGLISH_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9\-–]*\b")
SENTENCE_REWRITE_MARKERS = re.compile(r"(?:بررسی\s+می|می\s*‌?شود|لازم\s+است|باید\s+توجه)")


class ValidationStatus(str, Enum):
    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass
class LlmRepairValidation:
    status: ValidationStatus
    repair_confidence: float
    validation_status: ValidationStatus
    issues: list[str] = field(default_factory=list)
    unchanged_context_ratio: float = 1.0
    word_count_delta: int = 0
    numeric_changed: bool = False
    protected_token_changed: bool = False


@dataclass
class ApplyRepairResult:
    text: str
    applied: bool
    validation: LlmRepairValidation


def _extract_numbers(text: str) -> list[str]:
    return NUMBER_PATTERN.findall(text)


def _context_unchanged(text: str, original: str, replacement: str) -> tuple[bool, float]:
    start = text.find(original)
    if start < 0:
        return False, 0.0
    end = start + len(original)
    rebuilt = text[:start] + replacement + text[end:]
    prefix = text[:start]
    suffix = text[end:]
    if rebuilt[:start] != prefix or rebuilt[start + len(replacement) :] != suffix:
        return False, 0.0
    unchanged = sum(1 for a, b in zip(text, rebuilt) if a == b)
    ratio = unchanged / max(len(text), len(rebuilt), 1)
    return True, ratio


def validate_llm_repair(
    text: str,
    repair: dict[str, Any],
    *,
    protected_tokens: list[str] | None = None,
) -> LlmRepairValidation:
    """Validate a single LLM repair proposal against source text invariants."""
    protected_tokens = protected_tokens or []
    issues: list[str] = []
    original = (repair.get("original") or "").strip()
    replacement = (repair.get("replacement") or "").strip()
    repair_type = repair.get("type") or ""
    llm_confidence = float(repair.get("confidence") or 0.0)

    if repair_type and repair_type != "ocr_word_repair":
        issues.append("invalid repair type")
    if not original:
        issues.append("empty original span")
    if not replacement:
        issues.append("empty replacement span")
    if original == replacement:
        issues.append("no-op repair")

    if original and original not in text:
        issues.append("original span not found in source text")

    numeric_changed = False
    if original and replacement and _extract_numbers(original) != _extract_numbers(replacement):
        numeric_changed = True
        issues.append("numeric values changed in repair span")

    protected_token_changed = False
    for token in protected_tokens:
        in_orig = token in original
        in_repl = token in replacement
        if in_orig and not in_repl:
            protected_token_changed = True
            issues.append(f"protected token removed: {token}")
        if not in_orig and in_repl and token not in text:
            protected_token_changed = True
            issues.append(f"protected token introduced: {token}")

    if original and replacement:
        if CAS_PATTERN.findall(original) != CAS_PATTERN.findall(replacement):
            issues.append("CAS identifier changed")
        if UNIT_PATTERN.findall(original) != UNIT_PATTERN.findall(replacement):
            issues.append("unit changed")
        eng_orig = set(ENGLISH_TOKEN.findall(original))
        eng_repl = set(ENGLISH_TOKEN.findall(replacement))
        if eng_orig != eng_repl:
            issues.append("English technical token changed")

    if original and replacement and original in text:
        for phrase in PROTECTED_PHRASES:
            if phrase in text and original in phrase and original != replacement:
                issues.append(f"modifies protected phrase: {phrase}")

    word_count_delta = 0
    unchanged_ratio = 0.0
    if original and replacement and original in text:
        word_count_delta = len(replacement.split()) - len(original.split())
        if abs(word_count_delta) > 3:
            issues.append("word count delta too large")
        ok, unchanged_ratio = _context_unchanged(text, original, replacement)
        if not ok:
            issues.append("surrounding context invariant failed")
        if len(replacement) > len(original) * 2 + 15:
            issues.append("replacement span too long relative to original")
        if SENTENCE_REWRITE_MARKERS.search(replacement) and not SENTENCE_REWRITE_MARKERS.search(original):
            issues.append("replacement looks like sentence rewrite")

    # Hard rejects — invariants failed
    if issues:
        return LlmRepairValidation(
            status=ValidationStatus.REJECT,
            repair_confidence=llm_confidence,
            validation_status=ValidationStatus.REJECT,
            issues=issues,
            unchanged_context_ratio=unchanged_ratio,
            word_count_delta=word_count_delta,
            numeric_changed=numeric_changed,
            protected_token_changed=protected_token_changed,
        )

    review_flags: list[str] = []
    if llm_confidence < 0.9:
        review_flags.append("llm confidence below 0.9")
    if abs(word_count_delta) > 1:
        review_flags.append("multi-word boundary adjustment")
    if unchanged_ratio < 0.85 and len(text) > 40:
        review_flags.append("low unchanged-context ratio")

    if review_flags or llm_confidence < 0.7:
        if llm_confidence < 0.7:
            return LlmRepairValidation(
                status=ValidationStatus.REJECT,
                repair_confidence=llm_confidence,
                validation_status=ValidationStatus.REJECT,
                issues=["llm confidence below 0.7"],
                unchanged_context_ratio=unchanged_ratio,
                word_count_delta=word_count_delta,
            )
        return LlmRepairValidation(
            status=ValidationStatus.REVIEW,
            repair_confidence=llm_confidence,
            validation_status=ValidationStatus.REVIEW,
            issues=review_flags,
            unchanged_context_ratio=unchanged_ratio,
            word_count_delta=word_count_delta,
        )

    return LlmRepairValidation(
        status=ValidationStatus.ACCEPT,
        repair_confidence=llm_confidence,
        validation_status=ValidationStatus.ACCEPT,
        issues=[],
        unchanged_context_ratio=unchanged_ratio,
        word_count_delta=word_count_delta,
    )


def apply_validated_repair(text: str, repair: dict[str, Any]) -> ApplyRepairResult:
    """Apply one repair if validation ACCEPT; otherwise leave text unchanged."""
    validation = validate_llm_repair(text, repair)
    original = repair.get("original") or ""
    replacement = repair.get("replacement") or ""
    if validation.status != ValidationStatus.ACCEPT or not original:
        return ApplyRepairResult(text=text, applied=False, validation=validation)
    start = text.find(original)
    if start < 0:
        validation.status = ValidationStatus.REJECT
        validation.validation_status = ValidationStatus.REJECT
        validation.issues.append("original span not found at apply time")
        return ApplyRepairResult(text=text, applied=False, validation=validation)
    new_text = text[:start] + replacement + text[start + len(original) :]
    return ApplyRepairResult(text=new_text, applied=True, validation=validation)


def apply_accepted_repairs(
    text: str,
    repairs: list[dict[str, Any]],
    *,
    protected_tokens: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply repairs in reverse position order; return (new_text, applied, skipped)."""
    protected_tokens = protected_tokens or []
    indexed: list[tuple[int, dict[str, Any]]] = []
    for repair in repairs:
        original = repair.get("original") or ""
        pos = text.find(original)
        if pos >= 0:
            indexed.append((pos, repair))

    indexed.sort(key=lambda item: item[0], reverse=True)
    result = text
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for _, repair in indexed:
        validation = validate_llm_repair(result, repair, protected_tokens=protected_tokens)
        if validation.status != ValidationStatus.ACCEPT:
            skipped.append({**repair, "validation": validation.__dict__})
            continue
        apply_result = apply_validated_repair(result, repair)
        if apply_result.applied:
            result = apply_result.text
            applied.append({**repair, "validation": apply_result.validation.__dict__})
        else:
            skipped.append({**repair, "validation": apply_result.validation.__dict__})

    return result, applied, skipped


def compute_text_delta(before: str, after: str) -> dict[str, Any]:
    """Report span-level changes between two texts."""
    if before == after:
        return {"changed": False, "spans": []}
    # Simple diff: find common prefix/suffix
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(before) - prefix, len(after) - prefix)
        and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]
    ):
        suffix += 1
    orig_span = before[prefix : len(before) - suffix] if suffix else before[prefix:]
    new_span = after[prefix : len(after) - suffix] if suffix else after[prefix:]
    return {
        "changed": True,
        "spans": [{"original": orig_span, "replacement": new_span, "position": prefix}],
        "chars_before": len(before),
        "chars_after": len(after),
    }
