"""Embedding adapter contract (spec §1.11)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence, Union

import numpy as np


class EmbedderUnavailable(RuntimeError):
    """The embedder cannot answer right now (routes answer 503 + Retry-After: 5)."""


class EmbedderMismatch(RuntimeError):
    """The remote embed service serves another model or dimension (startup stops)."""


class EmbeddingAdapter(ABC):
    """Mirror of the SentenceTransformer surface KH uses.

    encode(str) -> 1-D float32 ndarray; encode(list[str]) -> 2-D float32 ndarray (len x dim).
    encode_query(text) -> list[float] with the model's query instruction prefixed (KH's /vector/query
    path deliberately does NOT use it).
    """

    name: str = ""
    dim: int = 0
    query_instruction: str = ""

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def _encode_batch(self, texts: list[str], normalize: bool, kind: str = "passage") -> np.ndarray: ...

    def encode(self, sentences: Union[str, Sequence[str]], normalize_embeddings: bool = True, **kw) -> np.ndarray:
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        arr = np.asarray(self._encode_batch(texts, bool(normalize_embeddings), kw.get("kind", "passage")),
                         dtype=np.float32)
        return arr[0] if single else arr

    def encode_query(self, text: str) -> list:
        return self.encode(self.query_instruction + text, normalize_embeddings=True).tolist()


def l2_normalize(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 1:
        n = float(np.linalg.norm(arr))
        return arr / n if n > 0 else arr
    n = np.linalg.norm(arr, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return arr / n
