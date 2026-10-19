"""Parse every log line from a real isolated Uvicorn process, including failures."""

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def request(host, path, payload=None):
    request_id = "json-probe-" + secrets.token_hex(6)
    headers = {"X-Request-ID": request_id, "Authorization": "Bearer hidden-probe-header"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = Request(host + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    try:
        response = urlopen(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        if response.headers.get("x-request-id") != request_id:
            raise RuntimeError("Response request ID does not match the client ID")
        return response.status, response.read()


def parse_logs(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    with tempfile.TemporaryDirectory(prefix="lab-json-logs-") as directory:
        temporary = Path(directory)
        (temporary / "logging_probe.py").write_text('''
import asyncio
import logging
import os
import warnings
from fastapi.responses import StreamingResponse
from app.main import app

@app.get("/__logging_probe/boom")
def boom():
    logging.getLogger("app.logging_probe").info("中文消息 %s", "before error")
    warnings.warn("probe warning", UserWarning)
    raise RuntimeError("LLM_API_KEY=" + os.environ["LLM_API_KEY"] + "\\nsecond line")

@app.get("/__logging_probe/stream")
def stream():
    async def events():
        yield 'data: {"type":"token","content":"hello"}\\n\\n'
        await asyncio.sleep(.05)
        yield 'data: {"type":"done","answer":"done"}\\n\\n'
    return StreamingResponse(events(), media_type="text/event-stream")
''')
        (temporary / "broken_logging_probe.py").write_text(
            'import os\nraise RuntimeError("LLM_API_KEY=" + os.environ["LLM_API_KEY"])\n')
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        host = f"http://127.0.0.1:{port}"
        token_secret = secrets.token_urlsafe(32)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((directory, str(BACKEND))),
               "DATABASE_URL": "sqlite+pysqlite:///" + str(temporary / "test.sqlite"),
               "JWT_SECRET_KEY": secrets.token_urlsafe(48), "LLM_API_KEY": token_secret,
               "LLM_BASE_URL": "http://127.0.0.1:9/v1", "LLM_MODEL": "logging-probe",
               "LLM_FORCE_IPV4": "false", "LANGSMITH_TRACING": "false", "LLM_CACHE_ENABLED": "false",
               "KB_WARMUP_ON_STARTUP": "false", "KB_RERANKER_ENABLED": "false",
               "LOG_LEVEL": "INFO", "LOG_ACCESS_ENABLED": "true", "LOG_ENVIRONMENT": "verification"}
        stdout, stderr = temporary / "stdout.log", temporary / "stderr.log"
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen([sys.executable, "-m", "app.server", "--app", "logging_probe:app",
                                        "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=BACKEND, env=env, stdout=out, stderr=err)
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Logging probe failed to start")
                    try:
                        if request(host, "/")[0] == 200:
                            break
                    except (URLError, OSError):
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Logging probe startup timed out")
                    time.sleep(.2)
                checks = [
                    ("/api/auth/login", {"username": "missing", "password": "hidden-probe-password"}, 200),
                    ("/__logging_probe/private-unknown?token=hidden-probe-query", None, 404),
                    ("/__logging_probe/boom", None, 500),
                    ("/__logging_probe/stream", None, 200),
                ]
                for path, payload, expected in checks:
                    status, _ = request(host, path, payload)
                    if status != expected:
                        raise RuntimeError(f"Unexpected probe response: {status}")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"Graceful SIGTERM shutdown failed: {process.returncode}")
        rows = parse_logs(stdout)
        if stderr.read_text().strip():
            from app.common.logging_config import _redact, secret_values
            detail = _redact(stderr.read_text(), secret_values(SimpleNamespace(**env)))
            raise RuntimeError("Expected JSON logs on stdout; unexpected stderr: " + detail[:2000])
        output = stdout.read_text()
        for private in (token_secret, env["JWT_SECRET_KEY"], "hidden-probe-header", "hidden-probe-query", "hidden-probe-password", "private-unknown"):
            if private in output:
                raise RuntimeError("Probe log contains a private value")
        if any(not {"@timestamp", "log", "service", "message", "process"} <= row.keys() for row in rows):
            raise RuntimeError("A log line is missing structured fields")
        lifecycle = [row for row in rows if row["log"]["logger"] == "uvicorn.error"]
        if not any("Application startup complete" in row["message"] for row in lifecycle):
            raise RuntimeError("Uvicorn startup logs are missing")
        if not any("Finished server process" in row["message"] for row in lifecycle):
            raise RuntimeError("Uvicorn shutdown logs are missing")
        completed = [row for row in rows if row.get("event", {}).get("action") == "http_request"]
        if len(completed) != 4 or not any(row["outcome"] == "business_error" for row in completed):
            raise RuntimeError("Structured request completions are incomplete or duplicated")
        if not any(row.get("error", {}).get("type") == "RuntimeError" for row in completed):
            raise RuntimeError("Structured exception stack trace is missing")
        if not any(row["log"]["logger"] == "py.warnings" for row in rows):
            raise RuntimeError("Python warnings did not enter the JSON logger")
        broken = subprocess.run([sys.executable, "-m", "app.server", "--app", "broken_logging_probe:app"],
                                cwd=BACKEND, env=env, capture_output=True, text=True, timeout=20)
        boot_rows = [json.loads(line) for line in broken.stdout.splitlines() if line.strip()]
        if broken.returncode == 0 or broken.stderr or token_secret in broken.stdout:
            raise RuntimeError("Bootstrap failure was not safely logged")
        if not any(row.get("error", {}).get("type") == "RuntimeError" for row in boot_rows):
            raise RuntimeError("Bootstrap error JSON is missing")
        report = {"stdout_json_records": len(rows), "stderr_lines": 0,
                  "request_completions": len(completed), "bootstrap_exit_code": broken.returncode,
                  "verified": ["startup_shutdown", "request_ids", "business_error", "exception_stack_trace",
                               "sse_completion", "warnings", "credential_redaction", "bootstrap_failure"],
                  "example": next(row for row in completed if row["outcome"] == "business_error")}
        destination = BACKEND / "profiles/json-logging-check.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
