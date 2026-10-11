"""验证 DeepSeek 请求会携带兼容当前工具链的模式参数。"""

import unittest
from unittest.mock import patch

from evals.offline_env import configure_offline

configure_offline()

from app.agent import planner


class LLMProviderCases(unittest.TestCase):
    def test_deepseek_disables_thinking_for_plain_and_tool_calls(self):
        with patch.object(planner.settings, "LLM_BASE_URL", "https://api.deepseek.com"):
            llm = planner.get_llm()
            self.assertEqual(llm.extra_body, {"thinking": {"type": "disabled"}})
            bound = llm.bind_tools(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ]
            )
            self.assertEqual(bound.kwargs["tools"][0]["function"]["name"], "lookup")
            self.assertEqual(bound.bound.extra_body, llm.extra_body)

    def test_other_provider_does_not_receive_deepseek_parameter(self):
        with patch.object(planner.settings, "LLM_BASE_URL", "https://other.example/v1"):
            self.assertIsNone(planner.get_llm().extra_body)
