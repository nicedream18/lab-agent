# 实验室预约 Agent

一个面向实验室与设备预约的 Agent 应用。用户可以用中文查实验室、设备、可预约时段和管理规定，也可以明确指示创建或取消自己的预约。前端展示流式回答、执行步骤和工具调用记录。

基于 LangGraph、FastAPI、MySQL 和 Chroma 构建实验室预约 Agent，实现 RAG 检索、智能工具调用、多步任务规划与 SSE 流式交互；引入 Redis 缓存、异步 I/O 和模型推理合批，Embedding 吞吐提升 3.94 倍，核心 API 在 20 并发下达到 800+ QPS、P99 小于 50 ms。

这个项目的重点是**让模型负责理解和选择工具，让代码负责写库边界与业务校验**。单步问题直接进入工具调用循环；需要多步查询时，模型可生成计划，按步骤调用工具，并在失败后调整尚未完成的步骤。

```mermaid
flowchart LR
    U[用户] --> A[需求分析]
    A -->|单步| T[模型选择工具]
    A -->|多步| P[任务规划]
    P --> T
    T --> G[按操作授权与执行校验]
    G --> B[预约业务服务 / 知识库]
    B --> T
    T --> R[回答与执行轨迹]
```

## 能做什么

- **真实业务工具**：查实验室、跨实验室找设备、查询时段与权限、创建和核验预约、查询和取消个人预约、检索制度文件。
- **写库控制**：仅根据本轮用户明确指定的操作开放对应工具。询问、含糊的“好的”、同时要求取消和重约都不会直接写库；模型即使输出其他写库工具名，执行节点也会拦截。取消目标不唯一时要求预约编号。
- **跨轮预约确认**：服务端按用户和会话保存 20 分钟的预约草稿。先提出预约再补充时间，或查到唯一可用时段后回复“确认预约”，都无需重复全部信息；提交参数必须与草稿一致，成功后重复确认不会再创建记录。改换实验室或日期会清掉旧时段。
- **跨轮取消确认**：同一天有多条待审核预约时，服务端展示并保存当前用户的候选编号；回复“都取消”、预约号或唯一时段即可继续。批量取消在同一事务中完成，任一条状态变化则整组不取消。
- **可观测性**：节点轨迹和工具调用结果分别落库；聊天记录留存 30 天，用户可查看和管理长期偏好。前端以 SSE 展示执行过程。
- **调用链分析**：可选 LangSmith 追踪，关联 LangGraph 节点、模型、工具与 RAG 各阶段，记录缓存命中和重试次数，见 [LangSmith 接入指南](docs/langsmith.md)。
- **指标监控**：通过 `/metrics` 暴露 API 请求数、延迟直方图与在途请求数，Prometheus 采集、Grafana 大盘展示 QPS、P99 和错误率，见 [监控指南](docs/monitoring.md)。
- **结构化日志**：Loguru 统一输出 JSON，包含请求 ID、接口耗时、失败原因和脱敏异常堆栈，为 ELK 采集准备，见 [日志指南](docs/logging.md)。
- **日志检索**：可选 Elasticsearch、Logstash 和 Kibana，采集后端 JSON 日志，按请求 ID、等级和接口检索，见 [ELK 指南](docs/elk.md)。
- **LLM 响应缓存**：可选 Redis 精确缓存，按用户和完整模型输入隔离，支持 TTL 与故障回退。启用方式见 [Redis 缓存指南](docs/llm-cache.md)。
- **知识库**：`backend/data/kb/*.md` 按章节切片、增量同步到本地 Chroma，使用中文 embedding 检索。新增文件需重启后端。

技术栈：FastAPI、SQLAlchemy、MySQL、LangGraph、LangChain 工具调用、Chroma、sentence-transformers、Vue 3、Vite、Element Plus。

## Docker Compose 一键启动

安装并启动 Docker 后，在项目根目录执行：

```bash
./scripts/start-stack.sh
```

自动构建并启动前端、FastAPI、MySQL、Redis、Prometheus 和 Grafana。前端为 <http://127.0.0.1:8080>，后端文档为 <http://127.0.0.1:8001/docs>，Grafana 为 <http://127.0.0.1:3001/d/lab-agent-api>。首次启动生成根目录 `.env` 保存基础设施密码；已有 `backend/.env` 用于模型服务配置。该栈使用独立数据库，初次使用可在页面注册账号。

