"""Opt-in cached-model smoke test; never downloads models or touches the business index."""

import os

import numpy as np
import pytest


@pytest.mark.skipif(os.getenv("RUN_INFERENCE_MODELS") != "1", reason="requires cached local models")
def test_existing_chroma_collection_works_with_batching_and_real_reranker(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    from app.services import inference_service as inference, kb_service

    inference.close()
    monkeypatch.setattr(kb_service, "KB_DIR", tmp_path / "kb")
    monkeypatch.setattr(kb_service, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(kb_service, "_collection", None)
    monkeypatch.setattr(kb_service.settings, "KB_RERANKER_ENABLED", True)
    kb_service.KB_DIR.mkdir()
    (kb_service.KB_DIR / "rules.md").write_text(
        "# 危险化学品管理\n危险化学品由专人分类保管，领用必须登记。\n"
        "# 实验室预约\n使用实验室须提前提交预约申请，管理员审核后使用。\n"
        "# 防火要求\n发现火灾立即报警，组织人员沿安全通道撤离。\n"
    )
    try:
        legacy = SentenceTransformerEmbeddingFunction(model_name=kb_service.EMBEDDING_MODEL,
                                                      normalize_embeddings=True)
        client = chromadb.PersistentClient(path=str(kb_service.CHROMA_DIR))
        original = client.get_or_create_collection(name=kb_service.COLLECTION_NAME,
            embedding_function=legacy, configuration={"hnsw": {"space": "cosine"}})
        kb_service._sync_index(original)
        before = original.get(include=["embeddings"])
        # Reopen the same persisted function configuration using the new adapter.
        current = kb_service.get_collection()
        assert current.count() == 3
        assert inference.get_embedding_fn(kb_service.EMBEDDING_MODEL).batcher.stats()["items"] == 0
        result = kb_service.search_many(["危险化学品如何保管？", "", "如何预约实验室？"])
        assert len(result) == 3 and result[1] == ""
        assert "危险化学品管理" in result[0] and "实验室预约" in result[2]
        assert inference._rerank_batcher.stats()["items"] == 6
        assert inference._rerank_batcher.stats()["errors"] == 0
        after = current.get(include=["embeddings"])
        assert before["ids"] == after["ids"]
        np.testing.assert_array_equal(before["embeddings"], after["embeddings"])
    finally:
        kb_service.close()
    assert kb_service._collection is None
