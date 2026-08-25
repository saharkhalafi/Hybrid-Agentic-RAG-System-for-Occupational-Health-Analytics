"""Diagnose D_hybrid_rerank anomaly vs ground-truth corpus validity."""

from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select

from database.models import DocumentChunk
from database.session import SessionLocal
from retrieval.pipeline import ProductionRetrievalPipeline, RetrievalConfig, RetrievalMode, reset_lexical_index
from retrieval.reranker import LexicalReranker, NoOpReranker
from retrieval.semantic_baseline_eval import load_semantic_eval_cases
from retrieval.semantic_retrieval import SEMANTIC_LANGUAGE, SEMANTIC_SOURCE_TYPE, SEMANTIC_VALIDATION_STATUS


def _active_semantic_chunks(session) -> dict[str, DocumentChunk]:
    rows = session.scalars(
        select(DocumentChunk).where(
            DocumentChunk.source_type == SEMANTIC_SOURCE_TYPE,
            DocumentChunk.validation_status == SEMANTIC_VALIDATION_STATUS,
            DocumentChunk.language == SEMANTIC_LANGUAGE,
            DocumentChunk.embedding.is_not(None),
        )
    ).all()
    return {r.chunk_id: r for r in rows if r.chunk_id}


def check_ground_truth_validity(session, *, limit: int | None = 30) -> dict:
    cases = load_semantic_eval_cases(limit)
    corpus = _active_semantic_chunks(session)
    missing: list[dict] = []
    retired: list[dict] = []
    valid = 0
    all_gt_ids: set[str] = set()

    for case in cases:
        for cid in case.relevant_chunk_ids:
            all_gt_ids.add(cid)
            if cid in corpus:
                valid += 1
                continue
            row = session.scalar(select(DocumentChunk).where(DocumentChunk.chunk_id == cid))
            if row is None:
                missing.append({"chunk_id": cid, "query_id": case.query_id, "status": "not_in_db"})
            elif row.validation_status != SEMANTIC_VALIDATION_STATUS:
                retired.append(
                    {
                        "chunk_id": cid,
                        "query_id": case.query_id,
                        "status": row.validation_status,
                        "has_embedding": row.embedding is not None,
                    }
                )
            else:
                missing.append(
                    {
                        "chunk_id": cid,
                        "query_id": case.query_id,
                        "status": f"present_but_{row.validation_status}",
                        "has_embedding": row.embedding is not None,
                    }
                )

    return {
        "eval_cases": len(cases),
        "unique_ground_truth_ids": len(all_gt_ids),
        "valid_in_active_corpus": valid,
        "missing_from_db": missing,
        "retired_or_inactive": retired,
        "missing_count": len(missing),
        "retired_count": len(retired),
        "active_corpus_size": len(corpus),
    }


def compare_c_vs_d(session, *, limit: int = 10) -> list[dict]:
    cases = load_semantic_eval_cases(limit)
    rows: list[dict] = []
    for case in cases:
        c_pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(
                mode=RetrievalMode.VECTOR_METADATA,
                candidate_k=30,
                final_k=5,
                reranker=NoOpReranker(),
            ),
        )
        d_pipe = ProductionRetrievalPipeline(
            session,
            config=RetrievalConfig(
                mode=RetrievalMode.HYBRID_RERANK,
                candidate_k=30,
                final_k=5,
                reranker=LexicalReranker(),
            ),
        )
        c_res = c_pipe.retrieve(case.query, page_hint=case.page_number)
        d_res = d_pipe.retrieve(case.query, page_hint=case.page_number)
        c_ids = [c.get("chunk_id") for c in c_res.chunks]
        d_ids = [c.get("chunk_id") for c in d_res.chunks]
        c_hit = any(cid in case.relevant_chunk_ids for cid in c_ids)
        d_hit = any(cid in case.relevant_chunk_ids for cid in d_ids)
        rows.append(
            {
                "query_id": case.query_id,
                "page": case.page_number,
                "relevant": sorted(case.relevant_chunk_ids),
                "c_top5": c_ids,
                "d_top5": d_ids,
                "c_hit": c_hit,
                "d_hit": d_hit,
                "c_trace": c_res.trace,
                "d_trace": d_res.trace,
            }
        )
    return rows


def main() -> None:
    session = SessionLocal()
    try:
        reset_lexical_index()
        gt = check_ground_truth_validity(session, limit=30)
        compare = compare_c_vs_d(session, limit=10)
        out = {
            "ground_truth_validity": gt,
            "c_vs_d_sample": compare,
            "c_hits": sum(1 for r in compare if r["c_hit"]),
            "d_hits": sum(1 for r in compare if r["d_hit"]),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    finally:
        session.close()


if __name__ == "__main__":
    main()
