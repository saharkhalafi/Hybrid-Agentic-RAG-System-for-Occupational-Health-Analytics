"""Production retrieval pipeline — vector + lexical + metadata + fusion + rerank."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from persistence.semantic_store import embed_query_vector, search_by_query_vector, search_persian_semantic
from retrieval.evidence_candidates import (
    annotate_candidate,
    candidate_log_rows,
    compact_pre_rerank,
    formula_row_to_candidate,
    identity_lookup_patterns,
    is_formula_query,
    requested_fields,
    structured_row_to_candidate,
    union_additive_candidates,
)
from retrieval.lexical_index import LexicalIndex
from retrieval.page_identity import candidate_page_values, retrieval_page_fields
from retrieval.reranker import (
    EvidenceReranker,
    LexicalReranker,
    NoOpReranker,
    RankedCandidate,
    Reranker,
    candidates_from_search_results,
)
from retrieval.semantic_retrieval import ROW_KNOWLEDGE_SOURCE_TYPE, SEMANTIC_SOURCE_TYPE, TEST_CHUNK_ID_PREFIX

_TEST_CHUNK_PREFIX = TEST_CHUNK_ID_PREFIX


class RetrievalMode(str, Enum):
    VECTOR_ONLY = "vector_only"
    LEXICAL_ONLY = "lexical_only"
    VECTOR_LEXICAL = "vector_lexical"
    VECTOR_METADATA = "vector_metadata"
    HYBRID_FUSION = "hybrid_fusion"
    HYBRID_RERANK = "hybrid_rerank"
    EVIDENCE_CANDIDATES = "evidence_candidates"


@dataclass
class RetrievalConfig:
    mode: RetrievalMode = RetrievalMode.VECTOR_METADATA
    candidate_k: int = 30
    final_k: int = 5
    rrf_k: int = 60
    vector_weight: float = 0.6
    lexical_weight: float = 0.4
    page_number: int | None = None
    exclude_test_chunks: bool = True
    reranker: Reranker | None = None


@dataclass
class RetrievalResult:
    chunks: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    mode: str = ""
    trace: list[str] = field(default_factory=list)
    pre_rerank_candidates: list[dict[str, Any]] = field(default_factory=list)


# Module-level lexical index cache (built once per process)
_LEXICAL_INDEX: LexicalIndex | None = None


def get_lexical_index(session: Session) -> LexicalIndex:
    global _LEXICAL_INDEX
    if _LEXICAL_INDEX is None or not _LEXICAL_INDEX._built:
        _LEXICAL_INDEX = LexicalIndex()
        _LEXICAL_INDEX.build_from_session(session)
    return _LEXICAL_INDEX


def reset_lexical_index() -> None:
    global _LEXICAL_INDEX
    _LEXICAL_INDEX = None


def _filter_test_chunks(results: list[dict[str, Any]], exclude: bool) -> list[dict[str, Any]]:
    if not exclude:
        return results
    return [r for r in results if not str(r.get("chunk_id", "")).startswith(_TEST_CHUNK_PREFIX)]


def _rrf_fuse(
    ranked_lists: list[list[dict[str, Any]]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion across multiple retrieval lists."""
    weights = weights or [1.0] * len(ranked_lists)
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for w, lst in zip(weights, ranked_lists, strict=False):
        for rank, item in enumerate(lst, start=1):
            cid = str(item.get("chunk_id", ""))
            if not cid:
                continue
            scores[cid] = scores.get(cid, 0.0) + w * (1.0 / (k + rank))
            if cid not in payloads:
                payloads[cid] = dict(item)
            payloads[cid]["fusion_score"] = scores[cid]
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [payloads[cid] for cid, _ in ordered]


def _metadata_filter(candidates: list[dict[str, Any]], page: int | None) -> list[dict[str, Any]]:
    if page is None:
        return candidates
    filtered = [c for c in candidates if page in candidate_page_values(c)]
    return filtered if filtered else candidates


