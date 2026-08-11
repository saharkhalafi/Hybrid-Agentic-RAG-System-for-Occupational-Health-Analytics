"""Deterministic builders for retrieval evaluation records."""

from __future__ import annotations

import hashlib
import itertools
from typing import Any, Callable, Iterator

import yaml

from config.settings import PROJECT_ROOT
from knowledge.calculation import evaluate_vibration_daily_exposure
from retrieval.eval_gold_loader import (
    FormulaRecord,
    GoldCorpus,
    OelRowRecord,
    SemanticChunkRecord,
    numeric_from_field,
)
from retrieval.eval_schema import (
    CitationRequirement,
    GroundTruth,
    MetadataRequirements,
    RelevanceItem,
    RelevanceLevel,
    RetrievalEvalRecord,
    SourceTraceability,
    FormulaGroundTruth,
    NumericGroundTruth,
)

TAXONOMY_PATH = PROJECT_ROOT / "data" / "intent_taxonomy" / "intent_taxonomy.yaml"
BUILDER_VERSION = "1.0.0"

TWA_TEMPLATES = [
    "حد TWA برای {name} چقدر است؟",
    "TWA {name} چنده؟",
    "{name} — حد TWA",
    "میانگین وزنی زمانی {name} چند {unit} است؟",
    "برای {name} TWA چقدره؟",
]
STEL_TEMPLATES = [
    "حد STEL برای {name} چقدر است؟",
    "STEL {name} چنده؟",
    "مواجهه ۱۵ دقیقه‌ای مجاز {name}",
    "{name} STEL",
]
CEILING_TEMPLATES = [
    "حد سقف (Ceiling) {name} چقدر است؟",
    "Ceiling {name}",
    "حد C برای {name}",
]
ALL_TEMPLATES = [
    "همه حدهای مجاز {name} را بگو",
    "TWA و STEL {name} چنده؟",
    "حدود مجاز مواجهه {name}",
]
CAS_TEMPLATES = [
    "برای CAS {cas} حد TWA چقدر است؟",
    "CAS {cas} — حد مجاز",
    "[{cas}] TWA",
]
MW_TEMPLATES = [
    "وزن مولکولی {name} چقدر است؟",
    "MW {name}",
]
PROVENANCE_TEMPLATES = [
    "منبع حد TWA {name} کدام صفحه است؟",
    "TWA {name} از کدام جدول آمده؟",
]

COLLOQUIAL_TWA = ["{name} چقدره؟", "حد {name}؟", "برای {name} حد مجاز داریم؟"]


class QueryCounter:
    def __init__(self, start: int = 1) -> None:
        self._n = start

    def next_id(self, prefix: str = "RET") -> str:
        qid = f"{prefix}-{self._n:06d}"
        self._n += 1
        return qid


def load_intent_index() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8"))
    return {item["intent_id"]: item for item in raw.get("intents", [])}


def _display_name(row: OelRowRecord) -> str:
    return row.english_name or row.persian_name or row.cas or "unknown"


def _structured_relevance(row: OelRowRecord, field: str) -> RelevanceItem:
    return RelevanceItem(
        source_type="structured",
        relevance=RelevanceLevel.REQUIRED,
        table="oel_chemical_limits",
        record_id=row.source_row_key,
        field=field,
        chemical=row.english_name,
        cas=row.cas,
        page_number=row.page_number,
        cell_id=(getattr(row, field.lower(), None) or {}).get("cell_id") if hasattr(row, field.lower()) else None,
        gold_artifact_path=row.gold_path,
    )


def _row_field(row: OelRowRecord, name: str) -> dict[str, Any] | None:
    return getattr(row, name.lower(), None)


