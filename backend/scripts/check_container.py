"""Smoke-test a built backend image using disposable SQLite data and a random local port."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BACKEND = Path(__file__).resolve().parents[1]


def docker(*args, env=None, timeout=120):
    return subprocess.run(["docker", *args], env=env, check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def fetch(host, path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(host + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers=headers)
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read().decode()
        return response.status, body


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="lab-agent-backend:local")
    parser.add_argument("--output", type=Path, default=BACKEND / "profiles/docker-image-check.json")
    args = parser.parse_args()
    image = json.loads(docker("image", "inspect", args.image))[0]
    require(image["Config"]["User"] not in ("", "0", "root", "0:0"), "Image must run as a non-root user")
    baked_env = dict(item.split("=", 1) for item in image["Config"].get("Env", []))
    for key in ("DATABASE_URL", "JWT_SECRET_KEY", "LLM_API_KEY", "LANGSMITH_API_KEY", "METRICS_TOKEN"):
        require(key not in baked_env, f"Runtime setting was baked into the image: {key}")

    name = "lab-agent-backend-check-" + secrets.token_hex(4)
    environment = {
        "DATABASE_URL": "sqlite+pysqlite:////app/run/container-check.sqlite",
        "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "LLM_API_KEY": "container-check-only", "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_MODEL": "container-check-only", "LLM_FORCE_IPV4": "false",
        "LLM_CACHE_ENABLED": "false", "LANGSMITH_TRACING": "false",
        "KB_WARMUP_ON_STARTUP": "false", "KB_RERANKER_ENABLED": "false",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "ANONYMIZED_TELEMETRY": "false",
        "METRICS_ENABLED": "true", "METRICS_TOKEN": "",
    }
    env_args = [arg for key in environment for arg in ("--env", key)]
    container = docker("run", "--detach", "--name", name, "--publish", "127.0.0.1::8000",
                       *env_args, args.image, env={**os.environ, **environment})
    try:
        state = json.loads(docker("inspect", container))[0]
        port = state["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        host = "http://127.0.0.1:" + port
        deadline = time.monotonic() + 90
        while True:
            state = json.loads(docker("inspect", container))[0]
            require(state["State"]["Running"], "Container exited before startup completed")
            try:
                status, _ = fetch(host, "/")
                if status == 200 and state["State"].get("Health", {}).get("Status") == "healthy":
                    break
            except (URLError, OSError):
                pass
            require(time.monotonic() < deadline, "Container did not become healthy within 90 seconds")
            time.sleep(.5)

        status, body = fetch(host, "/openapi.json")
        require(status == 200 and "/api/auth/login" in json.loads(body)["paths"], "OpenAPI routes missing")
        user = {"username": "container_check_" + secrets.token_hex(4), "password": secrets.token_urlsafe(18)}
        status, body = fetch(host, "/api/auth/register", user)
        require(status == 200 and json.loads(body)["code"] == 200, "SQLite registration failed")
        status, body = fetch(host, "/api/auth/login", user)
        require(status == 200 and json.loads(body)["code"] == 200, "Login failed")
        token = json.loads(body)["data"]["token"]
        status, body = fetch(host, "/api/user/info", token=token)
        require(status == 200 and json.loads(body)["data"]["username"] == user["username"], "Authenticated request failed")
        status, body = fetch(host, "/api/lab/list?page=1&page_size=10", token=token)
        require(status == 200 and json.loads(body)["code"] == 200, "Laboratory API failed")
        status, body = fetch(host, "/api/auth/login", {**user, "password": "incorrect-password"})
        require(status == 200 and json.loads(body)["code"] != 200, "Business failure contract changed")
        status, body = fetch(host, "/metrics")
        require(status == 200 and 'outcome="business_error"' in body
                and "lab_agent_http_request_duration_seconds_bucket" in body, "API metrics missing")

        runtime = json.loads(docker("exec", container, "python", "-c", """
import json, os
from pathlib import Path
import torch
import chromadb
from sentence_transformers import SentenceTransformer, CrossEncoder
print(json.dumps({
    "uid": os.getuid(), "torch_version": torch.__version__, "torch_cuda": torch.version.cuda,
    "model_dependencies_imported": True,
    "knowledge_documents": len(list(Path('/app/data/kb').glob('*.md'))),
    "excluded_paths_absent": all(not Path(p).exists() for p in (
        '/app/.env', '/app/.venv', '/app/.venv-loadtest', '/app/tests', '/app/profiles', '/app/loadtests')),
    "writable_paths": all(os.access(p, os.W_OK) for p in (
        '/app/uploads', '/app/data/chroma', '/app/run', '/home/app/.cache/huggingface')),
}))
"""))
        require(runtime["uid"] != 0 and runtime["torch_cuda"] is None, "Image is not non-root CPU runtime")
        require(runtime["knowledge_documents"] > 0 and runtime["excluded_paths_absent"]
                and runtime["writable_paths"], "Runtime files or permissions are incorrect")
        dependency_check = docker("exec", container, "python", "-m", "pip", "check")
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(), "image": args.image,
            "image_id": image["Id"], "platform": image["Os"] + "/" + image["Architecture"],
            "size_bytes": image["Size"], "health": "healthy", "runtime": runtime,
            "checks": ["root", "openapi", "registration", "login", "authenticated_user", "laboratory_list",
                       "http_200_business_error", "prometheus_metrics", "model_imports", "non_root",
                       "runtime_permissions", "excluded_local_files", "pip_check"],
            "dependency_check": dependency_check,
            "scope": "Isolated SQLite container; no real database, model API, or embedding weights used",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except BaseException as exc:
        logs = subprocess.run(["docker", "logs", "--tail", "60", container], capture_output=True, text=True)
        print(logs.stdout + logs.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr)
        raise
    finally:
        subprocess.run(["docker", "stop", "--time", "35", container], capture_output=True, timeout=45)
        subprocess.run(["docker", "rm", "--force", "--volumes", container], capture_output=True, timeout=15)


if __name__ == "__main__":
    main()
