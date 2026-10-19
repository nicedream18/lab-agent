# FastAPI 后端 Docker 镜像

前端、后端、MySQL、Redis 和监控一起启动，请使用根目录 `docker-compose.yml`，见 [Compose 一键启动指南](compose.md)。本文的命令用于单独构建和验证后端镜像。

`backend/Dockerfile` 将真实 FastAPI 应用和中文知识库文档打包到镜像，使用 Python 3.13.16、PyTorch 2.14.0 CPU 和现有 `requirements.txt`。构建阶段安装依赖，运行阶段以 UID 10001 的 `app` 用户启动单个 Uvicorn worker，监听容器内 8000 端口。

入口为 `python -m app.server`，统一配置 Loguru JSON stdout、启动异常和优雅关闭；请求字段、脱敏和采集方法见 [日志指南](logging.md)。

## 构建

项目根目录执行：

```bash
docker build -t lab-agent-backend:local backend
```

构建上下文必须是 `backend/`。该目录的 `.dockerignore` 使用允许列表，仅上传应用源码、依赖文件和 `data/kb/`，排除真实 `.env`、虚拟环境、上传文件、本地向量库、压测报告和测试数据。不要在 Dockerfile 的 `ARG` 或 `ENV` 中填写实际密钥。

PyTorch 从[官方 CPU 轮子源](https://pytorch.org/get-started/locally/)单独安装，避免镜像携带 CUDA 依赖。模型权重不在构建阶段下载；首次 RAG 调用需要下载或挂载模型缓存。依赖层使用 BuildKit pip 缓存，应用代码变化时可复用已安装依赖。当前直接依赖沿用项目固定版本，但全部传递依赖尚未生成完整锁文件。

可覆盖基础镜像和 CPU Torch 版本，修改后需要重新验证：

```bash
docker build --build-arg PYTHON_IMAGE=python:3.13.16-slim --build-arg TORCH_VERSION=2.14.0 -t lab-agent-backend:local backend
```

默认构建宿主 Docker 的架构；Apple Silicon 通常是 `linux/arm64`。部署到 x86 Linux 时需要单独构建并验证 `linux/amd64`，例如使用 `docker buildx build --platform linux/amd64 --load -t lab-agent-backend:amd64 backend`。本次构建结果与实测平台见本页末尾。

## 独立容器验证

无需提供业务数据库或模型凭证：

```bash
backend/.venv/bin/python backend/scripts/check_container.py
```

脚本启动刚构建的镜像，自动分配只绑定本机的空闲端口，使用容器内临时 SQLite 和随机测试账号，验证：

- Docker 健康检查、根路径和 OpenAPI。
- 注册、登录、JWT 鉴权、实验室列表接口。
- HTTP 200 业务错误与 Prometheus 计数、延迟直方图。
- 非 root 用户、可写的上传/索引/模型缓存目录、本地文件未被打包。
- Torch CPU、Chroma、SentenceTransformer/CrossEncoder 导入，以及 `pip check`。

测试结束会停止并删除该临时容器及其中的 SQLite 数据，不连接现有 MySQL、Redis 或 LLM，也不下载模型权重。已有宿主机 8000 后端和监控容器可继续运行。验证报告保存在被 Git 忽略的 `backend/profiles/docker-image-check.json`，包含镜像 ID、平台、大小与检查项。

可验证其他镜像：

```bash
backend/.venv/bin/python backend/scripts/check_container.py --image lab-agent-backend:local --output backend/profiles/docker-image-check.json
```

## 使用真实配置运行

准备容器运行专用的环境文件：

```bash
cp backend/.env backend/.env.docker
```

编辑 `backend/.env.docker`：数据库和 Redis 如果位于宿主机，将对应 URL 中的 `127.0.0.1` 改成容器可访问的 `host.docker.internal`，保留自己的账号、密码和数据库名。例如数据库地址的形式是 `mysql+pymysql://user:password@host.docker.internal:3306/lab_agent?charset=utf8mb4`。Linux 上还需确保服务绑定的地址允许 Docker 网关访问。容器里的 `127.0.0.1` 指向容器自身。

模型服务地址和真实密钥也在运行时提供。`.env.docker` 已由现有 `.gitignore` 忽略，不进入镜像。镜像默认设置 `KB_WARMUP_ON_STARTUP=false`，将模型加载延迟到首次使用；环境文件中的同名值会覆盖该默认值。可将它保持为 `false`，或在模型缓存准备好后开启预热。

启动到宿主机 8001 端口：

```bash
docker run -d --name lab-agent-backend \
  --publish 127.0.0.1:8001:8000 \
  --add-host host.docker.internal:host-gateway \
  --env-file backend/.env.docker \
  --mount type=volume,source=lab-agent-uploads,target=/app/uploads \
  --mount type=volume,source=lab-agent-chroma,target=/app/data/chroma \
  --mount type=volume,source=lab-agent-huggingface,target=/home/app/.cache/huggingface \
  lab-agent-backend:local
```

接口文档：<http://127.0.0.1:8001/docs>；原始指标：<http://127.0.0.1:8001/metrics>。启用 `METRICS_TOKEN` 时，抓取须发送对应 Bearer token。Docker 健康检查只判断应用 HTTP 可达，不保证模型、缓存和业务任务全部正常。

上传文件、Chroma 索引和模型缓存通过命名卷保留。若改用宿主目录绑定，目录需允许 UID 10001 写入。镜像内置原始知识库位于 `/app/data/kb`；自定义模型路径应挂载到容器内可读位置，并在环境文件中使用容器路径。

前端开发代理和现有 Prometheus 默认访问宿主机 8000；如果长期运行 8001 的容器，需要把前端代理和 `deploy/monitoring/prometheus.yml` 的目标端口同步改为 8001，或在停掉原宿主机后端后使用 `127.0.0.1:8000:8000`。不要同时让两个进程占用同一宿主端口。

查看状态、日志与停止：

```bash
docker inspect --format '{{.State.Health.Status}}' lab-agent-backend
docker logs --tail 100 lab-agent-backend
docker stop lab-agent-backend
docker rm lab-agent-backend
```

启动会运行应用现有的 `create_all()`、预约过期和会话清理任务；使用已有数据库时仍需按项目方式准备数据库和迁移。当前 Dockerfile 为 CPU 推理配置，不包含 GPU 运行时。

## 本次构建与验证记录

2026-10-19 已在本机 Docker Desktop 实际完成构建，并运行上述验证脚本通过全部检查：

| 项目 | 实测结果 |
| --- | --- |
| 镜像 | `lab-agent-backend:local` |
| 镜像 ID | `sha256:437d807afe2fb5adfb4cab7bd196659ee97a2ddab7dcf5d0c54471b5a66df312` |
| 平台 | `linux/arm64` |
| 本机 `docker image ls` 显示大小 | 2.44 GB（包含 Torch、Chroma 等模型运行依赖） |
| 容器健康状态 | `healthy` |
| 运行用户 | UID 10001 |
| Torch | `2.14.0+cpu`，无 CUDA |
| 知识库文件 | 5 份 |
| 验证 | 注册、登录、JWT 鉴权、实验室 API、业务错误、指标、目录权限、模型依赖导入、`pip check` 均通过 |

`docker build --check backend` 无警告；构建上下文约 401 KB。日志位于 `backend/profiles/docker-build.log`，镜像标识保存在 `backend/profiles/backend-image.id`，容器验证报告为 `backend/profiles/docker-image-check.json`。报告的 `size_bytes` 原样记录 `docker image inspect` 的 `Size`（本机为 500,106,827 字节），与上述镜像列表显示口径不同。这些本地产物均由现有忽略规则排除，不提交到 Git。

该验证使用临时容器和 SQLite，没有发起真实 LLM、MySQL 或模型权重推理测试。验证容器已清理，构建后的镜像保留在本机，可继续使用。`linux/amd64` 尚未实测。
