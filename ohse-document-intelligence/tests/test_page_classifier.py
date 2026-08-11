"""Tests for improved page classifier."""

from ingestion.page_classifier import classify_page
from ingestion.pdf_loader import PageContent


def _page(text: str) -> PageContent:
    return PageContent(
        page_number=1,
        text=text,
        has_digital_text=True,
        width=500,
        height=700,
    )


def test_domain_keywords_do_not_trigger_formula_heavy():
    result = classify_page(_page("CAS 67-56-1 TWA 200 ppm STEL 250 ppm OEL chemical limits table"))
    assert result.has_domain_keywords is True
    assert result.has_mathematical_formula is False
    assert result.page_type.value != "formula_heavy"


def test_equation_triggers_formula_heavy():
    result = classify_page(_page("a_w(OEL) = 2.45/sqrt(T) where T is exposure time in hours"))
    assert result.has_mathematical_formula is True
    assert result.page_type.value == "formula_heavy"
