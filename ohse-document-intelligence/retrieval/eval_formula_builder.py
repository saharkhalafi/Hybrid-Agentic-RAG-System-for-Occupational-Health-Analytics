"""Phase C.2 — Grounded formula evaluation dataset builder (300+ queries).

All numeric ground truth comes from the Formula Registry + deterministic engine.
"""

from __future__ import annotations

import itertools
import random
from typing import Any, Iterator

from knowledge.calculation import evaluate_formula_by_id, evaluate_vibration_daily_exposure
from retrieval.eval_builders import BUILDER_VERSION, QueryCounter, load_intent_index
from retrieval.eval_gold_loader import FormulaRecord, GoldCorpus
from retrieval.eval_schema import (
    CitationRequirement,
    FormulaGroundTruth,
    GroundTruth,
    MetadataRequirements,
    RelevanceItem,
    RelevanceLevel,
    RetrievalEvalRecord,
    ReviewStatus,
    SourceTraceability,
)

BUILDER_VERSION_C2 = "2.0.0-formula"
TARGET_MIN = 300

# Deterministic parameter sets for ahv calculation (formula_240_01)
AHV_PARAM_SETS: list[tuple[list[float], list[float]]] = [
    ([1.5], [3.0]),
    ([2.0], [4.0]),
    ([1.5, 2.5], [3.0, 5.0]),
    ([2.0, 3.0], [2.0, 6.0]),
    ([0.5, 1.0, 1.5], [2.0, 3.0, 4.0]),
    ([3.5, 4.0], [1.5, 2.5]),
    ([1.2, 2.8, 3.1], [4.0, 2.0, 2.0]),
    ([0.8, 1.1], [8.0, 8.0]),
]

CALC_TEMPLATES_FA = [
    "با ahw1={ahw1}, t1={t1} مقدار ahv را محاسبه کن",
    "ahv را با ahw_1={ahw1} و t_1={t1} حساب کن",
    "محاسبه ahv: ahw1={ahw1}, t1={t1}",
    "اگر ahw1 برابر {ahw1} و t1 برابر {t1} باشد ahv چقدر می‌شود؟",
    "برای ahw1={ahw1} و t1={t1} ahv را به دست بیار",
]

CALC_TEMPLATES_FA_MULTI = [
    "با ahw1={ahw1}, t1={t1}, ahw2={ahw2}, t2={t2} ahv را محاسبه کن",
    "ahv = ? ahw1={ahw1} t1={t1} ahw2={ahw2} t2={t2}",
    "محاسبه مواجهه: ahw1={ahw1}, t1={t1}, ahw2={ahw2}, t2={t2}",
    "با پارامترهای ahw1={ahw1}, t1={t1}, ahw2={ahw2}, t2={t2} ahv چنده؟",
]

CALC_TEMPLATES_EN = [
    "calculate ahv with ahw1={ahw1}, t1={t1}",
    "ahv for ahw_1={ahw1}, t_1={t1}",
    "compute daily vibration exposure ahw1={ahw1} t1={t1}",
]

CALC_TEMPLATES_MIXED = [
    "ahv را با ahw1={ahw1} و time t1={t1} محاسبه کن",
    "calculate ahv: ahw1={ahw1}, زمان t1={t1}",
]

LOOKUP_TEMPLATES = [
    "فرمول {fid} چیست؟",
    "formula {fid} چیه؟",
    "رابطه {fid} را بگو",
    "تعریف فرمول {fid}",
    "محتوای {fid} در رجیstry",
    "فرمول شماره {ref} صفحه {page} چیست؟",
]

VAR_TEMPLATES = [
    "متغیرهای فرمول {fid} کدامند؟",
    "پارامترهای {fid} چیه؟",
    "برای محاسبه {fid} چه ورودی‌هایی لازم است؟",
    "variables of {fid}",
    "ورودی‌های لازم برای ahv",
    "T در {fid} یعنی چی؟",
    "ahw در {fid} چه معنایی دارد؟",
]

DOMAIN_TEMPLATES = [
    "فرمول محاسبه مواجهه ارتعاش چیست؟",
    "رابطه vibration exposure در صفحه {page}",
    "فرمول exposure_calculation صفحه {page}",
    "رابطه {ref} برای ارتعاش",
]

INTERPRET_TEMPLATES = [
    "نتیجه ahv یعنی چی؟",
    "تفسیر فرمول {fid}",
    "اهمیت {fid} در ارزیابی مواجهه",
    "فرمول {fid} یعنی چی و چرا مهم است؟",
]

