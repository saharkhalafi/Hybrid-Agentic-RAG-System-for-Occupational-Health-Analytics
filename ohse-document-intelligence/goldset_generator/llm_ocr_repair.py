"""LLM-assisted OCR word repair — repairs broken fragments only, never rewrites sentences."""

from __future__ import annotations

import re
from typing import Any, Callable, Protocol

from config.logging import get_logger
from goldset_generator.gemini_client import GeminiClient
from goldset_generator.llm_ocr_repair_cache import LlmOcrRepairCache
from goldset_generator.llm_ocr_repair_validator import (
    ValidationStatus,
    apply_accepted_repairs,
    compute_text_delta,
    validate_llm_repair,
)
from goldset_generator.persian_text_repair import (
    PROTECTED_PHRASES,
    assess_fragmentation,
    normalize_repaired_text,
    protect_technical_tokens,
)

logger = get_logger(__name__)

SUSPICIOUS_MERGED = re.compile(
    r"[اآی]{3,}|[\u0600-\u06FF]{8,}[اآی][\u0600-\u06FF]{3,}|هریکاز|زانی|بنابرا\s|تدو\s|مورداستفاده|قرارگرفته|ازیک|کهاز|درپانی|طوالن|یطی\s*مح"
)
MEANINGLESS_TOKEN = re.compile(
    r"[\u0600-\u06FF]*(?:[اآی]\s+[اآی]|(?:زان|تدو|بنابرا|یطی|والن)\s?)[\u0600-\u06FF]*"
)

OCR_REPAIR_SYSTEM = """
You are an OCR repair engine, NOT an editor or translator.

TASK:
Identify Persian words that became meaningless because OCR separated, merged,
substituted, or fragmented their characters. Return ONLY minimal replacement
operations to reconstruct those broken words.

STRICT RULES:
- Repair ONLY broken OCR words/fragments.
- Do NOT paraphrase, summarize, rewrite sentences, or improve style.
- Do NOT change terminology, numbers, units, CAS numbers, chemical names, or English tokens.
- Do NOT add or remove information.
- Do NOT reorder words outside the broken span.
- If no safe repair exists, return {"repairs": []}.

OUTPUT JSON ONLY:
{
  "repairs": [
    {
      "original": "exact broken substring from source text",
      "replacement": "reconstructed Persian word/phrase",
      "type": "ocr_word_repair",
      "confidence": 0.95,
      "reason": "fragmented Persian word"
    }
  ]
}

confidence: 0.0-1.0 (your estimate; final acceptance is decided by validator)
type: must be "ocr_word_repair"
"""


class LlmRepairProvider(Protocol):
    def propose_repairs(self, prompt: str) -> list[dict[str, Any]] | None: ...


class GeminiOcrRepairProvider:
    def __init__(self, client: GeminiClient | None = None) -> None:
        self.client = client or GeminiClient()

    def available(self) -> bool:
        return self.client.available()

    def propose_repairs(self, prompt: str) -> list[dict[str, Any]] | None:
        payload = self.client.generate_ocr_repair_json(prompt)
        if not payload or not isinstance(payload, dict):
            return None
        repairs = payload.get("repairs")
        if repairs is None:
            return []
        if not isinstance(repairs, list):
            return None
        return repairs


def is_eligible_for_llm_repair(chunk: dict[str, Any]) -> bool:
    """Select chunks that still have likely OCR damage after deterministic repair."""
    prov = chunk.get("provenance") or {}
    if prov.get("llm_ocr_repaired"):
        return False
    text = chunk.get("text") or ""
    normalized = chunk.get("normalized_text") or text
    frag = assess_fragmentation(normalized)
    if frag.has_confirmed or frag.has_known_unresolved:
        return True
    if SUSPICIOUS_MERGED.search(normalized):
        return True
    issues = chunk.get("validation_issues") or []
    if any("ocr" in issue.lower() for issue in issues):
        return True
    if chunk.get("review_status") == "review_required" and frag.has_possible:
        return True
    return False


