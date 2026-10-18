"""Concurrency and cancellation contracts for the Agent's I/O boundaries."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, Mock

from langchain_core.messages import AIMessage
import pytest
from sqlalchemy.orm import Session

from app.agent import graph, llm_cache, planner, tracer
from app.agent.state import AgentContext
from app.common.async_io import run_sync
from app.common.exceptions import BusinessException
from app.schemas.ai import ChatRequest
from app.services import agent_service, reservation_service

pytestmark = pytest.mark.anyio


@contextmanager
def no_trace(*args, **kwargs):
    yield {"output": "", "status": "success"}


async def test_all_structured_nodes_can_wait_for_model_concurrently(monkeypatch):
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def reply(messages):
        nonlocal entered
        entered += 1
        if entered == 3:
            all_entered.set()
        await release.wait()
        return AIMessage(content='{"slots":{},"missing_slots":[],"steps":[{"goal":"查询","hint_tools":["list_open_labs"]}]}')

    provider = SimpleNamespace(ainvoke=AsyncMock(side_effect=reply), invoke=Mock())
    monkeypatch.setattr(planner, "get_llm", lambda: provider)
    monkeypatch.setattr(graph, "trace_node", no_trace)
    state = {"user_query": "有哪些实验室", "today": "2026-10-18"}
    ctx = AgentContext(object(), SimpleNamespace(id=1, role="student"), "async-test")
    calls = [asyncio.create_task(factory(ctx)(state)) for factory in (
        graph.node_analyze, graph.node_plan, graph.node_replan
    )]
    try:
        # All three must enter native ainvoke before any provider response exists.
        await asyncio.wait_for(all_entered.wait(), 2)
    finally:
        release.set()
        results = await asyncio.gather(*calls)
    assert "slots" in results[0]
    assert results[1]["plan"] and results[2]["plan"]
    provider.invoke.assert_not_called()


async def test_retry_backoff_allows_other_coroutines_to_run(monkeypatch):
    monkeypatch.setattr(planner, "RETRY_DELAYS", (0, 0.02))
    heartbeat = asyncio.Event()
    calls = 0

    async def invoke(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BusinessException(message="upstream 503")
        assert heartbeat.is_set(), "retry delay blocked the event loop"
        return '{"slots":{}}'

    async def tick():
        await asyncio.sleep(0)
        heartbeat.set()

    monkeypatch.setattr(planner, "_invoke", invoke)
    ctx = SimpleNamespace(llm_failures=0)
    result, _ = await asyncio.gather(planner._ask(ctx, "q"), tick())
    assert result == '{"slots":{}}' and calls == 2


@pytest.mark.parametrize("streaming", [False, True])
async def test_facade_offloads_initialization_and_history_in_order(monkeypatch, streaming):
    loop_thread = threading.get_ident()
    order = []

    class Runner:
        def __init__(self, *args, **kwargs):
            assert threading.get_ident() != loop_thread
            order.append("memory")

        async def ainvoke(self):
            assert threading.get_ident() == loop_thread
            order.append("model")
            return "answer"

        async def astream(self):
            yield {"type": "done", "answer": await self.ainvoke()}

    def record(*args):
        assert threading.get_ident() != loop_thread
        assert order == ["memory", "model"]
        order.append("history")

    monkeypatch.setattr(agent_service.workflow, "WorkflowRunner", Runner)
    monkeypatch.setattr(agent_service, "_record_turn", record)
    data = ChatRequest(messages=[{"role": "user", "content": "hello"}])
    if streaming:
        events = [event async for event in agent_service.stream_agent(object(), object(), data)]
        assert events[-1] == {"type": "done", "answer": "answer"}
    else:
        assert await agent_service.run_agent(object(), object(), data) == "answer"
    assert order == ["memory", "model", "history"]


async def test_async_node_offloads_drafts_and_trace_commits(monkeypatch):
    loop_thread = threading.get_ident()
    order = []

    def drafts(ctx, state, result):
        assert threading.get_ident() != loop_thread
        order.append("drafts")
        return result

    def write_trace(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        assert order == ["drafts"]
        order.append("trace")

    monkeypatch.setattr(graph, "_prepare_drafts", drafts)
    monkeypatch.setattr(tracer, "write_trace", write_trace)
    monkeypatch.setattr(planner, "analyze", AsyncMock(return_value={
        "slots": {}, "missing_slots": [], "authorized": False,
    }))
    with Session() as db:
        ctx = AgentContext(db, SimpleNamespace(id=1), "trace-test")
        await graph.node_analyze(ctx)({"user_query": "hello"})
    assert order == ["drafts", "trace"]


async def test_cancelled_worker_finishes_before_session_cleanup():
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()

    def transaction():
        entered.set()
        assert release.wait(3), "test failed to release worker"
        committed.set()

    async def wait_for_worker():
        while not entered.is_set():
            await asyncio.sleep(0.001)

    task = asyncio.create_task(graph._worker_node(lambda state: transaction())({}))
    try:
        await asyncio.wait_for(wait_for_worker(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated disconnect/shutdown cancellation must also wait.
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert committed.is_set(), "request unwound while its Session was still in use"


async def test_cancelled_model_still_writes_failed_trace_in_worker(monkeypatch):
    entered = asyncio.Event()
    traces = []
    loop_thread = threading.get_ident()

    async def analyze(*args):
        entered.set()
        await asyncio.Event().wait()

    def write_trace(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        traces.append(kwargs)

    monkeypatch.setattr(planner, "analyze", analyze)
    monkeypatch.setattr(tracer, "write_trace", write_trace)
    ctx = AgentContext(object(), SimpleNamespace(id=1), "cancel-test")
    task = asyncio.create_task(graph.node_analyze(ctx)({}))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert traces[0]["status"] == "failed"
    assert "CancelledError" in traces[0]["output_text"]


async def test_expiry_scan_offloads_io_and_recovers_after_failure(monkeypatch):
    loop_thread = threading.get_ident()
    attempts = 0

    def expire():
        nonlocal attempts
        assert threading.get_ident() != loop_thread
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary DB outage")

    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(reservation_service, "expire_pending_reservations", expire)
    monkeypatch.setattr(reservation_service, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(asyncio.CancelledError):
        await reservation_service.run_expire_scan()
    assert attempts == 2
    assert sleep.await_count == 2


async def test_native_redis_path_does_not_use_sync_client(monkeypatch):
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_ENABLED", True)
    cache = llm_cache.LLMResponseCache()
    client = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(cache, "_aclient", lambda: client)
    sync = Mock(side_effect=AssertionError("sync Redis used on event loop"))
    monkeypatch.setattr(cache, "_client", sync)
    await cache.aput_text("key", '{"slots":{}}')
    client.get.return_value = client.set.call_args.args[1].encode()
    assert await cache.aget_text("key") == '{"slots":{}}'
    client.get.assert_awaited_once()
    client.set.assert_awaited_once()
    sync.assert_not_called()


async def test_cache_identity_does_not_lazy_load_expired_user(monkeypatch):
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_ENABLED", True)

    class User:
        expired = False
        id = 7

        @property
        def role(self):
            assert not self.expired, "cache key triggered a blocking ORM refresh"
            return "student"

    user = User()
    ctx = await run_sync(AgentContext, object(), user, "identity-test")
    user.expired = True
    assert llm_cache.LLMResponseCache().make_key(ctx, [], {})


@pytest.mark.parametrize("warmup_enabled", [False, True])
async def test_shutdown_drains_warmup_before_closing_model_workers(monkeypatch, warmup_enabled):
    from app import main

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    stopped = []

    def warmup():
        entered.set()
        assert release.wait(3)
        finished.set()

    def close():
        assert finished.is_set() == warmup_enabled
        assert len(stopped) == 2

    async def scanner():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(True)

    async def wait_until_entered():
        while not entered.is_set():
            await asyncio.sleep(0.001)

    monkeypatch.setattr(main.settings, "KB_WARMUP_ON_STARTUP", warmup_enabled)
    monkeypatch.setattr(main, "_log_outbound_status", lambda: None)
    monkeypatch.setattr(main.kb_service, "warmup", warmup)
    monkeypatch.setattr(main.kb_service, "close", close)
    monkeypatch.setattr(main.reservation_service, "run_expire_scan", scanner)
    monkeypatch.setattr(main.conversation_service, "run_cleanup_scan", scanner)
    monkeypatch.setattr(main.response_cache, "aclose", AsyncMock())
    monkeypatch.setattr(main.response_cache, "close", Mock())
    try:
        async with main.lifespan(main.app):
            if warmup_enabled:
                await asyncio.wait_for(wait_until_entered(), 2)
                # Shutdown begins while the loading worker is still busy.
                asyncio.get_running_loop().call_later(0.02, release.set)
            else:
                await asyncio.sleep(0)
    finally:
        release.set()
    main.response_cache.aclose.assert_awaited_once()
