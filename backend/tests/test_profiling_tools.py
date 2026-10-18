"""Regression checks for native-context call graphs and Agent SSE measurements."""

from email.message import Message
import json
import pstats
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.measure_agent import measure_request, sse_events
from scripts.profile_agent import kb_worker, validate_kb_profile


def test_profile_keeps_search_frames_across_torch_inference_mode(tmp_path, monkeypatch):
    # This native context manager drops active cProfile frames on CPython 3.13.9
    # when built-in C-call events are enabled. No model/download is necessary.
    torch = pytest.importorskip("torch")
    from app.services import kb_service

    class Collection:
        def count(self):
            return 1

        def query(self, **kwargs):
            with torch.inference_mode():
                torch.ones(2).sum()
            return {"documents": [["test context"]], "distances": [[0.1]]}

    monkeypatch.setattr(kb_service, "get_collection", lambda: Collection())
    result_path = tmp_path / "result.json"
    output = tmp_path / "kb.prof"
    args = SimpleNamespace(query="test", repeat=3, cold=False,
                           _phase="profile", _result=result_path)
    kb_worker(args, output)
    result = json.loads(result_path.read_text())
    assert result["validation"]["valid"]
    stats = pstats.Stats(str(output))
    search = next(row for key, row in stats.stats.items()
                  if key[2] == "search" and key[0].endswith("kb_service.py"))
    assert search[1] == 3
    assert sum(edge[0] for edge in search[4].values()) == 3
    assert result["wall_ms"] > 0


def test_invalid_call_graph_is_rejected():
    root = ("profile_agent.py", 1, "run_kb_queries")
    search = ("kb_service.py", 1, "search")
    stats = SimpleNamespace(stats={
        root: (1, 1, 0.01, 0.1, {}),
        search: (3, 3, 0.01, 0.1, {root: (0, 0, 0.0, 0.0)}),
    })
    assert not validate_kb_profile(stats, 3)["valid"]


class Response:
    def __init__(self, body):
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = "text/event-stream; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def __iter__(self):
        return iter(self.body.splitlines(keepends=True))


def frame(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def test_sse_reset_and_done_do_not_hide_response_completion(monkeypatch):
    # Receiving done is earlier than reading EOF (server may still save chat).
    ticks = iter(range(100))
    monkeypatch.setattr("scripts.measure_agent.time.perf_counter", lambda: next(ticks))
    events = [
        {"type": "session"},
        {"type": "token", "content": "discarded"},
        {"type": "reset"},
        {"type": "node", "node": "agent", "status": "end",
         "state": "success", "duration_ms": 123},
        {"type": "step", "tool": "search_lab_docs", "status": "done",
         "duration_ms": 4, "result": "private tool context"},
        {"type": "token", "content": "private answer"},
        {"type": "done", "answer": "private answer"},
    ]
    opener = Mock()
    opener.open.return_value = Response(b"".join(frame(event) for event in events))
    result = measure_request(opener, "http://localhost/api/ai/chat/stream",
                             "secret-token", "private query", 10)
    assert result["status"] == "completed"
    assert result["first_event_ms"] < result["first_answer_ms"] < result["done_ms"]
    assert result["done_ms"] < result["complete_ms"]
    assert result["first_answer_ms"] == 6000
    assert result["nodes"][0]["duration_ms"] == 123
    assert result["tools"][0]["duration_ms"] == 4
    assert "private" not in json.dumps(result)
    assert "secret-token" not in json.dumps(result)


@pytest.mark.parametrize("events, expected", [
    ([{"type": "session"}], "missing_done"),
    ([{"type": "error", "message": "private error"}, {"type": "done"}], "agent_error"),
])
def test_http_200_does_not_imply_completed_agent_run(events, expected):
    opener = Mock()
    opener.open.return_value = Response(b"".join(frame(event) for event in events))
    result = measure_request(opener, "http://localhost/api/ai/chat/stream", "token", "q", 10)
    assert result["status"] == "failed"
    assert result["error"] == expected


def test_multiline_sse_and_truncated_frame():
    response = Response(b': heartbeat\r\ndata: {"type":\r\ndata: "done"}\r\n\r\n')
    assert list(sse_events(response)) == [{"type": "done"}]
    with pytest.raises(ValueError, match="Truncated"):
        list(sse_events(Response(b'data: {"type": "done"}\n')))