def _make_structured_record(
    counter: QueryCounter,
    row: OelRowRecord,
    *,
    intent_id: str,
    intent_meta: dict[str, Any],
    query: str,
    field_name: str,
    field_data: dict[str, Any] | None,
    answer_type: str = "numeric",
    difficulty: str = "easy",
    category: str = "structured",
    tags: list[str] | None = None,
) -> RetrievalEvalRecord | None:
    if not field_data or (field_data.get("normalized_value") is None and field_data.get("accepted_value") is None):
        if field_name not in {"provenance"}:
            return None
    num = numeric_from_field(field_data) if field_data else None
    if num and answer_type == "numeric":
        num.source_table = "oel_chemical_limits"
        num.source_row_key = row.source_row_key
        num.page_number = row.page_number
        num.field = field_name

    rel = _structured_relevance(row, field_name)
    if field_data and field_data.get("cell_id"):
        rel.cell_id = field_data["cell_id"]
        rel.bbox = field_data.get("bbox")

    unit = row.unit or (field_data or {}).get("unit") or "ppm"
    expected_answer = None
    if num and num.normalized_value is not None:
        expected_answer = f"{num.normalized_value} {unit}".strip()

    return RetrievalEvalRecord(
        query_id=counter.next_id(),
        query=query,
        standalone_query=query,
        resolved_query=query,
        intent=intent_id,
        domain=intent_meta.get("domain", "STRUCTURED"),
        category=category,
        difficulty=difficulty,  # type: ignore[arg-type]
        expected_agents=[intent_meta.get("agent", "structured")],
        expected_route=intent_meta.get("agent", "structured"),
        slots={"chemical_name": _display_name(row), "cas": row.cas, "oel_type": field_name.upper()},
        expected_slots={"chemical_name": _display_name(row), "oel_type": field_name.upper()},
        expected_answer=expected_answer,
        answer_type=answer_type,  # type: ignore[arg-type]
        ground_truth=GroundTruth(
            relevance=[rel],
            required_sources=["chemical_registry", "oel_chemical_limits"],
            required_facts=[f"{field_name.upper()} for {_display_name(row)}"],
            numeric_values=[num] if num else [],
        ),
        citations=[
            CitationRequirement(
                page_number=row.page_number,
                source_row_key=row.source_row_key,
                cell_id=field_data.get("cell_id") if field_data else None,
                source_type="structured",
                authority="postgresql",
            )
        ],
        metadata_requirements=MetadataRequirements(
            document_id=row.document_id,
            page_number=row.page_number,
            source_type="structured",
            table_name="oel_chemical_limits",
            cell_id=field_data.get("cell_id") if field_data else None,
            bbox=field_data.get("bbox") if field_data else None,
        ),
        source_traceability=SourceTraceability(
            gold_artifact_path=row.gold_path,
            evidence_path=row.gold_path,
            builder_version=BUILDER_VERSION,
        ),
        tags=tags or ["structured", field_name.lower()],
    )


