"""JSON framing, redaction, stdlib interoperability and request context isolation."""

import asyncio
import io
import json
import logging
from types import SimpleNamespace
import warnings

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr
import pytest

from app.common import request_logging
from app.common.exceptions import BusinessException, bussiness_excpetion_hadler, global_excpetion_hadler
from app.common.logging_config import configure_logging, flush_logging, secret_values
from app.common.metrics import install_metrics
from app.common.request_logging import RequestLoggingMiddleware


@pytest.fixture
def log_output():
    root_handlers = list(logging.root.handlers)
    root_level = logging.root.level
    states = {name: (list(item.handlers), item.level, item.propagate, item.disabled)
              for name, item in logging.root.manager.loggerDict.items() if isinstance(item, logging.Logger)}
    showwarning = warnings.showwarning
    output = io.StringIO()
    configure_logging(stream=output, enqueue=True, redactions=("known-secret-value",))
    try:
        yield output
    finally:
        flush_logging()
        from app.config import settings
        configure_logging(level=settings.LOG_LEVEL, service=settings.LOG_SERVICE_NAME,
                          environment=settings.LOG_ENVIRONMENT, redactions=secret_values(settings))
        logging.root.handlers = root_handlers
        logging.root.setLevel(root_level)
        for name, (handlers, level, propagate, disabled) in states.items():
            item = logging.getLogger(name)
            item.handlers, item.propagate, item.disabled = handlers, propagate, disabled
            item.setLevel(level)
        warnings.showwarning = showwarning


def documents(output):
    flush_logging()
    return [json.loads(line) for line in output.getvalue().splitlines()]


def completions(output):
    return [row for row in documents(output) if row.get("event", {}).get("action") == "http_request"]


def make_app(access_enabled=True):
    app = FastAPI()
    install_metrics(app)
    app.add_middleware(RequestLoggingMiddleware, access_enabled=access_enabled)
    app.add_exception_handler(BusinessException, bussiness_excpetion_hadler)
    app.add_exception_handler(Exception, global_excpetion_hadler)

    @app.get("/api/items/{item_id}")
    def item(item_id: int):
        logging.getLogger("app.test_worker").info("handled item")
        return {"id": item_id}

    @app.get("/api/business")
    def business():
        raise BusinessException("private business detail")

    @app.get("/api/forbidden")
    def forbidden():
        raise HTTPException(403)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("known-secret-value")

    @app.get("/")
    def root():
        return {"ok": True}

    return app


def test_stdlib_percent_format_and_loguru_braces_produce_single_json_lines(log_output):
    logging.getLogger("app.stdlib_example").info("中文 %s %d", "消息\n换行", 7)
    logger.info("Loguru {} {{literal}}", "message")
    rows = documents(log_output)
    assert len(rows) == 2
    assert rows[0]["message"] == "中文 消息\n换行 7"
    assert rows[0]["log"]["logger"] == "app.stdlib_example"
    assert rows[0]["log"]["origin"]["file"]["name"] == "test_json_logging.py"
    assert rows[0]["@timestamp"].endswith("Z")
    assert rows[0]["process"]["pid"] > 0
    assert rows[1]["message"] == "Loguru message {literal}"
    assert "\x1b" not in log_output.getvalue()


def test_exceptions_redact_credentials_without_dumping_local_values(log_output):
    private_local = "a-local-value-that-must-not-be-dumped"
    message = ('known-secret-value password="request-password" LLM_API_KEY="prefixed-private-key" Bearer private-token '
               'mysql://alice:database-password@db:3306/lab '
               'https://provider.invalid/v1?api_key=query-secret\nsecond line')
    try:
        raise RuntimeError(message)
    except RuntimeError:
        logging.getLogger("app.failure").exception("调用失败")
    rows = documents(log_output)
    assert len(rows) == 1
    error = rows[0]["error"]
    assert error["type"] == "RuntimeError"
    assert "RuntimeError" in error["stack_trace"] and "second line" in error["stack_trace"]
    for secret in (private_local, "known-secret-value", "request-password", "prefixed-private-key", "private-token", "database-password", "query-secret"):
        assert secret not in log_output.getvalue()


