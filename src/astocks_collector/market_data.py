"""AKShare 数据源适配层。"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak
import pandas as pd


class AkshareMarketData:
    """封装 AKShare A股接口，并将中文字段标准化为数据库字段。"""

    basic_source = "akshare.stock_zh_a_spot_em"
    basic_fallback_source = "akshare.stock_info_a_code_name"
    realtime_eastmoney_fallback_source = "eastmoney.push2.clist"
    realtime_sina_fallback_source = "sina.hq"
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

    def fetch_realtime_quotes(
        self, symbols: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """获取沪深京 A股实时行情快照，支持按股票代码定向拉取。"""

        target_symbols = _normalize_symbol_set(symbols)
        if target_symbols:
            try:
                return self._fetch_realtime_from_sina(target_symbols)
            except Exception:
                pass

        try:
            frame = ak.stock_zh_a_spot_em()
            records = self._normalize_realtime_frame(frame, self.basic_source)
            if not target_symbols and len(records) < 1000:
                try:
                    return self._fetch_realtime_from_eastmoney()
                except Exception:
                    return records
            if target_symbols:
                records = [item for item in records if item["symbol"] in target_symbols]
                if not records:
                    raise RuntimeError("AKShare 实时行情没有命中目标股票")
            return records
        except Exception:
            if target_symbols:
                return self._fetch_realtime_from_sina(target_symbols)
            return self._fetch_realtime_from_eastmoney()

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

    def _fetch_realtime_from_sina(self, symbols: Sequence[str]) -> list[dict[str, Any]]:
        """从新浪实时行情接口批量拉取指定股票，用作 T+1 估值兜底。"""

        records: list[dict[str, Any]] = []
        for chunk in _chunked_symbols(symbols, 80):
            prefixed_symbols = ",".join(to_prefixed_symbol(symbol) for symbol in chunk)
            url = f"https://hq.sinajs.cn/list={prefixed_symbols}"
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn/",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = response.read().decode("gbk", errors="ignore")
            records.extend(self._normalize_sina_realtime_payload(payload))
        if not records:
            raise RuntimeError("新浪实时行情没有返回可用数据")
        return records

    def _normalize_sina_realtime_payload(self, payload: str) -> list[dict[str, Any]]:
        """解析新浪批量行情脚本响应为统一实时行情结构。"""

        records: list[dict[str, Any]] = []
        pattern = re.compile(r'var hq_str_(?:sh|sz|bj)(\d{6})="([^"]*)";')
        for match in pattern.finditer(payload):
            symbol = _normalize_symbol(match.group(1))
            fields = match.group(2).split(",")
            if len(fields) < 32 or not symbol:
                continue
            name = fields[0].strip()
            open_price = _to_decimal(fields[1])
            pre_close = _to_decimal(fields[2])
            latest_price = _to_decimal(fields[3])
            high_price = _to_decimal(fields[4])
            low_price = _to_decimal(fields[5])
            change_amount = _calc_change_amount(latest_price, pre_close)
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "exchange": infer_exchange(symbol),
                    "latest_price": latest_price,
                    "pct_change": _calc_pct_change(change_amount, pre_close),
                    "change_amount": change_amount,
                    "volume": _to_decimal(fields[8]),
                    "amount": _to_decimal(fields[9]),
                    "amplitude": _calc_amplitude(high_price, low_price, pre_close),
                    "high_price": high_price,
                    "low_price": low_price,
                    "open_price": open_price,
                    "pre_close": pre_close,
                    "volume_ratio": None,
                    "turnover_rate": None,
                    "pe_dynamic": None,
                    "pb": None,
                    "total_market_value": None,
                    "circulating_market_value": None,
                    "source": self.realtime_sina_fallback_source,
                }
            )
        return records

    def _fetch_realtime_from_eastmoney(self) -> list[dict[str, Any]]:
        """从东方财富直连接口拉取全市场实时行情，用作 AKShare 失败后的兜底。"""

        records: list[dict[str, Any]] = []
        page_size = 100
        total = page_size
        page = 1
        while len(records) < total:
            try:
                payload = self._request_eastmoney_realtime_page(page, page_size)
            except Exception:
                if records:
                    break
                raise
            data = payload.get("data") or {}
            total = int(data.get("total") or 0)
            rows = data.get("diff") or []
            records.extend(self._normalize_eastmoney_realtime_rows(rows))
            if not rows or len(rows) < page_size:
                break
            page += 1
        if not records:
            raise RuntimeError("东方财富实时行情没有返回可用数据")
        return records

    def _request_eastmoney_realtime_page(
        self, page: int, page_size: int
    ) -> dict[str, Any]:
        """请求东方财富实时行情分页，并对临时 502/断连做轻量重试。"""

        params = {
            "pn": str(page),
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f18,f10,f8,f9,f23,f20,f21",
        }
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(
            params
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://quote.eastmoney.com/",
                    },
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    return json.loads(response.read())
            except Exception as exc:
                last_error = exc
                time.sleep(0.3 * (attempt + 1))
        raise RuntimeError(f"东方财富实时行情请求失败: {last_error}") from last_error

    def _normalize_eastmoney_realtime_rows(
        self, rows: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """标准化东方财富直连实时行情字段。"""

        records: list[dict[str, Any]] = []
        for row in rows:
            symbol = _normalize_symbol(row.get("f12"))
            if not symbol:
                continue
            records.append(
                {
                    "symbol": symbol,
                    "name": str(row.get("f14") or "").strip(),
                    "exchange": infer_exchange(symbol),
                    "latest_price": _to_decimal(row.get("f2")),
                    "pct_change": _to_decimal(row.get("f3")),
                    "change_amount": _to_decimal(row.get("f4")),
                    "volume": _to_decimal(row.get("f5")),
                    "amount": _to_decimal(row.get("f6")),
                    "amplitude": _to_decimal(row.get("f7")),
                    "high_price": _to_decimal(row.get("f15")),
                    "low_price": _to_decimal(row.get("f16")),
                    "open_price": _to_decimal(row.get("f17")),
                    "pre_close": _to_decimal(row.get("f18")),
                    "volume_ratio": _to_decimal(row.get("f10")),
                    "turnover_rate": _to_decimal(row.get("f8")),
                    "pe_dynamic": _to_decimal(row.get("f9")),
                    "pb": _to_decimal(row.get("f23")),
                    "total_market_value": _to_decimal(row.get("f20")),
                    "circulating_market_value": _to_decimal(row.get("f21")),
                    "source": self.realtime_eastmoney_fallback_source,
                }
            )
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


def _normalize_symbol_set(symbols: Sequence[str] | None) -> set[str]:
    """将外部传入的股票代码列表整理为去重后的六位代码集合。"""

    if not symbols:
        return set()
    return {symbol for item in symbols if (symbol := _normalize_symbol(item))}


def _chunked_symbols(symbols: Sequence[str], size: int) -> list[list[str]]:
    """按接口 URL 长度限制切分股票代码列表。"""

    items = list(symbols)
    return [items[index : index + size] for index in range(0, len(items), size)]


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