def _apply_page_scope(
    vector_hits: list[dict[str, Any]],
    lexical: list[dict[str, Any]],
    page: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply identical page scoping to vector and lexical candidate pools."""
    if page is None:
        return vector_hits, lexical
    return _metadata_filter(vector_hits, page), _metadata_filter(lexical, page)


def _merge_vector_lexical(
    vector: list[dict[str, Any]],
    lexical: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Keep vector ordering as primary; inject high-scoring lexical hits already in vector pool."""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    lex_by_id = {str(r.get("chunk_id", "")): r for r in lexical}
    for item in vector:
        cid = str(item.get("chunk_id", ""))
        if not cid or cid in seen:
            continue
        row = dict(item)
        lex = lex_by_id.get(cid)
        if lex:
            row["lexical_score"] = lex.get("score")
            row["enriched_content"] = lex.get("enriched_content")
        merged.append(row)
        seen.add(cid)
        if len(merged) >= limit:
            return merged
    # Only add lexical-only hits when they strongly match (top lexical, not in vector)
    for item in lexical:
        cid = str(item.get("chunk_id", ""))
        if not cid or cid in seen:
            continue
        merged.append(dict(item))
        seen.add(cid)
        if len(merged) >= limit:
            break
    return merged


def _metadata_boost(candidates: list[dict[str, Any]], page: int | None) -> list[dict[str, Any]]:
    if page is None:
        return candidates
    boosted = []
    for c in candidates:
        cp = c.get("page_number")
        bonus = 0.08 if cp == page else 0.0
        c = dict(c)
        c["score"] = min(1.0, float(c.get("score") or 0) + bonus)
        boosted.append(c)
    boosted.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return boosted


class ProductionRetrievalPipeline:
    """Query → normalize → vector + lexical → fusion → rerank → Top-K."""

    def __init__(self, session: Session, *, config: RetrievalConfig | None = None) -> None:
        self.session = session
        self.config = config or RetrievalConfig()

    def retrieve(
        self,
        query: str,
        *,
        page_hint: int | None = None,
        query_vector: list[float] | None = None,
        slots: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        t0 = time.perf_counter()
        cfg = self.config
        page = page_hint or cfg.page_number
        trace: list[str] = [f"mode:{cfg.mode.value}"]
        vector = query_vector or embed_query_vector(query)

        if cfg.mode == RetrievalMode.VECTOR_ONLY:
            vector_hits = _filter_test_chunks(
                search_by_query_vector(self.session, vector, limit=cfg.final_k),
                cfg.exclude_test_chunks,
            )
            return RetrievalResult(chunks=vector_hits[: cfg.final_k], latency_ms=_ms(t0), mode=cfg.mode.value, trace=trace)

        if cfg.mode == RetrievalMode.LEXICAL_ONLY:
            lex = get_lexical_index(self.session)
            lexical = lex.search(query, limit=cfg.final_k)
            return RetrievalResult(chunks=lexical, latency_ms=_ms(t0), mode=cfg.mode.value, trace=trace + ["lexical"])

        # Vector backbone: evidence mode keeps the previous semantic_text ANN window.
        vector_source_types = (
            (SEMANTIC_SOURCE_TYPE,)
            if cfg.mode == RetrievalMode.EVIDENCE_CANDIDATES
            else None
        )
        vector_hits = _filter_test_chunks(
            search_by_query_vector(
                self.session,
                vector,
                limit=cfg.candidate_k,
                source_types=vector_source_types,
            ),
            cfg.exclude_test_chunks,
        )
        trace.append(f"vector:{len(vector_hits)}")

        if cfg.mode == RetrievalMode.VECTOR_METADATA:
            if page:
                vector_hits = _metadata_filter(vector_hits, page)
                trace.append("metadata_filter")
            else:
                vector_hits = _metadata_boost(vector_hits, page)
                trace.append("metadata_boost")
            reranker = cfg.reranker or NoOpReranker()
            cands = candidates_from_search_results(vector_hits)
            ranked = reranker.rerank(query, cands, top_k=cfg.final_k)
            return RetrievalResult(
                chunks=[_candidate_to_result(c) for c in ranked],
                latency_ms=_ms(t0),
                mode=cfg.mode.value,
                trace=trace,
            )

        # Lexical candidates — page scope must match vector path (see scope_audit.py)
        lex_index = get_lexical_index(self.session)
        lexical = lex_index.search(query, limit=cfg.candidate_k)
        trace.append(f"lexical:{len(lexical)}")

        if page:
            vector_hits, lexical = _apply_page_scope(vector_hits, lexical, page)
            trace.append("page_scope_filter")

        if cfg.mode == RetrievalMode.VECTOR_LEXICAL:
            # Vector-first: rerank vector pool with lexical scores; avoid full RRF pollution
            merged = _merge_vector_lexical(vector_hits, lexical, limit=cfg.candidate_k)
            reranker = cfg.reranker or LexicalReranker()
            cands = candidates_from_search_results(merged)
            ranked = reranker.rerank(query, cands, top_k=cfg.final_k)
            trace.append(f"rerank:{reranker.name}")
            return RetrievalResult(
                chunks=[_candidate_to_result(c) for c in ranked],
                latency_ms=_ms(t0),
                mode=cfg.mode.value,
                trace=trace,
            )

        if cfg.mode == RetrievalMode.EVIDENCE_CANDIDATES:
            return self._retrieve_evidence_candidates(
                query,
                query_vector=vector,
                vector_hits=vector_hits,
                lexical=lexical,
                page=page,
                slots=slots or {},
                t0=t0,
                trace=trace,
            )

        # HYBRID_FUSION or HYBRID_RERANK (default production)
        merged = _merge_vector_lexical(vector_hits, lexical, limit=cfg.candidate_k)
        reranker = cfg.reranker or LexicalReranker()
        cands = candidates_from_search_results(merged)
        annotated = [annotate_candidate(m, source=str(m.get("retrieval_method") or "merged")) for m in merged]
        pre_full = compact_pre_rerank(annotated)
        ranked = reranker.rerank(query, cands, top_k=cfg.final_k)
        trace.append(f"rerank:{reranker.name}")

        return RetrievalResult(
            chunks=[_candidate_to_result(c) for c in ranked],
            latency_ms=_ms(t0),
            mode=cfg.mode.value,
            trace=trace,
            pre_rerank_candidates=pre_full,
        )

    def _retrieve_evidence_candidates(
        self,
        query: str,
        *,
        query_vector: list[float],
        vector_hits: list[dict[str, Any]],
        lexical: list[dict[str, Any]],
        page: int | None,
        slots: dict[str, Any],
        t0: float,
        trace: list[str],
    ) -> RetrievalResult:
        cfg = self.config
        if page:
            vector_hits, lexical = _apply_page_scope(vector_hits, lexical, page)
            trace.append("page_scope_filter")

        vector_pool = [
            annotate_candidate(
                item,
                source="vector",
                evidence_type=str(item.get("source_type") or SEMANTIC_SOURCE_TYPE),
            )
            for item in vector_hits
        ]
        lexical_pool = [
            annotate_candidate(
                item,
                source="lexical",
                evidence_type=str(item.get("source_type") or SEMANTIC_SOURCE_TYPE),
            )
            for item in lexical
        ]
        row_raw = _filter_test_chunks(
            search_by_query_vector(
                self.session,
                query_vector,
                limit=min(10, cfg.candidate_k),
                source_types=(ROW_KNOWLEDGE_SOURCE_TYPE,),
            ),
            cfg.exclude_test_chunks,
        )
        if page:
            row_raw = _metadata_filter(row_raw, page)
        row_pool = [
            annotate_candidate(
                item,
                source="row_knowledge",
                evidence_type=ROW_KNOWLEDGE_SOURCE_TYPE,
            )
            for item in row_raw
        ]
        identity_pool = _row_knowledge_identity_pool(self.session, query, slots)
        structured_pool = _structured_candidate_pool(self.session, query, slots)
        formula_pool = _formula_candidate_pool(self.session, query, slots)
        backbone_ids = {str(v.get("chunk_id")) for v in vector_pool}
        lexical_only = [
            item
            for item in lexical_pool
            if str(item.get("chunk_id")) not in backbone_ids
        ]
        trace.append(f"row_identity:{len(identity_pool)}")
        trace.append(f"row_knowledge:{len(row_pool)}")
        trace.append(f"structured:{len(structured_pool)}")
        trace.append(f"formula:{len(formula_pool)}")
        trace.append(f"lexical_extra:{len(lexical_only)}")

        extra_cap = min(30, cfg.candidate_k)
        merged = union_additive_candidates(
            vector_pool,
            [identity_pool, row_pool, lexical_only, structured_pool, formula_pool],
            query=query,
            slots=slots,
            extra_limit=extra_cap,
        )
        for item in merged:
            item.setdefault("slots", slots)
        pre_full = compact_pre_rerank(merged)
        pre_log = candidate_log_rows(merged)
        if pre_log:
            head = pre_log[0]
            trace.append(
                "pre_rerank:"
                f"{head.get('source')}|{head.get('evidence_type')}|{head.get('id')}|p{head.get('page')}"
            )
        reranker = cfg.reranker or EvidenceReranker()
        cands = candidates_from_search_results(merged)
        ranked = reranker.rerank(query, cands, top_k=cfg.final_k)
        trace.append(f"rerank:{reranker.name}")
        return RetrievalResult(
            chunks=[_candidate_to_result(c) for c in ranked],
            latency_ms=_ms(t0),
            mode=cfg.mode.value,
            trace=trace,
            pre_rerank_candidates=pre_full,
        )


def _row_knowledge_identity_pool(
    session: Session,
    query: str,
    slots: dict[str, Any],
) -> list[dict[str, Any]]:
    """Exact CAS/name lookup on stored row_knowledge. Additive; no invented values."""
    patterns = identity_lookup_patterns(query, slots)
    if not patterns:
        return []
    try:
        return _row_knowledge_identity_pool_query(session, patterns)
    except Exception:
        return []


def _row_knowledge_identity_pool_query(session: Session, patterns: list[str]) -> list[dict[str, Any]]:
    from sqlalchemy import or_, select

    from database.models import DocumentChunk
    from retrieval.semantic_retrieval import SEMANTIC_VALIDATION_STATUS

    text_filters = [DocumentChunk.content.ilike(f"%{pattern}%") for pattern in patterns]
    stmt = (
        select(DocumentChunk)
        .where(
            DocumentChunk.source_type == ROW_KNOWLEDGE_SOURCE_TYPE,
            DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
            DocumentChunk.chunk_id.is_not(None),
            ~DocumentChunk.chunk_id.like(f"{_TEST_CHUNK_PREFIX}%"),
            or_(*text_filters),
        )
        .limit(5)
    )
    chunks = list(session.scalars(stmt))
    chunks.sort(
        key=lambda chunk: (
            0 if str(chunk.chunk_id).startswith("row_table_") else 1,
            0 if not str(chunk.content or "").lstrip().startswith("{") else 1,
        )
    )
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        pages = retrieval_page_fields(
            page_number=chunk.page_number,
            printed_page_number=chunk.printed_page_number,
        )
        out.append(
            annotate_candidate(
                {
                    "chunk_id": chunk.chunk_id,
                    "content": chunk.content,
                    "score": 0.0,
                    "source_type": ROW_KNOWLEDGE_SOURCE_TYPE,
                    "page_number": pages["page_number"],
                    "printed_page_number": pages["printed_page_number"],
                    "section_title": chunk.section_title,
                    "provenance": chunk.provenance,
                },
                source="row_knowledge_identity",
                evidence_type=ROW_KNOWLEDGE_SOURCE_TYPE,
            )
        )
    return out


def _structured_candidate_pool(
    session: Session,
    query: str,
    slots: dict[str, Any],
) -> list[dict[str, Any]]:
    cas = slots.get("cas")
    name = slots.get("chemical_name")
    fields = requested_fields(query, slots)
    # Additive structured rows only for explicit chemical identity + requested field.
    if not fields or not (cas or name):
        return []
    from agents.structured.store import PostgresStructuredStore

    store = PostgresStructuredStore(session)
    rows: list[dict[str, Any]] = []
    if cas:
        rows = store.get_oel_by_cas(str(cas))
    elif name:
        rows = store.get_oel_by_chemical(str(name))
    if not rows and "molecular_weight" in fields:
        mw = store.resolve_molecular_weight(chemical_name=name, cas=cas)
        if mw:
            rows = [mw]
    out: list[dict[str, Any]] = []
    field = fields[0] if fields else None
    for row in rows[:2]:
        out.append(structured_row_to_candidate(row, field=field))
    return out


def _formula_candidate_pool(
    session: Session,
    query: str,
    slots: dict[str, Any],
) -> list[dict[str, Any]]:
    if not is_formula_query(query, slots):
        return []
    from agents.formula.agent import FormulaAgent

    agent = FormulaAgent(session)
    hits = agent.search_formulas(query, formula_id=slots.get("formula_id"), limit=5)
    return [formula_row_to_candidate(row) for row in hits]


def _candidate_to_result(c: RankedCandidate) -> dict[str, Any]:
    meta = c.metadata or {}
    pages = retrieval_page_fields(
        page_number=meta.get("page_number"),
        printed_page_number=meta.get("printed_page_number"),
    )
    return {
        "chunk_id": c.chunk_id,
        "content": c.content,
        "score": c.rerank_score if c.rerank_score is not None else c.score,
        "vector_score": c.score,
        "rerank_score": c.rerank_score,
        "page_number": pages["page_number"],
        "printed_page_number": pages["printed_page_number"],
        "document_page": pages["document_page"],
        "section_title": meta.get("section_title"),
        "retrieval_trace": meta.get("retrieval_method") or meta.get("candidate_source"),
        "source_type": meta.get("source_type"),
        "evidence_type": meta.get("evidence_type") or meta.get("source_type"),
        "candidate_source": meta.get("candidate_source"),
        "source_row_key": meta.get("source_row_key"),
        "formula_id": meta.get("formula_id"),
        "cas": meta.get("cas"),
        "chemical_name": meta.get("chemical_name"),
        "field": meta.get("field"),
        "value": meta.get("value"),
        "twa": meta.get("twa"),
        "stel": meta.get("stel"),
        "ceiling": meta.get("ceiling"),
        "provenance": meta.get("provenance"),
    }


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000
