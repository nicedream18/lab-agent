from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 后端项目的根路径


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    LLM_API_KEY: str
    LLM_BASE_URL: str
    LLM_MODEL: str

    # 启动时是否在后台线程预热向量库（加载嵌入模型约 10s，常驻约 500MB）。
    # 本地开发开着体验最好（第一个提问不用等模型加载）；
    # 内存吃紧的服务器（比如 2C2G 的 ECS）应该设成 false，
    # 让模型在第一次 AI 请求时才加载，空闲内存能从 ~600MB 降到 ~120MB。
    KB_WARMUP_ON_STARTUP: bool = True

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8"
    )


settings = Settings()

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
}
