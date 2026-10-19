"""HTTP outcomes, bounded labels, complete streams and protected Prometheus scrapes."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
import pytest

from app.api import ai
from app.common.exceptions import BusinessException, bussiness_excpetion_hadler
from app.common import metrics as monitoring
from app.database import get_db
from app.dependencies.auth import get_current_user


def sample(metrics, name, labels=None):
    return metrics.registry.get_sample_value(name, labels or {})


def make_app(**options):
    app = FastAPI()
    metrics = monitoring.install_metrics(app, **options)

    @app.get("/api/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    @app.get("/api/business")
    def business():
        raise BusinessException("private reason")

    @app.get("/api/forbidden")
    def forbidden():
        raise HTTPException(403)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("private failure")

    app.add_exception_handler(BusinessException, bussiness_excpetion_hadler)
    return app, metrics


def test_route_templates_hide_ids_queries_and_unknown_paths():
    app, metrics = make_app()
    client = TestClient(app)
    for ident in (1234567, 9876543):
        assert client.get(f"/api/items/{ident}?token=private-token").status_code == 200
    for path in ("/api/private-unknown-a", "/api/private-unknown-b"):
        assert client.get(path).status_code == 404
    labels = {"method": "GET", "route": "/api/items/{item_id}", "status": "200", "outcome": "success"}
    assert sample(metrics, "lab_agent_http_requests_total", labels) == 2
    labels.update(route="__unmatched_api__", status="404", outcome="http_error")
    assert sample(metrics, "lab_agent_http_requests_total", labels) == 2
    body = client.get("/metrics").text
    for secret in ("1234567", "9876543", "private-token", "private-unknown"):
        assert secret not in body
    assert sample(metrics, "lab_agent_http_requests_in_progress") == 0


@pytest.mark.parametrize("path,status,outcome", [
    ("/api/business", 200, "business_error"),
    ("/api/forbidden", 403, "http_error"),
    ("/api/boom", 500, "exception"),
    ("/api/items/not-an-int", 422, "http_error"),
])
def test_failures_count_once_even_when_http_is_200(path, status, outcome):
    app, metrics = make_app()
    response = TestClient(app, raise_server_exceptions=False).get(path)
    assert response.status_code == status
    route = "/api/items/{item_id}" if path.startswith("/api/items") else path
    assert sample(metrics, "lab_agent_http_requests_total",
                  {"method": "GET", "route": route, "status": str(status), "outcome": outcome}) == 1
    assert sample(metrics, "lab_agent_http_request_duration_seconds_count", {"method": "GET", "route": route}) == 1
    assert sample(metrics, "lab_agent_http_requests_in_progress") == 0


def test_metrics_endpoint_can_be_disabled_or_protected_without_recording_scrapes():
    app, metrics = make_app(token="private-token")
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers={"Authorization": "Bearer private-token"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    assert any(family.name == "lab_agent_http_requests_in_progress"
               for family in text_string_to_metric_families(response.text))
    assert "private-token" not in response.text
    assert list(metrics.requests.collect())[0].samples == []
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
    disabled, result = make_app(enabled=False)
    assert result is None and disabled.state.api_metrics is None
    assert TestClient(disabled).get("/metrics").status_code == 404


@pytest.mark.anyio
async def test_latency_and_in_progress_cover_the_whole_stream(monkeypatch):
    metrics = monitoring.APIMetrics()
    started, release = asyncio.Event(), asyncio.Event()
    ticks = iter((10.0, 18.0))
    monkeypatch.setattr(monitoring, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/stream")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        started.set()
        await release.wait()
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    async def send(message):
        pass

    labels = {"method": "POST", "route": "/api/stream"}
    task = asyncio.create_task(monitoring.HTTPMetricsMiddleware(app, metrics)(
        {"type": "http", "path": "/api/stream", "method": "POST"}, None, send))
    await started.wait()
    assert sample(metrics, "lab_agent_http_requests_in_progress") == 1
    assert sample(metrics, "lab_agent_http_request_duration_seconds_count", labels) is None
    release.set()
    await task
    assert sample(metrics, "lab_agent_http_request_duration_seconds_sum", labels) == 8
    assert sample(metrics, "lab_agent_http_requests_in_progress") == 0


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [False, True])
async def test_cancellation_preserves_http_status_and_cleans_gauge(headers):
    metrics = monitoring.APIMetrics()
    entered = asyncio.Event()

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/stream")
        if headers:
            await send({"type": "http.response.start", "status": 200})
        entered.set()
        await asyncio.Event().wait()

    async def send(message):
        pass

    task = asyncio.create_task(monitoring.HTTPMetricsMiddleware(app, metrics)(
        {"type": "http", "path": "/api/stream", "method": "GET"}, None, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sample(metrics, "lab_agent_http_requests_total", {"method": "GET", "route": "/api/stream",
                  "status": "200" if headers else "499", "outcome": "cancelled"}) == 1
    assert sample(metrics, "lab_agent_http_requests_in_progress") == 0


@pytest.mark.parametrize("events,outcome", [
    ([{"type": "done", "answer": "answer"}], "success"),
    ([{"type": "error", "message": "private failure"}], "agent_error"),
    ([{"type": "done", "answer": ""}], "agent_error"),
    ([{"type": "token", "content": "partial"}], "agent_error"),
    ([{"type": "node", "status": "end", "state": "failed"}, {"type": "done", "answer": "fallback"}], "agent_error"),
    ([{"type": "step", "status": "failed"}, {"type": "done", "answer": "fallback"}], "agent_error"),
])
def test_agent_sse_business_failures_are_visible_despite_http_200(monkeypatch, events, outcome):
    app = FastAPI()
    metrics = monitoring.install_metrics(app)
    app.include_router(ai.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)

    async def stream(*args):
        for event in events:
            yield event

    monkeypatch.setattr(ai.agent_service, "stream_agent", stream)
    response = TestClient(app).post("/api/ai/chat/stream", json={"messages": [{"role": "user", "content": "q"}]})
    assert response.status_code == 200
    assert sample(metrics, "lab_agent_http_requests_total", {"method": "POST", "route": "/api/ai/chat/stream",
                                                           "status": "200", "outcome": outcome}) == 1


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("state,outcome", [
    ({"final_response": "fallback", "error": "model unavailable"}, "agent_error"),
    ({"final_response": "fallback", "tool_results": [{"ok": False}]}, "agent_error"),
    ({"final_response": "  "}, "agent_error"),
    ({"final_response": "answer", "tool_results": [{"ok": True}]}, "success"),
])
def test_workflow_final_state_counts_degraded_answers_in_both_modes(monkeypatch, mode, state, outcome):
    from app.agent import graph

    class FakeGraph:
        async def ainvoke(self, *args, **kwargs):
            return state

        async def astream(self, *args, **kwargs):
            yield "values", state

    runner = graph.WorkflowRunner.__new__(graph.WorkflowRunner)
    runner.ctx = SimpleNamespace(conversation_id="private-conversation")
    runner.query, runner.state, runner.graph = "private query", {}, FakeGraph()
    saved = []
    monkeypatch.setattr(graph.memory, "append_turn", lambda *args: saved.append(args))
    app = FastAPI()
    metrics = monitoring.install_metrics(app)

    @app.get("/api/workflow")
    async def workflow():
        if mode == "invoke":
            return {"answer": await runner.ainvoke()}
        return {"events": [event async for event in runner.astream()]}

    response = TestClient(app).get("/api/workflow")
    assert response.status_code == 200
    assert saved == [(runner.ctx.conversation_id, runner.query, state["final_response"])]
    assert sample(metrics, "lab_agent_http_requests_total", {"method": "GET", "route": "/api/workflow",
                                                           "status": "200", "outcome": outcome}) == 1


@pytest.mark.anyio
async def test_failure_context_isolated_between_concurrent_requests_and_worker_threads():
    metrics = monitoring.APIMetrics()
    barrier = asyncio.Barrier(2)

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path=scope["path"])
        if scope["path"] == "/api/failed":
            await asyncio.to_thread(monitoring.mark_failure, None, "agent_error")
        await barrier.wait()
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        pass

    middleware = monitoring.HTTPMetricsMiddleware(app, metrics)
    monitoring.mark_failure(None, "agent_error")  # Outside HTTP: no-op.
    await asyncio.gather(*(middleware({"type": "http", "method": "GET", "path": path}, None, send)
                           for path in ("/api/failed", "/api/success")))
    assert monitoring._request_scope.get() is None
    for route, outcome in (("/api/failed", "agent_error"), ("/api/success", "success")):
        assert sample(metrics, "lab_agent_http_requests_total", {"method": "GET", "route": route,
                                                               "status": "200", "outcome": outcome}) == 1
