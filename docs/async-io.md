# Agent 异步 I/O

Agent 的需求解析、规划、重规划、自主决策均通过 `await llm.ainvoke(...)` 调用模型。结构化请求入口 `_ask()`、`analyze()`、`make_plan()`、`revise_plan()` 现在都是协程，调用方必须 `await`。重试退避使用 `asyncio.sleep()`，保留现有超时、限流、熔断与授权判断。

## 执行方式

| 操作 | 当前执行方式 |
| --- | --- |
| LLM API 调用 | 原生异步 `ainvoke`，等待响应期间让出事件循环 |
| Agent 的 Redis 文本/工具消息缓存 | `redis.asyncio.Redis`，异步 GET/SET；故障继续绕过缓存 |
| Agent 初始化和长期记忆读取 | 通过 `app.common.async_io.run_sync()` 放入 AnyIO 线程池 |
| 预约/取消草稿、数据库工具、知识库检索 | 整个同步操作放入线程池；同一请求内逐个等待执行，知识库模型另由共享工作线程合批 |
| 节点轨迹、工具审计、聊天记录保存 | 在线程池执行同步 SQLAlchemy 事务 |
| 过期预约扫描、历史会话清理 | 在线程池创建独立 Session，完成查询、提交和关闭 |
| 普通 CRUD、文件上传接口 | 保留同步 `def` 路由，由 FastAPI 调度到线程池 |
| embedding 模型预热 | 保留后台线程预热，避免模型加载阻塞事件循环 |

同步 SQLAlchemy/PyMySQL 与本地 embedding 仍使用现有驱动；本次没有迁移数据库驱动。线程池让这些操作不会直接阻塞事件循环，但仍受线程池、数据库连接数与计算资源限制。[FastAPI 官方说明](https://fastapi.tiangolo.com/async/)区分了同步路由的自动线程池调度和直接调用的普通工具函数；因此不能只给调用同步数据库的函数加 `async def`。

## 数据库与取消

一轮 Agent 内复用的 Session 按节点顺序使用，工具调用也保持串行。不要对共享 Session 的工具使用 `asyncio.gather()`。不同请求各用自己的 Session，可以在等待模型时并发推进。

`run_sync()` 使用 AnyIO 有并发上限的线程池。取消等待不能停止已经运行的线程，因此收到客户端取消或关闭信号时，先等待当前工作完成，再传播取消，避免请求依赖关闭 Session 后线程仍在提交。正在执行的业务写操作可能完成；取消请求不等于回滚已提交事务。长时间阻塞的 SQL 仍需数据库端超时约束。

原生异步模型/Redis 调用会传播取消。异步节点的轨迹在退出时通过线程池保存，取消会标记为失败。用户 ID/角色在初始化工作线程中取出，缓存键使用这份快照，避免数据库提交使 ORM 对象过期后在事件循环触发同步刷新。

Redis 的异步连接池按事件循环管理，单池最多 32 个连接，应用退出时 `await response_cache.aclose()`。同步缓存接口仅用于兼容同步调用方，另有连接池；`close()` 关闭同步池。短生命周期脚本若调用异步缓存，应在同一个事件循环的 `finally` 中调用 `aclose()`。[Redis 官方异步文档](https://redis.io/docs/latest/develop/clients/redis-py/async/)说明了异步连接的显式关闭要求。

## 启动与验证

无需新增配置，更新依赖后重启后端即可生效，已有缓存开关继续有效。

```bash
cd backend
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --loop asyncio
```

离线并发/取消与 Agent 行为回归：

```bash
.venv/bin/python -m pytest tests/test_async_io.py tests/test_agent_workflow.py tests/test_llm_cache.py
.venv/bin/python -m evals.run
```

已安装 `redis-server` 时，完整回归会使用临时 Unix socket Redis，不连接业务 Redis：

```bash
.venv/bin/python scripts/check_redis_cache.py --full
.venv/bin/python scripts/check_coverage.py coverage.json
```

并发测试验证三个结构化节点在模型尚未返回时都能进入异步调用，并覆盖线程执行边界、取消后工作完成顺序、后台扫描故障恢复及原生 Redis 接口。功能测试的通过与耗时不能换算为真实 Agent 的 QPS/P99。

实际收益使用[已有 Locust 接入](load-testing.md)对比：固定 worker 数、模型、问题、账户、缓存状态和数据库，分别记录并发 1/5/10/20 下的 QPS、P95/P99、错误率、完整 SSE 耗时。异步主要改善等待 I/O 时的并发能力；单次模型推理耗时、供应商限流、数据库连接池排队仍可能主导尾延迟。
