"""Phase B — Knowledge Engineering layer."""

from knowledge.metadata_contract import (
    KnowledgeMetadata,
    ProvenanceChain,
    build_provenance_chain,
    validate_knowledge_metadata,
)

__all__ = [
    "KnowledgeMetadata",
    "ProvenanceChain",
    "build_provenance_chain",
    "validate_knowledge_metadata",
]
