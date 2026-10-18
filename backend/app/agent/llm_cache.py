"""Exact LLM response cache. Optional Redis failures never fail an Agent turn."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any
import uuid

from langchain_core.messages import AIMessage, messages_to_dict
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.config import settings

logger = logging.getLogger(__name__)
MAX_ENTRY_BYTES = 128 * 1024
FAILURE_COOLDOWN_SECONDS = 5


class LLMResponseCache:
    def __init__(self):
        self._redis: Redis | None = None
        # Each pool belongs to one event loop; lifespan closes the current pool.
        self._async_clients: dict[asyncio.AbstractEventLoop, AsyncRedis] = {}
        self._lock = threading.Lock()
        self._unavailable_until = 0.0

    def make_key(self, ctx: Any, messages: list, options: dict) -> str | None:
        if not settings.LLM_CACHE_ENABLED:
            return None
        identity = getattr(ctx, "cache_identity", None)
        if identity is None:
            user = getattr(ctx, "user", None)
            identity = {"id": getattr(user, "id", None), "role": getattr(user, "role", "")}
        user_id = identity.get("id")
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None  # Never share a response without an authenticated scope.
        try:
            serialized = messages_to_dict(messages)
            for message in serialized:
                # SDK run IDs and token accounting do not change model input.
                for field in ("id", "response_metadata", "usage_metadata"):
                    message["data"].pop(field, None)
            payload = {
                "format": 1,
                "user": identity,
                "database": hashlib.sha256(settings.DATABASE_URL.encode()).hexdigest(),
                "provider": settings.LLM_BASE_URL,
                "credential": hashlib.sha256(settings.LLM_API_KEY.encode()).hexdigest(),
                "model": settings.LLM_MODEL,
                "messages": serialized,
                "options": options,
            }
            canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), allow_nan=False)
            digest = hmac.new(settings.JWT_SECRET_KEY.encode(), canonical.encode(),
                              hashlib.sha256).hexdigest()
            return f"{settings.LLM_CACHE_NAMESPACE}:{digest}"
        except (TypeError, ValueError, AttributeError):
            return None

    def _client(self) -> Redis | None:
        if not settings.LLM_CACHE_ENABLED or time.monotonic() < self._unavailable_until:
            return None
        with self._lock:
            if self._redis is None:
                self._redis = Redis.from_url(
                    settings.REDIS_URL.get_secret_value(),
                    socket_connect_timeout=settings.LLM_CACHE_TIMEOUT,
                    socket_timeout=settings.LLM_CACHE_TIMEOUT,
                    retry=Retry(NoBackoff(), 0),
                    max_connections=32,
                    protocol=2,
                )
            return self._redis

    def _failed(self, exc: Exception) -> None:
        self._unavailable_until = time.monotonic() + FAILURE_COOLDOWN_SECONDS
        # Do not log exception strings: URLs may contain Redis credentials.
        logger.warning("LLM cache bypassed for %ss (%s)",
                       FAILURE_COOLDOWN_SECONDS, type(exc).__name__)

    def _aclient(self) -> AsyncRedis | None:
        if not settings.LLM_CACHE_ENABLED or time.monotonic() < self._unavailable_until:
            return None
        loop = asyncio.get_running_loop()
        if loop not in self._async_clients:
            self._async_clients[loop] = AsyncRedis.from_url(
                settings.REDIS_URL.get_secret_value(),
                socket_connect_timeout=settings.LLM_CACHE_TIMEOUT,
                socket_timeout=settings.LLM_CACHE_TIMEOUT,
                retry=AsyncRetry(NoBackoff(), 0),
                max_connections=32,
                protocol=2,
            )
        return self._async_clients[loop]

    @staticmethod
    def _decode(raw: bytes | None, kind: str) -> Any:
        if raw is None or len(raw) > MAX_ENTRY_BYTES:
            return None
        entry = json.loads(raw)
        if not isinstance(entry, dict) or entry.get("format") != 1 or entry.get("kind") != kind:
            return None
        return entry.get("value")

    @staticmethod
    def _encode(kind: str, value: Any) -> str | None:
        raw = json.dumps({"format": 1, "kind": kind, "value": value},
                         ensure_ascii=False, allow_nan=False)
        return raw if len(raw.encode()) <= MAX_ENTRY_BYTES else None

    def _get(self, key: str | None, kind: str) -> Any:
        if key is None:
            return None
        try:
            client = self._client()
            if client is None:
                return None
            return self._decode(client.get(key), kind)
        except Exception as exc:
            self._failed(exc)
            return None

    def _put(self, key: str | None, kind: str, value: Any) -> None:
        if key is None:
            return
        try:
            raw = self._encode(kind, value)
            if raw is None:
                return
            client = self._client()
            if client is not None:
                client.set(key, raw, ex=settings.LLM_CACHE_TTL_SECONDS)
        except Exception as exc:
            self._failed(exc)

    def get_text(self, key: str | None) -> str | None:
        value = self._get(key, "text")
        return value if isinstance(value, str) and value.strip() else None

    def put_text(self, key: str | None, value: str) -> None:
        if isinstance(value, str) and value.strip():
            self._put(key, "text", value)

    def get_message(self, key: str | None) -> AIMessage | None:
        return self._message(self._get(key, "ai"))

    @staticmethod
    def _message(value: Any) -> AIMessage | None:
        if not isinstance(value, dict):
            return None
        try:
            message = AIMessage.model_validate(value)
            if message.type != "ai" or message.invalid_tool_calls:
                return None
            if not message.content and not message.tool_calls:
                return None
            # Fresh graph identity avoids add_messages replacing an older reply.
            message.id = uuid.uuid4().hex
            message.usage_metadata = None
            message.response_metadata = {"llm_cache_hit": True}
            return message
        except (TypeError, ValueError):
            return None

    def put_message(self, key: str | None, message: Any) -> None:
        value = self._message_value(message)
        if value is not None:
            self._put(key, "ai", value)

    @staticmethod
    def _message_value(message: Any) -> dict | None:
        if not isinstance(message, AIMessage) or message.invalid_tool_calls:
            return
        if not message.content and not message.tool_calls:
            return
        try:
            return message.model_dump(mode="json", exclude={"id", "response_metadata", "usage_metadata"})
        except (TypeError, ValueError):
            return

    async def _aget(self, key: str | None, kind: str) -> Any:
        if key is None:
            return None
        try:
            client = self._aclient()
            if client is not None:
                return self._decode(await client.get(key), kind)
        except Exception as exc:
            self._failed(exc)

    async def _aput(self, key: str | None, kind: str, value: Any) -> None:
        if key is None:
            return
        try:
            raw = self._encode(kind, value)
            if raw is None:
                return
            client = self._aclient()
            if client is not None:
                await client.set(key, raw, ex=settings.LLM_CACHE_TTL_SECONDS)
        except Exception as exc:
            self._failed(exc)

    async def aget_text(self, key: str | None) -> str | None:
        value = await self._aget(key, "text")
        return value if isinstance(value, str) and value.strip() else None

    async def aput_text(self, key: str | None, value: str) -> None:
        if isinstance(value, str) and value.strip():
            await self._aput(key, "text", value)

    async def aget_message(self, key: str | None) -> AIMessage | None:
        return self._message(await self._aget(key, "ai"))

    async def aput_message(self, key: str | None, message: Any) -> None:
        value = self._message_value(message)
        if value is not None:
            await self._aput(key, "ai", value)

    async def aclose(self) -> None:
        client = self._async_clients.pop(asyncio.get_running_loop(), None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Failed to close async LLM cache pool")

    def close(self) -> None:
        with self._lock:
            client, self._redis = self._redis, None
            self._unavailable_until = 0.0
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("Failed to close LLM cache pool")


response_cache = LLMResponseCache()
