# 2 核 2 GB 单机部署文件

- `lab-agent.service`: 单个 Uvicorn worker，知识库延迟加载，CPU 推理线程数 2。
- `lab-agent.nginx.conf`: 静态前端、`/api/` 和 `/uploads/` 反向代理，SSE 不缓冲。

预期路径：后端位于 `/opt/lab-agent/backend`，前端构建文件位于
`/var/www/lab-agent`；服务账号为 `labagent`。`backend/.env` 必须在服务器上配置，
不要上传开发机的 `.env`，也不要写入 Git。需要 MySQL、Python 虚拟环境及
`backend/requirements.txt` 中的依赖；重启服务前运行离线回归。
在无法访问 Hugging Face 的机器上，需要把 `BAAI/bge-small-zh-v1.5`
完整模型文件放到 `/opt/lab-agent/models/bge-small-zh-v1.5`。

服务器若仅有 2 GB 内存，应先确认磁盘空间、数据库所在位置、swap 状态、
现有服务和端口占用，再安装依赖。首次知识库检索会加载 embedding 模型，
`KB_WARMUP_ON_STARTUP=false` 只延后加载，并不降低其运行时常驻内存。

有域名后应给 Nginx 配置 HTTPS；当前配置仅提供 HTTP 的 IP 访问入口。
