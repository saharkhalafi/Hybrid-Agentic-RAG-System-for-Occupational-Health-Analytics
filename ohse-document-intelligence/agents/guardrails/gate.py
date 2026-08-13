"""Grounding and authority guardrails."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Minimum fused retrieval score before semantic answers are allowed.
SEMANTIC_MIN_SCORE = 0.42


@dataclass
class GuardrailDecision:
    allowed: bool
    action: str  # pass | clarify | refuse | no_data
    message_fa: str | None = None
    reasons: list[str] = field(default_factory=list)


class GuardrailGate:
    """Enforce numeric authority and citation requirements before synthesis."""

    def evaluate(
        self,
        *,
        intent: str,
        classification: dict[str, Any],
        agent_results: dict[str, Any],
    ) -> GuardrailDecision:
        if classification.get("requires_clarification"):
            msg = self._clarify_message(intent)
            if classification.get("ambiguous_chemical"):
                msg = "چند ماده شیمیایی با این نام یافت شد. لطفاً نام دقیق یا شماره CAS را مشخص کنید."
            return GuardrailDecision(
                allowed=False,
                action="clarify",
                message_fa=msg,
                reasons=["requires_clarification"],
            )

        if intent.startswith("GUARDRAIL."):
            return GuardrailDecision(
                allowed=False,
                action="refuse",
                message_fa=self._guardrail_message(intent),
                reasons=[intent],
            )

        nlevel = classification.get("numeric_safety_level", 0)
        if nlevel >= 2:
            structured = agent_results.get("structured") or agent_results.get("hybrid", {}).get("agent_results", {}).get("structured")
            if isinstance(structured, dict) and not structured.get("success"):
                return GuardrailDecision(
                    allowed=False,
                    action="no_data",
                    message_fa="اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد.",
                    reasons=["structured_lookup_failed"],
                )
            # forbid semantic-only numeric authority
            if nlevel >= 2 and not structured and agent_results.get("semantic") and not agent_results.get("structured"):
                return GuardrailDecision(
                    allowed=False,
                    action="refuse",
                    message_fa="مقادیر عددی حد مجاز باید از پایگاه داده ساختاریافته استخراج شوند.",
                    reasons=["semantic_numeric_forbidden"],
                )

        if intent.startswith("FORMULA.CALCULATION") and not agent_results.get("formula", {}).get("success"):
            err = (agent_results.get("formula") or {}).get("error")
            if err == "missing_inputs":
                return GuardrailDecision(allowed=False, action="clarify", message_fa="لطفاً مقادیر ورودی محاسبه را مشخص کنید.", reasons=[err])
            return GuardrailDecision(allowed=False, action="refuse", message_fa="محاسبه فرمول در حال حاضر پشتیبانی نمی‌شود.", reasons=[err or "unsupported"])

        if intent.startswith("SEMANTIC."):
            sem = agent_results.get("semantic") or {}
            if sem.get("success"):
                chunks = sem.get("chunks") or []
                top_score = max((float(c.get("score") or 0) for c in chunks), default=0.0)
                if top_score < SEMANTIC_MIN_SCORE:
                    return GuardrailDecision(
                        allowed=False,
                        action="no_data",
                        message_fa="اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد.",
                        reasons=["semantic_low_confidence", f"top_score={top_score:.3f}"],
                    )

        return GuardrailDecision(allowed=True, action="pass")

    @staticmethod
    def _clarify_message(intent: str) -> str:
        messages = {
            "CLARIFY.MISSING_CHEMICAL": "لطفاً نام ماده شیمیایی یا شماره CAS را مشخص کنید.",
            "CLARIFY.MISSING_LIMIT_TYPE": "کدام نوع حد مجاز را می‌خواهید؟ (TWA، STEL، Ceiling)",
            "CLARIFY.MISSING_EXPOSURE_VALUE": "لطفاً مقدار مواجهه (مثلاً ppm) را وارد کنید.",
            "CLARIFY.MISSING_DURATION": "لطفاً مدت مواجهه را مشخص کنید.",
            "CLARIFY.MISSING_AGENT": "لطفاً مشخص کنید منظورتان حد شیمیایی، صدا، ارتعاش یا مورد دیگر است.",
        }
        return messages.get(intent, "لطفاً اطلاعات بیشتری برای پاسخ دقیق ارائه دهید.")

    @staticmethod
    def _guardrail_message(intent: str) -> str:
        messages = {
            "GUARDRAIL.NO_DATA": "اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد.",
            "GUARDRAIL.PROFESSIONAL_JUDGMENT": "این سؤال نیاز به قضاوت حرفه‌ای دارد و خارج از حوزه این سامانه است.",
            "GUARDRAIL.UNSAFE_EXTRAPOLATION": "استقراء مواجهه فراتر از بازه‌های تعریف‌شده مجاز نیست.",
            "GUARDRAIL.INVALID_INPUT": "ورودی نامعتبر است.",
            "GUARDRAIL.CONFLICTING_DATA": "تعارض در منابع داده مشاهده شد؛ بررسی انسانی لازم است.",
        }
        return messages.get(intent, "امکان پاسخ‌دهی به این سؤال وجود ندارد.")
