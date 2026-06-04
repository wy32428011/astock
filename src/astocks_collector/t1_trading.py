"""T+1 专属模拟交易引擎。

本模块只处理 A 股 T+1 约束下的模拟交易：买入当日不可卖出，下一交易日起
才释放可卖数量。它使用独立的 t1_simulation_* 表，避免影响实时模拟交易。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .config import AppConfig
from .db import MySQLRepository
from .market_data import AkshareMarketData
from .realtime import a_share_market_status


DECIMAL_ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(slots=True)
class T1TradingRunResult:
    """T+1 模拟交易单次运行结果。"""

    analysis_date: date | None
    trade_date: date | None
    candidate_count: int
    buy_count: int
    sell_count: int
    order_count: int
    quote_count: int = 0
    realtime: bool = False
    market_open: bool | None = None
    market_session: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可直接返回的字典。"""
        return {
            "analysisDate": _date_to_str(self.analysis_date),
            "tradeDate": _date_to_str(self.trade_date),
            "candidateCount": self.candidate_count,
            "buyCount": self.buy_count,
            "sellCount": self.sell_count,
            "orderCount": self.order_count,
            "quoteCount": self.quote_count,
            "realtime": self.realtime,
            "marketOpen": self.market_open,
            "marketSession": self.market_session,
            "skipped": self.skipped,
            "skipReason": self.skip_reason,
        }


def next_trading_date_after(trade_dates: Iterable[date], current_date: date) -> date | None:
    """从交易日序列中返回严格晚于 current_date 的最近交易日。"""
    for trade_date in sorted(trade_dates):
        if trade_date > current_date:
            return trade_date
    return None


def is_t1_position_available(
    buy_trade_date: date,
    available_from_date: date,
    current_trade_date: date,
) -> bool:
    """判断 T+1 持仓在当前交易日是否可卖。"""
    return current_trade_date > buy_trade_date and current_trade_date >= available_from_date


