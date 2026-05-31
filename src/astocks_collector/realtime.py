"""实时行情分析和模拟交易引擎。"""

from __future__ import annotations

import logging
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from astocks_collector.config import AppConfig
from astocks_collector.db import MySQLRepository
from astocks_collector.llm_client import LLMClient
from astocks_collector.market_data import AkshareMarketData

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealtimeRunResult:
    """实时分析单次运行摘要。"""

    snapshot_time: datetime
    quote_count: int
    signal_count: int
    buy_count: int
    sell_count: int
    order_count: int
    decision_count: int
    decision_mode: str
    llm_used: bool
    llm_fallback: bool
    market_open: bool
    skipped: bool
    skip_reason: str
    market_session: str


class RealtimeTradingEngine:
    """执行实时行情入库、信号计算和模拟成交。"""

    account_id = "default"

    def __init__(
        self,
        config: AppConfig,
        repository: MySQLRepository | None = None,
        market_data: AkshareMarketData | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        """初始化实时分析依赖。"""

        self.config = config
        self.repository = repository or MySQLRepository(config)
        self.market_data = market_data or AkshareMarketData(
            daily_provider=config.daily_provider
        )
        self.llm_client = llm_client or LLMClient(
            base_url=config.llm_base_url,
            model=config.llm_model,
            api_key=config.llm_api_key,
            timeout_seconds=config.llm_timeout_seconds,
        )

    def reset_account(self, initial_cash: float | None = None) -> dict[str, Any]:
        """重置默认模拟账户并返回新账户状态。"""

        self.repository.ensure_schema()
        self.repository.reset_simulation(initial_cash or self.config.simulation_initial_cash)
        return self.dashboard()["account"]

    def run_once(
        self,
        limit: int | None = None,
        execute_trades: bool = True,
        decision_mode: str | None = None,
    ) -> RealtimeRunResult:
        """运行一次实时分析，可选择是否执行模拟交易。"""

        self.repository.ensure_schema()
        resolved_decision_mode = _normalize_decision_mode(
            decision_mode or self.config.realtime_decision_mode
        )
        snapshot_time = self._now()
        market_status = a_share_market_status(snapshot_time, self.config.timezone)
        if not market_status["marketOpen"]:
            return RealtimeRunResult(
                snapshot_time=snapshot_time,
                quote_count=0,
                signal_count=0,
                buy_count=0,
                sell_count=0,
                order_count=0,
                decision_count=0,
                decision_mode=resolved_decision_mode,
                llm_used=False,
                llm_fallback=False,
                market_open=False,
                skipped=True,
                skip_reason=str(market_status["reason"]),
                market_session=str(market_status["session"]),
            )

        quote_limit = limit or self.config.realtime_quote_limit
        quotes = self._fetch_quotes(snapshot_time, quote_limit)
        self.repository.upsert_realtime_quotes(quotes)

        positions = self._load_positions()
        metrics = self._load_history_metrics([quote["symbol"] for quote in quotes])
        signals = self._build_signals(
            quotes=quotes,
            metrics=metrics,
            positions=positions,
            snapshot_time=snapshot_time,
        )
        decision_records: list[dict[str, Any]]
        llm_used = False
        llm_fallback = False
        if resolved_decision_mode == "llm_review":
            account = self._load_account_snapshot()
            signals, decision_records, llm_used, llm_fallback = self._review_signals_with_llm(
                signals=signals,
                positions=positions,
                account=account,
                decision_time=snapshot_time,
            )
        else:
            decision_records = self._build_rule_decision_records(
                signals=signals,
                decision_mode=resolved_decision_mode,
                decision_time=snapshot_time,
            )
        signal_count = self.repository.insert_realtime_signals(signals)
        decision_count = self.repository.insert_realtime_decisions(decision_records)

        order_count = 0
        if execute_trades:
            order_count = self._execute_simulation(signals)

        buy_count = sum(1 for item in signals if item["signal_action"] == "BUY")
        sell_count = sum(1 for item in signals if item["signal_action"] == "SELL")
        return RealtimeRunResult(
            snapshot_time=snapshot_time,
            quote_count=len(quotes),
            signal_count=signal_count,
            buy_count=buy_count,
            sell_count=sell_count,
            order_count=order_count,
            decision_count=decision_count,
            decision_mode=resolved_decision_mode,
            llm_used=llm_used,
            llm_fallback=llm_fallback,
            market_open=True,
            skipped=False,
            skip_reason="",
            market_session=str(market_status["session"]),
        )

    def _now(self) -> datetime:
        """返回配置时区下的当前时间。"""

        return datetime.now(ZoneInfo(self.config.timezone)).replace(microsecond=0)

    def run_loop(
        self,
        interval_seconds: int,
        limit: int | None = None,
        decision_mode: str | None = None,
    ) -> None:
        """按固定间隔持续运行实时分析和模拟交易。"""

        if interval_seconds <= 0:
            raise ValueError("循环间隔必须是正整数秒")
        while True:
            result = self.run_once(
                limit=limit,
                execute_trades=True,
                decision_mode=decision_mode,
            )
            logger.info(
                "实时分析完成: 行情=%s 信号=%s 决策=%s 买=%s 卖=%s 订单=%s LLM降级=%s",
                result.quote_count,
                result.signal_count,
                result.decision_count,
                result.buy_count,
                result.sell_count,
                result.order_count,
                result.llm_fallback,
            )
            time.sleep(interval_seconds)

    def dashboard(self, signal_limit: int | None = None) -> dict[str, Any]:
        """读取实时分析和模拟交易工作台数据。"""

        self.repository.ensure_schema()
        limit = signal_limit or self.config.realtime_signal_limit
        self._ensure_account()
        self._refresh_account_values()

        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM simulation_account WHERE account_id=%s",
                    (self.account_id,),
                )
                account = cursor.fetchone() or {}
                cursor.execute(
                    """
                    SELECT symbol, name, quantity, available_quantity, avg_cost,
                           last_price, market_value, floating_pnl
                    FROM simulation_position
                    WHERE account_id=%s
                    ORDER BY market_value DESC
                    """,
                    (self.account_id,),
                )
                positions = cursor.fetchall()
                cursor.execute(
                    "SELECT MAX(signal_time) AS signal_time FROM stock_realtime_signal"
                )
                latest_signal_time = (cursor.fetchone() or {}).get("signal_time")
                signals: list[dict[str, Any]] = []
                if latest_signal_time is not None:
                    cursor.execute(
                        """
                        SELECT
                               s.id, s.signal_time, s.symbol, s.name, s.latest_price,
                               s.pct_change, s.trend_score, s.momentum_score,
                               s.liquidity_score, s.risk_score, s.final_score,
                               s.signal_action, s.confidence, s.reason, s.risk,
                               d.decision_source, d.llm_action, d.llm_score,
                               d.llm_reason, d.llm_risk, d.is_fallback AS llm_fallback
                        FROM stock_realtime_signal s
                        LEFT JOIN stock_realtime_decision d
                          ON d.signal_time=s.signal_time AND d.symbol=s.symbol
                        WHERE s.signal_time=%s
                        ORDER BY s.final_score DESC
                        LIMIT %s
                        """,
                        (latest_signal_time, limit),
                    )
                    signals = list(cursor.fetchall())
                cursor.execute(
                    """
                    SELECT id, order_time, symbol, name, side, quantity, price,
                           amount, fee, status, reason
                    FROM simulation_order
                    WHERE account_id=%s
                    ORDER BY order_time DESC, id DESC
                    LIMIT 30
                    """,
                    (self.account_id,),
                )
                orders = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT COUNT(*) AS quote_count, MAX(snapshot_time) AS snapshot_time
                    FROM stock_realtime_quote
                    """
                )
                quote_summary = cursor.fetchone() or {}
                cursor.execute(
                    "SELECT MAX(decision_time) AS decision_time FROM stock_realtime_decision"
                )
                latest_decision_time = (cursor.fetchone() or {}).get("decision_time")
                decision_summary: dict[str, Any] = {
                    "decision_time": latest_decision_time,
                    "decision_count": 0,
                    "llm_count": 0,
                    "fallback_count": 0,
                    "decision_mode": None,
                }
                if latest_decision_time is not None:
                    cursor.execute(
                        """
                        SELECT
                            COUNT(*) AS decision_count,
                            SUM(CASE WHEN decision_source='llm' THEN 1 ELSE 0 END)
                                AS llm_count,
                            SUM(CASE WHEN is_fallback=1 THEN 1 ELSE 0 END)
                                AS fallback_count,
                            MAX(decision_mode) AS decision_mode
                        FROM stock_realtime_decision
                        WHERE decision_time=%s
                        """,
                        (latest_decision_time,),
                    )
                    decision_summary.update(cursor.fetchone() or {})

        return {
            "account": account,
            "positions": positions,
            "signals": signals,
            "orders": orders,
            "latestSignalTime": latest_signal_time,
            "quoteSummary": quote_summary,
            "decisionSummary": decision_summary,
            "marketStatus": a_share_market_status(self._now(), self.config.timezone),
        }

    def _fetch_quotes(
        self, snapshot_time: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """读取实时行情；上游失败时降级为本地最新基础行情。"""

        try:
            quotes = self.market_data.fetch_realtime_quotes()
        except Exception as exc:
            logger.warning("实时行情接口不可用，降级使用 stock_basic 快照: %s", exc)
            quotes = self._load_quotes_from_stock_basic()

        normalized: list[dict[str, Any]] = []
        for quote in quotes:
            price = _decimal_or_none(quote.get("latest_price"))
            symbol = str(quote.get("symbol", "")).zfill(6)
            name = str(quote.get("name") or "")
            if not symbol or price is None or price <= 0 or _is_risk_name(name):
                continue
            item = dict(quote)
            item["symbol"] = symbol
            item["name"] = name
            item["snapshot_time"] = snapshot_time
            normalized.append(item)

        normalized.sort(
            key=lambda item: (
                _float_value(item.get("amount")),
                abs(_float_value(item.get("pct_change"))),
            ),
            reverse=True,
        )
        return normalized[:limit]

    def _load_quotes_from_stock_basic(self) -> list[dict[str, Any]]:
        """从最新完整日线和 stock_basic 构造降级行情快照。"""

        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS stock_count FROM stock_basic")
                stock_count = int((cursor.fetchone() or {}).get("stock_count") or 0)
                min_symbol_count = max(1, int(stock_count * 0.8)) if stock_count else 1
                cursor.execute(
                    """
                    SELECT trade_date
                    FROM stock_daily
                    WHERE adjust_type=%s
                    GROUP BY trade_date
                    HAVING COUNT(DISTINCT symbol) >= %s
                    ORDER BY trade_date DESC
                    LIMIT 1
                    """,
                    (self.config.adjust_type, min_symbol_count),
                )
                trade_date_row = cursor.fetchone()
                trade_date = trade_date_row["trade_date"] if trade_date_row else None
                if trade_date is None:
                    return []
                cursor.execute(
                    """
                    SELECT
                        b.symbol,
                        b.name,
                        b.exchange,
                        d.close_price AS latest_price,
                        d.pct_change,
                        d.change_amount,
                        d.volume,
                        d.amount,
                        d.amplitude,
                        d.high_price,
                        d.low_price,
                        d.open_price,
                        d.pre_close,
                        d.turnover_rate,
                        b.total_market_value,
                        b.circulating_market_value
                    FROM stock_daily d
                    JOIN stock_basic b ON b.symbol=d.symbol
                    WHERE d.trade_date=%s
                      AND d.adjust_type=%s
                      AND b.is_active=1
                      AND d.close_price IS NOT NULL
                    ORDER BY d.amount DESC
                    """,
                    (trade_date, self.config.adjust_type),
                )
                rows = cursor.fetchall()

        quotes: list[dict[str, Any]] = []
        for row in rows:
            quotes.append(
                {
                    "symbol": row["symbol"],
                    "name": row["name"],
                    "exchange": row["exchange"],
                    "latest_price": row["latest_price"],
                    "pct_change": row["pct_change"],
                    "change_amount": None,
                    "volume": None,
                    "amount": None,
                    "amplitude": None,
                    "high_price": None,
                    "low_price": None,
                    "open_price": None,
                    "pre_close": None,
                    "volume_ratio": 1,
                    "turnover_rate": row["turnover_rate"],
                    "pe_dynamic": None,
                    "pb": None,
                    "total_market_value": row["total_market_value"],
                    "circulating_market_value": row["circulating_market_value"],
                    "source": f"stock_daily.fallback.{trade_date}",
                }
            )
        return quotes

    def _load_history_metrics(self, symbols: list[str]) -> dict[str, dict[str, float]]:
        """读取最近日线并计算短周期趋势指标。"""

        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        sql = f"""
            SELECT symbol, trade_date, close_price, volume, pct_change
            FROM stock_daily
            WHERE adjust_type=%s
              AND symbol IN ({placeholders})
              AND trade_date >= DATE_SUB(
                  (SELECT MAX(trade_date) FROM stock_daily WHERE adjust_type=%s),
                  INTERVAL 80 DAY
              )
            ORDER BY symbol, trade_date
        """
        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, [self.config.adjust_type, *symbols, self.config.adjust_type])
                rows = cursor.fetchall()

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["symbol"]), []).append(row)

        metrics: dict[str, dict[str, float]] = {}
        for symbol, items in grouped.items():
            closes = [_float_value(item["close_price"]) for item in items if item["close_price"]]
            volumes = [_float_value(item["volume"]) for item in items if item["volume"]]
            if len(closes) < 20:
                continue
            metrics[symbol] = {
                "ma5": _mean(closes[-5:]),
                "ma20": _mean(closes[-20:]),
                "ret5": _pct_return(closes, 5),
                "ret20": _pct_return(closes, 20),
                "volume_ratio": _mean(volumes[-5:]) / _mean(volumes[-20:])
                if len(volumes) >= 20 and _mean(volumes[-20:]) > 0
                else 1.0,
            }
        return metrics

    def _build_signals(
        self,
        quotes: list[dict[str, Any]],
        metrics: dict[str, dict[str, float]],
        positions: dict[str, dict[str, Any]],
        snapshot_time: datetime,
    ) -> list[dict[str, Any]]:
        """基于实时行情和历史趋势生成交易信号。"""

        signals: list[dict[str, Any]] = []
        for quote in quotes:
            symbol = quote["symbol"]
            price = _float_value(quote.get("latest_price"))
            if price <= 0:
                continue
            pct_change = _float_value(quote.get("pct_change"))
            turnover = _float_value(quote.get("turnover_rate"))
            amount = _float_value(quote.get("amount"))
            amplitude = _float_value(quote.get("amplitude"))
            volume_ratio = _float_value(quote.get("volume_ratio")) or metrics.get(symbol, {}).get(
                "volume_ratio", 1.0
            )
            history = metrics.get(symbol, {})

            trend_score = _score_trend(price, history)
            momentum_score = _score_momentum(pct_change, volume_ratio)
            liquidity_score = _score_liquidity(amount, turnover)
            risk_score = _score_risk(pct_change, amplitude, quote["name"])
            final_score = (
                trend_score * 0.32
                + momentum_score * 0.28
                + liquidity_score * 0.18
                + risk_score * 0.22
            )
            action = _resolve_action(symbol, price, pct_change, final_score, positions)
            signals.append(
                {
                    "signal_time": snapshot_time,
                    "symbol": symbol,
                    "name": quote["name"],
                    "latest_price": round(price, 4),
                    "pct_change": round(pct_change, 4),
                    "trend_score": round(trend_score, 4),
                    "momentum_score": round(momentum_score, 4),
                    "liquidity_score": round(liquidity_score, 4),
                    "risk_score": round(risk_score, 4),
                    "final_score": round(final_score, 4),
                    "signal_action": action,
                    "confidence": round(final_score, 4),
                    "reason": _build_signal_reason(history, pct_change, volume_ratio, turnover),
                    "risk": _build_signal_risk(pct_change, amplitude, risk_score),
                    "quote_snapshot": {
                        "source": quote.get("source"),
                        "amount": str(quote.get("amount")),
                        "turnover_rate": str(quote.get("turnover_rate")),
                        "volume_ratio": str(quote.get("volume_ratio")),
                    },
                }
            )

        signals.sort(key=lambda item: item["final_score"], reverse=True)
        return signals[: self.config.realtime_signal_limit]

    def _execute_simulation(self, signals: list[dict[str, Any]]) -> int:
        """根据实时信号撮合模拟买卖。"""

        if not signals:
            self._ensure_account()
            return 0

        self._ensure_account()
        order_count = 0
        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                account = self._select_account(cursor, for_update=True)
                positions = self._select_positions(cursor, for_update=True)

                for signal in signals:
                    if signal["signal_action"] == "SELL" and signal["symbol"] in positions:
                        if self._sell_position(cursor, account, positions, signal):
                            order_count += 1

                for signal in signals:
                    if signal["signal_action"] != "BUY" or signal["symbol"] in positions:
                        continue
                    if len(positions) >= self.config.simulation_max_positions:
                        break
                    if self._buy_position(cursor, account, positions, signal):
                        order_count += 1

                self._refresh_account_values(cursor, account, positions)
            conn.commit()
        return order_count

    def _ensure_account(self) -> None:
        """确保默认模拟账户存在。"""

        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT account_id FROM simulation_account WHERE account_id=%s",
                    (self.account_id,),
                )
                if cursor.fetchone():
                    return
                cursor.execute(
                    """
                    INSERT INTO simulation_account (
                        account_id, initial_cash, cash, market_value, total_asset,
                        realized_pnl
                    )
                    VALUES (%s, %s, %s, 0, %s, 0)
                    """,
                    (
                        self.account_id,
                        self.config.simulation_initial_cash,
                        self.config.simulation_initial_cash,
                        self.config.simulation_initial_cash,
                    ),
                )
            conn.commit()

    def _load_positions(self) -> dict[str, dict[str, Any]]:
        """读取当前模拟持仓。"""

        self._ensure_account()
        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                positions = self._select_positions(cursor, for_update=False)
        return positions

    def _load_account_snapshot(self) -> dict[str, Any]:
        """读取当前模拟账户快照，供 LLM 决策了解资金约束。"""

        self._ensure_account()
        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                return self._select_account(cursor, for_update=False)

    def _review_signals_with_llm(
        self,
        signals: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        account: dict[str, Any],
        decision_time: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, bool]:
        """用大模型复核规则信号，失败时降级为规则决策。"""

        candidates = self._select_llm_candidates(signals)
        if not candidates:
            return signals, [], False, False

        try:
            payload = self._ask_llm_for_realtime_decisions(
                candidates=candidates,
                positions=positions,
                account=account,
            )
            decisions = self._normalize_llm_decisions(payload)
        except Exception as exc:
            logger.warning("实时交易 LLM 决策不可用，降级为规则引擎: %s", exc)
            return (
                signals,
                self._build_rule_decision_records(
                    signals=candidates,
                    decision_mode="llm_review",
                    decision_time=decision_time,
                    fallback_reason=f"LLM不可用，已降级规则: {exc}",
                    is_fallback=True,
                ),
                False,
                True,
            )

        decision_map = {item["symbol"]: item for item in decisions}
        candidate_symbols = {item["symbol"] for item in candidates}
        reviewed: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        has_fallback = False
        for signal in signals:
            item = dict(signal)
            if item["symbol"] not in candidate_symbols:
                reviewed.append(item)
                continue

            rule_action = str(signal["signal_action"])
            decision = decision_map.get(item["symbol"])
            if not decision:
                has_fallback = True
                records.extend(
                    self._build_rule_decision_records(
                        signals=[signal],
                        decision_mode="llm_review",
                        decision_time=decision_time,
                        fallback_reason="LLM未返回该股票决策，保留规则动作",
                        is_fallback=True,
                    )
                )
                reviewed.append(item)
                continue

            llm_action = _normalize_action(decision.get("action") or decision.get("llm_action"))
            llm_score = _clamp(
                _float_value(decision.get("score") or decision.get("llm_score")),
                0,
                100,
            )
            if llm_score <= 0:
                llm_score = _float_value(signal.get("final_score"))
            final_action = self._bounded_llm_action(signal, llm_action, positions)
            llm_reason = str(decision.get("reason") or "LLM未给出理由")[:500]
            llm_risk = str(decision.get("risk") or "注意盘中波动和模型误差")[:500]

            item["signal_action"] = final_action
            item["confidence"] = round(llm_score, 4)
            item["reason"] = _merge_llm_text(signal["reason"], llm_reason, "LLM")
            item["risk"] = _merge_llm_text(signal["risk"], llm_risk, "LLM风险")
            reviewed.append(item)
            records.append(
                _build_decision_record(
                    signal=signal,
                    decision_time=decision_time,
                    decision_mode="llm_review",
                    decision_source="llm",
                    rule_action=rule_action,
                    llm_action=llm_action,
                    final_action=final_action,
                    llm_score=llm_score,
                    llm_reason=llm_reason,
                    llm_risk=llm_risk,
                    is_fallback=False,
                    raw_response=decision,
                )
            )

        return reviewed, records, True, has_fallback

    def _ask_llm_for_realtime_decisions(
        self,
        candidates: list[dict[str, Any]],
        positions: dict[str, dict[str, Any]],
        account: dict[str, Any],
    ) -> Any:
        """请求大模型对实时候选信号输出结构化买卖决策。"""

        candidate_payload = [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "rule_action": item["signal_action"],
                "latest_price": item["latest_price"],
                "pct_change": item["pct_change"],
                "final_score": item["final_score"],
                "trend_score": item["trend_score"],
                "momentum_score": item["momentum_score"],
                "liquidity_score": item["liquidity_score"],
                "risk_score": item["risk_score"],
                "reason": item["reason"],
                "risk": item["risk"],
            }
            for item in candidates
        ]
        position_payload = [
            {
                "symbol": row["symbol"],
                "name": row.get("name"),
                "quantity": int(row.get("quantity") or 0),
                "avg_cost": _float_value(row.get("avg_cost")),
                "last_price": _float_value(row.get("last_price")),
            }
            for row in positions.values()
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是A股模拟交易风控助手。只能基于用户给出的候选信号、账户和持仓做决策，"
                    "不得编造候选池之外的股票。输出必须是JSON对象，字段decisions为数组。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请对实时候选信号做复核。只能输出BUY、SELL、WATCH或HOLD；"
                    "不要突破最大持仓、单笔预算、风险分和已有持仓约束。"
                    "\n输入JSON: "
                    + json.dumps(
                        self._realtime_decision_payload(
                            candidate_payload, position_payload, account
                        ),
                        ensure_ascii=False,
                    )
                ),
            },
        ]
        return self.llm_client.chat_json(messages, max_tokens=4096)

    def _realtime_decision_payload(
        self,
        candidates: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        account: dict[str, Any],
    ) -> dict[str, Any]:
        """组织发送给 LLM 的实时决策上下文。"""

        return {
            "task": "复核实时模拟交易候选，输出最终动作",
            "account": {
                "cash": _float_value(account.get("cash")),
                "total_asset": _float_value(account.get("total_asset")),
                "max_positions": self.config.simulation_max_positions,
                "order_cash_pct": self.config.simulation_order_cash_pct,
                "fee_rate": self.config.simulation_fee_rate,
            },
            "positions": positions,
            "rules": [
                "只能处理candidates中的股票",
                "无持仓股票不能输出SELL",
                "已有持仓股票不能输出BUY",
                "BUY必须同时具备正涨幅、较高综合评分和可控风险",
                "输出reason和risk均控制在30字以内",
            ],
            "output_schema": {
                "decisions": [
                    {
                        "symbol": "股票代码",
                        "action": "BUY|SELL|WATCH|HOLD",
                        "score": 0,
                        "reason": "决策理由",
                        "risk": "主要风险",
                    }
                ]
            },
            "candidates": candidates,
        }

    def _normalize_llm_decisions(self, payload: Any) -> list[dict[str, Any]]:
        """标准化 LLM 返回的实时决策数组。"""

        if isinstance(payload, dict):
            payload = payload.get("decisions") or payload.get("data") or payload.get("picks") or []
        if not isinstance(payload, list):
            raise RuntimeError("LLM 实时决策响应不是数组")

        decisions: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip()
            if not symbol:
                continue
            row = dict(item)
            row["symbol"] = symbol[-6:].zfill(6)
            row["action"] = _normalize_action(row.get("action") or row.get("llm_action"))
            decisions.append(row)
        return decisions

    def _select_llm_candidates(
        self, signals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """从规则信号中选择需要 LLM 复核的高优先级候选。"""

        priority = {"SELL": 0, "BUY": 1, "WATCH": 2, "HOLD": 3}
        candidates = [
            signal
            for signal in signals
            if signal["signal_action"] in {"SELL", "BUY", "WATCH"}
        ]
        candidates.sort(
            key=lambda item: (
                priority.get(str(item["signal_action"]), 9),
                -_float_value(item.get("final_score")),
            )
        )
        return candidates[: self.config.realtime_llm_candidate_limit]

    def _bounded_llm_action(
        self,
        signal: dict[str, Any],
        llm_action: str,
        positions: dict[str, dict[str, Any]],
    ) -> str:
        """将 LLM 动作限制在模拟账户和规则风控边界内。"""

        symbol = signal["symbol"]
        rule_action = str(signal["signal_action"])
        has_position = symbol in positions
        if has_position:
            if llm_action == "SELL" and rule_action == "SELL":
                return "SELL"
            return "HOLD"

        if llm_action == "BUY":
            if (
                _float_value(signal.get("final_score")) >= 60
                and _float_value(signal.get("risk_score")) >= 55
                and _float_value(signal.get("pct_change")) > 0
            ):
                return "BUY"
            return "WATCH"
        if llm_action == "WATCH":
            return "WATCH"
        return "HOLD"

    def _build_rule_decision_records(
        self,
        signals: list[dict[str, Any]],
        decision_mode: str,
        decision_time: datetime | None = None,
        fallback_reason: str | None = None,
        is_fallback: bool = False,
    ) -> list[dict[str, Any]]:
        """根据规则信号生成最终决策记录。"""

        records: list[dict[str, Any]] = []
        reason = fallback_reason or "规则引擎决策"
        for signal in signals:
            records.append(
                _build_decision_record(
                    signal=signal,
                    decision_time=decision_time,
                    decision_mode=decision_mode,
                    decision_source="rules_fallback" if is_fallback else "rules",
                    rule_action=str(signal["signal_action"]),
                    llm_action=str(signal["signal_action"]),
                    final_action=str(signal["signal_action"]),
                    llm_score=_float_value(signal.get("final_score")),
                    llm_reason=reason,
                    llm_risk=str(signal.get("risk") or "注意市场波动")[:500],
                    is_fallback=is_fallback,
                    raw_response={"fallback_reason": reason} if is_fallback else {},
                )
            )
        return records

    def _select_account(self, cursor, for_update: bool) -> dict[str, Any]:
        """读取模拟账户，必要时加行锁。"""

        sql = "SELECT * FROM simulation_account WHERE account_id=%s"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (self.account_id,))
        account = cursor.fetchone()
        if not account:
            raise RuntimeError("模拟账户不存在")
        return account

    def _select_positions(self, cursor, for_update: bool) -> dict[str, dict[str, Any]]:
        """读取模拟持仓，必要时加行锁。"""

        sql = "SELECT * FROM simulation_position WHERE account_id=%s"
        if for_update:
            sql += " FOR UPDATE"
        cursor.execute(sql, (self.account_id,))
        return {str(row["symbol"]): row for row in cursor.fetchall()}

    def _buy_position(
        self,
        cursor,
        account: dict[str, Any],
        positions: dict[str, dict[str, Any]],
        signal: dict[str, Any],
    ) -> bool:
        """按信号买入一手整数倍股票。"""

        cash = _dec(account["cash"])
        price = _dec(signal["latest_price"])
        slots = max(1, self.config.simulation_max_positions - len(positions))
        budget = min(cash * _dec(self.config.simulation_order_cash_pct), cash / slots)
        quantity = int((budget / price) // Decimal("100") * 100)
        if quantity <= 0:
            return False
        amount = price * quantity
        fee = amount * _dec(self.config.simulation_fee_rate)
        if amount + fee > cash:
            return False

        order_id = self._insert_order(cursor, signal, "BUY", quantity, price, amount, fee)
        self._insert_trade(cursor, order_id, signal, "BUY", quantity, price, amount, fee, Decimal("0"))
        cursor.execute(
            """
            INSERT INTO simulation_position (
                account_id, symbol, name, quantity, available_quantity, avg_cost,
                last_price, market_value, floating_pnl
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
            """,
            (
                self.account_id,
                signal["symbol"],
                signal["name"],
                quantity,
                quantity,
                price,
                price,
                amount,
            ),
        )
        account["cash"] = cash - amount - fee
        positions[signal["symbol"]] = {
            "symbol": signal["symbol"],
            "name": signal["name"],
            "quantity": quantity,
            "avg_cost": price,
            "last_price": price,
        }
        return True

    def _sell_position(
        self,
        cursor,
        account: dict[str, Any],
        positions: dict[str, dict[str, Any]],
        signal: dict[str, Any],
    ) -> bool:
        """按信号卖出当前全部持仓。"""

        position = positions[signal["symbol"]]
        quantity = int(position["quantity"])
        if quantity <= 0:
            return False
        price = _dec(signal["latest_price"])
        avg_cost = _dec(position["avg_cost"])
        amount = price * quantity
        fee = amount * _dec(self.config.simulation_fee_rate)
        realized_pnl = (price - avg_cost) * quantity - fee
        order_id = self._insert_order(cursor, signal, "SELL", quantity, price, amount, fee)
        self._insert_trade(
            cursor, order_id, signal, "SELL", quantity, price, amount, fee, realized_pnl
        )
        cursor.execute(
            "DELETE FROM simulation_position WHERE account_id=%s AND symbol=%s",
            (self.account_id, signal["symbol"]),
        )
        account["cash"] = _dec(account["cash"]) + amount - fee
        account["realized_pnl"] = _dec(account["realized_pnl"]) + realized_pnl
        positions.pop(signal["symbol"], None)
        return True

    def _insert_order(
        self,
        cursor,
        signal: dict[str, Any],
        side: str,
        quantity: int,
        price: Decimal,
        amount: Decimal,
        fee: Decimal,
    ) -> int:
        """写入模拟订单并返回订单编号。"""

        cursor.execute(
            """
            INSERT INTO simulation_order (
                account_id, signal_id, order_time, symbol, name, side,
                quantity, price, amount, fee, status, reason
            )
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, 'FILLED', %s)
            """,
            (
                self.account_id,
                signal["signal_time"],
                signal["symbol"],
                signal["name"],
                side,
                quantity,
                price,
                amount,
                fee,
                signal["reason"],
            ),
        )
        return int(cursor.lastrowid)

    def _insert_trade(
        self,
        cursor,
        order_id: int,
        signal: dict[str, Any],
        side: str,
        quantity: int,
        price: Decimal,
        amount: Decimal,
        fee: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        """写入模拟成交明细。"""

        cursor.execute(
            """
            INSERT INTO simulation_trade (
                order_id, account_id, trade_time, symbol, side, quantity,
                price, amount, fee, realized_pnl
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order_id,
                self.account_id,
                signal["signal_time"],
                signal["symbol"],
                side,
                quantity,
                price,
                amount,
                fee,
                realized_pnl,
            ),
        )

    def _refresh_account_values(
        self,
        cursor=None,
        account: dict[str, Any] | None = None,
        positions: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """用最新快照刷新持仓市值和账户总资产。"""

        own_connection = cursor is None
        if own_connection:
            conn_ctx = self.repository.connection()
            conn = conn_ctx.__enter__()
            cursor = conn.cursor()
            account = self._select_account(cursor, for_update=True)
            positions = self._select_positions(cursor, for_update=True)
        assert cursor is not None and account is not None and positions is not None

        symbols = list(positions)
        quote_map: dict[str, Decimal] = {}
        if symbols:
            placeholders = ",".join(["%s"] * len(symbols))
            cursor.execute(
                f"""
                SELECT symbol, latest_price
                FROM stock_realtime_quote
                WHERE symbol IN ({placeholders})
                """,
                symbols,
            )
            quote_map = {
                str(row["symbol"]): _dec(row["latest_price"]) for row in cursor.fetchall()
            }

        market_value = Decimal("0")
        for symbol, position in positions.items():
            price = quote_map.get(symbol, _dec(position["last_price"]))
            quantity = int(position["quantity"])
            avg_cost = _dec(position["avg_cost"])
            value = price * quantity
            floating_pnl = (price - avg_cost) * quantity
            market_value += value
            cursor.execute(
                """
                UPDATE simulation_position
                SET last_price=%s, market_value=%s, floating_pnl=%s
                WHERE account_id=%s AND symbol=%s
                """,
                (price, value, floating_pnl, self.account_id, symbol),
            )

        cash = _dec(account["cash"])
        total_asset = cash + market_value
        cursor.execute(
            """
            UPDATE simulation_account
            SET cash=%s, market_value=%s, total_asset=%s, realized_pnl=%s
            WHERE account_id=%s
            """,
            (
                cash,
                market_value,
                total_asset,
                _dec(account.get("realized_pnl")),
                self.account_id,
            ),
        )
        if own_connection:
            conn.commit()
            conn_ctx.__exit__(None, None, None)


def _normalize_decision_mode(value: str) -> str:
    """标准化实时决策模式。"""

    mode = value.strip().lower()
    aliases = {"rule": "rules", "rules": "rules", "llm": "llm_review", "llm_review": "llm_review"}
    if mode not in aliases:
        raise ValueError("实时决策模式只能是 rules 或 llm_review")
    return aliases[mode]


def a_share_market_status(now: datetime, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    """判断当前是否处于 A 股连续竞价开盘时段。"""

    local_now = now
    zone = ZoneInfo(timezone)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=zone)
    else:
        local_now = local_now.astimezone(zone)

    morning_start = local_now.replace(hour=9, minute=30, second=0, microsecond=0)
    morning_end = local_now.replace(hour=11, minute=30, second=0, microsecond=0)
    afternoon_start = local_now.replace(hour=13, minute=0, second=0, microsecond=0)
    afternoon_end = local_now.replace(hour=15, minute=0, second=0, microsecond=0)
    open_windows = "09:30-11:30、13:00-15:00"

    if local_now.weekday() >= 5:
        return {
            "now": local_now,
            "marketOpen": False,
            "session": "closed",
            "reason": f"非交易日，仅在工作日 {open_windows} 执行实时分析和模拟交易",
        }
    if morning_start <= local_now <= morning_end:
        return {
            "now": local_now,
            "marketOpen": True,
            "session": "morning",
            "reason": "上午开盘时段",
        }
    if afternoon_start <= local_now <= afternoon_end:
        return {
            "now": local_now,
            "marketOpen": True,
            "session": "afternoon",
            "reason": "下午开盘时段",
        }
    if morning_end < local_now < afternoon_start:
        reason = f"午间休市，仅在 {open_windows} 执行实时分析和模拟交易"
    else:
        reason = f"非开盘时间，仅在 {open_windows} 执行实时分析和模拟交易"
    return {
        "now": local_now,
        "marketOpen": False,
        "session": "closed",
        "reason": reason,
    }


def _normalize_action(value: Any) -> str:
    """标准化交易动作枚举。"""

    action = str(value or "").strip().upper()
    return action if action in {"BUY", "SELL", "WATCH", "HOLD"} else "HOLD"


def _build_decision_record(
    signal: dict[str, Any],
    decision_time: datetime | None,
    decision_mode: str,
    decision_source: str,
    rule_action: str,
    llm_action: str,
    final_action: str,
    llm_score: float,
    llm_reason: str,
    llm_risk: str,
    is_fallback: bool,
    raw_response: Any,
) -> dict[str, Any]:
    """构造实时最终决策落库记录。"""

    return {
        "decision_time": decision_time or signal["signal_time"],
        "signal_time": signal["signal_time"],
        "symbol": signal["symbol"],
        "name": signal["name"],
        "latest_price": signal["latest_price"],
        "rule_action": rule_action,
        "llm_action": llm_action,
        "final_action": final_action,
        "rule_score": signal["final_score"],
        "llm_score": round(llm_score, 4),
        "decision_mode": decision_mode,
        "decision_source": decision_source,
        "llm_reason": llm_reason[:1000],
        "llm_risk": llm_risk[:1000],
        "is_fallback": is_fallback,
        "raw_response": raw_response,
    }


def _merge_llm_text(rule_text: str, llm_text: str, label: str) -> str:
    """合并规则说明和 LLM 说明，限制展示长度。"""

    return f"{rule_text}；{label}: {llm_text}"[:1000]


def _score_trend(price: float, history: dict[str, float]) -> float:
    """计算实时价格相对均线和历史收益的趋势分。"""

    ma5 = history.get("ma5", 0)
    ma20 = history.get("ma20", 0)
    ret5 = history.get("ret5", 0)
    ret20 = history.get("ret20", 0)
    score = 50.0
    if ma5 > 0 and price > ma5:
        score += 12
    if ma20 > 0 and price > ma20:
        score += 12
    if ma5 > ma20 > 0:
        score += 10
    score += _clamp(ret5, -8, 8) * 1.1
    score += _clamp(ret20, -15, 15) * 0.35
    return _clamp(score, 0, 100)


def _score_momentum(pct_change: float, volume_ratio: float) -> float:
    """计算实时涨幅和量比动量分。"""

    score = 50.0
    if 0.5 <= pct_change <= 5.8:
        score += 22
    elif pct_change > 7.5:
        score -= 18
    elif pct_change < -3:
        score -= 20
    score += _clamp((volume_ratio - 1) * 18, -12, 18)
    return _clamp(score, 0, 100)


def _score_liquidity(amount: float, turnover: float) -> float:
    """计算成交额和换手率流动性分。"""

    score = 45.0
    if amount > 0:
        score += _clamp(math.log10(max(amount, 1)) * 7 - 35, 0, 30)
    if 1 <= turnover <= 12:
        score += 18
    elif turnover > 18:
        score -= 8
    return _clamp(score, 0, 100)


def _score_risk(pct_change: float, amplitude: float, name: str) -> float:
    """计算风险控制分，分数越高表示风险越可控。"""

    score = 82.0
    if _is_risk_name(name):
        score -= 45
    if pct_change > 8.5:
        score -= 24
    if pct_change < -5:
        score -= 22
    if amplitude > 8:
        score -= 16
    return _clamp(score, 0, 100)


def _resolve_action(
    symbol: str,
    price: float,
    pct_change: float,
    final_score: float,
    positions: dict[str, dict[str, Any]],
) -> str:
    """根据分数和持仓状态转换为买卖观察信号。"""

    position = positions.get(symbol)
    if position:
        avg_cost = _float_value(position.get("avg_cost"))
        if final_score < 48 or pct_change <= -3.5 or (avg_cost > 0 and price < avg_cost * 0.95):
            return "SELL"
        return "HOLD"
    if final_score >= 64 and pct_change > 0:
        return "BUY"
    if final_score >= 58:
        return "WATCH"
    return "HOLD"


def _build_signal_reason(
    history: dict[str, float],
    pct_change: float,
    volume_ratio: float,
    turnover: float,
) -> str:
    """生成实时信号理由。"""

    return (
        f"实时涨幅{pct_change:.2f}%，量比{volume_ratio:.2f}，"
        f"5日{history.get('ret5', 0):.2f}%，换手{turnover:.2f}%"
    )


def _build_signal_risk(pct_change: float, amplitude: float, risk_score: float) -> str:
    """生成实时信号风险提示。"""

    risks = []
    if pct_change > 7:
        risks.append("涨幅偏高")
    if pct_change < -3:
        risks.append("盘中转弱")
    if amplitude > 8:
        risks.append("振幅偏大")
    if risk_score < 55:
        risks.append("风控分偏低")
    return "；".join(risks) if risks else "注意盘中波动和成交持续性"


def _decimal_or_none(value: Any) -> Decimal | None:
    """将任意数值转换为 Decimal，失败时返回空。"""

    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _dec(value: Any) -> Decimal:
    """将空值安全转换为 Decimal 零值。"""

    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _float_value(value: Any) -> float:
    """将空值和 Decimal 安全转换为浮点数。"""

    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mean(values: list[float]) -> float:
    """计算均值，空列表返回零。"""

    return sum(values) / len(values) if values else 0.0


def _pct_return(values: list[float], periods: int) -> float:
    """计算指定周期收益率。"""

    if len(values) <= periods or values[-periods - 1] == 0:
        return 0.0
    return (values[-1] / values[-periods - 1] - 1) * 100


def _clamp(value: float, low: float, high: float) -> float:
    """将数值限制在指定区间。"""

    return max(low, min(high, value))


def _is_risk_name(name: str) -> bool:
    """识别 ST、退市和上市首日高波动股票名称。"""

    return (
        "ST" in name.upper()
        or "退" in name
        or name.startswith("N")
        or name.startswith("C")
    )
