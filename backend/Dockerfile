# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.13.16-slim
FROM ${PYTHON_IMAGE} AS dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ARG TORCH_VERSION=2.14.0
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
COPY requirements.txt ./requirements.txt
# The application uses CPU inference. Install the CPU wheel first so Linux
# builds do not pull the CUDA runtime through sentence-transformers.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install -r requirements.txt \
    && python -m pip check

FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HF_HOME=/home/app/.cache/huggingface \
    KB_WARMUP_ON_STARTUP=false \
    KB_INFERENCE_DEVICE=cpu \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates libgomp1 libstdc++6 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY app/ ./app/
COPY data/kb/ ./data/kb/
RUN install -d -o app -g app \
    /app/uploads /app/data/chroma /app/run /app/logs /home/app/.cache/huggingface

USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).close()"]

# One worker preserves the current in-process Prometheus registry and model queues.
CMD ["python", "-m", "app.server", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--loop", "asyncio", "--timeout-graceful-shutdown", "30"]
