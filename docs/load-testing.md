# Locust 压测

选择 Locust，是因为可以直接用 Python 复用本项目的 SSE 帧解析、校验业务响应，并管理登录状态。Locust 的 `HttpUser` 支持会话及 `catch_response` 业务校验，见 [官方说明](https://docs.locust.io/en/stable/writing-a-locustfile.html)。压测工具安装在独立虚拟环境，避免 gevent 的全局补丁进入业务服务和 pytest 进程。

## 安装与快速运行

在项目根目录安装一次：

```bash
python3 -m venv backend/.venv-loadtest
backend/.venv-loadtest/bin/python -m pip install -r backend/requirements-loadtest.txt
```

进入 `backend/`，使用业务环境的 Python 启动压测入口；它会自动调用独立环境里的 Locust：

```bash
cd backend
.venv/bin/python scripts/load_test.py --local --users 5 --spawn-rate 1 --seconds 30 --think-time 0
```

`--local` 自动创建临时 SQLite 数据库、随机密码、20 个实验室和 100 个设备，启动真实 FastAPI 应用和登录/查询接口，测试结束后停止服务并清理临时数据库。不读取真实数据库配置，不调用 LLM。该模式仅支持 `api` 场景，使用单 Uvicorn worker；暂停后台维护任务和知识库预热。结果是本机 API 基线，不能推算 MySQL、生产环境或真实 Agent 容量。

已完成的两组本地结果见 [基线报告](load-testing-baseline.md)。

## 场景与流量

| 场景 | 任务选择 |
| --- | --- |
| `api`（默认） | 实验室列表 50%、设备列表 25%、当前用户 25% |
| `agent` | 开放实验室查询、制度 RAG、按设备找实验室，各约三分之一 |
| `mixed` | 每次任务约 80% 选择 API、20% 选择 Agent |

比例是随机选择任务的概率，长耗时任务会影响最终请求数比例。每个虚拟用户先登录一次并复用 token；Agent 每次使用新会话，不执行预约、取消或修改资料。Agent 仍会正常生成聊天和审计记录，并消耗模型额度。

这是闭环负载：每个虚拟用户完成一次任务，等待 `--think-time` 秒后再发下一次请求。`--users` 表示虚拟用户数，不是目标 RPS；默认等待 0.5 秒，观察最大查询吞吐时可以设为 0。通过 `--spawn-rate` 控制每秒新增用户数。

## 对测试服务运行

先正常启动配置好数据库、模型服务的后端。以下命令中的 `your_test_user` 替换为测试用户名，密码隐藏输入：

```bash
.venv/bin/python scripts/load_test.py --host http://127.0.0.1:8000 --username your_test_user --scenario api --users 5 --seconds 60
```

真实 Agent 先从小并发开始：

```bash
.venv/bin/python scripts/load_test.py --host http://127.0.0.1:8000 --username your_test_user --scenario agent --users 2 --seconds 60
```

需要同时观察 Locust 网页时加 `--web`，打开 `http://127.0.0.1:8089`。网页仅监听本机；在页面启动和停止测试，终端 Ctrl+C 退出。该模式的时间与人数由网页控制；`--seconds` 是无界面模式的运行时间。

`--host` 传服务根地址，不带 `/api`。`--request-timeout` 默认 180 秒，为 socket 读取超时；停止产生新任务后，最多再等待该值加 5 秒，让正在执行的请求完成。达到用户总数前就结束的短任务会被入口拒绝。

## 多测试账号

使用单账号会让所有虚拟用户共享身份。模拟多用户时，准备已存在的测试账号文件 `backend/loadtests/accounts.local.json`（已被 Git 忽略）：

```json
[
  {"username": "loadtest_01", "password": "replace-with-test-password"},
  {"username": "loadtest_02", "password": "replace-with-test-password"}
]
```

```bash
.venv/bin/python scripts/load_test.py --accounts-file loadtests/accounts.local.json --scenario mixed --users 2 --seconds 60
```

也支持文件中使用 `{"token": "..."}`。账号按虚拟用户轮流分配，账号数不足时复用。自动化可使用 `LAB_LOADTEST_TOKEN` 或 `LAB_LOADTEST_USERNAME` / `LAB_LOADTEST_PASSWORD` 环境变量。密码和 token 不放进命令行或报告。入口当前面向单 Locust 进程；没有合并分布式 worker 的自定义时序指标。

## 报告怎么看

每次运行默认保存到 `backend/profiles/locust/<时间>-<随机后缀>/`，该目录已被 Git 忽略。可通过 `--output-dir` 指定目录。

| 文件 | 用途 |
| --- | --- |
| `report.html` | Locust 图表、吞吐、失败率和 P95/P99，浏览器直接打开 |
| `final-stats.csv` | 等待请求结束后的最终快照；用于精确核对请求总数 |
| `results_stats.csv` / `results_stats_history.csv` | Locust 周期采样与历史趋势，最后一次刷新可能早于最终结束 |
| `results_failures.csv` / `results_exceptions.csv` | 失败与压测脚本异常 |
| `agent-metrics.json` | 完成/失败次数、首段答案延迟与成功 Agent 请求的节点/工具时长汇总 |
| `run.json` | 场景、并发、等待时间、目标环境和退出码，不包含凭证 |

重点比较同一接口在不同并发下的 RPS、失败数和 P95。`/api/auth/login [setup]` 是用户启动时的登录，单独查看，不要把它当作持续查询负载。`Aggregated` 含登录请求；比较 API 吞吐时对三个 GET 行的 RPS 求和。API 查询返回空列表也可以成功，实际数据量和分布应一并记录。

Agent 的 HTTP 响应时间从发送请求到读完整个 SSE 流；脚本修正了 `stream=True` 默认只计响应头的统计。HTTP 200 还必须满足：SSE 帧合法、有非空 `done.answer`、没有错误事件或失败的节点/工具。失败样本会计入 Locust 失败数；业务回答的正确性仍需评测验证。

`first_answer` 会丢弃被 `reset` 撤销的临时内容。当前后端一次发送完整答案，所以该指标不是供应商的首 token 时间。节点时间包含模型等待和重试，工具时间嵌套在节点内，不能重复相加；这些扩展时序只汇总校验成功的 Agent 响应。自定义直方图按毫秒取整，Locust 的百分位也为近似值。

无请求完成、业务/传输失败会返回非零退出码。没有预设通用延迟门槛；结合项目的目标响应时间判断结果。同机压测还应观察服务端和 Locust 进程 CPU、内存、数据库连接与模型限流；若负载机 CPU 饱和，需要分机复测。

## 与性能采样配合

先预热，再按 1、5、10、20 个虚拟用户逐档运行，保持测试时长、数据、场景和等待时间一致。真实 LLM 与纯 API 分开测量，避免模型延迟掩盖本地服务排队。

Locust 发压期间可对后端 PID 运行 py-spy，使用方法见 [性能分析指南](performance.md)。不建议在开启 cProfile 或 coverage 时采集性能基准。

压测逻辑的独立检查：

```bash
.venv/bin/python -m pytest tests/test_loadtest_checks.py
.venv-loadtest/bin/python -m pytest loadtests/test_locust_stream.py
```
