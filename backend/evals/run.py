"""离线写库授权评测：模型输出故意错判，检验系统实际授予的工具。

运行：cd backend && .venv/bin/python -m evals.run
不连接数据库或 LLM。这个分数不代表自然语言答案质量。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.offline_env import configure_offline

configure_offline()

from app.agent import planner, tools


def main() -> int:
    cases = json.loads(Path(__file__).with_name("write_authorization.json").read_text())
    failures = []
    true_positive = false_positive = false_negative = 0
    for case in cases:
        model_reply = json.dumps(
            {
                "slots": {},
                "missing_slots": [],
                "authorized": case["model_authorized"],
                "plan_needed": False,
            }
        )
        ctx = SimpleNamespace(llm_failures=0)
        state = {"user_query": case["query"], "today": "2026-10-11", "history": []}
        with patch.object(planner, "_ask", return_value=model_reply):
            result = planner.analyze(ctx, state)
        actual = set(result["allowed_write_tools"])
        expected = set(case["expected"])
        bound = set(tools.bindable_tool_names(actual)) & set(tools.WRITE_TOOLS)
        if (
            actual != expected
            or bound != expected
            or result["authorized"] != bool(expected)
        ):
            failures.append(
                (case["id"], sorted(expected), sorted(actual), sorted(bound))
            )
        true_positive += len(expected & bound)
        false_positive += len(bound - expected)
        false_negative += len(expected - bound)

    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 1
    )
    print(f"授权用例: {len(cases) - len(failures)}/{len(cases)} 通过")
    print(f"写库工具 precision={precision:.3f} recall={recall:.3f}")
    for case_id, expected, actual, bound in failures:
        print(f"FAIL {case_id}: expected={expected}, decided={actual}, bound={bound}")
    print("范围：离线授权决策与工具清单；不评估真实模型、数据库或知识库回答。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
