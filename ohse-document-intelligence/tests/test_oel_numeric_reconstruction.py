"""Generic OEL STEL/TWA reconstruction from PDF word tokens."""

from __future__ import annotations

from ingestion.merged_row_splitter import (
    _numeric_from_fragments,
    reconstruct_limit_expression_from_pdf_words,
)


def _words(*tokens: str):
    x = 10.0
    words = []
    for token in tokens:
        width = max(8.0, 4.0 * len(token))
        words.append((x, 20.0, x + width, 30.0, token))
        x += width + 2.0
    return words


def test_decimal_fragment_patterns():
    assert _numeric_from_fragments(["/0", "01"]) == "0.01"
    assert _numeric_from_fragments(["/0", "02"]) == "0.02"
    assert _numeric_from_fragments(["/0", "05"]) == "0.05"
    assert _numeric_from_fragments(["/0", "08"]) == "0.08"
    assert _numeric_from_fragments(["1/0"]) == "0.1"
    assert _numeric_from_fragments(["2/0"]) == "0.2"
    assert _numeric_from_fragments(["5/0"]) == "0.5"
    assert _numeric_from_fragments(["10"]) == "10"
    assert _numeric_from_fragments(["20"]) == "20"
    assert _numeric_from_fragments(["0.02"]) == "0.02"
    assert _numeric_from_fragments(["0.08"]) == "0.08"
    assert _numeric_from_fragments(["1", "/", "0"]) == "0.1"
    assert _numeric_from_fragments(["/۰", "۰۸"]) == "0.08"


def test_leading_zero_mantissa_is_not_a_standalone_value_in_slash_pair():
    assert _numeric_from_fragments(["/0", "01"]) != "01"
    assert _numeric_from_fragments(["/0", "08"]) != "08"
    assert _numeric_from_fragments(["3"]) is None


def test_unit_patterns_with_fragmented_exponent():
    assert reconstruct_limit_expression_from_pdf_words(_words("20", "mg/m", "3")) == "20 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("10", "mg/m", "³")) == "10 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("/0", "08", "mg/m", "3(I)")) == "0.08 mg/m3(I)"
    assert reconstruct_limit_expression_from_pdf_words(_words("/0", "02", "mg/m", "3", "(I)")) == "0.02 mg/m3(I)"
    assert reconstruct_limit_expression_from_pdf_words(_words("/0", "01", "mg/m", "3")) == "0.01 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("1/0", "mg/m", "3")) == "0.1 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("2/0", "mg/m", "3")) == "0.2 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("5/0", "mg/m", "3")) == "0.5 mg/m3"
    assert reconstruct_limit_expression_from_pdf_words(_words("20", "mg/m", "3(l)")) == "20 mg/m3(l)"
    assert reconstruct_limit_expression_from_pdf_words(_words("35", "ppm")) == "35 ppm"


def test_persian_digits_with_fragmented_unit():
    words = _words("/۰", "۰۸", "mg/m", "3")
    assert reconstruct_limit_expression_from_pdf_words(words) == "0.08 mg/m3"


def test_mixed_latin_persian_slash_decimal():
    assert reconstruct_limit_expression_from_pdf_words(_words("/0", "۰۲", "mg/m", "³")) == "0.02 mg/m3"


def test_integer_limit_values():
    assert _numeric_from_fragments(["20"]) == "20"
    assert _numeric_from_fragments(["10"]) == "10"
    assert _numeric_from_fragments(["25"]) == "25"
    assert _numeric_from_fragments(["35"]) == "35"
    assert reconstruct_limit_expression_from_pdf_words(_words("25", "ppm")) == "25 ppm"
    assert reconstruct_limit_expression_from_pdf_words(_words("35", "ppm")) == "35 ppm"


def test_missing_stel_or_twa_is_unresolved():
    assert reconstruct_limit_expression_from_pdf_words([]) is None
    assert reconstruct_limit_expression_from_pdf_words(_words("-")) is None
    assert reconstruct_limit_expression_from_pdf_words(_words("mg/m", "3")) is None


def test_mw_slash_tokens_are_not_stel_twa_limits():
    for tokens in (
        ("431", "/", "10"),
        ("349", "/", "40"),
        ("252", "/", "07"),
        ("431/10",),
        ("349/40",),
        ("252/07",),
    ):
        reconstructed = reconstruct_limit_expression_from_pdf_words(_words(*tokens))
        if reconstructed:
            lowered = reconstructed.lower().replace(" ", "")
            assert "mg/m" not in lowered
            assert "ppm" not in lowered
            assert reconstructed.split()[0] not in {"431/10", "349/40", "252/07"}
        else:
            assert reconstructed is None


def test_latex_is_not_mapped_to_a_guessed_number():
    words = _words(r"r\cdot", "mg/m", "3")
    assert reconstruct_limit_expression_from_pdf_words(words) is None


def test_fragmented_ppm_unit_tokens():
    assert reconstruct_limit_expression_from_pdf_words(_words("6", "p", "pm")) == "6 ppm"
    assert reconstruct_limit_expression_from_pdf_words(_words("2", "pp", "m")) == "2 ppm"
