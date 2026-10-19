"""Start Uvicorn with JSON logging, including unexpected bootstrap failures."""

import argparse
import os
import signal
from types import SimpleNamespace

from loguru import logger
import uvicorn

from app.common.logging_config import configure_logging, flush_logging, secret_values


def _exit_after_shutdown(_signum, _frame):
    # Uvicorn restores and re-raises SIGTERM after graceful shutdown. Convert
    # that final signal into a Python exit so Loguru's queue and multiprocessing
    # semaphore finalizers can run (notably on macOS).
    raise SystemExit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="app.main:app")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, choices=[1], default=1)
    parser.add_argument("--loop", choices=["asyncio", "uvloop", "auto"], default="asyncio")
    parser.add_argument("--timeout-graceful-shutdown", type=int, default=30)
    args = parser.parse_args(argv)
    # Do this before loading app.main/settings, so invalid startup configuration
    # and database initialization failures also have a JSON exception record.
    configure_logging(level="INFO", log_file="", redactions=secret_values(SimpleNamespace(**os.environ)))
    previous_terminate = signal.signal(signal.SIGTERM, _exit_after_shutdown)
    try:
        uvicorn.run(args.app, host=args.host, port=args.port, workers=args.workers,
                    loop=args.loop, timeout_graceful_shutdown=args.timeout_graceful_shutdown,
                    log_config=None, access_log=False)
    except Exception:
        logger.exception("Application startup failed")
        return 1
    finally:
        flush_logging()
        signal.signal(signal.SIGTERM, previous_terminate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
