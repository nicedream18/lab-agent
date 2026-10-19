"""Check the real API -> shared JSON file -> Logstash -> Elasticsearch -> Kibana path."""

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def compose(*args):
    return subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
                           "--profile", "elk", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True, timeout=240).stdout.strip()


def fetch(host, path, *, method="GET", payload=None, basic=None, headers=None):
    headers = dict(headers or {})
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(host + path, method=method, headers=headers,
                      data=None if payload is None else json.dumps(payload).encode())
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response), response.headers


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def search(host, auth, ids):
    deadline = time.monotonic() + 60
    while True:
        status, body, _ = fetch(host, "/lab-agent-logs-*/_search", method="POST", basic=auth,
                                payload={"size": 100, "query": {"terms": {"request_id": ids}},
                                         "track_total_hits": True})
        hits = body.get("hits", {}).get("hits", [])
        if status == 200 and {row["_source"].get("request_id") for row in hits} == set(ids):
            require(body["hits"]["total"]["value"] == len(ids), "Duplicate request completion indexed")
            return [row["_source"] for row in hits]
        require(time.monotonic() < deadline, "API logs did not arrive in Elasticsearch within 60 seconds")
        time.sleep(1)


def emit_request(host, business=False):
    request_id = "elk-check-" + secrets.token_hex(8)
    path = "/api/auth/login" if business else "/api/user/info?token=elk-private-query"
    headers = {"X-Request-ID": request_id, "Authorization": "Bearer elk-private-authorization"}
    status, body, response_headers = fetch(host, path, method="POST" if business else "GET", headers=headers,
                                          payload={"username": "elk_missing_" + secrets.token_hex(6),
                                                   "password": "elk-private-password"} if business else None)
    require(status == (200 if business else 401), "Unexpected probe API status")
    if business:
        require(body["code"] != 200, "Business failure was not returned")
    require(response_headers.get("X-Request-ID") == request_id, "Request ID missing from API response")
    return request_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-logstash", action="store_true", help="restart Logstash and check read checkpoint recovery")
    parser.add_argument("--output", type=Path, default=ROOT / "backend/profiles/elk-check.json")
    args = parser.parse_args()
    # Keep resolved secrets in memory only; never print or save this configuration.
    config = json.loads(compose("config", "--format", "json"))
    password = config["services"]["elasticsearch"]["environment"]["ELASTIC_PASSWORD"]
    writer_password = config["services"]["logstash"]["environment"]["LOGSTASH_WRITER_PASSWORD"]
    auth = ("elastic", password)
    endpoints = {name: "http://" + compose("port", name, port).splitlines()[0]
                 for name, port in (("backend", "8000"), ("elasticsearch", "9200"), ("kibana", "5601"))}
    raw = compose("ps", "--all", "--format", "json")
    try:
        services = json.loads(raw)
        if isinstance(services, dict):
            services = [services]
    except json.JSONDecodeError:
        services = [json.loads(line) for line in raw.splitlines()]
    states = {row["Service"]: row for row in services}
    for service in ("backend", "elasticsearch", "logstash", "kibana"):
        require(states[service]["Health"] == "healthy", f"Unhealthy service: {service}")
    for service in ("elk-setup",):
        require(states[service]["State"] == "exited" and states[service]["ExitCode"] == 0,
                f"Provisioning did not complete: {service}")
    es, kibana = endpoints["elasticsearch"], endpoints["kibana"]
    require(fetch(es, "/")[0] == 401, "Elasticsearch must require authentication")
    status, health, _ = fetch(es, "/_cluster/health", basic=auth)
    require(status == 200 and health["status"] in ("green", "yellow"), "Elasticsearch is unavailable")
    status, policy, _ = fetch(es, "/_ilm/policy/lab-agent-logs-retention", basic=auth)
    require(status == 200 and policy["lab-agent-logs-retention"]["policy"]["phases"]["delete"]["min_age"] == "7d",
            "Seven-day retention policy is missing")
    status, template, _ = fetch(es, "/_index_template/lab-agent-logs", basic=auth)
    mapping = template["index_templates"][0]["index_template"]["template"]["mappings"]["properties"]
    require(status == 200 and mapping["event"]["properties"]["duration"]["type"] == "long"
            and mapping["http"]["properties"]["response"]["properties"]["status_code"]["type"] == "integer",
            "Log field mappings are incorrect")
    require(fetch(es, "/_security/user", basic=("lab_agent_writer", writer_password))[0] == 403,
            "Logstash writer must not have user-administration privileges")
    ids = [emit_request(endpoints["backend"]), emit_request(endpoints["backend"], business=True)]
    records = search(es, auth, ids)
    require({row["outcome"] for row in records} == {"http_error", "business_error"}, "Failure classification changed")
    require(all(row["event"]["duration"] >= 0 and row["service"]["name"] and row["log"]["level"] for row in records),
            "Structured fields are missing")
    encoded = json.dumps(records)
    for private in (password, writer_password, "elk-private-query", "elk-private-authorization", "elk-private-password"):
        require(private not in encoded, "A private value appeared in indexed logs")
    status, views, _ = fetch(kibana, "/api/data_views", basic=auth)
    view = next((view for view in views.get("data_view", []) if view["title"] == "lab-agent-logs-*"), None)
    require(status == 200 and view is not None, "Kibana data view is missing")
    status, default, _ = fetch(kibana, "/api/data_views/default", basic=auth)
    require(status == 200 and default["data_view_id"] == view["id"], "Kibana default data view is incorrect")
    status, detail, _ = fetch(kibana, "/api/data_views/data_view/" + view["id"], basic=auth)
    require(status == 200 and detail["data_view"]["timeFieldName"] == "@timestamp"
            and "duration_ms" in detail["data_view"]["runtimeFieldMap"], "Kibana time/duration fields are missing")
    if args.restart_logstash:
        compose("stop", "--timeout", "60", "logstash")
        try:
            ids.append(emit_request(endpoints["backend"]))
        finally:
            compose("start", "--wait", "--wait-timeout", "180", "logstash")
        records = search(es, auth, ids)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "endpoints": endpoints,
              "cluster_status": health["status"], "data_view": view["id"], "indexed_probe_requests": len(records),
              "checks": ["service_health", "authenticated_elasticsearch", "restricted_writer", "seven_day_retention",
                         "typed_mapping", "api_to_index", "request_ids", "business_failure", "redaction",
                         "kibana_data_view", "default_time_field", "millisecond_runtime_field"],
              "logstash_restart_verified": args.restart_logstash,
              "scope": "Local running ELK; synthetic API failures only; no real LLM calls"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
