# 当前系统 Locust 压测对比（2026-10-19 整理）

本次复用历史 API 场景，5、20 用户各运行三轮；另外为真实 Embedding + Chroma + Reranker 增加隔离 HTTP 压测。远程 LLM、业务 MySQL 和业务 Redis 未参与，所有数据库和向量索引均使用临时目录。

原始采样日志跨 10 月 18 日晚间至 19 日凌晨，采样时尚未接入 Prometheus 请求埋点，因此本报告不包含新增埋点的开销。

## API 与历史记录对比

环境与[历史基线](load-testing-baseline.md)一致：本机 macOS arm64、Python 3.13.9、Locust 2.46.7、单 Uvicorn worker、临时 SQLite，20 个实验室、100 个设备、共享测试账号。请求选择为实验室列表 50%、设备列表 25%、用户信息 25%；think-time=0，配置运行 20 秒，5/20 用户分别以 1/4 用户每秒增长。关闭后台维护任务、知识库预热和 LLM 缓存。

QPS 是三个持续 GET 接口的最终 RPS 之和，排除启动登录请求。历史数据从原始 HTML 的最终统计恢复；本轮使用请求结束后的 `final-stats.csv`。下表“当前”是三轮指标的中位数，区间是三轮最小/最大值；百分位保留在各自接口内，不对不同接口的 P99 取平均。

| 用户数 | 历史 QPS | 当前 QPS 中位数 | 当前 QPS 范围 | QPS 变化 | 当前请求总数（三轮） | 失败率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 1235.77 | 1268.55 | 1257.75–1277.51 | +2.65% | 75,411 | 0% |
| 20 | 857.28 | 827.25 | 816.37–835.56 | −3.50% | 49,157 | 0% |

| 用户数 | 接口 | P50：历史 → 当前 | P95：历史 → 当前 | P99：历史 → 当前 |
| --- | --- | ---: | ---: | ---: |
| 5 | 实验室列表 | 4 → 4 ms | 5 → 5 ms | 6 → 6 ms |
| 5 | 设备列表 | 4 → 4 ms | 6 → 5 ms | 6 → 7 ms |
| 5 | 用户信息 | 3 → 3 ms | 4 → 4 ms | 5 → 5 ms |
| 20 | 实验室列表 | 25 → 26 ms | 35 → 36 ms | 40 → 42 ms |
| 20 | 设备列表 | 28 → 29 ms | 38 → 39 ms | 44 → 45 ms |
| 20 | 用户信息 | 11 → 11 ms | 20 → 19 ms | 25 → 26 ms |

这两档没有显示明显的 API 容量提升，20 用户下吞吐仍低于 5 用户，尾延迟也更高。这里的 API 没有执行 LLM 或检索，无法检验 Redis 响应缓存、模型异步调用与推理合批的效果。

历史每档仅一次短测，本轮每档三次；负载机和服务端同机，未控制操作系统后台负载。两个时期都配置 20 秒，但历史 5 用户有效统计窗口约 17.67 秒，本轮约 19.82 秒，升压过程所占比例不同，因此 +2.65% 不能直接视为优化收益。有效窗口由最终 `请求数 / QPS` 核对，20 用户两期均约 19.82 秒。

原始报告：`backend/profiles/locust/local-api-{5,20}/`；本轮为 `backend/profiles/locust/current-api-{5,20}-r{1,2,3}/`。各目录保留 `run.json`、最终 CSV、HTML、错误统计和服务日志，历史目录未被覆盖。

## 复现 API

项目根目录执行，每档重复三次并换用不同输出目录：

```bash
backend/.venv/bin/python backend/scripts/load_test.py --local --users 5 --spawn-rate 1 --seconds 20 --think-time 0 --output-dir backend/profiles/locust/recheck-api-5-r1
backend/.venv/bin/python backend/scripts/load_test.py --local --users 20 --spawn-rate 4 --seconds 20 --think-time 0 --output-dir backend/profiles/locust/recheck-api-20-r1
```

