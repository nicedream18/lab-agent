# LangSmith 调用链追踪与分析

Agent 的流式与非流式入口均已集成 LangSmith。每轮问答以 `lab-agent` 为根 run，LangGraph 节点和 ChatOpenAI 调用自动记录；直接调用 `.func()` 的业务工具补充独立 tool span。原有数据库节点轨迹、工具审计和 SSE 事件继续保留。

## 启用

安装更新后的依赖：

```bash
backend/.venv/bin/python -m pip install -r backend/requirements.txt
```

在 `backend/.env` 中添加以下配置并重启后端。API Key 在 LangSmith 的设置中创建，不要提交到 Git。

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=replace-with-your-langsmith-api-key
LANGSMITH_PROJECT=lab-agent
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_HIDE_INPUTS=true
LANGSMITH_HIDE_OUTPUTS=true
LANGSMITH_SAMPLING_RATE=1.0
```

默认关闭追踪；未填写 Key 时记录提示并关闭追踪。`backend/.env` 通过 Pydantic 加载，追踪上下文和 Client 使用显式配置，无需额外 `export`。多 workspace Key 可设置 `LANGSMITH_WORKSPACE_ID`；EU 或自托管部署应将 endpoint 改成实际区域地址。配置方式参考 [LangSmith 官方文档](https://docs.langchain.com/langsmith/trace-without-env-vars)。

默认隐藏所有 run 的输入输出正文，仍可分析节点耗时、父子关系、模型尝试次数、缓存命中及错误状态。Client 同时使用 SDK 的 secret anonymizer 处理可识别的凭据，包括异常字符串。需要检查问答和检索内容时，可在测试环境将两个 `HIDE_*` 选项改为 `false`；这会上传问题、历史消息、模型回答和工具结果。隐藏输入输出不等于全面脱敏，异常文本和显式 metadata 仍需按实际数据审查，详见[官方隐私配置](https://docs.langchain.com/langsmith/mask-inputs-outputs)。

`LANGSMITH_SAMPLING_RATE` 取值为 0–1，每个请求只做一次采样，子调用沿用该结果。例如 0.1 表示约追踪 10% 的请求。请求未被采样或追踪关闭时，工作流上下文显式禁用追踪，也覆盖旧的 `LANGCHAIN_TRACING_V2` 开关。

## 产生和定位调用链

启动应用后，在现有聊天页面发起只读查询即可，例如“有哪些实验室开放？”或“实验室能带食物吗？”。调用 `/api/ai/chat` 和 `/api/ai/chat/stream` 都会采集。

服务日志会打印：

```text
LangSmith trace_id=<本轮UUID> conversation_id=<会话ID> mode=stream
```

进入 LangSmith 的 `lab-agent` 项目查看 `lab-agent` 根 run。`thread_id` 和 `conversation_id` metadata 均对应现有会话 ID，多轮对话共用会话关联，每轮拥有独立 trace ID；`request_mode` 区分 stream/invoke。根 run 同时记录模型名、缓存开关和精排开关，便于选择同口径样本。并发请求的上下文相互隔离。

典型 RAG 问答的调用结构如下；规划路径还会出现 `plan`、`advance`、`replan` 节点。

```text
lab-agent
├─ analyze
│  └─ llm.structured
│     └─ ChatOpenAI（每次真实尝试各一条）
├─ agent
│  └─ llm.agent
│     └─ ChatOpenAI
├─ execute
│  └─ search_lab_docs [tool]
│     └─ kb.search [retriever]
│        ├─ kb.recall [retriever]
│        │  └─ kb.embedding [embedding]
│        └─ kb.rerank
├─ agent
│  └─ llm.agent → ChatOpenAI
└─ respond
```

这是应用结构说明，具体节点次数由该轮规划、工具调用和缓存命中决定，LangGraph 内部还可能显示状态写入等辅助 run。LangGraph 自动追踪机制见[官方接入文档](https://docs.langchain.com/langsmith/trace-with-langgraph)。

## 分析耗时和失败

先看根 run 总耗时，再展开慢节点。不要把父节点和子节点耗时相加；它们存在包含关系。

| 观察位置 | 重点指标 | 如何解释 |
| --- | --- | --- |
| `analyze` / `plan` / `replan` → `llm.structured` | `cache_hit`、`attempts`、模型子 run | 判断解析或规划是否命中缓存，以及实际请求了几次模型 |
| `agent` → `llm.agent` | 模型调用次数、token 用量、工具轮数 | 多轮工具决策会增加模型往返；缓存命中时 `attempts=0`，没有新的模型子 run |
| 两个 `llm.*` helper | `attempts`、`retry_delay_seconds` | `attempts-1` 为重试次数；helper 耗时包含缓存访问和退避，模型子 run 只覆盖对应尝试 |
| `execute` → 工具名 | tool 耗时、`ok`、error | 工具返回 `ok=false` 会标记 `tool_returned_failure`；后续 Agent 仍可选择其他工具 |
| `kb.recall` → `kb.embedding` | 召回总耗时、Embedding 耗时 | recall 包含 Embedding；Embedding 包含合批和排队等待，不能当作纯模型计算时间 |
| `kb.rerank` | 候选对数量、耗时、error | 包含精排模型首次加载、合批等待和计算；失败时召回结果仍可返回，因此子 run 失败不一定表示整轮失败 |

`retry_delay_seconds` 保存最后一次尝试前的等待时间，不是累计退避时间。模型 token 指标由供应商/SDK 返回，缓存命中不会产生新的 token 用量。工具授权拦截和幂等跳过发生在实际执行之前，不会产生执行工具 span，可结合已有 SSE step 和数据库审计查看。

共享模型队列可能同时合并多个请求，追踪记录各请求调用方的等待区间，不为同一批计算绑定某个请求，也不展开模型工作线程的计算热点。纯计算分析仍使用 [py-spy 与推理基准](performance.md)。

根 run 覆盖 LangGraph 执行，包含图内模型、工具和节点审计；不包含入口鉴权、Runner 初始化/记忆读取、最终聊天记录落库和客户端 SSE 读取时间。端到端延迟仍使用 `scripts/measure_agent.py` 或 Locust。当前回答一次发送完整文本，不能将其解释为供应商首 token 延迟。

## 单条 trace 汇总

从服务日志取得 trace UUID，等待后台上传完成后，在 `backend/` 执行：

```bash
.venv/bin/python -m scripts.analyze_langsmith --trace-id YOUR_TRACE_UUID
```

脚本只读取指定项目中的该条 trace，输出 JSON：根 run 延迟、各 run 的父 ID、总耗时、自身耗时、模型调用数、缓存命中数、重试次数、失败 span 数和未结束 span 数。不导出输入输出、原始错误正文或会话消息。

`self_ms` 用父区间减去直接子区间的并集，子调用重叠时不会重复扣除。它表示未被子 span 覆盖的墙钟时间，可能包含线程调度和 I/O 等待，并非 CPU 时间。未结束的 run 或带有未结束直接子 run 的自身耗时返回 `null`。若 `orphan_count`、`unfinished_count` 非零或根 run 尚未出现，先等待上传完成再分析。单条 trace 不能推导系统 QPS、P95/P99 或优化收益。

## 验证与关闭

```bash
cd backend
.venv/bin/python -m pytest tests/test_langsmith.py -q
```

回归真实运行 LangGraph、脚本化模型和 LangSmith SDK，仅拦截 SDK 传输层；覆盖流式/非流式调用树、正文隐藏、工具失败、精排降级、跨线程父子关联、缓存/重试 metadata、并发上下文隔离、采样关闭和流关闭。测试不上传数据、不调用外部模型，也不加载真实推理权重。

关闭时设置 `LANGSMITH_TRACING=false` 并重启即可。SDK 在后台批量发送追踪，应用退出通过工作线程调用 Client.close，最多等待 5 秒收尾。网络或凭据异常时先查看 SDK 上传日志；本地数据库轨迹与工具审计继续作为排障依据。

当前仓库环境尚未配置 LangSmith Key，集成通过离线链路验证；云端真实模型调用耗时与瓶颈结论需要启用后采集。
