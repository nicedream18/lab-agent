# 2 核 2 GB 单机部署文件

完整应用栈使用根目录 `docker-compose.yml` 和 `./scripts/start-stack.sh`，包括前后端、MySQL、Redis 和监控，详见 [Compose 指南](../docs/compose.md)。完整栈与本目录的小内存宿主机部署方案具有不同资源需求。

FastAPI 后端 Dockerfile 位于 `backend/Dockerfile`，构建和容器验证见 [Docker 运行指南](../docs/docker.md)。下文的 systemd 方案使用宿主机虚拟环境，可按部署方式选择。

- `lab-agent.service`: 单个 Uvicorn worker，知识库延迟加载，CPU 推理线程数 2。
- `lab-agent.nginx.conf`: 静态前端、`/api/` 和 `/uploads/` 反向代理，SSE 不缓冲。

服务通过 `app.server` 输出 Loguru JSON 日志，systemd 收集到 journal；使用 `journalctl -u lab-agent -o cat` 可查看原始 JSON。日志等级、环境名称和后续 ELK 采集见 [日志指南](../docs/logging.md)。

根目录 `./scripts/start-elk.sh` 提供 Docker 本地 ELK：Logstash 读取 Compose 后端的共享 JSON 文件，Elasticsearch 存储、Kibana 检索，配置位于 `deploy/elk/`，见 [ELK 指南](../docs/elk.md)。该链路不自动采集本段 systemd 服务的 journal。

预期路径：后端位于 `/opt/lab-agent/backend`，前端构建文件位于
`/var/www/lab-agent`；服务账号为 `labagent`。`backend/.env` 必须在服务器上配置，
不要上传开发机的 `.env`，也不要写入 Git。需要 MySQL、Python 虚拟环境及
`backend/requirements.txt` 中的依赖；重启服务前运行离线回归。
如需启用 LLM 响应缓存，还需部署 Redis 并在后端 `.env` 配置
`LLM_CACHE_ENABLED=true` 和 `REDIS_URL`，详见 [Redis 缓存指南](../docs/llm-cache.md)。
在无法访问 Hugging Face 的机器上，需要把 `BAAI/bge-small-zh-v1.5`
完整模型文件放到 `/opt/lab-agent/models/bge-small-zh-v1.5`。

服务器若仅有 2 GB 内存，应先确认磁盘空间、数据库所在位置、swap 状态、
现有服务和端口占用，再安装依赖。首次知识库检索会加载 embedding 模型，
`KB_WARMUP_ON_STARTUP=false` 只延后加载，并不降低其运行时常驻内存。
此 2 GB 模板设置 `KB_RERANKER_ENABLED=false`，保留 Embedding 合批并将 batch 限为 8。
默认的 `BAAI/bge-reranker-v2-m3` 权重约 2.1 GiB，还需要推理内存；应在更大内存的
机器上开启精排或选择经过验证的小模型，配置见 [批处理说明](../docs/inference-batching.md)。

有域名后应给 Nginx 配置 HTTPS；当前配置仅提供 HTTP 的 IP 访问入口。

`monitoring/` 提供 Prometheus 与 Grafana Compose、采集和记录/告警规则、自动加载的大盘，详见 [API 监控指南](../docs/monitoring.md)。监控容器会额外占用内存，2 GB 主机可将监控服务放到其他机器；后端保持单 worker，指标端点无需代理到公网。