def build_llm_repair_prompt(chunk: dict[str, Any]) -> str:
    raw = chunk.get("raw_text") or ""
    text = chunk.get("text") or ""
    normalized = chunk.get("normalized_text") or ""
    _, _, protected = protect_technical_tokens(text)
    protected_list = ", ".join(protected[:20]) if protected else "(none detected)"

    return f"""
{OCR_REPAIR_SYSTEM}

CHUNK CONTEXT:
chunk_id: {chunk.get("chunk_id")}
section: {(chunk.get("section") or {}).get("title", "")}

PROTECTED TECHNICAL TOKENS (must NOT be altered):
{protected_list}

RAW TEXT (evidence — do not rewrite):
{raw[:2000]}

CURRENT REPAIRED TEXT (apply repairs to this text — match substrings exactly):
{text[:3000]}

NORMALIZED TEXT (reference):
{normalized[:2000]}

Return repairs for broken Persian OCR fragments only. Match "original" exactly as it appears in CURRENT REPAIRED TEXT.
""".strip()


def _normalize_repair_dict(repair: dict[str, Any]) -> dict[str, Any]:
    return {
        "original": (repair.get("original") or "").strip(),
        "replacement": (repair.get("replacement") or "").strip(),
        "type": repair.get("type") or "ocr_word_repair",
        "confidence": float(repair.get("confidence") or 0.0),
        "reason": repair.get("reason") or "",
    }