def build_structured_cases(corpus: GoldCorpus, intents: dict[str, dict[str, Any]], counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    twa_intent = "STRUCTURED.OEL.TWA_LOOKUP"
    stel_intent = "STRUCTURED.OEL.STEL_LOOKUP"
    ceil_intent = "STRUCTURED.OEL.CEILING_LOOKUP"
    all_intent = "STRUCTURED.OEL.ALL_LIMITS_LOOKUP"
    cas_intent = "STRUCTURED.OEL.BY_CAS"
    mw_intent = "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT"
    prov_intent = "STRUCTURED.OEL.PROVENANCE"

    for i, row in enumerate(corpus.oel_rows):
        name = _display_name(row)
        unit = row.unit or "ppm"

        if row.twa:
            for tmpl in TWA_TEMPLATES:
                q = tmpl.format(name=name, unit=unit, cas=row.cas or "")
                rec = _make_structured_record(
                    counter, row, intent_id=twa_intent, intent_meta=intents[twa_intent],
                    query=q, field_name="TWA", field_data=row.twa,
                    difficulty="easy" if i % 3 else "medium",
                )
                if rec:
                    records.append(rec)
            if i % 7 == 0:
                q = COLLOQUIAL_TWA[i % len(COLLOQUIAL_TWA)].format(name=name)
                rec = _make_structured_record(
                    counter, row, intent_id=twa_intent, intent_meta=intents[twa_intent],
                    query=q, field_name="TWA", field_data=row.twa, difficulty="medium",
                    tags=["structured", "twa", "colloquial"],
                )
                if rec:
                    records.append(rec)

        if row.stel:
            tmpl = STEL_TEMPLATES[i % len(STEL_TEMPLATES)]
            q = tmpl.format(name=name)
            rec = _make_structured_record(
                counter, row, intent_id=stel_intent, intent_meta=intents[stel_intent],
                query=q, field_name="STEL", field_data=row.stel,
            )
            if rec:
                records.append(rec)

        if row.ceiling:
            tmpl = CEILING_TEMPLATES[i % len(CEILING_TEMPLATES)]
            q = tmpl.format(name=name)
            rec = _make_structured_record(
                counter, row, intent_id=ceil_intent, intent_meta=intents[ceil_intent],
                query=q, field_name="CEILING", field_data=row.ceiling,
            )
            if rec:
                records.append(rec)

        if row.twa and (row.stel or row.ceiling) and i % 2 == 0:
            q = ALL_TEMPLATES[i % len(ALL_TEMPLATES)].format(name=name)
            rec = _make_structured_record(
                counter, row, intent_id=all_intent, intent_meta=intents[all_intent],
                query=q, field_name="TWA", field_data=row.twa, answer_type="multi",
            )
            if rec:
                nums = []
                for fn, fd in [("TWA", row.twa), ("STEL", row.stel), ("CEILING", row.ceiling)]:
                    if fd:
                        n = numeric_from_field(fd)
                        if n:
                            n.source_table = "oel_chemical_limits"
                            n.source_row_key = row.source_row_key
                            n.page_number = row.page_number
                            n.field = fn
                            nums.append(n)
                rec.ground_truth.numeric_values = nums
                records.append(rec)

        if row.cas and row.twa and i % 3 == 0:
            q = CAS_TEMPLATES[i % len(CAS_TEMPLATES)].format(cas=row.cas, name=name)
            rec = _make_structured_record(
                counter, row, intent_id=cas_intent, intent_meta=intents[cas_intent],
                query=q, field_name="TWA", field_data=row.twa,
                tags=["structured", "cas"],
            )
            if rec:
                rec.slots = {"cas": row.cas, "oel_type": "TWA"}
                records.append(rec)

        if row.molecular_weight and i % 5 == 0:
            q = MW_TEMPLATES[i % len(MW_TEMPLATES)].format(name=name)
            rec = _make_structured_record(
                counter, row, intent_id=mw_intent, intent_meta=intents[mw_intent],
                query=q, field_name="MW", field_data=row.molecular_weight,
            )
            if rec:
                records.append(rec)

        if row.twa and i % 11 == 0:
            q = PROVENANCE_TEMPLATES[i % len(PROVENANCE_TEMPLATES)].format(name=name)
            rec = _make_structured_record(
                counter, row, intent_id=prov_intent, intent_meta=intents[prov_intent],
                query=q, field_name="TWA", field_data=row.twa, answer_type="text",
            )
            if rec:
                rec.expected_answer = f"صفحه {row.page_number} — {row.gold_path}"
                records.append(rec)

    return records


def _semantic_query_templates(chunk: SemanticChunkRecord) -> list[tuple[str, str, str]]:
    """Return (query, intent_id, difficulty) from chunk content."""
    title = chunk.section_title or (chunk.section_path[-1] if chunk.section_path else None)
    queries: list[tuple[str, str, str]] = []
    page = chunk.page_number

    if title:
        queries.append((f"{title} به چه معناست؟", "SEMANTIC.DEFINITION.OEL", "easy"))
        queries.append((f"تعریف {title}", "SEMANTIC.DEFINITION.OEL", "medium"))

    text = chunk.text.strip()
    if "TWA" in text or "میانگین وزنی" in text:
        queries.append(("TWA یعنی چی؟", "SEMANTIC.DEFINITION.TWA", "easy"))
    if "STEL" in text or "کوتاه مدت" in text or "۱۵ دقیقه" in text:
        queries.append(("STEL و مواجهه کوتاه‌مدت چگونه تعریف شده؟", "SEMANTIC.DEFINITION.STEL", "medium"))
    if "سقف" in text or "Ceiling" in text:
        queries.append(("حد سقف مواجهه (Ceiling) یعنی چه؟", "SEMANTIC.DEFINITION.CEILING", "medium"))
    if "BEI" in text or "بیولوژ" in text or "زیستی" in text:
        queries.append(("شاخص زیستی مواجهه (BEI) چیست؟", "SEMANTIC.DEFINITION.BEI", "medium"))
    if "صدا" in text or "LAeq" in text:
        queries.append(("حد مجاز مواجهه با صدا چگونه تعریف شده؟", "SEMANTIC.DEFINITION.NOISE", "medium"))
    if "ارتعاش" in text or "ahv" in text.lower():
        queries.append(("A(8) در ارتعاش یعنی چی؟", "SEMANTIC.DEFINITION.VIBRATION", "medium"))
    if "توصیه" in text or "باید" in text:
        queries.append((f"توصیه‌های بخش {title or page} چیست؟", "SEMANTIC.REGULATION.RECOMMENDATION", "hard"))
    if "ممنوع" in text or "نباید" in text or "تشخیص" in text:
        queries.append(("آیا می‌توان از BEI برای تشخیص بیماری استفاده کرد؟", "SEMANTIC.REGULATION.PROHIBITION", "hard"))

    if not queries and len(text) > 40:
        snippet = text[:60].rsplit(" ", 1)[0]
        queries.append((f"{snippet}...", "SEMANTIC.EXPLANATION.CONCEPT", "hard"))

    if title and page < 50:
        queries.append((f"خلاصه بخش {title}", "SEMANTIC.CONTEXT.SECTION", "medium"))

    # dedupe by query text
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for q, iid, diff in queries:
        if q not in seen:
            seen.add(q)
            out.append((q, iid, diff))
    return out[:3]


def build_semantic_cases(corpus: GoldCorpus, intents: dict[str, dict[str, Any]], counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for chunk in corpus.semantic_chunks:
        templates = _semantic_query_templates(chunk)
        if not templates:
            continue
        for query, intent_id, difficulty in templates:
            if intent_id not in intents:
                continue
            meta = intents[intent_id]
            rel_required = RelevanceItem(
                source_type="semantic_text",
                relevance=RelevanceLevel.REQUIRED,
                chunk_id=chunk.chunk_id,
                page_number=chunk.page_number,
                document_id=chunk.document_id,
                gold_artifact_path=chunk.gold_path,
            )
            rel_support = RelevanceItem(
                source_type="semantic_text",
                relevance=RelevanceLevel.SUPPORTING,
                chunk_id=chunk.chunk_id,
                page_number=chunk.page_number,
            )
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=query,
                    standalone_query=query,
                    resolved_query=query,
                    intent=intent_id,
                    domain=meta.get("domain", "SEMANTIC"),
                    category="semantic",
                    difficulty=difficulty,  # type: ignore[arg-type]
                    expected_agents=["semantic"],
                    expected_route="semantic",
                    expected_answer=chunk.text[:300],
                    answer_type="text",
                    ground_truth=GroundTruth(
                        relevance=[rel_required, rel_support],
                        required_sources=["document_chunks"],
                        required_facts=[chunk.section_title or chunk.chunk_id],
                    ),
                    citations=[
                        CitationRequirement(
                            page_number=chunk.page_number,
                            printed_page_number=chunk.printed_page_number,
                            chunk_id=chunk.chunk_id,
                            source_type="semantic_text",
                            authority="semantic",
                        )
                    ],
                    metadata_requirements=MetadataRequirements(
                        document_id=chunk.document_id,
                        page_number=chunk.page_number,
                        printed_page_number=chunk.printed_page_number,
                        section_title=chunk.section_title,
                        chunk_id=chunk.chunk_id,
                        source_type="semantic_text",
                        language="fa",
                        bbox=chunk.bbox,
                    ),
                    source_traceability=SourceTraceability(
                        gold_artifact_path=chunk.gold_path,
                        builder_version=BUILDER_VERSION,
                    ),
                    tags=["semantic", f"page_{chunk.page_number}"],
                )
            )
    return records


