"""Run Locust in its own venv; keep credentials out of arguments and reports."""

import argparse
from contextlib import ExitStack
from datetime import datetime
from getpass import getpass
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo


BACKEND = Path(__file__).resolve().parents[1]


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def finite_nonnegative(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return result


def wait_ready(host, process=None, startup_timeout=30):
    deadline = time.monotonic() + (startup_timeout if process else 3)
    while True:
        try:
            with urlopen(host + "/", timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        if process is not None and process.poll() is not None:
            raise RuntimeError("Temporary backend failed; inspect server.log")
        if time.monotonic() >= deadline:
            raise RuntimeError("Backend is unavailable; start it first or use --local")
        time.sleep(0.2)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario", choices=("api", "agent", "mixed", "rag"), default="api")
    parser.add_argument("--username")
    parser.add_argument("--accounts-file", type=Path)
    parser.add_argument("--users", type=positive, default=5)
    parser.add_argument("--spawn-rate", type=positive, default=1)
    parser.add_argument("--seconds", type=positive, default=60)
    parser.add_argument("--think-time", type=finite_nonnegative, default=0.5)
    parser.add_argument("--request-timeout", type=positive, default=180)
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--web-port", type=positive, default=8089)
    parser.add_argument("--local", action="store_true",
                        help="temporary SQLite backend; API or cached-model RAG; no external services")
    parser.add_argument("--embedding-batch-size", type=positive)
    parser.add_argument("--reranker-batch-size", type=positive)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    executable = BACKEND / ".venv-loadtest" / "bin" / "python"
    if not executable.is_file():
        parser.error("Install requirements-loadtest.txt into backend/.venv-loadtest first")
    if args.local and args.scenario not in ("api", "rag"):
        parser.error("--local supports api/rag; Agent requires a real test backend")
    if args.scenario == "rag" and not args.local:
        parser.error("rag uses only the temporary backend; pass --local")
    if (args.embedding_batch_size or args.reranker_batch_size) and args.scenario != "rag":
        parser.error("batch overrides require --scenario rag")
    if (args.embedding_batch_size or 32) > 256 or (args.reranker_batch_size or 8) > 64:
        parser.error("embedding/reranker batch limits are 256/64")
    if not args.web and args.users / args.spawn_rate >= args.seconds:
        parser.error("--seconds must exceed the time needed to spawn all users")
    parsed = urlparse(args.host)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        parser.error("--host must be a server root URL without credentials")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        parser.error("--host must be the server root, without /api or query parameters")
    output = args.output_dir or BACKEND / "profiles" / "locust" / (
        datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
        + "-" + secrets.token_hex(2))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    with ExitStack() as stack:
        host = args.host.rstrip("/")
        if args.local:
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="lab-loadtest-"))
            password = secrets.token_urlsafe(18)
            env.update({"LAB_LOADTEST_USERNAME": "loadtest", "LAB_LOADTEST_PASSWORD": password,
                        "LAB_LOADTEST_LOCAL_PASSWORD": password,
                        "LAB_LOADTEST_LOCAL_DB": str(Path(directory) / "baseline.sqlite"),
                        "LAB_LOADTEST_LOCAL_RAG": "1" if args.scenario == "rag" else "0",
                        "LAB_LOADTEST_LOCAL_JWT": secrets.token_urlsafe(48)})
            if args.scenario == "rag":
                env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                            "ANONYMIZED_TELEMETRY": "false",
                            "LAB_LOADTEST_LOCAL_CHROMA": str(Path(directory) / "chroma"),
                            "KB_EMBEDDING_BATCH_SIZE": str(args.embedding_batch_size or 32),
                            "KB_RERANKER_BATCH_SIZE": str(args.reranker_batch_size or 8)})
            # Do not inherit an unrelated account pool into the temporary server.
            env.pop("LAB_LOADTEST_TOKEN", None)
            env.pop("LAB_LOADTEST_ACCOUNTS_FILE", None)
            with socket.socket() as port_probe:
                port_probe.bind(("127.0.0.1", 0))
                port = port_probe.getsockname()[1]
            host = f"http://127.0.0.1:{port}"
            log = stack.enter_context((output / "server.log").open("w"))
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "loadtests.local_api:app",
                 "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
                 "--loop", "asyncio", "--no-access-log"], cwd=BACKEND, env=env,
                stdout=log, stderr=subprocess.STDOUT,
            )
            stack.callback(stop_process, process)
            wait_ready(host, process, startup_timeout=120 if args.scenario == "rag" else 30)
        else:
            wait_ready(host)
            if args.accounts_file:
                env["LAB_LOADTEST_ACCOUNTS_FILE"] = str(args.accounts_file.resolve())
            elif args.username:
                env.pop("LAB_LOADTEST_TOKEN", None)
                env.pop("LAB_LOADTEST_ACCOUNTS_FILE", None)
                env["LAB_LOADTEST_USERNAME"] = args.username
                env["LAB_LOADTEST_PASSWORD"] = getpass("Test account password: ")
            elif not (env.get("LAB_LOADTEST_TOKEN") or env.get("LAB_LOADTEST_ACCOUNTS_FILE")
                      or env.get("LAB_LOADTEST_USERNAME") and env.get("LAB_LOADTEST_PASSWORD")):
                parser.error("Use --username, --accounts-file or LAB_LOADTEST_TOKEN")
        manifest = {"host": host, "scenario": args.scenario, "users": args.users,
                    "spawn_rate": args.spawn_rate, "seconds": args.seconds,
                    "think_time": args.think_time,
                    "target": ("temporary SQLite + real cached-model RAG" if args.scenario == "rag"
                               else "temporary SQLite API baseline" if args.local else "configured backend"),
                    "real_llm": not args.local and args.scenario in ("agent", "mixed")}
        if args.scenario == "rag":
            with urlopen(host + "/__loadtest/info", timeout=5) as response:
                manifest["inference_before"] = json.load(response)
        (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
        command = [str(executable), "-m", "locust", "-f", "loadtests/locustfile.py",
                   "--only-summary",
                   "--host", host, "--scenario", args.scenario,
                   "--users", str(args.users), "--spawn-rate", str(args.spawn_rate),
                   "--think-time", str(args.think_time),
                   "--request-timeout", str(args.request_timeout),
                   "--stop-timeout", str(args.request_timeout + 5),
                   "--csv", str(output / "results"), "--csv-full-history",
                   "--html", str(output / "report.html"),
                   "--metrics-json", str(output / "agent-metrics.json"),
                   "--exit-code-on-error", "1"]
        if args.web:
            command.extend(["--web-host", "127.0.0.1", "--web-port", str(args.web_port)])
        else:
            command.extend(["--headless", "--run-time", f"{args.seconds}s"])
        print(f"Target: {host}\nReports: {output}", flush=True)
        process = subprocess.Popen(command, cwd=BACKEND, env=env)
        stack.callback(stop_process, process)
        result = process.wait()
        if args.scenario == "rag":
            with urlopen(host + "/__loadtest/info", timeout=5) as response:
                manifest["inference_after"] = json.load(response)
        manifest["exit_code"] = result
        (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
