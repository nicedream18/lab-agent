# 本地 API 压测基线（2026-10-18）

本轮使用 Locust 2.46.7，对真实 FastAPI 登录和只读查询接口发压。环境为本机 macOS、Python 3.13.9、单 Uvicorn worker、临时 SQLite；20 个实验室、100 个设备、所有虚拟用户共用一个测试账号。不包含真实 MySQL、LLM、RAG embedding；后台维护任务与知识库预热关闭。负载机和服务端在同一台电脑运行。

两次均设置运行 20 秒、用户等待 0 秒，分别按 1 和 4 用户/秒增长至目标人数。指标来自结束后的 HTML 报告，含升压过程；持续 API 查询统计排除了启动时的登录请求。

| 最大虚拟用户 | API 完成请求数 | API 吞吐（次/秒） | API 失败数 | 实验室列表 P95 | 设备列表 P95 | 用户信息 P95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 21,833 | 1,235.77 | 0 | 5 ms | 6 ms | 4 ms |
| 20 | 16,986 | 857.28 | 0 | 35 ms | 38 ms | 20 ms |

后续对比时从以上两次原始 HTML 恢复的 P99 如下；仍是同一批历史请求：

| 最大虚拟用户 | 实验室列表 P99 | 设备列表 P99 | 用户信息 P99 |
| --- | ---: | ---: | ---: |
| 5 | 6 ms | 6 ms | 5 ms |
| 20 | 40 ms | 44 ms | 25 ms |

当前系统的三轮复测及真实 RAG 对照见[压测对比报告](load-testing-comparison.md)。

20 用户组的查询吞吐比 5 用户组低约 31%，且各接口 P95 明显升高。这提示当前同机单 worker 配置在较高并发下出现竞争或排队，无法从这两次短测试单独判定 CPU、线程池或数据库连接哪个是根因。下一步应在同档并发下结合 py-spy、进程 CPU 和连接池指标验证，并重复运行以确认稳定性。

本轮没有达到已知生产容量结论。SQLite 与 MySQL 的行为不同，同账号、有限数据、短时测试以及同机负载都限制结果的外推；真实 Agent 场景还需连接测试数据库和模型服务另测。

复现命令（项目根目录）：

```bash
backend/.venv/bin/python backend/scripts/load_test.py --local --users 5 --spawn-rate 1 --seconds 20 --think-time 0
backend/.venv/bin/python backend/scripts/load_test.py --local --users 20 --spawn-rate 4 --seconds 20 --think-time 0
```

本轮原始报告在 `backend/profiles/locust/local-api-5/` 和 `backend/profiles/locust/local-api-20/`，均被 Git 忽略。初次运行的周期 CSV 可能比最终请求数少，以上取 HTML 的最终值；入口现已额外导出 `final-stats.csv`，后续运行用该文件核对。