CLARIFY_TEMPLATES = [
    "ahv را محاسبه کن",
    "محاسبه مواجهه ارتعاش",
    "ahv چنده؟",
    "فرمول را حساب کن",
]

INVALID_TEMPLATES = [
    "ahv را با ahw1=-1 و t1=0 محاسبه کن",
    "محاسبه ahv: ahw1=abc, t1=xyz",
    "ahv با ahw1=1 و t1=-5",
]

TYPO_TEMPLATES = [
    "ahv ra ba ahw1={ahw1} t1={t1} mohasebe kon",
    "محاسبه ahw با t1={t1}",  # missing ahw
]


def _formula_relevance(formula: FormulaRecord) -> RelevanceItem:
    return RelevanceItem(
        source_type="formula",
        relevance=RelevanceLevel.REQUIRED,
        formula_id=formula.formula_id,
        page_number=formula.page_number,
        gold_artifact_path=formula.gold_path,
    )


def _base_record(
    counter: QueryCounter,
    formula: FormulaRecord,
    *,
    query: str,
    intent: str,
    agents: list[str],
    difficulty: str = "medium",
    answer_type: str = "text",
    expected_answer: str | None = None,
    slots: dict[str, Any] | None = None,
    requires_clarification: bool = False,
    ground_truth: GroundTruth | None = None,
    tags: list[str] | None = None,
    review_status: ReviewStatus = ReviewStatus.MACHINE_VALIDATED,
) -> RetrievalEvalRecord:
    return RetrievalEvalRecord(
        query_id=counter.next_id("FORM"),
        query=query,
        standalone_query=query,
        resolved_query=query,
        intent=intent,
        domain="FORMULA",
        category="formula",
        difficulty=difficulty,  # type: ignore[arg-type]
        expected_agents=agents,
        expected_route=agents[0] if agents else "formula",
        slots=slots or {},
        expected_answer=expected_answer,
        answer_type=answer_type,  # type: ignore[arg-type]
        requires_clarification=requires_clarification,
        ground_truth=ground_truth or GroundTruth(
            relevance=[_formula_relevance(formula)],
            required_sources=["formulas"],
        ),
        citations=[
            CitationRequirement(
                formula_id=formula.formula_id,
                page_number=formula.page_number,
                authority="postgresql" if intent.startswith("FORMULA.CALCULATION") else "formula_engine",
            )
        ],
        metadata_requirements=MetadataRequirements(
            page_number=formula.page_number,
            source_type="formula",
            bbox=formula.bbox,
        ),
        source_traceability=SourceTraceability(
            gold_artifact_path=formula.gold_path,
            builder_version=BUILDER_VERSION_C2,
        ),
        review_status=review_status,
        tags=tags or ["formula", "c2"],
    )


def _calc_inputs(ahw: list[float], t: list[float]) -> dict[str, float]:
    inputs: dict[str, float] = {}
    for i, v in enumerate(ahw, 1):
        inputs[f"ahw_{i}"] = v
    for i, v in enumerate(t, 1):
        inputs[f"t_{i}"] = v
    inputs["T"] = sum(t)
    return inputs


