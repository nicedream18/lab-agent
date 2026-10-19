"""Isolated API/RAG baseline. Started exclusively by load_test.py --local."""

from contextlib import asynccontextmanager
import os
from pathlib import Path


database_path = os.environ.get("LAB_LOADTEST_LOCAL_DB")
password = os.environ.get("LAB_LOADTEST_LOCAL_PASSWORD")
if not database_path or not password:
    raise RuntimeError("Start this temporary backend with scripts/load_test.py --local")

# Set before app imports so backend/.env can never select a real DB or model.
os.environ.update({
    "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
    "JWT_SECRET_KEY": os.environ["LAB_LOADTEST_LOCAL_JWT"],
    "LLM_API_KEY": "loadtest-offline",
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_MODEL": "loadtest-offline",
    "LLM_FORCE_IPV4": "false",
    "LLM_CACHE_ENABLED": "false",
    "KB_WARMUP_ON_STARTUP": "false",
    "LOG_ACCESS_ENABLED": "false",
    "KB_RERANKER_ENABLED": "true" if os.getenv("LAB_LOADTEST_LOCAL_RAG") == "1" else "false",
})

from app.main import app
from app.database import SessionLocal
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.user import User
from app.utils.password import hash_password

rag_enabled = os.getenv("LAB_LOADTEST_LOCAL_RAG") == "1"
if rag_enabled:
    from fastapi import Depends
    from pydantic import BaseModel, Field
    from app.common.async_io import run_sync
    from app.common.response import Response
    from app.dependencies.auth import get_current_user
    from app.services import inference_service, kb_service

    # Read repository documents, but build/query only a temporary vector index.
    kb_service.CHROMA_DIR = Path(os.environ["LAB_LOADTEST_LOCAL_CHROMA"])

    class RagRequest(BaseModel):
        query: str = Field(min_length=1, max_length=1000)

    @app.post("/__loadtest/rag")
    def rag(data: RagRequest, _: User = Depends(get_current_user)):
        content = kb_service.search(data.query)
        # Fail the test instead of timing an empty index or silent rerank fallback.
        if (not content or inference_service._reranker is None
                or inference_service._rerank_batcher.stats()["errors"]):
            raise RuntimeError("RAG did not execute both embedding and reranking")
        return Response.success(data={"content": content, "found": True})

    @app.get("/__loadtest/info")
    def info():
        import hashlib
        import platform
        import torch

        config = kb_service.settings
        return {
            "python": platform.python_version(), "platform": platform.platform(),
            "embedding_model": kb_service.EMBEDDING_MODEL,
            "reranker_model": config.KB_RERANKER_MODEL,
            "embedding_batch_size": config.KB_EMBEDDING_BATCH_SIZE,
            "reranker_batch_size": config.KB_RERANKER_BATCH_SIZE,
            "device": config.KB_INFERENCE_DEVICE, "torch_threads": torch.get_num_threads(),
            "wait_ms": config.KB_BATCH_WAIT_MS, "chunks": kb_service.get_collection().count(),
            "documents": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(kb_service.KB_DIR.glob("*.md"))
                          if not path.name.startswith(".")},
            "embedding": inference_service._embedding.batcher.stats(),
            "reranker": inference_service._rerank_batcher.stats(),
        }


@asynccontextmanager
async def lifespan(_):
    # Same API baseline as before. RAG optionally prewarms real cached models.
    if rag_enabled:
        await run_sync(kb_service.warmup)
        await run_sync(rag, RagRequest(query="实验室危险化学品如何管理？"), None)
    try:
        yield
    finally:
        if rag_enabled:
            await run_sync(kb_service.close)


app.router.lifespan_context = lifespan
with SessionLocal() as db:
    db.add(User(username="loadtest", password=hash_password(password),
                name="压测账号", role="student", status=1))
    labs = [Lab(name=f"压测实验室{i + 1}", capacity=30, status=1,
                open_time="08:00", close_time="20:00") for i in range(20)]
    db.add_all(labs)
    db.flush()
    db.add_all([Equipment(lab_id=lab.id, name=f"测试设备{index + 1}",
                          quantity=2, status=1)
                for lab in labs for index in range(5)])
    db.commit()
