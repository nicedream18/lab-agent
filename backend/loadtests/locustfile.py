"""Read-only workloads. Use scripts/load_test.py; see docs/load-testing.md."""

from collections import defaultdict
import csv
import itertools
import json
import os
from pathlib import Path
import random
import sys
import time
import uuid

from locust import HttpUser, events, task
from locust.exception import StopUser
from locust.stats import PERCENTILES_TO_REPORT, StatsCSV
from requests.exceptions import RequestException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loadtests.checks import Histogram, check_api, consume_agent_stream

accounts = None
metrics = None
QUERIES = (
    ("labs", "有哪些开放的实验室？"),
    ("rules", "实验室危险化学品如何管理？"),
    ("equipment", "哪间实验室有 RTX4090？"),
)


@events.init_command_line_parser.add_listener
def options(parser):
    parser.add_argument("--scenario", choices=("api", "agent", "mixed", "rag"), default="api")
    parser.add_argument("--accounts-file", default=os.getenv("LAB_LOADTEST_ACCOUNTS_FILE"))
    parser.add_argument("--think-time", type=float, default=0.5)
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--metrics-json", default="profiles/locust/metrics.json")


@events.test_start.add_listener
def start(environment, **kwargs):
    global accounts, metrics
    config = environment.parsed_options
    if config.think_time < 0 or config.request_timeout <= 0:
        raise SystemExit("think-time must be >= 0; request-timeout must be > 0")
    if config.accounts_file:
        rows = json.loads(Path(config.accounts_file).read_text())
    elif os.getenv("LAB_LOADTEST_TOKEN"):
        rows = [{"token": os.environ["LAB_LOADTEST_TOKEN"]}]
    else:
        rows = [{"username": os.getenv("LAB_LOADTEST_USERNAME"),
                 "password": os.getenv("LAB_LOADTEST_PASSWORD")}]
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict) or not (
            isinstance(row.get("token"), str) and row["token"].strip()
            or isinstance(row.get("username"), str) and row["username"].strip()
            and isinstance(row.get("password"), str) and row["password"]
        ) for row in rows
    ):
        raise SystemExit("Provide test accounts or LAB_LOADTEST_TOKEN")
    accounts = itertools.cycle(rows)
    metrics = {"completed": 0, "failed": 0, "first_answer": Histogram(),
               "nodes": defaultdict(Histogram), "tools": defaultdict(Histogram)}


@events.test_stop.add_listener
def finish(environment, **kwargs):
    if metrics is None:
        return
    path = Path(environment.parsed_options.metrics_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = {"scenario": environment.parsed_options.scenario,
              "completed": metrics["completed"], "failed": metrics["failed"],
              "first_answer": metrics["first_answer"].summary(),
              "nodes": {key: value.summary() for key, value in metrics["nodes"].items()},
              "tools": {key: value.summary() for key, value in metrics["tools"].items()}}
    result["http"] = [
        {"method": entry.method, "name": entry.name,
         "requests": entry.num_requests, "failures": entry.num_failures,
         "mean_ms": entry.avg_response_time, "rps": entry.total_rps,
         "p95_ms": entry.get_response_time_percentile(0.95),
         "p99_ms": entry.get_response_time_percentile(0.99)}
        for entry in environment.stats.entries.values()
    ]
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    # Locust's periodic CSV may stop before its last refresh. Export an explicit
    # final snapshot after in-flight requests have drained.
    with path.with_name("final-stats.csv").open("w", newline="") as handle:
        StatsCSV(environment, PERCENTILES_TO_REPORT).requests_csv(csv.writer(handle))
    if not metrics["completed"] or metrics["failed"]:
        environment.process_exit_code = 1


class LabAgentUser(HttpUser):
    host = "http://127.0.0.1:8000"

    def wait_time(self):
        return self.environment.parsed_options.think_time

    def on_start(self):
        credential = next(accounts)
        token = credential.get("token")
        if not token:
            with self.client.post("/api/auth/login", json=credential,
                                  name="/api/auth/login [setup]", catch_response=True,
                                  allow_redirects=False, timeout=15) as response:
                try:
                    if response.status_code != 200:
                        raise ValueError("login_http_error")
                    token = check_api(response.json()).get("token")
                    if not isinstance(token, str) or not token.strip():
                        raise ValueError("missing_token")
                except ValueError:
                    response.failure("login_failed")
                    token = None
            if not token:
                raise StopUser()
        self.client.headers.update({"Authorization": f"Bearer {token.strip()}"})

    @task
    def workload(self):
        scenario = self.environment.parsed_options.scenario
        if scenario == "rag":
            self.rag_query()
        elif scenario == "agent" or (scenario == "mixed" and random.random() < 0.2):
            self.agent_query()
        else:
            self.api_query()

    def api_query(self):
        path = random.choice(("/api/lab/list", "/api/lab/list",
                              "/api/equipment/list", "/api/user/info"))
        paginated = path.endswith("/list")
        with self.client.get(path, params={"page": 1, "page_size": 10} if paginated else None,
                             name=path, catch_response=True, allow_redirects=False,
                             timeout=self.environment.parsed_options.request_timeout) as response:
            try:
                if response.status_code != 200:
                    raise ValueError("http_error")
                check_api(response.json(), paginated=paginated)
                metrics["completed"] += 1
            except ValueError as exc:
                response.failure(str(exc))
                metrics["failed"] += 1

    def rag_query(self):
        query = random.choice(("实验室危险化学品如何管理？", "实验室发生火灾应怎样处理？",
                               "实验室安全教育培训有什么要求？"))
        with self.client.post("/__loadtest/rag", json={"query": query},
                              name="/__loadtest/rag", catch_response=True,
                              allow_redirects=False,
                              timeout=self.environment.parsed_options.request_timeout) as response:
            try:
                if response.status_code != 200:
                    raise ValueError("http_error")
                data = check_api(response.json())
                content = data.get("content")
                if data.get("found") is not True or not isinstance(content, str) or not content.strip():
                    raise ValueError("empty_retrieval")
                metrics["completed"] += 1
            except ValueError as exc:
                response.failure(str(exc))
                metrics["failed"] += 1

    def agent_query(self):
        label, query = random.choice(QUERIES)
        payload = {"conversation_id": uuid.uuid4().hex,
                   "messages": [{"role": "user", "content": query}]}
        started = time.perf_counter()
        with self.client.post("/api/ai/chat/stream", json=payload, stream=True,
                              name=f"/api/ai/chat/stream [{label}]", catch_response=True,
                              allow_redirects=False,
                              timeout=self.environment.parsed_options.request_timeout) as response:
            try:
                if response.status_code != 200:
                    raise ValueError("http_error")
                if response.headers.get("Content-Type", "").split(";")[0] != "text/event-stream":
                    raise ValueError("invalid_content_type")
                result = consume_agent_stream(response.iter_lines(chunk_size=None), started)
                if result["error"]:
                    raise ValueError(result["error"])
                for category in ("nodes", "tools"):
                    for item in result[category]:
                        metrics[category][item["name"]].add(item["duration_ms"])
                metrics["first_answer"].add(result["first_answer_ms"])
                metrics["completed"] += 1
            except (ValueError, UnicodeError, RequestException):
                response.failure("agent_stream_failed")
                metrics["failed"] += 1
            finally:
                response.close()
                # HttpUser records only headers when stream=True. Replace with
                # elapsed through EOF so reported P95 includes the whole reply.
                response.request_meta["response_time"] = (time.perf_counter() - started) * 1000
