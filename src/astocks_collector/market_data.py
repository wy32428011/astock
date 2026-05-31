"""AKShare 数据源适配层。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak
import pandas as pd


class AkshareMarketData:
    """封装 AKShare A股接口，并将中文字段标准化为数据库字段。"""

    basic_source = "akshare.stock_zh_a_spot_em"
    basic_fallback_source = "akshare.stock_info_a_code_name"
    daily_source = "akshare.stock_zh_a_hist"
    daily_fallback_source = "akshare.stock_zh_a_daily"

    def __init__(self, daily_provider: str = "sina") -> None:
        """初始化日线数据源策略。"""

        self.daily_provider = daily_provider
        self._eastmoney_available = daily_provider in {"auto", "eastmoney"}

    def fetch_stock_basic(self) -> list[dict[str, Any]]:
        """获取沪深京 A股股票列表和实时基础行情。"""

        try:
            frame = ak.stock_zh_a_spot_em()
            return self._normalize_spot_frame(frame, self.basic_source)
        except Exception:
            frame = ak.stock_info_a_code_name()
            return self._normalize_code_name_frame(frame)

    def fetch_realtime_quotes(self) -> list[dict[str, Any]]:
        """获取沪深京 A股实时行情快照。"""

        frame = ak.stock_zh_a_spot_em()
        return self._normalize_realtime_frame(frame, self.basic_source)

    def _normalize_spot_frame(
        self, frame: pd.DataFrame | None, source: str
    ) -> list[dict[str, Any]]:
        """标准化 Eastmoney 股票实时行情列表。"""

        if frame is None or frame.empty:
            raise RuntimeError("AKShare 未返回股票列表数据")

        records: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            symbol = _normalize_symbol(_get(row, "代码"))
            if not symbol:
                continue
            records.append(
                {
                    "symbol": symbol,
                    "name": str(_get(row, "名称") or "").strip(),
                    "exchange": infer_exchange(symbol),
                    "market": "A",
                    "latest_price": _to_decimal(_get(row, "最新价")),
                    "pct_change": _to_decimal(_get(row, "涨跌幅")),
                    "turnover_rate": _to_decimal(_get(row, "换手率")),
                    "total_market_value": _to_decimal(_get(row, "总市值")),
                    "circulating_market_value": _to_decimal(_get(row, "流通市值")),
                    "source": source,
                    "is_active": 1,
                }
            )
        if not records:
            raise RuntimeError("AKShare 股票列表没有可用代码")
        return records

    def _normalize_realtime_frame(
        self, frame: pd.DataFrame | None, source: str
    ) -> list[dict[str, Any]]:
        """标准化实时行情字段，供盘中分析和模拟交易使用。"""

        if frame is None or frame.empty:
            raise RuntimeError("AKShare 未返回实时行情数据")

        records: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            symbol = _normalize_symbol(_get(row, "代码"))
            if not symbol:
                continue
            records.append(
                {
                    "symbol": symbol,
                    "name": str(_get(row, "名称") or "").strip(),
                    "exchange": infer_exchange(symbol),
                    "latest_price": _to_decimal(_get(row, "最新价")),
                    "pct_change": _to_decimal(_get(row, "涨跌幅")),
                    "change_amount": _to_decimal(_get(row, "涨跌额")),
                    "volume": _to_decimal(_get(row, "成交量")),
                    "amount": _to_decimal(_get(row, "成交额")),
                    "amplitude": _to_decimal(_get(row, "振幅")),
                    "high_price": _to_decimal(_get(row, "最高")),
                    "low_price": _to_decimal(_get(row, "最低")),
                    "open_price": _to_decimal(_get(row, "今开")),
                    "pre_close": _to_decimal(_get(row, "昨收")),
                    "volume_ratio": _to_decimal(_get(row, "量比")),
                    "turnover_rate": _to_decimal(_get(row, "换手率")),
                    "pe_dynamic": _to_decimal(_get(row, "市盈率-动态")),
                    "pb": _to_decimal(_get(row, "市净率")),
                    "total_market_value": _to_decimal(_get(row, "总市值")),
                    "circulating_market_value": _to_decimal(_get(row, "流通市值")),
                    "source": source,
                }
            )
        if not records:
            raise RuntimeError("AKShare 实时行情没有可用代码")
        return records

    def _normalize_code_name_frame(self, frame: pd.DataFrame | None) -> list[dict[str, Any]]:
        """标准化备用股票代码名称列表。"""

        if frame is None or frame.empty:
            raise RuntimeError("AKShare 备用股票列表没有返回数据")

        records: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            symbol = _normalize_symbol(_get(row, "code", "代码"))
            if not symbol:
                continue
            records.append(
                {
                    "symbol": symbol,
                    "name": str(_get(row, "name", "名称") or "").strip(),
                    "exchange": infer_exchange(symbol),
                    "market": "A",
                    "latest_price": None,
                    "pct_change": None,
                    "turnover_rate": None,
                    "total_market_value": None,
                    "circulating_market_value": None,
                    "source": self.basic_fallback_source,
                    "is_active": 1,
                }
            )
        if not records:
            raise RuntimeError("AKShare 备用股票列表没有可用代码")
        return records

    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust_type: str,
    ) -> list[dict[str, Any]]:
        """获取单只股票指定日期范围内的日线行情。"""

        if self.daily_provider == "sina":
            return self._fetch_daily_from_sina(symbol, start_date, end_date, adjust_type)

        if self.daily_provider == "eastmoney":
            return self._fetch_daily_from_eastmoney(
                symbol, start_date, end_date, adjust_type
            )

        if self._eastmoney_available:
            try:
                return self._fetch_daily_from_eastmoney(
                    symbol, start_date, end_date, adjust_type
                )
            except Exception:
                self._eastmoney_available = False

        return self._fetch_daily_from_sina(symbol, start_date, end_date, adjust_type)

    def _fetch_daily_from_eastmoney(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust_type: str,
    ) -> list[dict[str, Any]]:
        """从 Eastmoney 日线接口获取行情。"""

        frame = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust_type,
        )
        return self._normalize_hist_frame(frame, symbol, adjust_type)

    def _fetch_daily_from_sina(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        adjust_type: str,
    ) -> list[dict[str, Any]]:
        """从新浪日线接口获取行情。"""

        frame = ak.stock_zh_a_daily(
            symbol=to_prefixed_symbol(symbol),
            start_date=start_date,
            end_date=end_date,
            adjust=adjust_type,
        )
        return self._normalize_daily_frame(frame, symbol, adjust_type)

    def _normalize_hist_frame(
        self,
        frame: pd.DataFrame | None,
        symbol: str,
        adjust_type: str,
    ) -> list[dict[str, Any]]:
        """标准化 Eastmoney 日线行情。"""

        if frame is None or frame.empty:
            return []

        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            trade_date = _to_date(_get(row, "日期"))
            if trade_date is None:
                continue

            close_price = _to_decimal(_get(row, "收盘"))
            change_amount = _to_decimal(_get(row, "涨跌额"))
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "adjust_type": adjust_type,
                    "open_price": _to_decimal(_get(row, "开盘")),
                    "close_price": close_price,
                    "high_price": _to_decimal(_get(row, "最高")),
                    "low_price": _to_decimal(_get(row, "最低")),
                    "pre_close": _calc_pre_close(close_price, change_amount),
                    "volume": _to_decimal(_get(row, "成交量")),
                    "amount": _to_decimal(_get(row, "成交额")),
                    "amplitude": _to_decimal(_get(row, "振幅")),
                    "pct_change": _to_decimal(_get(row, "涨跌幅")),
                    "change_amount": change_amount,
                    "turnover_rate": _to_decimal(_get(row, "换手率")),
                    "source": self.daily_source,
                }
            )
        return rows

    def _normalize_daily_frame(
        self,
        frame: pd.DataFrame | None,
        symbol: str,
        adjust_type: str,
    ) -> list[dict[str, Any]]:
        """标准化新浪日线行情备用数据。"""

        if frame is None or frame.empty:
            return []

        rows: list[dict[str, Any]] = []
        previous_close: Decimal | None = None
        for _, row in frame.sort_values("date").iterrows():
            trade_date = _to_date(_get(row, "date"))
            if trade_date is None:
                continue

            close_price = _to_decimal(_get(row, "close"))
            high_price = _to_decimal(_get(row, "high"))
            low_price = _to_decimal(_get(row, "low"))
            change_amount = _calc_change_amount(close_price, previous_close)
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "adjust_type": adjust_type,
                    "open_price": _to_decimal(_get(row, "open")),
                    "close_price": close_price,
                    "high_price": high_price,
                    "low_price": low_price,
                    "pre_close": previous_close,
                    "volume": _shares_to_hands(_to_decimal(_get(row, "volume"))),
                    "amount": _to_decimal(_get(row, "amount")),
                    "amplitude": _calc_amplitude(high_price, low_price, previous_close),
                    "pct_change": _calc_pct_change(change_amount, previous_close),
                    "change_amount": change_amount,
                    "turnover_rate": _ratio_to_percent(_to_decimal(_get(row, "turnover"))),
                    "source": self.daily_fallback_source,
                }
            )
            if close_price is not None:
                previous_close = close_price
        return rows


def infer_exchange(symbol: str) -> str:
    """根据 A股代码前缀推断交易所。"""

    if symbol.startswith("6"):
        return "SH"
    if symbol.startswith(("0", "2", "3")):
        return "SZ"
    if symbol.startswith(("4", "8", "9")):
        return "BJ"
    return "UNKNOWN"


def to_prefixed_symbol(symbol: str) -> str:
    """转换为 AKShare 新浪日线接口需要的市场前缀代码。"""

    exchange = infer_exchange(symbol)
    if exchange == "SH":
        return f"sh{symbol}"
    if exchange == "SZ":
        return f"sz{symbol}"
    if exchange == "BJ":
        return f"bj{symbol}"
    return symbol


def _get(row: Any, *names: str) -> Any:
    """兼容 AKShare 字段轻微变化的取值函数。"""

    for name in names:
        if name in row:
            return row[name]
    return None


def _normalize_symbol(value: Any) -> str:
    """将股票代码规范化为六位数字字符串。"""

    if value is None or pd.isna(value):
        return ""
    digits = "".join(ch for ch in str(value).strip() if ch.isdigit())
    if not digits:
        return ""
    return digits[-6:].zfill(6)


def _to_decimal(value: Any) -> Decimal | None:
    """将 AKShare 返回的数值安全转换为 Decimal。"""

    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass

    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "nan", "NaN", "None"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _to_date(value: Any) -> date | None:
    """将 AKShare 日期字段转换为 Python date。"""

    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).date()


def _calc_pre_close(
    close_price: Decimal | None, change_amount: Decimal | None
) -> Decimal | None:
    """根据收盘价和涨跌额估算昨收价。"""

    if close_price is None or change_amount is None:
        return None
    return close_price - change_amount


def _calc_change_amount(
    close_price: Decimal | None, previous_close: Decimal | None
) -> Decimal | None:
    """根据当前收盘价和前收盘价计算涨跌额。"""

    if close_price is None or previous_close is None:
        return None
    return close_price - previous_close


def _calc_pct_change(
    change_amount: Decimal | None, previous_close: Decimal | None
) -> Decimal | None:
    """根据涨跌额和前收盘价计算涨跌幅百分比。"""

    if change_amount is None or previous_close in {None, Decimal("0")}:
        return None
    return (change_amount / previous_close * Decimal("100")).quantize(Decimal("0.0001"))


def _calc_amplitude(
    high_price: Decimal | None,
    low_price: Decimal | None,
    previous_close: Decimal | None,
) -> Decimal | None:
    """根据最高价、最低价和前收盘价计算振幅百分比。"""

    if high_price is None or low_price is None or previous_close in {None, Decimal("0")}:
        return None
    return ((high_price - low_price) / previous_close * Decimal("100")).quantize(
        Decimal("0.0001")
    )


def _shares_to_hands(value: Decimal | None) -> Decimal | None:
    """将新浪日线接口的股数成交量转换为手。"""

    if value is None:
        return None
    return value / Decimal("100")


def _ratio_to_percent(value: Decimal | None) -> Decimal | None:
    """将小数形式换手率转换为百分比。"""

    if value is None:
        return None
    if abs(value) <= Decimal("1"):
        return (value * Decimal("100")).quantize(Decimal("0.0001"))
    return value
