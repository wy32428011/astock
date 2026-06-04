"""A股数据前端页面的 FastAPI 只读接口。"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import astocks_collector.t1_api_routes  # noqa: F401
from pydantic import BaseModel, Field
from pymysql.err import OperationalError

from astocks_collector.call_auction import CallAuctionSelector
from astocks_collector.config import AppConfig
from astocks_collector.db import MySQLRepository
from astocks_collector.t1_quality_scheduler import (
    start_api_t1_quality_scheduler,
    stop_api_t1_quality_scheduler,
)
from astocks_collector.t1_trading import T1TradingEngine
from astocks_collector.realtime import RealtimeTradingEngine
from astocks_collector.three_day_analysis import ThreeDayTrendAnalyzer


class RealtimeRunRequest(BaseModel):
    """实时分析运行请求参数。"""

    limit: int | None = Field(default=None, ge=1, le=2000)
    execute_trades: bool = True
    decision_mode: str | None = Field(default=None, pattern="^(rules|llm_review)$")


class ThreeDayRunRequest(BaseModel):
    """未来 3 个交易日涨势分析运行请求参数。"""

    trade_date: str | None = None
    preselect_limit: int | None = Field(default=None, ge=1, le=1000)
    final_limit: int | None = Field(default=None, ge=1, le=100)
    use_llm: bool = True


class CallAuctionRunRequest(BaseModel):
    """集合竞价 LLM 快速选股运行请求参数。"""

    final_limit: int | None = Field(default=None, ge=1, le=50)
    finalLimit: int | None = Field(default=None, ge=1, le=50)
    force: bool = False

    def limit(self) -> int | None:
        """兼容前端 camelCase 和后端 snake_case 字段。"""

        return self.finalLimit if self.finalLimit is not None else self.final_limit


class SimulationResetRequest(BaseModel):
    """模拟账户重置请求参数。"""

    initial_cash: float | None = Field(default=None, gt=0)


def create_app(config: AppConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用，并注册 A 股数据接口。"""

    app_config = config or AppConfig.from_env()
    repository = MySQLRepository(app_config)

    app = FastAPI(title="A股数据采集器 API", version="0.1.0")

    @app.on_event("startup")
    def _start_t1_quality_scheduler() -> None:
        """API 进程启动时注册 14:05 T+1 质量选股调度器。"""
        start_api_t1_quality_scheduler(app_config)

    @app.on_event("shutdown")
    def _stop_t1_quality_scheduler() -> None:
        """API 进程关闭时停止 T+1 质量选股调度器。"""
        stop_api_t1_quality_scheduler()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(OperationalError)
    def database_error_handler(
        _request: Request, _exception: OperationalError
    ) -> JSONResponse:
        """将数据库连接异常转换为明确的 503 响应。"""

        return JSONResponse(
            status_code=503,
            content={"detail": "数据库连接暂不可用，请稍后重试"},
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """返回服务健康状态。"""

        return {"status": "ok"}

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        """返回前端工作台概览指标。"""

        latest_trade_date = _latest_trade_date_or_none(repository)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS stock_count,
                        SUM(CASE WHEN name LIKE '%%ST%%' OR name LIKE '%%退%%' THEN 1 ELSE 0 END)
                            AS risk_count
                    FROM stock_basic
                    """
                )
                stock_summary = _json_row(cursor.fetchone())
                latest_summary: dict[str, Any] = {
                    "latest_trade_date": _json_value(latest_trade_date),
                    "latest_symbol_count": 0,
                }
                if latest_trade_date is not None:
                    cursor.execute(
                        """
                        SELECT COUNT(DISTINCT symbol) AS latest_symbol_count
                        FROM stock_daily
                        WHERE adjust_type=%s AND trade_date=%s
                        """,
                        (app_config.adjust_type, latest_trade_date),
                    )
                    latest_summary.update(_json_row(cursor.fetchone()))
                cursor.execute(
                    "SELECT COUNT(*) AS daily_count FROM stock_daily WHERE adjust_type=%s",
                    (app_config.adjust_type,),
                )
                daily_summary = _json_row(cursor.fetchone())
                cursor.execute(
                    """
                    SELECT COUNT(*) AS analysis_count
                    FROM stock_analysis_pick
                    WHERE analysis_date = (SELECT MAX(analysis_date) FROM stock_analysis_pick)
                    """
                )
                analysis_summary = _json_row(cursor.fetchone())
                cursor.execute(
                    """
                    SELECT COUNT(*) AS three_day_analysis_count
                    FROM stock_three_day_pick
                    WHERE analysis_date = (SELECT MAX(analysis_date) FROM stock_three_day_pick)
                    """
                )
                three_day_summary = _json_row(cursor.fetchone())

        return {
            **stock_summary,
            **latest_summary,
            **daily_summary,
            **analysis_summary,
            **three_day_summary,
        }

    @app.get("/api/stocks")
    def stocks(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        keyword: str | None = None,
        exchange: str | None = None,
        risk: str | None = Query(None, pattern="^(normal|risk)$"),
    ) -> dict[str, Any]:
        """分页返回股票基础信息和最新交易日行情。"""

        latest_trade_date = _latest_trade_date_or_none(repository)
        where_sql, params = _stock_filters(keyword, exchange, risk)
        offset = (page - 1) * page_size

        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) AS total FROM stock_basic b {where_sql}", params)
                total = int(cursor.fetchone()["total"])
                cursor.execute(
                    f"""
                    SELECT
                        b.symbol,
                        b.name,
                        b.exchange,
                        CASE WHEN b.name LIKE '%%ST%%' OR b.name LIKE '%%退%%' THEN 1 ELSE 0 END
                            AS is_risk,
                        d.trade_date,
                        d.close_price,
                        d.pct_change,
                        d.turnover_rate,
                        d.amount
                    FROM stock_basic b
                    LEFT JOIN stock_daily d
                        ON d.symbol=b.symbol
                       AND d.trade_date=%s
                       AND d.adjust_type=%s
                    {where_sql}
                    ORDER BY b.symbol
                    LIMIT %s OFFSET %s
                    """,
                    [latest_trade_date, app_config.adjust_type, *params, page_size, offset],
                )
                rows = _json_rows(cursor.fetchall())
        return {"data": rows, "total": total, "page": page, "pageSize": page_size}

    @app.get("/api/segments")
    def segments() -> dict[str, Any]:
        """返回分段统计、涨跌幅分布和成交额排行。"""

        latest_trade_date = _latest_trade_date_or_none(repository)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT exchange AS name, COUNT(*) AS value
                    FROM stock_basic
                    GROUP BY exchange
                    ORDER BY exchange
                    """
                )
                exchange_distribution = _json_rows(cursor.fetchall())
                cursor.execute(
                    """
                    SELECT risk_label AS name, COUNT(*) AS value
                    FROM (
                        SELECT
                            CASE
                                WHEN name LIKE '%%ST%%' OR name LIKE '%%退%%' THEN '风险股'
                                ELSE '普通股'
                            END AS risk_label
                        FROM stock_basic
                    ) t
                    GROUP BY risk_label
                    ORDER BY risk_label
                    """
                )
                risk_distribution = _json_rows(cursor.fetchall())
                pct_distribution: list[dict[str, Any]] = []
                amount_top: list[dict[str, Any]] = []
                if latest_trade_date is not None:
                    cursor.execute(
                        """
                        SELECT bucket AS name, COUNT(*) AS value
                        FROM (
                            SELECT CASE
                                WHEN d.pct_change <= -5 THEN '<=-5%%'
                                WHEN d.pct_change < 0 THEN '-5%%~0'
                                WHEN d.pct_change < 3 THEN '0~3%%'
                                WHEN d.pct_change < 6 THEN '3%%~6%%'
                                ELSE '>=6%%'
                            END AS bucket
                            FROM stock_daily d
                            JOIN stock_basic b ON b.symbol=d.symbol
                            WHERE d.trade_date=%s
                              AND d.adjust_type=%s
                              AND d.pct_change IS NOT NULL
                              AND b.name NOT LIKE '%%ST%%'
                              AND b.name NOT LIKE '%%退%%'
                        ) t
                        GROUP BY bucket
                        ORDER BY FIELD(bucket, '<=-5%%', '-5%%~0', '0~3%%', '3%%~6%%', '>=6%%')
                        """,
                        (latest_trade_date, app_config.adjust_type),
                    )
                    pct_distribution = _json_rows(cursor.fetchall())
                    cursor.execute(
                        """
                        SELECT b.symbol, b.name, d.amount, d.pct_change
                        FROM stock_daily d
                        JOIN stock_basic b ON b.symbol=d.symbol
                        WHERE d.trade_date=%s AND d.adjust_type=%s AND d.amount IS NOT NULL
                        ORDER BY d.amount DESC
                        LIMIT 15
                        """,
                        (latest_trade_date, app_config.adjust_type),
                    )
                    amount_top = _json_rows(cursor.fetchall())

        return {
            "tradeDate": latest_trade_date.isoformat() if latest_trade_date else None,
            "exchangeDistribution": exchange_distribution,
            "riskDistribution": risk_distribution,
            "pctDistribution": pct_distribution,
            "amountTop": amount_top,
        }

    @app.get("/api/history/{symbol}")
    def history(symbol: str, days: int = Query(120, ge=20, le=500)) -> dict[str, Any]:
        """返回指定股票最近 N 条历史日线。"""

        normalized_symbol = "".join(ch for ch in symbol if ch.isdigit())[-6:].zfill(6)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT symbol, name, exchange FROM stock_basic WHERE symbol=%s",
                    (normalized_symbol,),
                )
                stock = cursor.fetchone()
                if not stock:
                    raise HTTPException(status_code=404, detail="股票不存在")
                cursor.execute(
                    """
                    SELECT trade_date, open_price, close_price, high_price, low_price,
                           volume, amount, pct_change, turnover_rate
                    FROM stock_daily
                    WHERE symbol=%s AND adjust_type=%s
                    ORDER BY trade_date DESC
                    LIMIT %s
                    """,
                    (normalized_symbol, app_config.adjust_type, days),
                )
                rows = list(reversed(cursor.fetchall()))
        return {"stock": _json_row(stock), "data": _json_rows(rows)}

    @app.get("/api/analysis")
    def analysis() -> dict[str, Any]:
        """返回最新 T+1 大模型分析结果。"""

        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(analysis_date) AS analysis_date FROM stock_analysis_pick")
                analysis_date = cursor.fetchone()["analysis_date"]
                if analysis_date is None:
                    return {"analysisDate": None, "data": []}
                cursor.execute(
                    """
                    SELECT rank_no, symbol, name, trade_date, quant_score, llm_score,
                           final_score, expected_direction, reason, risk
                    FROM stock_analysis_pick
                    WHERE analysis_date=%s
                    ORDER BY rank_no
                    """,
                    (analysis_date,),
                )
                rows = _json_rows(cursor.fetchall())
        return {"analysisDate": _json_value(analysis_date), "data": rows}

    @app.get("/api/three-day-analysis")
    def three_day_analysis() -> dict[str, Any]:
        """返回最新未来 3 个交易日涨势分析结果。"""

        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT MAX(analysis_date) AS analysis_date FROM stock_three_day_pick"
                )
                analysis_date = cursor.fetchone()["analysis_date"]
                if analysis_date is None:
                    return {
                        "analysisDate": None,
                        "tradeDate": None,
                        "horizonDays": 3,
                        "llmFallback": False,
                        "candidateCount": 0,
                        "data": [],
                    }
                cursor.execute(
                    """
                    SELECT rank_no, symbol, name, trade_date, horizon_days,
                           quant_score, llm_score, final_score, expected_direction,
                           reason, risk, llm_fallback
                    FROM stock_three_day_pick
                    WHERE analysis_date=%s
                    ORDER BY rank_no
                    """,
                    (analysis_date,),
                )
                rows = _json_rows(cursor.fetchall())

        trade_date = rows[0]["trade_date"] if rows else None
        horizon_days = rows[0]["horizon_days"] if rows else 3
        llm_fallback = any(bool(row.get("llm_fallback")) for row in rows)
        return {
            "analysisDate": _json_value(analysis_date),
            "tradeDate": trade_date,
            "horizonDays": horizon_days,
            "llmFallback": llm_fallback,
            "candidateCount": len(rows),
            "data": rows,
        }

    @app.post("/api/three-day-analysis/run")
    def run_three_day_analysis(request: ThreeDayRunRequest) -> dict[str, Any]:
        """手动触发一次未来 3 个交易日涨势分析。"""

        result = ThreeDayTrendAnalyzer(app_config, repository).analyze(
            preselect_limit=request.preselect_limit,
            final_limit=request.final_limit,
            trade_date=request.trade_date,
            use_llm=request.use_llm,
        )
        return {
            "analysisDate": _json_value(result.analysis_date),
            "tradeDate": _json_value(result.trade_date),
            "horizonDays": 3,
            "preselectCount": result.preselect_count,
            "finalCount": result.final_count,
            "llmFallback": result.llm_fallback,
            "data": _json_rows(result.picks),
        }

    @app.get("/api/call-auction")
    def call_auction_dashboard(finalLimit: int = 20) -> dict[str, Any]:
        """返回集合竞价 LLM 快速选股看板数据。"""

        repository.ensure_schema()
        selector = CallAuctionSelector(app_config, repository)
        market_status = selector.market_status()
        trade_date = date.fromisoformat(str(market_status["now"])[:10])
        return {
            "latestRun": repository.latest_call_auction_run(),
            "picks": repository.latest_call_auction_picks(limit=finalLimit),
            "marketStatus": market_status,
            "quoteSummary": repository.call_auction_quote_summary(trade_date),
            "autoIntervalSeconds": app_config.call_auction_auto_interval_seconds,
        }

    @app.post("/api/call-auction/run")
    def run_call_auction(request: CallAuctionRunRequest | None = None) -> dict[str, Any]:
        """执行一次集合竞价 LLM 快速选股。"""

        payload = request or CallAuctionRunRequest()
        result = CallAuctionSelector(app_config, repository).run_once(
            final_limit=payload.limit(),
            force=payload.force,
            trigger_type="api",
        )
        return result.to_dict()

    @app.get("/api/realtime")
    def realtime_dashboard() -> dict[str, Any]:
        """返回实时分析信号和模拟交易状态。"""

        return _json_row(RealtimeTradingEngine(app_config, repository).dashboard())

    @app.post("/api/realtime/run")
    def run_realtime(request: RealtimeRunRequest) -> dict[str, Any]:
        """触发一次实时分析和模拟交易。"""

        result = RealtimeTradingEngine(app_config, repository).run_once(
            limit=request.limit,
            execute_trades=request.execute_trades,
            decision_mode=request.decision_mode,
        )
        return {
            "snapshotTime": _json_value(result.snapshot_time),
            "quoteCount": result.quote_count,
            "signalCount": result.signal_count,
            "buyCount": result.buy_count,
            "sellCount": result.sell_count,
            "orderCount": result.order_count,
            "decisionCount": result.decision_count,
            "decisionMode": result.decision_mode,
            "llmUsed": result.llm_used,
            "llmFallback": result.llm_fallback,
            "marketOpen": result.market_open,
            "skipped": result.skipped,
            "skipReason": result.skip_reason,
            "marketSession": result.market_session,
        }

    @app.post("/api/simulation/reset")
    def reset_simulation(request: SimulationResetRequest) -> dict[str, Any]:
        """重置模拟交易账户。"""

        account = RealtimeTradingEngine(app_config, repository).reset_account(
            initial_cash=request.initial_cash
        )
        return _json_row(account)

    _register_static_frontend(app)
    return app


