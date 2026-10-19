"""Isolated local incident exercise. Never imported by the production app."""

import asyncio
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

from app.common.exceptions import global_excpetion_hadler
from app.common.logging_config import configure_logging
from app.common.metrics import BUCKETS, HTTPMetricsMiddleware
from app.common.request_logging import RequestLoggingMiddleware

configure_logging(log_file=os.getenv("LOG_FILE", ""))


class DrillMetrics:
    """Use a separate namespace so production dashboards/alerts stay isolated."""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("lab_agent_drill_http_requests_total", "Drill completed requests",
                                ("method", "route", "status", "outcome"), registry=self.registry)
        self.duration = Histogram("lab_agent_drill_http_request_duration_seconds", "Drill HTTP duration",
                                  ("method", "route"), buckets=BUCKETS, registry=self.registry)
        self.in_progress = Gauge("lab_agent_drill_http_requests_in_progress", "Drill active requests",
                                 registry=self.registry)


class Fault(BaseModel):
    enabled: bool


class LLMUpstreamTimeout(RuntimeError):
    pass


upstream = FastAPI()
upstream.state.fault_enabled = False


@upstream.get("/")
async def upstream_health():
    return {"status": "ok", "exercise": True}


@upstream.post("/control/fault")
async def upstream_fault(fault: Fault):
    upstream.state.fault_enabled = fault.enabled
    return {"enabled": fault.enabled}


@upstream.post("/v1/chat/completions")
async def completion():
    await asyncio.sleep(1.6 if upstream.state.fault_enabled else .03)
    return {"answer": "演练用本地模型响应"}


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(base_url="http://incident-upstream:8000",
                                timeout=httpx.Timeout(.8, connect=.3, pool=.3),
                                limits=httpx.Limits(max_connections=8)) as client:
        app.state.client = client
        yield


api = FastAPI(lifespan=lifespan)
metrics = DrillMetrics()
api.add_middleware(HTTPMetricsMiddleware, metrics=metrics)
api.add_middleware(RequestLoggingMiddleware)
api.add_exception_handler(Exception, global_excpetion_hadler)


@api.get("/")
async def api_health():
    return {"status": "ok", "exercise": True}


@api.get("/metrics")
async def scrape():
    return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})


@api.post("/control/fault")
async def control(fault: Fault):
    response = await api.state.client.post("/control/fault", json=fault.model_dump())
    response.raise_for_status()
    return response.json()


@api.get("/api/drill/catalog")
async def catalog():
    await asyncio.sleep(.005)
    return {"items": [], "exercise": True}


@api.post("/api/drill/chat")
async def chat():
    try:
        response = await api.state.client.post("/v1/chat/completions", json={"message": "exercise"})
        response.raise_for_status()
        return response.json()
    except httpx.ReadTimeout as exc:
        raise LLMUpstreamTimeout(
            "Exercise LLM upstream exceeded read_timeout=0.8s; upstream=http://incident-upstream:8000"
        ) from exc
