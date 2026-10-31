"""Embedding model registry (spec §1.11)."""
from __future__ import annotations

from app.embeddings.base import EmbeddingAdapter
from app.embeddings.sentence_transformer_adapter import SentenceTransformerAdapter

BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

REGISTRY: dict[str, dict] = {
    "bge-small-en-v1.5": {
        "hf_id": "BAAI/bge-small-en-v1.5",
        "query_instruction": BGE_QUERY_INSTRUCTION,
        "dim": 384,
    },
}


def spec_of(name: str) -> dict:
    return REGISTRY.get(name, {"hf_id": name, "query_instruction": "", "dim": 0})


def make_local(name: str) -> EmbeddingAdapter:
    """Unregistered keys are treated as plain SentenceTransformer ids."""
    s = spec_of(name)
    return SentenceTransformerAdapter(name=name, hf_id=s["hf_id"], query_instruction=s["query_instruction"],
                                      dim=s["dim"])
