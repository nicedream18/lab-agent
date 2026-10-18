# Redis LLM 响应缓存

缓存接在 `app/agent/planner.py` 的异步结构化调用和 Agent 调用上，使用独立的 `app/agent/llm_cache.py` 管理 Redis 连接与响应序列化。默认关闭，通过后端 `.env` 启用。

## 本地启用

安装后端依赖：

```bash
cd backend
.venv/bin/python -m pip install -r requirements.txt
```

在一个终端运行 Redis（如已有 Redis，使用其连接地址即可）：

```bash
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --maxmemory 128mb --maxmemory-policy allkeys-lru
```

编辑 `backend/.env`：

```dotenv
LLM_CACHE_ENABLED=true
REDIS_URL=redis://127.0.0.1:6379/0
LLM_CACHE_TTL_SECONDS=60
LLM_CACHE_TIMEOUT=0.2
LLM_CACHE_NAMESPACE=lab-agent:llm:v1
```

重启后端。同一用户以新会话重复相同问题时，在提示词、历史、长期记忆和模型参数都相同的情况下可命中；续聊改变上下文通常产生新的键。日志中的 `LLM cache hit: structured` 和 `LLM cache hit: agent` 表示跳过了对应的模型调用。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `LLM_CACHE_ENABLED` | `false` | 是否启用缓存 |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接；配置中以 SecretStr 保存 |
| `LLM_CACHE_TTL_SECONDS` | `60` | 每次写入的 TTL，至少 1 秒；读取不续期 |
| `LLM_CACHE_TIMEOUT` | `0.2` | 连接和单条命令的 socket 超时，单位秒 |
| `LLM_CACHE_NAMESPACE` | `lab-agent:llm:v1` | 环境/版本前缀；修改后重启即可逻辑失效旧缓存 |

部署时可使用专用 Redis 实例/数据库和不同的命名空间。缓存值包含模型返回文本与工具调用参数，应按业务数据配置 Redis 的访问权限；连接可使用带认证的 `redis://` 或 TLS 的 `rediss://`。示例启动参数禁用持久化，使用 128 MB 内存上限和 LRU 淘汰策略。

## 缓存范围与正确性

- 需求解析、规划和重规划：只缓存成功返回且能解析为 JSON 对象的结构化响应；确定性授权判断仍在每次调用后执行。
- 只读 Agent 决策：缓存完整 AIMessage 的内容与工具调用字段，保留工具调用协议。每次命中生成新的消息 ID，清空原始调用的 token 用量，标记 `response_metadata.llm_cache_hit=true`。
- 本轮允许写库，或本轮消息中已经出现写库工具调用时，Agent 决策跳过缓存。返回值包含预约/取消等写工具调用也不会被缓存或从缓存回放。
- 图编排和工具照常执行。缓存命中了“查询实验室”决策后，仍执行当前查询；查询结果发生变化时，后续模型消息的缓存键也随之变化。

缓存键用 HMAC-SHA256 生成，包含当前用户 ID 和角色、数据库标识、模型供应商、模型名、API 凭证指纹、实际生成参数、完整模型消息、工具 schema 和 `tool_choice`。消息含系统提示词、日期、历史、偏好、槽位和已有工具结果；改变这些模型输入会导致未命中。SDK 消息 ID 和 token 用量等非模型输入字段被排除，工具调用关联 ID 保留。

原始问题和凭证不出现在键名中；不同用户不共享响应。工具结果没有独立的业务数据缓存，预约授权和业务校验仍走现有代码。单条响应超过 128 KiB、空响应、无效工具参数、模型异常或结构化响应解析失败时，不写缓存。

缓存读取/写入异常会跳过缓存并继续模型链路，不计为模型失败。Agent 异步连接池每个事件循环最多 32 个连接，关闭 Redis 自动重试；失败后 5 秒内暂时绕过 Redis，避免每个请求都等待超时。Agent 使用 `redis.asyncio` 原生异步读写，应用退出时显式关闭连接池；兼容同步调用方的接口另有同步池。生命周期与数据库线程池策略见 [Agent 异步 I/O](async-io.md)，Redis 客户端超时配置见 [官方文档](https://redis.io/docs/latest/develop/clients/redis-py/produsage/)。

当前使用精确输入匹配，没有语义相似缓存或分布式请求合并；冷缓存下同时到来的相同请求仍可能重复调用模型。相同工具输入出现不同随机关联 ID 也可能降低命中率。TTL 控制响应复用窗口，变更模型别名或服务端行为时可同时调整命名空间。

## 验证与回归

无需 Redis 的单元回归：

```bash
.venv/bin/python -m pytest tests/test_llm_cache.py
```

本机已安装 `redis-server` 时，自动启动仅使用临时 Unix socket 的 Redis，运行真实读写/TTL 回归，结束后清理：

```bash
.venv/bin/python scripts/check_redis_cache.py
.venv/bin/python scripts/check_redis_cache.py --full
```

也可设置 `TEST_REDIS_URL` 后运行 `tests/test_llm_cache_redis.py`。测试只创建和删除随机测试前缀的键，不调用 FLUSHDB。CI 的后端任务已增加 Redis 7.4 服务，正常 pytest 流程会执行集成测试；没有 `TEST_REDIS_URL` 时这两项集成测试跳过。

回归包括：同一输入命中、TTL 过期重新调用、用户/模型/参数隔离、坏缓存与 Redis 故障回退、写操作跳过、异步工具消息恢复，以及真实 LangGraph 在命中模型缓存后仍重新读取工具结果。

Locust 测量命中收益时，使用固定测试账号、相同问题和新的会话，分别测缓存关闭、冷缓存、已预热缓存。记录实际命中情况和供应商调用次数；纯 API 场景不调用 LLM，因此该场景的 QPS 不能验证此缓存的收益。压测入口见 [Locust 指南](load-testing.md)。
