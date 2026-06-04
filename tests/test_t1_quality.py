from datetime import date, datetime

from astocks_collector.t1_api_routes import T1QualityRunRequest
from astocks_collector.t1_quality import (
    T1IntradayQualitySelector,
    build_quality_pick_row,
    build_quality_run_row,
    calculate_quality_score,
    is_t1_quality_window_open,
    parse_quality_schedule_time,
)
from astocks_collector.t1_trading import is_t1_position_available


class FailingLlm:
    def chat_json(self, messages, max_tokens=4096):
        raise RuntimeError("boom")


def test_parse_quality_schedule_time_defaults_to_1405():
    assert parse_quality_schedule_time(None) == (14, 5)


def test_parse_quality_schedule_time_accepts_hhmm():
    assert parse_quality_schedule_time("14:05") == (14, 5)
    assert parse_quality_schedule_time("14:00") == (14, 0)


def test_parse_quality_schedule_time_rejects_before_1400():
    try:
        parse_quality_schedule_time("13:59")
    except ValueError as exc:
        assert "14:00" in str(exc)
    else:
        raise AssertionError("13:59 should be rejected")


def test_quality_window_opens_at_1405():
    now = datetime(2026, 6, 2, 14, 5, 0)
    assert is_t1_quality_window_open(now, "Asia/Shanghai", "14:05")[0] is True


def test_quality_window_closed_before_1405():
    now = datetime(2026, 6, 2, 14, 4, 59)
    open_, reason = is_t1_quality_window_open(now, "Asia/Shanghai", "14:05")
    assert open_ is False
    assert "14:05" in reason


def test_build_quality_run_row_marks_llm_skip():
    row = build_quality_run_row(
        trade_date=date(2026, 6, 2),
        snapshot_time=datetime(2026, 6, 2, 14, 5),
        trigger_type="scheduler",
        status="SKIPPED",
        quote_count=3000,
        valid_quote_count=2500,
        candidate_count=120,
        pick_count=0,
        buy_count=0,
        sell_count=0,
        execute_trades=True,
        llm_required=True,
        llm_success=False,
        market_session="afternoon",
        skip_reason="LLM 复核失败，已跳过买入",
        error_message="",
        summary={"reason": "llm_failed"},
    )
    assert row["status"] == "SKIPPED"
    assert row["llm_required"] is True
    assert row["llm_success"] is False
    assert row["buy_count"] == 0


def test_build_quality_pick_row_contains_t1_expected_direction():
    row = build_quality_pick_row(
        run_id=1,
        trade_date=date(2026, 6, 2),
        snapshot_time=datetime(2026, 6, 2, 14, 5),
        rank_no=1,
        candidate={
            "symbol": "000001",
            "name": "平安银行",
            "latestPrice": 10.5,
            "pctChange": 2.1,
            "volumeRatio": 1.8,
            "turnoverRate": 3.2,
            "trendScore": 72,
            "momentumScore": 68,
            "liquidityScore": 70,
            "riskScore": 80,
            "quantScore": 72.5,
            "llmScore": 74,
            "finalScore": 73.2,
            "action": "BUY",
            "reason": "趋势向上",
            "risk": "盘中波动",
            "factorSnapshot": {"ret5": 3.1},
            "rawResponse": {"action": "KEEP"},
        },
    )
    assert row["expected_direction"] == "T日买入，T+1可卖"
    assert row["signal_action"] == "BUY"


def test_quality_selector_requires_llm_success_for_buys():
    selector = object.__new__(T1IntradayQualitySelector)
    selector.llm_client = FailingLlm()
    selector.settings = type("Settings", (), {"t1_quality_llm_max_tokens": 4096})()
    candidates = [{"symbol": "000001", "name": "平安银行", "quantScore": 80, "finalScore": 80}]
    reviewed, status = selector._review_with_llm(candidates)
    assert reviewed == []
    assert status["llm_success"] is False
    assert "LLM" in status["skip_reason"]


def test_quality_score_prefers_positive_momentum_and_controlled_risk():
    score = calculate_quality_score(
        latest_price=10.5,
        pct_change=2.0,
        volume_ratio=1.8,
        turnover_rate=3.0,
        amount=300000000,
        amplitude=3.5,
        name="平安银行",
        history={"ma5": 10, "ma20": 9.5, "ret5": 3.0, "ret20": 8.0},
    )
    assert score["finalScore"] >= 64
    assert score["riskScore"] >= 55


def test_quality_buy_is_locked_on_t_day_and_sellable_on_t_plus_one():
    buy_date = date(2026, 6, 2)
    available_from = date(2026, 6, 3)
    assert is_t1_position_available(buy_date, available_from, buy_date) is False
    assert is_t1_position_available(buy_date, available_from, date(2026, 6, 3)) is True


def test_t1_quality_run_request_defaults_to_execute_trades():
    request = T1QualityRunRequest()
    assert request.should_execute_trades() is True
    assert request.limit() == 20
    assert request.should_force() is False