def _build_ahv_calc_records(
    formula: FormulaRecord,
    counter: QueryCounter,
    intents: dict[str, dict[str, Any]],
) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    calc_intent = "FORMULA.CALCULATION.VIBRATION_AHV"

    for ahw_vals, t_vals in AHV_PARAM_SETS:
        result = evaluate_vibration_daily_exposure(
            formula_id=formula.formula_id,
            ahw_values=ahw_vals,
            t_values=t_vals,
        )
        if not result.valid:
            continue
        inputs = _calc_inputs(ahw_vals, t_vals)
        fmt = {
            "ahw1": ahw_vals[0],
            "t1": t_vals[0],
            "ahw2": ahw_vals[1] if len(ahw_vals) > 1 else None,
            "t2": t_vals[1] if len(t_vals) > 1 else None,
        }

        template_groups: list[tuple[list[str], str]] = [
            (CALC_TEMPLATES_FA, "fa_direct"),
            (CALC_TEMPLATES_EN, "en_direct"),
            (CALC_TEMPLATES_MIXED, "mixed"),
        ]
        if len(ahw_vals) > 1:
            template_groups.append((CALC_TEMPLATES_FA_MULTI, "fa_multi"))
            template_groups.append((CALC_TEMPLATES_EN, "en_multi"))

        for templates, tag in template_groups:
            for tmpl in templates:
                try:
                    q = tmpl.format(**{k: v for k, v in fmt.items() if v is not None})
                except KeyError:
                    continue
                # Add formula_id variant
                for q_variant in [q, f"{q} ({formula.formula_id})"]:
                    records.append(
                        _base_record(
                            counter,
                            formula,
                            query=q_variant,
                            intent=calc_intent,
                            agents=["formula"],
                            difficulty="easy" if len(ahw_vals) == 1 else "medium",
                            answer_type="numeric",
                            expected_answer=f"{result.result:.6f}",
                            slots={"formula_id": formula.formula_id, "variables": inputs},
                            ground_truth=GroundTruth(
                                relevance=[_formula_relevance(formula)],
                                required_sources=["formulas"],
                                formula=FormulaGroundTruth(
                                    formula_id=formula.formula_id,
                                    normalized_expression=formula.normalized_expression,
                                    calculation_inputs=inputs,
                                    calculation_output=result.result,
                                    valid=True,
                                ),
                            ),
                            tags=["formula", "calculation", tag],
                        )
                    )

        # Decimal formatting variants
        dec_q = f"ahv با ahw1={fmt['ahw1']:.1f} و t1={fmt['t1']:.1f} چنده؟"
        records.append(
            _base_record(
                counter,
                formula,
                query=dec_q,
                intent=calc_intent,
                agents=["formula"],
                answer_type="numeric",
                expected_answer=f"{result.result:.6f}",
                slots={"formula_id": formula.formula_id, "variables": inputs},
                ground_truth=GroundTruth(
                    relevance=[_formula_relevance(formula)],
                    formula=FormulaGroundTruth(
                        formula_id=formula.formula_id,
                        calculation_inputs=inputs,
                        calculation_output=result.result,
                        valid=True,
                    ),
                ),
                tags=["formula", "calculation", "decimal"],
            )
        )

    return records


def _build_lookup_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    ref = formula.gold_path.split("_")[-1].replace(".json", "") if formula.gold_path else "2"
    for tmpl in LOOKUP_TEMPLATES:
        q = tmpl.format(fid=formula.formula_id, page=formula.page_number, ref=ref)
        records.append(
            _base_record(
                counter,
                formula,
                query=q,
                intent="FORMULA.LOOKUP.BY_ID",
                agents=["formula"],
                difficulty="easy",
                answer_type="formula",
                expected_answer=formula.normalized_expression,
                slots={"formula_id": formula.formula_id},
                ground_truth=GroundTruth(
                    relevance=[_formula_relevance(formula)],
                    required_sources=["formulas"],
                    formula=FormulaGroundTruth(
                        formula_id=formula.formula_id,
                        normalized_expression=formula.normalized_expression,
                    ),
                ),
                tags=["formula", "lookup"],
            )
        )
    return records


def _build_variable_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    if not formula.variables:
        return records
    for tmpl in VAR_TEMPLATES:
        q = tmpl.format(fid=formula.formula_id)
        records.append(
            _base_record(
                counter,
                formula,
                query=q,
                intent="FORMULA.VARIABLE.EXPLANATION",
                agents=["formula"],
                slots={"formula_id": formula.formula_id},
                tags=["formula", "variables"],
            )
        )
    return records


def _build_domain_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for tmpl in DOMAIN_TEMPLATES:
        q = tmpl.format(page=formula.page_number, ref="2")
        records.append(
            _base_record(
                counter,
                formula,
                query=q,
                intent="FORMULA.LOOKUP.BY_DOMAIN",
                agents=["formula"],
                slots={"formula_id": formula.formula_id},
                tags=["formula", "domain_lookup"],
            )
        )
    return records


def _build_interpret_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for tmpl in INTERPRET_TEMPLATES:
        q = tmpl.format(fid=formula.formula_id)
        records.append(
            _base_record(
                counter,
                formula,
                query=q,
                intent="FORMULA.INTERPRETATION",
                agents=["formula", "semantic"],
                difficulty="hard",
                tags=["formula", "interpretation"],
            )
        )
    # Hybrid formula + explain
    q = f"فرمول {formula.formula_id} چیست و چه کاربردی دارد؟"
    records.append(
        _base_record(
            counter,
            formula,
            query=q,
            intent="HYBRID.FORMULA_AND_EXPLAIN",
            agents=["formula", "semantic"],
            tags=["formula", "hybrid_explain"],
        )
    )
    return records


