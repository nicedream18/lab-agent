"""Agent Workflow 包 —— 面向实验室资源管理场景的自主工作流 Agent。

模块划分（严格按职责分层，禁止越界调用）：

    state.py    状态契约：AgentState / AgentContext / 各类常量与标签
    prompts.py  提示词模板：所有给模型的话术都写在这里，便于统一调优
    planner.py  LLM 交互层：只负责「问模型 + 解析它的话」，不碰数据库
    tools.py    工具层：所有业务能力的唯一出口，**数据库只能在这里被读到**
    memory.py   记忆层：短期会话上下文 + 长期用户偏好
    tracer.py   可观测层：把每个节点的输入输出落库，供前端轨迹面板回放
    graph.py    编排层：LangGraph 工作流，决定「先做什么、失败怎么办」

调用关系是单向的：

    graph.py ──调用──> planner.py ──调用──> (无副作用，只发请求)
       │
       ├────调用──> tools.py ────调用───> services/*
       ├────调用──> memory.py
       └────调用──> tracer.py

关键设计约束：**LLM 永远拿不到数据库连接。**
模型能做的只有「发出 tool_calls」，工具的参数由它自己填、由 tools.py 执行，
它看不到 ctx（数据库会话、当前用户）—— 那些参数根本不在工具的 JSON Schema 里。
写库仍然要走闸门，但不是靠提示词请求，而是靠 bind_tools 的清单：
没有授权时，create_reservation / cancel_reservation 压根不在模型能选的工具里。

这里刻意不写 __all__：包内子模块之间都是显式导入（from app.agent import tools），
没有任何 from app.agent import *。写了反而要求先导入各子模块，而 graph.py 又反过来
导入本包，会造成「半初始化的包」的循环导入。
"""
