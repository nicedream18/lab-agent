"""Keep test collection isolated from production services and credentials."""

import os

# Hugging Face reads offline mode at import time. Set it before collection can
# import sentence_transformers in another test module, not inside the smoke test.
if os.getenv("RUN_INFERENCE_MODELS") == "1":
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["ANONYMIZED_TELEMETRY"] = "false"

from evals.offline_env import configure_offline

configure_offline()


import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