def classify_repairs(
    text: str,
    repairs: list[dict[str, Any]],
    *,
    protected_tokens: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for raw_repair in repairs:
        repair = _normalize_repair_dict(raw_repair)
        validation = validate_llm_repair(text, repair, protected_tokens=protected_tokens)
        enriched = {
            **repair,
            "repair_confidence": validation.repair_confidence,
            "validation_status": validation.validation_status.value,
            "validation_issues": validation.issues,
            "unchanged_context_ratio": validation.unchanged_context_ratio,
            "word_count_delta": validation.word_count_delta,
        }
        if validation.status == ValidationStatus.ACCEPT:
            accepted.append(enriched)
        elif validation.status == ValidationStatus.REVIEW:
            review.append(enriched)
        else:
            rejected.append(enriched)
    return accepted, review, rejected


class LlmOcrRepairEngine:
    """Batch LLM OCR repair with cache, validation, and provenance."""

    def __init__(
        self,
        *,
        cache: LlmOcrRepairCache | None = None,
        provider: LlmRepairProvider | None = None,
        model_name: str | None = None,
    ) -> None:
        self.cache = cache
        self.provider = provider or GeminiOcrRepairProvider()
        self.model_name = model_name

    def process_chunk(
        self,
        chunk: dict[str, Any],
        *,
        use_llm: bool = True,
        apply_accepted: bool = True,
    ) -> dict[str, Any]:
        chunk_id = chunk.get("chunk_id") or ""
        if not is_eligible_for_llm_repair(chunk):
            return chunk

        cached = self.cache.get_chunk_result(chunk_id) if self.cache else None
        if cached and cached.get("llm_ocr_repair"):
            return self._merge_chunk_result(chunk, cached)

        text = chunk.get("text") or ""
        _, _, protected_tokens = protect_technical_tokens(text)
        proposed: list[dict[str, Any]] = []

        if use_llm and self.provider.available():
            prompt = build_llm_repair_prompt(chunk)
            llm_repairs = self.provider.propose_repairs(prompt)
            if llm_repairs is not None:
                proposed = [_normalize_repair_dict(r) for r in llm_repairs]
                for repair in proposed:
                    if self.cache and repair.get("original"):
                        self.cache.set_pattern_repairs(repair["original"], [repair])

        # Also check pattern cache for known originals in text
        if self.cache:
            for match in set(SUSPICIOUS_MERGED.findall(text)):
                cached_repairs = self.cache.get_pattern_repairs(match)
                if cached_repairs:
                    for cr in cached_repairs:
                        if cr.get("original") and cr["original"] in text:
                            proposed.append(_normalize_repair_dict(cr))

        # Deduplicate by original span
        seen: set[str] = set()
        unique_proposed: list[dict[str, Any]] = []
        for repair in proposed:
            key = repair.get("original") or ""
            if key and key not in seen:
                seen.add(key)
                unique_proposed.append(repair)

        accepted, review, rejected = classify_repairs(
            text, unique_proposed, protected_tokens=protected_tokens
        )

        new_text = text
        applied: list[dict[str, Any]] = []
        if apply_accepted and accepted:
            new_text, applied, skipped_accept = apply_accepted_repairs(
                text,
                accepted,
                protected_tokens=protected_tokens,
            )
            # Re-classify any that failed at apply time
            for item in skipped_accept:
                if item not in rejected:
                    rejected.append(item)

        delta = compute_text_delta(text, new_text)
        normalized = normalize_repaired_text(new_text) if new_text != text else chunk.get("normalized_text") or ""

        llm_meta = {
            "status": "applied" if applied else ("proposed_only" if review else "no_repairs"),
            "repairs_proposed": unique_proposed,
            "repairs_accepted": applied,
            "repairs_review": review,
            "repairs_rejected": rejected,
            "text_delta": delta,
        }

        updated = dict(chunk)
        if applied:
            updated["text"] = new_text
            updated["normalized_text"] = normalized
        updated["llm_ocr_repair"] = llm_meta

        prov = dict(updated.get("provenance") or {})
        prov["llm_rewritten"] = False
        prov["llm_ocr_repaired"] = bool(applied)
        if applied:
            prov["llm_repair_model"] = self.model_name or getattr(
                getattr(self.provider, "client", None), "model_name", "unknown"
            )
            prov["llm_repairs"] = applied
        updated["provenance"] = prov

        # Update review status
        frag = assess_fragmentation(updated.get("normalized_text") or "")
        if applied and not frag.has_confirmed and not review:
            updated["review_status"] = "pending"
            updated["validation_issues"] = [
                i
                for i in (updated.get("validation_issues") or [])
                if "ocr" not in i.lower()
            ]
        elif review or rejected:
            updated["review_status"] = "review_required"
            issues = list(updated.get("validation_issues") or [])
            if review:
                issues.append("llm_ocr_repairs_pending_review")
            updated["validation_issues"] = issues

        if self.cache:
            self.cache.set_chunk_result(chunk_id, updated)
        return updated

    def _merge_chunk_result(self, chunk: dict[str, Any], cached: dict[str, Any]) -> dict[str, Any]:
        merged = dict(chunk)
        for key in ("text", "normalized_text", "llm_ocr_repair", "provenance", "review_status", "validation_issues"):
            if key in cached:
                merged[key] = cached[key]
        return merged

    def process_corpus(
        self,
        chunks: list[dict[str, Any]],
        *,
        use_llm: bool = True,
        limit: int | None = None,
        chunk_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        stats = {
            "total": len(chunks),
            "eligible": 0,
            "processed": 0,
            "applied": 0,
            "review": 0,
            "rejected": 0,
            "skipped": 0,
        }
        results: list[dict[str, Any]] = []
        count = 0

        for chunk in chunks:
            eligible = is_eligible_for_llm_repair(chunk)
            if chunk_filter and not chunk_filter(chunk):
                results.append(chunk)
                continue
            if not eligible:
                stats["skipped"] += 1
                results.append(chunk)
                continue
            stats["eligible"] += 1
            if limit is not None and count >= limit:
                results.append(chunk)
                continue

            updated = self.process_chunk(chunk, use_llm=use_llm)
            stats["processed"] += 1
            count += 1
            llm_meta = updated.get("llm_ocr_repair") or {}
            if llm_meta.get("repairs_accepted"):
                stats["applied"] += 1
            if llm_meta.get("repairs_review"):
                stats["review"] += 1
            if llm_meta.get("repairs_rejected"):
                stats["rejected"] += len(llm_meta["repairs_rejected"])
            results.append(updated)

        if self.cache:
            self.cache.save()
        return results, stats
