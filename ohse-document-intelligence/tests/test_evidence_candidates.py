"""General evidence-candidate and identity-rerank tests. No Goldset IDs."""

from __future__ import annotations

from retrieval.evidence_candidates import (
    identity_matches,
    is_formula_query,
    merge_evidence_candidates,
    requested_fields,
    structured_row_to_candidate,
    union_additive_candidates,
)
from retrieval.lexical_index import _tokenize
from retrieval.reranker import EvidenceReranker, candidates_from_search_results
from retrieval.semantic_retrieval import ROW_KNOWLEDGE_SOURCE_TYPE, SEMANTIC_SOURCE_TYPE


def test_stel_c_and_cas_are_requested_fields():
    fields = requested_fields("STEL/C for 107-02-8", {"cas": "107-02-8", "oel_type": "STEL_C"})
    assert "stel_c" in fields


def test_tokenize_keeps_cas_as_one_token():
    tokens = _tokenize("Acrolein 107-02-8 TWA")
    assert "107-02-8" in tokens


def test_merge_preserves_backbone_order_when_later_source_has_higher_score():
    semantic = [
        {
            "chunk_id": "semantic_021_01",
            "content": "narrative definition",
            "score": 0.71,
            "source_type": SEMANTIC_SOURCE_TYPE,
            "candidate_source": "vector",
            "evidence_type": SEMANTIC_SOURCE_TYPE,
        }
    ]
    structured = [
        structured_row_to_candidate(
            {
                "cas": "71-43-2",
                "english_name": "Benzene",
                "twa": 0.5,
                "source_row_key": "row_table_048_01_001",
                "page_number": 48,
            },
            field="TWA",
        )
    ]
    structured[0]["score"] = 0.99
    merged = merge_evidence_candidates([semantic, structured], limit=10)
    assert merged[0]["chunk_id"] == "semantic_021_01"
    assert any(m.get("evidence_type") == "structured" for m in merged)


def test_is_formula_query_ignores_generic_calculation_wording():
    assert is_formula_query("TWA چگونه محاسبه می‌شود؟") is False
    assert is_formula_query("فرمول AHV چیست؟") is True
    assert is_formula_query("مقدار ahv را حساب کن", {"variables": {"t1": 1}}) is True


def test_merge_keeps_structured_when_semantic_duplicates_row():
    structured = [
        structured_row_to_candidate(
            {
                "cas": "107-02-8",
                "english_name": "Acrolein",
                "stel": 0.05,
                "source_row_key": "row_table_047_01_008",
                "page_number": 47,
            },
            field="STEL",
        )
    ]
    semantic = [
        {
            "chunk_id": "row_table_047_01_008",
            "content": "unrelated narrative about solvents",
            "score": 0.99,
            "page_number": 12,
            "source_type": SEMANTIC_SOURCE_TYPE,
            "candidate_source": "vector",
            "evidence_type": SEMANTIC_SOURCE_TYPE,
        }
    ]
    merged = merge_evidence_candidates([structured, semantic], limit=10)
    types = {m["evidence_type"] for m in merged}
    assert "structured" in types
    assert SEMANTIC_SOURCE_TYPE in types
    assert any(m.get("cas") == "107-02-8" for m in merged)


def test_evidence_reranker_prefers_cas_field_structured():
    cands = candidates_from_search_results(
        [
            {
                "chunk_id": "semantic_noise",
                "content": "general occupational exposure discussion",
                "score": 0.78,
                "source_type": SEMANTIC_SOURCE_TYPE,
                "evidence_type": SEMANTIC_SOURCE_TYPE,
            },
            {
                "chunk_id": "row_table_047_01_008",
                "content": "CAS: 107-02-8\nchemical_name: Acrolein\nstel: 0.05",
                "score": 0.78,
                "source_type": ROW_KNOWLEDGE_SOURCE_TYPE,
                "evidence_type": "structured",
                "cas": "107-02-8",
                "field": "STEL",
                "chemical_name": "Acrolein",
            },
        ]
    )
    ranked = EvidenceReranker().rerank("STEL/C 107-02-8", cands, top_k=2)
    assert ranked[0].chunk_id == "row_table_047_01_008"


def test_identity_lookup_patterns_prefer_cas():
    from retrieval.evidence_candidates import identity_lookup_patterns

    assert identity_lookup_patterns("EPN", {"cas": "2104-64-5", "chemical_name": "EPN"}) == ["2104-64-5"]
    assert identity_lookup_patterns("TWA benzene", {}) == ["benzene"]
    assert identity_lookup_patterns("TWA چیست", {"cas": "71-43-2", "chemical_name": "Benzene"}) == []


def test_identity_matches_cas_and_latin_name():
    row = {
        "content": "chemical_name: EPN\nCAS: 2104-64-5\nsymbols: skin",
        "evidence_type": ROW_KNOWLEDGE_SOURCE_TYPE,
    }
    assert identity_matches("مبنای حد مجاز برای EPN چیست؟", row, {}) is True
    assert identity_matches("TWA benzene", row, {}) is False