def _build_unsupported_calc_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    """Formulas without deterministic evaluator → unsupported/guardrail."""
    if formula.formula_id == "formula_240_01":
        return []
    records: list[RetrievalEvalRecord] = []
    q_specs = [
        (f"مقدار نتیجه {formula.formula_id} را با ahv=2 و Tv=3600 محاسبه کن", "FORMULA.CALCULATION.UNSUPPORTED"),
        (f"calculate {formula.formula_id} with default inputs", "FORMULA.CALCULATION.UNSUPPORTED"),
        (f"محاسبه {formula.formula_id}", "CLARIFY.MISSING_EXPOSURE_VALUE"),
    ]
    for q, intent in q_specs:
        agents = ["guardrail"] if "UNSUPPORTED" in intent else ["clarify"]
        records.append(
            _base_record(
                counter,
                formula,
                query=q,
                intent=intent,
                agents=agents,
                requires_clarification="CLARIFY" in intent,
                answer_type="refusal" if "UNSUPPORTED" in intent else "clarification",
                tags=["formula", "unsupported"],
            )
        )
    return records


def _build_clarify_invalid_records(formula: FormulaRecord, counter: QueryCounter) -> list[RetrievalEvalRecord]:
    records: list[RetrievalEvalRecord] = []
    for tmpl in CLARIFY_TEMPLATES:
        records.append(
            _base_record(
                counter,
                formula,
                query=tmpl,
                intent="CLARIFY.MISSING_EXPOSURE_VALUE",
                agents=["clarify"],
                requires_clarification=True,
                answer_type="clarification",
                difficulty="medium",
                tags=["formula", "clarify"],
            )
        )
    for tmpl in INVALID_TEMPLATES:
        records.append(
            _base_record(
                counter,
                formula,
                query=f"{tmpl} ({formula.formula_id})",
                intent="GUARDRAIL.INVALID_INPUT",
                agents=["guardrail"],
                answer_type="refusal",
                difficulty="adversarial",
                tags=["formula", "invalid_input"],
            )
        )
    return records


def build_formula_eval_dataset(
    corpus: GoldCorpus,
    *,
    counter: QueryCounter | None = None,
    target_min: int = TARGET_MIN,
) -> list[RetrievalEvalRecord]:
    """Build 300+ grounded formula evaluation records from registry formulas only."""
    counter = counter or QueryCounter(start=10000)
    intents = load_intent_index()
    records: list[RetrievalEvalRecord] = []

    ahv_formula = next((f for f in corpus.formulas if f.formula_id == "formula_240_01"), None)

    for formula in corpus.formulas:
        records.extend(_build_lookup_records(formula, counter))
        records.extend(_build_variable_records(formula, counter))
        records.extend(_build_domain_records(formula, counter))
        records.extend(_build_interpret_records(formula, counter))
        records.extend(_build_unsupported_calc_records(formula, counter))
        records.extend(_build_clarify_invalid_records(formula, counter))

    if ahv_formula:
        records.extend(_build_ahv_calc_records(ahv_formula, counter, intents))

    # Pad with permuted calc queries if below target
    if ahv_formula and len(records) < target_min:
        extra_params = list(itertools.product(
            [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
        ))
        random.seed(42)
        random.shuffle(extra_params)
        for ahw1, t1 in extra_params:
            if len(records) >= target_min:
                break
            result = evaluate_vibration_daily_exposure(
                formula_id=ahv_formula.formula_id,
                ahw_values=[ahw1],
                t_values=[t1],
            )
            if not result.valid:
                continue
            inputs = _calc_inputs([ahw1], [t1])
            q = f"ahv با ahw1={ahw1} و t1={t1} محاسبه کن"
            records.append(
                _base_record(
                    counter,
                    ahv_formula,
                    query=q,
                    intent="FORMULA.CALCULATION.VIBRATION_AHV",
                    agents=["formula"],
                    answer_type="numeric",
                    expected_answer=f"{result.result:.6f}",
                    slots={"formula_id": ahv_formula.formula_id, "variables": inputs},
                    ground_truth=GroundTruth(
                        relevance=[_formula_relevance(ahv_formula)],
                        formula=FormulaGroundTruth(
                            formula_id=ahv_formula.formula_id,
                            calculation_inputs=inputs,
                            calculation_output=result.result,
                            valid=True,
                        ),
                    ),
                    tags=["formula", "calculation", "padded"],
                )
            )

    return records
