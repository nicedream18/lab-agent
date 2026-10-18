"""Verify with a temporary Redis Unix socket; no external model or database."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from redis import Redis
from redis.exceptions import RedisError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="run the whole backend suite with coverage")
    args = parser.parse_args()
    executable = shutil.which("redis-server")
    if executable is None:
        raise SystemExit("redis-server is missing; install Redis or set TEST_REDIS_URL for pytest")
    backend = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="lab-redis-") as directory:
        # /tmp avoids macOS Unix-socket path length limits in nested tmpdirs.
        socket_path = f"/tmp/lab-redis-{os.getpid()}.sock"
        log = (Path(directory) / "redis.log").open("w")
        process = subprocess.Popen(
            [executable, "--port", "0", "--unixsocket", socket_path,
             "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
             "--dir", directory], stdout=log, stderr=subprocess.STDOUT,
        )
        client = Redis.from_url("unix://" + socket_path, socket_timeout=0.2)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if client.ping():
                        break
                except RedisError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Temporary Redis did not start")
                    time.sleep(0.05)
            env = {**os.environ, "TEST_REDIS_URL": "unix://" + socket_path}
            command = [sys.executable, "-m", "pytest"]
            if args.full:
                command.extend(["--cov=app", "--cov-report=term-missing",
                                "--cov-report=json:coverage.json", "--cov-fail-under=80"])
            else:
                command.extend(["tests/test_llm_cache.py", "tests/test_llm_cache_redis.py", "-q"])
            return subprocess.call(command, cwd=backend, env=env)
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
            Path(socket_path).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
