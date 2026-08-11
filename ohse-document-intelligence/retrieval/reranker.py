"""Pluggable reranking interface for semantic retrieval (Phase C.2)."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RankedCandidate:
    chunk_id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    original_rank: int = 0
    rerank_score: float | None = None


@dataclass
class RerankerMetrics:
    latency_ms: float = 0.0
    model_name: str = "none"
    model_size_mb: float | None = None
    memory_mb: float | None = None
    installation: str = "built-in"


class Reranker(ABC):
    """Abstract reranker — SemanticAgent must not hard-code a specific model."""

    name: str = "base"

    @abstractmethod
    def rerank(self, query: str, candidates: list[RankedCandidate], *, top_k: int = 5) -> list[RankedCandidate]:
        ...

    def info(self) -> RerankerMetrics:
        return RerankerMetrics(model_name=self.name)


class NoOpReranker(Reranker):
    """Baseline: preserve vector similarity order."""

    name = "baseline_no_rerank"

    def rerank(self, query: str, candidates: list[RankedCandidate], *, top_k: int = 5) -> list[RankedCandidate]:
        return candidates[:top_k]


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _normalize(text: str) -> set[str]:
    t = text.lower().translate(_PERSIAN_DIGITS)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return {w for w in t.split() if len(w) > 1}


class LexicalReranker(Reranker):
    """Local token-overlap reranker (no external deps)."""

    name = "lexical_overlap"

    def rerank(self, query: str, candidates: list[RankedCandidate], *, top_k: int = 5) -> list[RankedCandidate]:
        q_tokens = _normalize(query)
        if not q_tokens:
            return candidates[:top_k]

        scored: list[tuple[float, RankedCandidate]] = []
        for cand in candidates:
            text = cand.content
            if cand.metadata.get("enriched_content"):
                text = str(cand.metadata["enriched_content"])
            c_tokens = _normalize(text)
            if not c_tokens:
                overlap = 0.0
            else:
                overlap = len(q_tokens & c_tokens) / len(q_tokens | c_tokens)
            combined = 0.6 * cand.score + 0.4 * overlap
            cand.rerank_score = combined
            scored.append((combined, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    def info(self) -> RerankerMetrics:
        return RerankerMetrics(
            model_name=self.name,
            model_size_mb=0.0,
            installation="built-in (token overlap + vector blend)",
        )


class CrossEncoderReranker(Reranker):
    """Optional cross-encoder reranker via sentence-transformers (if installed)."""

    name = "cross_encoder"

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model_name = model_name
        self._model = None
        self._available = False
        try:
            from sentence_transformers import CrossEncoder  # type: ignore[import-untyped]

            self._model = CrossEncoder(model_name)
            self._available = True
            self.name = f"cross_encoder:{model_name.split('/')[-1]}"
        except ImportError:
            pass

    @property
    def available(self) -> bool:
        return self._available

    def rerank(self, query: str, candidates: list[RankedCandidate], *, top_k: int = 5) -> list[RankedCandidate]:
        if not self._available or not candidates:
            return LexicalReranker().rerank(query, candidates, top_k=top_k)

        pairs = [[query, c.content] for c in candidates]
        t0 = time.perf_counter()
        scores = self._model.predict(pairs)  # type: ignore[union-attr]
        latency = (time.perf_counter() - t0) * 1000

        scored = list(zip(scores, candidates, strict=True))
        scored.sort(key=lambda x: float(x[0]), reverse=True)
        out: list[RankedCandidate] = []
        for score, cand in scored[:top_k]:
            cand.rerank_score = float(score)
            out.append(cand)
        self._last_latency_ms = latency
        return out

    def info(self) -> RerankerMetrics:
        return RerankerMetrics(
            model_name=self.name,
            model_size_mb=80.0 if self._available else None,
            installation="pip install sentence-transformers" if not self._available else "sentence-transformers",
        )


class BGEReranker(Reranker):
    """BGE-family reranker via FlagEmbedding (if installed)."""

    name = "bge_reranker"

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3") -> None:
        self.model_name = model_name
        self._model = None
        self._available = False
        try:
            from FlagEmbedding import FlagReranker  # type: ignore[import-untyped]

            self._model = FlagReranker(model_name, use_fp16=False)
            self._available = True
            self.name = f"bge:{model_name.split('/')[-1]}"
        except ImportError:
            pass

    @property
    def available(self) -> bool:
        return self._available

    def rerank(self, query: str, candidates: list[RankedCandidate], *, top_k: int = 5) -> list[RankedCandidate]:
        if not self._available or not candidates:
            return LexicalReranker().rerank(query, candidates, top_k=top_k)

        pairs = [[query, c.content] for c in candidates]
        t0 = time.perf_counter()
        scores = self._model.compute_score(pairs)  # type: ignore[union-attr]
        if isinstance(scores, float):
            scores = [scores]
        latency = (time.perf_counter() - t0) * 1000

        scored = list(zip(scores, candidates, strict=True))
        scored.sort(key=lambda x: float(x[0]), reverse=True)
        out: list[RankedCandidate] = []
        for score, cand in scored[:top_k]:
            cand.rerank_score = float(score)
            out.append(cand)
        self._last_latency_ms = latency
        return out

    def info(self) -> RerankerMetrics:
        return RerankerMetrics(
            model_name=self.name,
            model_size_mb=1100.0 if self._available else None,
            installation="pip install FlagEmbedding" if not self._available else "FlagEmbedding",
        )


def get_available_rerankers() -> list[Reranker]:
    """Return all rerankers to benchmark (always includes baseline + lexical)."""
    rerankers: list[Reranker] = [NoOpReranker(), LexicalReranker()]
    ce = CrossEncoderReranker()
    if ce.available:
        rerankers.append(ce)
    bge = BGEReranker()
    if bge.available:
        rerankers.append(bge)
    return rerankers


def candidates_from_search_results(results: list[dict[str, Any]]) -> list[RankedCandidate]:
    out: list[RankedCandidate] = []
    for i, r in enumerate(results):
        content = str(r.get("content") or r.get("text") or "")
        meta = dict(r)
        if r.get("enriched_content"):
            meta["enriched_content"] = r["enriched_content"]
        out.append(
            RankedCandidate(
                chunk_id=str(r.get("chunk_id", "")),
                content=content,
                score=float(r.get("score") or 0.0),
                metadata=meta,
                original_rank=i + 1,
            )
        )
    return out
