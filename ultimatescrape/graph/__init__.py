"""Local knowledge graph — accumulates entities, relations and provenance across runs."""

from .ingest import extract_relations_llm, ingest_pages, ingest_swarm_result, maybe_ingest
from .store import Entity, KnowledgeGraph, Relation, canonical_key

__all__ = [
    "Entity",
    "KnowledgeGraph",
    "Relation",
    "canonical_key",
    "extract_relations_llm",
    "ingest_pages",
    "ingest_swarm_result",
    "maybe_ingest",
]
