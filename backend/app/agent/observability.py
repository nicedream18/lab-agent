"""Request-scoped LangSmith tracing; business audit remains in tracer.py."""

from contextlib import contextmanager
import logging
import random
from threading import Lock
from typing import Literal
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langsmith import Client, get_current_run_tree, trace, traceable, tracing_context
from langsmith.anonymizer import create_secret_anonymizer

from app.config import settings

logger = logging.getLogger(__name__)
_client: Client | None = None
_lock = Lock()


def get_client() -> Client | None:
    """Construct lazily: importing the app or disabled tracing never uploads."""
    global _client
    if not settings.LANGSMITH_TRACING:
        return None
    if not settings.LANGSMITH_API_KEY.get_secret_value():
        logger.warning("LangSmith disabled: LANGSMITH_API_KEY is missing")
        return None
    with _lock:
        if _client is None:
            try:
                _client = Client(
                    api_key=settings.LANGSMITH_API_KEY.get_secret_value(),
                    api_url=settings.LANGSMITH_ENDPOINT,
                    workspace_id=settings.LANGSMITH_WORKSPACE_ID,
                    hide_inputs=settings.LANGSMITH_HIDE_INPUTS,
                    hide_outputs=settings.LANGSMITH_HIDE_OUTPUTS,
                    anonymizer=create_secret_anonymizer(),
                    auto_batch_tracing=True,
                    tracing_sampling_rate=1.0,  # Sampling is decided once by request_tracing.
                    timeout_ms=2000,
                )
            except Exception as exc:
                logger.warning("LangSmith initialization skipped (%s)", type(exc).__name__)
    return _client


@contextmanager
def request_tracing(conversation_id: str, mode: str, recursion_limit: int):
    """One sample decision and one graph root per request, including all children."""
    sampled = settings.LANGSMITH_TRACING and random.random() < settings.LANGSMITH_SAMPLING_RATE
    client = get_client() if sampled else None
    enabled = client is not None
    run_id = uuid4()
    config: RunnableConfig = {
        "run_name": "lab-agent",
        "run_id": run_id,
        "recursion_limit": recursion_limit,
        "tags": ["lab-agent", mode],
        "metadata": {
            "thread_id": conversation_id,
            "conversation_id": conversation_id,
            "request_mode": mode,
            "llm_model": settings.LLM_MODEL,
            "llm_cache_enabled": settings.LLM_CACHE_ENABLED,
            "reranker_enabled": settings.KB_RERANKER_ENABLED,
        },
    }
    # Explicit False also overrides legacy LANGCHAIN_TRACING_V2 and inherited
    # contexts. backend/.env is loaded by Pydantic, not exported to the process.
    with tracing_context(
        enabled=enabled, client=client, parent=False,
        project_name=settings.LANGSMITH_PROJECT,
    ):
        if enabled:
            logger.info("LangSmith trace_id=%s conversation_id=%s mode=%s",
                        run_id, conversation_id, mode)
        yield config


def annotate(**metadata) -> None:
    """Attach only explicit operational metadata; never serialize AgentContext."""
    run = get_current_run_tree()
    if run is not None:
        run.add_metadata(metadata)


def observe_llm(name: str):
    # These helpers receive ctx/LLM instances. Actual model children carry the
    # messages when input/output logging is enabled; helper arguments stay empty.
    return traceable(name=name, process_inputs=lambda _: {}, process_outputs=lambda _: {})


@contextmanager
def span(
    name: str,
    *,
    run_type: Literal["chain", "tool", "retriever", "embedding"] = "chain",
    inputs: dict | None = None,
    metadata: dict | None = None,
):
    # Standalone KB warmups are outside a graph and should not create orphan runs.
    if get_current_run_tree() is None:
        yield None
        return
    with trace(name, run_type=run_type, inputs=inputs, metadata=metadata) as run:
        yield run


def close() -> None:
    """Drain the background uploader off the event loop during app shutdown."""
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.close(timeout=5)
        except Exception as exc:
            logger.warning("LangSmith shutdown skipped (%s)", type(exc).__name__)
