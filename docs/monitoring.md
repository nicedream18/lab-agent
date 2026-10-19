# Prometheus 与 Grafana API 监控

完整应用栈现在可通过根目录 [Compose](compose.md) 一起启动，监控地址为 Prometheus `9091`、Grafana `3001`。下文的 `deploy/monitoring/compose.yml` 是针对宿主机后端的独立监控方案，使用 `9090` 和 `3000`。

后端默认通过 `GET /metrics` 暴露请求计数、延迟直方图和在途请求数。Prometheus 定期抓取，并从计数器增量计算 QPS、错误率和 P99；请求处理期间不访问 Prometheus，也不写监控数据库。

后端同时输出 [Loguru JSON 日志](logging.md)，可通过响应头 `X-Request-ID` 定位具体请求的异常和耗时；请求 ID 不作为 Prometheus 标签，避免产生大量时间序列。

需要集中检索这些日志时，使用 [ELK 部署](elk.md)，本机 Kibana 地址为 <http://127.0.0.1:5601/app/discover>；Prometheus/Grafana 继续用于聚合指标分析。

## 本地启动与查看

项目根目录执行：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements.txt
docker compose -f deploy/monitoring/compose.yml up -d
```

后端使用已有数据库配置正常启动；本实现使用单 worker：

```bash
cd backend
.venv/bin/python -m app.server --host 127.0.0.1 --port 8000
```

- 后端原始指标：<http://127.0.0.1:8000/metrics>。
- Grafana 大盘：<http://127.0.0.1:3000/d/lab-agent-api>，登录后自动加载，无需手动添加数据源或导入 JSON。
- Prometheus 查询页面：<http://127.0.0.1:9090>，在 Query 页面输入下文的 PromQL。
- 采集状态：<http://127.0.0.1:9090/targets>，`lab-agent` 应为 `UP`；也可查询 `up{job="lab-agent"}`，1 表示抓取成功，0 表示失败。

先从前端执行几次 API 请求，再等待至少两次抓取（默认间隔 5 秒）。尚未访问过的路由没有请求序列；没有完成样本的延迟分位数可能显示 `NaN`。`UP` 只表示指标端点可达，不代表数据库、模型或 Agent 全部健康。

2026-10-19 接入验证时，已启动本机 Prometheus，使用临时 SQLite 后端验证真实抓取及查询；随后清理临时后端。本机业务后端 8000 端口当时没有运行，因此业务采集目标为 `DOWN`，按上面的命令启动后会自动恢复抓取。

## Grafana 大盘

Grafana `13.2.4` 随同一份 Compose 启动，仅监听本机 3000 端口；Prometheus 数据源使用容器内地址 `http://prometheus:9090`。浏览器查看地址仍是宿主机的 `127.0.0.1`。版本及安装方式可查 [Grafana 官方下载页面](https://grafana.com/grafana/download?edition=oss&platform=docker)。

本机已生成 `deploy/monitoring/.env`（被 Git 忽略、权限 0600），包含用户名 `admin` 和随机初始密码；**查看该文件的 `GRAFANA_ADMIN_PASSWORD` 值登录**。它是监控 Compose 的配置，与 `backend/.env` 分开。

新环境首次启动前可自行配置：

```bash
cp deploy/monitoring/.env.example deploy/monitoring/.env
```

将其中密码改为自己的值，再执行上文的 Compose 启动命令。如果不创建该文件，Grafana 使用 `admin/admin`，首次登录要求改密码。环境变量只在首次创建 Grafana 数据库时初始化管理员；后续改密码使用页面的用户设置，修改 `.env` 不会重置已有账号。

大盘位于 **Dashboards → Lab Agent → Lab Agent · API 监控**，也是登录后的默认首页：

| 区域 | 内容 |
| --- | --- |
| 概览卡片 | 采集 UP/DOWN、API QPS、P99、API 失败率、平均延迟、在途请求 |
| 趋势图 | 各接口 QPS、P50/P95/P99、按原因拆分的失败请求速率、在途请求 |
| 筛选与时间 | 实例、接口均可多选；默认最近 15 分钟、每 5 秒刷新 |

API 图表随接口筛选变化；采集状态和在途请求是实例级指标。QPS/失败率使用 `rate`，延迟按直方图桶合并后计算；窗口由 Grafana 的 `$__rate_interval` 根据时间范围和 5 秒采集间隔自动选择。它可能不同于下面固定 5 分钟的记录规则。P99 不对不同接口或实例的分位数取平均。

`DOWN` 表示抓取失败，`No data` 表示没有可用样本。刚启动、后端未运行或尚未产生 API 流量时，这些显示是预期状态；没有失败序列时“失败请求 / 秒”也可能无数据。无流量时延迟没有定义，不能当作零延迟。概览中的失败率包含 4xx、业务错误和 Agent 降级，需要判断服务端故障时使用下文的 HTTP 5xx 查询。

