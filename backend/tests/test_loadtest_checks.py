"""Load-test success means validated application/SSE completion, not HTTP 200."""

import json

import pytest

from loadtests.checks import Histogram, check_api, consume_agent_stream


def frames(events):
    for event in events:
        yield ("data: " + json.dumps(event)).encode()
        yield b""


@pytest.mark.parametrize("events,error", [
    ([{"type": "session"}], "missing_done"),
    ([{"type": "done", "answer": ""}], "empty_answer"),
    ([{"type": "error"}, {"type": "done", "answer": "fallback"}], "agent_error"),
    ([{"type": "node", "status": "end", "state": "failed"},
      {"type": "done", "answer": "fallback"}], "node_failed"),
    ([{"type": "step", "status": "failed"},
      {"type": "done", "answer": "fallback"}], "tool_failed"),
])
def test_degraded_or_incomplete_agent_run_fails(events, error):
    assert consume_agent_stream(frames(events), 0)["error"] == error


def test_stream_reset_discards_provisional_answer_and_drains_to_eof():
    calls = iter((1, 3))
    events = [{"type": "token", "content": "provisional"}, {"type": "reset"},
              {"type": "token", "content": "final"},
              {"type": "done", "answer": "final"}, {"type": "error"}]
    result = consume_agent_stream(frames(events), 0, clock=lambda: next(calls))
    assert result["first_answer_ms"] == 3000
    assert result["error"] == "agent_error"


def test_http_success_does_not_hide_business_error_or_bad_page():
    with pytest.raises(ValueError, match="business_error"):
        check_api({"code": 400, "message": "not available"})
    with pytest.raises(ValueError, match="invalid_page"):
        check_api({"code": 200, "data": {}}, paginated=True)
    assert check_api({"code": 200, "data": {"list": [], "total": 0}}, True)["total"] == 0


def test_histogram_aggregates_without_retaining_sample_payloads():
    histogram = Histogram()
    for value in range(1, 101):
        histogram.add(value)
    histogram.add(float("nan"))
    histogram.add(None)
    result = histogram.summary()
    assert result == {"count": 100, "mean_ms": 50.5,
                      "p50_ms": 50, "p95_ms": 95, "p99_ms": 99}
