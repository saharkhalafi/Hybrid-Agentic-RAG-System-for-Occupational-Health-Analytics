"""Retrieval scope filter audit — documents filter parity across pipeline paths.

Run ``python -m retrieval.scope_audit`` for a human-readable parity report.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeFilterSpec:
    name: str
    vector_stage: str
    lexical_stage: str
    notes: str


# Corpus-level filters enforced identically for vector (SQL WHERE) and lexical (index build).
CORPUS_FILTERS: tuple[ScopeFilterSpec, ...] = (
    ScopeFilterSpec(
        name="source_type=semantic_text",
        vector_stage="db_query",
        lexical_stage="index_build",
        notes="search_by_query_vector + LexicalIndex.build_from_session",
    ),
    ScopeFilterSpec(
        name="validation_status=accepted",
        vector_stage="db_query",
        lexical_stage="index_build",
        notes="Production semantic scope only",
    ),
    ScopeFilterSpec(
        name="language=fa",
        vector_stage="db_query",
        lexical_stage="index_build",
        notes="Persian production chunks only",
    ),
    ScopeFilterSpec(
        name="embedding IS NOT NULL",
        vector_stage="db_query",
        lexical_stage="n/a",
        notes="Vector path only; lexical index skips unembedded rows implicitly",
    ),
    ScopeFilterSpec(
        name="exclude_test_chunks",
        vector_stage="post_retrieval",
        lexical_stage="index_build",
        notes="test_persian_* prefix excluded in both paths",
    ),
)

RUNTIME_FILTERS: tuple[ScopeFilterSpec, ...] = (
    ScopeFilterSpec(
        name="page_hint / page_number",
        vector_stage="post_retrieval",
        lexical_stage="post_retrieval",
        notes="_apply_page_scope() in pipeline.py — shared for HYBRID_RERANK and VECTOR_LEXICAL",
    ),
    ScopeFilterSpec(
        name="page_hint (metadata boost, no filter)",
        vector_stage="post_retrieval (boost)",
        lexical_stage="n/a",
        notes="VECTOR_METADATA without page uses _metadata_boost on vector only",
    ),
)

# Intentionally excluded from semantic retrieval scope (NOT a parity bug).
#
# chemical_id_hint and cas_hint are resolved in structured routing (QueryOrchestrator,
# chemical_resolver, PostgresStructuredStore). Semantic retrieval answers document
# passages by query embedding + page_hint — it does not filter chunks by chemical_id.
# Adding chemical_id filtering here would be a new feature, not a missing parity fix.
INTENTIONALLY_OUT_OF_SEMANTIC_SCOPE: tuple[ScopeFilterSpec, ...] = (
    ScopeFilterSpec(
        name="chemical_id_hint",
        vector_stage="n/a (structured routing)",
        lexical_stage="n/a (structured routing)",
        notes="Resolved before agent dispatch; authoritative OELs use structured store, not chunk filter",
    ),
    ScopeFilterSpec(
        name="cas_hint",
        vector_stage="n/a (structured routing)",
        lexical_stage="n/a (structured routing)",
        notes="Same as chemical_id_hint — CAS maps to structured lookup, not semantic chunk scope",
    ),
    ScopeFilterSpec(
        name="document_id",
        vector_stage="optional SQL param only",
        lexical_stage="n/a",
        notes="search_by_query_vector accepts document_id; ProductionRetrievalPipeline.retrieve does not expose it",
    ),
)


def parity_report() -> str:
    lines = ["Retrieval scope filter parity audit", "=" * 40, "", "Corpus filters (build-time / SQL):"]
    for spec in CORPUS_FILTERS:
        lines.append(f"  {spec.name}")
        lines.append(f"    vector:  {spec.vector_stage}")
        lines.append(f"    lexical: {spec.lexical_stage}")
        lines.append(f"    notes:   {spec.notes}")
        lines.append("")
    lines.append("Runtime filters:")
    for spec in RUNTIME_FILTERS:
        lines.append(f"  {spec.name}")
        lines.append(f"    vector:  {spec.vector_stage}")
        lines.append(f"    lexical: {spec.lexical_stage}")
        lines.append(f"    notes:   {spec.notes}")
        lines.append("")
    lines.append("Intentionally outside semantic retrieval scope (not parity bugs):")
    for spec in INTENTIONALLY_OUT_OF_SEMANTIC_SCOPE:
        lines.append(f"  {spec.name}")
        lines.append(f"    vector:  {spec.vector_stage}")
        lines.append(f"    lexical: {spec.lexical_stage}")
        lines.append(f"    notes:   {spec.notes}")
        lines.append("")
    lines.extend(
        [
            "",
            "Reranker (LexicalReranker): operates on pre-scoped candidate pool only — no independent metadata filter.",
            "",
            "Known asymmetry class (fixed 2026-08-14): page_hint was applied to vector but not lexical in HYBRID_RERANK.",
            "Regression: tests/test_hybrid_rerank_page_scope.py",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    print(parity_report())
