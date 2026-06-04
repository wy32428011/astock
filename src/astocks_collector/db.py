"""MySQL 建库建表和行情数据写入。"""

from __future__ import annotations

import logging
import json
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from astocks_collector.config import AppConfig

logger = logging.getLogger(__name__)


class MySQLRepository:
    """封装 MySQL 连接、Schema 初始化和幂等写入逻辑。"""

    def __init__(self, config: AppConfig) -> None:
        """保存应用配置，连接在每次操作时按需创建。"""

        self.config = config

    @contextmanager
    def connection(self, include_database: bool = True):
        """创建并自动关闭 MySQL 连接。"""

        conn = pymysql.connect(
            **self.config.mysql_kwargs(include_database=include_database),
            cursorclass=DictCursor,
        )
        try:
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """创建数据库和所有业务表。"""

        self.create_database()
        with self.connection(include_database=True) as conn:
            with conn.cursor() as cursor:
                for sql in self._schema_sql():
                    cursor.execute(sql)
            conn.commit()
        logger.info("数据库与表结构已确认: %s", self.config.mysql_database)

    def create_database(self) -> None:
        """创建目标数据库，数据库名经过白名单校验。"""

        database = _quote_identifier(self.config.mysql_database)
        charset = self.config.mysql_charset
        with self.connection(include_database=False) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS {database} "
                    f"DEFAULT CHARACTER SET {charset} COLLATE {charset}_unicode_ci"
                )
            conn.commit()

    def start_run(self, task_type: str, message: str | None = None) -> int:
        """写入采集任务开始记录，并返回运行编号。"""

        sql = """
            INSERT INTO collector_run (task_type, status, started_at, message)
            VALUES (%s, 'running', NOW(), %s)
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (task_type, message))
                run_id = int(cursor.lastrowid)
            conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: int,
        status: str,
        total_symbols: int = 0,
        success_symbols: int = 0,
        failed_symbols: int = 0,
        rows_written: int = 0,
        message: str | None = None,
    ) -> None:
        """更新采集任务结束状态和统计信息。"""

        sql = """
            UPDATE collector_run
            SET status=%s,
                finished_at=NOW(),
                total_symbols=%s,
                success_symbols=%s,
                failed_symbols=%s,
                rows_written=%s,
                message=%s
            WHERE id=%s
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    sql,
                    (
                        status,
                        total_symbols,
                        success_symbols,
                        failed_symbols,
                        rows_written,
                        message,
                        run_id,
                    ),
                )
            conn.commit()

    def record_error(
        self,
        task_type: str,
        error_message: str,
        run_id: int | None = None,
        symbol: str | None = None,
    ) -> None:
        """记录单只股票或任务级别的采集异常。"""

        sql = """
            INSERT INTO collector_error (run_id, task_type, symbol, error_message, created_at)
            VALUES (%s, %s, %s, %s, NOW())
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, (run_id, task_type, symbol, error_message[:2000]))
            conn.commit()

    def upsert_stock_basic(self, records: Sequence[Mapping[str, Any]]) -> int:
        """批量写入股票基础信息，按 symbol 幂等更新。"""

        if not records:
            return 0

        sql = """
            INSERT INTO stock_basic (
                symbol, name, exchange, market, latest_price, pct_change,
                turnover_rate, total_market_value, circulating_market_value,
                source, is_active
            )
            VALUES (
                %(symbol)s, %(name)s, %(exchange)s, %(market)s, %(latest_price)s,
                %(pct_change)s, %(turnover_rate)s, %(total_market_value)s,
                %(circulating_market_value)s, %(source)s, %(is_active)s
            )
            ON DUPLICATE KEY UPDATE
                name=VALUES(name),
                exchange=VALUES(exchange),
                market=VALUES(market),
                latest_price=VALUES(latest_price),
                pct_change=VALUES(pct_change),
                turnover_rate=VALUES(turnover_rate),
                total_market_value=VALUES(total_market_value),
                circulating_market_value=VALUES(circulating_market_value),
                source=VALUES(source),
                is_active=VALUES(is_active),
                updated_at=CURRENT_TIMESTAMP
        """
        return self._executemany(sql, records)

    def upsert_stock_daily(self, rows: Sequence[Mapping[str, Any]], conn=None) -> int:
        """批量写入日线行情，按 symbol、trade_date、adjust_type 幂等更新。"""

        if not rows:
            return 0

        sql = """
            INSERT INTO stock_daily (
                symbol, trade_date, adjust_type, open_price, close_price,
                high_price, low_price, pre_close, volume, amount, amplitude,
                pct_change, change_amount, turnover_rate, source
            )
            VALUES (
                %(symbol)s, %(trade_date)s, %(adjust_type)s, %(open_price)s,
                %(close_price)s, %(high_price)s, %(low_price)s, %(pre_close)s,
                %(volume)s, %(amount)s, %(amplitude)s, %(pct_change)s,
                %(change_amount)s, %(turnover_rate)s, %(source)s
            )
            ON DUPLICATE KEY UPDATE
                open_price=VALUES(open_price),
                close_price=VALUES(close_price),
                high_price=VALUES(high_price),
                low_price=VALUES(low_price),
                pre_close=VALUES(pre_close),
                volume=VALUES(volume),
                amount=VALUES(amount),
                amplitude=VALUES(amplitude),
                pct_change=VALUES(pct_change),
                change_amount=VALUES(change_amount),
                turnover_rate=VALUES(turnover_rate),
                source=VALUES(source),
                updated_at=CURRENT_TIMESTAMP
        """
        return self._executemany(sql, rows, conn=conn)

    def get_symbols(self, limit: int | None = None) -> list[str]:
        """读取已同步的股票代码列表。"""

        sql = "SELECT symbol FROM stock_basic WHERE is_active=1 ORDER BY symbol"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT %s"
            params = (limit,)
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        return [str(row["symbol"]) for row in rows]

    def recent_stock_daily_history(
        self,
        symbols: Sequence[str],
        trade_date: Any,
        lookback_days: int = 30,
    ) -> dict[str, list[dict[str, Any]]]:
        """按股票批量读取信号日前最近日线，用于盘中质量选股历史因子。"""

        unique_symbols = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
        if not unique_symbols:
            return {}
        lookback = max(1, int(lookback_days))
        placeholders = ", ".join(["%s"] * len(unique_symbols))
        sql = f"""
            SELECT
                symbol,
                trade_date,
                close_price,
                volume,
                amount,
                amplitude,
                pct_change,
                turnover_rate
            FROM (
                SELECT
                    symbol,
                    trade_date,
                    close_price,
                    volume,
                    amount,
                    amplitude,
                    pct_change,
                    turnover_rate,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol
                        ORDER BY trade_date DESC
                    ) AS rn
                FROM stock_daily
                WHERE adjust_type=%s
                  AND trade_date < %s
                  AND symbol IN ({placeholders})
            ) ranked
            WHERE rn <= %s
            ORDER BY symbol ASC, trade_date ASC
        """
        params: tuple[Any, ...] = (
            self.config.adjust_type,
            trade_date,
            *unique_symbols,
            lookback,
        )
        history: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in unique_symbols}
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall() or []
        for row in rows:
            history.setdefault(str(row.get("symbol") or ""), []).append(dict(row))
        return history

    def table_counts(self) -> dict[str, int]:
        """返回核心表行数，用于验证运行结果。"""

        counts: dict[str, int] = {}
        with self.connection() as conn:
            with conn.cursor() as cursor:
                for table in (
                    "stock_basic",
                    "stock_daily",
                    "stock_analysis_pick",
                    "stock_call_auction_quote",
                    "stock_call_auction_run",
                    "stock_call_auction_pick",
                    "stock_three_day_pick",
                    "stock_realtime_quote",
                    "stock_realtime_signal",
                    "stock_realtime_decision",
                    "simulation_account",
                    "simulation_position",
                    "simulation_order",
                    "simulation_trade",
                    "collector_run",
                    "collector_error",
                ):
                    cursor.execute(f"SELECT COUNT(*) AS total FROM {table}")
                    counts[table] = int(cursor.fetchone()["total"])
        return counts

    def upsert_realtime_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """批量写入实时行情快照，按股票代码保留最新一条。"""

        if not rows:
            return 0

        sql = """
            INSERT INTO stock_realtime_quote (
                symbol, name, exchange, snapshot_time, latest_price, pct_change,
                change_amount, volume, amount, amplitude, high_price, low_price,
                open_price, pre_close, volume_ratio, turnover_rate, pe_dynamic,
                pb, total_market_value, circulating_market_value, source
            )
            VALUES (
                %(symbol)s, %(name)s, %(exchange)s, %(snapshot_time)s,
                %(latest_price)s, %(pct_change)s, %(change_amount)s,
                %(volume)s, %(amount)s, %(amplitude)s, %(high_price)s,
                %(low_price)s, %(open_price)s, %(pre_close)s, %(volume_ratio)s,
                %(turnover_rate)s, %(pe_dynamic)s, %(pb)s,
                %(total_market_value)s, %(circulating_market_value)s, %(source)s
            )
            ON DUPLICATE KEY UPDATE
                name=VALUES(name),
                exchange=VALUES(exchange),
                snapshot_time=VALUES(snapshot_time),
                latest_price=VALUES(latest_price),
                pct_change=VALUES(pct_change),
                change_amount=VALUES(change_amount),
                volume=VALUES(volume),
                amount=VALUES(amount),
                amplitude=VALUES(amplitude),
                high_price=VALUES(high_price),
                low_price=VALUES(low_price),
                open_price=VALUES(open_price),
                pre_close=VALUES(pre_close),
                volume_ratio=VALUES(volume_ratio),
                turnover_rate=VALUES(turnover_rate),
                pe_dynamic=VALUES(pe_dynamic),
                pb=VALUES(pb),
                total_market_value=VALUES(total_market_value),
                circulating_market_value=VALUES(circulating_market_value),
                source=VALUES(source),
                updated_at=CURRENT_TIMESTAMP
        """
        return self._executemany(sql, rows)

    def insert_realtime_signals(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """批量写入实时分析信号，保留信号生成历史。"""

        if not rows:
            return 0

        prepared: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["quote_snapshot"] = json.dumps(
                item.get("quote_snapshot") or {}, ensure_ascii=False
            )
            prepared.append(item)

        sql = """
            INSERT INTO stock_realtime_signal (
                signal_time, symbol, name, latest_price, pct_change,
                trend_score, momentum_score, liquidity_score, risk_score,
                final_score, signal_action, confidence, reason, risk,
                quote_snapshot
            )
            VALUES (
                %(signal_time)s, %(symbol)s, %(name)s, %(latest_price)s,
                %(pct_change)s, %(trend_score)s, %(momentum_score)s,
                %(liquidity_score)s, %(risk_score)s, %(final_score)s,
                %(signal_action)s, %(confidence)s, %(reason)s, %(risk)s,
                %(quote_snapshot)s
            )
        """
        return self._executemany(sql, prepared)

    def insert_realtime_decisions(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """批量写入实时 LLM/规则最终决策记录。"""

        if not rows:
            return 0

        prepared: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["raw_response"] = json.dumps(
                item.get("raw_response") or {}, ensure_ascii=False
            )
            item["is_fallback"] = 1 if item.get("is_fallback") else 0
            prepared.append(item)

        sql = """
            INSERT INTO stock_realtime_decision (
                decision_time, signal_time, symbol, name, latest_price,
                rule_action, llm_action, final_action, rule_score, llm_score,
                decision_mode, decision_source, llm_reason, llm_risk,
                is_fallback, raw_response
            )
            VALUES (
                %(decision_time)s, %(signal_time)s, %(symbol)s, %(name)s,
                %(latest_price)s, %(rule_action)s, %(llm_action)s,
                %(final_action)s, %(rule_score)s, %(llm_score)s,
                %(decision_mode)s, %(decision_source)s, %(llm_reason)s,
                %(llm_risk)s, %(is_fallback)s, %(raw_response)s
            )
        """
        return self._executemany(sql, prepared)

    def reset_simulation(self, initial_cash: float) -> None:
        """重置默认模拟账户，清空持仓、订单和成交记录。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM simulation_trade WHERE account_id='default'")
                cursor.execute("DELETE FROM simulation_order WHERE account_id='default'")
                cursor.execute("DELETE FROM simulation_position WHERE account_id='default'")
                cursor.execute("DELETE FROM simulation_account WHERE account_id='default'")
                cursor.execute(
                    """
                    INSERT INTO simulation_account (
                        account_id, initial_cash, cash, market_value, total_asset,
                        realized_pnl
                    )
                    VALUES ('default', %s, %s, 0, %s, 0)
                    """,
                    (initial_cash, initial_cash, initial_cash),
                )
            conn.commit()

    def replace_analysis_picks(
        self,
        analysis_date: str,
        picks: Sequence[Mapping[str, Any]],
        raw_response: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> int:
        """替换指定分析日期的 T+1 选股结果。"""

        raw_json = json.dumps(raw_response or {}, ensure_ascii=False)
        sql = """
            INSERT INTO stock_analysis_pick (
                analysis_date, rank_no, symbol, name, trade_date,
                quant_score, llm_score, final_score, expected_direction,
                reason, risk, raw_response
            )
            VALUES (
                %(analysis_date)s, %(rank_no)s, %(symbol)s, %(name)s, %(trade_date)s,
                %(quant_score)s, %(llm_score)s, %(final_score)s, %(expected_direction)s,
                %(reason)s, %(risk)s, %(raw_response)s
            )
            ON DUPLICATE KEY UPDATE
                name=VALUES(name),
                trade_date=VALUES(trade_date),
                quant_score=VALUES(quant_score),
                llm_score=VALUES(llm_score),
                final_score=VALUES(final_score),
                expected_direction=VALUES(expected_direction),
                reason=VALUES(reason),
                risk=VALUES(risk),
                raw_response=VALUES(raw_response),
                updated_at=CURRENT_TIMESTAMP
        """
        rows = []
        for pick in picks:
            row = dict(pick)
            row["analysis_date"] = analysis_date
            row["raw_response"] = raw_json
            rows.append(row)

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM stock_analysis_pick WHERE analysis_date=%s",
                    (analysis_date,),
                )
                affected = cursor.executemany(sql, rows) if rows else 0
            conn.commit()
        return affected

    def insert_t1_quality_run(self, row: Mapping[str, Any]) -> int:
        """写入一次 T+1 盘中质量选股运行记录，并返回运行编号。"""

        sql = """
            INSERT INTO stock_t1_intraday_run (
                trade_date, snapshot_time, trigger_type, status, quote_count,
                valid_quote_count, candidate_count, pick_count, buy_count,
                sell_count, execute_trades, llm_required, llm_success,
                market_session, skip_reason, error_message, summary_json
            )
            VALUES (
                %(trade_date)s, %(snapshot_time)s, %(trigger_type)s, %(status)s,
                %(quote_count)s, %(valid_quote_count)s, %(candidate_count)s,
                %(pick_count)s, %(buy_count)s, %(sell_count)s, %(execute_trades)s,
                %(llm_required)s, %(llm_success)s, %(market_session)s,
                %(skip_reason)s, %(error_message)s, %(summary_json)s
            )
        """
        payload = dict(row)
        payload["execute_trades"] = 1 if payload.get("execute_trades") else 0
        payload["llm_required"] = 1 if payload.get("llm_required") else 0
        payload["llm_success"] = 1 if payload.get("llm_success") else 0
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, payload)
                run_id = int(cursor.lastrowid)
            conn.commit()
        return run_id

    def update_t1_quality_run(self, run_id: int, updates: Mapping[str, Any]) -> None:
        """更新 T+1 盘中质量选股运行统计。"""

        allowed = {"status", "pick_count", "buy_count", "sell_count", "skip_reason", "error_message", "summary_json"}
        values = {key: value for key, value in updates.items() if key in allowed}
        if not values:
            return
        assignments = ", ".join(f"{key}=%s" for key in values)
        params = [*values.values(), run_id]
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE stock_t1_intraday_run SET {assignments} WHERE id=%s",
                    params,
                )
            conn.commit()

    def replace_t1_quality_picks(self, run_id: int, picks: Sequence[Mapping[str, Any]]) -> int:
        """替换指定运行编号下的 T+1 盘中质量候选。"""

        sql = """
            INSERT INTO stock_t1_intraday_pick (
                run_id, trade_date, snapshot_time, rank_no, symbol, name,
                latest_price, pct_change, volume_ratio, turnover_rate,
                trend_score, momentum_score, liquidity_score, risk_score,
                quant_score, llm_score, final_score, signal_action,
                expected_direction, reason, risk, factor_snapshot, raw_response
            )
            VALUES (
                %(run_id)s, %(trade_date)s, %(snapshot_time)s, %(rank_no)s,
                %(symbol)s, %(name)s, %(latest_price)s, %(pct_change)s,
                %(volume_ratio)s, %(turnover_rate)s, %(trend_score)s,
                %(momentum_score)s, %(liquidity_score)s, %(risk_score)s,
                %(quant_score)s, %(llm_score)s, %(final_score)s,
                %(signal_action)s, %(expected_direction)s, %(reason)s,
                %(risk)s, %(factor_snapshot)s, %(raw_response)s
            )
            ON DUPLICATE KEY UPDATE
                rank_no=VALUES(rank_no),
                latest_price=VALUES(latest_price),
                pct_change=VALUES(pct_change),
                volume_ratio=VALUES(volume_ratio),
                turnover_rate=VALUES(turnover_rate),
                trend_score=VALUES(trend_score),
                momentum_score=VALUES(momentum_score),
                liquidity_score=VALUES(liquidity_score),
                risk_score=VALUES(risk_score),
                quant_score=VALUES(quant_score),
                llm_score=VALUES(llm_score),
                final_score=VALUES(final_score),
                signal_action=VALUES(signal_action),
                expected_direction=VALUES(expected_direction),
                reason=VALUES(reason),
                risk=VALUES(risk),
                factor_snapshot=VALUES(factor_snapshot),
                raw_response=VALUES(raw_response),
                updated_at=CURRENT_TIMESTAMP
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM stock_t1_intraday_pick WHERE run_id=%s", (run_id,))
                affected = cursor.executemany(sql, [dict(item) for item in picks]) if picks else 0
            conn.commit()
        return affected

    def latest_t1_quality_run(self) -> dict[str, Any] | None:
        """读取最近一次 T+1 盘中质量选股运行记录。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM stock_t1_intraday_run
                    ORDER BY snapshot_time DESC, id DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        return _quality_run_to_dict(row) if row else None

    def latest_t1_quality_picks(self, limit: int = 20) -> list[dict[str, Any]]:
        """读取最近一次 T+1 盘中质量选股候选。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM stock_t1_intraday_pick
                    WHERE run_id = (SELECT MAX(id) FROM stock_t1_intraday_run)
                    ORDER BY rank_no ASC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = cursor.fetchall() or []
        return [_quality_pick_to_dict(row) for row in rows]

    def has_successful_t1_quality_run(self, trade_date: Any) -> bool:
        """判断指定交易日是否已有成功的 T+1 质量选股，避免重复买入。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM stock_t1_intraday_run
                    WHERE trade_date=%s AND status='SUCCESS'
                    """,
                    (trade_date,),
                )
                row = cursor.fetchone() or {}
        return int(row.get("cnt") or 0) > 0

    def insert_call_auction_run(self, row: Mapping[str, Any]) -> int:
        """写入一次集合竞价选股运行记录，并返回运行编号。"""

        sql = """
            INSERT INTO stock_call_auction_run (
                trade_date, snapshot_time, trigger_type, status, quote_count,
                valid_quote_count, candidate_count, pick_count, llm_required,
                llm_success, market_session, skip_reason, error_message, summary_json
            )
            VALUES (
                %(trade_date)s, %(snapshot_time)s, %(trigger_type)s, %(status)s,
                %(quote_count)s, %(valid_quote_count)s, %(candidate_count)s,
                %(pick_count)s, %(llm_required)s, %(llm_success)s,
                %(market_session)s, %(skip_reason)s, %(error_message)s,
                %(summary_json)s
            )
        """
        payload = dict(row)
        payload["llm_required"] = 1 if payload.get("llm_required") else 0
        payload["llm_success"] = 1 if payload.get("llm_success") else 0
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, payload)
                run_id = int(cursor.lastrowid)
            conn.commit()
        return run_id

    def upsert_call_auction_quotes(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """批量写入集合竞价融合快照，按交易日和股票保留最佳字段。"""

        if not rows:
            return 0
        sql = """
            INSERT INTO stock_call_auction_quote (
                trade_date, symbol, name, exchange, latest_price, pct_change,
                volume, amount, volume_ratio, turnover_rate, amplitude, source,
                first_sample_time, latest_sample_time, sample_count,
                quality_flags, raw_snapshot
            )
            VALUES (
                %(trade_date)s, %(symbol)s, %(name)s, %(exchange)s,
                %(latest_price)s, %(pct_change)s, %(volume)s, %(amount)s,
                %(volume_ratio)s, %(turnover_rate)s, %(amplitude)s, %(source)s,
                %(first_sample_time)s, %(latest_sample_time)s, 1,
                %(quality_flags)s, %(raw_snapshot)s
            )
            ON DUPLICATE KEY UPDATE
                name=IF(VALUES(name) <> '', VALUES(name), name),
                exchange=IF(VALUES(exchange) <> '', VALUES(exchange), exchange),
                latest_price=IF(VALUES(latest_price) > 0, VALUES(latest_price), latest_price),
                pct_change=IF(VALUES(latest_price) > 0, VALUES(pct_change), pct_change),
                volume=IF(VALUES(volume) > 0, VALUES(volume), volume),
                amount=IF(VALUES(amount) > 0, VALUES(amount), amount),
                volume_ratio=IF(VALUES(volume_ratio) > 0, VALUES(volume_ratio), volume_ratio),
                turnover_rate=IF(VALUES(turnover_rate) > 0, VALUES(turnover_rate), turnover_rate),
                amplitude=IF(VALUES(amplitude) > 0, VALUES(amplitude), amplitude),
                source=IF(VALUES(source) <> '', VALUES(source), source),
                latest_sample_time=GREATEST(latest_sample_time, VALUES(latest_sample_time)),
                sample_count=sample_count + 1,
                quality_flags=VALUES(quality_flags),
                raw_snapshot=VALUES(raw_snapshot),
                updated_at=CURRENT_TIMESTAMP
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                affected = cursor.executemany(sql, [dict(item) for item in rows])
            conn.commit()
        return int(affected)

    def latest_call_auction_quotes(self, trade_date: Any) -> list[dict[str, Any]]:
        """读取指定交易日的集合竞价融合快照。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM stock_call_auction_quote
                    WHERE trade_date = %s
                    ORDER BY latest_sample_time DESC, symbol ASC
                    """,
                    (trade_date,),
                )
                rows = cursor.fetchall() or []
        return [_call_auction_quote_to_dict(row) for row in rows]

    def call_auction_quote_summary(self, trade_date: Any) -> dict[str, Any]:
        """统计指定交易日集合竞价融合快照的覆盖情况。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS quote_count,
                        SUM(CASE WHEN latest_price > 0 THEN 1 ELSE 0 END) AS valid_price_count,
                        MIN(first_sample_time) AS first_sample_time,
                        MAX(latest_sample_time) AS latest_sample_time,
                        SUM(sample_count) AS total_sample_count
                    FROM stock_call_auction_quote
                    WHERE trade_date = %s
                    """,
                    (trade_date,),
                )
                row = cursor.fetchone() or {}
        return {
            "quoteCount": int(row.get("quote_count") or 0),
            "validPriceCount": int(row.get("valid_price_count") or 0),
            "firstSampleTime": _datetime_text(row.get("first_sample_time")),
            "latestSampleTime": _datetime_text(row.get("latest_sample_time")),
            "totalSampleCount": int(row.get("total_sample_count") or 0),
        }

    def replace_call_auction_picks(
        self, run_id: int, picks: Sequence[Mapping[str, Any]]
    ) -> int:
        """替换指定运行编号下的集合竞价候选。"""

        sql = """
            INSERT INTO stock_call_auction_pick (
                run_id, trade_date, snapshot_time, rank_no, symbol, name,
                latest_price, pct_change, volume, amount, volume_ratio,
                turnover_rate, price_score, volume_score, trend_score,
                risk_score, quant_score, llm_score, final_score, signal_action,
                reason, risk, factor_snapshot, raw_response
            )
            VALUES (
                %(run_id)s, %(trade_date)s, %(snapshot_time)s, %(rank_no)s,
                %(symbol)s, %(name)s, %(latest_price)s, %(pct_change)s,
                %(volume)s, %(amount)s, %(volume_ratio)s, %(turnover_rate)s,
                %(price_score)s, %(volume_score)s, %(trend_score)s,
                %(risk_score)s, %(quant_score)s, %(llm_score)s,
                %(final_score)s, %(signal_action)s, %(reason)s, %(risk)s,
                %(factor_snapshot)s, %(raw_response)s
            )
            ON DUPLICATE KEY UPDATE
                rank_no=VALUES(rank_no),
                latest_price=VALUES(latest_price),
                pct_change=VALUES(pct_change),
                volume=VALUES(volume),
                amount=VALUES(amount),
                volume_ratio=VALUES(volume_ratio),
                turnover_rate=VALUES(turnover_rate),
                price_score=VALUES(price_score),
                volume_score=VALUES(volume_score),
                trend_score=VALUES(trend_score),
                risk_score=VALUES(risk_score),
                quant_score=VALUES(quant_score),
                llm_score=VALUES(llm_score),
                final_score=VALUES(final_score),
                signal_action=VALUES(signal_action),
                reason=VALUES(reason),
                risk=VALUES(risk),
                factor_snapshot=VALUES(factor_snapshot),
                raw_response=VALUES(raw_response),
                updated_at=CURRENT_TIMESTAMP
        """
        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM stock_call_auction_pick WHERE run_id=%s", (run_id,))
                affected = cursor.executemany(sql, [dict(item) for item in picks]) if picks else 0
            conn.commit()
        return affected

    def latest_call_auction_run(self) -> dict[str, Any] | None:
        """读取最近一次集合竞价选股运行记录。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM stock_call_auction_run
                    ORDER BY snapshot_time DESC, id DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        return _call_auction_run_to_dict(row) if row else None

    def latest_call_auction_picks(self, limit: int = 20) -> list[dict[str, Any]]:
        """读取最近一次集合竞价选股候选。"""

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM stock_call_auction_pick
                    WHERE run_id = (
                        SELECT MAX(id)
                        FROM stock_call_auction_run
                        WHERE status = 'SUCCESS'
                    )
                    ORDER BY rank_no ASC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = cursor.fetchall() or []
        return [_call_auction_pick_to_dict(row) for row in rows]

    def replace_three_day_picks(
        self,
        analysis_date: str,
        picks: Sequence[Mapping[str, Any]],
        raw_response: Mapping[str, Any] | Sequence[Any] | None = None,
        llm_fallback: bool = False,
    ) -> int:
        """替换指定分析日期的未来 3 个交易日涨势候选结果。"""

        if not picks:
            return 0

        raw_json = json.dumps(raw_response or {}, ensure_ascii=False)
        sql = """
            INSERT INTO stock_three_day_pick (
                analysis_date, rank_no, symbol, name, trade_date, horizon_days,
                quant_score, llm_score, final_score, expected_direction,
                reason, risk, factor_snapshot, raw_response, llm_fallback
            )
            VALUES (
                %(analysis_date)s, %(rank_no)s, %(symbol)s, %(name)s, %(trade_date)s,
                %(horizon_days)s, %(quant_score)s, %(llm_score)s, %(final_score)s,
                %(expected_direction)s, %(reason)s, %(risk)s, %(factor_snapshot)s,
                %(raw_response)s, %(llm_fallback)s
            )
            ON DUPLICATE KEY UPDATE
                rank_no=VALUES(rank_no),
                name=VALUES(name),
                trade_date=VALUES(trade_date),
                horizon_days=VALUES(horizon_days),
                quant_score=VALUES(quant_score),
                llm_score=VALUES(llm_score),
                final_score=VALUES(final_score),
                expected_direction=VALUES(expected_direction),
                reason=VALUES(reason),
                risk=VALUES(risk),
                factor_snapshot=VALUES(factor_snapshot),
                raw_response=VALUES(raw_response),
                llm_fallback=VALUES(llm_fallback),
                updated_at=CURRENT_TIMESTAMP
        """
        rows = []
        for pick in picks:
            row = dict(pick)
            row["analysis_date"] = analysis_date
            row["horizon_days"] = int(row.get("horizon_days") or 3)
            row["factor_snapshot"] = json.dumps(
                row.get("factor_snapshot") or {}, ensure_ascii=False
            )
            row["raw_response"] = raw_json
            row["llm_fallback"] = 1 if llm_fallback or row.get("llm_fallback") else 0
            rows.append(row)

        with self.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM stock_three_day_pick WHERE analysis_date=%s",
                    (analysis_date,),
                )
                affected = cursor.executemany(sql, rows)
            conn.commit()
        return affected

    def _executemany(
        self, sql: str, rows: Sequence[Mapping[str, Any]], conn=None
    ) -> int:
        """按配置批量执行 executemany 并提交。"""

        affected = 0
        if conn is not None:
            with conn.cursor() as cursor:
                for chunk in _chunks(rows, self.config.batch_size):
                    affected += cursor.executemany(sql, list(chunk))
            conn.commit()
            return affected

        with self.connection() as conn:
            with conn.cursor() as cursor:
                for chunk in _chunks(rows, self.config.batch_size):
                    affected += cursor.executemany(sql, list(chunk))
            conn.commit()
        return affected

    def _schema_sql(self) -> list[str]:
        """返回建表 SQL，集中维护数据库结构。"""

        return [
            """
            CREATE TABLE IF NOT EXISTS stock_basic (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                exchange VARCHAR(16) NOT NULL COMMENT '交易所: SH/SZ/BJ',
                market VARCHAR(16) NOT NULL DEFAULT 'A' COMMENT '市场类型',
                latest_price DECIMAL(18,4) NULL COMMENT '最新价',
                pct_change DECIMAL(10,4) NULL COMMENT '涨跌幅百分比',
                turnover_rate DECIMAL(10,4) NULL COMMENT '换手率百分比',
                total_market_value DECIMAL(24,4) NULL COMMENT '总市值，单位元',
                circulating_market_value DECIMAL(24,4) NULL COMMENT '流通市值，单位元',
                source VARCHAR(64) NOT NULL COMMENT '数据来源',
                is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否仍在采集范围',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_stock_basic_symbol (symbol),
                KEY idx_stock_basic_exchange (exchange)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股股票基础信息';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_daily (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                trade_date DATE NOT NULL COMMENT '交易日期',
                adjust_type VARCHAR(8) NOT NULL DEFAULT '' COMMENT '复权类型: 空/qfq/hfq',
                open_price DECIMAL(18,4) NULL COMMENT '开盘价',
                close_price DECIMAL(18,4) NULL COMMENT '收盘价',
                high_price DECIMAL(18,4) NULL COMMENT '最高价',
                low_price DECIMAL(18,4) NULL COMMENT '最低价',
                pre_close DECIMAL(18,4) NULL COMMENT '估算昨收价',
                volume DECIMAL(24,4) NULL COMMENT '成交量，单位手',
                amount DECIMAL(24,4) NULL COMMENT '成交额，单位元',
                amplitude DECIMAL(10,4) NULL COMMENT '振幅百分比',
                pct_change DECIMAL(10,4) NULL COMMENT '涨跌幅百分比',
                change_amount DECIMAL(18,4) NULL COMMENT '涨跌额',
                turnover_rate DECIMAL(10,4) NULL COMMENT '换手率百分比',
                source VARCHAR(64) NOT NULL COMMENT '数据来源',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_stock_daily_symbol_date_adjust (symbol, trade_date, adjust_type),
                KEY idx_stock_daily_trade_date (trade_date),
                KEY idx_stock_daily_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股日线行情';
            """,
            """
            CREATE TABLE IF NOT EXISTS collector_run (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '运行编号',
                task_type VARCHAR(32) NOT NULL COMMENT '任务类型',
                status VARCHAR(16) NOT NULL COMMENT '运行状态',
                started_at DATETIME NOT NULL COMMENT '开始时间',
                finished_at DATETIME NULL COMMENT '结束时间',
                total_symbols INT NOT NULL DEFAULT 0 COMMENT '股票总数',
                success_symbols INT NOT NULL DEFAULT 0 COMMENT '成功股票数',
                failed_symbols INT NOT NULL DEFAULT 0 COMMENT '失败股票数',
                rows_written INT NOT NULL DEFAULT 0 COMMENT '写入或更新行数',
                message TEXT NULL COMMENT '运行说明',
                PRIMARY KEY (id),
                KEY idx_collector_run_task_started (task_type, started_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集任务运行记录';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_analysis_pick (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                analysis_date DATE NOT NULL COMMENT '分析日期',
                rank_no INT NOT NULL COMMENT '排名',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                trade_date DATE NOT NULL COMMENT '使用的最新交易日',
                quant_score DECIMAL(10,4) NOT NULL COMMENT '量化预筛评分',
                llm_score DECIMAL(10,4) NOT NULL COMMENT '大模型评分',
                final_score DECIMAL(10,4) NOT NULL COMMENT '综合评分',
                expected_direction VARCHAR(16) NOT NULL COMMENT '预期方向',
                reason TEXT NOT NULL COMMENT '入选理由',
                risk TEXT NOT NULL COMMENT '主要风险',
                raw_response JSON NULL COMMENT '大模型原始结构化响应',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_analysis_pick_date_symbol (analysis_date, symbol),
                KEY idx_analysis_pick_date_rank (analysis_date, rank_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='T+1 大模型选股结果';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_t1_intraday_run (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '运行编号',
                trade_date DATE NOT NULL COMMENT '交易日期',
                snapshot_time DATETIME NOT NULL COMMENT '行情快照时间',
                trigger_type VARCHAR(16) NOT NULL COMMENT '触发来源: scheduler/api_scheduler/manual',
                status VARCHAR(16) NOT NULL COMMENT '运行状态: SUCCESS/SKIPPED/FAILED',
                quote_count INT NOT NULL DEFAULT 0 COMMENT '原始行情数量',
                valid_quote_count INT NOT NULL DEFAULT 0 COMMENT '有效行情数量',
                candidate_count INT NOT NULL DEFAULT 0 COMMENT '量化候选数量',
                pick_count INT NOT NULL DEFAULT 0 COMMENT 'LLM复核后候选数量',
                buy_count INT NOT NULL DEFAULT 0 COMMENT '模拟买入数量',
                sell_count INT NOT NULL DEFAULT 0 COMMENT '模拟卖出数量',
                execute_trades TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否执行模拟交易',
                llm_required TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否要求LLM复核成功',
                llm_success TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'LLM复核是否成功',
                market_session VARCHAR(32) NULL COMMENT '市场时段',
                skip_reason VARCHAR(512) NULL COMMENT '跳过原因',
                error_message TEXT NULL COMMENT '错误信息',
                summary_json JSON NULL COMMENT '运行摘要',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_t1_quality_run_trade_time (trade_date, snapshot_time),
                KEY idx_t1_quality_run_status (status, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='T+1盘中质量选股运行记录';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_t1_intraday_pick (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                run_id BIGINT UNSIGNED NOT NULL COMMENT '关联运行编号',
                trade_date DATE NOT NULL COMMENT '交易日期',
                snapshot_time DATETIME NOT NULL COMMENT '行情快照时间',
                rank_no INT NOT NULL COMMENT '排名',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                latest_price DECIMAL(18,4) NOT NULL COMMENT '实时最新价',
                pct_change DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '实时涨跌幅',
                volume_ratio DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量比',
                turnover_rate DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '换手率',
                trend_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '趋势分',
                momentum_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '动量分',
                liquidity_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '流动性分',
                risk_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '风险控制分',
                quant_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量化质量分',
                llm_score DECIMAL(10,4) NULL COMMENT 'LLM复核分',
                final_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '最终质量分',
                signal_action VARCHAR(16) NOT NULL DEFAULT 'WATCH' COMMENT '候选动作',
                expected_direction VARCHAR(64) NOT NULL DEFAULT 'T日买入，T+1可卖' COMMENT '预期方向',
                reason TEXT NULL COMMENT '入选理由',
                risk TEXT NULL COMMENT '风险提示',
                factor_snapshot JSON NULL COMMENT '因子快照',
                raw_response JSON NULL COMMENT 'LLM原始响应',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_t1_quality_pick_run_symbol (run_id, symbol),
                KEY idx_t1_quality_pick_trade_rank (trade_date, rank_no),
                KEY idx_t1_quality_pick_trade_symbol (trade_date, symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='T+1盘中质量选股候选';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_call_auction_quote (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                trade_date DATE NOT NULL COMMENT '交易日期',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                exchange VARCHAR(8) NULL COMMENT '交易所',
                latest_price DECIMAL(18,4) NOT NULL DEFAULT 0 COMMENT '最新有效竞价参考价',
                pct_change DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '竞价涨跌幅',
                volume DECIMAL(24,4) NOT NULL DEFAULT 0 COMMENT '累计成交量',
                amount DECIMAL(24,4) NOT NULL DEFAULT 0 COMMENT '累计成交额',
                volume_ratio DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量比',
                turnover_rate DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '换手率',
                amplitude DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '振幅',
                source VARCHAR(64) NULL COMMENT '最近有效行情来源',
                first_sample_time DATETIME NOT NULL COMMENT '首次采样时间',
                latest_sample_time DATETIME NOT NULL COMMENT '最近采样时间',
                sample_count INT NOT NULL DEFAULT 1 COMMENT '融合采样次数',
                quality_flags JSON NULL COMMENT '字段质量标记',
                raw_snapshot JSON NULL COMMENT '最近原始行情快照',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_call_auction_quote_trade_symbol (trade_date, symbol),
                KEY idx_call_auction_quote_latest (trade_date, latest_sample_time),
                KEY idx_call_auction_quote_amount (trade_date, amount)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='集合竞价融合行情快照';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_call_auction_run (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '运行编号',
                trade_date DATE NOT NULL COMMENT '交易日期',
                snapshot_time DATETIME NOT NULL COMMENT '行情快照时间',
                trigger_type VARCHAR(32) NOT NULL COMMENT '触发来源: manual/loop/api',
                status VARCHAR(16) NOT NULL COMMENT '运行状态: SUCCESS/SKIPPED/FAILED',
                quote_count INT NOT NULL DEFAULT 0 COMMENT '原始行情数量',
                valid_quote_count INT NOT NULL DEFAULT 0 COMMENT '有效行情数量',
                candidate_count INT NOT NULL DEFAULT 0 COMMENT '规则预筛候选数量',
                pick_count INT NOT NULL DEFAULT 0 COMMENT 'LLM通过候选数量',
                llm_required TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否要求LLM复核成功',
                llm_success TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'LLM复核是否成功',
                market_session VARCHAR(32) NULL COMMENT '市场时段',
                skip_reason VARCHAR(512) NULL COMMENT '跳过原因',
                error_message TEXT NULL COMMENT '错误信息',
                summary_json JSON NULL COMMENT '运行摘要',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_call_auction_run_trade_time (trade_date, snapshot_time),
                KEY idx_call_auction_run_status (status, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='集合竞价LLM快速选股运行记录';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_call_auction_pick (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                run_id BIGINT UNSIGNED NOT NULL COMMENT '关联运行编号',
                trade_date DATE NOT NULL COMMENT '交易日期',
                snapshot_time DATETIME NOT NULL COMMENT '行情快照时间',
                rank_no INT NOT NULL COMMENT '排名',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                latest_price DECIMAL(18,4) NOT NULL COMMENT '集合竞价参考价或最新价',
                pct_change DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '竞价涨跌幅',
                volume DECIMAL(24,4) NULL COMMENT '竞价阶段累计成交量',
                amount DECIMAL(24,4) NULL COMMENT '竞价阶段成交额',
                volume_ratio DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量比',
                turnover_rate DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '换手率',
                price_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '价格强度分',
                volume_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量能流动性分',
                trend_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '历史趋势分',
                risk_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '风险控制分',
                quant_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '量化预筛分',
                llm_score DECIMAL(10,4) NULL COMMENT 'LLM复核分',
                final_score DECIMAL(10,4) NOT NULL DEFAULT 0 COMMENT '最终综合分',
                signal_action VARCHAR(24) NOT NULL DEFAULT 'WATCH' COMMENT '候选动作',
                reason TEXT NULL COMMENT '入选理由',
                risk TEXT NULL COMMENT '风险提示',
                factor_snapshot JSON NULL COMMENT '因子快照',
                raw_response JSON NULL COMMENT 'LLM原始响应',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_call_auction_pick_run_symbol (run_id, symbol),
                KEY idx_call_auction_pick_trade_rank (trade_date, rank_no),
                KEY idx_call_auction_pick_trade_symbol (trade_date, symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='集合竞价LLM快速选股候选';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_three_day_pick (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                analysis_date DATE NOT NULL COMMENT '分析日期',
                rank_no INT NOT NULL COMMENT '排名',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                trade_date DATE NOT NULL COMMENT '使用的最新完整交易日',
                horizon_days INT NOT NULL DEFAULT 3 COMMENT '预测交易日跨度',
                quant_score DECIMAL(10,4) NOT NULL COMMENT '量化 3 日趋势评分',
                llm_score DECIMAL(10,4) NOT NULL COMMENT '大模型评分',
                final_score DECIMAL(10,4) NOT NULL COMMENT '综合评分',
                expected_direction VARCHAR(128) NOT NULL COMMENT '预期方向',
                reason TEXT NOT NULL COMMENT '入选理由',
                risk TEXT NOT NULL COMMENT '主要风险',
                factor_snapshot JSON NULL COMMENT '量化因子快照',
                raw_response JSON NULL COMMENT '大模型原始结构化响应摘要',
                llm_fallback TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否使用量化降级结果',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_three_day_pick_date_symbol (analysis_date, symbol),
                KEY idx_three_day_pick_date_rank (analysis_date, rank_no),
                KEY idx_three_day_pick_trade_score (trade_date, final_score)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='未来3个交易日上涨趋势分析结果';
            """,
            """
            CREATE TABLE IF NOT EXISTS collector_error (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '错误编号',
                run_id BIGINT UNSIGNED NULL COMMENT '运行编号',
                task_type VARCHAR(32) NOT NULL COMMENT '任务类型',
                symbol VARCHAR(16) NULL COMMENT '股票代码',
                error_message TEXT NOT NULL COMMENT '错误信息',
                created_at DATETIME NOT NULL COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_collector_error_run_id (run_id),
                KEY idx_collector_error_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集错误明细';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_realtime_quote (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                exchange VARCHAR(16) NOT NULL COMMENT '交易所: SH/SZ/BJ',
                snapshot_time DATETIME NOT NULL COMMENT '行情快照时间',
                latest_price DECIMAL(18,4) NULL COMMENT '最新价',
                pct_change DECIMAL(10,4) NULL COMMENT '涨跌幅百分比',
                change_amount DECIMAL(18,4) NULL COMMENT '涨跌额',
                volume DECIMAL(24,4) NULL COMMENT '成交量，单位手',
                amount DECIMAL(24,4) NULL COMMENT '成交额，单位元',
                amplitude DECIMAL(10,4) NULL COMMENT '振幅百分比',
                high_price DECIMAL(18,4) NULL COMMENT '最高价',
                low_price DECIMAL(18,4) NULL COMMENT '最低价',
                open_price DECIMAL(18,4) NULL COMMENT '今开价',
                pre_close DECIMAL(18,4) NULL COMMENT '昨收价',
                volume_ratio DECIMAL(10,4) NULL COMMENT '量比',
                turnover_rate DECIMAL(10,4) NULL COMMENT '换手率百分比',
                pe_dynamic DECIMAL(18,4) NULL COMMENT '动态市盈率',
                pb DECIMAL(18,4) NULL COMMENT '市净率',
                total_market_value DECIMAL(24,4) NULL COMMENT '总市值，单位元',
                circulating_market_value DECIMAL(24,4) NULL COMMENT '流通市值，单位元',
                source VARCHAR(64) NOT NULL COMMENT '数据来源',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_realtime_quote_symbol (symbol),
                KEY idx_realtime_quote_snapshot (snapshot_time),
                KEY idx_realtime_quote_pct (pct_change)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股实时行情最新快照';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_realtime_signal (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '信号编号',
                signal_time DATETIME NOT NULL COMMENT '信号生成时间',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                latest_price DECIMAL(18,4) NOT NULL COMMENT '信号使用价格',
                pct_change DECIMAL(10,4) NULL COMMENT '实时涨跌幅百分比',
                trend_score DECIMAL(10,4) NOT NULL COMMENT '趋势评分',
                momentum_score DECIMAL(10,4) NOT NULL COMMENT '动量评分',
                liquidity_score DECIMAL(10,4) NOT NULL COMMENT '流动性评分',
                risk_score DECIMAL(10,4) NOT NULL COMMENT '风险评分',
                final_score DECIMAL(10,4) NOT NULL COMMENT '综合评分',
                signal_action VARCHAR(16) NOT NULL COMMENT '信号动作: BUY/SELL/WATCH/HOLD',
                confidence DECIMAL(10,4) NOT NULL COMMENT '信号置信度',
                reason TEXT NOT NULL COMMENT '信号理由',
                risk TEXT NOT NULL COMMENT '风险提示',
                quote_snapshot JSON NULL COMMENT '信号对应行情快照',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_realtime_signal_time_score (signal_time, final_score),
                KEY idx_realtime_signal_symbol_time (symbol, signal_time),
                KEY idx_realtime_signal_action (signal_action)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股实时分析信号';
            """,
            """
            CREATE TABLE IF NOT EXISTS stock_realtime_decision (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '决策编号',
                decision_time DATETIME NOT NULL COMMENT '决策生成时间',
                signal_time DATETIME NOT NULL COMMENT '关联信号生成时间',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                latest_price DECIMAL(18,4) NOT NULL COMMENT '决策使用价格',
                rule_action VARCHAR(16) NOT NULL COMMENT '规则引擎动作',
                llm_action VARCHAR(16) NOT NULL COMMENT '大模型建议动作',
                final_action VARCHAR(16) NOT NULL COMMENT '最终执行动作',
                rule_score DECIMAL(10,4) NOT NULL COMMENT '规则综合评分',
                llm_score DECIMAL(10,4) NULL COMMENT '大模型评分',
                decision_mode VARCHAR(32) NOT NULL COMMENT '决策模式',
                decision_source VARCHAR(32) NOT NULL COMMENT '决策来源',
                llm_reason TEXT NOT NULL COMMENT '大模型或降级理由',
                llm_risk TEXT NOT NULL COMMENT '大模型或降级风险',
                is_fallback TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否由大模型降级',
                raw_response JSON NULL COMMENT '大模型原始结构化响应摘要',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_realtime_decision_time_score (decision_time, llm_score),
                KEY idx_realtime_decision_signal (signal_time, symbol),
                KEY idx_realtime_decision_source (decision_source, is_fallback)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='A股实时交易最终决策';
            """,
            """
            CREATE TABLE IF NOT EXISTS simulation_account (
                account_id VARCHAR(32) NOT NULL COMMENT '模拟账户编号',
                initial_cash DECIMAL(24,4) NOT NULL COMMENT '初始资金',
                cash DECIMAL(24,4) NOT NULL COMMENT '可用现金',
                market_value DECIMAL(24,4) NOT NULL DEFAULT 0 COMMENT '持仓市值',
                total_asset DECIMAL(24,4) NOT NULL COMMENT '总资产',
                realized_pnl DECIMAL(24,4) NOT NULL DEFAULT 0 COMMENT '已实现盈亏',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (account_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟交易账户';
            """,
            """
            CREATE TABLE IF NOT EXISTS simulation_position (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '持仓编号',
                account_id VARCHAR(32) NOT NULL COMMENT '模拟账户编号',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                quantity INT NOT NULL COMMENT '持仓股数',
                available_quantity INT NOT NULL COMMENT '可卖股数',
                avg_cost DECIMAL(18,4) NOT NULL COMMENT '平均成本',
                last_price DECIMAL(18,4) NOT NULL COMMENT '最新价格',
                market_value DECIMAL(24,4) NOT NULL COMMENT '持仓市值',
                floating_pnl DECIMAL(24,4) NOT NULL COMMENT '浮动盈亏',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_sim_position_account_symbol (account_id, symbol),
                KEY idx_sim_position_account (account_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟交易持仓';
            """,
            """
            CREATE TABLE IF NOT EXISTS simulation_order (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '订单编号',
                account_id VARCHAR(32) NOT NULL COMMENT '模拟账户编号',
                signal_id BIGINT UNSIGNED NULL COMMENT '关联信号编号',
                order_time DATETIME NOT NULL COMMENT '下单时间',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                name VARCHAR(64) NOT NULL COMMENT '股票名称',
                side VARCHAR(8) NOT NULL COMMENT '方向: BUY/SELL',
                quantity INT NOT NULL COMMENT '委托股数',
                price DECIMAL(18,4) NOT NULL COMMENT '成交价格',
                amount DECIMAL(24,4) NOT NULL COMMENT '成交金额',
                fee DECIMAL(18,4) NOT NULL DEFAULT 0 COMMENT '交易费用',
                status VARCHAR(16) NOT NULL COMMENT '订单状态',
                reason TEXT NOT NULL COMMENT '下单原因',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_sim_order_account_time (account_id, order_time),
                KEY idx_sim_order_symbol_time (symbol, order_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟交易订单';
            """,
            """
            CREATE TABLE IF NOT EXISTS simulation_trade (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '成交编号',
                order_id BIGINT UNSIGNED NOT NULL COMMENT '订单编号',
                account_id VARCHAR(32) NOT NULL COMMENT '模拟账户编号',
                trade_time DATETIME NOT NULL COMMENT '成交时间',
                symbol VARCHAR(16) NOT NULL COMMENT '股票代码',
                side VARCHAR(8) NOT NULL COMMENT '方向: BUY/SELL',
                quantity INT NOT NULL COMMENT '成交股数',
                price DECIMAL(18,4) NOT NULL COMMENT '成交价格',
                amount DECIMAL(24,4) NOT NULL COMMENT '成交金额',
                fee DECIMAL(18,4) NOT NULL DEFAULT 0 COMMENT '交易费用',
                realized_pnl DECIMAL(24,4) NOT NULL DEFAULT 0 COMMENT '本次已实现盈亏',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                PRIMARY KEY (id),
                KEY idx_sim_trade_account_time (account_id, trade_time),
                KEY idx_sim_trade_order (order_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模拟交易成交明细';
            """,
        ]


def _call_auction_run_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """将集合竞价运行记录转换为前端字段。"""

    return {
        "id": int(row.get("id") or 0),
        "tradeDate": _date_text(row.get("trade_date")),
        "snapshotTime": _datetime_text(row.get("snapshot_time")),
        "triggerType": row.get("trigger_type"),
        "status": row.get("status"),
        "quoteCount": int(row.get("quote_count") or 0),
        "validQuoteCount": int(row.get("valid_quote_count") or 0),
        "candidateCount": int(row.get("candidate_count") or 0),
        "pickCount": int(row.get("pick_count") or 0),
        "llmRequired": bool(row.get("llm_required")),
        "llmSuccess": bool(row.get("llm_success")),
        "marketSession": row.get("market_session"),
        "skipReason": row.get("skip_reason") or "",
        "errorMessage": row.get("error_message") or "",
        "summary": _json_object(row.get("summary_json")),
        "createdAt": _datetime_text(row.get("created_at")),
    }


def _call_auction_quote_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """将集合竞价融合快照转换为候选构建可用字段。"""

    return {
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "exchange": row.get("exchange"),
        "latest_price": _float_or_none(row.get("latest_price")) or 0,
        "pct_change": _float_or_none(row.get("pct_change")) or 0,
        "volume": _float_or_none(row.get("volume")) or 0,
        "amount": _float_or_none(row.get("amount")) or 0,
        "volume_ratio": _float_or_none(row.get("volume_ratio")) or 0,
        "turnover_rate": _float_or_none(row.get("turnover_rate")) or 0,
        "amplitude": _float_or_none(row.get("amplitude")) or 0,
        "source": row.get("source"),
        "first_sample_time": row.get("first_sample_time"),
        "latest_sample_time": row.get("latest_sample_time"),
        "sample_count": int(row.get("sample_count") or 0),
        "quality_flags": _json_object(row.get("quality_flags")),
        "raw_snapshot": _json_object(row.get("raw_snapshot")),
    }


def _call_auction_pick_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """将集合竞价候选转换为前端字段。"""

    return {
        "id": int(row.get("id") or 0),
        "runId": int(row.get("run_id") or 0),
        "rank": int(row.get("rank_no") or 0),
        "tradeDate": _date_text(row.get("trade_date")),
        "snapshotTime": _datetime_text(row.get("snapshot_time")),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "latestPrice": _float_or_none(row.get("latest_price")) or 0,
        "pctChange": _float_or_none(row.get("pct_change")) or 0,
        "volume": _float_or_none(row.get("volume")) or 0,
        "amount": _float_or_none(row.get("amount")) or 0,
        "volumeRatio": _float_or_none(row.get("volume_ratio")) or 0,
        "turnoverRate": _float_or_none(row.get("turnover_rate")) or 0,
        "priceScore": _float_or_none(row.get("price_score")) or 0,
        "volumeScore": _float_or_none(row.get("volume_score")) or 0,
        "trendScore": _float_or_none(row.get("trend_score")) or 0,
        "riskScore": _float_or_none(row.get("risk_score")) or 0,
        "quantScore": _float_or_none(row.get("quant_score")) or 0,
        "llmScore": _float_or_none(row.get("llm_score")),
        "finalScore": _float_or_none(row.get("final_score")) or 0,
        "action": row.get("signal_action"),
        "reason": row.get("reason") or "",
        "risk": row.get("risk") or "",
    }


def _quality_run_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """将 T+1 质量选股运行记录转换为前端字段。"""

    return {
        "id": int(row.get("id") or 0),
        "tradeDate": _date_text(row.get("trade_date")),
        "snapshotTime": _datetime_text(row.get("snapshot_time")),
        "triggerType": row.get("trigger_type"),
        "status": row.get("status"),
        "quoteCount": int(row.get("quote_count") or 0),
        "validQuoteCount": int(row.get("valid_quote_count") or 0),
        "candidateCount": int(row.get("candidate_count") or 0),
        "pickCount": int(row.get("pick_count") or 0),
        "buyCount": int(row.get("buy_count") or 0),
        "sellCount": int(row.get("sell_count") or 0),
        "executeTrades": bool(row.get("execute_trades")),
        "llmRequired": bool(row.get("llm_required")),
        "llmSuccess": bool(row.get("llm_success")),
        "marketSession": row.get("market_session"),
        "skipReason": row.get("skip_reason") or "",
        "errorMessage": row.get("error_message") or "",
        "createdAt": _datetime_text(row.get("created_at")),
    }


def _quality_pick_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """将 T+1 质量选股候选转换为前端字段。"""

    return {
        "id": int(row.get("id") or 0),
        "runId": int(row.get("run_id") or 0),
        "rank": int(row.get("rank_no") or 0),
        "tradeDate": _date_text(row.get("trade_date")),
        "snapshotTime": _datetime_text(row.get("snapshot_time")),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "latestPrice": _float_or_none(row.get("latest_price")) or 0,
        "pctChange": _float_or_none(row.get("pct_change")) or 0,
        "volumeRatio": _float_or_none(row.get("volume_ratio")) or 0,
        "turnoverRate": _float_or_none(row.get("turnover_rate")) or 0,
        "trendScore": _float_or_none(row.get("trend_score")) or 0,
        "momentumScore": _float_or_none(row.get("momentum_score")) or 0,
        "liquidityScore": _float_or_none(row.get("liquidity_score")) or 0,
        "riskScore": _float_or_none(row.get("risk_score")) or 0,
        "quantScore": _float_or_none(row.get("quant_score")) or 0,
        "llmScore": _float_or_none(row.get("llm_score")),
        "finalScore": _float_or_none(row.get("final_score")) or 0,
        "action": row.get("signal_action"),
        "expectedDirection": row.get("expected_direction"),
        "reason": row.get("reason") or "",
        "risk": row.get("risk") or "",
    }


def _json_object(value: Any) -> dict[str, Any]:
    """把 JSON 字段转换为字典，异常时返回空字典。"""

    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _date_text(value: Any) -> str | None:
    """把日期值转换为 ISO 字符串。"""

    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _datetime_text(value: Any) -> str | None:
    """把日期时间值转换为前端可读字符串。"""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat(sep=" ", timespec="seconds") if hasattr(value, "isoformat") else str(value)


def _float_or_none(value: Any) -> float | None:
    """把数字值转换为 float，空值保持 None。"""

    return None if value is None else float(value)


def _quote_identifier(identifier: str) -> str:
    """校验并引用 MySQL 标识符，避免数据库名拼接时出现注入风险。"""

    if not identifier or not identifier.replace("_", "").isalnum():
        raise ValueError(f"MySQL 标识符不安全: {identifier}")
    return f"`{identifier}`"


def _chunks(
    rows: Sequence[Mapping[str, Any]], size: int
) -> Iterable[Sequence[Mapping[str, Any]]]:
    """将批量数据按固定大小切片。"""

    for start in range(0, len(rows), size):
        yield rows[start : start + size]
