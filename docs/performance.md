# Agent 性能分析

调用树、模型/工具耗时、缓存命中与重试分析可启用 [LangSmith 追踪](langsmith.md)，并与本文的客户端计时和采样分析交叉核对。

并发压测使用 Locust，见 [压测指南](load-testing.md) 和 [本地 API 基线报告](load-testing-baseline.md)。本页的 cProfile / py-spy 用于解释具体函数或阶段的耗时。

当前系统的 QPS、P95/P99 与历史数据对比见[最新压测报告](load-testing-comparison.md)。

重复 LLM 请求可启用 [Redis 响应缓存](llm-cache.md)；比较优化效果时分别记录冷缓存、命中后的耗时及实际模型调用次数。

模型和缓存已使用原生异步调用，同步 ORM/业务工具在线程池执行，详见 [Agent 异步 I/O](async-io.md)。

Embedding 跨请求合批与批量 Reranker 的配置、独立吞吐基准见 [推理批处理](inference-batching.md)。

所有命令在 `backend/` 下执行，使用已有 `.env` 配置。分析工具独立于运行和测试依赖，默认不开启，也不改变请求链路。

```bash
cd backend
source .venv/bin/activate
python -m pip install -r requirements-profile.txt
python scripts/profile_agent.py --help
```

## py-spy：采样真实 Agent 服务

### macOS

macOS 的 py-spy 需要管理员权限。先在普通用户终端启动服务（已有服务可跳过）：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --loop asyncio
```

在另一个终端进入 `backend/`，查出监听进程 PID：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

把下列 `12345` 换成上一步 Python 进程的 PID，采样期间从前端发送请求：

```bash
sudo "$PWD/.venv/bin/py-spy" record --pid 12345 --duration 60 --output /tmp/lab-agent-profile.svg
```

报告位于 `/tmp/lab-agent-profile.svg`。只对采样器使用 sudo，业务服务仍用普通用户启动。

### 其他平台

```bash
# 启动单 worker 服务，采样 60 秒；在另一个终端或前端发送请求。
python scripts/profile_agent.py spy --duration 60

# 或附加到已运行的 Python worker，替换 12345 为实际 PID。
python scripts/profile_agent.py spy --pid 12345 --duration 60
```

默认输出 `profiles/agent.svg`，用浏览器打开即可查看。端口占用时加 `--port 8001`，并将客户端指向该端口。不要使用 reload 进程的 PID。
首次采样包含启动开销；分析稳定态时先启动并预热服务，再用 `--pid` 附加。
启动模式下 py-spy 负责管理子进程，采样结束后确认服务是否仍运行，再开始下一轮。

`--idle` 包含空闲线程，辅助观察等待；支持的平台可用 `--native` 查看 embedding 的原生计算栈。macOS 或 Linux 的进程采样权限可能要求管理员配置；遇到权限错误按 py-spy 提示处理，不要改用 root 启动业务服务。

火焰图宽度代表采样占比。异步协程等待模型时，线程栈不一定保留该协程的完整调用链；不能从事件循环空闲占比推导单个模型请求耗时。同步 LangGraph 节点和预热线程也需要观察，因此真实服务优先使用 py-spy。

## cProfile：分析 RAG 局部函数

```bash
# 稳定态：先成功检索一次，再分析 20 次检索。
python scripts/profile_agent.py kb --repeat 20

# 冷启动计时：未插桩的新进程测首次检索；函数图仍单独预热后采集。
python scripts/profile_agent.py kb --cold --repeat 1 --output profiles/kb-cold.prof

python scripts/profile_agent.py report profiles/kb.prof --limit 25
snakeviz profiles/kb.prof
```

每次运行分为两个独立子进程：`baseline` 不开启 profiler，逐次使用 `perf_counter()` 测量实际耗时；`profile` 开启 cProfile，用于看函数热点。两阶段运行相同次数的查询，因此默认会执行 40 次测量检索，以及各进程自己的预热。输出包括 `.prof` 和同名 `.json`；JSON 保存每次耗时、均值、P50、P95、Python 版本与调用图校验结果。`report` 会先显示这些指标，再显示函数表。样本很少时 P95 仅供参考，不能作为稳定的尾延迟估计。

`--cold` 默认只测 1 次，baseline 包含应用依赖导入、模型加载、集合初始化和首次检索；profile 阶段仍在另一个进程预热后运行，JSON 分别标注 `baseline_mode` / `profile_mode`，不能用热调用图解释全部冷启动耗时。冷启动指新 Python 进程，磁盘与模型下载缓存仍可能存在。

可用 `--query '实验室开放时间'` 固定检索文本。这里运行真实 Chroma 与 embedding，可能下载模型并同步本地向量索引；无需调用远程 LLM。使用测试环境的知识库和配置。空知识库或检索异常会让命令失败，避免把没有执行 embedding 的快速空返回误判为优化效果。

本项目实测 CPython 3.13.9 / Torch 的 `inference_mode` 会在开启 cProfile C 内建函数事件时丢失调用入口；冷依赖初始化也会产生不完整的事件。因此稳定态默认使用 `cProfile.Profile(builtins=False)`，并验证 `run_kb_queries` 根节点、`search` 次数和父子调用记录。原生计算时间仍计入所属 Python 函数，但不会单独显示 `torch._C.*`；需要原生栈时用 py-spy。校验失败会非零退出，诊断数据保存为 `.failed.prof`，不会冒充完整报告。

Hugging Face 的 `unauthenticated requests` 是匿名下载提示，不代表检索失败；首次加载请等待命令完成。成功时会打印 `Profile: ...`。只有命令成功退出后再查看报告，异常退出也可能留下不完整的性能数据。

zsh 粘贴包含 `#` 的命令块时，若提示 `command not found: #`，可先执行 `setopt interactivecomments`，或只复制命令行。

