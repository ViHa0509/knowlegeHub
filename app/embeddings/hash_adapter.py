"""Deterministic, dependency-free 384-dim embedder (rebuild contract, `KH_EMBEDDER=hash`).

Lower-cased word tokens + character trigrams of each word, hashed (blake2b) into 384 buckets,
L2-normalized. Used by tests and by machines without the model. Similar wording -> similar vectors,
but it is NOT semantically calibrated (thresholds like 0.68 are meaningless with it).
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from app.embeddings.base import EmbeddingAdapter, l2_normalize

HASH_DIM = 384
HASH_NAME = "kh-hash-384"
_WORD = re.compile(r"[a-z0-9]+")


def _bucket(token: str) -> int:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "little") % HASH_DIM


def hash_vector(text: str) -> np.ndarray:
    v = np.zeros(HASH_DIM, dtype=np.float32)
    words = _WORD.findall((text or "").lower())
    for w in words:
        v[_bucket("w:" + w)] += 1.0
        padded = f"#{w}#"
        for i in range(len(padded) - 2):
            v[_bucket("t:" + padded[i:i + 3])] += 0.5
    if not words:
        v[0] = 1.0  # stable non-zero vector for empty / symbol-only text
    return v


class HashEmbeddingAdapter(EmbeddingAdapter):
    name = HASH_NAME
    dim = HASH_DIM
    query_instruction = ""

    def load(self) -> None:
        return None

    def _encode_batch(self, texts, normalize, kind="passage"):
        arr = np.stack([hash_vector(t) for t in texts])
        return l2_normalize(arr) if normalize else arr
