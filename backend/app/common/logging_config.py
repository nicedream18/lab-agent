"""Loguru NDJSON output shared by application and standard-library loggers."""

from datetime import timezone
import json
import logging
import os
import re
import sys
import traceback
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from loguru import logger

_configured = False
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@]+@")
_QUERY = re.compile(r'''([a-zA-Z][a-zA-Z0-9+.-]*://[^\s?<>"']+)\?[^\s<>"']+''')
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r'''(\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|authorization|password|passwd|token|secret(?:[_-]?key)?)\b["']?\s*[:=]\s*)'''
    r'''(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)''', re.IGNORECASE,
)


def secret_values(settings) -> tuple[str, ...]:
    values = []
    for name in ("JWT_SECRET_KEY", "LLM_API_KEY", "LANGSMITH_API_KEY", "METRICS_TOKEN"):
        value = getattr(settings, name, "")
        values.append(value.get_secret_value() if hasattr(value, "get_secret_value") else value)
    for name in ("DATABASE_URL", "REDIS_URL"):
        value = getattr(settings, name, "")
        value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        try:
            password = urlsplit(value).password
            if password:
                values.extend((password, unquote(password)))
        except ValueError:
            pass
    # Avoid replacing common single characters when a local URL has a tiny password.
    return tuple(sorted({v for v in values if isinstance(v, str) and len(v) >= 6}, key=len, reverse=True))


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    text = _ANSI.sub("", text)
    for value in secrets:
        text = text.replace(value, "[REDACTED]")
    text = _USERINFO.sub(r"\1[REDACTED]@", text)
    text = _QUERY.sub(r"\1?[REDACTED]", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    return _CREDENTIAL.sub(r"\1[REDACTED]", text)


def _serializer(service: str, environment: str, secrets: tuple[str, ...]):
    def format_record(record):
        extra = record["extra"]
        document = {
            "@timestamp": record["time"].astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "log": {"level": record["level"].name.lower(),
                    "logger": extra.get("source_name", record["name"]),
                    "origin": {"file": {"name": extra.get("source_file", record["file"].name),
                                         "line": extra.get("source_line", record["line"])},
                               "function": extra.get("source_function", record["function"])}},
            "service": {"name": service, "environment": environment},
            "process": {"pid": record["process"].id, "thread": {"id": record["thread"].id}},
            "message": _redact(record["message"], secrets),
        }
        if extra.get("request_id"):
            document["request_id"] = extra["request_id"]
        if extra.get("event_action"):
            document["event"] = {"action": extra["event_action"]}
            for name in ("duration", "outcome"):
                if "event_" + name in extra:
                    document["event"][name] = extra["event_" + name]
        if "http_method" in extra:
            document["http"] = {"request": {"method": extra["http_method"]},
                                "response": {"status_code": extra["http_status"]}}
            document["route"] = extra["route"]
            document["outcome"] = extra["outcome"]
        if record["exception"]:
            exc = record["exception"]
            document["error"] = {
                "type": exc.type.__name__, "message": _redact(str(exc.value), secrets),
                "stack_trace": _redact("".join(traceback.format_exception(exc.type, exc.value, exc.traceback)), secrets),
            }
        # Only explicit scalar/context fields are exported. Bound ORM/session or
        # arbitrary extra objects are intentionally not repr()'d into the log.
        record["extra"] = {"serialized": json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))}

    return format_record


class InterceptHandler(logging.Handler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Uvicorn's --log-config constructs this before importing app.main.
        if not _configured:
            configure_logging(intercept=False, log_file="", redactions=secret_values(SimpleNamespace(**os.environ)))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access":
            return  # The ASGI middleware emits structured, query-free access logs.
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.bind(source_name=record.name, source_function=record.funcName,
                    source_file=os.path.basename(record.pathname), source_line=record.lineno).opt(
            exception=record.exc_info,
        ).log(level, record.getMessage())


def configure_logging(*, level=None, service=None, environment=None, redactions=(), stream=None,
                      enqueue=True, intercept=True, log_file=None) -> None:
    global _configured
    level = level or os.getenv("LOG_LEVEL", "INFO")
    service = service or os.getenv("LOG_SERVICE_NAME", "lab-agent-backend")
    environment = environment or os.getenv("LOG_ENVIRONMENT", "development")
    log_file = os.getenv("LOG_FILE", "") if log_file is None else log_file
    logger.remove()
    # Serialize once before dispatch: stdout and the optional file must retain
    # identical context, and queued handlers must never pickle bound objects.
    logger.configure(patcher=_serializer(service, environment, tuple(redactions)))
    options = dict(level=level, format=lambda _: "{extra[serialized]}\n", colorize=False,
                   serialize=False, backtrace=False, diagnose=False, enqueue=enqueue)
    logger.add(sys.stdout if stream is None else stream, **options)
    if log_file:
        logger.add(log_file, rotation="10 MB", retention=3, encoding="utf-8", **options)
    _configured = True
    if not intercept:
        return
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logging.captureWarnings(True)
    # Remove existing text handlers (notably Uvicorn's CLI defaults) to avoid
    # duplicate lines. Keep third-party logger level decisions intact.
    for item in logging.root.manager.loggerDict.values():
        if isinstance(item, logging.Logger):
            item.handlers.clear()
            item.propagate = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
    for name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)


def flush_logging() -> None:
    logger.complete()
