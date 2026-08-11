"""Regression tests for conservative Persian OCR repair."""

from __future__ import annotations

from goldset_generator.persian_text_repair import (
    RepairConfidence,
    assess_fragmentation,
    repair_ocr_fragments,
    repair_ocr_text,
)


def test_char_fragmentation_zianavar():
    result = repair_ocr_fragments("عوامل ز\nان ی\nآور")
    assert "زیان" in result.repaired_text and "آور" in result.repaired_text
    assert result.high_confidence_count >= 1


def test_char_fragmentation_bayan():
    assert "بیان" in repair_ocr_text("ب\nیان")


def test_char_fragmentation_tayin():
    assert "تعیین" in repair_ocr_text("ت\nع یی\nن")


def test_word_fragmentation_bonabarin():
    assert repair_ocr_text("بنابرا ین") == "بنابراین"


def test_word_fragmentation_tadvin():
    assert "تدوین" in repair_ocr_text("تدو ین")


def test_missing_space_har_yek_az_anha():
    assert repair_ocr_text("هریکازآنها") == "هر یک از آنها"


def test_missing_space_ke_az_an():
    assert "که از آن" in repair_ocr_text("کهازآن")


def test_missing_space_baraye_har():
    assert repair_ocr_text("برای یهر") == "برای هر"


def test_mojavehe_shoghli():
    repaired = repair_ocr_text("م واجهه شغل ی")
    assert "مواجهه" in repaired
    assert "شغلی" in repaired


def test_toulani_modat():
    assert "طولانی" in repair_ocr_text("طوالن\nمدت ی")


def test_zist_mohiti():
    assert "زیست" in repair_ocr_text("ز\nست ی\nیطی مح")


def test_pain_tarini_sath():
    assert "پایین" in repair_ocr_text("درپانیترنیی.سطح")


def test_technical_token_oels():
    assert repair_ocr_text("OELs") == "OELs"
    assert "OELs" in repair_ocr_text("OELs برابر")


def test_technical_token_occupational():
    text = "Occupational Exposure Limits"
    assert repair_ocr_text(text) == text


def test_false_positive_prevention_digari():
    assert repair_ocr_text("عوامل دیگری نیز") == "عوامل دیگری نیز"


def test_false_positive_27_made():
    assert repair_ocr_text("27 ماده شیمیایی") == "27 ماده شیمیایی"


def test_repair_audit_trail():
    result = repair_ocr_fragments("تع\nن\nیی\nشده")
    assert result.repairs
    assert all(r.confidence == RepairConfidence.HIGH for r in result.repairs if r.rule.startswith("char"))
    audit = result.to_dict()
    assert "fragmentation_before" in audit
    assert "repairs" in audit


def test_fragmentation_assessment():
    bad = assess_fragmentation("تع ن یی شده")
    assert bad.has_confirmed or bad.has_possible
    good = assess_fragmentation("عوامل دیگری نیز در مواجهه")
    assert not good.has_known_unresolved


def test_tayin_shode():
    assert "تعیین شده" in repair_ocr_text("تع\nن\nیی\nشده")


def test_mitavanad():
    assert "می\u200cتواند" in repair_ocr_text("می\nن\nتوان\nد")


def test_shimiai():
    assert "شیمیایی" in repair_ocr_text("مواد ش\nیمیایی")