def test_bootstrap_failures_are_json_and_return_nonzero(log_output, monkeypatch):
    from app import server

    def fail(*args, **kwargs):
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(server.uvicorn, "run", fail)
    monkeypatch.setattr(server, "configure_logging", lambda **options: configure_logging(stream=log_output, **options))
    assert server.main([]) == 1
    rows = documents(log_output)
    assert len(rows) == 1 and rows[0]["error"]["type"] == "RuntimeError"
    assert rows[0]["message"] == "Application startup failed"


def test_unknown_bound_objects_do_not_break_queued_json_or_leak_repr(log_output):
    logger.bind(session=lambda: "private-object", password="private-extra").info("safe fields")
    rows = documents(log_output)
    assert len(rows) == 1 and rows[0]["message"] == "safe fields"
    assert "private-extra" not in log_output.getvalue()
    assert "session" not in rows[0]


def test_stdout_and_file_have_identical_context_and_redacted_stack(log_output, tmp_path):
    destination = tmp_path / "nested" / "backend.jsonl"
    configure_logging(stream=log_output, log_file=str(destination), redactions=("known-secret-value",))
    with logger.contextualize(request_id="file-request-123"):
        try:
            raise RuntimeError("known-secret-value\nsecond line")
        except RuntimeError:
            logger.bind(session=lambda: "private-object").exception("检索失败")
    rows = documents(log_output)
    saved = [json.loads(line) for line in destination.read_text().splitlines()]
    assert rows == saved and len(saved) == 1
    assert saved[0]["request_id"] == "file-request-123"
    assert saved[0]["error"]["type"] == "RuntimeError"
    assert "known-secret-value" not in destination.read_text()


def test_reconfiguring_removes_old_handlers_and_does_not_duplicate_records(log_output):
    old_text = io.StringIO()
    logging.getLogger("uvicorn.error").addHandler(logging.StreamHandler(old_text))
    configure_logging(stream=log_output, level="WARNING", service="test-api", environment="test", enqueue=False)
    logging.getLogger("uvicorn.error").info("hidden")
    logging.getLogger("uvicorn.error").error("server failed")
    logging.getLogger("uvicorn.access").info("raw URL with private query")
    rows = documents(log_output)
    assert len(rows) == 1 and rows[0]["message"] == "server failed"
    assert rows[0]["service"] == {"name": "test-api", "environment": "test"}
    assert old_text.getvalue() == ""


def test_custom_stdlib_level_is_forwarded(log_output):
    logging.getLogger("app.custom").log(35, "custom level")
    assert documents(log_output)[0]["message"] == "custom level"


def test_configured_secret_and_encoded_url_passwords_are_collected():
    values = secret_values(SimpleNamespace(
        JWT_SECRET_KEY="jwt-secret-value", LLM_API_KEY=SecretStr("api-secret-value"),
        LANGSMITH_API_KEY=SecretStr(""), METRICS_TOKEN=SecretStr("metric-secret"),
        DATABASE_URL="mysql://alice:p%40ssword@db", REDIS_URL=SecretStr("redis://:redis-secret@cache"),
    ))
    assert {"jwt-secret-value", "api-secret-value", "metric-secret", "p%40ssword", "p@ssword", "redis-secret"} <= set(values)
    assert secret_values(SimpleNamespace(DATABASE_URL="http://[invalid")) == ()


@pytest.mark.parametrize("path,status,outcome", [
    ("/api/items/1234567", 200, "success"),
    ("/api/business", 200, "business_error"),
    ("/api/forbidden", 403, "http_error"),
    ("/api/items/invalid", 422, "http_error"),
    ("/api/boom", 500, "exception"),
    ("/api/private-unknown", 404, "http_error"),
])
def test_request_log_has_normalized_route_status_and_matching_header(log_output, path, status, outcome):
    response = TestClient(make_app(), raise_server_exceptions=False).get(
        path + "?token=private-query", headers={"X-Request-ID": "client-request-01", "Authorization": "Bearer private-header"})
    assert response.status_code == status
    assert response.headers["x-request-id"] == "client-request-01"
    rows = completions(log_output)
    assert len(rows) == 1
    row = rows[0]
    assert row["http"] == {"request": {"method": "GET"}, "response": {"status_code": status}}
    assert row["request_id"] == "client-request-01" and row["outcome"] == outcome
    assert row["event"]["duration"] >= 0
    if path.startswith("/api/items/"):
        assert row["route"] == "/api/items/{item_id}"
    if status == 500:
        assert row["error"]["type"] == "RuntimeError"
    for value in ("1234567", "private-query", "private-header", "private-unknown", "known-secret-value"):
        assert value not in log_output.getvalue()