def _register_static_frontend(app: FastAPI) -> None:
    """按配置挂载前端生产静态资源，便于 Docker 单容器部署。"""

    dist_dir_text = os.getenv("ASTOCKS_WEB_DIST", "").strip()
    if not dist_dir_text:
        return

    dist_dir = Path(dist_dir_text).resolve()
    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend_entry(frontend_path: str) -> FileResponse:
        """返回前端文件；未知路径回退到 SPA 入口页面。"""

        if frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")

        requested_file = (dist_dir / frontend_path).resolve()
        try:
            requested_file.relative_to(dist_dir)
        except ValueError:
            return FileResponse(index_file)

        if requested_file.is_file():
            return FileResponse(requested_file)
        return FileResponse(index_file)


app = create_app()


def _latest_trade_date(repository: MySQLRepository) -> date:
    """读取数据库中的最新交易日。"""

    trade_date = _latest_trade_date_or_none(repository)
    if trade_date is None:
        raise HTTPException(status_code=404, detail="暂无日线数据")
    return trade_date


def _latest_trade_date_or_none(repository: MySQLRepository) -> date | None:
    """读取全市场覆盖较完整的最新交易日；没有日线时返回空值。"""

    with repository.connection() as conn:
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
                (repository.config.adjust_type, min_symbol_count),
            )
            row = cursor.fetchone()
            if row:
                return row["trade_date"]

            cursor.execute(
                "SELECT MAX(trade_date) AS trade_date FROM stock_daily WHERE adjust_type=%s",
                (repository.config.adjust_type,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    return row["trade_date"]


def _stock_filters(
    keyword: str | None, exchange: str | None, risk: str | None
) -> tuple[str, list[Any]]:
    """构建股票基础信息筛选条件。"""

    clauses: list[str] = []
    params: list[Any] = []
    if keyword:
        clauses.append("(b.symbol LIKE %s OR b.name LIKE %s)")
        like = f"%{keyword.strip()}%"
        params.extend([like, like])
    if exchange:
        clauses.append("b.exchange=%s")
        params.append(exchange)
    if risk == "risk":
        clauses.append("(b.name LIKE '%%ST%%' OR b.name LIKE '%%退%%')")
    elif risk == "normal":
        clauses.append("b.name NOT LIKE '%%ST%%' AND b.name NOT LIKE '%%退%%'")
    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _json_rows(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """将数据库行转换为 JSON 友好的列表。"""

    return [_json_row(row) for row in rows]


def _json_row(row: dict[str, Any] | None) -> dict[str, Any]:
    """将数据库单行转换为 JSON 友好的字典。"""

    if not row:
        return {}
    return {key: _json_value(value) for key, value in row.items()}


def _json_value(value: Any) -> Any:
    """转换日期和 Decimal，避免 JSON 序列化失败。"""

    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
