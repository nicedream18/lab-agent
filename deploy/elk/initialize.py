"""Idempotent local ELK provisioning. Never print credentials or HTTP bodies."""

import argparse
import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def api(host, path, method="GET", payload=None):
    password = os.environ.get("ELASTIC_PASSWORD", "")
    if not password:
        raise RuntimeError("ELASTIC_PASSWORD is missing; run ./scripts/start-elk.sh")
    headers = {"Authorization": "Basic " + base64.b64encode(("elastic:" + password).encode()).decode(),
               "Content-Type": "application/json", "kbn-xsrf": "elk-setup"}
    request = Request(host + path, method=method, headers=headers,
                      data=None if payload is None else json.dumps(payload).encode())
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"ELK setup {method} {path} returned HTTP {exc.code}") from None


def setup_elasticsearch():
    host = "http://elasticsearch:9200"
    for user, variable in (("kibana_system", "KIBANA_SYSTEM_PASSWORD"),
                           ("lab_agent_writer", "LOGSTASH_WRITER_PASSWORD")):
        if not os.environ.get(variable):
            raise RuntimeError(f"{variable} is missing; run ./scripts/start-elk.sh")
    api(host, "/_security/user/kibana_system/_password", "POST",
        {"password": os.environ["KIBANA_SYSTEM_PASSWORD"]})
    api(host, "/_security/role/lab_agent_writer", "PUT", {
        "cluster": ["monitor"],
        "indices": [{"names": ["lab-agent-logs-*"], "privileges": ["create_index", "create_doc"]}],
    })
    api(host, "/_security/user/lab_agent_writer", "PUT", {
        "password": os.environ["LOGSTASH_WRITER_PASSWORD"], "roles": ["lab_agent_writer"],
    })
    api(host, "/_ilm/policy/lab-agent-logs-retention", "PUT", {
        "policy": {"phases": {"delete": {"min_age": "7d", "actions": {"delete": {}}}}},
    })
    template = json.loads(Path(__file__).with_name("index-template.json").read_text())
    api(host, "/_index_template/lab-agent-logs", "PUT", template)
    print(json.dumps({"setup": "elasticsearch", "status": "complete",
                      "configured": ["service_users", "writer_role", "index_template", "seven_day_retention"]}))


def setup_kibana():
    host = "http://kibana:5601"
    views = api(host, "/api/data_views")["data_view"]
    existing = next((view for view in views if view["title"] == "lab-agent-logs-*"), None)
    if existing:
        view_id = existing["id"]
    else:
        view = api(host, "/api/data_views/data_view", "POST", {"data_view": {
            "title": "lab-agent-logs-*", "name": "Lab Agent · 后端日志",
            "timeFieldName": "@timestamp", "allowNoIndex": True,
            "runtimeFieldMap": {"duration_ms": {"type": "double", "script": {
                "source": "if (doc.containsKey('event.duration') && doc['event.duration'].size() > 0) { emit(doc['event.duration'].value / 1000000.0); }",
            }}},
        }})["data_view"]
        view_id = view["id"]
    api(host, "/api/data_views/default", "POST", {"data_view_id": view_id, "force": True})
    print(json.dumps({"setup": "kibana", "status": "complete", "data_view": view_id,
                      "title": "lab-agent-logs-*", "discover": "/app/discover"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("elasticsearch", "kibana"))
    args = parser.parse_args()
    if args.phase == "elasticsearch":
        setup_elasticsearch()
    else:
        setup_kibana()


if __name__ == "__main__":
    main()