def build_formula_cases(corpus: GoldCorpus, intents: dict[str, dict[str, Any]], counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    lookup_intent = "FORMULA.LOOKUP.BY_ID"
    var_intent = "FORMULA.VARIABLE.EXPLANATION"
    calc_intent = "FORMULA.CALCULATION.VIBRATION_AHV"
    unsup_intent = "FORMULA.CALCULATION.UNSUPPORTED"

    for formula in corpus.formulas:
        # lookup
        q_lookup = f"فرمول {formula.formula_id} چیست؟"
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=q_lookup,
                intent=lookup_intent,
                domain="FORMULA",
                category="formula",
                difficulty="easy",
                expected_agents=["formula"],
                expected_answer=formula.normalized_expression,
                answer_type="formula",
                ground_truth=GroundTruth(
                    relevance=[
                        RelevanceItem(
                            source_type="formula",
                            relevance=RelevanceLevel.REQUIRED,
                            formula_id=formula.formula_id,
                            page_number=formula.page_number,
                            gold_artifact_path=formula.gold_path,
                        )
                    ],
                    required_sources=["formulas"],
                    formula=FormulaGroundTruth(
                        formula_id=formula.formula_id,
                        normalized_expression=formula.normalized_expression,
                    ),
                ),
                citations=[CitationRequirement(formula_id=formula.formula_id, page_number=formula.page_number, authority="postgresql")],
                metadata_requirements=MetadataRequirements(page_number=formula.page_number, source_type="formula", bbox=formula.bbox),
                source_traceability=SourceTraceability(gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION),
                tags=["formula", "lookup"],
            )
        )

        # variables
        if formula.variables:
            q_var = f"متغیرهای فرمول {formula.formula_id} کدامند؟"
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=q_var,
                    intent=var_intent,
                    domain="FORMULA",
                    category="formula",
                    expected_agents=["formula"],
                    slots={"formula_id": formula.formula_id},
                    ground_truth=GroundTruth(
                        relevance=[RelevanceItem(source_type="formula", relevance=RelevanceLevel.REQUIRED, formula_id=formula.formula_id)],
                        required_sources=["formulas"],
                    ),
                    tags=["formula", "variables"],
                    source_traceability=SourceTraceability(gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION),
                )
            )

        # vibration ahv calculation (only formula_240_01)
        if "ahv" in formula.normalized_expression.lower() and formula.formula_id == "formula_240_01":
            inputs = {"ahw_1": 1.5, "t_1": 3.0, "ahw_2": 2.5, "t_2": 5.0}
            result = evaluate_vibration_daily_exposure(
                formula_id=formula.formula_id,
                ahw_values=[1.5, 2.5],
                t_values=[3.0, 5.0],
            )
            q_calc = f"با ahw1=1.5,t1=3,ahw2=2.5,t2=5 مقدار ahv را محاسبه کن ({formula.formula_id})"
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=q_calc,
                    intent=calc_intent,
                    domain="FORMULA",
                    category="formula",
                    difficulty="medium",
                    expected_agents=["formula"],
                    expected_answer=str(round(result.result, 6)),
                    answer_type="numeric",
                    ground_truth=GroundTruth(
                        relevance=[RelevanceItem(source_type="formula", relevance=RelevanceLevel.REQUIRED, formula_id=formula.formula_id)],
                        required_sources=["formulas"],
                        numeric_values=[],
                        formula=FormulaGroundTruth(
                            formula_id=formula.formula_id,
                            normalized_expression=formula.normalized_expression,
                            calculation_inputs=inputs,
                            calculation_output=round(result.result, 6),
                            valid=result.valid,
                        ),
                    ),
                    tags=["formula", "calculation"],
                    source_traceability=SourceTraceability(gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION),
                )
            )
            # missing parameter → clarify
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=f"فرمول {formula.formula_id} را محاسبه کن",
                    intent="CLARIFY.MISSING_EXPOSURE_VALUE",
                    domain="CLARIFY",
                    category="clarification",
                    requires_clarification=True,
                    expected_agents=["clarify"],
                    answer_type="clarification",
                    tags=["formula", "clarify"],
                    source_traceability=SourceTraceability(gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION),
                )
            )
        else:
            q_unsup = f"فرمول {formula.formula_id} را با مقادیر نمونه محاسبه کن"
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=q_unsup,
                    intent=unsup_intent,
                    domain="FORMULA",
                    category="formula",
                    expected_agents=["guardrail"],
                    answer_type="refusal",
                    ground_truth=GroundTruth(
                        relevance=[RelevanceItem(source_type="formula", relevance=RelevanceLevel.REQUIRED, formula_id=formula.formula_id)],
                    ),
                    tags=["formula", "unsupported"],
                    source_traceability=SourceTraceability(gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION),
                )
            )

    return records


