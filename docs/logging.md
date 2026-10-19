# Loguru JSON 日志

后端通过 Loguru 0.7.3 将应用、标准库 `logging`、Uvicorn 和 Python warnings 日志统一输出到 **stdout**。每条记录是一行 JSON（NDJSON），异常堆栈中的换行会被转义。Compose 还将同样的 JSON 写入共享文件，供 [ELK 日志检索](elk.md) 采集。

## 启动与配置

本地安装依赖后，从 `backend/` 启动：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.server --host 127.0.0.1 --port 8000
```

`app.server` 在加载应用之前初始化日志，配置错误、数据库初始化失败等启动异常也有 JSON 堆栈。默认使用一个 worker 和 asyncio；退出时刷新队列，支持 SIGTERM 优雅关闭。

仍使用 Uvicorn CLI 时，指定日志配置以覆盖应用导入前的启动输出：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio --log-config app/logging.json
```

可在 `backend/.env` 设置以下参数，修改后重启生效：

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | 最低日志等级；支持 `TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL` |
| `LOG_SERVICE_NAME` | `lab-agent-backend` | Elasticsearch 中用于筛选服务的名称 |
| `LOG_ENVIRONMENT` | `development` | 环境名称，例如 `production` |
| `LOG_ACCESS_ENABLED` | `true` | 输出请求完成日志；关闭后仍记录未处理异常，受日志等级限制 |
| `LOG_FILE` | 空 | 可选 JSON 文件路径；每份 10 MB、保留 3 份备份，Compose 固定为 `/app/logs/backend.jsonl` |

配置示例：

```dotenv
LOG_LEVEL=INFO
LOG_SERVICE_NAME=lab-agent-backend
LOG_ENVIRONMENT=production
LOG_ACCESS_ENABLED=true
```

Docker 镜像和 systemd 服务均通过 `app.server` 启动。更新 Compose 中的后端：

```bash
docker compose up -d --build --wait backend
docker compose logs --no-log-prefix --no-color --tail=20 backend
```

## 字段与请求关联

以下示例为方便阅读展开；实际输出占一行。采用常见 ECS 字段命名，并增加 `request_id`、`route`、`outcome`：

```json
{
  "@timestamp": "2026-10-19T06:13:26.707Z",
  "log": {"level": "warning", "logger": "app.common.request_logging", "origin": {"file": {"name": "request_logging.py", "line": 75}, "function": "__call__"}},
  "service": {"name": "lab-agent-backend", "environment": "production"},
  "process": {"pid": 1, "thread": {"id": 12345}},
  "message": "HTTP request completed",
  "request_id": "example-request-123",
  "event": {"action": "http_request", "duration": 1557042, "outcome": "failure"},
  "http": {"request": {"method": "POST"}, "response": {"status_code": 200}},
  "route": "/api/auth/login",
  "outcome": "business_error"
}
```

- `@timestamp`：UTC 时间，毫秒精度。
- `event.duration`：**纳秒**；除以 `1,000,000` 得到毫秒。计时覆盖完整 SSE 输出及应用收尾，不是首 token 延迟。
- `route`：路由模板，例如 `/api/agent/trace/{conversation_id}`；未匹配路由记为 `__unmatched__`。
- `event.outcome`：`success` / `failure`；`outcome` 进一步区分 `success`、`http_error`、`business_error`、`agent_error`、`exception`、`cancelled`，与 [Prometheus 指标](monitoring.md) 共享失败标记。业务错误即使返回 HTTP 200，也记录为失败。
- `error.type`、`error.message`、`error.stack_trace`：异常类型、脱敏后的消息和完整堆栈；关闭局部变量展示。

请求进入时读取 `X-Request-ID`：只接受 1–64 位字母、数字、下划线或连字符，否则生成新的随机 ID。响应返回该 ID，同一请求内的异步任务及项目线程池日志自动携带它。可从浏览器响应头复制 ID，在日志平台搜索 `request_id`。

每个请求最多输出一条 `event.action=http_request` 完成记录；未处理异常还可能触发 Uvicorn 自身的异常记录。健康检查、成功的 `/metrics`、文档和静态上传文件请求默认不输出完成记录，减少周期采集噪声。

安装了 `jq` 时，可快速查看请求耗时：

```bash
docker compose logs --no-log-prefix --no-color backend | jq -c 'select(.event.action == "http_request") | {request_id, route, status: .http.response.status_code, duration_ms: (.event.duration / 1000000), outcome}'
```

## 编写日志与脱敏

现有代码可以继续使用标准库及 `%s` 参数：

```python
import logging

logger = logging.getLogger(__name__)
logger.info("检索完成，返回 %s 条结果", count)
```

新增代码也可直接使用 Loguru 的 `{}` 参数：

```python
from loguru import logger

logger.info("检索完成，返回 {} 条结果", count)
```

统一配置位于 `app/common/logging_config.py`，无需在各模块重复 `logger.add()`。采用 Loguru 队列输出，关闭颜色、自动回溯扩展和局部变量展示；仅导出明确指定的结构化字段，不输出任意绑定对象的 `repr()`。

请求完成记录不采集原始路径参数、查询参数、Authorization、请求体或响应体。消息和异常堆栈会替换配置中的模型/数据库/Redis/JWT/监控密钥，以及常见的密码键值、Bearer/Basic 凭证、URL 用户信息和 URL 查询串。这是辅助脱敏机制，不能识别所有个人信息；业务日志仍应避免主动写入完整用户问题、模型提示词或隐私数据。第三方库直接 `print()`、原生扩展的 stderr 不经过 Python 日志系统。

## ELK 采集

执行 `./scripts/start-elk.sh` 启动 Elasticsearch、Logstash 和 Kibana。Logstash 从只读共享卷读取后端 JSON 文件，解析后写入 `lab-agent-logs-*`，Kibana 自动创建默认数据视图，完整说明见 [ELK 部署指南](elk.md)。stdout 仍由 Docker `json-file` 保存；手工使用 `docker compose logs` 时加上 `--no-log-prefix --no-color`，避免前缀破坏 JSON。

初始化已将 `@timestamp` 映射为 `date`，`log.level`、`service.name`、`service.environment`、`request_id`、`route`、`outcome` 映射为 `keyword`，`event.duration` 为 `long`，`http.response.status_code` 为 `integer`。异常堆栈已经属于同一个 JSON 事件，无需额外用多行规则拼接 Python traceback。

实现参考 Loguru 官方的 [序列化与标准库日志转接示例](https://loguru.readthedocs.io/en/stable/resources/recipes.html) 和 [logger API](https://loguru.readthedocs.io/en/stable/api/logger.html)。

## 验证

从 `backend/` 执行：

```bash
.venv/bin/python -m pytest -q tests/test_json_logging.py
.venv/bin/python scripts/check_json_logs.py
```

22 项测试覆盖 JSON 格式、旧日志兼容、脱敏、异常、上下文隔离、线程池、SSE、取消和 stdout/文件一致性。真实进程验证使用临时 SQLite 和临时端口，不调用真实模型；检查启动、HTTP 200 业务错误、404、500、SSE、warnings、SIGTERM 关闭和导入失败。报告保存在被 Git 忽略的 `backend/profiles/json-logging-check.json`。
