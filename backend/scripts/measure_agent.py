"""Client-side measurements of the real Agent SSE endpoint (no profiler)."""

from getpass import getpass
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep credentials on the explicitly selected endpoint.
        raise HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def sse_events(response):
    """Read full SSE frames, including multiline data and comment heartbeats."""
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                event = json.loads("\n".join(data))
                if not isinstance(event, dict):
                    raise ValueError("Expected JSON object in SSE data")
                yield event
                data.clear()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        raise ValueError("Truncated SSE frame")


def measure_request(opener, url, token, query, timeout):
    conversation_id = uuid.uuid4().hex
    payload = {"conversation_id": conversation_id,
               "messages": [{"role": "user", "content": query}]}
    request = Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    result = {"conversation_id": conversation_id, "status": "failed",
              "first_event_ms": None, "first_answer_ms": None,
              "done_ms": None, "nodes": [], "tools": []}
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise ValueError("Expected text/event-stream")
            for event in sse_events(response):
                elapsed = (time.perf_counter() - started) * 1000
                if result["first_event_ms"] is None:
                    result["first_event_ms"] = elapsed
                kind = event.get("type")
                if kind == "error":
                    result["error"] = "agent_error"
                elif kind == "reset":
                    result["first_answer_ms"] = None
                elif kind == "token" and event.get("content", "").strip():
                    if result["first_answer_ms"] is None:
                        result["first_answer_ms"] = elapsed
                elif kind == "done":
                    result["done_ms"] = elapsed
                elif kind == "node" and event.get("status") == "end":
                    result["nodes"].append({"name": event.get("node"),
                                            "state": event.get("state"),
                                            "duration_ms": event.get("duration_ms"),
                                            "received_ms": elapsed})
                elif kind == "step" and event.get("status") in ("done", "failed", "skipped"):
                    result["tools"].append({"name": event.get("tool"),
                                            "state": event.get("status"),
                                            "duration_ms": event.get("duration_ms"),
                                            "received_ms": elapsed})
            if result["done_ms"] is None:
                result.setdefault("error", "missing_done")
            if "error" not in result:
                result["status"] = "completed"
    except HTTPError as exc:
        result["error"] = f"http_{exc.code}"
        exc.close()
    except (TimeoutError, URLError, OSError) as exc:
        result["error"] = "timeout" if isinstance(exc, TimeoutError) else "connection_error"
    except (ValueError, UnicodeError):
        result["error"] = "invalid_sse"
    result["complete_ms"] = (time.perf_counter() - started) * 1000
    return result


def run_chat(args, output):
    from scripts.profile_agent import timing_summary, write_json

    base = args.base_url.rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("--base-url must be an HTTP(S) server URL without credentials")
    opener = build_opener(NoRedirect())
    if args.username:
        password = getpass("Test account password: ")
        request = Request(
            base + "/api/auth/login",
            data=json.dumps({"username": args.username, "password": password}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with opener.open(request, timeout=args.timeout) as response:
            login = json.load(response)
        token = (login.get("data") or {}).get("token")
        if login.get("code") != 200 or not token:
            raise RuntimeError("Login failed; check the test account credentials")
    else:
        token = os.getenv("LAB_AGENT_PROFILE_TOKEN") or getpass("Bearer token: ")
    if not token.strip():
        raise ValueError("Token cannot be empty")

    report = {"measurement": "client_sse", "requests": [], "summary": {}}
    try:
        for index in range(args.repeat):
            sample = measure_request(opener, base + "/api/ai/chat/stream",
                                     token.strip(), args.query, args.timeout)
            report["requests"].append(sample)
            print(f"Request {index + 1}: {sample['status']}, "
                  f"complete={sample['complete_ms']:.1f} ms, "
                  f"first_answer={sample['first_answer_ms']}")
            for node in sample["nodes"]:
                print(f"  node {node['name']}: {node['duration_ms']} ms ({node['state']})")
            if sample.get("error"):
                print(f"  error: {sample['error']}")
    finally:
        completed = [sample for sample in report["requests"]
                     if sample["status"] == "completed"]
        report["summary"] = {"completed": len(completed),
                             "failed": len(report["requests"]) - len(completed)}
        if completed:
            report["summary"]["complete"] = timing_summary(
                [sample["complete_ms"] for sample in completed])
        write_json(output, report)
        print(f"Metrics: {output}")
    return int(report["summary"]["failed"] > 0)
