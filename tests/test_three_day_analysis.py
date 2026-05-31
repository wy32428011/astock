"""未来 3 个交易日涨势分析的行为测试。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import pandas as pd

from astocks_collector.config import AppConfig
from astocks_collector.three_day_analysis import ThreeDayTrendAnalyzer


class FakeLLMClient:
    """用于测试的结构化大模型客户端。"""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        """保存测试响应或异常。"""

        self.payload = payload
        self.error = error

    def chat_json(self, messages, max_tokens=4096):
        """模拟 JSON 响应。"""

        if self.error:
            raise self.error
        return self.payload


class FakeRepository:
    """记录 3 日分析落库请求的测试仓储。"""

    def __init__(self) -> None:
        """初始化记录容器。"""

        self.saved_picks = []
        self.saved_raw_response = None
        self.saved_llm_fallback = None

    def ensure_schema(self) -> None:
        """测试中无需真实建表。"""

    def replace_three_day_picks(
        self,
        analysis_date: str,
        picks,
        raw_response=None,
        llm_fallback: bool = False,
    ) -> int:
        """记录待写入的 3 日分析结果。"""

        self.saved_picks = list(picks)
        self.saved_raw_response = raw_response
        self.saved_llm_fallback = llm_fallback
        return len(self.saved_picks)


class ThreeDayTrendAnalyzerTest(unittest.TestCase):
    """验证 3 日趋势分析的核心行为。"""

    def test_build_candidates_prefers_breakout_with_healthy_volume(self) -> None:
        """量化预筛应优先选择趋势突破且量能温和放大的股票。"""

        analyzer = ThreeDayTrendAnalyzer(AppConfig())
        latest_trade_date = date(2026, 5, 27)
        frame = _history_frame(
            latest_trade_date,
            [
                ("000001", "强势股份", [10 + index * 0.12 for index in range(36)], 1.6),
                ("000002", "平淡股份", [12 - index * 0.01 for index in range(36)], 0.8),
            ],
        )

        candidates = analyzer._build_candidates(frame, latest_trade_date, limit=5)

        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual("000001", candidates[0]["symbol"])
        self.assertGreater(candidates[0]["three_day_score"], 60)
        self.assertIn("breakout_strength", candidates[0]["factor_snapshot"])

    def test_merge_llm_uses_sixty_forty_weighting(self) -> None:
        """综合分应按量化 60% 和 LLM 40% 融合。"""

        analyzer = ThreeDayTrendAnalyzer(AppConfig())
        candidates = [
            _candidate("000001", "强势股份", 70),
            _candidate("000002", "稳健股份", 65),
        ]

        picks = analyzer._merge_llm(
            candidates=candidates,
            llm_payload=[
                {
                    "symbol": "000001",
                    "llm_score": 90,
                    "expected_direction": "未来3个交易日震荡上行",
                    "reason": "趋势延续",
                    "risk": "追高回落",
                }
            ],
            final_limit=1,
            trade_date=date(2026, 5, 27),
            llm_fallback=False,
        )

        self.assertEqual(1, len(picks))
        self.assertEqual("000001", picks[0]["symbol"])
        self.assertEqual(78.0, float(picks[0]["final_score"]))
        self.assertEqual("未来3个交易日震荡上行", picks[0]["expected_direction"])

    def test_analyze_falls_back_to_quant_when_llm_fails(self) -> None:
        """LLM 异常时应使用量化排序完成分析并标记降级。"""

        repository = FakeRepository()
        latest_trade_date = date(2026, 5, 27)

        class TestAnalyzer(ThreeDayTrendAnalyzer):
            """注入固定历史数据的测试分析器。"""

            def _resolve_trade_date(self, trade_date):
                """返回固定基准交易日。"""

                return latest_trade_date

            def _load_history(self, latest_trade_date):
                """返回固定全市场样本。"""

                return _history_frame(
                    latest_trade_date,
                    [("000001", "强势股份", [10 + index * 0.12 for index in range(36)], 1.6)],
                )

        analyzer = TestAnalyzer(
            AppConfig(three_day_preselect_limit=10, three_day_final_limit=3),
            repository=repository,
            llm_client=FakeLLMClient(error=RuntimeError("llm unavailable")),
        )

        result = analyzer.analyze(use_llm=True)

        self.assertTrue(result.llm_fallback)
        self.assertEqual(1, result.final_count)
        self.assertEqual(1, len(repository.saved_picks))
        self.assertTrue(repository.saved_llm_fallback)
        self.assertEqual("000001", repository.saved_picks[0]["symbol"])


def _history_frame(latest_trade_date: date, specs: list[tuple[str, str, list[float], float]]) -> pd.DataFrame:
    """构造多只股票的历史日线 DataFrame。"""

    rows = []
    start_date = latest_trade_date - timedelta(days=len(specs[0][2]) - 1)
    for symbol, name, closes, latest_volume_ratio in specs:
        base_volume = 100000.0
        for index, close in enumerate(closes):
            trade_date = start_date + timedelta(days=index)
            volume = base_volume * (latest_volume_ratio if index >= len(closes) - 5 else 1.0)
            previous = closes[index - 1] if index > 0 else close
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "exchange": "SZ",
                    "trade_date": trade_date,
                    "open_price": close * 0.99,
                    "close_price": close,
                    "high_price": close * 1.02,
                    "low_price": close * 0.98,
                    "volume": volume,
                    "amount": volume * close * 100,
                    "pct_change": ((close / previous - 1) * 100) if previous else 0,
                    "turnover_rate": 5.0,
                }
            )
    return pd.DataFrame(rows)


def _candidate(symbol: str, name: str, score: float) -> dict:
    """构造最小 3 日候选。"""

    return {
        "symbol": symbol,
        "name": name,
        "trade_date": "2026-05-27",
        "close_price": 10.0,
        "pct_change": 2.0,
        "three_day_score": score,
        "quant_reason": "趋势向上",
        "risk": "注意波动",
        "factor_snapshot": {"ret_3": 2.1},
    }


if __name__ == "__main__":
    unittest.main()
