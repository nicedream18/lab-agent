"""Shared, serialized model workers for Chroma embeddings and candidate reranking."""

import threading
import time

from chromadb.api.types import Documents, Embeddings
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
import numpy as np

from app.agent.observability import span
from app.common.batching import MicroBatcher
from app.config import settings


class BatchedEmbeddingFunction(SentenceTransformerEmbeddingFunction):
    """Keep Chroma's existing name/config and the exact normalized vector space."""

    def __init__(self, model_name: str):
        super().__init__(model_name=model_name, device=settings.KB_INFERENCE_DEVICE,
                         normalize_embeddings=True)
        self.batcher = MicroBatcher(
            self._encode, batch_size=settings.KB_EMBEDDING_BATCH_SIZE,
            wait_ms=settings.KB_BATCH_WAIT_MS, max_pending=settings.KB_BATCH_MAX_PENDING,
            name="embedding",
        )

    def _encode(self, texts: list[str]) -> list[np.ndarray]:
        vectors = np.asarray(self._model.encode(
            texts, batch_size=self.batcher.batch_size, show_progress_bar=False,
            convert_to_numpy=True, normalize_embeddings=True,
        ), dtype=np.float32)
        if vectors.ndim != 2 or len(vectors) != len(texts) or not np.isfinite(vectors).all():
            raise ValueError("invalid embedding batch")
        return list(vectors)

    def __call__(self, input: Documents) -> Embeddings:
        with span("kb.embedding", run_type="embedding",
                  metadata={"item_count": len(input), "batch_size": self.batcher.batch_size}):
            return self.batcher.map(list(input))


_lock = threading.Lock()
_embedding: BatchedEmbeddingFunction | None = None
_reranker = None
_rerank_batcher: MicroBatcher[tuple[str, str], float] | None = None
_rerank_unavailable_until = 0.0


def get_embedding_fn(model_name: str) -> BatchedEmbeddingFunction:
    global _embedding
    with _lock:
        if _embedding is None:
            _embedding = BatchedEmbeddingFunction(model_name)
        return _embedding


def _predict(pairs: list[tuple[str, str]]) -> list[float]:
    global _rerank_unavailable_until
    if time.monotonic() < _rerank_unavailable_until:
        raise RuntimeError("reranker temporarily unavailable")
    try:
        return _predict_model(pairs)
    except Exception:
        _rerank_unavailable_until = time.monotonic() + 5
        raise


def _predict_model(pairs: list[tuple[str, str]]) -> list[float]:
    global _reranker
    # Only the reranking worker calls this function, including model loading.
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder(settings.KB_RERANKER_MODEL,
                               device=settings.KB_INFERENCE_DEVICE,
                               max_length=settings.KB_RERANKER_MAX_LENGTH,
                               trust_remote_code=False)
    scores = np.asarray(_reranker.predict(
        pairs, batch_size=settings.KB_RERANKER_BATCH_SIZE,
        show_progress_bar=False, convert_to_numpy=True,
    )).reshape(-1)
    if len(scores) != len(pairs) or not np.isfinite(scores).all():
        raise ValueError("invalid reranker scores")
    return [float(score) for score in scores]


def rerank(pairs: list[tuple[str, str]]) -> list[float]:
    with span("kb.rerank", metadata={"pair_count": len(pairs),
                                    "batch_size": settings.KB_RERANKER_BATCH_SIZE}):
        return _rerank(pairs)


def _rerank(pairs: list[tuple[str, str]]) -> list[float]:
    global _rerank_batcher
    if not pairs:
        return []
    with _lock:
        if time.monotonic() < _rerank_unavailable_until:
            raise RuntimeError("reranker temporarily unavailable")
        if _rerank_batcher is None:
            _rerank_batcher = MicroBatcher(
                _predict, batch_size=settings.KB_RERANKER_BATCH_SIZE,
                wait_ms=settings.KB_BATCH_WAIT_MS, max_pending=settings.KB_BATCH_MAX_PENDING,
                name="reranker",
            )
        batcher = _rerank_batcher
    return batcher.map(pairs)


def close() -> None:
    global _embedding, _rerank_batcher, _reranker, _rerank_unavailable_until
    with _lock:
        embedding, _embedding = _embedding, None
        batcher, _rerank_batcher = _rerank_batcher, None
    if embedding is not None:
        embedding.batcher.close()
    if batcher is not None:
        batcher.close()
    _reranker = None
    _rerank_unavailable_until = 0.0
