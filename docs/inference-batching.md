# Embedding 与 Reranker 批处理

在线 `search()` 请求使用共享模型工作线程合批：Embedding 默认每批最多 32 条文本，低流量时最多等待 3 ms；Reranker 默认每批最多 8 个「查询、候选片段」对。达到批次上限立即执行，不需要等待队列填满。入库文本也走同一 Embedding 批处理接口，Chroma 写入另按每批 64 个切片控制。

原来入库已有 SentenceTransformer 默认 batch，但在线查询每次只有 1 条，且尚未接入 Reranker。本次增加跨请求合批并接入精排。吞吐对比使用同一个模型的 batch=1 和 batch=8；新增精排本身需要额外推理，不能将其与原来的纯向量检索直接比较为性能收益。

## 检索调用

`kb_service.search(query)` 保持原有字符串返回值，Agent 工具无需改动。新增的批量入口保留输入顺序，空查询对应空字符串：

```python
from app.common.async_io import run_sync
from app.services import kb_service

contexts = await run_sync(kb_service.search_many, [
    "危险化学品如何保管？",
    "如何预约实验室？",
])
```

每个查询最多召回 8 个候选；精排合并这些查询的候选对一起推理，再按原查询切回各自的分数、稳定排序并返回前 3 个片段。同分候选保持原向量顺序，结果不会跨查询混用。`search()`/`search_many()` 是供工作线程调用的同步接口，异步代码使用上面的 `run_sync()`，避免阻塞事件循环。

Embedding 保持原来的模型、float32、归一化及 Chroma 的 `sentence_transformer` 配置标识。只调整 batch 参数无需重建已有向量库；更换模型、归一化或向量度量仍需重建。分批入库会写入 `chunk_count`，下次初始化发现切片不完整会重新同步，避免将中断写入当成完整索引。

## 配置

更新代码后重启后端生效，不需要新增 Python 模型库。默认开启 Embedding 合批和 Reranker；首次使用 Reranker 会加载模型，没有缓存时可能下载权重。

| `backend/.env` 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `LAB_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 模型名或本地完整目录 |
| `KB_INFERENCE_DEVICE` | `cpu` | 两个模型的推理设备，可按硬件配置 |
| `KB_EMBEDDING_BATCH_SIZE` | `32` | 每次 Embedding 推理上限，1–256 |
| `KB_RERANKER_ENABLED` | `true` | 开启候选精排；关闭时保留向量顺序 |
| `KB_RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3` | CrossEncoder 模型名或本地目录 |
| `KB_RERANKER_BATCH_SIZE` | `8` | 每次精排候选对上限，1–64 |
| `KB_RERANKER_MAX_LENGTH` | `512` | 查询与候选的总 token 上限，超长会截断 |
| `KB_BATCH_WAIT_MS` | `3` | 每次收集批次的等待窗口，0–100 ms |
| `KB_BATCH_MAX_PENDING` | `512` | 每个模型队列最多等待的条目数，256–16384；不含正在推理的批次 |
| `KB_INDEX_BATCH_SIZE` | `64` | 每次 Chroma 写入切片数，1–1024 |

`bge-reranker-v2-m3` 本机权重约 2.1 GiB，运行还需额外内存。仓库的 2 GB systemd 部署模板已设置 `KB_RERANKER_ENABLED=false` 和 Embedding batch=8。足够内存的服务可开启精排；较大 batch 通常需要更多内存，应按实际模型和文本长度测量。底层 `encode()` 和 `predict()` 均显式传入 batch 大小，相关接口见 [SentenceTransformer 文档](https://www.sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html)和 [CrossEncoder 文档](https://www.sbert.net/docs/package_reference/cross_encoder/model.html)。

每进程每种模型只有一个推理工作线程，避免并发调用同一个模型。队列满时 Embedding 抛出 `BatchQueueFull`，现有 Agent 工具会返回检索失败；Reranker 失败或排队满则保留向量召回结果，并记录警告。模型异常会传给本批等待方，不会停止工作线程；精排模型失败后 5 秒内绕过精排，避免反复加载失败模型。应用退出先等待预热/请求工作完成，再拒绝排队任务、关闭模型工作线程。

队列和模型按进程独立，多个 Uvicorn worker 不会互相合批，而且每个进程都会加载模型。CPU 线程数量仍由 PyTorch/`OMP_NUM_THREADS` 等设置决定，模型工作线程数不等于原生计算线程数。

## 回归与本机基准

离线单元回归不加载真实权重：

```bash
cd backend
.venv/bin/python -m pytest tests/test_inference_batching.py tests/test_kb_index.py tests/test_async_io.py
```

已经缓存两个模型时，使用临时知识库检查原有 Chroma 配置、新批处理适配器和真实精排；不读写业务索引，也不下载模型：

```bash
RUN_INFERENCE_MODELS=1 .venv/bin/python -m pytest tests/test_inference_models.py -q
```

吞吐基准不调用 LLM、数据库或业务向量库，默认只使用本机缓存模型。每组测量前预热，不计加载时间；两组模型和输入完全相同，8 个并发调用分别比较 batch=1 与 batch=8。`--allow-download` 可以显式允许下载，`--device` 与模型参数可覆盖默认设置。

```bash
.venv/bin/python scripts/benchmark_inference.py --kind embedding --items 256 --repeat 3 --workers 8 --batch-size 8 --output profiles/embedding-batching.json
.venv/bin/python scripts/benchmark_inference.py --kind reranker --items 64 --repeat 3 --workers 8 --batch-size 8 --output profiles/reranker-batching.json
```

2026-10-18 本机结果：macOS arm64、Python 3.13.9、Sentence Transformers 6.0.1、PyTorch 2.14.0、CPU 4 个原生计算线程、合批窗口 3 ms。Embedding 为 4 类短查询，共测量 768 条；精排为查询与 66–71 字中文制度段落的组合，共测量 192 对，最大长度设为 512。这里按模型推理阶段的文本条数/候选对数计吞吐。

| 阶段 | batch=1 | batch=8 | 吞吐倍数 | 推理调用次数 |
| --- | ---: | ---: | ---: | ---: |
| Embedding | 346.5 条/秒 | 1366.0 条/秒 | 3.94× | 768 → 96 |
| Reranker | 15.53 对/秒 | 29.13 对/秒 | 1.88× | 192 → 24 |

所有测量项成功，批量和逐条输出均满足 `numpy.allclose(rtol=1e-4, atol=1e-5)`。Embedding 的 P50/P99 约为 22.44/35.58 ms → 5.80/6.94 ms；Reranker 为 508.47/584.67 ms → 280.59/324.52 ms。这里的延迟从工作线程进入推理接口开始计算，包含合批和模型队列等待，不包含 ThreadPoolExecutor 尚未分配线程的时间。P99 样本有限，仅用于本次局部对照。

JSON 保存环境、配置、吞吐、延迟、实际批次数和输出一致性，`profiles/` 不入 Git。测试只证明该固定负载的局部收益；长文档、更高并发、GPU 或其他 batch 大小需要重新测量。完整 Agent 的 QPS/P99 仍应使用 [Locust 压测](load-testing.md)，保持模型、缓存、并发、硬件和精排开关一致。
