"""Schema and validation for the production intent taxonomy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TAXONOMY_DIR = Path(__file__).resolve().parents[1] / "data" / "intent_taxonomy"

VALID_AGENTS = frozenset({"structured", "semantic", "formula", "hybrid", "guardrail", "clarify"})
VALID_HYBRID_AGENTS = frozenset({"structured", "semantic", "formula"})
VALID_NUMERIC_AUTHORITY = frozenset(
    {"none", "postgresql", "formula_engine", "semantic_descriptive_only", "llm_forbidden"}
)
VALID_RISK = frozenset({"low", "medium", "high", "critical"})
REQUIRED_INTENT_FIELDS = frozenset(
    {
        "intent_id",
        "parent_category",
        "domain",
        "capability",
        "description",
        "purpose",
        "agent",
        "required_sources",
        "requires_calculation",
        "requires_structured_lookup",
        "requires_semantic_retrieval",
        "requires_multiple_agents",
        "requires_clarification",
        "risk_level",
        "numeric_safety_level",
        "numeric_authority",
        "citation_required",
        "expected_answer_language",
        "routing_rules",
        "required_slots",
        "optional_slots",
        "supported",
    }
)
SUPPORTED_STRUCTURED = frozenset({"chemical_registry", "oel_chemical_limits"})
SCHEMA_ONLY_STRUCTURED = frozenset(
    {
        "noise_limits",
        "vibration_limits",
        "biological_exposure_limits",
        "regulatory_constraints",
    }
)


@dataclass
class IntentDefinition:
    intent_id: str
    parent_category: str
    domain: str
    capability: str
    description: str
    purpose: str
    agent: str
    required_sources: list[str] = field(default_factory=list)
    optional_sources: list[str] = field(default_factory=list)
    requires_calculation: bool = False
    requires_structured_lookup: bool = False
    requires_semantic_retrieval: bool = False
    requires_multiple_agents: bool = False
    requires_clarification: bool = False
    risk_level: str = "low"
    numeric_safety_level: int = 0
    numeric_authority: str = "none"
    citation_required: bool = True
    expected_answer_language: str = "fa"
    routing_rules: list[str] = field(default_factory=list)
    required_slots: list[str] = field(default_factory=list)
    optional_slots: list[str] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    supported: bool = True
    fallback_agent: str | None = None
    fallback_note: str | None = None
    hybrid_agents: list[str] = field(default_factory=list)
    example_queries_fa: list[str] = field(default_factory=list)
    example_queries_en: list[str] = field(default_factory=list)
    negative_examples: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentDefinition:
        na = data.get("numeric_authority") or {}
        if isinstance(na, dict):
            numeric_authority = na.get("source", "none")
            if na.get("llm_authoritative") is True:
                numeric_authority = "llm_forbidden"
        else:
            numeric_authority = str(na)
        return cls(
            intent_id=data["intent_id"],
            parent_category=data["parent_category"],
            domain=data["domain"],
            capability=data["capability"],
            description=data["description"],
            purpose=data["purpose"],
            agent=data["agent"],
            required_sources=list(data.get("required_sources") or []),
            optional_sources=list(data.get("optional_sources") or []),
            requires_calculation=bool(data.get("requires_calculation")),
            requires_structured_lookup=bool(data.get("requires_structured_lookup")),
            requires_semantic_retrieval=bool(data.get("requires_semantic_retrieval")),
            requires_multiple_agents=bool(data.get("requires_multiple_agents")),
            requires_clarification=bool(data.get("requires_clarification")),
            risk_level=str(data.get("risk_level", "low")),
            numeric_safety_level=int(data.get("numeric_safety_level", 0)),
            numeric_authority=str(data.get("numeric_authority_source", numeric_authority)),
            citation_required=bool(data.get("citation_required", True)),
            expected_answer_language=str(data.get("expected_answer_language", "fa")),
            routing_rules=list(data.get("routing_rules") or []),
            required_slots=list(data.get("required_slots") or []),
            optional_slots=list(data.get("optional_slots") or []),
            missing_slots=list(data.get("missing_slots") or []),
            supported=bool(data.get("supported", True)),
            fallback_agent=data.get("fallback_agent"),
            fallback_note=data.get("fallback_note"),
            hybrid_agents=list(data.get("hybrid_agents") or []),
            example_queries_fa=list(data.get("example_queries_fa") or []),
            example_queries_en=list(data.get("example_queries_en") or []),
            negative_examples=list(data.get("negative_examples") or []),
        )


@dataclass
class IntentTaxonomy:
    version: str
    language_primary: str
    intents: list[IntentDefinition]

    def by_id(self) -> dict[str, IntentDefinition]:
        return {i.intent_id: i for i in self.intents}


def load_taxonomy(path: Path | None = None) -> IntentTaxonomy:
    path = path or TAXONOMY_DIR / "intent_taxonomy.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    intents = [IntentDefinition.from_dict(item) for item in raw.get("intents", [])]
    return IntentTaxonomy(
        version=str(raw.get("version", "1.0.0")),
        language_primary=str(raw.get("language_primary", "fa")),
        intents=intents,
    )


def validate_taxonomy(taxonomy: IntentTaxonomy, *, min_examples_fa: int = 3) -> list[str]:
    issues: list[str] = []
    seen_ids: set[str] = set()

    for intent in taxonomy.intents:
        if intent.intent_id in seen_ids:
            issues.append(f"duplicate intent_id: {intent.intent_id}")
        seen_ids.add(intent.intent_id)

        if intent.agent not in VALID_AGENTS:
            issues.append(f"{intent.intent_id}: invalid agent {intent.agent}")
        if intent.risk_level not in VALID_RISK:
            issues.append(f"{intent.intent_id}: invalid risk_level")
        if intent.numeric_authority not in VALID_NUMERIC_AUTHORITY:
            issues.append(f"{intent.intent_id}: invalid numeric_authority {intent.numeric_authority}")

        if intent.numeric_safety_level >= 2:
            if intent.agent == "semantic" and not intent.requires_clarification:
                issues.append(
                    f"{intent.intent_id}: safety-critical numeric query must not use semantic as authority"
                )
            if intent.numeric_authority not in {"postgresql", "formula_engine"}:
                issues.append(
                    f"{intent.intent_id}: level-{intent.numeric_safety_level} requires postgresql or formula_engine authority"
                )

        if intent.requires_multiple_agents and intent.agent != "hybrid":
            issues.append(f"{intent.intent_id}: requires_multiple_agents but agent != hybrid")
        if intent.agent == "hybrid" and not intent.hybrid_agents:
            issues.append(f"{intent.intent_id}: hybrid intent missing hybrid_agents list")
        if intent.hybrid_agents:
            invalid = [a for a in intent.hybrid_agents if a not in VALID_HYBRID_AGENTS]
            if invalid:
                issues.append(f"{intent.intent_id}: invalid hybrid_agents {invalid}")

        if intent.requires_clarification and intent.agent not in {"clarify", "guardrail"}:
            issues.append(f"{intent.intent_id}: requires_clarification should route to clarify/guardrail")
        if not intent.routing_rules:
            issues.append(f"{intent.intent_id}: missing routing_rules")
        if len(intent.example_queries_fa) < min_examples_fa and intent.supported:
            issues.append(
                f"{intent.intent_id}: insufficient Persian examples ({len(intent.example_queries_fa)})"
            )
        if intent.numeric_safety_level >= 2 and intent.requires_semantic_retrieval and intent.agent == "semantic":
            issues.append(f"{intent.intent_id}: level-2+ numeric must not be semantic-only")

        for src in intent.required_sources:
            if src in SCHEMA_ONLY_STRUCTURED and intent.supported:
                issues.append(
                    f"{intent.intent_id}: claims supported but source {src} is schema-only (not populated)"
                )

    return issues


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def validate_examples(
    taxonomy: IntentTaxonomy,
    fa_path: Path | None = None,
    en_path: Path | None = None,
    *,
    min_per_intent: int = 1,
) -> list[str]:
    issues: list[str] = []
    fa_path = fa_path or TAXONOMY_DIR / "intent_examples_fa.jsonl"
    en_path = en_path or TAXONOMY_DIR / "intent_examples_en.jsonl"
    by_id = taxonomy.by_id()

    fa_rows = _read_jsonl(fa_path)
    en_rows = _read_jsonl(en_path)
    fa_counts: dict[str, int] = {}
    for row in fa_rows:
        iid = row.get("intent_id")
        if iid not in by_id:
            issues.append(f"unknown intent_id in fa examples: {iid}")
            continue
        fa_counts[iid] = fa_counts.get(iid, 0) + 1
        if row.get("language") != "fa":
            issues.append(f"fa example not labeled fa: {row.get('query')}")

    for iid, intent in by_id.items():
        if intent.supported and fa_counts.get(iid, 0) < min_per_intent:
            issues.append(f"{iid}: no fa examples in JSONL")

    for row in en_rows:
        if row.get("intent_id") not in by_id:
            issues.append(f"unknown intent_id in en examples: {row.get('intent_id')}")

    return issues


def validate_eval_set(
    taxonomy: IntentTaxonomy,
    eval_path: Path | None = None,
    fa_examples_path: Path | None = None,
    *,
    min_queries: int = 100,
) -> list[str]:
    issues: list[str] = []
    eval_path = eval_path or TAXONOMY_DIR / "intent_eval_set.jsonl"
    fa_examples_path = fa_examples_path or TAXONOMY_DIR / "intent_examples_fa.jsonl"
    by_id = taxonomy.by_id()

    eval_rows = _read_jsonl(eval_path)
    if len(eval_rows) < min_queries:
        issues.append(f"eval set too small: {len(eval_rows)} < {min_queries}")

    training_queries = {r["query"] for r in _read_jsonl(fa_examples_path)}
    seen_eval: set[str] = set()

    for row in eval_rows:
        query = row.get("query", "")
        if query in seen_eval:
            issues.append(f"duplicate eval query: {query}")
        seen_eval.add(query)

        if "سؤال ارزیابی" in query or query.startswith("سوال ارزیابی"):
            issues.append(f"eval contains placeholder query: {query}")

        if query in training_queries:
            issues.append(f"eval query duplicates training example: {query}")

        for iid in row.get("expected_intents", []):
            if iid not in by_id:
                issues.append(f"eval references unknown intent: {iid}")
                continue
            intent = by_id[iid]
            if intent.numeric_safety_level >= 2 and row.get("numeric_authority") == "none":
                if not row.get("requires_clarification"):
                    issues.append(f"eval {query}: level-2+ intent but numeric_authority=none")
            if intent.agent == "semantic" and intent.numeric_safety_level >= 2:
                issues.append(f"eval {query}: safety-critical routed to semantic-only")

        agents = row.get("expected_agents", [])
        for iid in row.get("expected_intents", []):
            intent = by_id.get(iid)
            if not intent:
                continue
            if intent.requires_multiple_agents and len(agents) < 2:
                issues.append(f"eval {query}: hybrid intent but single agent in eval")

    return issues


def validate_routing_rules(path: Path | None = None) -> list[str]:
    issues: list[str] = []
    path = path or TAXONOMY_DIR / "intent_routing_rules.yaml"
    if not path.exists():
        return ["intent_routing_rules.yaml missing"]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("priority_order", "numeric_authority_rules", "guardrail_actions"):
        if key not in raw:
            issues.append(f"routing rules missing key: {key}")
    for level in ("level_0", "level_1", "level_2", "level_3", "level_4"):
        if level not in raw.get("numeric_authority_rules", {}):
            issues.append(f"routing rules missing {level}")
    return issues


def validate_slots(path: Path | None = None) -> list[str]:
    issues: list[str] = []
    path = path or TAXONOMY_DIR / "intent_slots.yaml"
    if not path.exists():
        return ["intent_slots.yaml missing"]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    required_slots = {
        "chemical_name",
        "cas",
        "oel_type",
        "concentration",
        "duration",
        "formula_id",
    }
    defined = set(raw.get("slots", {}).keys())
    missing = required_slots - defined
    if missing:
        issues.append(f"slot schema missing: {sorted(missing)}")
    return issues


def validate_all(*, min_eval: int = 100) -> list[str]:
    taxonomy = load_taxonomy()
    issues: list[str] = []
    issues.extend(validate_taxonomy(taxonomy))
    issues.extend(validate_examples(taxonomy))
    issues.extend(validate_eval_set(taxonomy, min_queries=min_eval))
    issues.extend(validate_routing_rules())
    issues.extend(validate_slots())
    return issues
