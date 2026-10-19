"""Run a reversible local LLM timeout exercise against the existing observability stack."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import secrets
import signal
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "deploy/drills/targets/incident.json"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
           "-f", str(ROOT / "deploy/drills/compose.yml")]


def compose(*args):
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True, capture_output=True,
                          text=True, timeout=180).stdout.strip()


def fetch(host, path, *, method="GET", payload=None, basic=None, request_id=None):
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    if request_id:
        headers["X-Request-ID"] = request_id
    started = time.perf_counter()
    try:
        response = urlopen(Request(host + path, method=method, headers=headers,
                                   data=None if payload is None else json.dumps(payload).encode()), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return {"status": response.status, "body": json.loads(raw),
                "request_id": response.headers.get("X-Request-ID"),
                "duration_ms": (time.perf_counter() - started) * 1000}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def register(active):
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps([{"targets": ["incident-api:8000"]}] if active else []) + "\n")
    temporary.replace(TARGET)


def cleanup(host=None):
    if host:
        try:
            fetch(host, "/control/fault", method="POST", payload={"enabled": False})
        except Exception:
            pass  # Removing the isolated containers below also removes the fault.
    register(False)
    compose("rm", "--stop", "--force", "incident-api", "incident-upstream")


def query(prometheus, expr, at=None):
    params = {"query": expr}
    if at is not None:
        params["time"] = at
    response = fetch(prometheus, "/api/v1/query?" + urlencode(params))
    require(response["status"] == 200 and response["body"]["status"] == "success", "Prometheus query failed")
    return response["body"]["data"]["result"]


def value(prometheus, expr, at):
    result = query(prometheus, expr, at)
    require(len(result) == 1, "Expected one Prometheus result: " + expr)
    number = float(result[0]["value"][1])
    require(math.isfinite(number), "Prometheus returned an undefined value")
    return number


def snapshot(prometheus, at):
    scope = '{job="lab-agent-drill",route=~"/api/drill/.*"}'
    failed = scope[:-1] + ',outcome!="success"}'
    qps = f'sum(rate(lab_agent_drill_http_requests_total{scope}[20s]))'
    p99 = 'histogram_quantile(.99,sum by(le)(rate(lab_agent_drill_http_request_duration_seconds_bucket{job="lab-agent-drill",route="/api/drill/chat"}[20s])))'
    return {"at": at, "up": value(prometheus, 'up{job="lab-agent-drill"}', at),
            "api_qps": value(prometheus, qps, at),
            "failure_percent": value(prometheus, f'100*(sum(rate(lab_agent_drill_http_requests_total{failed}[20s])) or vector(0))/clamp_min({qps},1e-9)', at),
            "chat_p99_ms": 1000 * value(prometheus, p99, at)}


def phase(host, run_id, name, seconds, prometheus):
    print(json.dumps({"phase": name, "status": "started", "seconds": seconds}), flush=True)
    started = time.time()
    rows = []

    def request(kind, sequence):
        identifier = f"incident-{run_id}-{name}-{sequence}"
        result = fetch(host, "/api/drill/" + kind, method="POST" if kind == "chat" else "GET", request_id=identifier)
        require(result["request_id"] == identifier, "Response correlation ID mismatch")
        return {key: result[key] for key in ("status", "request_id", "duration_ms")} | {"kind": kind}

    with ThreadPoolExecutor(max_workers=3) as pool:
        sequence = 0
        while time.time() - started < seconds:
            tick = time.monotonic()
            work = []
            for kind in ("chat", "chat", "catalog"):
                sequence += 1
                work.append(pool.submit(request, kind, sequence))
            rows.extend(future.result() for future in work)
            time.sleep(max(0, 1 - (time.monotonic() - tick)))
    finished = time.time()
    chat = [row for row in rows if row["kind"] == "chat"]
    expected = 500 if name == "fault" else 200
    require(all(row["status"] == expected for row in chat), f"Unexpected chat outcome in {name}")
    require(all(row["status"] == 200 for row in rows if row["kind"] == "catalog"), "Catalog was affected by the fault")
    durations = sorted(row["duration_ms"] for row in chat)
    result = {"name": name, "started_at": started, "finished_at": finished, "requests": len(rows),
              "failure_percent": 100 * sum(row["status"] >= 400 for row in rows) / len(rows),
              "client_chat_p99_ms": durations[math.ceil(.99 * len(durations)) - 1],
              "prometheus": snapshot(prometheus, finished), "samples": rows}
    print(json.dumps({key: result[key] for key in ("name", "requests", "failure_percent", "client_chat_p99_ms", "prometheus")}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase-seconds", type=int, default=35)
    parser.add_argument("--cleanup", action="store_true", help="remove only the two exercise containers/target")
    args = parser.parse_args()
    if args.cleanup:
        cleanup()
        print("Temporary exercise containers and scrape target removed.")
        return
    require(args.phase_seconds >= 30, "Use at least 30 seconds per phase for 5-second scrapes and a 20-second rate window")
    require(not compose("ps", "--all", "--quiet", "incident-api", "incident-upstream"),
            "Exercise containers already exist; finish that exercise or run this script with --cleanup")
    require(json.loads(TARGET.read_text()) == [], "An exercise scrape target is already registered")
    # Resolve passwords in memory for owned local services; never print this config.
    config = json.loads(compose("--profile", "elk", "config", "--format", "json"))
    services = config["services"]
    endpoints = {name: "http://" + compose("port", name, port).splitlines()[0]
                 for name, port in (("backend", "8000"), ("prometheus", "9090"), ("grafana", "3000"),
                                    ("kibana", "5601"), ("elasticsearch", "9200"))}
    require(fetch(endpoints["backend"], "/")["status"] == 200, "Main backend is not healthy")
    grafana_auth = (services["grafana"]["environment"]["GF_SECURITY_ADMIN_USER"],
                    services["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD"])
    es_auth = ("elastic", services["elasticsearch"]["environment"]["ELASTIC_PASSWORD"])
    run_id = secrets.token_hex(5)
    host = None
    report_path = ROOT / "backend/profiles/incident-drill.json"
    report = {"run_id": run_id, "started_at": datetime.now(timezone.utc).isoformat(), "phases": []}
    try:
        compose("up", "-d", "--wait", "--wait-timeout", "90", "incident-api")
        host = "http://" + compose("port", "incident-api", "8000")
        register(True)
        deadline = time.monotonic() + 40
        while True:
            up = query(endpoints["prometheus"], 'up{job="lab-agent-drill"}')
            if up and up[0]["value"][1] == "1":
                break
            require(time.monotonic() < deadline, "Prometheus did not discover the exercise; apply the new scrape config first")
            time.sleep(1)
        fetch(host, "/control/fault", method="POST", payload={"enabled": False})
        report["phases"].append(phase(host, run_id, "baseline", args.phase_seconds, endpoints["prometheus"]))
        fetch(host, "/control/fault", method="POST", payload={"enabled": True})
        report["phases"].append(phase(host, run_id, "fault", args.phase_seconds, endpoints["prometheus"]))
        fetch(host, "/control/fault", method="POST", payload={"enabled": False})
        report["phases"].append(phase(host, run_id, "recovered", args.phase_seconds, endpoints["prometheus"]))
        baseline, fault, recovered = report["phases"]
        require(all(part["prometheus"]["up"] == 1 for part in report["phases"]), "Scrape failed during exercise")
        require(fault["prometheus"]["failure_percent"] > 50 and recovered["prometheus"]["failure_percent"] < 1,
                "Metrics did not show the incident and recovery")
        sample = next(row for row in fault["samples"] if row["status"] == 500)
        deadline = time.monotonic() + 30
        while True:
            result = fetch(endpoints["elasticsearch"], "/lab-agent-logs-*/_search", method="POST", basic=es_auth,
                           payload={"size": 10, "query": {"term": {"request_id": sample["request_id"]}}})
            hits = result["body"].get("hits", {}).get("hits", [])
            evidence = next((hit["_source"] for hit in hits if hit["_source"].get("event", {}).get("action") == "http_request"), None)
            if evidence:
                break
            require(time.monotonic() < deadline, "Could not correlate failed request with Elasticsearch logs")
            time.sleep(1)
        require(evidence["error"]["type"] == "LLMUpstreamTimeout" and "ReadTimeout" in evidence["error"]["stack_trace"],
                "Expected upstream read-timeout evidence is missing")
        report["error_evidence"] = evidence
        report["main_backend_healthy"] = fetch(endpoints["backend"], "/")["status"] == 200
        require(report["main_backend_healthy"], "Main backend health check failed")
        report["main_prometheus_up"] = value(endpoints["prometheus"], 'up{job="lab-agent"}', time.time())
        require(report["main_prometheus_up"] == 1, "Main monitoring target is unavailable")
        dashboard = fetch(endpoints["grafana"], "/api/dashboards/uid/lab-agent-incident-drill", basic=grafana_auth)
        require(dashboard["status"] == 200, "The exercise Grafana dashboard is not provisioned")
        graph = fetch(endpoints["grafana"], "/api/ds/query", method="POST", basic=grafana_auth, payload={
            "from": str(int((fault["finished_at"] - 60) * 1000)), "to": str(int(fault["finished_at"] * 1000)),
            "queries": [{"refId": "A", "datasource": {"type": "prometheus", "uid": "lab-agent-prometheus"},
                         "expr": 'up{job="lab-agent-drill"}', "instant": True, "range": False,
                         "intervalMs": 5000, "maxDataPoints": 1000}]})
        result = graph["body"].get("results", {}).get("A", {})
        require(graph["status"] == 200 and not result.get("error") and result.get("frames"), "Grafana query chain failed")
        views = fetch(endpoints["kibana"], "/api/data_views", basic=es_auth)["body"]["data_view"]
        view_id = next(view["id"] for view in views if view["title"] == "lab-agent-logs-*")
        start_ms, fault_ms, end_ms = (int(value * 1000) for value in (baseline["started_at"] - 5, fault["finished_at"], recovered["finished_at"] + 5))
        report["links"] = {
            "grafana_incident": endpoints["grafana"] + "/d/lab-agent-incident-drill?" + urlencode({"from": start_ms, "to": fault_ms, "var-instance": "incident-api:8000"}),
            "grafana_recovery": endpoints["grafana"] + "/d/lab-agent-incident-drill?" + urlencode({"from": start_ms, "to": end_ms, "var-instance": "incident-api:8000"}),
            "kibana_request": endpoints["kibana"] + "/app/discover#/?" + urlencode({
                "_a": f"(columns:!(log.level,route,http.response.status_code,duration_ms,outcome,request_id,message),index:'{view_id}',query:(language:kuery,query:'request_id: \"{sample['request_id']}\"'))",
                "_g": f"(time:(from:'{datetime.fromtimestamp(baseline['started_at'] - 5, timezone.utc).isoformat()}',to:'{datetime.fromtimestamp(recovered['finished_at'] + 5, timezone.utc).isoformat()}'))"}),
        }
        report["scope"] = "Two isolated containers, local fake LLM, no real model calls or database writes; main metric namespace unchanged"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(report_path), "request_id": sample["request_id"], "links": report["links"]}), flush=True)
    finally:
        cleanup(host)
        print(json.dumps({"cleanup": "complete", "fault_enabled": False, "temporary_target": "removed"}), flush=True)


if __name__ == "__main__":
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    main()
