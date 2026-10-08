import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import UPLOAD_DIR, settings
from app.database import Base, engine
from app.services import kb_service, reservation_service

# 必须配一次根 logger，否则 logger.info 会被静默丢弃
# （root logger 默认级别是 WARNING，只有 error/exception 才打得出来）。
# 预热耗时这种关键信息就是 info 级别的，不配这里就等于没写。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

Base.metadata.create_all(bind=engine)
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.middleware.cors import CORSMiddleware

from app.api import api
from app.common.exceptions import (
    BusinessException,
    bussiness_excpetion_hadler,
    global_excpetion_hadler,
    http_excpetion_hadler,
    validation_excpetion_hadler,
)

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(
        reservation_service.run_expire_scan()
    )  # 启动项目开启异步的扫描任务

    # 向量库预热：模型加载 + 首次检索约 10s，交给后台线程，别落在第一个用户请求上。
    #
    # 为什么是 asyncio.to_thread？kb_service.warmup 是同步阻塞函数（sentence-transformers
    # 要导入 torch、读权重、算向量），直接调用会把事件循环钉死 10s。
    #
    # 为什么刻意不 await？实测：
    #   uvicorn 要等 lifespan startup 全部跑完才 bind 端口。
    #   用 await 版本，启动期间 curl 一直 exit=7（连接被拒），端口恰好在第 10s 才通，
    #   也就是页面这 10s 内根本打不开（前端 dev server 反代会直接 500）。
    #   不 await 的版本：端口 0.8s 就通，模型在后台慢慢加载。
    #
    # 代价是「端口通了但模型还没好」的窗口（实测 0~2s），这期间提问会同步等模型加载完。
    # 如果哪天要上生产、需要 readiness 探针语义（宁可启动慢也要保证模型就绪），
    # 把这一行换成 await asyncio.to_thread(kb_service.warmup) 即可。
    #
    # KB_WARMUP_ON_STARTUP=false 时完全不预热：模型推迟到第一次 AI 请求再加载
    # （search() → get_collection() 兜底，逻辑见 kb_service）。
    # 小内存服务器靠这个把空闲内存从 ~600MB 压到 ~120MB，代价是首个提问慢约 10s。
    warmup_task = (
        asyncio.create_task(asyncio.to_thread(kb_service.warmup))
        if settings.KB_WARMUP_ON_STARTUP
        else None
    )

    yield

    task.cancel()  # 关闭项目同时取消异步任务
    # 注意 to_thread 跑在线程里，cancel 只能取消「等待」这个动作，
    # 已经进到线程里的模型加载是停不下来的，进程退出时它会自然结束。
    if warmup_task is not None:
        warmup_task.cancel()


app = FastAPI(lifespan=lifespan)
app.include_router(api)
# 注册异常处理器
app.add_exception_handler(BusinessException, bussiness_excpetion_hadler)
app.add_exception_handler(HTTPException, http_excpetion_hadler)
app.add_exception_handler(RequestValidationError, validation_excpetion_hadler)
# 全局的异常兜底，必须放在最后注册！！
app.add_exception_handler(Exception, global_excpetion_hadler)
#  跨域中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # 允许的前端源，不要直接写 ["*"]
    allow_credentials=True,  # ✅ 关键：允许前端携带 Authorization token
    allow_methods=["*"],  # 允许所有请求方法 GET POST PUT DELETE OPTIONS
    allow_headers=["*"],  # 允许所有请求头（包含Authorization）
)
# 挂载静态资源：/uploads/xxx.jpg → uploads/xxx.jpg
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.get("/")
def root():
    return {"message": "Hello FastAPI"}