`cumtime` 看函数及下游耗时，`tottime` 看函数自身耗时，`ncalls` 看重复调用。cProfile 增加运行开销，且默认只覆盖当前线程；不能通过包装整个 Uvicorn 得到所有线程池节点的数据。局部分析应在实际执行函数的线程内启用 profiler。

接入批处理后，Embedding 和 Reranker 在独立模型线程中执行，`kb` 的 cProfile 会主要记录调用线程的调度、检索与等待，无法展开模型线程内的计算热点。用上面的 py-spy 查看模型线程，用 `scripts/benchmark_inference.py` 对比批处理吞吐；当前完整检索还包含新启用的精排，不能直接与旧的纯向量检索耗时比较。

## 完整 Agent 请求：客户端实际耗时与 SSE 节点

先按普通用户方式启动已有后端，在另一个终端激活虚拟环境，使用测试账号运行：

```bash
python scripts/profile_agent.py chat --username your_test_user --repeat 3
```

脚本隐藏输入登录密码，默认询问“实验室危险化学品如何管理？”。也可以不传 `--username`，按提示输入已有 Bearer token；自动化可通过环境变量 `LAB_AGENT_PROFILE_TOKEN` 提供 token。凭证不会写入报告。

```bash
python scripts/profile_agent.py chat --username your_test_user --query '有哪些开放的实验室？' --repeat 3 --output profiles/chat-labs.json
```

`--base-url` 默认 `http://127.0.0.1:8000`，传服务器根地址（不带 `/api`）；`--timeout` 默认 180 秒，为连接/读取的 socket 超时。请求依次执行，每次使用新会话，不进行自动重试或隐藏的预热请求，避免写操作被重复提交。`--repeat` 会真实重复执行指定请求，写操作只在专用测试数据上测量。运行会消耗真实模型额度，并正常生成聊天与审计记录。

`profiles/chat.json` 默认只保存时序、会话编号和节点/工具名称与状态，不保存问题、答案、工具参数或凭证。关键字段：

| 字段 | 测量范围 |
| --- | --- |
| `first_event_ms` | 发起请求到首个 SSE 事件；通常是会话或状态事件 |
| `first_answer_ms` | 首段未被 `reset` 撤销的非空答案内容到达时间 |
| `done_ms` | 收到 `done` 事件的时间 |
| `complete_ms` | 完整响应读取结束；包含 `done` 后服务端保存聊天等开销 |
| `nodes` / `tools` | 服务端上报的执行时长和客户端收到事件的时间 |

当前 `node_respond` 一次发出完整答案，所以 `first_answer_ms` 不等于模型供应商的首 token 延迟。节点耗时包含节点内部等待、重试等工作，不能直接视为纯模型推理时间；模型尝试次数还需结合服务端日志。工具耗时属于执行节点内部，不能与节点耗时重复相加。

HTTP 错误、SSE `error`、缺失 `done` 或帧截断会标记失败并返回非零退出码，已有样本仍会保存。`completed` 只表示响应协议完整，不保证答案正确或业务任务成功；同时检查节点/工具失败状态。汇总 P50/P95 只包含完整响应，失败数单独报告。串行测试不代表并发性能。

## 固定工作负载与结果解释

分别测试开放实验室查询、制度 RAG、多步查询；预约和取消使用测试账号与数据库。每个样本用新会话，预热后重复请求，记录端到端耗时、首个答案 token 延迟、节点与工具耗时、LLM 调用与重试次数。并发逐步从 1 增加到 5、10，比较 P50/P95、CPU 和错误率。

结合现有 SSE `node` / `step` 的 `duration_ms` 和数据库轨迹判断瓶颈。`trace_node` 在写入轨迹之前结束计时，因此其自身的 `commit()` 不包含在节点耗时内；工具审计、会话保存、调度和传输也可能造成总耗时与节点耗时之和的差值。不要把嵌套节点和工具耗时重复相加。

优先验证：模型轮数和重试、轨迹/工具审计的频繁提交、RAG 首次加载、并发排队。CPU 低而总耗时高时，继续对模型和数据库等待做阶段计时。

`profiles/` 已被 Git 忽略。报告可能包含代码路径和调用信息，分享前检查内容。现有 CI 保持功能测试；不要把带 coverage 的测试耗时当性能基准，离线 mock 测试也不能代表真实 Agent 延迟。
