"""Cache correctness across identity, tool protocol, TTL and Redis failures."""

import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest
from redis.exceptions import ConnectionError

from app.agent import llm_cache, planner


class MemoryRedis:
    def __init__(self):
        self.entries = {}
        self.now = 0

    def get(self, key):
        raw, deadline = self.entries.get(key, (None, 0))
        return raw if deadline > self.now else None

    def set(self, key, value, ex):
        self.entries[key] = (value.encode(), self.now + ex)

    def close(self):
        pass


class AsyncMemoryRedis:
    def __init__(self, client):
        self.client = client

    async def get(self, key):
        return self.client.get(key)

    async def set(self, key, value, ex):
        return self.client.set(key, value, ex)


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_TTL_SECONDS", 60)
    result = llm_cache.LLMResponseCache()
    result._redis = MemoryRedis()
    monkeypatch.setattr(result, "_aclient", lambda: (
        AsyncMemoryRedis(result._redis) if llm_cache.settings.LLM_CACHE_ENABLED
        and llm_cache.time.monotonic() >= result._unavailable_until else None
    ))
    monkeypatch.setattr(planner, "response_cache", result)
    return result


def context(user_id=7, role="student"):
    return SimpleNamespace(user=SimpleNamespace(id=user_id, role=role), llm_failures=0)


def test_cache_key_is_scoped_and_contains_no_input_or_credentials(cache, monkeypatch):
    messages = [HumanMessage(content="private question")]
    base = cache.make_key(context(), messages, {"tools": [], "temperature": 0})
    assert base and "private" not in base and len(base.rsplit(":", 1)[1]) == 64
    assert cache.make_key(context(8), messages, {"tools": [], "temperature": 0}) != base
    assert cache.make_key(context(role="admin"), messages, {"tools": [], "temperature": 0}) != base
    assert cache.make_key(context(), messages, {"tools": ["new"], "temperature": 0}) != base
    assert cache.make_key(context(), messages, {"tools": [], "temperature": 1}) != base
    for field in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DATABASE_URL"):
        with monkeypatch.context() as patch:
            patch.setattr(llm_cache.settings, field, "different")
            assert cache.make_key(context(), messages, {"tools": [], "temperature": 0}) != base
    assert cache.make_key(SimpleNamespace(), messages, {}) is None


def test_message_metadata_is_ignored_but_tool_observations_change_key(cache):
    original = AIMessage(content="result", id="run-1", response_metadata={"request": "one"})
    second = original.model_copy(update={"id": "run-2", "response_metadata": {}})
    assert cache.make_key(context(), [original], {}) == cache.make_key(context(), [second], {})
    a = ToolMessage(content="available", tool_call_id="call1")
    b = ToolMessage(content="occupied", tool_call_id="call1")
    assert cache.make_key(context(), [a], {}) != cache.make_key(context(), [b], {})


@pytest.mark.anyio
async def test_structured_hit_and_expiry_skip_then_resume_provider(cache, monkeypatch):
    invoke = AsyncMock(return_value='{"slots": {}, "authorized": false}')
    monkeypatch.setattr(planner, "_invoke", invoke)
    ctx = context()
    first = await planner._ask(ctx, "same request")
    ctx.llm_failures = 1
    assert await planner._ask(ctx, "same request") == first
    assert ctx.llm_failures == 0
    assert invoke.call_count == 1
    cache._redis.now = 61
    await planner._ask(ctx, "same request")
    assert invoke.call_count == 2
    await planner._ask(context(8), "same request")
    assert invoke.call_count == 3


def test_disabled_cache_never_connects(cache, monkeypatch):
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_ENABLED", False)
    client = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(cache, "_client", client)
    assert cache.make_key(context(), [HumanMessage(content="q")], {}) is None
    assert cache.get_text(None) is None
    cache.put_text(None, "response")
    client.assert_not_called()


@pytest.mark.anyio
async def test_bad_model_json_and_provider_errors_are_not_cached(cache, monkeypatch):
    invoke = AsyncMock(return_value="not JSON")
    monkeypatch.setattr(planner, "_invoke", invoke)
    await planner._ask(context(), "q")
    await planner._ask(context(), "q")
    assert invoke.call_count == 2
    assert not cache._redis.entries
    invoke.side_effect = planner.BusinessException(message="offline")
    with pytest.raises(planner.BusinessException):
        await planner._ask(context(), "q")
    assert not cache._redis.entries


def test_corrupt_or_large_cache_data_falls_back(cache, monkeypatch):
    key = "test-key"
    cache._redis.entries[key] = (b"invalid JSON", 60)
    assert cache.get_message(key) is None
    cache._unavailable_until = 0
    cache.put_message(key, AIMessage(content="x" * (llm_cache.MAX_ENTRY_BYTES + 1)))
    assert cache._redis.entries[key][0] == b"invalid JSON"
    cache._redis.entries[key] = (json.dumps({"format": 1, "kind": "ai", "value": {"type": "system"}}).encode(), 60)
    assert cache.get_message(key) is None


