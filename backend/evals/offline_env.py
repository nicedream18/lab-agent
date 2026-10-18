"""离线测试在没有 backend/.env 时也能导入应用配置。"""

import os


def configure_offline() -> None:
    defaults = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "JWT_SECRET_KEY": "offline-tests-only",
        "LLM_API_KEY": "offline-tests-only",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_MODEL": "offline-tests-only",
        "LLM_FORCE_IPV4": "false",
        "LLM_CACHE_ENABLED": "false",
        "LANGSMITH_TRACING": "false",
        "KB_RERANKER_ENABLED": "false",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)
