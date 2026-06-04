"""集合竞价分阶段采集与降级选股测试。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from astocks_collector.call_auction import (
    CALL_AUCTION_ACTION_BUY,
    CallAuctionSelector,
    call_auction_market_status,
)
from astocks_collector.config import AppConfig


class FakeRepository:
    """记录集合竞价采集、融合快照和候选写入的测试仓储。"""

    def __init__(self) -> None:
        """初始化仓储调用状态。"""

        self.schema_checked = False
        self.runs: list[dict] = []
        self.quote_rows: list[dict] = []
        self.pick_rows: list[dict] = []
        self.history: dict[str, list[dict]] = {}

    def ensure_schema(self) -> None:
        """记录建表检查调用。"""

        self.schema_checked = True

    def upsert_call_auction_quotes(self, rows) -> int:
        """融合写入测试行情快照。"""

        for row in rows:
            payload = dict(row)
            existing = next(
                (
                    item
                    for item in self.quote_rows
                    if item["trade_date"] == payload["trade_date"]
                    and item["symbol"] == payload["symbol"]
                ),
                None,
            )
            if existing is None:
                self.quote_rows.append(payload)
                continue
            for key in ("latest_price", "pct_change", "volume", "amount", "volume_ratio", "turnover_rate", "amplitude"):
                if payload.get(key, 0) > 0:
                    existing[key] = payload[key]
            existing["latest_sample_time"] = payload["latest_sample_time"]
            existing["source"] = payload["source"] or existing.get("source", "")
        return len(rows)

    def call_auction_quote_summary(self, trade_date) -> dict:
        """返回测试融合快照覆盖摘要。"""

        return {
            "quoteCount": len(self.quote_rows),
            "validPriceCount": len([row for row in self.quote_rows if row["latest_price"] > 0]),
            "firstSampleTime": None,
            "latestSampleTime": None,
            "totalSampleCount": len(self.quote_rows),
        }

    def latest_call_auction_quotes(self, trade_date):
        """返回已融合的测试行情快照。"""

        return [
            {
                "symbol": row["symbol"],
                "name": row["name"],
                "exchange": row["exchange"],
                "latest_price": row["latest_price"],
                "pct_change": row["pct_change"],
                "volume": row["volume"],
                "amount": row["amount"],
                "volume_ratio": row["volume_ratio"],
                "turnover_rate": row["turnover_rate"],
                "amplitude": row["amplitude"],
                "source": row["source"],
                "sample_count": 1,
                "latest_sample_time": row["latest_sample_time"],
            }
            for row in self.quote_rows
        ]

    def recent_stock_daily_history(self, symbols, trade_date, lookback_days=30):
        """返回支撑评分的测试日线历史。"""

        return {
            symbol: self.history.get(
                symbol,
                [
                    {
                        "close_price": 9 + index * 0.05,
                        "volume": 10000 + index * 100,
                        "pct_change": 0.5,
                    }
                    for index in range(30)
                ],
            )
            for symbol in symbols
        }

    def insert_call_auction_run(self, row) -> int:
        """记录集合竞价运行记录。"""

        self.runs.append(dict(row))
        return len(self.runs)

    def replace_call_auction_picks(self, run_id, picks) -> int:
        """记录集合竞价候选写入。"""

        self.pick_rows = [dict(row) for row in picks]
        return len(picks)


class FakeMarketData:
    """返回固定实时行情的测试行情源。"""

    def __init__(self, quotes) -> None:
        """保存测试行情。"""

        self.quotes = quotes
        self.call_count = 0

    def fetch_realtime_quotes(self):
        """返回测试行情并记录调用次数。"""

        self.call_count += 1
        return list(self.quotes)


class FailingLlm:
    """模拟不可用的 LLM 客户端。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.call_count = 0

    def chat_json(self, messages, max_tokens=1536):
        """抛出异常模拟 LLM 失败。"""

        self.call_count += 1
        raise RuntimeError("llm down")


