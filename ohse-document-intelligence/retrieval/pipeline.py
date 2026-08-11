"""Production retrieval pipeline — vector + lexical + metadata + fusion + rerank."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from persistence.semantic_store import embed_query_vector, search_by_query_vector, search_persian_semantic
from retrieval.lexical_index import LexicalIndex
from retrieval.reranker import LexicalReranker, NoOpReranker, RankedCandidate, Reranker, candidates_from_search_results

_TEST_CHUNK_PREFIX = "test_persian_"


class RetrievalMode(str, Enum):
    VECTOR_ONLY = "vector_only"
    LEXICAL_ONLY = "lexical_only"
    VECTOR_LEXICAL = "vector_lexical"
    VECTOR_METADATA = "vector_metadata"
    HYBRID_FUSION = "hybrid_fusion"
    HYBRID_RERANK = "hybrid_rerank"


@dataclass
class RetrievalConfig:
    mode: RetrievalMode = RetrievalMode.HYBRID_RERANK
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
    filtered = [c for c in candidates if c.get("page_number") == page]
    return filtered if filtered else candidates


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

        # Vector candidates
        vector_hits = _filter_test_chunks(
            search_by_query_vector(self.session, vector, limit=cfg.candidate_k),
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

        # Lexical candidates — used for supplemental fusion only
        lex_index = get_lexical_index(self.session)
        lexical = lex_index.search(query, limit=cfg.candidate_k)
        trace.append(f"lexical:{len(lexical)}")

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

        # HYBRID_FUSION or HYBRID_RERANK (default production)
        if page:
            vector_hits = _metadata_filter(vector_hits, page)
            trace.append("metadata_filter")
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


def _candidate_to_result(c: RankedCandidate) -> dict[str, Any]:
    meta = c.metadata or {}
    return {
        "chunk_id": c.chunk_id,
        "content": c.content,
        "score": c.rerank_score if c.rerank_score is not None else c.score,
        "vector_score": c.score,
        "rerank_score": c.rerank_score,
        "page_number": meta.get("page_number"),
        "section_title": meta.get("section_title"),
        "retrieval_trace": meta.get("retrieval_method"),
    }


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000
