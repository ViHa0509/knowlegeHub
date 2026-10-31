"""Query-side semantic chunking (spec §1.8a) and model loading (`load_model`, lifespan step 1).

`nltk` is optional: when `punkt` is unavailable a regex sentence splitter is used.
"""
from __future__ import annotations

import logging
import re

import numpy as np

from app.embeddings import get_embedder

log = logging.getLogger(__name__)

_nltk_ok: bool | None = None
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def _ensure_nltk() -> bool:
    global _nltk_ok
    if _nltk_ok is not None:
        return _nltk_ok
    try:
        import nltk  # optional

        for pkg in ("punkt", "punkt_tab"):
            try:
                nltk.download(pkg, quiet=True, raise_on_error=False)
            except Exception:  # noqa: BLE001
                pass
        nltk.sent_tokenize("Probe sentence. Another one.")
        _nltk_ok = True
    except Exception:  # noqa: BLE001
        _nltk_ok = False
    return _nltk_ok


def load_model():
    """Lifespan step [1/3]: NLTK data (quietly, optional) + get_embedder().load(). Returns the embedder."""
    _ensure_nltk()
    emb = get_embedder()
    emb.load()
    return emb


def sent_tokenize(text: str) -> list[str]:
    if _ensure_nltk():
        try:
            import nltk

            return [s for s in nltk.sent_tokenize(text) if s.strip()]
        except Exception:  # noqa: BLE001
            pass
    parts = [p.strip() for p in _SENT_RE.split(text.strip())]
    return [p for p in parts if p]


def semantic_chunks(text: str, c: float = 1.0, model=None) -> list[str]:
    """Split where adjacent-sentence similarity drops below mean(s) - c*std(s)."""
    model = model or get_embedder()
    sents = sent_tokenize(text)
    if len(sents) <= 1:
        return [text.strip()] if text.strip() else []
    emb = np.asarray(model.encode(sents, normalize_embeddings=True, batch_size=64), dtype=np.float32)
    sims = np.sum(emb[:-1] * emb[1:], axis=1)
    cutoff = float(np.mean(sims) - c * np.std(sims))
    chunks, cur = [], [sents[0]]
    for i, s in enumerate(sims):
        if s < cutoff:
            chunks.append(" ".join(cur))
            cur = []
        cur.append(sents[i + 1])
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def chunk_with_embeddings(text: str, c: float = 1.0, model=None) -> list[dict]:
    """POST /vector/chunk body: [{chunk_text, embedding}]."""
    model = model or get_embedder()
    chunks = semantic_chunks(text, c, model)
    if not chunks:
        return []
    vecs = model.encode(chunks, normalize_embeddings=True)
    return [{"chunk_text": t, "embedding": np.asarray(v, dtype=float).tolist()} for t, v in zip(chunks, vecs)]
