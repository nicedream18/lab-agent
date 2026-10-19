# Docker Compose 一键启动

根目录 `docker-compose.yml` 管理完整的 6 服务应用栈：Vue/Nginx 前端、FastAPI、MySQL、Redis、Prometheus、Grafana。服务通过 Compose DNS 连接，不依赖宿主机数据库或 Redis；前端通过同源 `/api` 代理访问后端。

## 一条命令启动

本机需已启动 Docker，并提供 Docker Compose v2.24+、POSIX shell 和 OpenSSL；macOS / Linux 终端中，在项目根目录执行：

```bash
./scripts/start-stack.sh
```

脚本首次生成被 Git 忽略的根目录 `.env`，写入随机 MySQL、JWT、Grafana 和可选 ELK 的密钥，权限为 0600；已有非空配置会保留。然后执行 `docker compose up -d --build --wait --wait-timeout 360`，构建镜像并等待所有服务健康。首次运行需要联网拉取基础镜像、安装后端依赖并构建前端。

需要一起启动 Elasticsearch、Logstash 和 Kibana 时执行 `./scripts/start-elk.sh`；日志页面与采集、保留策略见 [ELK 指南](elk.md)。默认应用栈为 6 个常驻服务，启用 ELK 后增加 3 个常驻服务。

配置已初始化后，也可直接使用：

```bash
docker compose up -d --build --wait --wait-timeout 240
```

无需在宿主机安装 Node.js、Python 依赖、MySQL 或 Redis。开发机原有的 8000 后端、9090 Prometheus、3000 Grafana 可以继续运行；完整栈采用以下端口：

| 服务 | 地址 / 用途 |
| --- | --- |
| 前端 | <http://127.0.0.1:8080> |
| 后端文档 | <http://127.0.0.1:8001/docs> |
| 后端原始指标 | <http://127.0.0.1:8001/metrics> |
| Prometheus | <http://127.0.0.1:9091>，采集目标 `backend:8000` |
| Grafana 大盘 | <http://127.0.0.1:3001/d/lab-agent-api> |
| MySQL / Redis | 仅在 Compose 网络中开放，不映射宿主机端口 |

首次打开前端可自行注册账号。**这是独立数据库**，不会自动导入现有宿主机数据库中的用户、实验室和预约，初始实验室列表为空。MySQL 初始化由官方镜像创建数据库和应用账号，后端的 `create_all()` 创建表；不会自动运行包含 `TRUNCATE` 的历史演示 SQL。

Grafana 用户名默认 `admin`，初始密码为**根目录 `.env`** 中的 `GRAFANA_ADMIN_PASSWORD`。它与旧的 `deploy/monitoring/.env` 是两套独立配置，使用独立 Grafana 数据卷。

## 模型与环境配置

两类配置分开保存：

- 根目录 `.env`：Compose 基础设施密码、JWT 密钥、端口、缓存和启动预热开关；模板是根目录 `.env.example`。
- `backend/.env`：沿用已有的 LLM API Key、模型地址、LangSmith 和检索参数；不进入构建上下文。

后端先加载 `deploy/stack/backend.defaults.env`，再加载可选的 `backend/.env`。Compose 的 `environment` 最后覆盖数据库地址、JWT、Redis 地址、缓存开关、预热和监控配置，以保证访问容器服务而不是宿主机 `127.0.0.1`：

| 参数 | Compose 行为 |
| --- | --- |
| `DATABASE_URL` | 使用根目录 MySQL 应用账号，连接 `mysql:3306` |
| `JWT_SECRET_KEY` | 使用根目录 `.env` 的独立密钥 |
| `REDIS_URL` | `redis://redis:6379/0` |
| `LLM_CACHE_ENABLED` | 默认 `true`，可在根目录 `.env` 关闭 |
| `KB_WARMUP_ON_STARTUP` | 默认 `false`，模型延迟到首次 RAG 使用时加载 |
| `METRICS_ENABLED` / `METRICS_TOKEN` | 开启指标、token 为空，供内部 Prometheus 抓取；宿主访问仅绑定回环地址 |