def build_hybrid_cases(
    structured: list[RetrievalEvalRecord],
    semantic: list[RetrievalEvalRecord],
    intents: dict[str, dict[str, Any]],
    counter: QueryCounter,
) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    struct_by_chem: dict[str, RetrievalEvalRecord] = {}
    for r in structured:
        chem = r.slots.get("chemical_name")
        if chem and r.intent == "STRUCTURED.OEL.TWA_LOOKUP" and chem not in struct_by_chem:
            struct_by_chem[chem] = r

    for chem, base in list(struct_by_chem.items())[:40]:
        q = f"حد TWA {chem} چقدر است و TWA یعنی چی؟"
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=q,
                intent="HYBRID.LOOKUP_AND_EXPLAIN",
                domain="HYBRID",
                category="hybrid",
                expected_agents=["structured", "semantic"],
                slots={"chemical_name": chem, "oel_type": "TWA"},
                ground_truth=GroundTruth(
                    relevance=base.ground_truth.relevance + [
                        RelevanceItem(source_type="semantic_text", relevance=RelevanceLevel.SUPPORTING, page_number=25)
                    ],
                    required_sources=["chemical_registry", "oel_chemical_limits", "document_chunks"],
                    numeric_values=base.ground_truth.numeric_values,
                ),
                tags=["hybrid", "lookup_explain"],
                source_traceability=base.source_traceability,
            )
        )
        q2 = f"مواجهه ۲ ppm {chem} — حد TWA و آیا بیشتره؟"
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=q2,
                intent="HYBRID.LOOKUP_COMPARE_EXPLAIN",
                domain="HYBRID",
                category="hybrid",
                difficulty="hard",
                expected_agents=["structured", "formula", "semantic"],
                slots={"chemical_name": chem, "concentration": 2, "unit": "ppm"},
                tags=["hybrid", "compare"],
                source_traceability=base.source_traceability,
            )
        )
    return records


