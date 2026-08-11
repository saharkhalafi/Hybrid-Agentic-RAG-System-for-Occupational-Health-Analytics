"""Tests for Persian text normalization."""

from normalization.persian_normalizer import normalize_persian_text


def test_persian_digit_conversion():
    result = normalize_persian_text("مقدار ۱۲۳ ppm")
    assert result.original == "مقدار ۱۲۳ ppm"
    assert "123" in result.normalized


def test_yeh_kaf_normalization():
    result = normalize_persian_text("ك ي")
    assert "ک" in result.normalized
    assert "ی" in result.normalized
