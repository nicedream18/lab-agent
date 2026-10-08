from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # 后端项目的根路径


class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 24
    LLM_API_KEY: str
    LLM_BASE_URL: str
    LLM_MODEL: str

    # 单次模型调用的超时（秒）。必须显式设置：openai SDK 默认 600 秒，
    # 供应商只要卡一下，用户看到的就是「页面死了」。
    # 取值依据：健康状态下最慢的一次结构化调用实测 4.4s，15s 留了三倍余量。
    # 故意不取更小：卡太紧会把「供应商慢但在正常工作」误判成失败，
    # 用户拿到的是兜底文案而不是真正的推理结果，代价比多等几秒大得多。
    # 真正把最坏耗时压住的是 planner 里的单轮熔断 —— 一个节点超时之后，
    # 后面的节点不会再各等一遍，所以这 15s 只是「每轮上限」而不是「每节点上限」。
    LLM_TIMEOUT: float = 15.0

    # 流式回复的「首包」超时（秒）。
    # 实测供应商存在「TCP 连得上、但长时间不吐第一个字」的形态，
    # 这种情况下继续等没有任何意义，早点降级成本地摘要体验更好。
    LLM_FIRST_TOKEN_TIMEOUT: float = 10.0

    # 出网时是否强制大模型域名直走 IPv4。
    # 部分服务商的 IPv6 入口不接受公网 TLS 建连，而异步 HTTP 客户端
    # 不像同步客户端那样会自动回退 IPv4，表现为「非流式正常、流式报
    # ConnectError」。详见 app/utils/net.py。
    LLM_FORCE_IPV4: bool = True

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
