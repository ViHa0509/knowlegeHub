"""Embeddings (spec §1.11).

Public API:
    get_embedder() -> EmbeddingAdapter   configured singleton (KH_EMBEDDER = local|remote|auto|hash)
    encode(texts, normalize=True)        shorthand for get_embedder().encode(...)
    embedder_name() -> str               name stamped as metadata.emb_model
    EmbedderUnavailable, EmbedderMismatch
"""
from __future__ import annotations

import threading

from app import config
from app.embeddings.base import EmbedderMismatch, EmbedderUnavailable, EmbeddingAdapter

__all__ = ["get_embedder", "set_embedder", "encode", "embedder_name", "EmbedderUnavailable",
           "EmbedderMismatch", "EmbeddingAdapter"]

_embedder: EmbeddingAdapter | None = None
_lock = threading.Lock()


def _build(kind: str) -> EmbeddingAdapter:
    if kind == "hash":
        from app.embeddings.hash_adapter import HashEmbeddingAdapter

        return HashEmbeddingAdapter()
    if kind == "remote":
        from app.embeddings.remote import RemoteEmbeddingAdapter

        return RemoteEmbeddingAdapter()
    if kind == "auto":
        from app.embeddings.remote import AutoEmbeddingAdapter

        return AutoEmbeddingAdapter()
    from app.embeddings.registry import make_local

    return make_local(config.EMBEDDING_MODEL)


def get_embedder() -> EmbeddingAdapter:
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = _build(config.EMBEDDER)
    return _embedder


def set_embedder(adapter: EmbeddingAdapter | None) -> None:
    """Tests / tools: replace the singleton (None = rebuild from config on next use)."""
    global _embedder
    _embedder = adapter


def encode(texts, normalize: bool = True):
    return get_embedder().encode(texts, normalize_embeddings=normalize)


def embedder_name() -> str:
    return get_embedder().name or config.EMBEDDING_MODEL
