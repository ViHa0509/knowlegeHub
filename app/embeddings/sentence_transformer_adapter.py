"""In-process SentenceTransformer adapter (spec §1.11). `sentence_transformers` is imported lazily."""
from __future__ import annotations

import logging
import threading

import numpy as np

from app import config
from app.embeddings.base import EmbedderUnavailable, EmbeddingAdapter

log = logging.getLogger(__name__)


class SentenceTransformerAdapter(EmbeddingAdapter):
    def __init__(self, name: str, hf_id: str | None = None, query_instruction: str = "", dim: int = 0):
        self.name = name
        self.hf_id = hf_id or name
        self.query_instruction = query_instruction
        self.dim = dim
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer  # lazy, heavy
            except Exception as e:  # noqa: BLE001
                raise EmbedderUnavailable(f"sentence-transformers is not installed: {e}") from e
            path = config.local_model_path(self.name)
            if path is not None:
                log.info("loading embedder %s from %s", self.name, path)
                model = SentenceTransformer(str(path), device="cpu")
            else:
                log.info("embedder %s not found under %s; downloading %s", self.name,
                         [str(r) for r in config.MODEL_ROOTS], self.hf_id)
                model = SentenceTransformer(self.hf_id, device="cpu")
                try:
                    target = config.MODEL_ROOTS[0] / self.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    model.save(str(target))
                except Exception as e:  # noqa: BLE001
                    log.warning("could not save model weights: %s", e)
            got = int(model.get_sentence_embedding_dimension() or 0)
            if self.dim and got and got != self.dim:
                log.warning("embedder %s reports dim %s (registry says %s)", self.name, got, self.dim)
            self.dim = got or self.dim
            self._model = model

    def _encode_batch(self, texts, normalize, kind="passage"):
        if self._model is None:
            self.load()
        if kind == "query" and self.query_instruction:
            texts = [self.query_instruction + t for t in texts]
        return np.asarray(self._model.encode(texts, normalize_embeddings=normalize, batch_size=64,
                                             show_progress_bar=False), dtype=np.float32)
