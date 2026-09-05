"""Pre-pipeline Domain / Safety Gate.

Multi-signal gate that runs **before** Session, Intent, Agent, Retrieval, or SQL.
Not keyword-only: combines injection/abuse detection, off-domain rejection,
HSE vocabulary scoring, session-context awareness, and structural validation.

Decisions:
  PASS    — valid OHSE query, proceed to pipeline
  REJECT  — clearly unrelated to Occupational Health & Safety
  BLOCK   — unsafe / injection / malicious / abusive
  CLARIFY — ambiguous but potentially relevant (short follow-up without context)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agents.routing.normalizer import normalize_persian_query


class GateDecision(str, Enum):
    PASS = "pass"
    REJECT = "reject"
    BLOCK = "block"
    CLARIFY = "clarify"


@dataclass
class GateResult:
    decision: GateDecision
    confidence: float
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    message_fa: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision in (GateDecision.PASS, GateDecision.CLARIFY)


# ── Prompt-injection / jailbreak patterns ──────────────────────────────────
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I | re.UNICODE)
    for p in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
        r"disregard\s+(your\s+)?(instructions?|rules?|guidelines?)",
        r"forget\s+(everything|all|your)\s+(you\s+)?(know|instructions?|rules?)",
        r"you\s+are\s+now\s+(a|an)\s+\w+",
        r"act\s+as\s+(if\s+you\s+are\s+)?(a|an)\s+\w+\s+(ai|assistant|bot)",
        r"system\s*:\s*",
        r"<\s*/?\s*(system|prompt|instruction)\s*>",
        r"\[\s*INST\s*\]",
        r"###\s*(system|instruction|human|assistant)",
        r"do\s+not\s+follow\s+(your|the)\s+(rules?|guidelines?|instructions?)",
        r"override\s+(safety|security|guardrail)",
        r"reveal\s+(your\s+)?(system\s+)?prompt",
        r"print\s+(your\s+)?(system\s+)?prompt",
        r"show\s+(me\s+)?(your\s+)?(system\s+)?(prompt|instructions?)",
        r"DAN\s+mode",
        r"jailbreak",
        r"bypass\s+(filter|guardrail|safety|restriction)",
        r"SQL\s+injection",
        r"DROP\s+TABLE",
        r";\s*--",
        r"UNION\s+SELECT",
        r"<script[\s>]",
        r"javascript\s*:",
        r"on\w+\s*=",
        r"eval\s*\(",
        r"exec\s*\(",
        r"__import__",
        r"os\.system",
        r"subprocess\.",
        # Persian injection attempts
        r"دستور\s+سیستم",
        r"نادیده\s+بگیر",
        r"فراموش\s+کن",
        r"نقش\s+تو\s+را\s+عوض\s+کن",
    )
]

# ── Abuse / malicious patterns ─────────────────────────────────────────────
_ABUSE_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I | re.UNICODE)
    for p in (
        r"\b(hack|exploit|attack|malware|ransomware|phishing)\b",
        r"\b(kill|murder|bomb|weapon|terror)\b",
        r"\b(drug\s+deal|illegal\s+substance)\b",
        r"how\s+to\s+(make|build|create)\s+(a\s+)?(bomb|weapon|poison|drug)",
        r"چطور\s+(بسازم|درست\s+کنم)\s+(بمب|سلاح|ماده\s+مخدر)",
    )
]

# ── Clearly off-domain topics (high-confidence reject) ─────────────────────
_OFF_DOMAIN_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I | re.UNICODE)
    for p in (
        r"\b(weather|forecast|temperature\s+today)\b",
        r"\b(football|soccer|basketball|sport|match\s+score)\b",
        r"\b(movie|film|series|netflix|music|song|album)\b",
        r"\b(recipe|cooking|restaurant|food\s+delivery)\b",
        r"\b(stock\s+market|crypto|bitcoin|forex|trading)\b",
        r"\b(dating|relationship|marriage\s+advice)\b",
        r"\b(homework|math\s+problem|solve\s+equation|calculate\s+\d)\b",
        r"\b(translate\s+this|translation\s+of)\b",
        r"\b(joke|funny|meme|story|poem)\b",
        r"\b(pizza|burger|coffee\s+shop|restaurant)\b",
        r"\b(capital\s+of|population\s+of|president\s+of)\b",
        r"\b(news|politics|election|vote)\b",
        r"\b(lose\s+weight|diet\s+plan|gym\s+workout)\b",
        r"\b(write\s+me|compose|generate\s+a)\b",
        r"آب\s+و\s+هوا",
        r"فوتبال|والیبال|بسکتبال",
        r"فیلم|سریال|موسیقی|آهنگ",
        r"دستور\s+پخت|غذا|رستوران",
        r"بورس|ارز\s+دیجیتال|بیت\s*کوین",
        r"جوک|طنز|داستان\s+خنده",
        r"دلم(?:\s+\S+){0,4}\s*گرفته|حوصله\s*ندار|نمی\s*خوام|نمیخوام|بیخیال",
        r"don't\s*want|feeling\s*down|i\s*am\s*sad|not\s*interested",
    )
]

# ── HSE task intent — vocabulary alone is not enough ───────────────────────
_HSE_TASK_PATTERN = re.compile(
    r"(حد|TWA|STEL|Ceiling|سقف|CAS|MW|ppm|ppb|mg/m|مواجهه|BEI|"
    r"فرمول|formula_|محاسبه|ahv|ahw|تعریف|یعنی|چیست|چیه|"
    r"چنده|چقدر|چقدره|مقایسه|منبع|صفحه|جدول|"
    r"وزن\s*مولکول|molecular\s*weight|exposure|limit)",
    re.IGNORECASE,
)

# Structured lookup markers — sufficient to pass gate even with low vocabulary score.
_STRONG_HSE_TASK = re.compile(
    r"(وزن\s*مولکول|molecular\s*weight|\bMW\b|TWA|STEL|Ceiling|"
    r"\bCAS\b|formula_\d+|BEI|حد\s|مواجهه|فرمول|ahv|محاسبه)",
    re.IGNORECASE,
)

# ── Positive HSE domain vocabulary (from intent taxonomy + OHE6 corpus) ────
_HSE_POSITIVE_TERMS: frozenset[str] = frozenset({
    # OEL / exposure
    "twa", "stel", "ceiling", "oel", "oels", "ppm", "ppb", "mg/m3", "mg/m³",
    "exposure", "limit", "limits", "threshold", "concentration", "dose",
    "مواجهه", "حد", "مجاز", "سقف", "میانگین", "وزنی", "غلظت", "ppm",
    # Chemicals
    "cas", "chemical", "substance", "compound", "molecule", "molecular",
    "benzene", "benz", "toluene", "ammonia", "acetone", "formaldehyde",
    "ماده", "شیمیایی", "بنزن", "تولوئن", "آمونیاک", "استون",
    # Safety concepts
    "hazard", "risk", "safety", "health", "occupational", "workplace",
    "industrial", "hygiene", "toxicology", "ventilation", "ppe",
    "ایمنی", "بهداشت", "شغلی", "کار", "صنعتی", "خطر", "ریسک",
    "تهویه", "ماسک", "تجهیزات", "حفاظت",
    # Standards / regulation
    "ohe6", "ohe", "ohse", "hse", "osha", "acgih", "tlv", "pel",
    "regulation", "standard", "guideline", "recommendation", "prohibition",
    "استاندارد", "مقررات", "توصیه", "ممنوعیت",
    # Formula / calculation
    "formula", "calculate", "calculation", "ahv", "ahw", "vibration",
    "noise", "laeq", "a(8)", "exposure", "duration",
    "فرمول", "محاسبه", "ارتعاش", "صدا", "نویز",
    # Definitions / semantic
    "definition", "define", "meaning", "explain", "concept", "what is",
    "تعریف", "یعنی", "چیست", "چیه", "معنی", "توضیح", "مفهوم",
    # BEI / biological
    "bei", "biological", "biomarker", "زیستی", "بیولوژ",
    # Provenance
    "source", "page", "table", "provenance", "reference",
    "منبع", "صفحه", "جدول",
    # Clarification / follow-up markers
    "stel?", "twa?", "cas?", "more", "compare", "comparison",
    "مقایسه", "بیشتر", "چنده", "چقدر", "چقدره",
})

# ── Follow-up fragments that are valid in HSE context ────────────────────
_FOLLOWUP_FRAGMENTS = re.compile(
    r"^(STEL|TWA|Ceiling|سقف|CAS|MW|منبع|فرمول|ahv|"
    r"نداره\؟|چنده\؟|چقدره\؟|همون|اون|اش|ش\؟|پس |باز |دوباره |"
    r"TWA\؟|STEL\؟|CAS\؟|سقف\؟|حد\؟|بیشتره\؟)",
    re.IGNORECASE,
)

# Limits
MAX_QUERY_LENGTH = 2000
MIN_QUERY_LENGTH = 1


def _normalize_for_scoring(text: str) -> str:
    """Lowercase, strip diacritics, normalize Persian digits."""
    t = text.lower().strip()
    t = unicodedata.normalize("NFKC", t)
    fa_digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    t = t.translate(fa_digits)
    return t


def _tokenize(text: str) -> set[str]:
    """Extract meaningful tokens for domain scoring."""
    t = _normalize_for_scoring(text)
    # Split on whitespace and common punctuation
    tokens = set(re.split(r"[\s,;:.!?()\-–—/\\|]+", t))
    tokens.discard("")
    # Also check multi-word phrases in the original (lowercased)
    return tokens


def _hse_relevance_score(query: str) -> float:
    """Score 0.0–1.0 based on HSE vocabulary overlap."""
    tokens = _tokenize(query)
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in _HSE_POSITIVE_TERMS)
    # Also check if any token is a CAS pattern or formula_id
    if re.search(r"\b\d{2,7}-\d{2}-\d\b", query):
        hits += 2
    if re.search(r"\bformula_\d+_\d+\b", query, re.I):
        hits += 2
    # Latin chemical-like token (capitalized word ≥3 chars)
    if re.search(r"\b[A-Z][a-z]{2,}(?:\s+[a-z]+)?\b", query):
        hits += 1
    # Persian chemical names
    for fa in ("بنزن", "تولوئن", "آمونیاک", "استون", "فرمالدئید"):
        if fa in query:
            hits += 1
    return min(1.0, hits / max(len(tokens), 1))


def _has_hse_task_intent(query: str) -> bool:
    """True when the query expresses an OHSE lookup/definition/calculation task."""
    return bool(_HSE_TASK_PATTERN.search(query))


class DomainSafetyGate:
    """Production domain/safety gate — runs before the query pipeline."""

    def evaluate(
        self,
        query: str,
        *,
        session_context: dict[str, Any] | None = None,
    ) -> GateResult:
        reasons: list[str] = []
        signals: dict[str, Any] = {}

        # ── 1. Structural validation ──────────────────────────────────────
        if not query or not query.strip():
            return GateResult(
                decision=GateDecision.BLOCK,
                confidence=1.0,
                reasons=["empty_query"],
                message_fa="سؤال خالی است.",
            )

        q = normalize_persian_query(query.strip())
        if len(q) > MAX_QUERY_LENGTH:
            return GateResult(
                decision=GateDecision.BLOCK,
                confidence=1.0,
                reasons=["query_too_long"],
                message_fa=f"سؤال بیش از حد طولانی است (حداکثر {MAX_QUERY_LENGTH} کاراکتر).",
            )

        # Null bytes / control characters
        if "\x00" in q or re.search(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", q):
            return GateResult(
                decision=GateDecision.BLOCK,
                confidence=1.0,
                reasons=["control_characters"],
                message_fa="ورودی نامعتبر است.",
            )

        # ── 2. Prompt injection / jailbreak ───────────────────────────────
        for pat in _INJECTION_PATTERNS:
            if pat.search(q):
                return GateResult(
                    decision=GateDecision.BLOCK,
                    confidence=0.95,
                    reasons=["prompt_injection"],
                    signals={"pattern": pat.pattern[:60]},
                    message_fa="این درخواست به دلیل تلاش برای دستکاری سامانه رد شد.",
                )

        # ── 3. Abuse / malicious ──────────────────────────────────────────
        for pat in _ABUSE_PATTERNS:
            if pat.search(q):
                return GateResult(
                    decision=GateDecision.BLOCK,
                    confidence=0.9,
                    reasons=["abusive_content"],
                    signals={"pattern": pat.pattern[:60]},
                    message_fa="این درخواست به دلیل محتوای نامناسب رد شد.",
                )

        # ── 4. Session context lowers the bar for follow-ups ──────────────
        has_session = bool(session_context)
        session_has_hse = bool(
            session_context
            and (
                session_context.get("chemical_name")
                or session_context.get("cas")
                or session_context.get("formula_id")
                or session_context.get("previous_intent")
            )
        )
        signals["has_session"] = has_session
        signals["session_has_hse"] = session_has_hse

        # Follow-up fragments in active HSE session → PASS immediately
        if session_has_hse and _FOLLOWUP_FRAGMENTS.match(q):
            return GateResult(
                decision=GateDecision.PASS,
                confidence=0.95,
                reasons=["session_followup"],
                signals=signals,
            )

        # ── 5. Off-domain rejection (pattern match → always reject) ─────
        for pat in _OFF_DOMAIN_PATTERNS:
            if pat.search(q):
                return GateResult(
                    decision=GateDecision.REJECT,
                    confidence=0.85,
                    reasons=["off_domain"],
                    signals={"pattern": pat.pattern[:60], "hse_score": _hse_relevance_score(q)},
                    message_fa="این سؤال مربوط به بهداشت و ایمنی شغلی (OHSE) نیست.",
                )

        # Conversational / emotional queries are off-domain even with incidental HSE words.
        if not _has_hse_task_intent(q) and re.search(
            r"(دلم(?:\s+\S+){0,4}\s*گرفته|حوصله\s*ندار|نمی\s*خوام|نمیخوام|بیخیال|don't\s*want|feeling\s*down)",
            q,
            re.I,
        ):
            return GateResult(
                decision=GateDecision.REJECT,
                confidence=0.88,
                reasons=["conversational_off_topic"],
                signals={"hse_score": _hse_relevance_score(q)},
                message_fa="این سؤال مربوط به بهداشت و ایمنی شغلی (OHSE) نیست.",
            )

        # ── 6. HSE relevance scoring ──────────────────────────────────────
        hse_score = _hse_relevance_score(q)
        signals["hse_score"] = round(hse_score, 3)

        if hse_score >= 0.25 and _has_hse_task_intent(q):
            return GateResult(
                decision=GateDecision.PASS,
                confidence=min(0.95, 0.5 + hse_score),
                reasons=["hse_relevant"],
                signals=signals,
            )

        if _STRONG_HSE_TASK.search(q) and _has_hse_task_intent(q):
            return GateResult(
                decision=GateDecision.PASS,
                confidence=0.78,
                reasons=["hse_task_intent"],
                signals=signals,
            )

        if _has_hse_task_intent(q) and len(q) >= 20:
            return GateResult(
                decision=GateDecision.PASS,
                confidence=0.72,
                reasons=["hse_task_intent"],
                signals=signals,
            )

        # ── 7. Ambiguous — potentially relevant but no clear signal ───────
        # Short queries in active session → PASS only for real follow-ups / HSE tasks
        if session_has_hse and len(q) < 60:
            if _FOLLOWUP_FRAGMENTS.match(q) or _has_hse_task_intent(q):
                return GateResult(
                    decision=GateDecision.PASS,
                    confidence=0.7,
                    reasons=["session_context_ambiguous"],
                    signals=signals,
                )
            return GateResult(
                decision=GateDecision.REJECT,
                confidence=0.8,
                reasons=["session_off_topic"],
                signals=signals,
                message_fa="این سؤال مربوط به بهداشت و ایمنی شغلی (OHSE) نیست.",
            )

        # Very short query without session and no HSE signal → CLARIFY (not reject)
        if len(q) < 20 and not has_session and hse_score < 0.1:
            return GateResult(
                decision=GateDecision.CLARIFY,
                confidence=0.6,
                reasons=["ambiguous_no_context"],
                signals=signals,
                message_fa="لطفاً سؤال خود را در حوزه بهداشت و ایمنی شغلی مشخص‌تر بیان کنید.",
            )

        # Missing keyword / unseen wording is not evidence of being off-domain.
        # Clearly unrelated queries are already rejected by off-domain patterns.
        return GateResult(
            decision=GateDecision.PASS,
            confidence=0.5,
            reasons=["default_pass"],
            signals=signals,
        )
