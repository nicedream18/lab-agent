"""Real Redis round trips. CI provides TEST_REDIS_URL; local helper starts Redis."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr
import pytest
from redis import Redis

from app.agent import llm_cache, planner


@pytest.fixture
def real_cache(monkeypatch):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("set TEST_REDIS_URL or run scripts/check_redis_cache.py")
    prefix = f"lab-agent:test:{uuid.uuid4().hex}"
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_ENABLED", True)
    monkeypatch.setattr(llm_cache.settings, "REDIS_URL", SecretStr(url))
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_NAMESPACE", prefix)
    monkeypatch.setattr(llm_cache.settings, "LLM_CACHE_TTL_SECONDS", 60)
    client = Redis.from_url(url)
    assert client.ping()
    cache = llm_cache.LLMResponseCache()
    monkeypatch.setattr(planner, "response_cache", cache)
    try:
        yield cache, client
    finally:
        # Never FLUSHDB; CI/dev instances may be shared by other tests.
        for key in client.scan_iter(match=prefix + ":*"):
            client.delete(key)
        cache.close()
        client.close()


def ctx():
    return SimpleNamespace(user=SimpleNamespace(id=7, role="student"), llm_failures=0)


@pytest.mark.anyio
async def test_real_redis_hit_ttl_and_invalidation(real_cache, monkeypatch):
    cache, client = real_cache
    provider = AsyncMock(return_value='{"slots":{}}')
    monkeypatch.setattr(planner, "_invoke", provider)
    await planner._ask(ctx(), "same question")
    await planner._ask(ctx(), "same question")
    assert provider.call_count == 1
    key = cache.make_key(ctx(), [HumanMessage(content="same question")],
                         {"stage": "structured", **planner._generation_options()})
    assert 0 < client.ttl(key) <= 60
    # Expire on the server; the following request must contact the provider.
    client.expire(key, 0)
    await planner._ask(ctx(), "same question")
    assert provider.call_count == 2
    await cache.aclose()


def test_real_async_response_round_trip(real_cache):
    cache, client = real_cache

    async def run():
        provider = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
            content="", tool_calls=[{"name": "list_open_labs", "args": {}, "id": "call1"}]
        )))
        key = cache.make_key(ctx(), [HumanMessage(content="question")], {"stage": "agent"})
        first = await planner._ainvoke(ctx(), provider, [], cache_key=key)
        cached = await planner._ainvoke(ctx(), provider, [], cache_key=key)
        assert provider.ainvoke.await_count == 1
        assert first.tool_calls == cached.tool_calls
        assert cached.response_metadata["llm_cache_hit"]
        assert client.ttl(key) > 0
        await cache.aclose()

    asyncio.run(run())
