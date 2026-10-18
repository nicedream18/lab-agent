"""Batch boundaries, cross-request mapping and retrieval fallback without model downloads."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.common.batching import BatchQueueFull, MicroBatcher
from app.services import inference_service as inference, kb_service
from tests.test_kb_index import FakeCollection


def make_batcher(predict, size=4, wait=10, pending=32):
    return MicroBatcher(predict, batch_size=size, wait_ms=wait,
                        max_pending=pending, name="test")


def test_concurrent_requests_share_batches_and_keep_their_results():
    barrier = threading.Barrier(8)
    calls = []

    def predict(values):
        calls.append(list(values))
        return [value * 10 for value in values]

    batcher = make_batcher(predict, size=8, wait=100)
    try:
        def request(value):
            barrier.wait(timeout=3)
            return batcher.map([value])[0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(request, range(8))) == [i * 10 for i in range(8)]
        assert len(calls) == 1
        assert batcher.stats() == {"batches": 1, "items": 8, "errors": 0,
                                   "max_batch": 8, "pending": 0}
    finally:
        batcher.close()


def test_large_input_is_bounded_and_lone_request_flushes_without_filling_batch():
    calls = []

    def predict(values):
        calls.append(values)
        return values

    batcher = make_batcher(predict, size=3, wait=1)
    try:
        assert batcher.map(list(range(8))) == list(range(8))
        assert list(map(len, calls)) == [3, 3, 2]
        assert batcher.map([]) == []
    finally:
        batcher.close()


@pytest.mark.parametrize("bad", [RuntimeError("model error"), []])
def test_model_failure_reaches_waiters_and_worker_handles_next_batch(bad):
    predict = Mock(side_effect=[bad, [2]])
    batcher = make_batcher(predict, size=1, wait=0)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            batcher.map([1])
        assert batcher.map([2]) == [2]
        assert batcher.stats()["errors"] == 1
    finally:
        batcher.close()


def test_queue_limit_and_shutdown_release_waiters_without_interrupting_active_call():
    entered, release = threading.Event(), threading.Event()

    def predict(values):
        entered.set()
        assert release.wait(3)
        return values

    batcher = make_batcher(predict, size=1, wait=0, pending=1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        active = pool.submit(batcher.map, [1])
        try:
            assert entered.wait(2)
            queued = pool.submit(batcher.map, [2])
            deadline = time.monotonic() + 2
            while batcher.stats()["pending"] != 1 and time.monotonic() < deadline:
                time.sleep(0.001)
            assert batcher.stats()["pending"] == 1
            with pytest.raises(BatchQueueFull):
                batcher.map([3])
            closing = pool.submit(batcher.close)
            with pytest.raises(RuntimeError, match="closed"):
                queued.result(timeout=2)
            assert not closing.done()
        finally:
            release.set()
            batcher.close()
        assert active.result(timeout=2) == [1]
        closing.result(timeout=2)
    with pytest.raises(RuntimeError, match="closed"):
        batcher.map([4])


def test_invalid_batch_limits_are_rejected():
    for size, pending, wait in [(0, 1, 0), (2, 1, 0), (1, 1, -1)]:
        with pytest.raises(ValueError):
            make_batcher(lambda values: values, size=size, pending=pending, wait=wait)


@pytest.fixture
def models(monkeypatch):
    inference.close()
    monkeypatch.setattr(inference.settings, "KB_EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(inference.settings, "KB_RERANKER_BATCH_SIZE", 2)
    monkeypatch.setattr(inference.settings, "KB_BATCH_WAIT_MS", 0)
    yield
    inference.close()


def test_embedding_preserves_normalization_chroma_identity_and_bounded_model_calls(models, monkeypatch):
    model = SimpleNamespace(encode=Mock(side_effect=lambda texts, **kwargs:
                                       np.array([[float(text), 1] for text in texts])))
    monkeypatch.setitem(inference.SentenceTransformerEmbeddingFunction.models, "test-model", model)
    first = inference.get_embedding_fn("test-model")
    assert inference.get_embedding_fn("test-model") is first
    assert first.name() == "sentence_transformer"
    assert first.get_config()["normalize_embeddings"] is True
    vectors = first(["1", "2", "3"])
    np.testing.assert_array_equal(vectors, [[1, 1], [2, 1], [3, 1]])
    assert all(vector.dtype == np.float32 for vector in vectors)
    assert [call.args[0] for call in model.encode.call_args_list] == [["1", "2"], ["3"]]
    assert model.encode.call_args.kwargs["normalize_embeddings"] is True
    assert model.encode.call_args.kwargs["batch_size"] == 2


def test_invalid_embedding_vectors_are_rejected(models, monkeypatch):
    model = SimpleNamespace(encode=Mock(return_value=[[float("nan"), 1]]))
    monkeypatch.setitem(inference.SentenceTransformerEmbeddingFunction.models, "bad-model", model)
    with pytest.raises(ValueError, match="invalid embedding"):
        inference.get_embedding_fn("bad-model")(["1"])


def test_reranker_batches_pairs_and_passes_explicit_model_batch_size(models, monkeypatch):
    model = SimpleNamespace(predict=Mock(side_effect=lambda pairs, **kwargs:
                                        np.array([float(doc) for _, doc in pairs])))
    monkeypatch.setattr(inference, "_reranker", model)
    assert inference.rerank([("q", "1"), ("q", "2"), ("q", "3")]) == [1, 2, 3]
    assert [len(call.args[0]) for call in model.predict.call_args_list] == [2, 1]
    assert model.predict.call_args.kwargs["batch_size"] == 2
    assert inference.rerank([]) == []


@pytest.mark.parametrize("scores", [[float("nan")], [0.1, 0.2]])
def test_bad_reranker_scores_fail_and_cooldown_avoids_repeated_model_work(models, monkeypatch, scores):
    model = SimpleNamespace(predict=Mock(return_value=np.asarray(scores)))
    monkeypatch.setattr(inference, "_reranker", model)
    with pytest.raises(ValueError):
        inference.rerank([("q", "1")])
    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        inference.rerank([("q", "2")])
    model.predict.assert_called_once()


def test_multi_query_reranking_maps_scores_to_correct_candidates(monkeypatch):
    col = Mock()
    col.count.return_value = 20
    col.query.return_value = {"documents": [["a", "b", "c", "d"], ["x", "y"]]}
    monkeypatch.setattr(kb_service, "get_collection", lambda: col)
    monkeypatch.setattr(kb_service.settings, "KB_RERANKER_ENABLED", True)
    predict = Mock(return_value=[0.1, 0.9, 0.9, 0.2, 0.8, 0.1])
    monkeypatch.setattr(inference, "rerank", predict)
    assert kb_service.search_many(["q1", "", "q2"]) == ["b\n\nc\n\nd", "", "x\n\ny"]
    assert predict.call_args.args[0] == [("q1", doc) for doc in "abcd"] + [("q2", doc) for doc in "xy"]
    assert col.query.call_args.kwargs["query_texts"] == ["q1", "q2"]


def test_reranker_failure_or_disabled_preserves_vector_order(monkeypatch):
    col = FakeCollection()
    col.seed(5)
    monkeypatch.setattr(kb_service, "get_collection", lambda: col)
    monkeypatch.setattr(kb_service.settings, "KB_RERANKER_ENABLED", True)
    predict = Mock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(inference, "rerank", predict)
    expected = "\n\n".join(list(col.docs.values())[:3])
    assert kb_service.search("q") == expected
    monkeypatch.setattr(kb_service.settings, "KB_RERANKER_ENABLED", False)
    assert kb_service.search("q") == expected
    predict.assert_called_once()


def test_multi_query_chunking_and_empty_inputs(monkeypatch):
    col = Mock()
    col.count.return_value = 1
    col.query.side_effect = lambda query_texts, **kwargs: {"documents": [[q] for q in query_texts]}
    monkeypatch.setattr(kb_service, "get_collection", lambda: col)
    monkeypatch.setattr(kb_service.settings, "KB_EMBEDDING_BATCH_SIZE", 2)
    assert kb_service.search_many(["a", "", "b", "c"]) == ["a", "", "b", "c"]
    assert [len(call.kwargs["query_texts"]) for call in col.query.call_args_list] == [2, 1]
    assert kb_service.search_many([]) == []
    assert kb_service.search_many([" ", ""]) == ["", ""]
    with pytest.raises(TypeError):
        kb_service.search_many([None])
    col.query.return_value = {}
    col.query.side_effect = None
    with pytest.raises(ValueError, match="result count"):
        kb_service.search_many(["q"])


def test_partial_batched_index_is_retried_until_all_chunks_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_service, "KB_DIR", tmp_path)
    monkeypatch.setattr(kb_service.settings, "KB_INDEX_BATCH_SIZE", 2)
    (tmp_path / "test.md").write_text("\n".join(f"# 节{i}\n正文{i}" for i in range(5)))
    col = FakeCollection()
    original = col.add
    attempts = 0

    def add(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("interrupted write")
        original(**kwargs)

    monkeypatch.setattr(col, "add", add)
    with pytest.raises(RuntimeError):
        kb_service._sync_index(col)
    assert col.count() == 2
    assert {meta["chunk_count"] for meta in col.metas.values()} == {5}
    kb_service._sync_index(col)
    assert col.count() == 5
    assert [len(ids) for ids in col.add_calls] == [2, 2, 2, 1]
    previous = attempts
    kb_service._sync_index(col)
    assert attempts == previous
