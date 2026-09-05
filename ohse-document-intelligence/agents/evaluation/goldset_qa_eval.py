"""Goldset QA evaluator — production routing + eval-time qrels. No second retriever."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.evaluation.goldset_qa_loader import GoldsetQAItem
from agents.evaluation.goldset_qa_relevance import (
    is_formula_record_relevant,
    is_semantic_chunk_relevant,
    is_structured_record_relevant,
)
from agents.orchestrator.pipeline import QueryOrchestrator, QueryResponse
from retrieval.eval_metrics import aggregate_metrics, hit_at_k, mrr, precision_at_k, recall_at_k

SEMANTIC_RANK_KS = (1, 3, 5, 10)


def retain_semantic_chunk(chunk: dict[str, Any], *, rank: int | None = None) -> dict[str, Any]:
    """Keep ranked chunk fields needed for page+text scoring."""
    meta = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    retained: dict[str, Any] = {}
    retained["chunk_id"] = chunk.get("chunk_id") or meta.get("chunk_id")
    retained["content"] = chunk.get("content") or chunk.get("text") or meta.get("content")
    retained["page_number"] = (
        chunk.get("page_number") if chunk.get("page_number") is not None else meta.get("page_number")
    )
    printed = chunk.get("printed_page_number")
    if printed is None:
        printed = meta.get("printed_page_number")
    retained["printed_page_number"] = printed
    score = chunk.get("score")
    if score is None:
        score = meta.get("score")
    retained["score"] = score
    retained["rank"] = chunk.get("rank") if chunk.get("rank") is not None else rank
    return retained


def retain_structured_record(
    record: dict[str, Any],
    *,
    citations: list[dict[str, Any]] | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    retained = dict(record)
    cite = (citations or record.get("citations") or [None])[0] or {}
    if isinstance(cite, dict):
        for key in ("source_row_key", "cell_id", "page_number", "record_id"):
            if retained.get(key) is None and cite.get(key) is not None:
                retained[key] = cite[key]
    retained["chemical_name"] = retained.get("chemical_name") or retained.get("english_name")
    retained["cas"] = retained.get("cas")
    retained["field"] = retained.get("field")
    retained["value"] = retained.get("value")
    retained["source_row_key"] = retained.get("source_row_key")
    retained["cell_id"] = retained.get("cell_id")
    retained["page_number"] = retained.get("page_number")
    retained["rank"] = retained.get("rank") if retained.get("rank") is not None else rank
    if retained.get("score") is None:
        retained["score"] = record.get("score")
    return retained


def retain_formula_record(
    record: dict[str, Any],
    *,
    citations: list[dict[str, Any]] | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    retained = dict(record)
    cite = (citations or [None])[0] or {}
    if isinstance(cite, dict):
        retained.setdefault("formula_id", cite.get("formula_id"))
        retained.setdefault("page_number", cite.get("page_number"))
    retained["formula_id"] = retained.get("formula_id")
    retained["page_number"] = retained.get("page_number")
    retained["expression"] = (
        retained.get("expression")
        or retained.get("original_expression")
        or retained.get("normalized_expression")
    )
    retained["content"] = retained.get("content") or retained.get("description")
    retained["rank"] = retained.get("rank") if retained.get("rank") is not None else rank
    if retained.get("score") is None:
        retained["score"] = record.get("score")
    return retained


def _unwrap_hybrid(agent_results: dict[str, Any]) -> dict[str, Any]:
    hybrid = agent_results.get("hybrid")
    if not isinstance(hybrid, dict):
        return {}
    inner = hybrid.get("agent_results")
    return inner if isinstance(inner, dict) else {}


def planned_agents_of(agent_results: dict[str, Any], response: QueryResponse | None) -> list[str]:
    planned = agent_results.get("planned_agents")
    if isinstance(planned, list) and planned:
        return [str(a) for a in planned]
    hybrid = _unwrap_hybrid(agent_results)
    plan = hybrid.get("execution_plan") if isinstance(hybrid.get("execution_plan"), dict) else {}
    if plan.get("agents"):
        return [str(a) for a in plan["agents"]]
    if response is not None and response.agents:
        return list(response.agents)
    found: list[str] = []
    for name in ("structured", "semantic", "formula"):
        if name in agent_results or name in hybrid:
            found.append(name)
    return found


def extract_structured_records(agent_results: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(agent_results.get("structured"), dict):
        blocks.append(agent_results["structured"])
    inner = _unwrap_hybrid(agent_results)
    if isinstance(inner.get("structured"), dict):
        blocks.append(inner["structured"])
    records: list[dict[str, Any]] = []
    for i, block in enumerate(blocks, start=1):
        data = block.get("data") if isinstance(block.get("data"), dict) else {}
        citations = block.get("citations") if isinstance(block.get("citations"), list) else []
        rec = retain_structured_record(data, citations=citations, rank=i)
        rec["success"] = block.get("success")
        rec["no_data_reason"] = block.get("no_data_reason")
        records.append(rec)
    return records


def extract_semantic_chunks(agent_results: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    if isinstance(agent_results.get("semantic"), dict):
        blocks.append(agent_results["semantic"])
    inner = _unwrap_hybrid(agent_results)
    if isinstance(inner.get("semantic"), dict):
        blocks.append(inner["semantic"])
    for block in blocks:
        for i, chunk in enumerate(block.get("chunks") or [], start=1):
            if isinstance(chunk, dict):
                chunks.append(retain_semantic_chunk(chunk, rank=i))
    return chunks


def extract_pre_rerank_candidates(agent_results: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    if isinstance(agent_results.get("semantic"), dict):
        blocks.append(agent_results["semantic"])
    inner = _unwrap_hybrid(agent_results)
    if isinstance(inner.get("semantic"), dict):
        blocks.append(inner["semantic"])
    for block in blocks:
        for i, item in enumerate(block.get("pre_rerank_candidates") or [], start=1):
            if not isinstance(item, dict):
                continue
            row = retain_semantic_chunk(item, rank=i)
            row["evidence_type"] = item.get("evidence_type") or item.get("source_type") or item.get("source")
            row["source_row_key"] = item.get("source_row_key")
            row["formula_id"] = item.get("formula_id")
            row["cas"] = item.get("cas")
            row["field"] = item.get("field")
            row["value"] = item.get("value")
            row["twa"] = item.get("twa")
            row["stel"] = item.get("stel")
            row["ceiling"] = item.get("ceiling")
            chunks.append(row)
    return chunks


def is_unified_item_relevant(item: dict[str, Any], gold: GoldsetQAItem) -> bool:
    et = str(item.get("evidence_type") or item.get("source_type") or "")
    if et == "structured":
        if is_structured_record_relevant({**item, "success": item.get("success", True)}, gold):
            return True
    if et == "formula" or item.get("formula_id"):
        if is_formula_record_relevant({**item, "success": item.get("success", True)}, gold):
            return True
    return is_semantic_chunk_relevant(item, gold)


def extract_formula_records(agent_results: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    if isinstance(agent_results.get("formula"), dict):
        blocks.append(agent_results["formula"])
    inner = _unwrap_hybrid(agent_results)
    if isinstance(inner.get("formula"), dict):
        blocks.append(inner["formula"])
    for i, block in enumerate(blocks, start=1):
        data = block.get("data") if isinstance(block.get("data"), dict) else {}
        citations = block.get("citations") if isinstance(block.get("citations"), list) else []
        rec = retain_formula_record(data, citations=citations, rank=i)
        rec["success"] = block.get("success")
        records.append(rec)
    return records


def ranked_ids_and_relevant(
    items: list[dict[str, Any]],
    *,
    relevant_flags: list[bool],
    id_prefix: str,
) -> tuple[list[str], set[str]]:
    retrieved: list[str] = []
    relevant: set[str] = set()
    for i, (item, flag) in enumerate(zip(items, relevant_flags, strict=False)):
        rid = str(
            item.get("chunk_id")
            or item.get("source_row_key")
            or item.get("record_id")
            or item.get("formula_id")
            or f"{id_prefix}:{i}"
        )
        if not rid:
            rid = f"{id_prefix}:{i}"
        if rid in retrieved:
            rid = f"{id_prefix}:{i}:{rid}"
        retrieved.append(rid)
        if flag:
            relevant.add(rid)
    return retrieved, relevant


def semantic_metrics(retrieved: list[str], relevant: set[str]) -> dict[str, float]:
    gold_span = {"gold_span"} if relevant else set()
    retrieved_hits = ["gold_span" if rid in relevant else rid for rid in retrieved]
    out: dict[str, float] = {}
    for k in SEMANTIC_RANK_KS:
        out[f"hit@{k}"] = hit_at_k(retrieved_hits, gold_span, k)
        out[f"recall@{k}"] = recall_at_k(retrieved_hits, gold_span, k) if gold_span else 0.0
        out[f"precision@{k}"] = precision_at_k(retrieved, relevant, k)
    out["mrr"] = mrr(retrieved, relevant)
    return out


def structured_metrics(retrieved: list[str], relevant: set[str]) -> dict[str, float]:
    top1 = retrieved[:1]
    return {
        "hit@1": hit_at_k(top1, relevant, 1) if top1 else 0.0,
        "recall@1": recall_at_k(top1, relevant, 1) if relevant else 0.0,
        "precision@1": precision_at_k(top1, relevant, 1),
        "mrr": mrr(top1, relevant) if top1 else 0.0,
    }


def formula_metrics(retrieved: list[str], relevant: set[str]) -> dict[str, float]:
    return structured_metrics(retrieved, relevant)


def empty_semantic_metrics() -> dict[str, float]:
    return semantic_metrics([], set())


@dataclass
class CaseEvalResult:
    gold_id: str
    question_type: str
    actual_intent: str | None
    actual_agents: list[str]
    metric_families: list[str]
    semantic: dict[str, float] | None = None
    structured: dict[str, float] | None = None
    formula: dict[str, float] | None = None
    semantic_chunks: list[dict[str, Any]] = field(default_factory=list)
    structured_records: list[dict[str, Any]] = field(default_factory=list)
    formula_records: list[dict[str, Any]] = field(default_factory=list)
    mixed_table: bool = False
    no_result: bool = False
    pre_rerank: dict[str, float] | None = None
    unified_final: dict[str, float] | None = None
    failure_category: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gold_id": self.gold_id,
            "question_type": self.question_type,
            "actual_intent": self.actual_intent,
            "actual_agents": self.actual_agents,
            "metric_families": self.metric_families,
            "semantic": self.semantic,
            "structured": self.structured,
            "formula": self.formula,
            "mixed_table": self.mixed_table,
            "no_result": self.no_result,
            "pre_rerank": self.pre_rerank,
            "unified_final": self.unified_final,
            "failure_category": self.failure_category,
            "diagnostics": self.diagnostics,
        }


class GoldsetQAEvaluator:
    """Score Goldset QA against live QueryOrchestrator results without changing routing."""

    def __init__(self, orchestrator: QueryOrchestrator | None = None) -> None:
        self.orchestrator = orchestrator

    def _run_production(self, query: str) -> tuple[QueryResponse, dict[str, Any]]:
        if self.orchestrator is None:
            raise RuntimeError("orchestrator is required unless agent_results are injected")
        captured: dict[str, Any] = {}
        orig = self.orchestrator.router.execute

        def _execute(classification, q, slots):
            result = orig(classification, q, slots)
            captured["agent_results"] = result
            return result

        self.orchestrator.router.execute = _execute  # type: ignore[method-assign]
        try:
            response = self.orchestrator.handle(query)
        finally:
            self.orchestrator.router.execute = orig
        return response, captured.get("agent_results") or {}

    def evaluate_case(
        self,
        gold: GoldsetQAItem,
        *,
        agent_results: dict[str, Any] | None = None,
        response: QueryResponse | None = None,
        ranked_semantic_chunks: list[dict[str, Any]] | None = None,
    ) -> CaseEvalResult:
        if agent_results is None:
            response, agent_results = self._run_production(gold.question)

        planned = planned_agents_of(agent_results, response)
        actual_intent = response.intent if response is not None else None

        structured_records = extract_structured_records(agent_results)
        formula_records = extract_formula_records(agent_results)
        if ranked_semantic_chunks is not None:
            semantic_chunks = [
                retain_semantic_chunk(c, rank=i) for i, c in enumerate(ranked_semantic_chunks, start=1)
            ]
        else:
            semantic_chunks = extract_semantic_chunks(agent_results)

        inner = _unwrap_hybrid(agent_results)
        score_structured = "structured" in agent_results or "structured" in inner or "structured" in planned
        score_semantic = (
            "semantic" in agent_results
            or "semantic" in inner
            or "semantic" in planned
            or ranked_semantic_chunks is not None
        )
        score_formula = "formula" in agent_results or "formula" in inner or "formula" in planned

        mixed_table = gold.question_type == "table" and score_structured and score_semantic

        families: list[str] = []
        structured_m = None
        semantic_m = None
        formula_m = None

        if score_structured:
            families.append("structured")
            flags = [is_structured_record_relevant(r, gold) for r in structured_records]
            retrieved, relevant = ranked_ids_and_relevant(
                structured_records, relevant_flags=flags, id_prefix="structured"
            )
            structured_m = structured_metrics(retrieved, relevant)

        if score_semantic:
            families.append("semantic")
            flags = [is_semantic_chunk_relevant(c, gold) for c in semantic_chunks]
            retrieved, relevant = ranked_ids_and_relevant(
                semantic_chunks, relevant_flags=flags, id_prefix="semantic"
            )
            semantic_m = semantic_metrics(retrieved, relevant)

        if score_formula:
            families.append("formula")
            flags = [is_formula_record_relevant(r, gold) for r in formula_records]
            retrieved, relevant = ranked_ids_and_relevant(
                formula_records, relevant_flags=flags, id_prefix="formula"
            )
            formula_m = formula_metrics(retrieved, relevant)

        no_result = (
            not any(r.get("success") for r in structured_records)
            and not semantic_chunks
            and not any(r.get("success") for r in formula_records)
        )
        if not families:
            families = ["semantic"]
            semantic_m = empty_semantic_metrics()
            no_result = True

        pre_items = extract_pre_rerank_candidates(agent_results)
        final_items: list[dict[str, Any]] = []
        for rec in structured_records:
            if rec.get("success"):
                final_items.append({**rec, "evidence_type": "structured"})
        final_items.extend(semantic_chunks)
        for rec in formula_records:
            if rec.get("success"):
                final_items.append({**rec, "evidence_type": "formula"})
        if not pre_items:
            pre_items = list(final_items)

        pre_flags = [is_unified_item_relevant(c, gold) for c in pre_items]
        pre_retrieved, pre_relevant = ranked_ids_and_relevant(
            pre_items, relevant_flags=pre_flags, id_prefix="pre"
        )
        pre_m = semantic_metrics(pre_retrieved, pre_relevant)

        final_flags = [is_unified_item_relevant(c, gold) for c in final_items]
        final_retrieved, final_relevant = ranked_ids_and_relevant(
            final_items, relevant_flags=final_flags, id_prefix="final"
        )
        unified_m = semantic_metrics(final_retrieved, final_relevant)

        failure_category = None
        if unified_m.get("hit@1", 0) < 1.0:
            structured_success = any(r.get("success") for r in structured_records)
            if structured_success and structured_m and structured_m.get("hit@1", 0) < 1.0:
                failure_category = "invalid/mismatched Gold"
            elif pre_m.get("hit@5", 0) < 1.0 and not structured_success and "structured" in families:
                failure_category = "structured data"
            elif pre_m.get("hit@5", 0) < 1.0:
                failure_category = "candidate generation"
            elif unified_m.get("hit@5", 0) < 1.0 and pre_m.get("hit@5", 0) >= 1.0:
                failure_category = "ranking"
            else:
                failure_category = "answer generation"

        return CaseEvalResult(
            gold_id=gold.id,
            question_type=gold.question_type,
            actual_intent=actual_intent,
            actual_agents=planned,
            metric_families=families,
            semantic=semantic_m,
            structured=structured_m,
            formula=formula_m,
            semantic_chunks=semantic_chunks,
            structured_records=structured_records,
            formula_records=formula_records,
            mixed_table=mixed_table,
            no_result=no_result,
            pre_rerank=pre_m,
            unified_final=unified_m,
            failure_category=failure_category,
            diagnostics={
                "actual_intent": actual_intent,
                "actual_agents": planned,
                "question_type": gold.question_type,
                "failure_category": failure_category,
            },
        )

    def evaluate_items(self, items: list[GoldsetQAItem]) -> dict[str, Any]:
        cases = [self.evaluate_case(item) for item in items]
        return summarize_case_results(cases)


def summarize_case_results(cases: list[CaseEvalResult]) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}
    all_semantic: list[dict[str, float]] = []
    all_structured: list[dict[str, float]] = []
    all_formula: list[dict[str, float]] = []
    all_pre: list[dict[str, float]] = []
    all_unified: list[dict[str, float]] = []
    failure_counts: dict[str, int] = {}

    for case in cases:
        bucket = by_type.setdefault(
            case.question_type,
            {"n": 0, "semantic": [], "structured": [], "formula": [], "mixed_table": 0},
        )
        bucket["n"] += 1
        if case.mixed_table:
            bucket["mixed_table"] += 1
        if case.semantic is not None:
            bucket["semantic"].append(case.semantic)
            all_semantic.append(case.semantic)
        if case.structured is not None:
            bucket["structured"].append(case.structured)
            all_structured.append(case.structured)
        if case.formula is not None:
            bucket["formula"].append(case.formula)
            all_formula.append(case.formula)
        if case.pre_rerank is not None:
            all_pre.append(case.pre_rerank)
        if case.unified_final is not None:
            all_unified.append(case.unified_final)
        if case.failure_category:
            failure_counts[case.failure_category] = failure_counts.get(case.failure_category, 0) + 1

    aggregated: dict[str, Any] = {}
    for qtype, bucket in by_type.items():
        entry: dict[str, Any] = {"n": bucket["n"], "mixed_table": bucket["mixed_table"]}
        if bucket["semantic"]:
            entry["semantic"] = aggregate_metrics(bucket["semantic"])
        if bucket["structured"]:
            entry["structured"] = aggregate_metrics(bucket["structured"])
        if bucket["formula"]:
            entry["formula"] = aggregate_metrics(bucket["formula"])
        aggregated[qtype] = entry

    report: dict[str, Any] = {
        "n_items": len(cases),
        "semantic": aggregate_metrics(all_semantic) if all_semantic else None,
        "structured": aggregate_metrics(all_structured) if all_structured else None,
        "formula": aggregate_metrics(all_formula) if all_formula else None,
        "pre_rerank": aggregate_metrics(all_pre) if all_pre else None,
        "unified_final": aggregate_metrics(all_unified) if all_unified else None,
        "failure_categories": failure_counts,
        "by_question_type": aggregated,
        "cases": [c.to_dict() for c in cases],
    }
    return report
