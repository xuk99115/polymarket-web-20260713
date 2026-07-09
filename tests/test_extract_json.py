"""AI JSON 提取鲁棒性单测。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestExtractJson(unittest.TestCase):
    def setUp(self):
        from src.ai.decision import _extract_json
        self.extract = _extract_json

    def test_pure_json(self):
        text = '{"action": "BUY", "confidence": 0.7, "reason": "test"}'
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        data = json.loads(result)
        self.assertEqual(data["action"], "BUY")

    def test_think_block_stripped(self):
        text = '''<think>
Let me think about this carefully.
The market is at 50/50 so I'll skip.
</think>
{"action": "SKIP", "confidence": 0.3, "reason": "50/50 no edge"}'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "SKIP")

    def test_nested_think_blocks(self):
        text = '''<think>outer <think>inner still thinking</think> more outer</think>{"action": "BUY", "confidence": 0.8}'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "BUY")

    def test_unclosed_think_block(self):
        text = '''<think>never closes here
{"action": "SKIP", "confidence": 0.5, "reason": "no"}'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "SKIP")

    def test_markdown_fence_stripped(self):
        text = '''Here is my decision:
```json
{"action": "BUY", "outcome_index": 0, "confidence": 0.65, "reason": "edge found"}
```'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "BUY")

    def test_json_with_surrounding_text(self):
        text = '''My analysis suggests:
{"action": "SKIP", "outcome_index": null, "reason": "too close to expiry"}

That's my final answer.'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "SKIP")

    def test_nested_json_object(self):
        """JSON 内嵌套对象也应能正确匹配。"""
        text = '''{"action": "BUY", "context": {"price": 0.5, "edge": 0.1}, "reason": "test"}'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        data = json.loads(result)
        self.assertEqual(data["action"], "BUY")
        self.assertEqual(data["context"]["price"], 0.5)

    def test_no_json_returns_none(self):
        text = "Sorry, I cannot provide a decision right now."
        result = self.extract(text)
        self.assertIsNone(result)

    def test_empty_string(self):
        result = self.extract("")
        self.assertIsNone(result)

    def test_think_plus_markdown(self):
        """最常见的真实场景: think 块 + markdown fence。"""
        text = '''<think>
The market is at 50/50 and spread is wide.
Need to skip.
</think>

```json
{"action": "SKIP", "outcome_index": null, "confidence": 0.2, "reason": "无方向优势"}
```'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        self.assertEqual(json.loads(result)["action"], "SKIP")

    def test_real_m_world_sample(self):
        """模拟一个真实 MiniMax 推理模型可能返回的样本。"""
        text = '''<think>Let me analyze this BTC 15-minute market carefully.

Market: Bitcoin Up or Down - June 20, 7:00PM-7:15PM ET
End time: 2026-06-20T23:15:00Z

Current pricing:
- Up: 0.50 (bid 0.49, ask 0.51)
- Down: 0.50 (bid 0.49, ask 0.51)

BTC momentum:
- 1m: -0.02%
- 3m: +0.01%
- 5m: +0.03%
- 15m: +0.05%

The momentum is mixed and not strong enough. The market is at 50/50 with no clear edge.
Spread is 0.02 which is acceptable but there's no directional advantage.

Decision: SKIP.</think>{
  "action": "SKIP",
  "outcome_index": null,
  "outcome_label": null,
  "confidence": 0.3,
  "reason": "无明确方向优势,定价 50/50,短线动量混杂"
}'''
        result = self.extract(text)
        self.assertIsNotNone(result)
        import json
        data = json.loads(result)
        self.assertEqual(data["action"], "SKIP")
        self.assertEqual(data["confidence"], 0.3)


if __name__ == "__main__":
    unittest.main()