def test_union_prepends_identity_row_knowledge_without_dropping_backbone():
    backbone = [
        {
            "chunk_id": "semantic_021_01",
            "content": "definition of TWA",
            "score": 0.8,
            "source_type": SEMANTIC_SOURCE_TYPE,
            "candidate_source": "vector",
            "evidence_type": SEMANTIC_SOURCE_TYPE,
        },
        {
            "chunk_id": "semantic_022_01",
            "content": "other narrative",
            "score": 0.7,
            "source_type": SEMANTIC_SOURCE_TYPE,
            "candidate_source": "vector",
            "evidence_type": SEMANTIC_SOURCE_TYPE,
        },
    ]
    rows = [
        {
            "chunk_id": "row_table_090_01_001",
            "content": "chemical_name: EPN CAS: 2104-64-5",
            "score": 0.4,
            "source_type": ROW_KNOWLEDGE_SOURCE_TYPE,
            "candidate_source": "row_knowledge",
            "evidence_type": ROW_KNOWLEDGE_SOURCE_TYPE,
        }
    ]
    merged = union_additive_candidates(
        backbone,
        [rows],
        query="مبنای حد مجاز برای EPN چیست؟",
        extra_limit=10,
    )
    assert merged[0]["chunk_id"] == "semantic_021_01"
    assert any(m["chunk_id"] == "row_table_090_01_001" for m in merged)
    assert [m["chunk_id"] for m in merged if m["evidence_type"] == SEMANTIC_SOURCE_TYPE] == [
        "semantic_021_01",
        "semantic_022_01",
    ]


def test_evidence_reranker_promotes_row_knowledge_on_name_identity():
    cands = candidates_from_search_results(
        [
            {
                "chunk_id": "semantic_top",
                "content": "general occupational exposure discussion",
                "score": 0.81,
                "source_type": SEMANTIC_SOURCE_TYPE,
                "evidence_type": SEMANTIC_SOURCE_TYPE,
            },
            {
                "chunk_id": "row_table_090_01_001",
                "content": "chemical_name: EPN CAS: 2104-64-5 skin BEI",
                "score": 0.41,
                "source_type": ROW_KNOWLEDGE_SOURCE_TYPE,
                "evidence_type": ROW_KNOWLEDGE_SOURCE_TYPE,
            },
        ]
    )
    ranked = EvidenceReranker().rerank("مبنای حد مجاز برای EPN چیست؟", cands, top_k=2)
    assert ranked[0].chunk_id == "row_table_090_01_001"


def test_evidence_reranker_does_not_replace_backbone_already_carrying_identity():
    cands = candidates_from_search_results(
        [
            {
                "chunk_id": "semantic_naphthalene",
                "content": "نفتالین Naphthalene effects on blood and eyes",
                "score": 0.81,
                "source_type": SEMANTIC_SOURCE_TYPE,
                "evidence_type": SEMANTIC_SOURCE_TYPE,
            },
            {
                "chunk_id": "row_table_121_01_001",
                "content": "chemical_name: Naphthalene CAS: 91-20-3",
                "score": 0.2,
                "source_type": ROW_KNOWLEDGE_SOURCE_TYPE,
                "evidence_type": ROW_KNOWLEDGE_SOURCE_TYPE,
            },
        ]
    )
    ranked = EvidenceReranker().rerank("مواجهه با نفتالین (Naphthalene) چه اثراتی دارد؟", cands, top_k=2)
    assert ranked[0].chunk_id == "semantic_naphthalene"


def test_evidence_reranker_preserves_vector_order_without_requested_field():
    cands = candidates_from_search_results(
        [
            {
                "chunk_id": "semantic_top",
                "content": "general occupational exposure discussion",
                "score": 0.81,
                "source_type": SEMANTIC_SOURCE_TYPE,
                "evidence_type": SEMANTIC_SOURCE_TYPE,
            },
            {
                "chunk_id": "structured:row_table_047_01_008",
                "content": "CAS: 107-02-8\nchemical_name: Acrolein\nstel: 0.05",
                "score": 0.0,
                "source_type": "structured",
                "evidence_type": "structured",
                "cas": "107-02-8",
                "chemical_name": "Acrolein",
            },
        ]
    )
    ranked = EvidenceReranker().rerank("What is occupational exposure for solvents?", cands, top_k=2)
    assert ranked[0].chunk_id == "semantic_top"


def test_structured_candidate_copies_stored_values_only():
    cand = structured_row_to_candidate(
        {"cas": "71-43-2", "english_name": "Benzene", "twa": 0.5, "page_number": 48, "source_row_key": "r1"},
        field="TWA",
    )
    assert "0.5" in cand["content"]
    assert "71-43-2" in cand["content"]
    assert cand["value"] is None or cand["twa"] == 0.5