class T1TradingEngine:
    """基于 T+1 分析结果执行独立模拟交易。"""

    def __init__(
        self,
        settings: AppConfig,
        repository: MySQLRepository,
        account_id: str = "default",
    ) -> None:
        """初始化 T+1 交易引擎。"""
        self.settings = settings
        self.repository = repository
        self.account_id = account_id
        self.adjust_type = getattr(settings, "adjust_type", getattr(settings, "default_adjust_type", "qfq"))
        self.initial_cash = _to_decimal(
            getattr(settings, "t1_simulation_initial_cash", None)
            or getattr(settings, "simulation_initial_cash", None)
            or 100000
        )
        self.max_positions = int(
            getattr(settings, "t1_simulation_max_positions", None)
            or getattr(settings, "simulation_max_positions", None)
            or 5
        )
        self.order_cash_pct = _to_decimal(
            getattr(settings, "t1_simulation_order_cash_pct", None)
            or getattr(settings, "simulation_order_cash_pct", None)
            or "0.20"
        )
        self.fee_rate = _to_decimal(
            getattr(settings, "t1_simulation_fee_rate", None)
            or getattr(settings, "simulation_fee_rate", None)
            or "0.0003"
        )
        self.stop_loss_pct = _to_decimal(getattr(settings, "t1_stop_loss_pct", "0.06"))
        self.take_profit_pct = _to_decimal(getattr(settings, "t1_take_profit_pct", "0.10"))
        self.market_data = AkshareMarketData(daily_provider=getattr(settings, "daily_provider", "sina"))

    def reset_account(self, initial_cash: float | Decimal | None = None) -> dict[str, Any]:
        """重置 T+1 模拟账户、持仓、订单和成交。"""
        cash = _to_decimal(initial_cash if initial_cash is not None else self.initial_cash)
        self.repository.ensure_schema()
        with self.repository.connection() as conn:
            self._ensure_schema(conn)
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM t1_simulation_trade WHERE account_id=%s", (self.account_id,))
                cursor.execute("DELETE FROM t1_simulation_order WHERE account_id=%s", (self.account_id,))
                cursor.execute("DELETE FROM t1_simulation_position WHERE account_id=%s", (self.account_id,))
                cursor.execute("DELETE FROM t1_simulation_account WHERE account_id=%s", (self.account_id,))
                cursor.execute(
                    """
                    INSERT INTO t1_simulation_account
                        (account_id, initial_cash, cash, market_value, total_asset, realized_pnl)
                    VALUES (%s, %s, %s, 0, %s, 0)
                    """,
                    (self.account_id, cash, cash, cash),
                )
            conn.commit()
        return self.dashboard()["account"]

    def dashboard(self, final_limit: int = 20) -> dict[str, Any]:
        """读取 T+1 专属页面需要的账户、持仓、订单和候选数据。"""
        self.repository.ensure_schema()
        snapshot_time = self._now()
        market_status = self._market_status(snapshot_time)
        use_realtime_quotes = bool(market_status.get("marketOpen"))
        quotes: list[dict[str, Any]] = []
        quote_map: dict[str, dict[str, Any]] = {}
        price_source = "daily_fallback"
        with self.repository.connection() as conn:
            self._ensure_schema(conn)
            account = self._ensure_account(conn)
            analysis_date = self._latest_analysis_date(conn)
            trade_date = self._default_trade_date(conn, analysis_date)
            latest_trade_date = self._latest_trade_date(conn)
            valuation_trade_date = trade_date or latest_trade_date or snapshot_time.date()
            candidate_trade_date = trade_date or latest_trade_date
            picks = self._load_candidates(conn, analysis_date, candidate_trade_date, final_limit) if analysis_date and candidate_trade_date else []
            quote_symbols = self._dashboard_quote_symbols(conn, picks)
            if use_realtime_quotes and quote_symbols:
                quotes = self._fetch_realtime_quotes(snapshot_time, quote_symbols)
                quote_map = {str(item["symbol"]): item for item in quotes}
            if quote_map:
                self._refresh_positions_with_quotes(conn, snapshot_time.date(), quote_map)
                price_source = "realtime"
            else:
                self._refresh_position_availability(conn, valuation_trade_date)
                price_source = "last_known"
            self._recalculate_account(conn)
            account = self._ensure_account(conn)
            conn.commit()
            positions = self._load_positions(conn)
            orders = self._load_orders(conn)
            if quote_map:
                picks = self._apply_realtime_quotes_to_candidates(picks, quote_map)
        market_status.update(
            {
                "quoteCount": 0,
                "quoteTime": _datetime_to_str(snapshot_time) if quotes else None,
                "priceSource": price_source,
            }
        )
        return {
            "account": account,
            "positions": positions,
            "orders": orders,
            "picks": picks,
            "analysisDate": _date_to_str(analysis_date),
            "tradeDate": _date_to_str(trade_date),
            "summary": {
                "candidateCount": len(picks),
                "positionCount": len(positions),
                "unavailablePositionCount": sum(1 for item in positions if int(item.get("availableQuantity") or 0) <= 0),
            },
            "marketStatus": market_status,
        }

    def run_once(self, execute_trades: bool = True, final_limit: int = 20) -> T1TradingRunResult:
        """执行一次 T+1 模拟交易。"""
        self.repository.ensure_schema()
        with self.repository.connection() as conn:
            self._ensure_schema(conn)
            account = self._ensure_account(conn)
            analysis_date = self._latest_analysis_date(conn)
            if not analysis_date:
                conn.commit()
                return T1TradingRunResult(
                    analysis_date=None,
                    trade_date=None,
                    candidate_count=0,
                    buy_count=0,
                    sell_count=0,
                    order_count=0,
                    skipped=True,
                    skip_reason="尚未生成 T+1 分析结果",
                )

            trade_date = self._default_trade_date(conn, analysis_date)
            if not trade_date:
                conn.commit()
                return T1TradingRunResult(
                    analysis_date=analysis_date,
                    trade_date=None,
                    candidate_count=0,
                    buy_count=0,
                    sell_count=0,
                    order_count=0,
                    skipped=True,
                    skip_reason="尚无晚于分析日的 T+1 交易日行情",
                )

            candidates = self._load_candidates(conn, analysis_date, trade_date, final_limit)
            self._refresh_positions(conn, trade_date)
            if not execute_trades:
                conn.commit()
                return T1TradingRunResult(analysis_date, trade_date, len(candidates), 0, 0, 0)

            sell_count = self._execute_sells(conn, account, analysis_date, trade_date)
            buy_count = self._execute_buys(conn, account, analysis_date, trade_date, candidates)
            self._recalculate_account(conn)
            conn.commit()
            return T1TradingRunResult(
                analysis_date=analysis_date,
                trade_date=trade_date,
                candidate_count=len(candidates),
                buy_count=buy_count,
                sell_count=sell_count,
                order_count=buy_count + sell_count,
            )

    def execute_quality_buys(
        self,
        trade_date: date,
        candidates: Sequence[dict[str, Any]],
        execute_trades: bool = True,
    ) -> int:
        """执行 14:05 T+1 质量候选买入，返回真实买入数量。"""
        self.repository.ensure_schema()
        with self.repository.connection() as conn:
            self._ensure_schema(conn)
            account = self._ensure_account(conn)
            self._refresh_positions(conn, trade_date)
            if not execute_trades:
                conn.commit()
                return 0
            normalized_candidates = [
                self._quality_candidate_to_dict(candidate) for candidate in candidates
            ]
            buy_count = self._execute_buys(
                conn,
                account,
                trade_date,
                trade_date,
                [candidate for candidate in normalized_candidates if candidate["price"] > 0],
            )
            self._recalculate_account(conn)
            conn.commit()
            return buy_count

    def _quality_candidate_to_dict(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """将盘中质量候选转换为 T+1 买入候选格式。"""
        return {
            "symbol": str(candidate.get("symbol") or ""),
            "name": str(candidate.get("name") or ""),
            "price": _to_decimal(candidate.get("price") or candidate.get("latestPrice")),
            "finalScore": candidate.get("finalScore"),
            "reason": candidate.get("reason"),
            "risk": candidate.get("risk"),
        }

    def run_realtime_once(self, execute_trades: bool = True, final_limit: int = 20) -> T1TradingRunResult:
        """按实时行情执行一次 T+1 模拟交易。"""
        self.repository.ensure_schema()
        snapshot_time = self._now()
        market_status = self._market_status(snapshot_time)
        market_session = str(market_status.get("session") or "")
        if not market_status.get("marketOpen"):
            return T1TradingRunResult(
                analysis_date=None,
                trade_date=snapshot_time.date(),
                candidate_count=0,
                buy_count=0,
                sell_count=0,
                order_count=0,
                quote_count=0,
                realtime=True,
                market_open=False,
                market_session=market_session,
                skipped=True,
                skip_reason=str(market_status.get("reason") or "非开盘时间"),
            )

        with self.repository.connection() as conn:
            self._ensure_schema(conn)
            account = self._ensure_account(conn)
            analysis_date = self._latest_analysis_date(conn)
            if not analysis_date:
                conn.commit()
                return T1TradingRunResult(
                    None,
                    snapshot_time.date(),
                    0,
                    0,
                    0,
                    0,
                    0,
                    True,
                    True,
                    market_session,
                    True,
                    "尚未生成 T+1 分析结果",
                )

            trade_date = snapshot_time.date()
            fallback_trade_date = self._default_trade_date(conn, analysis_date) or self._latest_trade_date(conn)
            candidates = self._load_candidates(conn, analysis_date, fallback_trade_date, final_limit)
            quote_symbols = self._realtime_quote_symbols(conn, candidates)
            quotes = self._fetch_realtime_quotes(snapshot_time, quote_symbols)
            quote_map = {str(item["symbol"]): item for item in quotes}
            if not quote_map:
                conn.commit()
                return T1TradingRunResult(
                    analysis_date=analysis_date,
                    trade_date=trade_date,
                    candidate_count=len(candidates),
                    buy_count=0,
                    sell_count=0,
                    order_count=0,
                    quote_count=0,
                    realtime=True,
                    market_open=True,
                    market_session=market_session,
                    skipped=True,
                    skip_reason="实时行情为空，已跳过本轮",
                )
            candidates = self._apply_realtime_quotes_to_candidates(candidates, quote_map)
            realtime_candidates = [item for item in candidates if item.get("quoteSource") == "realtime"]
            self._refresh_positions_with_quotes(conn, trade_date, quote_map)
            if not execute_trades:
                conn.commit()
                return T1TradingRunResult(
                    analysis_date=analysis_date,
                    trade_date=trade_date,
                    candidate_count=len(candidates),
                    buy_count=0,
                    sell_count=0,
                    order_count=0,
                    quote_count=len(quotes),
                    realtime=True,
                    market_open=True,
                    market_session=market_session,
                )

            sell_count = self._execute_sells(conn, account, analysis_date, trade_date)
            buy_count = self._execute_buys(conn, account, analysis_date, trade_date, realtime_candidates)
            self._recalculate_account(conn)
            conn.commit()
            return T1TradingRunResult(
                analysis_date=analysis_date,
                trade_date=trade_date,
                candidate_count=len(candidates),
                buy_count=buy_count,
                sell_count=sell_count,
                order_count=buy_count + sell_count,
                quote_count=len(quotes),
                realtime=True,
                market_open=True,
                market_session=market_session,
            )

    def _ensure_schema(self, conn: Any) -> None:
        """创建 T+1 模拟交易独立表。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t1_simulation_account (
                    account_id VARCHAR(64) PRIMARY KEY,
                    initial_cash DECIMAL(18,2) NOT NULL DEFAULT 100000.00,
                    cash DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    market_value DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    total_asset DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    realized_pnl DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t1_simulation_position (
                    account_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    quantity INT NOT NULL,
                    available_quantity INT NOT NULL DEFAULT 0,
                    avg_cost DECIMAL(18,4) NOT NULL,
                    last_price DECIMAL(18,4) NOT NULL,
                    market_value DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    floating_pnl DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    buy_trade_date DATE NOT NULL,
                    available_from_date DATE NOT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (account_id, symbol),
                    INDEX idx_t1_position_available (available_from_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t1_simulation_order (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    account_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    side VARCHAR(8) NOT NULL,
                    order_price DECIMAL(18,4) NOT NULL,
                    quantity INT NOT NULL,
                    amount DECIMAL(18,2) NOT NULL,
                    fee DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    status VARCHAR(16) NOT NULL DEFAULT 'FILLED',
                    reason VARCHAR(512) NULL,
                    signal_analysis_date DATE NULL,
                    trade_date DATE NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_t1_order_account_time (account_id, created_at),
                    INDEX idx_t1_order_signal (signal_analysis_date, symbol)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS t1_simulation_trade (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    account_id VARCHAR(64) NOT NULL,
                    order_id BIGINT NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    side VARCHAR(8) NOT NULL,
                    trade_price DECIMAL(18,4) NOT NULL,
                    quantity INT NOT NULL,
                    amount DECIMAL(18,2) NOT NULL,
                    fee DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    realized_pnl DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    trade_date DATE NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_t1_trade_account_time (account_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )

    def _ensure_account(self, conn: Any) -> dict[str, Any]:
        """读取账户；不存在时自动创建默认账户。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM t1_simulation_account WHERE account_id=%s", (self.account_id,))
            row = cursor.fetchone()
            if not row:
                cursor.execute(
                    """
                    INSERT INTO t1_simulation_account
                        (account_id, initial_cash, cash, market_value, total_asset, realized_pnl)
                    VALUES (%s, %s, %s, 0, %s, 0)
                    """,
                    (self.account_id, self.initial_cash, self.initial_cash, self.initial_cash),
                )
                cursor.execute("SELECT * FROM t1_simulation_account WHERE account_id=%s", (self.account_id,))
                row = cursor.fetchone()
        return self._account_to_dict(row)

    def _latest_analysis_date(self, conn: Any) -> date | None:
        """读取最新 T+1 分析日期。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT MAX(analysis_date) AS analysis_date FROM stock_analysis_pick")
            row = cursor.fetchone() or {}
        return _to_date(row.get("analysis_date"))

    def _latest_trade_date(self, conn: Any) -> date | None:
        """读取数据库中最新完整交易日。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT trade_date
                FROM stock_daily
                WHERE adjust_type=%s
                GROUP BY trade_date
                HAVING COUNT(*) >= 100
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (self.adjust_type,),
            )
            row = cursor.fetchone() or {}
        return _to_date(row.get("trade_date"))

    def _default_trade_date(self, conn: Any, analysis_date: date | None) -> date | None:
        """计算 T+1 交易日，必须晚于分析基准日。"""
        if not analysis_date:
            return None
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT trade_date
                FROM stock_daily
                WHERE adjust_type=%s AND trade_date > %s
                GROUP BY trade_date
                HAVING COUNT(*) >= 100
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                (self.adjust_type, analysis_date),
            )
            row = cursor.fetchone() or {}
        return _to_date(row.get("trade_date"))

    def _next_trade_date(self, conn: Any, trade_date: date) -> date:
        """读取下一交易日；若数据库暂无下一交易日，则用自然日兜底。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT trade_date
                FROM stock_daily
                WHERE adjust_type=%s AND trade_date > %s
                GROUP BY trade_date
                HAVING COUNT(*) >= 100
                ORDER BY trade_date ASC
                LIMIT 1
                """,
                (self.adjust_type, trade_date),
            )
            row = cursor.fetchone() or {}
        return _to_date(row.get("trade_date")) or trade_date + timedelta(days=1)

    def _load_candidates(
        self,
        conn: Any,
        analysis_date: date | None,
        trade_date: date | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """读取 T+1 分析候选并绑定交易日价格。"""
        if not analysis_date or not trade_date:
            return []
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    p.rank_no,
                    p.symbol,
                    p.name,
                    p.quant_score,
                    p.llm_score,
                    p.final_score,
                    p.expected_direction,
                    p.reason,
                    p.risk,
                    d.close_price,
                    d.pct_change
                FROM stock_analysis_pick p
                JOIN stock_daily d
                  ON d.symbol = p.symbol
                 AND d.trade_date = %s
                 AND d.adjust_type = %s
                WHERE p.analysis_date = %s
                ORDER BY p.rank_no ASC
                LIMIT %s
                """,
                (trade_date, self.adjust_type, analysis_date, int(limit)),
            )
            rows = cursor.fetchall() or []
        return [self._candidate_to_dict(row) for row in rows]

    def _fetch_realtime_quotes(
        self, snapshot_time: datetime, symbols: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """读取并规范化真实实时行情，可按持仓和候选股定向拉取。"""
        try:
            raw_quotes = self.market_data.fetch_realtime_quotes(symbols)
        except Exception:
            raw_quotes = []
        quotes: list[dict[str, Any]] = []
        for quote in raw_quotes:
            symbol = str(quote.get("symbol") or "").zfill(6)
            price = _to_decimal(quote.get("latest_price"))
            if not symbol or price <= 0:
                continue
            item = dict(quote)
            item["symbol"] = symbol
            item["latest_price"] = price
            item["snapshot_time"] = snapshot_time
            quotes.append(item)
        return quotes

    def _apply_realtime_quotes_to_candidates(
        self,
        candidates: Sequence[dict[str, Any]],
        quote_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """用实时行情覆盖 T+1 候选的交易价格。"""
        enriched: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            quote = quote_map.get(str(item["symbol"]))
            if quote:
                item["price"] = float(_to_decimal(quote.get("latest_price")))
                item["pctChange"] = _float_or_none(quote.get("pct_change"))
                item["quoteSource"] = "realtime"
            else:
                item["quoteSource"] = "daily_fallback"
            enriched.append(item)
        return enriched

    def _refresh_positions(self, conn: Any, trade_date: date) -> None:
        """用交易日收盘价刷新持仓市值和可卖数量。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM t1_simulation_position WHERE account_id=%s", (self.account_id,))
            positions = cursor.fetchall() or []
        if not positions:
            return
        prices = self._load_prices(conn, [row["symbol"] for row in positions], trade_date)
        with conn.cursor() as cursor:
            for row in positions:
                price = prices.get(row["symbol"], _to_decimal(row["last_price"]))
                quantity = int(row["quantity"])
                market_value = (price * Decimal(quantity)).quantize(Decimal("0.01"))
                floating_pnl = (market_value - _to_decimal(row["avg_cost"]) * Decimal(quantity)).quantize(Decimal("0.01"))
                available = quantity if is_t1_position_available(
                    _to_date(row["buy_trade_date"]) or trade_date,
                    _to_date(row["available_from_date"]) or trade_date,
                    trade_date,
                ) else 0
                cursor.execute(
                    """
                    UPDATE t1_simulation_position
                    SET available_quantity=%s, last_price=%s, market_value=%s, floating_pnl=%s
                    WHERE account_id=%s AND symbol=%s
                    """,
                    (available, price, market_value, floating_pnl, self.account_id, row["symbol"]),
                )

    def _refresh_position_availability(self, conn: Any, trade_date: date) -> None:
        """只刷新 T+1 可卖数量，保留已有实时估值价格。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM t1_simulation_position WHERE account_id=%s", (self.account_id,))
            positions = cursor.fetchall() or []
        if not positions:
            return
        with conn.cursor() as cursor:
            for row in positions:
                quantity = int(row["quantity"])
                available = quantity if is_t1_position_available(
                    _to_date(row["buy_trade_date"]) or trade_date,
                    _to_date(row["available_from_date"]) or trade_date,
                    trade_date,
                ) else 0
                cursor.execute(
                    """
                    UPDATE t1_simulation_position
                    SET available_quantity=%s
                    WHERE account_id=%s AND symbol=%s
                    """,
                    (available, self.account_id, row["symbol"]),
                )

    def _refresh_positions_with_quotes(
        self,
        conn: Any,
        trade_date: date,
        quote_map: dict[str, dict[str, Any]],
    ) -> None:
        """用实时行情刷新持仓价格和 T+1 可卖数量。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM t1_simulation_position WHERE account_id=%s", (self.account_id,))
            positions = cursor.fetchall() or []
        if not positions:
            return
        with conn.cursor() as cursor:
            for row in positions:
                quote = quote_map.get(row["symbol"])
                quantity = int(row["quantity"])
                available = quantity if is_t1_position_available(
                    _to_date(row["buy_trade_date"]) or trade_date,
                    _to_date(row["available_from_date"]) or trade_date,
                    trade_date,
                ) else 0
                if not quote:
                    cursor.execute(
                        """
                        UPDATE t1_simulation_position
                        SET available_quantity=%s
                        WHERE account_id=%s AND symbol=%s
                        """,
                        (available, self.account_id, row["symbol"]),
                    )
                    continue
                price = _to_decimal(quote.get("latest_price"))
                market_value = (price * Decimal(quantity)).quantize(Decimal("0.01"))
                floating_pnl = (market_value - _to_decimal(row["avg_cost"]) * Decimal(quantity)).quantize(Decimal("0.01"))
                cursor.execute(
                    """
                    UPDATE t1_simulation_position
                    SET available_quantity=%s, last_price=%s, market_value=%s, floating_pnl=%s
                    WHERE account_id=%s AND symbol=%s
                    """,
                    (available, price, market_value, floating_pnl, self.account_id, row["symbol"]),
                )

    def _execute_sells(self, conn: Any, account: dict[str, Any], analysis_date: date, trade_date: date) -> int:
        """执行 T+1 可卖持仓的止盈止损卖出。"""
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM t1_simulation_position WHERE account_id=%s AND available_quantity > 0",
                (self.account_id,),
            )
            positions = cursor.fetchall() or []
        sell_count = 0
        for row in positions:
            price = _to_decimal(row["last_price"])
            avg_cost = _to_decimal(row["avg_cost"])
            if price <= avg_cost * (Decimal("1") - self.stop_loss_pct):
                reason = "T+1 止损卖出"
            elif price >= avg_cost * (Decimal("1") + self.take_profit_pct):
                reason = "T+1 止盈卖出"
            else:
                continue
            self._sell_position(conn, account, row, price, reason, analysis_date, trade_date)
            sell_count += 1
        return sell_count

    def _execute_buys(
        self,
        conn: Any,
        account: dict[str, Any],
        analysis_date: date,
        trade_date: date,
        candidates: Sequence[dict[str, Any]],
    ) -> int:
        """按 T+1 候选结果买入，买入当日持仓不可卖。"""
        held_symbols = set(self._held_symbols(conn))
        available_slots = max(self.max_positions - len(held_symbols), 0)
        if available_slots <= 0:
            return 0
        buy_count = 0
        for candidate in candidates:
            if buy_count >= available_slots:
                break
            symbol = str(candidate["symbol"])
            if symbol in held_symbols or self._already_bought_signal(conn, analysis_date, symbol):
                continue
            price = _to_decimal(candidate["price"])
            if price <= 0:
                continue
            cash = _to_decimal(account["cash"])
            budget = min(cash * self.order_cash_pct, cash)
            quantity = _lot_quantity(budget, price, self.fee_rate)
            if quantity <= 0:
                continue
            self._buy_candidate(conn, account, candidate, price, quantity, analysis_date, trade_date)
            held_symbols.add(symbol)
            buy_count += 1
        return buy_count

    def _buy_candidate(
        self,
        conn: Any,
        account: dict[str, Any],
        candidate: dict[str, Any],
        price: Decimal,
        quantity: int,
        analysis_date: date,
        trade_date: date,
    ) -> None:
        """买入单个 T+1 候选股票。"""
        gross = (price * Decimal(quantity)).quantize(Decimal("0.01"))
        fee = (gross * self.fee_rate).quantize(Decimal("0.01"))
        cost = gross + fee
        account["cash"] = (_to_decimal(account["cash"]) - cost).quantize(Decimal("0.01"))
        available_from_date = self._next_trade_date(conn, trade_date)
        reason = f"T+1候选买入，综合评分 {candidate.get('finalScore')}"
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t1_simulation_order
                    (account_id, symbol, name, side, order_price, quantity, amount, fee,
                     status, reason, signal_analysis_date, trade_date)
                VALUES (%s, %s, %s, 'BUY', %s, %s, %s, %s, 'FILLED', %s, %s, %s)
                """,
                (
                    self.account_id,
                    candidate["symbol"],
                    candidate["name"],
                    price,
                    quantity,
                    gross,
                    fee,
                    reason,
                    analysis_date,
                    trade_date,
                ),
            )
            order_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO t1_simulation_trade
                    (account_id, order_id, symbol, name, side, trade_price, quantity,
                     amount, fee, realized_pnl, trade_date)
                VALUES (%s, %s, %s, %s, 'BUY', %s, %s, %s, %s, 0, %s)
                """,
                (
                    self.account_id,
                    order_id,
                    candidate["symbol"],
                    candidate["name"],
                    price,
                    quantity,
                    gross,
                    fee,
                    trade_date,
                ),
            )
            cursor.execute(
                """
                INSERT INTO t1_simulation_position
                    (account_id, symbol, name, quantity, available_quantity, avg_cost,
                     last_price, market_value, floating_pnl, buy_trade_date, available_from_date)
                VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self.account_id,
                    candidate["symbol"],
                    candidate["name"],
                    quantity,
                    price,
                    price,
                    gross,
                    -fee,
                    trade_date,
                    available_from_date,
                ),
            )
            cursor.execute(
                "UPDATE t1_simulation_account SET cash=%s WHERE account_id=%s",
                (account["cash"], self.account_id),
            )

    def _sell_position(
        self,
        conn: Any,
        account: dict[str, Any],
        position: dict[str, Any],
        price: Decimal,
        reason: str,
        analysis_date: date,
        trade_date: date,
    ) -> None:
        """卖出一个已满足 T+1 可卖条件的持仓。"""
        quantity = int(position["available_quantity"])
        gross = (price * Decimal(quantity)).quantize(Decimal("0.01"))
        fee = (gross * self.fee_rate).quantize(Decimal("0.01"))
        net = gross - fee
        cost = (_to_decimal(position["avg_cost"]) * Decimal(quantity)).quantize(Decimal("0.01"))
        realized_pnl = (net - cost).quantize(Decimal("0.01"))
        account["cash"] = (_to_decimal(account["cash"]) + net).quantize(Decimal("0.01"))
        account["realizedPnl"] = (_to_decimal(account.get("realizedPnl", 0)) + realized_pnl).quantize(Decimal("0.01"))
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO t1_simulation_order
                    (account_id, symbol, name, side, order_price, quantity, amount, fee,
                     status, reason, signal_analysis_date, trade_date)
                VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, 'FILLED', %s, %s, %s)
                """,
                (
                    self.account_id,
                    position["symbol"],
                    position["name"],
                    price,
                    quantity,
                    gross,
                    fee,
                    reason,
                    analysis_date,
                    trade_date,
                ),
            )
            order_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO t1_simulation_trade
                    (account_id, order_id, symbol, name, side, trade_price, quantity,
                     amount, fee, realized_pnl, trade_date)
                VALUES (%s, %s, %s, %s, 'SELL', %s, %s, %s, %s, %s, %s)
                """,
                (
                    self.account_id,
                    order_id,
                    position["symbol"],
                    position["name"],
                    price,
                    quantity,
                    gross,
                    fee,
                    realized_pnl,
                    trade_date,
                ),
            )
            cursor.execute(
                "DELETE FROM t1_simulation_position WHERE account_id=%s AND symbol=%s",
                (self.account_id, position["symbol"]),
            )
            cursor.execute(
                """
                UPDATE t1_simulation_account
                SET cash=%s, realized_pnl=%s
                WHERE account_id=%s
                """,
                (account["cash"], account["realizedPnl"], self.account_id),
            )

    def _recalculate_account(self, conn: Any) -> None:
        """重新计算账户市值、总资产和累计盈亏。"""
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(market_value), 0) AS market_value FROM t1_simulation_position WHERE account_id=%s",
                (self.account_id,),
            )
            market_row = cursor.fetchone() or {}
            cursor.execute("SELECT * FROM t1_simulation_account WHERE account_id=%s", (self.account_id,))
            account_row = cursor.fetchone() or {}
            market_value = _to_decimal(market_row.get("market_value"))
            cash = _to_decimal(account_row.get("cash"))
            total_asset = (cash + market_value).quantize(Decimal("0.01"))
            cursor.execute(
                """
                UPDATE t1_simulation_account
                SET market_value=%s, total_asset=%s
                WHERE account_id=%s
                """,
                (market_value, total_asset, self.account_id),
            )

    def _load_prices(self, conn: Any, symbols: Sequence[str], trade_date: date) -> dict[str, Decimal]:
        """批量读取指定交易日收盘价。"""
        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        sql = f"""
            SELECT symbol, close_price
            FROM stock_daily
            WHERE adjust_type=%s AND trade_date=%s AND symbol IN ({placeholders})
        """
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.adjust_type, trade_date, *symbols))
            rows = cursor.fetchall() or []
        return {row["symbol"]: _to_decimal(row["close_price"]) for row in rows}

    def _dashboard_quote_symbols(self, conn: Any, picks: Sequence[dict[str, Any]]) -> list[str]:
        """整理看板刷新需要的持仓和候选股代码。"""

        return self._quote_symbols_from(self._held_symbols(conn), picks)

    def _realtime_quote_symbols(self, conn: Any, candidates: Sequence[dict[str, Any]]) -> list[str]:
        """整理实时任务执行需要的持仓和候选股代码。"""

        return self._quote_symbols_from(self._held_symbols(conn), candidates)

    def _quote_symbols_from(
        self, held_symbols: Sequence[str], candidates: Sequence[dict[str, Any]]
    ) -> list[str]:
        """将持仓和候选股代码合并去重，保持稳定顺序便于行情接口批量请求。"""

        symbols: list[str] = []
        seen: set[str] = set()
        for symbol in [*held_symbols, *(str(item.get("symbol") or "") for item in candidates)]:
            digits = "".join(ch for ch in str(symbol) if ch.isdigit())
            normalized = digits[-6:].zfill(6) if digits else ""
            if normalized and normalized not in seen:
                symbols.append(normalized)
                seen.add(normalized)
        return symbols

    def _held_symbols(self, conn: Any) -> list[str]:
        """读取当前持仓股票代码。"""
        with conn.cursor() as cursor:
            cursor.execute("SELECT symbol FROM t1_simulation_position WHERE account_id=%s", (self.account_id,))
            rows = cursor.fetchall() or []
        return [row["symbol"] for row in rows]

    def _already_bought_signal(self, conn: Any, analysis_date: date, symbol: str) -> bool:
        """判断某个分析信号是否已买入，避免重复执行同一 T+1 信号。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM t1_simulation_order
                WHERE account_id=%s AND side='BUY' AND signal_analysis_date=%s AND symbol=%s
                """,
                (self.account_id, analysis_date, symbol),
            )
            row = cursor.fetchone() or {}
        return int(row.get("cnt") or 0) > 0

    def _load_positions(self, conn: Any) -> list[dict[str, Any]]:
        """读取 T+1 当前持仓。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM t1_simulation_position
                WHERE account_id=%s
                ORDER BY market_value DESC
                """,
                (self.account_id,),
            )
            rows = cursor.fetchall() or []
        return [self._position_to_dict(row) for row in rows]

    def _load_orders(self, conn: Any, limit: int = 50) -> list[dict[str, Any]]:
        """读取最近 T+1 模拟订单。"""
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM t1_simulation_order
                WHERE account_id=%s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (self.account_id, int(limit)),
            )
            rows = cursor.fetchall() or []
        return [self._order_to_dict(row) for row in rows]

    def _account_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """将账户行转换为前端字段。"""
        initial_cash = _to_decimal(row.get("initial_cash"))
        total_asset = _to_decimal(row.get("total_asset"))
        return {
            "accountId": row.get("account_id"),
            "initialCash": float(initial_cash),
            "cash": float(_to_decimal(row.get("cash"))),
            "marketValue": float(_to_decimal(row.get("market_value"))),
            "totalAsset": float(total_asset),
            "realizedPnl": float(_to_decimal(row.get("realized_pnl"))),
            "totalPnl": float((total_asset - initial_cash).quantize(Decimal("0.01"))),
            "updatedAt": _datetime_to_str(row.get("updated_at")),
        }

    def _candidate_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """将 T+1 候选行转换为前端字段。"""
        return {
            "rank": int(row.get("rank_no") or 0),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "quantScore": _float_or_none(row.get("quant_score")),
            "llmScore": _float_or_none(row.get("llm_score")),
            "finalScore": _float_or_none(row.get("final_score")),
            "expectedDirection": row.get("expected_direction"),
            "reason": row.get("reason"),
            "risk": row.get("risk"),
            "price": _float_or_none(row.get("close_price")) or 0,
            "pctChange": _float_or_none(row.get("pct_change")),
        }

    def _position_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """将持仓行转换为前端字段。"""
        return {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "quantity": int(row.get("quantity") or 0),
            "availableQuantity": int(row.get("available_quantity") or 0),
            "avgCost": _float_or_none(row.get("avg_cost")) or 0,
            "lastPrice": _float_or_none(row.get("last_price")) or 0,
            "marketValue": _float_or_none(row.get("market_value")) or 0,
            "floatingPnl": _float_or_none(row.get("floating_pnl")) or 0,
            "buyTradeDate": _date_to_str(_to_date(row.get("buy_trade_date"))),
            "availableFromDate": _date_to_str(_to_date(row.get("available_from_date"))),
            "updatedAt": _datetime_to_str(row.get("updated_at")),
        }

    def _order_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        """将订单行转换为前端字段。"""
        return {
            "id": int(row.get("id") or 0),
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "side": row.get("side"),
            "price": _float_or_none(row.get("order_price")) or 0,
            "quantity": int(row.get("quantity") or 0),
            "amount": _float_or_none(row.get("amount")) or 0,
            "fee": _float_or_none(row.get("fee")) or 0,
            "status": row.get("status"),
            "reason": row.get("reason"),
            "analysisDate": _date_to_str(_to_date(row.get("signal_analysis_date"))),
            "tradeDate": _date_to_str(_to_date(row.get("trade_date"))),
            "createdAt": _datetime_to_str(row.get("created_at")),
        }

    def _now(self) -> datetime:
        """返回配置时区下的当前时间。"""
        return datetime.now(ZoneInfo(getattr(self.settings, "timezone", "Asia/Shanghai"))).replace(microsecond=0)

    def _market_status(self, now: datetime | None = None) -> dict[str, Any]:
        """读取 A 股交易时段状态，兼容实时模块不同函数签名。"""
        timezone = getattr(self.settings, "timezone", "Asia/Shanghai")
        try:
            return a_share_market_status(now or self._now(), timezone)
        except TypeError:
            try:
                return a_share_market_status()
            except Exception:
                return {"marketOpen": False, "session": "unknown", "reason": "无法读取交易时段"}


def _to_decimal(value: Any) -> Decimal:
    """把数据库或配置值转换为 Decimal。"""
    if value is None:
        return DECIMAL_ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_date(value: Any) -> date | None:
    """把数据库日期值转换为 date。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _date_to_str(value: date | None) -> str | None:
    """把 date 转换为 ISO 字符串。"""
    return value.isoformat() if value else None


def _datetime_to_str(value: Any) -> str | None:
    """把 datetime 转换为前端可读字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _float_or_none(value: Any) -> float | None:
    """把数字值转换为 float，空值保持 None。"""
    if value is None:
        return None
    return float(value)


def _lot_quantity(budget: Decimal, price: Decimal, fee_rate: Decimal) -> int:
    """按 A 股一手 100 股计算可买数量。"""
    if budget <= 0 or price <= 0:
        return 0
    raw_quantity = (budget / (price * (Decimal("1") + fee_rate))).to_integral_value(rounding=ROUND_DOWN)
    lot_count = (raw_quantity / HUNDRED).to_integral_value(rounding=ROUND_DOWN)
    return int(lot_count * HUNDRED)
