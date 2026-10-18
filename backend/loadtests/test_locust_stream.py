"""Run with .venv-loadtest/bin/python -m pytest loadtests/test_locust_stream.py."""

import json
import os
from types import SimpleNamespace

os.environ["LOCUST_SKIP_MONKEY_PATCH"] = "1"

import pytest

from loadtests import locustfile


class Response:
    status_code = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, events):
        self.events = events
        self.request_meta = {"response_time": 1}
        self.failure_message = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, **kwargs):
        for event in self.events:
            yield ("data: " + json.dumps(event)).encode()
            yield b""

    def failure(self, message):
        self.failure_message = message

    def close(self):
        self.closed = True


@pytest.mark.parametrize("events, failed", [
    ([{"type": "done", "answer": "answer"}], False),
    ([{"type": "error"}, {"type": "done", "answer": "fallback"}], True),
    ([{"type": "session"}], True),
])
def test_locust_stream_records_full_elapsed_time_and_failures(monkeypatch, events, failed):
    clock = iter((1, 6))
    monkeypatch.setattr(locustfile.time, "perf_counter", lambda: next(clock))
    locustfile.metrics = {"completed": 0, "failed": 0,
                         "nodes": {}, "tools": {}, "first_answer": locustfile.Histogram()}
    response = Response(events)
    user = SimpleNamespace(
        environment=SimpleNamespace(parsed_options=SimpleNamespace(request_timeout=10)),
        client=SimpleNamespace(post=lambda *args, **kwargs: response),
    )
    locustfile.LabAgentUser.agent_query(user)
    assert response.request_meta["response_time"] == 5000
    assert response.closed
    assert bool(response.failure_message) == failed
    assert locustfile.metrics["failed"] == int(failed)


@pytest.mark.parametrize("data,failed", [
    ({"found": True, "content": "retrieved context"}, False),
    ({"found": False, "content": ""}, True),
    ({"found": True, "content": " "}, True),
    ({"found": True, "content": {"wrong": "type"}}, True),
])
def test_rag_load_test_rejects_empty_or_invalid_retrieval(data, failed):
    locustfile.metrics = {"completed": 0, "failed": 0}
    response = Response([])
    response.json = lambda: {"code": 200, "data": data}
    user = SimpleNamespace(
        environment=SimpleNamespace(parsed_options=SimpleNamespace(request_timeout=10)),
        client=SimpleNamespace(post=lambda *args, **kwargs: response),
    )
    locustfile.LabAgentUser.rag_query(user)
    assert bool(response.failure_message) == failed
    assert locustfile.metrics == {"completed": int(not failed), "failed": int(failed)}
