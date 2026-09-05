"""Focused tests for English token-set aliasing and structured provenance fields."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.structured.agent import StructuredAgent
from agents.structured.store import (
    canonical_row_text_field,
    english_name_tokens,
    token_sets_match,
)


class TestEnglishNameTokenSet:
    def test_order_independent_multiword_match(self):
        assert token_sets_match("Sulfuric acid", "acid Sulfuric")
        assert token_sets_match("ethylene oxide", "oxide Ethylene")
        assert token_sets_match("acetic acid", "acid Acetic")

    def test_tokens_ignore_punctuation_and_case(self):
        assert english_name_tokens("Sulfuric Acid") == english_name_tokens("acid, sulfuric")

    def test_different_chemicals_do_not_match(self):
        assert not token_sets_match("Sulfuric acid", "acid Acetic")
        assert not token_sets_match("ethylene oxide", "ethylene glycol")

    def test_single_token_is_not_a_token_set_match(self):
        assert not token_sets_match("Sulfuric", "acid Sulfuric")
        assert not token_sets_match("acid", "acid Sulfuric")

    def test_generic_only_phrase_is_rejected(self):
        assert not token_sets_match("acid oxide", "oxide acid")

    def test_cas_style_strings_are_not_token_reordered(self):
        assert not token_sets_match("7664-93-9", "acid Sulfuric")


class TestCanonicalRowTextField:
    def test_prefers_accepted_over_original(self):
        assert (
            canonical_row_text_field(
                {"symbols": {"value": "2A"}},
                {"symbols": {"value": "A3"}},
                key="symbols",
            )
            == "2A"
        )

    def test_reads_plain_string_and_skips_empty(self):
        assert canonical_row_text_field({"health_effect": ""}, {"health_effect": "پوست"}, key="health_effect") == "پوست"
        assert canonical_row_text_field({}, {}, key="symbols") is None


class TestChemicalAliasPriority:
    def test_cas_and_exact_name_win_over_token_set(self):
        store = MagicMock()
        from agents.structured.store import PostgresStructuredStore

        real = PostgresStructuredStore.__dict__["get_chemical_by_alias"]
        session = MagicMock()
        exact = MagicMock()
        exact.english_name = "Benzene"
        session.scalar.return_value = exact
        inst = PostgresStructuredStore(session)
        inst._chem_dict = PostgresStructuredStore._chem_dict
        inst._match_english_token_set = MagicMock(return_value=MagicMock())
        result = real(inst, "71-43-2")
        inst._match_english_token_set.assert_not_called()
        assert result["english_name"] == "Benzene"

    def test_token_set_used_when_exact_misses(self):
        from agents.structured.store import PostgresStructuredStore

        real = PostgresStructuredStore.__dict__["get_chemical_by_alias"]
        session = MagicMock()
        session.scalar.return_value = None
        token_hit = MagicMock()
        token_hit.id = "cid"
        token_hit.cas = "7664-93-9"
        token_hit.english_name = "acid Sulfuric"
        token_hit.persian_name = None
        token_hit.molecular_weight = None
        token_hit.aliases = None
        inst = PostgresStructuredStore(session)
        inst._match_registry_alias = MagicMock(return_value=None)
        inst._match_english_token_set = MagicMock(return_value=token_hit)
        result = real(inst, "Sulfuric acid")
        assert result["cas"] == "7664-93-9"
        assert result["english_name"] == "acid Sulfuric"
        session.scalars.assert_not_called()

    def test_ambiguous_token_set_does_not_use_partial_shortcut(self):
        from agents.structured.store import PostgresStructuredStore

        session = MagicMock()
        a = MagicMock(english_name="acid Sulfuric")
        b = MagicMock(english_name="acid Sulfuric")
        session.scalars.return_value.all.return_value = [a, b]
        inst = PostgresStructuredStore(session)
        assert inst._match_english_token_set("Sulfuric acid") is None


class TestProvenancePayload:
    def _store(self, **extra):
        store = MagicMock()
        store.lookup_oel_field.return_value = {
            "field": "TWA",
            "value": 1.0,
            "unit": "ppm",
            "source_row_key": "row_table_093_101_006",
            "page_number": 93,
            "cell_id": "cell-1",
            "chemical_name": "oxide Ethylene",
            "cas": "75-21-8",
            "chemical_id": "chem-1",
            "validation_status": "accepted",
            "gold_artifact_path": "canonical_evidence_v1",
            **extra,
        }
        return store

    def test_includes_symbols_and_health_effect_without_dropping_citation(self):
        agent = StructuredAgent(self._store(symbols="2A", health_effect="پوست"))
        res = agent.execute("STRUCTURED.OEL.PROVENANCE", {"chemical_name": "Ethylene oxide"})
        assert res.success
        assert res.data["source_row_key"] == "row_table_093_101_006"
        assert res.data["page_number"] == 93
        assert res.data["cell_id"] == "cell-1"
        assert res.data["symbols"] == "2A"
        assert res.data["health_effect"] == "پوست"
        assert "2A" in res.data["original_value"]
        assert res.data.get("value") is None
        assert res.citations[0]["page_number"] == 93
        assert res.citations[0]["source_row_key"] == "row_table_093_101_006"

    def test_omits_missing_canonical_text_fields(self):
        agent = StructuredAgent(self._store())
        res = agent.execute("STRUCTURED.OEL.PROVENANCE", {"chemical_name": "Ethylene oxide"})
        assert res.success
        assert "symbols" not in res.data
        assert "health_effect" not in res.data
        assert "original_value" not in res.data
        assert res.data["page_number"] == 93
