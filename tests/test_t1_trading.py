from datetime import date

from astocks_collector.t1_trading import is_t1_position_available, next_trading_date_after


def test_t1_position_is_not_available_on_buy_trade_date() -> None:
    """T+1 持仓在买入交易日不可卖出。"""
    buy_date = date(2026, 5, 29)
    available_date = date(2026, 6, 1)

    assert not is_t1_position_available(buy_date, available_date, buy_date)


def test_t1_position_is_available_from_next_trade_date() -> None:
    """T+1 持仓从下一交易日起允许卖出。"""
    buy_date = date(2026, 5, 29)
    available_date = date(2026, 6, 1)

    assert is_t1_position_available(buy_date, available_date, available_date)


def test_next_trading_date_after_uses_strictly_later_date() -> None:
    """下一交易日必须严格晚于当前交易日。"""
    trade_dates = [date(2026, 5, 28), date(2026, 5, 29), date(2026, 6, 1)]

    assert next_trading_date_after(trade_dates, date(2026, 5, 29)) == date(2026, 6, 1)
