"""Deterministic Persian OCR repair — separate from character normalization.

Pipeline (evidence-preserving, no LLM):
    reconstruct_block_text()
        ↓
    repair_ocr_fragments()
        ↓ repair_split_characters()
        ↓ repair_split_words()
        ↓ repair_known_phrases()
        ↓
    normalize_repaired_text()   # ی/ک only
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from goldset_generator.persian_text_reconstructor import reconstruct_block_text
from normalization.persian_normalizer import normalize_persian_text

PERSIAN_CHAR = re.compile(r"[\u0600-\u06FF]")
SINGLE_PERSIAN = re.compile(r"^[\u0600-\u06FF]{1,2}$")

TECHNICAL_TOKEN_PATTERN = re.compile(
    r"(?:"
    r"\bOEL(?:s|-(?:TWA|STEL|C))?\b"
    r"|\b\d{2,7}-\d{2}-\d\b"
    r"|\b\d+(?:[./]\d+)?\s*(?:ppm|mg/m³|mg/m3|dBA|mg/L|f/ml|%)\b"
    r"|\b\d+(?:\.\d+)?\b"
    r"|\bOccupational\s+[A-Za-z\s]+"
    r"|\b[A-Za-z][A-Za-z0-9\-–]*(?:\s+[A-Za-z][A-Za-z0-9\-–]*)*\b"
    r")",
    re.I,
)

CONFIRMED_FRAGMENT = re.compile(
    r"(?:[\u0600-\u06FF]{1,2}\s+){3,}[\u0600-\u06FF]+"
)
POSSIBLE_FRAGMENT = re.compile(r"[\u0600-\u06FF]\s+[\u0600-\u06FF]{1,2}\s+[\u0600-\u06FF]")
KNOWN_UNRESOLVED = re.compile(
    r"تع\s+ن|می\s+ن\s+توان|ش\s+یمی|به\s+طور\s*کل\s*ی|زان\s+ی|بنابرا\s+ین|هریکاز|درپانی"
)


class RepairConfidence(str, Enum):
    HIGH = "HIGH_CONFIDENCE"
    MEDIUM = "MEDIUM_CONFIDENCE"
    LOW = "LOW_CONFIDENCE"


@dataclass
class RepairRecord:
    original: str
    replacement: str
    rule: str
    confidence: RepairConfidence
    reason: str


@dataclass
class OcrRepairResult:
    raw_text: str
    repaired_text: str
    repairs: list[RepairRecord] = field(default_factory=list)
    protected_tokens: list[str] = field(default_factory=list)
    fragmentation_before: int = 0
    fragmentation_after: int = 0
    high_confidence_count: int = 0
    medium_confidence_count: int = 0
    low_confidence_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fragmentation_before": self.fragmentation_before,
            "fragmentation_after": self.fragmentation_after,
            "high_confidence_repairs": self.high_confidence_count,
            "medium_confidence_repairs": self.medium_confidence_count,
            "low_confidence_repairs": self.low_confidence_count,
            "protected_tokens": self.protected_tokens,
            "repairs": [
                {
                    "original": r.original,
                    "replacement": r.replacement,
                    "rule": r.rule,
                    "confidence": r.confidence.value,
                    "reason": r.reason,
                }
                for r in self.repairs
            ],
        }


# (pattern, replacement, rule_name, confidence, reason) — order matters
KNOWN_PHRASE_RULES: tuple[tuple[str, str, str, RepairConfidence, str], ...] = (
    (r"تع\s+ن\s+یی\s*شده", "تعیین شده", "char_split_tayin_shode", RepairConfidence.HIGH, "OCR char split"),
    (r"تع\s+ن\s+یی", "تعیین", "char_split_tayin", RepairConfidence.HIGH, "OCR char split"),
    (r"ت\s*ع\s*ی+\s*ن", "تعیین", "char_split_tayin_alt", RepairConfidence.HIGH, "OCR char split"),
    (r"تعنی(?:ی)?\s*شده", "تعیین شده", "char_split_tayin_compact", RepairConfidence.HIGH, "post-collapse"),
    (r"ب\s+یان", "بیان", "char_split_bayan", RepairConfidence.HIGH, "OCR char split"),
    (r"ز\s*ان\s*ی\s*آور", "زیان\u200cآور", "char_split_zianavar", RepairConfidence.HIGH, "OCR char split"),
    (r"زانی\s+آور", "زیان\u200cآور", "post_collapse_zianavar", RepairConfidence.HIGH, "post-collapse"),
    (r"زانیآور", "زیان\u200cآور", "post_collapse_zianavar_compact", RepairConfidence.HIGH, "post-collapse"),
    (r"زیان\s+آور", "زیان\u200cآور", "word_split_zian_avor", RepairConfidence.HIGH, "intra-word space"),
    (r"می\s+ن\s+توان\s*د", "می\u200cتواند", "char_split_mitavanad", RepairConfidence.HIGH, "OCR char split"),
    (r"می\s+توان\s*د", "می\u200cتواند", "char_split_mitavanad2", RepairConfidence.HIGH, "OCR char split"),
    (r"مین\s+توان\s*د", "می\u200cتواند", "char_split_mintavanad", RepairConfidence.HIGH, "post-collapse"),
    (r"مینتواند", "می\u200cتواند", "char_split_mintavanad_compact", RepairConfidence.HIGH, "post-collapse"),
    (r"ش\s+یمی\s*ایی", "شیمیایی", "char_split_shimiai", RepairConfidence.HIGH, "OCR char split"),
    (r"ش\s+یمیایی", "شیمیایی", "char_split_shimiai2", RepairConfidence.HIGH, "OCR char split"),
    (r"شیمی\s+ایی", "شیمیایی", "word_split_shimiai", RepairConfidence.HIGH, "intra-word space"),
    (r"ش\s+یی\s*ای", "شی", "char_split_shi", RepairConfidence.HIGH, "OCR char split"),
    (r"م\s+واجهه", "مواجهه", "word_split_mojavehe", RepairConfidence.HIGH, "intra-word space"),
    (r"مواجه\s+ه", "مواجهه", "word_split_mojavehe2", RepairConfidence.HIGH, "intra-word space"),
    (r"شغل\s+ی\b", "شغلی", "word_split_oghli", RepairConfidence.HIGH, "intra-word space"),
    (r"به\s+طور\s*کل\s*ی", "به طور کلی", "char_split_be_tour_koli", RepairConfidence.HIGH, "OCR char split"),
    (r"به\s+طورکل\s*ی", "به طور کلی", "word_split_be_tour_koli", RepairConfidence.HIGH, "intra-word space"),
    (r"طوالن\s*مدت\s*ی", "طولانی\u200cمدت", "word_split_toulani_modat", RepairConfidence.HIGH, "intra-word space"),
    (r"والن\s*مدت\s*ی", "طولانی\u200cمدت", "word_split_toulani_modat2", RepairConfidence.HIGH, "intra-word space"),
    (r"ز\s*ست\s*ی\s*یطی\s*مح", "زیست\u200cمحیطی", "char_split_zist_mohiti", RepairConfidence.HIGH, "OCR char split"),
    (r"ی\s*از\s*ستی\s*یطی\s*مح", "زیست\u200cمحیطی", "char_split_zist_mohiti2", RepairConfidence.HIGH, "OCR char split"),
    (r"زیست\s*ی\s*طی\s*مح", "زیست\u200cمحیطی", "word_split_zist_mohiti", RepairConfidence.HIGH, "intra-word space"),
    (r"بنابرا\s+ین", "بنابراین", "word_split_bonabarin", RepairConfidence.HIGH, "intra-word space"),
    (r"تدو\s+ین(?:\s*ین\s*ا)?", "تدوین", "word_split_tadvin", RepairConfidence.HIGH, "intra-word space"),
    (r"قطع\s+ی\s*ین\s*ب", "قطعی بین", "word_split_ghati_beyn", RepairConfidence.HIGH, "intra-word space"),
    (r"حرفه\s+ای\s+مورد", "حرفه\u200cای مورد", "word_split_herfei", RepairConfidence.HIGH, "intra-word space"),
    (r"احت\s+یاط", "احتیاط", "char_split_ehtiat", RepairConfidence.HIGH, "OCR char split"),
    (r"ارا\s*ئه", "ارائه", "char_split_araee", RepairConfidence.HIGH, "OCR char split"),
    (r"ن\s+ست\s*ی(?:\s*و)?", "نیست", "char_split_nist", RepairConfidence.HIGH, "OCR char split"),
    (r"مواد\s+شیمی\s+ایی\s+نستیو", "مواد شیمیایی نیست و", "word_split_shimiai_nist", RepairConfidence.HIGH, "known OCR corruption"),
    (r"OEL\s*-\s*TWA", "OEL-TWA", "protect_oel", RepairConfidence.HIGH, "normalize hyphen"),
    (r"OEL\s*-\s*STEL", "OEL-STEL", "protect_oel", RepairConfidence.HIGH, "normalize hyphen"),
    (r"OEL\s*-\s*C", "OEL-C", "protect_oel", RepairConfidence.HIGH, "normalize hyphen"),
    (r"هریکازآنها", "هر یک از آنها", "missing_space_har_yek", RepairConfidence.HIGH, "missing word boundary"),
    (r"هریکاز", "هر یک از", "missing_space_har_yek_az", RepairConfidence.HIGH, "missing word boundary"),
    (r"کهازآن", "که از آن", "missing_space_ke_az_an", RepairConfidence.HIGH, "missing word boundary"),
    (r"کهاز", "که از", "missing_space_ke_az", RepairConfidence.HIGH, "missing word boundary"),
    (r"برای\s*یهر", "برای هر", "missing_space_baraye_har", RepairConfidence.HIGH, "missing word boundary"),
    (r"ازحد\b", "از حد", "missing_space_az_had", RepairConfidence.HIGH, "missing word boundary"),
    (r"ازآن\b", "از آن", "missing_space_az_an", RepairConfidence.HIGH, "missing word boundary"),
    (r"باشن\s+د", "باشند", "char_split_beashand", RepairConfidence.HIGH, "OCR char split"),
    (r"چد\s+ار", "چند", "char_split_chand", RepairConfidence.HIGH, "OCR char split"),
    (r"درپانیتر(?:نیی)?(?:\.سطح)?", "در پایین\u200cترین سطح", "missing_space_pain_tar", RepairConfidence.HIGH, "missing word boundary"),
    (r"درحد(?=OEL|\s*OEL)", "در حد ", "missing_space_dar_had", RepairConfidence.HIGH, "missing word boundary"),
    (r"درحد\b", "در حد", "missing_space_dar_had2", RepairConfidence.HIGH, "missing word boundary"),
    (r"باهدف", "با هدف", "missing_space_ba_hadaf", RepairConfidence.HIGH, "missing word boundary"),
    (r"کهدر", "که در", "missing_space_ke_dar", RepairConfidence.HIGH, "missing word boundary"),
    (r"رادر", "را در", "missing_space_ra_dar", RepairConfidence.HIGH, "missing word boundary"),
    (r"بادر", "با در", "missing_space_ba_dar", RepairConfidence.HIGH, "missing word boundary"),
    (r"توانددر", "تواند در", "missing_space_tavanad_dar", RepairConfidence.HIGH, "missing word boundary"),
    (r"می\u200cتوانددر", "می\u200cتواند در", "missing_space_mitavanad_dar", RepairConfidence.HIGH, "missing word boundary"),
    (r"عالوه", "علاوه", "substitution_alave", RepairConfidence.HIGH, "known OCR substitution"),
    (r"قرارگرفته", "قرار گرفته", "missing_space_gharar_gerefte", RepairConfidence.MEDIUM, "possible missing space"),
    (r"مورداستفاده", "مورد استفاده", "missing_space_mored_estefade", RepairConfidence.MEDIUM, "possible missing space"),
)

# Words that must never be joined — regression guard
PROTECTED_PHRASES = (
    "عوامل دیگری نیز",
    "27 ماده شیمیایی",
    "Occupational Exposure Limits",
)


def protect_technical_tokens(text: str) -> tuple[str, dict[str, str], list[str]]:
    placeholders: dict[str, str] = {}
    protected: list[str] = []

    def repl(match: re.Match[str]) -> str:
        key = f"__TECH{len(placeholders)}__"
        placeholders[key] = match.group(0)
        protected.append(match.group(0))
        return key

    return TECHNICAL_TOKEN_PATTERN.sub(repl, text), placeholders, protected


def restore_technical_tokens(text: str, placeholders: dict[str, str]) -> str:
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def count_confirmed_fragmentation(text: str) -> int:
    count = 0
    for match in CONFIRMED_FRAGMENT.finditer(text):
        fragment = match.group(0)
        compact = fragment.replace(" ", "")
        if 4 <= len(compact) <= 24:
            count += 1
    return count


def count_possible_fragmentation(text: str) -> int:
    return len(POSSIBLE_FRAGMENT.findall(text))


def _apply_rule(
    text: str,
    pattern: str,
    replacement: str,
    rule: str,
    confidence: RepairConfidence,
    reason: str,
    repairs: list[RepairRecord],
    *,
    apply_high_only: bool = False,
) -> str:
    if apply_high_only and confidence != RepairConfidence.HIGH:
        return text

    def replacer(match: re.Match[str]) -> str:
        original = match.group(0)
        if original == replacement:
            return original
        for phrase in PROTECTED_PHRASES:
            if phrase in original or original in phrase:
                return original
        repairs.append(
            RepairRecord(
                original=original,
                replacement=replacement,
                rule=rule,
                confidence=confidence,
                reason=reason,
            )
        )
        return replacement

    return re.sub(pattern, replacer, text)


def repair_split_characters(text: str, repairs: list[RepairRecord]) -> str:
    """Join consecutive 1–2 char Persian OCR fragments only."""
    protected, placeholders, _ = protect_technical_tokens(text)
    tokens = protected.split()
    if not tokens:
        return restore_technical_tokens(protected, placeholders)

    merged: list[str] = []
    fragment_buf: list[str] = []

    def flush_fragments() -> None:
        nonlocal fragment_buf
        if len(fragment_buf) >= 2:
            original = " ".join(fragment_buf)
            joined = "".join(fragment_buf)
            merged.append(joined)
            repairs.append(
                RepairRecord(
                    original=original,
                    replacement=joined,
                    rule="repair_split_characters",
                    confidence=RepairConfidence.HIGH,
                    reason="consecutive short OCR fragments",
                )
            )
        elif fragment_buf:
            merged.extend(fragment_buf)
        fragment_buf = []

    for token in tokens:
        persian_only = re.sub(r"[^\u0600-\u06FF]", "", token)
        if persian_only and len(persian_only) <= 2:
            fragment_buf.append(token)
            continue
        flush_fragments()
        merged.append(token)

    flush_fragments()
    return restore_technical_tokens(" ".join(merged), placeholders)


def repair_split_words(text: str, repairs: list[RepairRecord]) -> str:
    """Apply MEDIUM-confidence missing-space repairs only (tracked, not auto-validated)."""
    protected, placeholders, _ = protect_technical_tokens(text)
    for pattern, replacement, rule, confidence, reason in KNOWN_PHRASE_RULES:
        if confidence != RepairConfidence.MEDIUM:
            continue
        protected = _apply_rule(protected, pattern, replacement, rule, confidence, reason, repairs)
    return restore_technical_tokens(protected, placeholders)


def repair_known_phrases(text: str, repairs: list[RepairRecord]) -> str:
    """Apply HIGH-confidence known OCR patterns."""
    protected, placeholders, _ = protect_technical_tokens(text)
    for pattern, replacement, rule, confidence, reason in KNOWN_PHRASE_RULES:
        if confidence != RepairConfidence.HIGH:
            continue
        protected = _apply_rule(protected, pattern, replacement, rule, confidence, reason, repairs)
    return restore_technical_tokens(protected, placeholders)


def _cleanup_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([،؛:.!?])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    return text.strip()


def repair_ocr_fragments(raw: str) -> OcrRepairResult:
    """Full staged OCR repair with audit trail."""
    reconstructed = reconstruct_block_text(raw or "")
    if not reconstructed:
        return OcrRepairResult(raw_text=raw or "", repaired_text="")

    repairs: list[RepairRecord] = []
    _, _, protected_tokens = protect_technical_tokens(reconstructed)
    frag_before = count_confirmed_fragmentation(reconstructed)

    text = reconstructed
    text = repair_known_phrases(text, repairs)
    text = repair_split_characters(text, repairs)
    text = repair_known_phrases(text, repairs)
    text = repair_split_words(text, repairs)
    text = _cleanup_whitespace(text)

    frag_after = count_confirmed_fragmentation(text)
    high = sum(1 for r in repairs if r.confidence == RepairConfidence.HIGH)
    medium = sum(1 for r in repairs if r.confidence == RepairConfidence.MEDIUM)
    low = sum(1 for r in repairs if r.confidence == RepairConfidence.LOW)

    return OcrRepairResult(
        raw_text=raw or "",
        repaired_text=text,
        repairs=repairs,
        protected_tokens=protected_tokens,
        fragmentation_before=frag_before,
        fragmentation_after=frag_after,
        high_confidence_count=high,
        medium_confidence_count=medium,
        low_confidence_count=low,
    )


def repair_ocr_text(raw: str) -> str:
    """Backward-compatible: return repaired text only."""
    return repair_ocr_fragments(raw).repaired_text


def normalize_repaired_text(repaired: str) -> str:
    """Character-level normalization after OCR repair."""
    return normalize_persian_text(repaired).normalized.strip()


def repair_and_normalize(raw: str) -> tuple[str, str, OcrRepairResult]:
    result = repair_ocr_fragments(raw)
    return result.repaired_text, normalize_repaired_text(result.repaired_text), result


def build_raw_text(evidence_texts: list[str]) -> str:
    return "\n\n".join(t for t in evidence_texts if (t or "").strip()).strip()


def build_repaired_text(evidence_texts: list[str]) -> str:
    return "\n\n".join(repair_ocr_text(t) for t in evidence_texts if (t or "").strip()).strip()


def build_repair_audit(evidence_texts: list[str]) -> OcrRepairResult:
    combined_raw = build_raw_text(evidence_texts)
    combined_result = repair_ocr_fragments(combined_raw)
    if len(evidence_texts) <= 1:
        return combined_result
    all_repairs: list[RepairRecord] = list(combined_result.repairs)
    for text in evidence_texts:
        if (text or "").strip():
            all_repairs.extend(repair_ocr_fragments(text).repairs)
    combined_result.repairs = all_repairs
    combined_result.high_confidence_count = sum(
        1 for r in all_repairs if r.confidence == RepairConfidence.HIGH
    )
    combined_result.medium_confidence_count = sum(
        1 for r in all_repairs if r.confidence == RepairConfidence.MEDIUM
    )
    combined_result.low_confidence_count = sum(
        1 for r in all_repairs if r.confidence == RepairConfidence.LOW
    )
    return combined_result


@dataclass
class FragmentationAssessment:
    confirmed_count: int
    possible_count: int
    has_known_unresolved: bool

    @property
    def has_confirmed(self) -> bool:
        return self.confirmed_count > 0 or self.has_known_unresolved

    @property
    def has_possible(self) -> bool:
        return self.possible_count > 0


def assess_fragmentation(text: str) -> FragmentationAssessment:
    return FragmentationAssessment(
        confirmed_count=count_confirmed_fragmentation(text),
        possible_count=count_possible_fragmentation(text),
        has_known_unresolved=bool(KNOWN_UNRESOLVED.search(text)),
    )


def has_unresolved_ocr(text: str) -> bool:
    assessment = assess_fragmentation(text)
    return assessment.has_confirmed


def has_possible_ocr(text: str) -> bool:
    assessment = assess_fragmentation(text)
    return assessment.has_possible and not assessment.has_confirmed


# Legacy wrappers for backward compatibility
def collapse_persian_fragments(text: str) -> str:
    repairs: list[RepairRecord] = []
    return repair_split_characters(text, repairs)


def apply_phrase_fixes(text: str) -> str:
    repairs: list[RepairRecord] = []
    return repair_known_phrases(text, repairs)


protect_latin_tokens = protect_technical_tokens
restore_latin_tokens = restore_technical_tokens