@pytest.mark.anyio
async def test_redis_get_failure_does_not_change_llm_result(cache, monkeypatch):
    client = Mock()
    client.get.side_effect = ConnectionError("secret-redis-url")
    cache._redis = client
    invoke = AsyncMock(return_value='{"slots":{}}')
    monkeypatch.setattr(planner, "_invoke", invoke)
    assert await planner._ask(context(), "q") == '{"slots":{}}'
    assert await planner._ask(context(), "q") == '{"slots":{}}'
    assert client.get.call_count == 1  # Cooldown avoids per-request Redis waits.
    client.set.assert_not_called()
    assert invoke.call_count == 2


@pytest.mark.anyio
async def test_redis_set_failure_does_not_change_llm_result(cache, monkeypatch):
    client = Mock()
    client.get.return_value = None
    client.set.side_effect = ConnectionError("offline")
    cache._redis = client
    monkeypatch.setattr(planner, "_invoke", AsyncMock(return_value='{"slots":{}}'))
    assert await planner._ask(context(), "q") == '{"slots":{}}'


def test_async_tool_response_cache_preserves_protocol_without_token_charges(cache):
    async def run():
        message = AIMessage(content="", tool_calls=[
            {"name": "list_open_labs", "args": {}, "id": "call1", "type": "tool_call"}
        ], id="provider-run", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=message))
        key = cache.make_key(context(), [HumanMessage(content="q")], {})
        await planner._ainvoke(context(), llm, [], cache_key=key)
        a = await planner._ainvoke(context(), llm, [], cache_key=key)
        b = await planner._ainvoke(context(), llm, [], cache_key=key)
        assert llm.ainvoke.await_count == 1
        assert a.tool_calls == message.tool_calls
        assert a.id != b.id != message.id
        assert a.usage_metadata is None
        assert a.response_metadata["llm_cache_hit"]
    asyncio.run(run())


def test_agent_step_hits_read_cache_and_bypasses_write_turns(cache, monkeypatch):
    async def run():
        llm = Mock()
        llm.bind_tools.return_value = llm
        llm.ainvoke = AsyncMock(return_value=AIMessage(content="response"))
        monkeypatch.setattr(planner, "get_llm", lambda: llm)
        state = {"today": "2026-10-18", "user_query": "有哪些实验室", "history": []}
        await planner.agent_step(context(), state)
        await planner.agent_step(context(), state)
        assert llm.ainvoke.await_count == 1
        await planner.agent_step(context(), state, tool_choice="none")
        assert llm.ainvoke.await_count == 2
        writable = {**state, "allowed_write_tools": ["create_reservation"]}
        await planner.agent_step(context(), writable)
        await planner.agent_step(context(), writable)
        assert llm.ainvoke.await_count == 4
    asyncio.run(run())


def test_write_tool_calls_are_never_replayed_from_cache(cache):
    async def run():
        key = cache.make_key(context(), [HumanMessage(content="q")], {})
        write = AIMessage(content="", tool_calls=[
            {"name": "create_reservation", "args": {}, "id": "call", "type": "tool_call"}
        ])
        cache.put_message(key, write)  # Simulate an old/incorrect cache entry.
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="ask user")))
        result = await planner._ainvoke(context(), llm, [], cache_key=key)
        assert result.content == "ask user"
        assert llm.ainvoke.await_count == 1
    asyncio.run(run())


def test_invalid_tool_calls_are_not_stored(cache):
    message = AIMessage(content="", invalid_tool_calls=[
        {"name": "lookup", "args": "bad", "id": "call", "error": "bad args", "type": "invalid_tool_call"}
    ])
    cache.put_message("key", message)
    assert not cache._redis.entries


def test_cached_llm_decisions_still_query_fresh_tool_data(cache, monkeypatch):
    from app.agent import graph, tools

    @contextmanager
    def trace(*args, **kwargs):
        yield {"output": "", "status": "success"}

    async def run():
        invoke = AsyncMock(return_value='{"slots":{},"missing_slots":[],"plan_needed":false}')
        monkeypatch.setattr(planner, "_invoke", invoke)
        llm = Mock()
        llm.bind_tools.return_value = llm

        async def reply(messages):
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content=messages[-1].content)
            return AIMessage(content="", tool_calls=[
                {"name": "list_open_labs", "args": {}, "id": "lookup", "type": "tool_call"}
            ])

        llm.ainvoke = AsyncMock(side_effect=reply)
        monkeypatch.setattr(planner, "get_llm", lambda: llm)
        monkeypatch.setattr(graph, "trace_node", trace)
        monkeypatch.setattr(graph, "write_tool_execution", Mock())
        monkeypatch.setattr(graph.memory, "load_memory_context", lambda *_: "")
        monkeypatch.setattr(graph.memory, "get_history", lambda *_: [])
        monkeypatch.setattr(graph.memory, "append_turn", Mock())
        run_tool = Mock(return_value={"ok": True, "labs": ["实验室A"]})
        monkeypatch.setattr(tools, "run_tool", run_tool)

        async def turn(index):
            return await graph.WorkflowRunner(
                object(), context().user, f"cache-test-{index}", "有哪些开放实验室？", history=[]
            ).ainvoke()

        first = await turn(1)
        assert await turn(2) == first
        assert invoke.call_count == 1
        assert llm.ainvoke.await_count == 2
        assert run_tool.call_count == 2
        run_tool.return_value = {"ok": True, "labs": ["实验室B"]}
        assert "实验室B" in await turn(3)
        assert llm.ainvoke.await_count == 3
        assert run_tool.call_count == 3

    asyncio.run(run())