def test_call_auction_market_status_has_collect_and_decision_stage():
    """集合竞价窗口应区分采集期、决策期和关闭状态。"""

    zone = ZoneInfo("Asia/Shanghai")
    before = call_auction_market_status(datetime(2026, 6, 2, 9, 14, tzinfo=zone))
    collecting = call_auction_market_status(datetime(2026, 6, 2, 9, 15, tzinfo=zone))
    deciding = call_auction_market_status(datetime(2026, 6, 2, 9, 20, tzinfo=zone))
    weekend = call_auction_market_status(datetime(2026, 6, 6, 9, 20, tzinfo=zone))

    assert before["marketOpen"] is False
    assert collecting["session"] == "pre_call_auction"
    assert collecting["collectOpen"] is True
    assert collecting["decisionOpen"] is False
    assert deciding["session"] == "call_auction"
    assert deciding["decisionOpen"] is True
    assert weekend["marketOpen"] is False


def test_collect_stage_only_writes_fused_snapshot_without_llm():
    """09:15 采集期只融合快照，不调用 LLM，也不写候选。"""

    repository = FakeRepository()
    market_data = FakeMarketData([sample_quote()])
    llm = FailingLlm()
    selector = CallAuctionSelector(
        AppConfig(call_auction_require_llm=1),
        repository=repository,
        market_data=market_data,
        llm_client=llm,
    )

    result = selector.run_once(
        now=datetime(2026, 6, 2, 9, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert result.status == "COLLECTING"
    assert result.skipped is False
    assert result.market_session == "pre_call_auction"
    assert market_data.call_count == 1
    assert llm.call_count == 0
    assert len(repository.quote_rows) == 1
    assert repository.pick_rows == []


def test_decision_stage_uses_fused_snapshot_to_fill_missing_current_fields():
    """09:20 后应使用融合快照补齐当前单帧缺失的量能字段。"""

    repository = FakeRepository()
    market_data = FakeMarketData([sample_quote()])
    selector = CallAuctionSelector(
        AppConfig(call_auction_require_llm=1),
        repository=repository,
        market_data=market_data,
        llm_client=FailingLlm(),
    )
    selector.run_once(now=datetime(2026, 6, 2, 9, 16, tzinfo=ZoneInfo("Asia/Shanghai")))
    market_data.quotes = [sample_quote(amount=0, volume_ratio=0)]

    result = selector.run_once(
        now=datetime(2026, 6, 2, 9, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert result.status == "SUCCESS"
    assert result.summary["quoteSnapshotCount"] >= 1
    assert repository.pick_rows[0]["amount"] >= 3000000
    assert repository.pick_rows[0]["volume_ratio"] >= 0.5


def test_llm_failure_falls_back_to_rule_candidates():
    """LLM 失败时应按量化规则降级输出候选。"""

    repository = FakeRepository()
    market_data = FakeMarketData([sample_quote()])
    selector = CallAuctionSelector(
        AppConfig(call_auction_require_llm=1),
        repository=repository,
        market_data=market_data,
        llm_client=FailingLlm(),
    )

    result = selector.run_once(
        now=datetime(2026, 6, 2, 9, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
    )

    assert result.status == "SUCCESS"
    assert result.llm_success is False
    assert result.summary["llmFallback"] is True
    assert result.pick_count == 1
    assert repository.pick_rows[0]["signal_action"] == CALL_AUCTION_ACTION_BUY
    assert "规则降级" in repository.pick_rows[0]["risk"]


def sample_quote(amount=5000000, volume_ratio=1.2) -> dict:
    """构造默认可入选的集合竞价测试行情。"""

    return {
        "symbol": "000001",
        "name": "平安银行",
        "exchange": "SZ",
        "latest_price": 10.8,
        "pct_change": 2.2,
        "change_amount": 0.22,
        "volume": 500000,
        "amount": amount,
        "amplitude": 2.5,
        "high_price": 10.9,
        "low_price": 10.6,
        "open_price": 10.7,
        "pre_close": 10.56,
        "volume_ratio": volume_ratio,
        "turnover_rate": 2.0,
        "source": "fake",
    }
