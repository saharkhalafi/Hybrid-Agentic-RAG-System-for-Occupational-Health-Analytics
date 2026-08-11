"""Formal contracts between HSE6 pipeline stages (Evidence → Grid → Table → Domain)."""

from pipeline_contracts.canonical_grid import CanonicalGrid, CanonicalGridBuilder
from pipeline_contracts.canonical_table import CanonicalTable, CanonicalTableBuilder
from pipeline_contracts.confidence import LayeredConfidence
from pipeline_contracts.evidence import EvidenceManifest, EvidenceStore
from pipeline_contracts.provenance import ProcessorProvenance
from pipeline_contracts.table_family_classifier import (
    TableFamilyClassification,
    TableFamilyClassifier,
)
from pipeline_contracts.validation_codes import ValidationIssue, ValidationErrorCode

__all__ = [
    "CanonicalGrid",
    "CanonicalGridBuilder",
    "CanonicalTable",
    "CanonicalTableBuilder",
    "EvidenceManifest",
    "EvidenceStore",
    "LayeredConfidence",
    "ProcessorProvenance",
    "TableFamilyClassification",
    "TableFamilyClassifier",
    "ValidationErrorCode",
    "ValidationIssue",
]
