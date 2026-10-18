"""Verify real Prometheus scrapes/PromQL using a disposable API backend and container."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.load_test import BACKEND, stop_process, wait_ready


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fetch(url, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def main():
    root = BACKEND.parent
    port, prom_port = free_port(), free_port()
    host = f"http://127.0.0.1:{port}"
    prom = f"http://127.0.0.1:{prom_port}"
    name = "lab-agent-metrics-check-" + secrets.token_hex(4)
    with ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="lab-metrics-")))
        password = secrets.token_urlsafe(20)
        env = {**os.environ, "LAB_LOADTEST_LOCAL_DB": str(directory / "api.sqlite"),
               "LAB_LOADTEST_LOCAL_PASSWORD": password, "LAB_LOADTEST_LOCAL_JWT": secrets.token_urlsafe(48),
               "LAB_LOADTEST_LOCAL_RAG": "0", "METRICS_ENABLED": "true", "METRICS_TOKEN": "",
               "LANGSMITH_TRACING": "false"}
        log = stack.enter_context((directory / "api.log").open("w"))
        backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "loadtests.local_api:app",
                                    "--host", "127.0.0.1", "--port", str(port),
                                    "--loop", "asyncio", "--no-access-log"],
                                   cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
        stack.callback(stop_process, backend)
        wait_ready(host, backend)
        config = {"global": {"scrape_interval": "1s", "evaluation_interval": "1s"},
                  "rule_files": ["/etc/prometheus/rules.yml"],
                  "scrape_configs": [{"job_name": "lab-agent", "metrics_path": "/metrics",
                      "static_configs": [{"targets": [f"host.docker.internal:{port}"]}]}]}
        path = directory / "prometheus.yml"
        path.write_text(json.dumps(config))  # JSON is also valid YAML.
        subprocess.run(["docker", "run", "--detach", "--rm", "--name", name,
                        "--publish", f"127.0.0.1:{prom_port}:9090",
                        "--add-host", "host.docker.internal:host-gateway",
                        "--mount", f"type=bind,source={path},target=/etc/prometheus/prometheus.yml,readonly",
                        "--mount", f"type=bind,source={root / 'deploy/monitoring/rules.yml'},target=/etc/prometheus/rules.yml,readonly",
                        "prom/prometheus:v3.13.4", "--config.file=/etc/prometheus/prometheus.yml"],
                       check=True, stdout=subprocess.DEVNULL)
        stack.callback(lambda: subprocess.run(["docker", "rm", "--force", "--volumes", name],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_ready(prom, startup_timeout=30)

        def query(expression):
            status, body = fetch(prom + "/api/v1/query?" + urlencode({"query": expression}))
            if status != 200 or body.get("status") != "success":
                raise RuntimeError("Prometheus query failed")
            return [float(row["value"][1]) for row in body["data"]["result"]]

        deadline = time.monotonic() + 30
        while query('up{job="lab-agent"}') != [1.0]:
            if time.monotonic() > deadline:
                _, targets = fetch(prom + "/api/v1/targets")
                errors = [row.get("lastError") for row in targets["data"]["activeTargets"]]
                raise RuntimeError(f"Prometheus cannot scrape temporary backend: {errors}")
            time.sleep(.5)
        _, login = fetch(host + "/api/auth/login", {"username": "loadtest", "password": password})
        token = login["data"]["token"]
        for _ in range(4):
            assert fetch(host + "/api/lab/list?page=1&page_size=10", token=token)[0] == 200
            assert fetch(host + "/api/user/info")[0] == 401
            status, body = fetch(host + "/api/auth/login", {"username": "loadtest", "password": "wrong"})
            assert status == 200 and body["code"] != 200
            time.sleep(1)
        checks = {
            "successful_requests": ('sum(lab_agent_http_requests_total{route="/api/lab/list",outcome="success"})', lambda v: v >= 4),
            "http_errors": ('sum(lab_agent_http_requests_total{route="/api/user/info",status="401"})', lambda v: v >= 4),
            "business_errors": ('sum(lab_agent_http_requests_total{route="/api/auth/login",status="200",outcome="business_error"})', lambda v: v >= 4),
            "qps": ('sum(lab_agent:api_qps:rate5m{route="/api/lab/list"})', lambda v: v > 0),
            "p99_seconds": ('max(lab_agent:api_p99_seconds:rate5m{route="/api/lab/list"})', lambda v: v > 0),
            "success_route_error_ratio": ('max(lab_agent:api_failure_ratio:rate5m{route="/api/lab/list"})', lambda v: v == 0),
            "unauthorized_route_error_ratio": ('max(lab_agent:api_failure_ratio:rate5m{route="/api/user/info"})', lambda v: v == 1),
        }
        deadline = time.monotonic() + 15
        while True:
            results = {name: query(expr) for name, (expr, _) in checks.items()}
            if all(values and checks[name][1](values[0]) for name, values in results.items()):
                print(json.dumps({"scrape_up": 1, "verified": results}, indent=2))
                return 0
            if time.monotonic() > deadline:
                raise RuntimeError(f"Unexpected metrics: {results}")
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