配置文件：

- `deploy/monitoring/grafana/provisioning/datasources/prometheus.yml`：数据源，固定 UID `lab-agent-prometheus`。
- `deploy/monitoring/grafana/provisioning/dashboards/lab-agent.yml`：自动导入配置。
- `deploy/monitoring/grafana/dashboards/lab-agent-api.json`：大盘，固定 UID `lab-agent-api`。

大盘从文件自动加载，文件变更每 30 秒检测一次；刷新页面可查看更新。仓库中的这份大盘不允许直接保存 UI 编辑，修改时更新 JSON，或复制成新的大盘后自行编辑。机制见 [Grafana 官方 provisioning 文档](https://grafana.com/docs/grafana/latest/administration/provisioning/)。账号与自建大盘保存在 `grafana-data` 命名卷，普通 `compose down` 后重启仍保留。

只重启 Grafana：

```bash
docker compose -f deploy/monitoring/compose.yml restart grafana
docker compose -f deploy/monitoring/compose.yml logs --tail=50 grafana
```

## 指标口径

| 指标 | 类型与标签 | 含义 |
| --- | --- | --- |
| `lab_agent_http_requests_total` | Counter；`method, route, status, outcome` | 每个结束或中断的请求计一次；QPS 按完成速率计算 |
| `lab_agent_http_request_duration_seconds` | Histogram；`method, route` | 服务端应用处理时长，单位秒，包含成功与失败样本 |
| `lab_agent_http_requests_in_progress` | Gauge；无标签 | 当前正在处理的请求总数，包含未结束的 SSE |

直方图导出 `_bucket`、`_sum`、`_count` 序列，桶边界覆盖 1 ms 到 300 s，并包含 `+Inf`。计时从请求进入监控中间件开始，直到应用处理返回，包含完整 SSE 生成、等待和收尾保存；它不等于模型首 token 时间，也不包含客户端到服务端的完整网络耗时。

`outcome` 是互斥分类，每次请求只计一次：

| 值 | 判定 |
| --- | --- |
| `success` | 正常完成且没有显式失败标记 |
| `http_error` | HTTP 4xx/5xx 且没有更具体的失败标记，例如 401、422 |
| `business_error` | 项目的 `BusinessException`，即使响应仍是 HTTP 200 |
| `agent_error` | Agent 的错误事件、失败节点/工具、空答案、缺失完成事件，或工作流最终状态包含错误/失败工具结果；包含返回兜底文本的情况 |
| `exception` | 未处理异常，包括流开始后发生的异常 |
| `cancelled` | 请求取消或未发送完整响应；响应头尚未发出时记为合成状态 499 |

失败后重规划成功的请求，如果本轮存在失败工具或失败事件，也归为 `agent_error`，用于暴露发生过异常的执行。指标不能判断自然语言答案是否正确，也不会把所有业务拒绝都等同于服务故障；服务可用性应另看 HTTP 5xx 比例。

路由使用 `/api/agent/trace/{conversation_id}` 这类模板；未知 API 路径统一为 `__unmatched_api__`。指标不带用户、会话编号、查询参数、问题正文或异常文本。`/metrics` 自身、文档和上传静态资源不参与统计；下文 API 查询还排除了根路径等非 API 请求。

## 常用 PromQL

**API 总 QPS**，最近 5 分钟的平均每秒完成请求数：

```promql
sum(lab_agent:api_qps:rate5m)
```

**按接口查看 QPS**：

```promql
sum by (method, route) (lab_agent:api_qps:rate5m)
```

**按接口查看 P99，单位秒**：

```promql
histogram_quantile(0.99,
  sum by (method, route, le) (
    rate(lab_agent_http_request_duration_seconds_bucket{route=~"/api/.*|__unmatched_api__"}[5m])
  )
)
```

把 `0.99` 改为 `0.95` 可看 P95；乘以 `1000` 转为毫秒。单实例也可以直接查 `lab_agent:api_p99_seconds:rate5m`。多实例先合并桶再计算分位数，不能对各实例 P99 取平均。直方图分位数是桶内插值估计，与 Locust 的客户端测量及分桶方式不同，参见 [Prometheus 官方说明](https://prometheus.io/docs/practices/histograms/)。

**API 平均延迟，单位秒**：

```promql
sum(rate(lab_agent_http_request_duration_seconds_sum{route=~"/api/.*|__unmatched_api__"}[5m]))
/
sum(rate(lab_agent_http_request_duration_seconds_count{route=~"/api/.*|__unmatched_api__"}[5m]))
```

没有请求时平均延迟无定义；不要把它解释为零延迟。

**API 总失败率，百分比**（包含业务错误、Agent 显式失败、HTTP 错误、异常与取消）：

```promql
100 * (
  sum(rate(lab_agent_http_requests_total{route=~"/api/.*|__unmatched_api__",outcome!="success"}[5m]))
  or vector(0)
)
/ clamp_min(sum(lab_agent:api_qps:rate5m), 1e-9)
```

按接口可查 `100 * lab_agent:api_failure_ratio:rate5m`。无失败样本时该记录规则补零；无流量时零失败率只表示没有观测到失败。

**仅 HTTP 5xx 比例，百分比**：

```promql
100 * (
  sum(rate(lab_agent_http_requests_total{route=~"/api/.*|__unmatched_api__",status=~"5.."}[5m]))
  or vector(0)
)
/ clamp_min(sum(lab_agent:api_qps:rate5m), 1e-9)
```

查看在途请求直接查询 `lab_agent_http_requests_in_progress`。拆分失败原因可以使用 `sum by (outcome) (rate(lab_agent_http_requests_total{route=~"/api/.*|__unmatched_api__",outcome!="success"}[5m]))`。

## 配置、部署与限制

`backend/.env` 可配置：

```dotenv
METRICS_ENABLED=true
METRICS_TOKEN=
```

缺省启用监控，token 为空时允许直接抓取；`METRICS_ENABLED=false` 会关闭中间件并移除 `/metrics`。修改配置后重启后端。填写 `METRICS_TOKEN` 后，抓取须发送 `Authorization: Bearer <token>`。启用 token 时，把相同值存入被 Git 忽略的 `deploy/monitoring/metrics-token.local`，在 Compose 的 `volumes` 中增加：

```yaml
- ./metrics-token.local:/etc/prometheus/metrics-token:ro
```

然后取消 `prometheus.yml` 中 `authorization` 三行的注释，重新执行 `docker compose -f deploy/monitoring/compose.yml up -d --force-recreate`。token 文件只写值，不写 `Bearer`；文件需允许容器内 Prometheus 用户读取。项目的 Nginx 模板没有代理 `/metrics`，部署时可保持指标仅在监控内网可达。

Compose 配置使用 Prometheus `v3.13.4`，UI 仅映射到本机 9090，默认抓取 `host.docker.internal:8000`。该路径已在本机 Docker Desktop 验证。Linux Docker 的 host gateway 通常不能访问宿主机只绑定 `127.0.0.1` 的服务；应改为内网可达的目标地址、适当的容器网络或本机 Prometheus，并保留访问控制。

当前应用指标注册表在进程内，**使用 `--workers 1`**。多个 worker 共享一个 HTTP 端口时，单次抓取只能取到被选中的 worker，不能得到完整聚合数据；需要扩展为[官方多进程模式](https://prometheus.github.io/client_python/multiprocess/)或按 worker 独立端口采集后，再增加 worker。应用重启会重置计数器，Prometheus 的 `rate` 会处理重置。

数据保存在 Docker 命名卷 `prometheus-data`，保留时间 7 天、容量上限 512 MB。停止服务使用 `docker compose -f deploy/monitoring/compose.yml down`，保留数据卷；需要查看日志时用同一 Compose 文件执行 `logs --tail=50 prometheus`。

`rules.yml` 包含三个记录规则（每接口 QPS、失败率、P99）和三个示例告警：目标不可达 1 分钟、HTTP 5xx 超过 5%、非 Agent API P99 超过 1 秒。后两项要求 5 分钟内至少 100 次请求并持续 5 分钟，以减少小样本误报。阈值需要按实际目标调整；未配置 Alertmanager，告警目前只能在 Prometheus 页面查看。

## 验证

监控测试覆盖路由标签、HTTP 200 业务错误、两种 Agent 调用方式的降级、完整 SSE 时长、取消、并发上下文隔离和 token：

```bash
cd backend
.venv/bin/python -m pytest tests/test_metrics.py -q
```

项目根目录校验 Prometheus 配置及规则：

```bash
docker compose -f deploy/monitoring/compose.yml run --rm --no-deps --entrypoint promtool prometheus check config /etc/prometheus/prometheus.yml
```

真实抓取验证（需 Docker）：

```bash
backend/.venv/bin/python backend/scripts/check_metrics.py
```

脚本创建临时 SQLite 后端和独立 Prometheus 容器，检查 `up=1`、请求计数、401、HTTP 200 业务错误、QPS、P99，以及成功接口零失败率，结束后自动清理；不连接业务数据库或调用模型。Locust 的历史对比结果见[压测报告](load-testing-comparison.md)，其采样早于本次埋点接入。

加入 Grafana 完整链路验证：

```bash
backend/.venv/bin/python backend/scripts/check_metrics.py --grafana
```

该模式额外启动临时 Grafana 和独立网络，检查真实数据源健康、文件大盘导入和全部 12 条图表查询返回数值；结束后清理容器、临时数据库和网络。它使用独立的 Prometheus，不会往正在运行的业务监控中加入测试流量。本机已通过此验证，并通过浏览器检查布局和实例筛选；业务后端未运行时，大盘显示 `DOWN` 和无请求数据。
