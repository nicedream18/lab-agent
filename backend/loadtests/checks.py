"""Validation and bounded aggregate metrics; no Locust/app imports."""

from collections import Counter
import math
import time

from scripts.measure_agent import sse_events


def check_api(body, paginated=False):
    if not isinstance(body, dict) or body.get("code") != 200:
        raise ValueError("business_error")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("invalid_response_data")
    if paginated and (not isinstance(data.get("list"), list)
                      or not isinstance(data.get("total"), int)):
        raise ValueError("invalid_page")
    return data


def consume_agent_stream(lines, started, clock=time.perf_counter):
    result = {"first_answer_ms": None, "nodes": [], "tools": []}
    done = False
    failure = None
    for event in sse_events(lines):
        kind = event.get("type")
        if kind == "reset":
            result["first_answer_ms"] = None
        elif kind == "token":
            content = event.get("content")
            if isinstance(content, str) and content.strip() and result["first_answer_ms"] is None:
                result["first_answer_ms"] = (clock() - started) * 1000
        elif kind == "done":
            done = True
            answer = event.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                failure = failure or "empty_answer"
        elif kind == "error":
            failure = "agent_error"
        elif kind == "node" and event.get("status") == "end":
            result["nodes"].append({"name": event.get("node"),
                                    "duration_ms": event.get("duration_ms")})
            if event.get("state") == "failed":
                failure = failure or "node_failed"
        elif kind == "step" and event.get("status") in ("done", "failed", "skipped"):
            result["tools"].append({"name": event.get("tool"),
                                    "duration_ms": event.get("duration_ms")})
            if event.get("status") == "failed":
                failure = failure or "tool_failed"
    result["error"] = failure or (None if done else "missing_done")
    return result


class Histogram:
    """Millisecond buckets; does not retain queries, results or credentials."""

    def __init__(self):
        self.buckets = Counter()
        self.count = 0
        self.total = 0.0

    def add(self, value):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            return
        self.buckets[round(value)] += 1
        self.count += 1
        self.total += value

    def summary(self):
        if not self.count:
            return {"count": 0}
        percentiles = {}
        for label, fraction in (("p50_ms", 0.5), ("p95_ms", 0.95), ("p99_ms", 0.99)):
            rank = math.ceil(self.count * fraction)
            seen = 0
            for value, count in sorted(self.buckets.items()):
                seen += count
                if seen >= rank:
                    percentiles[label] = value
                    break
        return {"count": self.count, "mean_ms": self.total / self.count, **percentiles}