def build_conversational_chains(
    corpus: GoldCorpus,
    intents: dict[str, dict[str, Any]],
    counter: QueryCounter,
) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    candidates = [r for r in corpus.oel_rows if r.twa and r.english_name][:60]
    session_num = 1

    for row in candidates:
        name = _display_name(row)
        sid = f"SES-{session_num:04d}"
        session_num += 1

        turn1_id = counter.next_id()
        q1 = f"حد مجاز {name} چقدره؟"
        turn1 = RetrievalEvalRecord(
            query_id=turn1_id,
            session_id=sid,
            turn_id=1,
            query=q1,
            standalone_query=q1,
            resolved_query=q1,
            intent="STRUCTURED.OEL.ALL_LIMITS_LOOKUP",
            domain="STRUCTURED",
            category="conversational",
            expected_agents=["structured"],
            slots={"chemical_name": name},
            expected_context={"chemical_name": name},
            tags=["conversation", "turn1"],
            source_traceability=SourceTraceability(gold_artifact_path=row.gold_path, builder_version=BUILDER_VERSION),
        )
        records.append(turn1)

        turn2_id = counter.next_id()
        q2 = "STEL نداره؟"
        turn2 = RetrievalEvalRecord(
            query_id=turn2_id,
            session_id=sid,
            turn_id=2,
            previous_turn_ids=[turn1_id],
            query=q2,
            standalone_query=q2,
            resolved_query=f"آیا {name} STEL دارد؟",
            intent="STRUCTURED.OEL.STEL_LOOKUP",
            domain="STRUCTURED",
            category="conversational",
            requires_context=True,
            requires_session_context=True,
            expected_agents=["structured"],
            expected_context={"chemical_name": name, "cas": row.cas},
            slots={"chemical_name": name, "oel_type": "STEL"},
            tags=["conversation", "turn2", "followup"],
            source_traceability=SourceTraceability(gold_artifact_path=row.gold_path, builder_version=BUILDER_VERSION),
        )
        records.append(turn2)

        if row.cas and session_num % 2 == 0:
            turn3_id = counter.next_id()
            q3 = "CASش چیه؟"
            records.append(
                RetrievalEvalRecord(
                    query_id=turn3_id,
                    session_id=sid,
                    turn_id=3,
                    previous_turn_ids=[turn1_id, turn2_id],
                    query=q3,
                    standalone_query=q3,
                    resolved_query=f"CAS {name} چیست؟",
                    intent="STRUCTURED.CHEMICAL.BY_NAME",
                    domain="STRUCTURED",
                    category="conversational",
                    requires_context=True,
                    requires_session_context=True,
                    expected_agents=["structured"],
                    expected_context={"chemical_name": name},
                    expected_answer=row.cas,
                    slots={"chemical_name": name},
                    tags=["conversation", "turn3", "cas_followup"],
                    source_traceability=SourceTraceability(gold_artifact_path=row.gold_path, builder_version=BUILDER_VERSION),
                )
            )

    return records


