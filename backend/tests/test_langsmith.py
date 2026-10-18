"""Exercise real LangGraph + LangSmith callbacks; intercept only SDK transport."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith import Client, get_current_run_tree, trace
from pydantic import SecretStr
import pytest

from app.agent import graph, observability, planner, tools
from app.agent.state import AgentContext
from app.common.async_io import run_sync
from app.services import inference_service, kb_service
from scripts.analyze_langsmith import summarize


@pytest.fixture
def recorded(monkeypatch):
    runs = {}

    def create(_self, run, **kwargs):
        runs.setdefault(str(run["id"]), {}).update(deepcopy(run))

    def update(_self, run, **kwargs):
        runs.setdefault(str(run["id"]), {}).update(deepcopy(run))

    monkeypatch.setattr(Client, "_create_run", create)
    monkeypatch.setattr(Client, "_update_run", update)
    client = Client(api_url="http://127.0.0.1:9", api_key="offline-tests-only",
                    auto_batch_tracing=False, hide_inputs=True, hide_outputs=True, info={})
    monkeypatch.setattr(observability, "_client", client)
    monkeypatch.setattr(observability.settings, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(observability.settings, "LANGSMITH_API_KEY", SecretStr("offline-tests-only"))
    monkeypatch.setattr(observability.settings, "LANGSMITH_SAMPLING_RATE", 1)
    monkeypatch.setattr(observability.settings, "LANGSMITH_PROJECT", "offline-trace-test")
    yield runs
    wait_for_all_tracers()
    client.close(timeout=0)


@contextmanager
def no_db_trace(*args, **kwargs):
    yield {"output": "", "status": "success"}


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, *args, **kwargs):
        return self


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_graph_tree_and_privacy(recorded, monkeypatch, streaming):
    model = ScriptedModel(responses=[
        AIMessage(content='{"slots":{},"missing_slots":[],"authorized":false,"plan_needed":false}'),
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "list_open_labs", "args": {}}]),
        AIMessage(content="计算机实验室开放。"),
    ])
    monkeypatch.setattr(planner, "get_llm", lambda: model)
    monkeypatch.setattr(graph, "trace_node", no_db_trace)
    monkeypatch.setattr(graph, "write_tool_execution", lambda *a, **kw: None)
    monkeypatch.setattr(graph.memory, "load_memory_context", lambda *a, **kw: "")
    monkeypatch.setattr(graph.memory, "append_turn", lambda *a, **kw: None)
    monkeypatch.setattr(tools.TOOL_REGISTRY["list_open_labs"], "func",
                        lambda ctx, **kw: {"ok": True, "labs": []})
    runner = graph.WorkflowRunner(object(), SimpleNamespace(id=7, role="student"),
                                  "trace-test", "有哪些实验室开放？", history=[])
    if streaming:
        events = [event async for event in runner.astream()]
        assert events[-1]["type"] == "done"
        assert "开放" in events[-1]["answer"]
    else:
        assert "开放" in await runner.ainvoke()
    await run_sync(wait_for_all_tracers)
    runs = list(recorded.values())
    report = summarize(runs)
    assert report["root_count"] == 1
    assert report["orphan_count"] == report["unfinished_count"] == 0
    assert report["llm_calls"] == 3
    root = next(run for run in runs if not run.get("parent_run_id"))
    assert root["name"] == "lab-agent"
    assert root["session_name"] == "offline-trace-test"
    assert root["extra"]["metadata"]["thread_id"] == "trace-test"
    tool = next(run for run in runs if run["run_type"] == "tool")
    assert tool["name"] == "list_open_labs"
    parent = recorded[str(tool["parent_run_id"])]
    assert parent["name"] == "execute"
    assert all(not run.get("inputs") and not run.get("outputs") for run in runs)
    assert get_current_run_tree() is None


@pytest.mark.anyio
async def test_cache_hit_and_retry_metadata(recorded, monkeypatch):
    ctx = SimpleNamespace(llm_failures=0)
    monkeypatch.setattr(planner.response_cache, "make_key", lambda *a: None)

    async def hit(*args):
        return '{"slots":{}}'

    monkeypatch.setattr(planner.response_cache, "aget_text", hit)
    with observability.request_tracing("cached", "invoke", 60):
        await planner._ask(ctx, "private question")
    cached = next(run for run in recorded.values() if run["name"] == "llm.structured")
    assert cached["extra"]["metadata"]["cache_hit"] is True
    assert cached["extra"]["metadata"]["attempts"] == 0

    async def miss(*args):
        return None

    monkeypatch.setattr(planner.response_cache, "aget_message", miss)
    monkeypatch.setattr(planner, "RETRY_DELAYS", (0, 0))
    calls = 0

    class TransientModel:
        async def ainvoke(self, messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("upstream 503")
            return AIMessage(content="ok")

    with observability.request_tracing("retry", "invoke", 60):
        await planner._ainvoke(ctx, TransientModel(), [HumanMessage(content="private")])
    run = next(run for run in recorded.values() if run["name"] == "llm.agent")
    assert run["extra"]["metadata"]["attempts"] == 2
    assert run["extra"]["metadata"]["cache_hit"] is False


@pytest.mark.anyio
async def test_concurrent_requests_keep_separate_roots(recorded):
    async def request(conversation):
        with observability.request_tracing(conversation, "invoke", 60):
            @observability.observe_llm("request-test")
            async def work():
                await asyncio.sleep(0)
                return str(get_current_run_tree().trace_id)

            return await work()

    a, b = await asyncio.gather(request("a"), request("b"))
    assert a != b
    assert get_current_run_tree() is None


@pytest.mark.anyio
async def test_disabled_and_zero_sampling_override_ambient_tracing(recorded, monkeypatch):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    for enabled, rate in [(False, 1), (True, 0)]:
        monkeypatch.setattr(observability.settings, "LANGSMITH_TRACING", enabled)
        monkeypatch.setattr(observability.settings, "LANGSMITH_SAMPLING_RATE", rate)
        with observability.request_tracing("off", "invoke", 60):
            await planner._ainvoke(SimpleNamespace(llm_failures=0),
                                  ScriptedModel(responses=[AIMessage(content="ok")]), [])
    await run_sync(wait_for_all_tracers)
    assert not recorded


def test_missing_key_and_failed_initialization(monkeypatch):
    monkeypatch.setattr(observability.settings, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(observability.settings, "LANGSMITH_API_KEY", SecretStr(""))
    assert observability.get_client() is None
    monkeypatch.setattr(observability.settings, "LANGSMITH_API_KEY", SecretStr("dummy"))
    monkeypatch.setattr(observability, "_client", None)

    def broken(**kwargs):
        raise ValueError("bad config")

    monkeypatch.setattr(observability, "Client", broken)
    assert observability.get_client() is None


def test_client_config_and_shutdown_are_explicit(monkeypatch):
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(observability, "Client", factory)
    monkeypatch.setattr(observability.settings, "LANGSMITH_TRACING", True)
    monkeypatch.setattr(observability.settings, "LANGSMITH_API_KEY", SecretStr("dummy"))
    assert observability.get_client() is client
    assert observability.get_client() is client
    factory.assert_called_once()
    options = factory.call_args.kwargs
    assert options["hide_inputs"] is options["hide_outputs"] is True
    assert options["tracing_sampling_rate"] == 1
    client.close.side_effect = RuntimeError("offline uploader")
    observability.close()
    client.close.assert_called_once_with(timeout=5)
    assert observability._client is None
    observability.close()


def test_summary_subtracts_overlapping_children_once():
    def run(name, start, end, parent=None):
        return {"id": name, "name": name, "run_type": "chain", "parent_run_id": parent,
                "start_time": f"2026-10-18T00:00:0{start}+00:00",
                "end_time": f"2026-10-18T00:00:0{end}+00:00"}

    result = summarize([run("root", 0, 5), run("a", 1, 3, "root"), run("b", 2, 4, "root")])
    assert result["root_latency_ms"] == 5000
    assert result["runs"][0]["self_ms"] == 2000


def test_summary_counts_inherited_metadata_only_on_helpers():
    runs = [{"id": name, "name": name, "run_type": kind,
             "extra": {"metadata": {"cache_hit": True, "attempts": 2}}}
            for name, kind in [("lab-agent", "chain"), ("llm.agent", "chain"),
                               ("ChatOpenAI", "llm")]]
    result = summarize(runs)
    assert result["cache_hits"] == result["retries"] == 1


@pytest.mark.anyio
async def test_rag_spans_cross_worker_and_rerank_failure_is_visible(recorded, monkeypatch):
    batcher = SimpleNamespace(batch_size=8, map=lambda texts: [[1.0] for _ in texts])
    embedding = SimpleNamespace(batcher=batcher)

    class Collection:
        def count(self):
            return 1

        def query(self, query_texts, n_results):
            inference_service.BatchedEmbeddingFunction.__call__(embedding, query_texts)
            return {"documents": [["禁止携带食物"]]}

    def broken_rerank(pairs):
        raise RuntimeError("offline model unavailable")

    monkeypatch.setattr(kb_service, "get_collection", lambda: Collection())
    monkeypatch.setattr(kb_service.settings, "KB_RERANKER_ENABLED", True)
    monkeypatch.setattr(inference_service, "_rerank", broken_rerank)
    ctx = AgentContext(object(), SimpleNamespace(id=7), "rag")
    with observability.request_tracing("rag", "invoke", 60):
        with trace("rag-test"):
            result = await run_sync(tools.run_tool, ctx, "search_lab_docs", {"query": "能带食物吗"})
    assert result["ok"] is True
    runs = list(recorded.values())
    by_name = {run["name"]: run for run in runs}
    assert by_name["kb.rerank"]["error"]
    assert not by_name["kb.search"].get("error")
    assert str(by_name["kb.search"]["parent_run_id"]) == str(by_name["search_lab_docs"]["id"])
    assert str(by_name["kb.embedding"]["parent_run_id"]) == str(by_name["kb.recall"]["id"])
    assert summarize(runs)["orphan_count"] == 0


@pytest.mark.anyio
async def test_tool_business_failure_marks_span_without_raising(recorded):
    ctx = AgentContext(object(), SimpleNamespace(id=7), "tool-failure")
    with observability.request_tracing("tool-failure", "invoke", 60):
        with trace("tool-test"):
            result = await run_sync(tools.run_tool, ctx, "unknown-tool", {})
    assert result["ok"] is False
    tool = next(run for run in recorded.values() if run["run_type"] == "tool")
    assert tool["error"] == "tool_returned_failure"
    assert tool["extra"]["metadata"]["ok"] is False


@pytest.mark.anyio
async def test_stream_close_finishes_root_and_resets_context(recorded, monkeypatch):
    monkeypatch.setattr(graph.memory, "load_memory_context", lambda *a, **kw: "")
    runner = graph.WorkflowRunner(object(), SimpleNamespace(id=7), "close-test", "你好", history=[])
    release = asyncio.Event()

    async def wait(state):
        graph.get_stream_writer()({"type": "status", "message": "waiting"})
        await release.wait()
        return {}

    from langgraph.graph import END, START, StateGraph
    workflow = StateGraph(dict)
    workflow.add_node("wait", wait)
    workflow.add_edge(START, "wait")
    workflow.add_edge("wait", END)
    runner.graph = workflow.compile()
    stream = runner.astream()
    try:
        assert (await anext(stream))["type"] == "status"
    finally:
        await stream.aclose()
    await run_sync(wait_for_all_tracers)
    assert summarize(list(recorded.values()))["unfinished_count"] == 0
    assert get_current_run_tree() is None
