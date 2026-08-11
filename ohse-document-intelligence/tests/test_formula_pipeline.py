"""Tests for evidence-first formula extraction pipeline."""

from __future__ import annotations

from extraction.formula_candidate_detector import detect_formula_candidates
from extraction.formula_reconstructor import reconstruct_formula
from goldset_generator.formula_generator import FormulaGoldGenerator
from goldset_generator.formula_validator import FormulaValidator


class _UnavailableGemini:
    def available(self) -> bool:
        return False

PAGE_240_PARAGRAPHS = [
    {"text": "در یک\nنوبت\nکار یم ی\nتوان \n از رابطه2 \n:استفاده نمود", "bbox": {"x": 1, "y": 138, "width": 100, "height": 15}},
    {"text": "(رابطه2\n  )\n \n                          \n  \nahv=√", "bbox": {"x": 72, "y": 171, "width": 332, "height": 17}},
    {"text": "1", "bbox": {"x": 97, "y": 168, "width": 3, "height": 7}},
    {"text": "T ((ahw 12×t 1)+(ahw 22×t 2)+…+(ahw n2×t n))", "bbox": {"x": 51, "y": 171, "width": 214, "height": 17}},
    {"text": "گاهی توسط تجهیزات\nاندازه\nریگ ی ارتعاش انسانی در دسترس تجاری.نیز  قابل انجام است", "bbox": {"x": 105, "y": 398, "width": 318, "height": 15}},
    {"text": "(رابطه3\n  )\n \n \n \n \n \n \nA(8)= ahv√", "bbox": {"x": 80, "y": 430, "width": 343, "height": 17}},
    {"text": "Tv\nT0", "bbox": {"x": 128, "y": 427, "width": 7, "height": 19}},
    {"text": "مواجهه برابر با8 \n( ساعت28800 \n \n ثانیه) در نظر\nگرفته", "bbox": {"x": 69, "y": 503, "width": 354, "height": 17}},
]

PAGE_240_TEXT = "\n".join(p["text"] for p in PAGE_240_PARAGRAPHS)


def test_regex_produces_candidates_not_final_formulas():
    candidates = detect_formula_candidates(240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS)
    assert len(candidates) >= 2
    for candidate in candidates:
        assert candidate.candidate is True
        assert candidate.reason in {"equation_reference_anchor", "mathematical_tokens_detected"}
        assert candidate.document_equation_reference in {"2", "3"}


def test_page_240_equation_references_detected():
    candidates = detect_formula_candidates(240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS)
    refs = {c.document_equation_reference for c in candidates}
    assert "2" in refs
    assert "3" in refs


def test_incomplete_sqrt_fragment_is_not_validated_gold():
    candidates = detect_formula_candidates(240, "ahv=√", [{"text": "ahv=√", "bbox": None}])
    reconstruction = reconstruct_formula(candidates[0])
    assert reconstruction.status == "extraction_uncertain"


def test_equation_two_layout_reconstruction():
    candidates = detect_formula_candidates(240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS)
    eq2 = next(c for c in candidates if c.document_equation_reference == "2")
    reconstruction = reconstruct_formula(eq2)
    assert "sqrt" in reconstruction.expression
    assert "ahw_1^2" in reconstruction.expression or "ahw" in reconstruction.expression
    assert reconstruction.status == "validated"
    # Entire summation must stay inside sqrt alongside (1/T)
    assert "* ((" in reconstruction.expression or "* ((ahw" in reconstruction.expression
    assert "+ ... +" in reconstruction.expression or "+(" in reconstruction.expression
    assert reconstruction.expression.index("*") < reconstruction.expression.rindex(")")


def test_equation_three_fraction_reconstruction():
    candidates = detect_formula_candidates(240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS)
    eq3 = next(c for c in candidates if c.document_equation_reference == "3")
    reconstruction = reconstruct_formula(eq3)
    assert "A(8)" in reconstruction.expression
    assert "Tv" in reconstruction.expression


def test_28800_only_from_document_evidence():
    generator = FormulaGoldGenerator(gemini_client=_UnavailableGemini())
    approved, review, all_records = generator.generate_for_page(
        240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS
    )
    eq3 = next(r for r in all_records if r.get("document_equation_reference") == "3")
    refs = eq3.get("semantics", {}).get("reference_values") or []
    t0_refs = [r for r in refs if r.get("variable") in {"T0", "To"}]
    assert t0_refs
    assert t0_refs[0]["reference_value"] == "28800"
    assert t0_refs[0]["source"] == "document_evidence"


def test_validator_rejects_untraceable_numeric():
    validator = FormulaValidator()
    gold = {
        "reconstruction": {"expression": "A(8) = ahv * sqrt(Tv/T0)", "status": "validated"},
        "semantics": {
            "variables": {"A(8)": {"role": "result"}},
            "units": {"A(8)": "index"},
            "reference_values": [{"variable": "T0", "reference_value": "99999", "unit": "s", "source": "document_evidence"}],
        },
        "evidence": {"raw_text_fragments": ["A(8)= ahv√", "Tv/T0"]},
    }
    result = validator.validate(gold, PAGE_240_TEXT, reconstruction_status="validated")
    assert result["numeric_traceability"] is False


def test_uncertain_formulas_route_to_review_not_approved():
    generator = FormulaGoldGenerator(gemini_client=_UnavailableGemini())
    approved, review, all_records = generator.generate_for_page(
        240, PAGE_240_TEXT, PAGE_240_PARAGRAPHS
    )
    assert len(all_records) >= 2
    assert len(review) + len(approved) == len(all_records)
