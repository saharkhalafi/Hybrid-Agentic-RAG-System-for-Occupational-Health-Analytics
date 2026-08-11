"""Tests for LLM OCR repair validator and engine (no live LLM calls)."""

from __future__ import annotations

from goldset_generator.llm_ocr_repair import (
    LlmOcrRepairEngine,
    is_eligible_for_llm_repair,
)
from goldset_generator.llm_ocr_repair import classify_repairs
from goldset_generator.llm_ocr_repair_cache import LlmOcrRepairCache
from goldset_generator.llm_ocr_repair_validator import (
    ValidationStatus,
    apply_accepted_repairs,
    compute_text_delta,
    validate_llm_repair,
)


class MockProvider:
    def available(self) -> bool:
        return True

    def propose_repairs(self, prompt: str) -> list[dict]:
        if "زان یآور" in prompt:
            return [
                {
                    "original": "زان یآور",
                    "replacement": "زیان\u200cآور",
                    "type": "ocr_word_repair",
                    "confidence": 0.98,
                    "reason": "fragmented Persian word",
                }
            ]
        return []


def test_validator_accepts_valid_word_repair():
    text = "عوامل زان یآور شیمیایی"
    repair = {
        "original": "زان یآور",
        "replacement": "زیان\u200cآور",
        "type": "ocr_word_repair",
        "confidence": 0.98,
        "reason": "fragmented Persian word",
    }
    result = validate_llm_repair(text, repair)
    assert result.validation_status == ValidationStatus.ACCEPT


def test_validator_rejects_sentence_rewrite():
    text = "در این فصل حدود مجاز مواجهه شغلی عوامل زان یآور شیمیایی"
    repair = {
        "original": "در این فصل حدود مجاز مواجهه شغلی عوامل زان یآور شیمیایی",
        "replacement": "در این فصل حدود مجاز مواجهه شغلی با عوامل زیان\u200cآور شیمیایی بررسی می\u200cشود",
        "type": "ocr_word_repair",
        "confidence": 0.99,
        "reason": "rewrite",
    }
    result = validate_llm_repair(text, repair)
    assert result.validation_status == ValidationStatus.REJECT


def test_validator_rejects_numeric_change():
    text = "27 ماده شیمیایی"
    repair = {
        "original": "27",
        "replacement": "28",
        "type": "ocr_word_repair",
        "confidence": 0.99,
        "reason": "bad",
    }
    result = validate_llm_repair(text, repair)
    assert result.validation_status == ValidationStatus.REJECT


def test_validator_rejects_oels_change():
    text = "استفاده از OELs برای"
    repair = {
        "original": "OELs",
        "replacement": "O ELs",
        "type": "ocr_word_repair",
        "confidence": 0.99,
        "reason": "bad",
    }
    result = validate_llm_repair(text, repair, protected_tokens=["OELs"])
    assert result.validation_status == ValidationStatus.REJECT


def test_apply_repairs_preserves_context():
    text = "در این فصل عوامل زان یآور شیمیایی به همراه مطالب"
    repairs = [
        {
            "original": "زان یآور",
            "replacement": "زیان\u200cآور",
            "type": "ocr_word_repair",
            "confidence": 0.98,
            "reason": "fragmented Persian word",
        }
    ]
    new_text, applied, skipped = apply_accepted_repairs(text, repairs)
    assert applied
    assert "زیان" in new_text
    assert "زان یآور" not in new_text
    assert new_text.startswith("در این فصل عوامل")
    assert new_text.endswith("به همراه مطالب")
    delta = compute_text_delta(text, new_text)
    assert delta["changed"]


def test_false_positive_phrase_unchanged():
    text = "عوامل دیگری نیز"
    repair = {
        "original": "عوامل دیگری",
        "replacement": "عواملی دیگر",
        "type": "ocr_word_repair",
        "confidence": 0.95,
        "reason": "bad join",
    }
    result = validate_llm_repair(text, repair)
    assert result.validation_status == ValidationStatus.REJECT


def test_engine_with_mock_provider(tmp_path):
    chunk = {
        "chunk_id": "semantic_021_01",
        "raw_text": "عوامل ز\nان ی\nآور",
        "text": "در این فصل عوامل زان یآور شیمیایی",
        "normalized_text": "در این فصل عوامل زان یآور شیمیایی",
        "review_status": "review_required",
        "validation_issues": ["confirmed_ocr_fragmentation"],
        "provenance": {"llm_rewritten": False},
    }
    cache = LlmOcrRepairCache(tmp_path / "cache.json")
    engine = LlmOcrRepairEngine(cache=cache, provider=MockProvider())
    updated = engine.process_chunk(chunk)
    assert "زیان" in updated["text"]
    assert updated["provenance"]["llm_rewritten"] is False
    assert updated["provenance"]["llm_ocr_repaired"] is True
    assert updated["raw_text"] == chunk["raw_text"]
    assert updated["llm_ocr_repair"]["repairs_accepted"]


def test_eligible_detection():
    eligible = {
        "text": "متن با زان یآور",
        "normalized_text": "متن با زان یآور",
        "review_status": "review_required",
        "validation_issues": ["confirmed_ocr_fragmentation"],
        "provenance": {},
    }
    assert is_eligible_for_llm_repair(eligible)
    done = {**eligible, "provenance": {"llm_ocr_repaired": True}}
    assert not is_eligible_for_llm_repair(done)


def test_medium_confidence_goes_to_review():
    text = "قرارگرفته اند"
    repair = {
        "original": "قرارگرفته",
        "replacement": "قرار گرفته",
        "type": "ocr_word_repair",
        "confidence": 0.75,
        "reason": "missing space",
    }
    accepted, review, rejected = classify_repairs(text, [repair], protected_tokens=[])
    assert not accepted
    assert review
    assert not rejected or rejected[0]["validation_status"] == "REJECT"