def build_clarify_guardrail_negative(intents: dict[str, dict[str, Any]], counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    clarify_specs = [
        ("CLARIFY.MISSING_CHEMICAL", "حدش چنده؟", {"missing_slots": ["chemical_name"]}),
        ("CLARIFY.MISSING_LIMIT_TYPE", "برای بنزن چنده؟", {"chemical_name": "Benzene", "missing_slots": ["oel_type"]}),
        ("CLARIFY.MISSING_EXPOSURE_VALUE", "آیا از حد مجاز بیشتره؟", {"missing_slots": ["concentration"]}),
        ("CLARIFY.MISSING_DURATION", "اگر ۱۰ ساعت باشه چی؟", {"missing_slots": ["duration", "chemical_name"]}),
    ]
    for intent_id, query, ctx in clarify_specs:
        meta = intents[intent_id]
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=query,
                intent=intent_id,
                domain=meta.get("domain", "CLARIFY"),
                category="clarification",
                requires_clarification=True,
                expected_agents=["clarify"],
                answer_type="clarification",
                expected_context=ctx,
                tags=["clarify"],
            )
        )

    guard_specs = [
        ("GUARDRAIL.NO_DATA", "حد مجاز ماده UnknownChem-9999 چقدر است؟"),
        ("GUARDRAIL.PROFESSIONAL_JUDGMENT", "آیا باید این کارگر را از کار اخراج کنم؟"),
        ("GUARDRAIL.UNSAFE_EXTRAPOLATION", "برای ۳۰ ساعت مواجهه روزانه حد مجاز را بگو"),
        ("GUARDRAIL.INVALID_INPUT", "CAS 12-34-5 معتبر است؟"),
        ("GUARDRAIL.CONFLICTING_DATA", "چرا دو مقدار مختلف برای TWA بنزن دارید؟"),
    ]
    for intent_id, query in guard_specs:
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=query,
                intent=intent_id,
                domain="GUARDRAIL",
                category="negative",
                difficulty="adversarial",
                expected_agents=["guardrail"],
                answer_type="refusal",
                tags=["guardrail", "negative"],
            )
        )

    adversarial = [
        ("SEMANTIC.DEFINITION.TWA", "تعریف TWA", "صفحه اشتباه — باید chunk صفحه 25 نه 221"),
        ("STRUCTURED.OEL.TWA_LOOKUP", "حد TWA بنزن", "lexical overlap with benzene vs benzoic"),
    ]
    for intent_id, query, note in adversarial:
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=query,
                intent=intent_id,
                domain=intents[intent_id].get("domain", ""),
                category="adversarial",
                difficulty="adversarial",
                expected_agents=[intents[intent_id].get("agent", "semantic")],
                tags=["adversarial", note.split("—")[0].strip()],
            )
        )

    return records


def build_formula_extended(corpus: GoldCorpus, intents: dict[str, dict[str, Any]], counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for formula in corpus.formulas:
        page = formula.page_number
        variants = [
            (f"فرمول صفحه {page} رابطه ۲ چیست؟", "FORMULA.LOOKUP.BY_ID"),
            (f"رابطه محاسبه ارتعاش در صفحه {page}", "FORMULA.LOOKUP.BY_DOMAIN"),
            (f"برای محاسبه ahv چه پارامترهایی لازم است؟ ({formula.formula_id})", "FORMULA.VARIABLE.EXPLANATION"),
            (f"متغیر T در {formula.formula_id} یعنی چی؟", "FORMULA.VARIABLE.EXPLANATION"),
            (f"متغیر ahw در {formula.formula_id} چه معنایی دارد؟", "FORMULA.VARIABLE.EXPLANATION"),
            (f"اهمیت فرمول {formula.formula_id} در ارزیابی مواجهه", "FORMULA.INTERPRETATION"),
        ]
        for query, intent_id in variants:
            if intent_id not in intents:
                continue
            agents = intents[intent_id].get("hybrid_agents") or [intents[intent_id].get("agent", "formula")]
            records.append(
                RetrievalEvalRecord(
                    query_id=counter.next_id(),
                    query=query,
                    intent=intent_id,
                    domain=intents[intent_id].get("domain", "FORMULA"),
                    category="formula",
                    expected_agents=agents if intent_id == "FORMULA.INTERPRETATION" else ["formula"],
                    ground_truth=GroundTruth(
                        relevance=[
                            RelevanceItem(
                                source_type="formula",
                                relevance=RelevanceLevel.REQUIRED,
                                formula_id=formula.formula_id,
                                page_number=page,
                            )
                        ],
                        required_sources=["formulas"],
                    ),
                    tags=["formula", "extended"],
                    source_traceability=SourceTraceability(
                        gold_artifact_path=formula.gold_path, builder_version=BUILDER_VERSION
                    ),
                )
            )
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=f"ahv را با ahw1=-1 و t1=0 محاسبه کن ({formula.formula_id})",
                intent="GUARDRAIL.INVALID_INPUT",
                domain="GUARDRAIL",
                category="negative",
                difficulty="adversarial",
                expected_agents=["guardrail"],
                answer_type="refusal",
                tags=["formula", "invalid_input"],
            )
        )
    return records


