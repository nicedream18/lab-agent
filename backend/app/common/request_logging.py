"""Request correlation and JSON completion records, including full SSE lifetimes."""

import asyncio
import re
import time
from uuid import uuid4

from loguru import logger
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.common.metrics import FAILURE_KEY

REQUEST_ID_KEY = "_lab_agent_request_id"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_QUIET_PATHS = frozenset({"/", "/metrics", "/metrics/", "/healthz", "/docs", "/redoc", "/openapi.json"})


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, access_enabled: bool = True):
        self.app = app
        self.access_enabled = access_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        candidate = next((value.decode("latin-1") for name, value in scope.get("headers", [])
                          if name.lower() == b"x-request-id"), "")
        request_id = candidate if _REQUEST_ID.fullmatch(candidate) else uuid4().hex
        scope[REQUEST_ID_KEY] = request_id
        started = time.perf_counter_ns()
        status = None
        complete = False
        error = None
        cancelled = False

        async def observed_send(message: Message):
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", request_id.encode("ascii"))]}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        with logger.contextualize(request_id=request_id):
            try:
                await self.app(scope, receive, observed_send)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as exc:
                error = exc
                raise
            finally:
                outcome = scope.get(FAILURE_KEY)
                if error is not None:
                    outcome = "exception"
                elif cancelled or not complete:
                    outcome = "cancelled"
                elif not outcome:
                    outcome = "http_error" if status is not None and status >= 400 else "success"
                if status is None:
                    status = 499 if outcome == "cancelled" else 500
                path = scope.get("path", "")
                quiet = path in _QUIET_PATHS or path.startswith(("/uploads/", "/docs/"))
                if error is not None or self.access_enabled and (not quiet or outcome != "success"):
                    route = getattr(scope.get("route"), "path", "__unmatched__")
                    level = "ERROR" if error is not None or status >= 500 else "WARNING" if outcome != "success" else "INFO"
                    logger.bind(event_action="http_request", event_duration=time.perf_counter_ns() - started,
                                event_outcome="success" if outcome == "success" else "failure",
                                http_method=scope.get("method", "OTHER"), http_status=status,
                                route=route, outcome=outcome).opt(exception=error).log(level, "HTTP request completed")