@pytest.mark.parametrize("candidate", ["x" * 65, "bad request id", ""])
def test_invalid_request_id_is_replaced_with_generated_id(log_output, candidate):
    response = TestClient(make_app()).get("/api/items/1", headers={"X-Request-ID": candidate})
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32 and request_id != candidate
    rows = documents(log_output)
    assert {row["request_id"] for row in rows} == {request_id}  # Sync worker and completion.


def test_successful_health_scrapes_are_quiet_and_access_logging_can_be_disabled(log_output):
    client = TestClient(make_app(access_enabled=False))
    assert client.get("/").status_code == 200
    assert client.get("/metrics").status_code == 200
    response = client.get("/api/items/1")
    assert response.headers["x-request-id"]
    assert completions(log_output) == []
    assert documents(log_output)[0]["message"] == "handled item"
    # Exceptions still emit a diagnostic record when normal access logs are off.
    TestClient(make_app(access_enabled=False), raise_server_exceptions=False).get("/api/boom")
    assert completions(log_output)[0]["outcome"] == "exception"


@pytest.mark.anyio
async def test_stream_duration_covers_last_body_and_context_reaches_worker(log_output, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    ticks = iter((10_000_000_000, 18_000_000_000))
    monkeypatch.setattr(request_logging, "time", SimpleNamespace(perf_counter_ns=lambda: next(ticks)))

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/stream")
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        entered.set()
        await release.wait()
        await asyncio.to_thread(logging.getLogger("app.worker").info, "stream worker")
        await send({"type": "http.response.body", "body": b"last"})

    async def send(message):
        pass

    task = asyncio.create_task(RequestLoggingMiddleware(app)({
        "type": "http", "path": "/api/stream", "method": "POST", "headers": []}, None, send))
    await entered.wait()
    assert completions(log_output) == []
    release.set()
    await task
    rows = documents(log_output)
    assert len({row["request_id"] for row in rows}) == 1
    assert completions(log_output)[0]["event"]["duration"] == 8_000_000_000


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [False, True])
async def test_cancelled_stream_is_logged_once_with_actual_status(log_output, headers):
    entered = asyncio.Event()

    async def app(scope, receive, send):
        if headers:
            await send({"type": "http.response.start", "status": 200})
        entered.set()
        await asyncio.Event().wait()

    async def send(message):
        pass

    task = asyncio.create_task(RequestLoggingMiddleware(app)({
        "type": "http", "path": "/api/stream", "method": "GET", "headers": []}, None, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = completions(log_output)
    assert len(rows) == 1 and rows[0]["outcome"] == "cancelled"
    assert rows[0]["http"]["response"]["status_code"] == (200 if headers else 499)


@pytest.mark.anyio
async def test_concurrent_requests_and_following_logs_do_not_share_context(log_output):
    barrier = asyncio.Barrier(2)

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/work")
        await barrier.wait()
        await asyncio.to_thread(logging.getLogger("app.worker").info, "worker " + scope["path"])
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"done"})

    async def send(message):
        pass

    middleware = RequestLoggingMiddleware(app)
    await asyncio.gather(*(middleware({"type": "http", "path": "/" + value, "method": "GET",
                                      "headers": [(b"x-request-id", value.encode())]}, None, send)
                           for value in ("one", "two")))
    logging.getLogger("app.outside").info("outside request")
    rows = documents(log_output)
    for row in rows:
        if row["message"].startswith("worker "):
            assert row["request_id"] == row["message"].split("/")[-1]
    assert "request_id" not in rows[-1]
