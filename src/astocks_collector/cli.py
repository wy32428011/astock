"""A股采集器命令行入口。"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from astocks_collector.call_auction import CallAuctionSelector
from astocks_collector.analysis import T1StockAnalyzer
from astocks_collector.collector import CollectResult, StockCollector
from astocks_collector.config import AppConfig
from astocks_collector.db import MySQLRepository
from astocks_collector.realtime import RealtimeTradingEngine
from astocks_collector.scheduler import run_scheduler
from astocks_collector.t1_task_manager import (
    DEFAULT_PRESELECT_LIMIT,
    T1TaskConfig,
    normalize_interval_seconds,
    t1_task_manager,
)
from astocks_collector.three_day_analysis import ThreeDayTrendAnalyzer


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并执行对应采集任务。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    config = _load_config(args)
    _setup_logging(config.log_level)

    collector = StockCollector(config)
    if args.command == "init-db":
        collector.init_db()
        counts = MySQLRepository(config).table_counts()
        print("数据库和表结构已创建/确认：", counts)
        return 0

    if args.command == "sync-basic":
        result = collector.sync_basic()
        print(_format_result(result))
        return 0

    if args.command == "backfill":
        end_date = args.end_date or _today_text(config)
        start_date = args.start_date or (
            _years_ago_text(config, args.years) if args.years else config.default_start_date
        )
        result = collector.backfill(
            start_date=start_date,
            end_date=end_date,
            symbols=_parse_symbols(args.symbols),
            limit=args.limit,
        )
        print(_format_result(result))
        return 0

    if args.command == "incremental":
        result = collector.incremental(
            days=args.days,
            symbols=_parse_symbols(args.symbols),
            limit=args.limit,
            end_date=args.end_date,
        )
        print(_format_result(result))
        return 0

    if args.command == "scheduler":
        run_scheduler(config)
        return 0

    if args.command == "serve-api":
        import uvicorn

        _export_config_to_env(config)
        uvicorn.run(
            "astocks_collector.web_api:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0

    if args.command == "analyze-t1":
        result = T1StockAnalyzer(config).analyze(
            preselect_limit=args.preselect_limit,
            final_limit=args.final_limit,
            trade_date=args.trade_date,
            use_llm=not args.no_llm,
        )
        print(_format_analysis_result(result))
        return 0

    if args.command == "t1-loop":
        task_config = T1TaskConfig(
            interval_seconds=normalize_interval_seconds(args.interval_seconds),
            preselect_limit=args.preselect_limit,
            final_limit=args.final_limit,
            execute_trades=not args.no_trade,
            use_llm=not args.no_llm,
            run_analysis=not args.no_analysis,
        )
        status = t1_task_manager.start(config, task_config)
        print("T+1 长期模型任务已启动，按 Ctrl+C 手动停止：", _json_like(status))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            status = t1_task_manager.stop(config)
            print("T+1 长期模型任务已停止：", _json_like(status))
        return 0

    if args.command == "analyze-3d":
        result = ThreeDayTrendAnalyzer(config).analyze(
            preselect_limit=args.preselect_limit,
            final_limit=args.final_limit,
            trade_date=args.trade_date,
            use_llm=not args.no_llm,
        )
        print(_format_three_day_result(result))
        return 0

    if args.command == "realtime-once":
        result = RealtimeTradingEngine(config).run_once(
            limit=args.limit,
            execute_trades=not args.no_trade,
            decision_mode=args.decision_mode,
        )
        print(_format_realtime_result(result))
        return 0

    if args.command == "realtime-loop":
        RealtimeTradingEngine(config).run_loop(
            interval_seconds=args.interval_seconds,
            limit=args.limit,
            decision_mode=args.decision_mode,
        )
        return 0

    if args.command == "call-auction-once":
        result = CallAuctionSelector(config).run_once(
            final_limit=args.final_limit,
            force=args.force,
            trigger_type="cli",
        )
        print(_format_call_auction_result(result))
        return 0

    if args.command == "call-auction-loop":
        selector = CallAuctionSelector(config)
        interval = args.interval_seconds or config.call_auction_auto_interval_seconds
        print(f"集合竞价 LLM 快速选股循环已启动，间隔 {interval} 秒，按 Ctrl+C 停止")
        try:
            while True:
                result = selector.run_once(
                    final_limit=args.final_limit,
                    force=args.force,
                    trigger_type="loop",
                )
                print(_format_call_auction_result(result))
                time.sleep(interval)
        except KeyboardInterrupt:
            print("集合竞价 LLM 快速选股循环已停止")
        return 0

    if args.command == "sim-reset":
        account = RealtimeTradingEngine(config).reset_account(
            initial_cash=args.initial_cash
        )
        print("模拟账户已重置：", _json_like(account))
        return 0

    if args.command == "status":
        repository = MySQLRepository(config)
        repository.ensure_schema()
        counts = repository.table_counts()
        print("核心表行数：", counts)
        return 0

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="A股历史与每日行情数据采集器")
    parser.add_argument("--env-file", default=None, help="指定 .env 配置文件路径")
    parser.add_argument(
        "--adjust",
        choices=["none", "qfq", "hfq"],
        default=None,
        help="复权类型: none=不复权, qfq=前复权, hfq=后复权",
    )
    parser.add_argument(
        "--daily-provider",
        choices=["auto", "eastmoney", "sina"],
        default=None,
        help="日线数据源: sina=新浪, eastmoney=东方财富, auto=自动降级",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="创建数据库和表结构")
    subparsers.add_parser("sync-basic", help="同步沪深京 A股基础信息")

    backfill = subparsers.add_parser("backfill", help="回填历史日线行情")
    backfill.add_argument("--start-date", default=None, help="开始日期，格式 YYYYMMDD")
    backfill.add_argument("--end-date", default=None, help="结束日期，格式 YYYYMMDD")
    backfill.add_argument("--years", type=int, default=None, help="回填最近 N 年数据")
    backfill.add_argument("--symbols", default=None, help="逗号分隔股票代码")
    backfill.add_argument("--limit", type=int, default=None, help="仅采集前 N 只股票")

    incremental = subparsers.add_parser("incremental", help="采集最近窗口日线行情")
    incremental.add_argument("--days", type=int, default=None, help="最近 N 天窗口")
    incremental.add_argument("--end-date", default=None, help="窗口结束日期，格式 YYYYMMDD")
    incremental.add_argument("--symbols", default=None, help="逗号分隔股票代码")
    incremental.add_argument("--limit", type=int, default=None, help="仅采集前 N 只股票")

    subparsers.add_parser("scheduler", help="启动每日增量采集常驻调度器")
    serve_api = subparsers.add_parser("serve-api", help="启动前端页面数据 API")
    serve_api.add_argument("--host", default="127.0.0.1", help="API 监听地址")
    serve_api.add_argument("--port", type=int, default=8000, help="API 监听端口")
    serve_api.add_argument("--reload", action="store_true", help="开启开发热重载")
    analyze = subparsers.add_parser("analyze-t1", help="使用历史数据和大模型选择 T+1 候选")
    analyze.add_argument("--trade-date", default=None, help="分析交易日，默认最新交易日")
    analyze.add_argument("--preselect-limit", type=int, default=None, help="量化预筛数量")
    analyze.add_argument("--final-limit", type=int, default=None, help="最终输出数量")
    analyze.add_argument(
        "--no-llm",
        action="store_true",
        help="只使用量化预筛，不调用大模型",
    )
    t1_loop = subparsers.add_parser(
        "t1-loop", help="长期运行 T+1 模型和模拟任务，直到手动停止"
    )
    t1_loop.add_argument(
        "--interval-seconds", type=int, default=60, help="长期任务轮询间隔秒数"
    )
    t1_loop.add_argument(
        "--preselect-limit", type=int, default=DEFAULT_PRESELECT_LIMIT, help="量化预筛数量"
    )
    t1_loop.add_argument("--final-limit", type=int, default=20, help="最终候选数量")
    t1_loop.add_argument(
        "--no-llm", action="store_true", help="长期任务中不调用大模型"
    )
    t1_loop.add_argument(
        "--no-trade", action="store_true", help="长期任务中只刷新模型，不执行模拟交易"
    )
    t1_loop.add_argument(
        "--no-analysis", action="store_true", help="长期任务中不刷新 T+1 模型"
    )
    analyze_3d = subparsers.add_parser(
        "analyze-3d", help="筛选未来 3 个交易日涨势较好的全 A 股候选"
    )
    analyze_3d.add_argument("--trade-date", default=None, help="分析交易日，默认最新交易日")
    analyze_3d.add_argument("--preselect-limit", type=int, default=None, help="量化预筛数量")
    analyze_3d.add_argument("--final-limit", type=int, default=None, help="最终输出数量")
    analyze_3d.add_argument(
        "--no-llm",
        action="store_true",
        help="只使用量化预筛，不调用大模型",
    )
    realtime_once = subparsers.add_parser(
        "realtime-once", help="执行一次实时分析并模拟交易"
    )
    realtime_once.add_argument("--limit", type=int, default=None, help="实时行情分析数量")
    realtime_once.add_argument(
        "--decision-mode",
        choices=["rules", "llm_review"],
        default=None,
        help="实时交易决策模式",
    )
    realtime_once.add_argument(
        "--no-trade", action="store_true", help="只生成信号，不执行模拟交易"
    )
    realtime_loop = subparsers.add_parser(
        "realtime-loop", help="循环执行实时分析和模拟交易"
    )
    realtime_loop.add_argument("--limit", type=int, default=None, help="实时行情分析数量")
    realtime_loop.add_argument(
        "--decision-mode",
        choices=["rules", "llm_review"],
        default=None,
        help="实时交易决策模式",
    )
    realtime_loop.add_argument(
        "--interval-seconds", type=int, default=60, help="循环间隔秒数"
    )
    call_auction_once = subparsers.add_parser(
        "call-auction-once", help="执行一次集合竞价分阶段采集或快速选股"
    )
    call_auction_once.add_argument(
        "--final-limit", type=int, default=None, help="最终输出候选数量"
    )
    call_auction_once.add_argument(
        "--force", action="store_true", help="跳过时间窗口限制，仅用于调试补跑"
    )
    call_auction_loop = subparsers.add_parser(
        "call-auction-loop", help="循环执行集合竞价 LLM 快速选股"
    )
    call_auction_loop.add_argument(
        "--interval-seconds", type=int, default=None, help="循环间隔秒数"
    )
    call_auction_loop.add_argument(
        "--final-limit", type=int, default=None, help="最终输出候选数量"
    )
    call_auction_loop.add_argument(
        "--force", action="store_true", help="跳过时间窗口限制，仅用于调试补跑"
    )
    sim_reset = subparsers.add_parser("sim-reset", help="重置默认模拟交易账户")
    sim_reset.add_argument(
        "--initial-cash", type=float, default=None, help="重置后的初始资金"
    )
    subparsers.add_parser("status", help="查看核心表行数")
    return parser


def _load_config(args: argparse.Namespace) -> AppConfig:
    """读取配置并应用命令行覆盖项。"""

    config = AppConfig.from_env(args.env_file)
    adjust_type = None
    if getattr(args, "adjust", None) == "none":
        adjust_type = ""
    elif getattr(args, "adjust", None):
        adjust_type = args.adjust
    return config.with_overrides(
        adjust_type=adjust_type,
        daily_provider=getattr(args, "daily_provider", None),
    )


def _setup_logging(level: str) -> None:
    """初始化控制台日志格式。"""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _export_config_to_env(config: AppConfig) -> None:
    """将命令行覆盖后的配置写入环境变量，供 Uvicorn 子进程读取。"""

    values = {
        "MYSQL_HOST": config.mysql_host,
        "MYSQL_PORT": config.mysql_port,
        "MYSQL_USER": config.mysql_user,
        "MYSQL_PASSWORD": config.mysql_password,
        "MYSQL_DATABASE": config.mysql_database,
        "MYSQL_CHARSET": config.mysql_charset,
        "ASTOCKS_ADJUST": config.adjust_type,
        "ASTOCKS_DAILY_PROVIDER": config.daily_provider,
        "ASTOCKS_DEFAULT_START_DATE": config.default_start_date,
        "ASTOCKS_DAILY_WINDOW_DAYS": config.daily_window_days,
        "ASTOCKS_REQUEST_INTERVAL_SECONDS": config.request_interval_seconds,
        "ASTOCKS_MAX_RETRIES": config.max_retries,
        "ASTOCKS_MAX_WORKERS": config.max_workers,
        "ASTOCKS_BATCH_SIZE": config.batch_size,
        "ASTOCKS_SCHEDULE_TIME": config.schedule_time,
        "ASTOCKS_TIMEZONE": config.timezone,
        "LOG_LEVEL": config.log_level,
        "LLM_BASE_URL": config.llm_base_url,
        "LLM_MODEL": config.llm_model,
        "LLM_API_KEY": config.llm_api_key,
        "LLM_TIMEOUT_SECONDS": config.llm_timeout_seconds,
        "ANALYSIS_LOOKBACK_DAYS": config.analysis_lookback_days,
        "ANALYSIS_PRESELECT_LIMIT": config.analysis_preselect_limit,
        "ANALYSIS_FINAL_LIMIT": config.analysis_final_limit,
        "THREE_DAY_ANALYSIS_LOOKBACK_DAYS": config.three_day_analysis_lookback_days,
        "THREE_DAY_PRESELECT_LIMIT": config.three_day_preselect_limit,
        "THREE_DAY_FINAL_LIMIT": config.three_day_final_limit,
        "REALTIME_QUOTE_LIMIT": config.realtime_quote_limit,
        "REALTIME_SIGNAL_LIMIT": config.realtime_signal_limit,
        "REALTIME_DECISION_MODE": config.realtime_decision_mode,
        "REALTIME_AUTO_INTERVAL_SECONDS": config.realtime_auto_interval_seconds,
        "REALTIME_LLM_CANDIDATE_LIMIT": config.realtime_llm_candidate_limit,
        "CALL_AUCTION_ENABLED": config.call_auction_enabled,
        "CALL_AUCTION_START_TIME": config.call_auction_start_time,
        "CALL_AUCTION_DECISION_START_TIME": config.call_auction_decision_start_time,
        "CALL_AUCTION_END_TIME": config.call_auction_end_time,
        "CALL_AUCTION_AUTO_INTERVAL_SECONDS": config.call_auction_auto_interval_seconds,
        "CALL_AUCTION_PRESELECT_LIMIT": config.call_auction_preselect_limit,
        "CALL_AUCTION_LLM_REVIEW_LIMIT": config.call_auction_llm_review_limit,
        "CALL_AUCTION_FINAL_LIMIT": config.call_auction_final_limit,
        "CALL_AUCTION_REQUIRE_LLM": config.call_auction_require_llm,
        "CALL_AUCTION_LLM_TIMEOUT_SECONDS": config.call_auction_llm_timeout_seconds,
        "CALL_AUCTION_MIN_PCT_CHANGE": config.call_auction_min_pct_change,
        "CALL_AUCTION_MAX_PCT_CHANGE": config.call_auction_max_pct_change,
        "CALL_AUCTION_MIN_VOLUME_RATIO": config.call_auction_min_volume_ratio,
        "CALL_AUCTION_MIN_AMOUNT": config.call_auction_min_amount,
        "CALL_AUCTION_MIN_FINAL_SCORE": config.call_auction_min_final_score,
        "SIMULATION_INITIAL_CASH": config.simulation_initial_cash,
        "SIMULATION_ORDER_CASH_PCT": config.simulation_order_cash_pct,
        "SIMULATION_MAX_POSITIONS": config.simulation_max_positions,
        "SIMULATION_FEE_RATE": config.simulation_fee_rate,
    }
    for name, value in values.items():
        os.environ[name] = str(value)


def _parse_symbols(symbols_text: str | None) -> list[str] | None:
    """解析逗号分隔的股票代码参数。"""

    if not symbols_text:
        return None
    return [item.strip() for item in symbols_text.replace(";", ",").split(",") if item.strip()]


def _today_text(config: AppConfig) -> str:
    """返回配置时区下的当前日期。"""

    return datetime.now(ZoneInfo(config.timezone)).strftime("%Y%m%d")


def _years_ago_text(config: AppConfig, years: int) -> str:
    """返回配置时区下 N 年前的日期。"""

    if years <= 0:
        raise ValueError("--years 必须是正整数")
    today = datetime.now(ZoneInfo(config.timezone)).date()
    try:
        target = today.replace(year=today.year - years)
    except ValueError:
        target = today.replace(month=2, day=28, year=today.year - years)
    return target.strftime("%Y%m%d")


def _format_result(result: CollectResult) -> str:
    """格式化采集结果，便于命令行查看。"""

    return (
        f"任务={result.task_type} 状态={result.status} "
        f"股票数={result.total_symbols} 成功={result.success_symbols} "
        f"失败={result.failed_symbols} 行情行数={result.rows_written}"
    )


def _format_analysis_result(result) -> str:
    """格式化 T+1 分析结果。"""

    lines = [
        (
            f"分析日期={result.analysis_date} 交易日={result.trade_date} "
            f"预筛={result.preselect_count} 最终={result.final_count}"
        )
    ]
    for pick in result.picks:
        lines.append(
            f"{pick['rank_no']:02d}. {pick['symbol']} {pick['name']} "
            f"综合={pick['final_score']} 量化={pick['quant_score']} "
            f"LLM={pick['llm_score']} 理由={pick['reason']} 风险={pick['risk']}"
        )
    return "\n".join(lines)


def _format_three_day_result(result) -> str:
    """格式化未来 3 个交易日涨势分析结果。"""

    lines = [
        (
            f"未来3日分析日期={result.analysis_date} 交易日={result.trade_date} "
            f"预筛={result.preselect_count} 最终={result.final_count} "
            f"LLM降级={result.llm_fallback}"
        )
    ]
    for pick in result.picks:
        lines.append(
            f"{pick['rank_no']:02d}. {pick['symbol']} {pick['name']} "
            f"综合={pick['final_score']} 量化={pick['quant_score']} "
            f"LLM={pick['llm_score']} 方向={pick['expected_direction']} "
            f"理由={pick['reason']} 风险={pick['risk']}"
        )
    return "\n".join(lines)


def _format_realtime_result(result) -> str:
    """格式化实时分析和模拟交易结果。"""

    return (
        f"快照={result.snapshot_time} 行情={result.quote_count} "
        f"信号={result.signal_count} 买入={result.buy_count} "
        f"卖出={result.sell_count} 决策={result.decision_count} "
        f"订单={result.order_count} 模式={result.decision_mode} "
        f"LLM使用={result.llm_used} LLM降级={result.llm_fallback} "
        f"开盘={result.market_open} 跳过={result.skipped} {result.skip_reason}"
    )


def _format_call_auction_result(result) -> str:
    """格式化集合竞价 LLM 快速选股结果。"""

    summary = result.summary or {}
    quote_count = summary.get("quoteSnapshotCount", "-")
    llm_fallback = bool(summary.get("llmFallback"))
    return (
        f"快照={result.snapshot_time} 状态={result.status} 行情={result.quote_count} "
        f"有效={result.valid_quote_count} 规则候选={result.candidate_count} "
        f"LLM通过={result.pick_count} LLM成功={result.llm_success} "
        f"LLM降级={llm_fallback} 融合快照={quote_count} "
        f"跳过={result.skipped} {result.skip_reason or result.error_message}"
    )


def _json_like(row: dict) -> dict:
    """将命令行输出中的 Decimal 和日期转换为易读文本。"""

    result = {}
    for key, value in row.items():
        result[key] = str(value) if not isinstance(value, (str, int, float)) else value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
