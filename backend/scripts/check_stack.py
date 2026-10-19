"""Verify the running root Compose stack without using external model services."""

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def compose(*args):
    return subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), *args],
                          cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def fetch(host, path, payload=None, token=None, basic=None, raw=None, content_type=None):
    headers = {}
    if payload is not None:
        raw, content_type = json.dumps(payload).encode(), "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = "Bearer " + token
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    try:
        response = urlopen(Request(host + path, data=raw, headers=headers), timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "backend/profiles/compose-check.json")
    args = parser.parse_args()
    rows = compose("ps", "--format", "json")
    try:
        services = json.loads(rows)
        if isinstance(services, dict):
            services = [services]
    except json.JSONDecodeError:
        services = [json.loads(line) for line in rows.splitlines()]
    expected = {"mysql", "redis", "backend", "frontend", "prometheus", "grafana"}
    require(expected <= {row["Service"] for row in services}, "Expected all six core Compose services")
    require(all(row["Health"] == "healthy" for row in services), "Some Compose services are not healthy")
    endpoints = {name: "http://" + compose("port", name, port).splitlines()[0]
                 for name, port in (("frontend", "80"), ("backend", "8000"), ("prometheus", "9090"), ("grafana", "3000"))}
    frontend = endpoints["frontend"]
    status, html = fetch(frontend, "/manager/ai-chat")
    require(status == 200 and b'id="app"' in html, "Frontend SPA fallback failed")
    status, _ = fetch(frontend, "/healthz")
    require(status == 200, "Nginx health endpoint failed")
    status, _ = fetch(frontend, "/metrics")
    require(status == 404, "Frontend must not proxy the metrics endpoint")

    username = "stack_check_" + secrets.token_hex(5)
    user = {"username": username, "password": secrets.token_urlsafe(18)}
    upload_name = ""
    registered = False
    try:
        status, body = fetch(frontend, "/api/auth/register", user)
        require(status == 200 and json.loads(body)["code"] == 200, "Registration through Nginx failed")
        registered = True
        status, body = fetch(frontend, "/api/auth/login", user)
        require(status == 200 and json.loads(body)["code"] == 200, "Login through Nginx failed")
        token = json.loads(body)["data"]["token"]
        for path in ("/api/user/info", "/api/lab/list", "/api/equipment/list"):
            status, body = fetch(frontend, path, token=token)
            require(status == 200 and json.loads(body)["code"] == 200, f"Proxy/JWT check failed: {path}")
        status, _ = fetch(frontend, "/api/user/info")
        require(status == 401, "Unauthenticated request was not rejected")
        status, body = fetch(frontend, "/api/auth/login", {**user, "password": "incorrect-password"})
        require(status == 200 and json.loads(body)["code"] != 200, "Business error contract failed")

        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lRkAAAAASUVORK5CYII=")
        boundary = "compose-check-" + secrets.token_hex(8)
        upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="compose-check.png"\r\n'
                  'Content-Type: image/png\r\n\r\n').encode() + image + f'\r\n--{boundary}--\r\n'.encode()
        status, body = fetch(frontend, "/api/files/upload", raw=upload,
                             content_type="multipart/form-data; boundary=" + boundary)
        require(status == 200 and json.loads(body)["code"] == 200, "Upload through Nginx failed")
        uploaded = json.loads(body)["data"]
        upload_name = uploaded["disk_name"]
        status, body = fetch(frontend, uploaded["url"])
        require(status == 200 and body == image, "Uploaded file proxy failed")

        infrastructure = json.loads(compose("exec", "-T", "backend", "python", "-c", """
import json, secrets
from sqlalchemy import text
from redis import Redis
from app.config import settings
from app.database import engine
cache = Redis.from_url(settings.REDIS_URL.get_secret_value(), socket_timeout=3)
key = 'compose-check:' + secrets.token_hex(8)
try:
    cache.setex(key, 30, 'ok')
    cached = cache.get(key) == b'ok'
finally:
    cache.delete(key)
    cache.close()
with engine.connect() as db:
    charset = db.execute(text('SELECT @@character_set_database')).scalar()
print(json.dumps({'database': engine.dialect.name, 'charset': charset,
                  'redis_roundtrip': cached, 'llm_cache_enabled': settings.LLM_CACHE_ENABLED}))
"""))
        require(infrastructure["database"] == "mysql" and infrastructure["charset"] == "utf8mb4",
                "Backend is not connected to the Compose MySQL database")
        require(infrastructure["redis_roundtrip"], "Backend cannot use the Compose Redis service")

        status, body = fetch(endpoints["backend"], "/metrics")
        require(status == 200 and b'outcome="business_error"' in body
                and b"lab_agent_http_request_duration_seconds_bucket" in body, "Backend metrics failed")
        query = urlencode({"query": 'sum(rate(lab_agent_http_requests_total{job="lab-agent",route="/api/user/info"}[1m]))'})
        deadline = time.monotonic() + 30
        while True:
            # Create a measurable counter delta across two scrapes.
            fetch(frontend, "/api/user/info", token=token)
            status, body = fetch(endpoints["prometheus"], "/api/v1/query?" + query)
            result = json.loads(body).get("data", {}).get("result", [])
            if status == 200 and result and float(result[0]["value"][1]) > 0:
                break
            require(time.monotonic() < deadline, "Prometheus did not observe live API traffic")
            time.sleep(1)
        status, body = fetch(endpoints["prometheus"], "/api/v1/query?" + urlencode({"query": 'up{job="lab-agent"}'}))
        up = json.loads(body)["data"]["result"]
        require(status == 200 and len(up) == 1 and up[0]["metric"]["instance"] == "backend:8000"
                and up[0]["value"][1] == "1", "Prometheus is not scraping the Compose backend")

        grafana_user = compose("exec", "-T", "grafana", "sh", "-c", 'printf %s "$GF_SECURITY_ADMIN_USER"')
        grafana_password = compose("exec", "-T", "grafana", "sh", "-c", 'printf %s "$GF_SECURITY_ADMIN_PASSWORD"')
        auth = (grafana_user, grafana_password)
        status, body = fetch(endpoints["grafana"], "/api/datasources/uid/lab-agent-prometheus/health", basic=auth)
        require(status == 200 and json.loads(body)["status"] == "OK", "Grafana data source failed")
        status, body = fetch(endpoints["grafana"], "/api/dashboards/uid/lab-agent-api", basic=auth)
        dashboard = json.loads(body)
        require(status == 200 and dashboard["meta"]["provisioned"] and len(dashboard["dashboard"]["panels"]) == 11,
                "Grafana dashboard provisioning failed")
        now = int(time.time() * 1000)
        status, body = fetch(endpoints["grafana"], "/api/ds/query", basic=auth, payload={
            "from": str(now - 60_000), "to": str(now), "queries": [{"refId": "A",
            "datasource": {"type": "prometheus", "uid": "lab-agent-prometheus"},
            "expr": 'up{job="lab-agent"}', "instant": True, "range": False,
            "intervalMs": 5000, "maxDataPoints": 1000}]})
        result = json.loads(body).get("results", {}).get("A", {})
        values = [value for frame in result.get("frames", [])
                  for field, column in zip(frame["schema"]["fields"], frame["data"]["values"])
                  if field["type"] == "number" for value in column]
        require(status == 200 and not result.get("error") and values == [1], "Grafana query chain failed")

        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "endpoints": endpoints,
                  "services": {row["Service"]: row["Health"] for row in services},
                  "infrastructure": infrastructure, "prometheus_target": "backend:8000", "grafana_query": "UP=1",
                  "checks": ["spa_fallback", "nginx_proxy", "mysql_registration_login", "jwt",
                             "file_upload_download", "redis_roundtrip", "business_error_metrics",
                             "prometheus_live_qps", "grafana_datasource_dashboard_query"],
                  "scope": "Running Compose stack; temporary test account/file removed; no LLM calls"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if registered or upload_name:
            compose("exec", "-T", "backend", "python", "-c", """
import sys
from pathlib import Path
from app.config import UPLOAD_DIR
from app.database import SessionLocal
from app.models.user import User
with SessionLocal() as db:
    db.query(User).filter(User.username == sys.argv[1]).delete()
    db.commit()
if sys.argv[2]:
    assert Path(sys.argv[2]).name == sys.argv[2]
    (UPLOAD_DIR / sys.argv[2]).unlink(missing_ok=True)
""", username, upload_name)


if __name__ == "__main__":
    main()