已有 `backend/.env` 的真实模型配置会直接用于该栈。新克隆项目没有该文件时，Web、注册登录和业务 API 可以启动；真实 Agent 问答仍需复制 `backend/.env.example` 为 `backend/.env`，配置有效的模型密钥，再运行启动命令。密钥修改会使后端重建容器，不需要重新安装依赖。

Embedding 和 Reranker 权重在首次使用时下载到 `huggingface-cache` 卷，模型与 Chroma 数据重启后保留。当前镜像为 CPU 推理，Reranker 内存需求见[批处理说明](inference-batching.md)。MySQL 和 Redis 就绪后才启动后端，后端健康后才启动前端和 Prometheus，Prometheus 健康后启动 Grafana，依据 [Compose 健康依赖机制](https://docs.docker.com/compose/how-tos/startup-order/)。HTTP 健康不等于模型已经加载或回答质量已通过评测。

需要改端口时，在根目录 `.env` 设置：

```dotenv
FRONTEND_PORT=8080
BACKEND_PORT=8001
PROMETHEUS_PORT=9091
GRAFANA_PORT=3001
```

前端使用相对 `/api` 路径，改前端端口无需重新设置 API 地址。Grafana 的数据源使用内部 `http://prometheus:9090`，大盘的 Explore 链接使用当前 Grafana 地址，不依赖宿主机监控端口。

## 数据与日常操作

Compose 项目名为 `lab-agent`，使用项目专属网络与 8 个应用命名卷：MySQL、Redis AOF、上传文件、Chroma、Hugging Face 模型缓存、Prometheus 数据、Grafana 配置、应用 JSON 日志。ELK 额外使用 Elasticsearch、Logstash、Kibana 的 3 个数据卷。Redis 最大缓存内存为 128 MB，并使用 LRU 淘汰；监控数据保留 7 天 / 512 MB，容器日志按 10 MB × 3 轮转。

查看状态和日志：

```bash
docker compose ps
docker compose logs --tail=100 backend
docker compose logs --tail=100 frontend
```

后端通过 Loguru 输出逐行 JSON，包含请求 ID、耗时和脱敏异常堆栈。将日志交给 JSON 解析器时使用 `docker compose logs --no-log-prefix --no-color backend`；参数与 ELK 采集说明见 [日志指南](logging.md)。

停止整栈并保留数据：

```bash
docker compose down
```

再次启动仍执行 `./scripts/start-stack.sh`。不要删除数据卷来解决普通启动问题。MySQL 密码只在空数据卷初始化时设置；已有数据库改密码需要同步更新数据库账号，单改根目录 `.env` 不会修改已有账号。Grafana 初始密码同样只在首次创建其数据库时生效。

Nginx 支持 Vue history 路由、API 和上传资源代理，SSE 关闭缓冲并允许 300 秒等待；`/metrics` 不经前端入口代理。部署公网时应另外配置 HTTPS、访问地址和所需的访问控制。

## 已执行验证

2026-10-19 已在本机 Docker Desktop `linux/arm64` 构建并启动全部 6 个服务，均达到 `healthy`。实际检查：

- 前端页面和 Vue history 路径可访问，浏览器正确渲染登录页。
- 经过 Nginx 完成 MySQL 注册、登录、JWT 鉴权、实验室和设备查询。
- 经过 `/api/files/upload` 上传文件，再由 `/uploads/` 取回同样内容。
- 后端连接 MySQL `utf8mb4` 数据库，并成功读写 Compose Redis；LLM 缓存开关为开启。
- Prometheus 抓取 `backend:8000` 为 `UP`，可观察真实请求产生的 QPS。
- Grafana 数据源、大盘自动导入和查询链路通过，查询到 `UP=1`。
- 再次运行同一启动命令后，6 个服务仍健康，根目录配置文件内容未改变，已有数据卷被复用。

复现完整检查：

```bash
backend/.venv/bin/python backend/scripts/check_stack.py
```

验证脚本仅依赖 Python 标准库，也可使用 `python3` 执行。它会创建并清理专属临时账号和上传文件，Redis 测试键会立即删除；少量验证流量会进入本栈监控。结果保存为被 Git 忽略的 `backend/profiles/compose-check.json`。本次未调用真实 LLM，也未下载模型权重或验证真实 Agent 答案。
