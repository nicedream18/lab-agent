"""Agent Workflow 包 —— 面向实验室资源管理场景的自主工作流 Agent。

模块划分（严格按职责分层，禁止越界调用）：

    state.py    状态契约：AgentState / AgentContext / PlanStep 的定义处
    prompts.py  提示词模板：所有给模型的话术都写在这里，便于统一调优
    planner.py  LLM 交互层：只负责「问模型 + 解析 JSON」，不碰数据库
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
模型能做的只有「提出计划」和「填写参数」，真正落库由 tools.py 执行，
且 create_reservation 必须通过 graph 里的授权闸门。
"""

__all__ = ["state", "prompts", "planner", "tools", "memory", "tracer", "graph"]