汇总脚本 `backend/scripts/summarize_load_tests.py` 比较相同负载配置，优先使用最终 CSV，旧目录缺失该文件时恢复 HTML 最终值。每轮输出请求数、QPS、统计窗口，以及各接口的平均耗时、P50、P95、P99；多轮输出中位数与范围。Locust 百分位是近似统计，快照与收尾语义见[官方配置文档](https://docs.locust.io/en/2.46.7/configuration.html)。

## 真实 RAG HTTP 对照

历史记录没有 RAG HTTP 压测，因此这组是本轮新增的 batch=1 控制对照，不代表历史 Agent 端到端基线。两组均使用当前检索代码、同一个 BGE Embedding 和 `bge-reranker-v2-m3`，读取同样 5 份文档、38 个切片，构建临时 Chroma 索引；CPU 原生线程数 4。启动前预热，全部模型已缓存且强制离线，索引、模型加载不计入发压窗口。

8 个虚拟用户、每秒生成 8 用户、think-time=0，配置发压 60 秒，等待在途请求结束后导出最终统计；两组各两轮。随机等概率选择危险化学品、火灾处理、安全培训三个问题，每次召回 8 个候选并精排前 3 个。每个用户先登录，表内 QPS 排除登录。临时端点为 `POST /__loadtest/rag`，只存在于 `loadtests.local_api`，不会注册到正常生产入口。

| 指标（各轮统计的中位数） | Embedding/Reranker batch=1/1 | 当前 batch=32/8 |
| --- | ---: | ---: |
| QPS | 1.504 | 1.674 |
| QPS 范围 | 1.480–1.527 | 1.628–1.721 |
| 平均延迟 | 5.278 s | 4.607 s |
| P50 | 5.25 s | 4.70 s |
| P95 | 5.85 s | 5.25 s |
| P99 | 5.90 s | 5.45 s |
| 完成请求数（两轮总计） | 192 | 216 |
| 失败率 | 0% | 0% |

本组 QPS 提升约 **11.32%**，P99 中位数下降约 **7.63%**。这里的 P99 是“两轮 P99 的中位数”，不是合并样本后的 P99。各轮仅约百个请求，持续时间短、同机发压、三个问题随机混合，不能作为生产容量或稳定 P99 承诺。

真实 HTTP 收益小于[短文本模型基准](inference-batching.md)中的 3.94×/1.88×。真实检索包含认证、查询向量化、候选召回、不同长度候选的重排和并发排队。第一轮当前配置中，106 个查询只合成了 101 个在线 Embedding 批次，说明真实到达节奏下 Embedding 合批率很低；Reranker 则以 106 批处理 848 个候选对。不能把离线批次吞吐倍率直接代入整体系统 QPS。

验证同时检查了非空检索结果和精排模型的实际执行状态。模型不可用、精排回退或推理错误会使测试失败，避免将降级返回算成成功。`run.json` 保存前后模型工作线程计数、模型名、CPU 线程数、文档哈希和切片数；没有远程 LLM 调用，也未评估最终自然语言回答质量。

复现命令（每组至少两次，使用不同输出目录）：

```bash
backend/.venv/bin/python backend/scripts/load_test.py --local --scenario rag --users 8 --spawn-rate 8 --seconds 60 --think-time 0 --embedding-batch-size 1 --reranker-batch-size 1 --output-dir backend/profiles/locust/recheck-rag-single-r1
backend/.venv/bin/python backend/scripts/load_test.py --local --scenario rag --users 8 --spawn-rate 8 --seconds 60 --think-time 0 --output-dir backend/profiles/locust/recheck-rag-batched-r1
```

本轮 RAG 原始目录为 `backend/profiles/locust/current-rag-{single,batched}-r{1,2}/`。API 与 RAG 的机器可读汇总为 `backend/profiles/locust/current-comparison.json`，均被 Git 忽略；本页保留关键测量值与口径。