配置优先级、数据卷、监控和验证方法见 [Compose 指南](docs/compose.md)。

需要同时启动 ELK 时执行 `./scripts/start-elk.sh`，日志页面为 <http://127.0.0.1:5601/app/discover>；用户名 `elastic`，密码在根目录 `.env` 的 `ELASTIC_PASSWORD`。

## 本地启动

需要 Python 3.11+、Node.js 22.18+（或 24.12+）、MySQL，以及支持 OpenAI 兼容接口和工具调用的模型服务。首次使用知识库时可能需要下载 embedding 模型 `BAAI/bge-small-zh-v1.5`。

1. 创建一个 MySQL 数据库，例如 `lab_agent`，并为应用账号授予建表及读写权限。后端启动时会通过 SQLAlchemy 创建缺失的表。
2. 安装依赖并配置密钥：

   ```bash
   python3 -m venv backend/.venv
   backend/.venv/bin/python -m pip install -r backend/requirements.txt
   cp backend/.env.example backend/.env
   ```

   编辑 `backend/.env`，填入 `DATABASE_URL`、`JWT_SECRET_KEY` 和自己的 DeepSeek API Key。示例默认使用 `https://api.deepseek.com` 与 `deepseek-flash`；`LLM_API_KEY` 必须换成真实密钥。调用时会关闭 DeepSeek 的思考模式，以兼容当前多轮工具调用。
3. 在两个终端分别运行：

   ```bash
   cd backend
   .venv/bin/python -m app.server --host 127.0.0.1 --port 8000
   ```

   ```bash
   cd frontend
   npm ci
   npm run dev
   ```

4. 打开 `http://localhost:5173/`，注册并登录后进入 `/manager/ai-chat`。接口文档位于 `http://localhost:8000/docs`。前端开发服务器会把 `/api` 代理到本地后端。

如需演示数据，可在**空的演示数据库**中执行 `backend/sql/seed_demo_data.sql`。该脚本会清空已有的实验室和设备数据，不应对正在使用的数据库运行。已有数据库变更目前需要手写迁移；`create_all()` 不会修改旧表结构。

后端也支持 Docker 构建：`docker build -t lab-agent-backend:local backend`。镜像验证、容器配置与持久化目录见 [Docker 运行指南](docs/docker.md)。

## 演示路径

- “哪间实验室有 RTX4090？明天下午有空吗？”：观察按条件规划与分步工具调用。
- “实验室能带食物吗？”：观察知识库检索及来源内容。
- “帮我预约明天下午的计算机实验室”：补齐目标时段后提交预约，查看工具审计与预约状态。
- “帮我取消这个预约”：存在多个候选时要求指定编号；“能帮我取消吗？”只询问，不执行取消。

## 回归与评测

性能分析入口与 cProfile / py-spy 使用方法见 [性能分析指南](docs/performance.md)；并发、吞吐与尾延迟测量见 [Locust 压测指南](docs/load-testing.md)。

Agent 的模型、Redis 异步调用和数据库线程池边界见 [异步 I/O 说明](docs/async-io.md)。

Embedding 与 Reranker 支持批处理，参数及本机吞吐对比见 [推理批处理](docs/inference-batching.md)。

无需数据库和模型服务即可运行离线检查：

```bash
cd backend
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=70
.venv/bin/python -m evals.run
```

`tests/` 用脚本化模型与工具真实运行 LangGraph 执行图，并以独立内存 SQLite 检查真实预约写入、跨用户取消拦截和歧义取消。`evals/write_authorization.json` 包含 34 条授权用例，其中故意让模型给出错误授权判断；评测报告通过数、写库工具 precision 和 recall，失败时返回非零退出码。用例可直接扩充并重跑。

**评测边界**：脚本化工具返回值验证控制流与安全约束，不等于真实 LLM 的任务成功率、真实知识库召回率或生产环境并发性能。34 条授权用例也无法穷尽中文表达；面向生产写操作仍应增加由服务端保存目标的显式确认流程。实际端到端效果需配置数据库和模型服务后另行测量。当前预约服务是先查冲突再写入，尚未通过事务锁或数据库约束解决并发抢同一时段的问题。

详细的实现取舍与历史改造记录见 [`docs/agent-architecture.md`](docs/agent-architecture.md)。