def build_formula_followup_chains(corpus: GoldCorpus, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    ahv = next((f for f in corpus.formulas if f.formula_id == "formula_240_01"), None)
    if not ahv:
        return records
    sid = "SES-FORM-0001"
    t1 = counter.next_id()
    records.append(
        RetrievalEvalRecord(
            query_id=t1,
            session_id=sid,
            turn_id=1,
            query="برای محاسبه مواجهه ارتعاش چه چیزهایی لازم است؟",
            resolved_query="پارامترهای فرمول ahv",
            intent="FORMULA.VARIABLE.EXPLANATION",
            domain="FORMULA",
            category="conversational",
            expected_agents=["formula"],
            tags=["formula", "conversation"],
            source_traceability=SourceTraceability(gold_artifact_path=ahv.gold_path, builder_version=BUILDER_VERSION),
        )
    )
    t2 = counter.next_id()
    records.append(
        RetrievalEvalRecord(
            query_id=t2,
            session_id=sid,
            turn_id=2,
            previous_turn_ids=[t1],
            query="مقدار ahw1 من 2.5 و t1 برابر 4 است.",
            resolved_query="ورودی‌های فرمول ahv: ahw1=2.5, t1=4",
            intent="FORMULA.CALCULATION.VIBRATION_AHV",
            domain="FORMULA",
            category="conversational",
            requires_context=True,
            requires_session_context=True,
            expected_agents=["formula"],
            tags=["formula", "conversation"],
        )
    )
    t3 = counter.next_id()
    records.append(
        RetrievalEvalRecord(
            query_id=t3,
            session_id=sid,
            turn_id=3,
            previous_turn_ids=[t1, t2],
            query="پس وضعیتش چطوره؟",
            resolved_query="تفسیر نتیجه ahv با ورودی‌های قبلی",
            intent="FORMULA.INTERPRETATION",
            domain="FORMULA",
            category="conversational",
            requires_context=True,
            requires_session_context=True,
            expected_agents=["formula", "semantic"],
            tags=["formula", "conversation"],
        )
    )
    return records


def build_adversarial_extended(corpus: GoldCorpus, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    names = sorted({r.english_name for r in corpus.oel_rows if r.english_name})
    pairs = [(names[i], names[i + 1]) for i in range(min(40, len(names) - 1))]
    for a, b in pairs:
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=f"حد TWA {a} — نه {b}",
                intent="STRUCTURED.OEL.TWA_LOOKUP",
                domain="STRUCTURED",
                category="adversarial",
                difficulty="adversarial",
                expected_agents=["structured"],
                slots={"chemical_name": a},
                tags=["adversarial", "similar_name"],
            )
        )
    for i in range(30):
        records.append(
            RetrievalEvalRecord(
                query_id=counter.next_id(),
                query=f"حد مجاز ماده NonExistent-{1000 + i} چقدر است؟",
                intent="GUARDRAIL.NO_DATA",
                domain="GUARDRAIL",
                category="negative",
                difficulty="adversarial",
                expected_agents=["guardrail"],
                answer_type="refusal",
                tags=["negative", "unknown_chemical"],
            )
        )
    return records


def dedupe_records(records: list[RetrievalEvalRecord]) -> list[RetrievalEvalRecord]:
    seen_query: set[str] = set()
    seen_key: set[str] = set()
    out: list[RetrievalEvalRecord] = []
    for r in records:
        key = hashlib.sha256(f"{r.query}|{r.intent}|{r.session_id}|{r.turn_id}".encode()).hexdigest()
        if key in seen_key:
            continue
        # allow same query for different sessions; dedupe identical query+intent otherwise
        qkey = f"{r.query}|{r.intent}|{r.session_id or ''}"
        if qkey in seen_query and not r.session_id:
            continue
        seen_key.add(key)
        seen_query.add(qkey)
        out.append(r)
    return out


def build_all_records(corpus: GoldCorpus | None = None) -> list[RetrievalEvalRecord]:
    corpus = corpus or __import__("retrieval.eval_gold_loader", fromlist=["load_gold_corpus"]).load_gold_corpus()
    intents = load_intent_index()
    counter = QueryCounter()

    structured = build_structured_cases(corpus, intents, counter)
    semantic = build_semantic_cases(corpus, intents, counter)
    formula = build_formula_cases(corpus, intents, counter)
    formula_ext = build_formula_extended(corpus, intents, counter)
    formula_conv = build_formula_followup_chains(corpus, counter)
    hybrid = build_hybrid_cases(structured, semantic, intents, counter)
    conversational = build_conversational_chains(corpus, intents, counter)
    clarify = build_clarify_guardrail_negative(intents, counter)
    adversarial = build_adversarial_extended(corpus, counter)

    all_records = dedupe_records(
        structured + semantic + formula + formula_ext + formula_conv + hybrid + conversational + clarify + adversarial
    )
    return all_records
