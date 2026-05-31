"""A股开盘时段限制的行为测试。"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from astocks_collector.config import AppConfig
from astocks_collector.realtime import RealtimeTradingEngine


class FakeRepository:
    """记录实时引擎是否触发数据库写入的测试仓储。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.schema_checked = False
        self.quote_rows = 0
        self.signal_rows = 0
        self.decision_rows = 0

    def ensure_schema(self) -> None:
        """记录建表检查调用。"""

        self.schema_checked = True

    def upsert_realtime_quotes(self, rows) -> int:
        """记录行情写入调用。"""

        self.quote_rows += len(rows)
        return len(rows)

    def insert_realtime_signals(self, rows) -> int:
        """记录信号写入调用。"""

        self.signal_rows += len(rows)
        return len(rows)

    def insert_realtime_decisions(self, rows) -> int:
        """记录决策写入调用。"""

        self.decision_rows += len(rows)
        return len(rows)


class FakeMarketData:
    """返回固定行情的测试行情源。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.call_count = 0

    def fetch_realtime_quotes(self):
        """模拟实时行情接口。"""

        self.call_count += 1
        return [
            {
                "symbol": "000001",
                "name": "平安银行",
                "exchange": "SZ",
                "latest_price": 10,
                "pct_change": 1.2,
                "change_amount": 0.12,
                "volume": 10000,
                "amount": 1000000,
                "amplitude": 2,
                "high_price": 10.2,
                "low_price": 9.9,
                "open_price": 10,
                "pre_close": 9.88,
                "volume_ratio": 1.2,
                "turnover_rate": 2.1,
                "pe_dynamic": None,
                "pb": None,
                "total_market_value": None,
                "circulating_market_value": None,
                "source": "fake",
            }
        ]


class TradingHoursTest(unittest.TestCase):
    """验证实时分析和模拟交易只在 A 股开盘时段执行。"""

    def test_realtime_run_skips_before_market_open(self) -> None:
        """开盘前实时分析应跳过，不取行情也不写信号。"""

        repository = FakeRepository()
        market_data = FakeMarketData()
        engine = TestableRealtimeEngine(
            AppConfig(realtime_decision_mode="rules"),
            repository=repository,
            market_data=market_data,
            fixed_now=datetime(2026, 5, 29, 9, 29, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        result = engine.run_once(execute_trades=True)

        self.assertTrue(result.skipped)
        self.assertFalse(result.market_open)
        self.assertEqual("closed", result.market_session)
        self.assertEqual(0, market_data.call_count)
        self.assertEqual(0, repository.quote_rows)
        self.assertEqual(0, repository.signal_rows)
        self.assertEqual(0, result.order_count)

    def test_realtime_run_works_during_morning_session(self) -> None:
        """开盘时段实时分析应正常取行情并写入信号。"""

        repository = FakeRepository()
        market_data = FakeMarketData()
        engine = TestableRealtimeEngine(
            AppConfig(realtime_decision_mode="rules"),
            repository=repository,
            market_data=market_data,
            fixed_now=datetime(2026, 5, 29, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        result = engine.run_once(execute_trades=False)

        self.assertFalse(result.skipped)
        self.assertTrue(result.market_open)
        self.assertEqual("morning", result.market_session)
        self.assertEqual(1, market_data.call_count)
        self.assertEqual(1, repository.quote_rows)
        self.assertEqual(1, repository.signal_rows)
        self.assertEqual(0, result.order_count)


class TestableRealtimeEngine(RealtimeTradingEngine):
    """注入固定时间并简化依赖的实时引擎。"""

    def __init__(self, *args, fixed_now: datetime, **kwargs) -> None:
        """保存测试时间。"""

        super().__init__(*args, **kwargs)
        self.fixed_now = fixed_now

    def _now(self) -> datetime:
        """返回固定测试时间。"""

        return self.fixed_now

    def _load_positions(self):
        """测试中没有持仓。"""

        return {}

    def _load_history_metrics(self, symbols):
        """测试中用固定历史指标。"""

        return {
            symbol: {
                "ma5": 9,
                "ma20": 8,
                "ret5": 2,
                "ret20": 5,
                "volume_ratio": 1.2,
            }
            for symbol in symbols
        }

    def _execute_simulation(self, signals):
        """测试中不真实写入模拟交易。"""

        return len([row for row in signals if row["signal_action"] == "BUY"])


if __name__ == "__main__":
    unittest.main()
