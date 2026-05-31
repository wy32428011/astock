"""实时交易 LLM 决策复核的行为测试。"""

from __future__ import annotations

import unittest

from astocks_collector.config import AppConfig
from astocks_collector.realtime import RealtimeTradingEngine


class FakeLLMClient:
    """返回固定结构化结果的测试大模型客户端。"""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        """保存测试响应或异常。"""

        self.payload = payload
        self.error = error

    def chat_json(self, messages, max_tokens=4096):
        """模拟 OpenAI 兼容 JSON 调用。"""

        if self.error:
            raise self.error
        return self.payload


class RealtimeLLMDecisionTest(unittest.TestCase):
    """验证规则预筛与 LLM 复核的组合逻辑。"""

    def test_llm_review_updates_candidate_actions_and_records_reason(self) -> None:
        """LLM 复核结果应覆盖候选动作并写入决策记录。"""

        engine = RealtimeTradingEngine(
            AppConfig(realtime_llm_candidate_limit=5),
            llm_client=FakeLLMClient(
                {
                    "decisions": [
                        {
                            "symbol": "000001",
                            "action": "HOLD",
                            "score": 72,
                            "reason": "短线追高",
                            "risk": "量能回落",
                        },
                        {
                            "symbol": "000002",
                            "action": "BUY",
                            "score": 88,
                            "reason": "趋势确认",
                            "risk": "回撤风险",
                        },
                    ]
                }
            ),
        )
        signals = [
            _signal("000001", "平安银行", "BUY", final_score=70, risk_score=76),
            _signal("000002", "万科A", "WATCH", final_score=66, risk_score=72),
        ]

        reviewed, records, used, fallback = engine._review_signals_with_llm(
            signals=signals,
            positions={},
            account={"cash": 100000, "total_asset": 100000},
        )

        self.assertTrue(used)
        self.assertFalse(fallback)
        self.assertEqual(["HOLD", "BUY"], [item["signal_action"] for item in reviewed])
        self.assertIn("LLM: 短线追高", reviewed[0]["reason"])
        self.assertEqual(2, len(records))
        self.assertEqual("llm", records[0]["decision_source"])
        self.assertEqual("BUY", records[1]["final_action"])

    def test_llm_failure_falls_back_to_rule_actions(self) -> None:
        """LLM 异常时应保留规则动作并标记降级记录。"""

        engine = RealtimeTradingEngine(
            AppConfig(realtime_llm_candidate_limit=5),
            llm_client=FakeLLMClient(error=RuntimeError("llm down")),
        )
        signals = [_signal("000001", "平安银行", "BUY", final_score=70, risk_score=76)]

        reviewed, records, used, fallback = engine._review_signals_with_llm(
            signals=signals,
            positions={},
            account={"cash": 100000, "total_asset": 100000},
        )

        self.assertFalse(used)
        self.assertTrue(fallback)
        self.assertEqual("BUY", reviewed[0]["signal_action"])
        self.assertEqual("rules_fallback", records[0]["decision_source"])
        self.assertTrue(records[0]["is_fallback"])
        self.assertIn("llm down", records[0]["llm_reason"])


def _signal(
    symbol: str,
    name: str,
    action: str,
    final_score: float,
    risk_score: float,
) -> dict:
    """构造最小实时信号。"""

    return {
        "signal_time": "2026-05-28 10:00:00",
        "symbol": symbol,
        "name": name,
        "latest_price": 10.0,
        "pct_change": 2.0,
        "trend_score": 70,
        "momentum_score": 70,
        "liquidity_score": 70,
        "risk_score": risk_score,
        "final_score": final_score,
        "signal_action": action,
        "confidence": final_score,
        "reason": "规则理由",
        "risk": "规则风险",
        "quote_snapshot": {},
    }


if __name__ == "__main__":
    unittest.main()
