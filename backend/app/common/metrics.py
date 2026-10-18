"""Low-cardinality HTTP metrics with complete streaming request lifetimes."""

import asyncio
import secrets
import time

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FAILURE_KEY = "_lab_agent_api_failure"
FAILURES = frozenset({"business_error", "agent_error", "exception", "cancelled"})
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
BUCKETS = (.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 20, 30, 60, 120, 180, 300)


def mark_failure(scope: Scope, kind: str) -> None:
    if kind in FAILURES:
        scope[FAILURE_KEY] = kind


class APIMetrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "lab_agent_http_requests_total", "Completed HTTP requests, including failed/cancelled requests",
            ("method", "route", "status", "outcome"), registry=self.registry,
        )
        self.duration = Histogram(
            "lab_agent_http_request_duration_seconds", "Application request lifetime, including the full SSE stream",
            ("method", "route"), buckets=BUCKETS, registry=self.registry,
        )
        self.in_progress = Gauge(
            "lab_agent_http_requests_in_progress", "Requests currently executing (metrics and static/docs excluded)",
            registry=self.registry,
        )


class HTTPMetricsMiddleware:
    def __init__(self, app: ASGIApp, metrics: APIMetrics):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (scope["type"] != "http" or path in {"/metrics", "/metrics/", "/docs", "/redoc", "/openapi.json"}
                or path.startswith(("/uploads/", "/docs/"))):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = None
        complete = False
        self.metrics.in_progress.inc()

        async def observed_send(message: Message):
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            mark_failure(scope, "cancelled")
            raise
        except BaseException:
            mark_failure(scope, "exception")
            raise
        finally:
            method = scope.get("method", "OTHER")
            if method not in METHODS:
                method = "OTHER"
            # Resolve AFTER routing: concrete IDs/query strings never become labels.
            route = getattr(scope.get("route"), "path", None)
            if not route:
                route = "__unmatched_api__" if path.startswith("/api/") else "__unmatched__"
            outcome = scope.get(FAILURE_KEY)
            if outcome not in FAILURES:
                outcome = "http_error" if status is not None and status >= 400 else "success"
                if not complete:
                    outcome = "cancelled"
            if status is None:
                status = 499 if outcome == "cancelled" else 500
            self.metrics.requests.labels(method, route, str(status), outcome).inc()
            self.metrics.duration.labels(method, route).observe(time.perf_counter() - started)
            self.metrics.in_progress.dec()


def install_metrics(app: FastAPI, *, enabled: bool = True, token: str = "") -> APIMetrics | None:
    if not enabled:
        app.state.api_metrics = None
        return None
    metrics = APIMetrics()
    app.state.api_metrics = metrics
    app.add_middleware(HTTPMetricsMiddleware, metrics=metrics)

    def scrape(request: Request):
        if token and not secrets.compare_digest(request.headers.get("authorization", "").encode(),
                                               f"Bearer {token}".encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    # A sync route puts encoding in FastAPI's thread pool and avoids /metrics redirects.
    app.add_api_route("/metrics", scrape, methods=["GET", "HEAD"], include_in_schema=False)
    return metrics
