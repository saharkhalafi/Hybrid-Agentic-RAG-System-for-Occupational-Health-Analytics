"""Unit tests for chemical identity extraction and resolver priority."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agents.routing.chemical_resolver import ChemicalResolver
from persistence.chemical_identity import (
    extract_identity_names,
    is_generic_identity,
    normalize_cas,
)


def test_extracts_full_persian_and_latin_from_mixed_cell():
    extracted = extract_identity_names("استون سیانو هیدرین Acetone cyanohydrin [75-86-5]")
    assert extracted.cas == "75-86-5"
    assert extracted.english_name == "Acetone cyanohydrin"
    assert extracted.persian_name == "استون سیانو هیدرین"


def test_generic_tokens_are_not_identities():
    assert is_generic_identity("acid")
    assert is_generic_identity("oxide")
    assert is_generic_identity("اسید")
    assert is_generic_identity("دی")
    assert not is_generic_identity("Acetone")
    assert not is_generic_identity("استون")


def test_normalize_cas_collapses_spacing_and_brackets():
    assert normalize_cas("[67 - 64 - 1]") == "67-64-1"


def _chem(**kwargs):
    defaults = {
        "id": kwargs.get("id", "id-1"),
        "cas": None,
        "english_name": None,
        "persian_name": None,
        "aliases": {},
        "synonyms": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestResolverPriority:
    def test_exact_cas_wins(self):
        acetone = _chem(id="a", cas="67-64-1", english_name="Acetone", persian_name="استون")
        with patch("agents.routing.chemical_resolver.get_accepted_chemicals", return_value=[acetone]):
            res = ChemicalResolver(MagicMock()).resolve("67-64-1 TWA")
        assert res.method == "cas_exact"
        assert res.cas == "67-64-1"

    def test_exact_latin_name_is_unique(self):
        acetone = _chem(id="a", cas="67-64-1", english_name="Acetone", persian_name="استون")
        cyano = _chem(id="b", cas="75-86-5", english_name="Acetone cyanohydrin", persian_name="استون سیانو هیدرین")
        with patch("agents.routing.chemical_resolver.get_accepted_chemicals", return_value=[acetone, cyano]):
            res = ChemicalResolver(MagicMock()).resolve("TWA Acetone")
        assert res.method == "exact"
        assert res.cas == "67-64-1"
        assert res.ambiguous is False

    def test_exact_persian_name_is_not_missing_chemical(self):
        acetone = _chem(id="a", cas="67-64-1", english_name="Acetone", persian_name="استون")
        cyano = _chem(
            id="b",
            cas="75-86-5",
            english_name="Acetone cyanohydrin",
            persian_name="استون سیانو هیدرین",
            aliases={"fa": ["استون"]},
        )
        with patch("agents.routing.chemical_resolver.get_accepted_chemicals", return_value=[acetone, cyano]):
            res = ChemicalResolver(MagicMock()).resolve("استون CAS چیست؟")
        assert res.method == "persian_exact"
        assert res.cas == "67-64-1"
        assert res.ambiguous is False

    def test_generic_latin_token_is_not_an_identity(self):
        acid = _chem(id="a", cas="64-19-7", english_name="Acetic acid", persian_name="اسید استیک")
        with patch("agents.routing.chemical_resolver.get_accepted_chemicals", return_value=[acid]):
            res = ChemicalResolver(MagicMock()).resolve("TWA acid")
        assert res.chemical_id is None
        assert res.method in {"no_match", "no_tokens", "empty", "latin_explicit", "below_threshold"}
