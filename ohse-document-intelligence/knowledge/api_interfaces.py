"""Knowledge layer interfaces for future agent consumption."""

from __future__ import annotations

from typing import Any, Protocol

from knowledge.metadata_contract import ProvenanceChain


class StructuredKnowledgeStore(Protocol):
    def get_oel_by_cas(self, cas: str) -> list[dict[str, Any]]: ...
    def get_chemical_by_alias(self, name: str) -> dict[str, Any] | None: ...


class SemanticKnowledgeStore(Protocol):
    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]: ...


class FormulaKnowledgeStore(Protocol):
    def get_formula(self, formula_id: str) -> dict[str, Any] | None: ...
    def calculate(self, formula_id: str, inputs: dict[str, float]) -> dict[str, Any]: ...


class ProvenanceResolver(Protocol):
    def resolve(self, record_id: str, record_type: str) -> ProvenanceChain: ...
