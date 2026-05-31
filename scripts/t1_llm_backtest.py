"""T+1 量化与 LLM 复核历史收益对比脚本。"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pymysql

from astocks_collector.config import AppConfig
from astocks_collector.llm_client import LLMClient


@dataclass(frozen=True)
class BacktestArgs:
    """回测命令行参数。"""

    days: int
    top_n: int
    preselect_limit: int
    use_llm: bool
    llm_timeout: int
    review_mode: str
    cache_input: str | None
    cache_output: str | None
    grid_search: bool
    output: str | None


@dataclass(frozen=True)
class ReviewParams:
    """LLM 保守复核参数。"""

    pool_multiplier: int
    score_weight: float
    max_adjustment: float
    rank_penalty: float
    avoid_penalty: float
    protected_top_n: int
    entry_limit_multiplier: int
    allowed_actions: tuple[str, ...]


def parse_args() -> BacktestArgs:
    """解析 T+1 LLM 回测参数。"""

    parser = argparse.ArgumentParser(description="验证 T+1 加入 LLM 复核后的历史收益")
    parser.add_argument("--days", type=int, default=60, help="回测最近多少个完整信号日")
    parser.add_argument("--top-n", type=int, default=10, help="每日最终选股数量")
    parser.add_argument("--preselect-limit", type=int, default=80, help="每日交给 LLM 的预筛数量")
    parser.add_argument("--no-llm", action="store_true", help="只跑量化基线，不调用 LLM")
    parser.add_argument("--llm-timeout", type=int, default=30, help="单次 LLM 请求超时时间")
    parser.add_argument(
        "--review-mode",
        choices=["direct", "conservative"],
        default="conservative",
        help="direct 为 LLM 直接选 TopN；conservative 为量化锚定小幅复核",
    )
    parser.add_argument("--cache-input", help="读取已有 LLM 复核缓存，避免重复调用模型")
    parser.add_argument("--cache-output", help="保存本次 LLM 复核缓存")
    parser.add_argument("--grid-search", action="store_true", help="基于 LLM 缓存搜索保守复核参数")
    parser.add_argument("--output", help="可选：将 JSON 结果写入指定文件")
    parsed = parser.parse_args()
    return BacktestArgs(
        days=parsed.days,
        top_n=parsed.top_n,
        preselect_limit=parsed.preselect_limit,
        use_llm=not parsed.no_llm,
        llm_timeout=parsed.llm_timeout,
        review_mode=parsed.review_mode,
        cache_input=parsed.cache_input,
        cache_output=parsed.cache_output,
        grid_search=parsed.grid_search,
        output=parsed.output,
    )


def params_from_config(config: AppConfig) -> ReviewParams:
    """从应用配置构造回测复核参数。"""

    return ReviewParams(
        pool_multiplier=config.t1_llm_review_pool_multiplier,
        score_weight=config.t1_llm_score_weight,
        max_adjustment=config.t1_llm_max_adjustment,
        rank_penalty=config.t1_llm_rank_penalty,
        avoid_penalty=config.t1_llm_avoid_penalty,
        protected_top_n=config.t1_llm_protected_top_n,
        entry_limit_multiplier=config.t1_llm_entry_limit_multiplier,
        allowed_actions=tuple(sorted(config.t1_llm_allowed_action_set())),
    )


def clamp(value: float, low: float, high: float) -> float:
    """将数值限制在指定区间内。"""

    return max(low, min(high, value))


def pct_return(closes: np.ndarray, index: int, periods: int) -> float:
    """计算指定周期收益率百分比。"""

    if index <= periods:
        return 0.0
    base = float(closes[index - periods - 1])
    latest = float(closes[index])
    return 0.0 if base == 0 else (latest / base - 1) * 100


def max_drawdown(values: np.ndarray) -> float:
    """计算窗口最大回撤百分比。"""

    running_max = np.maximum.accumulate(values)
    return float(np.min(values / running_max - 1) * 100)


def score_candidate(
    ret_5: float,
    ret_10: float,
    ret_20: float,
    close: float,
    ma5: float,
    ma10: float,
    ma20: float,
    volume_ratio: float,
    volatility_20: float,
    max_drawdown_20: float,
    latest_pct: float,
    turnover: float,
) -> float:
    """复刻现有 T+1 分析器的量化评分。"""

    score = 45.0
    score += clamp(ret_5, -8, 8)
    score += clamp(ret_10, -12, 12) * 0.55
    score += clamp(ret_20, -18, 18) * 0.25
    if close > ma5 > ma10 > ma20:
        score += 10
    elif close > ma20:
        score += 5
    score += clamp((volume_ratio - 1) * 10, -6, 8)
    score -= max(0.0, volatility_20 - 4.5) * 1.8
    score += clamp(max_drawdown_20 + 12, -6, 4)
    if 0.5 <= latest_pct <= 5.5:
        score += 5
    elif latest_pct > 7:
        score -= 8
    if 1 <= turnover <= 12:
        score += 3
    return clamp(score, 0, 100)


def net_return(buy_price: float, sell_price: float, fee_rate: float) -> float | None:
    """计算扣双边手续费后的买卖收益率。"""

    if (
        buy_price is None
        or sell_price is None
        or buy_price <= 0
        or sell_price <= 0
        or math.isnan(buy_price)
        or math.isnan(sell_price)
    ):
        return None
    return ((sell_price * (1 - fee_rate)) / (buy_price * (1 + fee_rate)) - 1) * 100


def summary(values: list[float]) -> dict[str, Any]:
    """汇总单笔交易收益统计。"""

    vals = [float(value) for value in values if value is not None and not math.isnan(float(value))]
    if not vals:
        return {
            "count": 0,
            "win_rate_pct": None,
            "avg_pct": None,
            "median_pct": None,
            "p25_pct": None,
            "p75_pct": None,
            "min_pct": None,
            "max_pct": None,
        }
    series = pd.Series(vals, dtype="float64")
    return {
        "count": int(series.count()),
        "win_rate_pct": round(float((series > 0).mean() * 100), 4),
        "avg_pct": round(float(series.mean()), 4),
        "median_pct": round(float(series.median()), 4),
        "p25_pct": round(float(series.quantile(0.25)), 4),
        "p75_pct": round(float(series.quantile(0.75)), 4),
        "min_pct": round(float(series.min()), 4),
        "max_pct": round(float(series.max()), 4),
    }


def portfolio_stats(values: list[float]) -> dict[str, Any]:
    """汇总每日等权组合收益、累计收益和回撤。"""

    vals = [float(value) for value in values if value is not None and not math.isnan(float(value))]
    if not vals:
        return {
            "days": 0,
            "daily_win_rate_pct": None,
            "avg_daily_pct": None,
            "median_daily_pct": None,
            "cumulative_pct": None,
            "max_drawdown_pct": None,
        }
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in vals:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)
    series = pd.Series(vals, dtype="float64")
    return {
        "days": len(vals),
        "daily_win_rate_pct": round(float((series > 0).mean() * 100), 4),
        "avg_daily_pct": round(float(series.mean()), 4),
        "median_daily_pct": round(float(series.median()), 4),
        "cumulative_pct": round((equity - 1) * 100, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
    }


def load_market_data(
    config: AppConfig, days: int
) -> tuple[pd.DataFrame, dict[str, Any], list[Any], list[Any]]:
    """从 MySQL 读取回测所需的基础信息和日线行情。"""

    conn = pymysql.connect(**config.mysql_kwargs(), cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    MIN(trade_date) min_date,
                    MAX(trade_date) max_date,
                    COUNT(*) rows_count,
                    COUNT(DISTINCT symbol) symbols_count
                FROM stock_daily
                WHERE adjust_type=%s
                """,
                (config.adjust_type,),
            )
            range_row = cursor.fetchone()
            cursor.execute(
                "SELECT DISTINCT trade_date FROM stock_daily WHERE adjust_type=%s ORDER BY trade_date",
                (config.adjust_type,),
            )
            all_trade_dates = [row["trade_date"] for row in cursor.fetchall()]
            signal_dates = all_trade_dates[:-2][-days:]
            min_signal = signal_dates[0]
            max_sell = all_trade_dates[all_trade_dates.index(signal_dates[-1]) + 2]

            cursor.execute("SELECT symbol, name FROM stock_basic WHERE is_active=1")
            basic = {
                str(row["symbol"]): str(row["name"])
                for row in cursor.fetchall()
                if "ST" not in str(row["name"])
                and "退" not in str(row["name"])
                and not str(row["name"]).startswith(("N", "C"))
            }
            cursor.execute(
                """
                SELECT
                    symbol,
                    trade_date,
                    open_price,
                    close_price,
                    high_price,
                    low_price,
                    volume,
                    amount,
                    pct_change,
                    turnover_rate
                FROM stock_daily FORCE INDEX (idx_stock_daily_trade_date)
                WHERE trade_date <= %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL %s DAY)
                  AND adjust_type=%s
                  AND open_price > 0
                  AND close_price > 0
                  AND high_price >= GREATEST(open_price, close_price, low_price)
                  AND low_price <= LEAST(open_price, close_price, high_price)
                  AND volume > 0
                  AND amount > 0
                """,
                (max_sell, min_signal, config.analysis_lookback_days * 2, config.adjust_type),
            )
            rows = cursor.fetchall()
    finally:
        conn.close()

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("没有读取到可回测行情")
    frame["symbol"] = frame["symbol"].astype(str)
    frame = frame[frame["symbol"].isin(basic.keys())].copy()
    frame["name"] = frame["symbol"].map(basic)
    for column in [
        "open_price",
        "close_price",
        "high_price",
        "low_price",
        "volume",
        "amount",
        "pct_change",
        "turnover_rate",
    ]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.date
    frame = frame.dropna(
        subset=["trade_date", "open_price", "close_price", "volume", "amount"]
    )
    frame = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    return frame, range_row, all_trade_dates, signal_dates


def build_candidates(frame: pd.DataFrame, signal_dates: list[Any]) -> dict[Any, list[dict[str, Any]]]:
    """根据历史窗口为每个信号日生成 T+1 量化候选。"""

    signal_set = set(signal_dates)
    candidates_by_date: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for symbol, group in frame.groupby("symbol", sort=False):
        dates = group["trade_date"].to_numpy()
        closes = group["close_price"].astype(float).to_numpy()
        volumes = group["volume"].astype(float).to_numpy()
        pct_changes = group["pct_change"].fillna(0).astype(float).to_numpy()
        turnovers = group["turnover_rate"].fillna(0).astype(float).to_numpy()
        names = group["name"].astype(str).to_numpy()
        for index, trade_date in enumerate(dates):
            if trade_date not in signal_set or index + 1 < 30:
                continue
            close = float(closes[index])
            latest_pct = float(pct_changes[index])
            if close <= 0 or latest_pct > 9.6 or latest_pct < -8.5:
                continue
            last20 = closes[index - 19 : index + 1]
            last21 = closes[index - 20 : index + 1] if index >= 20 else closes[: index + 1]
            if len(last20) < 20 or len(last21) < 21:
                continue
            ma5 = float(np.mean(closes[index - 4 : index + 1]))
            ma10 = float(np.mean(closes[index - 9 : index + 1]))
            ma20 = float(np.mean(last20))
            volume20 = float(np.mean(volumes[index - 19 : index + 1]))
            volume_ratio = (
                float(np.mean(volumes[index - 4 : index + 1]) / volume20)
                if volume20
                else 0.0
            )
            pct_window = np.diff(last21) / last21[:-1]
            volatility_20 = (
                float(np.std(pct_window, ddof=1) * 100) if len(pct_window) >= 2 else 0.0
            )
            max_drawdown_20 = max_drawdown(last20)
            if volatility_20 > 8.5 or max_drawdown_20 < -25:
                continue

            ret_5 = pct_return(closes, index, 5)
            ret_10 = pct_return(closes, index, 10)
            ret_20 = pct_return(closes, index, 20)
            turnover = float(turnovers[index])
            quant_score = score_candidate(
                ret_5,
                ret_10,
                ret_20,
                close,
                ma5,
                ma10,
                ma20,
                volume_ratio,
                volatility_20,
                max_drawdown_20,
                latest_pct,
                turnover,
            )
            if quant_score < 45:
                continue
            candidates_by_date[trade_date].append(
                {
                    "symbol": str(symbol),
                    "name": str(names[index]),
                    "quant_score": round(quant_score, 4),
                    "close_price": round(close, 4),
                    "pct_change": round(latest_pct, 4),
                    "turnover_rate": round(turnover, 4),
                    "ret_5": round(ret_5, 4),
                    "ret_10": round(ret_10, 4),
                    "ret_20": round(ret_20, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "volatility_20": round(volatility_20, 4),
                    "max_drawdown_20": round(max_drawdown_20, 4),
                }
            )
    for trade_date in list(candidates_by_date):
        candidates_by_date[trade_date].sort(
            key=lambda item: item["quant_score"], reverse=True
        )
    return candidates_by_date


def ask_llm_for_picks(
    client: LLMClient,
    trade_date: Any,
    candidates: list[dict[str, Any]],
    final_limit: int,
    review_mode: str,
) -> tuple[list[str], dict[str, Any]]:
    """请求 LLM 复核 T+1 候选名单。"""

    payload_candidates = [
        {
            "symbol": item["symbol"],
            "name": item["name"],
            "quant_score": item["quant_score"],
            "pct_change": item["pct_change"],
            "turnover_rate": item["turnover_rate"],
            "ret_5": item["ret_5"],
            "ret_10": item["ret_10"],
            "ret_20": item["ret_20"],
            "volume_ratio": item["volume_ratio"],
            "volatility_20": item["volatility_20"],
            "max_drawdown_20": item["max_drawdown_20"],
        }
        for item in candidates
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是A股T+1短线风险复核助手。只能基于候选池判断，不得编造股票。"
                "不要使用未来信息。历史模拟显示BOOST容易放大回撤，默认把可交易候选标为KEEP，"
                "只有极少数低风险共振才可BOOST；追高、放量过猛或回撤恶化时输出DOWNRANK或AVOID。"
                "只输出JSON数组。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": (
                        "从量化预筛候选中直接选出T+1名单"
                        if review_mode == "direct"
                        else "对量化前排候选做保守小幅复核"
                    ),
                    "trade_date": trade_date.isoformat(),
                    "final_limit": final_limit,
                    "execution_rule": "D日收盘后出信号，D+1开盘买入，D+2开盘卖出",
                    "preference": [
                        "量化排序是主依据，只有明显风险或明显共振才调整",
                        "优先保留量化前10，不要为了多样性替换强势候选",
                        "趋势延续但不过度追高，量能温和放大，波动率和回撤可控",
                        "KEEP表示可进入模拟交易候选，BOOST只用于极高置信且风险更低的例外情形",
                        "如果上涨来自单日脉冲、波动突然放大或20日回撤压力较大，不要输出BOOST",
                        "action只能是BOOST、KEEP、DOWNRANK、AVOID",
                    ],
                    "output_schema": [
                        {
                            "symbol": "六位股票代码",
                            "llm_score": 0,
                            "action": "BOOST|KEEP|DOWNRANK|AVOID",
                            "reason": "20字以内",
                            "risk": "20字以内",
                        }
                    ],
                    "candidates": payload_candidates,
                },
                ensure_ascii=False,
            ),
        },
    ]
    raw_payload = client.chat_json(messages, max_tokens=1536)
    if isinstance(raw_payload, dict):
        raw_payload = raw_payload.get("picks") or raw_payload.get("data") or []
    if not isinstance(raw_payload, list):
        raise RuntimeError("LLM JSON 响应不是数组")

    allowed = {item["symbol"] for item in candidates}
    symbols: list[str] = []
    detail: dict[str, Any] = {}
    for row in raw_payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).zfill(6)
        if symbol in allowed and symbol not in symbols:
            symbols.append(symbol)
            detail[symbol] = {
                "llm_score": row.get("llm_score"),
                "action": row.get("action"),
                "reason": row.get("reason"),
                "risk": row.get("risk"),
            }
        if review_mode == "direct" and len(symbols) >= final_limit:
            break
    return symbols, detail


def select_conservative_picks(
    candidates: list[dict[str, Any]],
    llm_detail: dict[str, Any],
    top_n: int,
    params: ReviewParams,
) -> list[dict[str, Any]]:
    """用量化锚定的小幅 LLM 调整选择最终候选。"""

    rows: list[dict[str, Any]] = []
    review_limit = max(top_n, top_n * params.pool_multiplier)
    entry_limit = max(top_n, top_n * params.entry_limit_multiplier)
    for rank_index, candidate in enumerate(candidates, start=1):
        detail = llm_detail.get(candidate["symbol"], {})
        llm_score = read_llm_score(detail, candidate["quant_score"])
        adjustment = clamp(
            (llm_score - candidate["quant_score"]) * params.score_weight,
            -params.max_adjustment,
            params.max_adjustment,
        )
        action = str(detail.get("action") or "KEEP").upper()
        if action not in params.allowed_actions:
            continue
        if action == "BOOST":
            adjustment = min(
                params.max_adjustment,
                adjustment + params.max_adjustment * 0.25,
            )
        elif action == "DOWNRANK":
            adjustment = max(
                -params.max_adjustment,
                adjustment - params.max_adjustment * 0.5,
            )
        elif action == "AVOID":
            adjustment = -params.max_adjustment - params.avoid_penalty
        if rank_index <= params.protected_top_n and action != "AVOID":
            adjustment = max(0.0, adjustment)
        if rank_index > review_limit:
            adjustment = min(0.0, adjustment)
        final_score = (
            candidate["quant_score"]
            + adjustment
            - (rank_index - 1) * params.rank_penalty
        )
        if rank_index > entry_limit:
            final_score -= 100.0
        merged = dict(candidate)
        merged.update(
            {
                "llm_score": llm_score,
                "action": action,
                "final_score": round(final_score, 4),
                "reason": detail.get("reason"),
                "risk": detail.get("risk"),
            }
        )
        rows.append(merged)
    rows.sort(key=lambda item: item["final_score"], reverse=True)
    return rows[:top_n]


def read_llm_score(detail: dict[str, Any], fallback: float) -> float:
    """读取 LLM 分数，异常时退回量化分。"""

    try:
        return clamp(float(detail.get("llm_score")), 0, 100)
    except (TypeError, ValueError):
        return fallback


def build_market_maps(frame: pd.DataFrame) -> tuple[dict[tuple[str, Any], dict[str, float]], dict[Any, set[str]]]:
    """构建价格索引和每日可交易股票集合。"""

    price_map = {
        (str(row.symbol), row.trade_date): {
            "open": float(row.open_price),
            "close": float(row.close_price),
        }
        for row in frame[["symbol", "trade_date", "open_price", "close_price"]].itertuples(
            index=False
        )
    }
    symbols_by_date: dict[Any, set[str]] = defaultdict(set)
    for row in frame[["symbol", "trade_date"]].itertuples(index=False):
        symbols_by_date[row.trade_date].add(str(row.symbol))
    return price_map, symbols_by_date


def evaluate_picks(
    selected_by_date: dict[Any, list[dict[str, Any]]],
    signal_dates: list[Any],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    fee_rate: float,
) -> dict[str, Any]:
    """按严格 T+1 开盘买入、次交易日开盘卖出评价候选组合。"""

    trade_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
    trades: list[dict[str, Any]] = []
    daily_returns: list[float] = []
    for signal_date in signal_dates:
        index = trade_index[signal_date]
        buy_date = all_trade_dates[index + 1]
        sell_date = all_trade_dates[index + 2]
        day_returns: list[float] = []
        for rank, pick in enumerate(selected_by_date.get(signal_date, []), start=1):
            symbol = pick["symbol"]
            buy_price = price_map.get((symbol, buy_date))
            sell_price = price_map.get((symbol, sell_date))
            if not buy_price or not sell_price:
                continue
            ret = net_return(buy_price["open"], sell_price["open"], fee_rate)
            if ret is None:
                continue
            day_returns.append(ret)
            trades.append(
                {
                    "signal_date": signal_date.isoformat(),
                    "buy_date": buy_date.isoformat(),
                    "sell_date": sell_date.isoformat(),
                    "symbol": symbol,
                    "name": pick["name"],
                    "rank": rank,
                    "quant_score": pick["quant_score"],
                    "llm_score": pick.get("llm_score"),
                    "strict_return_pct": round(ret, 4),
                }
            )
        if day_returns:
            daily_returns.append(float(np.mean(day_returns)))
    return {
        "trade_summary": summary([item["strict_return_pct"] for item in trades]),
        "daily_portfolio": portfolio_stats(daily_returns),
        "trades": trades,
    }


def evaluate_benchmark(
    signal_dates: list[Any],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    symbols_by_date: dict[Any, set[str]],
    fee_rate: float,
) -> dict[str, Any]:
    """计算同过滤股票池全市场等权基准。"""

    trade_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
    daily_returns: list[float] = []
    for signal_date in signal_dates:
        buy_date = all_trade_dates[trade_index[signal_date] + 1]
        sell_date = all_trade_dates[trade_index[signal_date] + 2]
        returns: list[float] = []
        for symbol in symbols_by_date.get(signal_date, set()):
            buy_price = price_map.get((symbol, buy_date))
            sell_price = price_map.get((symbol, sell_date))
            if not buy_price or not sell_price:
                continue
            ret = net_return(buy_price["open"], sell_price["open"], fee_rate)
            if ret is not None:
                returns.append(ret)
        if returns:
            daily_returns.append(float(np.mean(returns)))
    return portfolio_stats(daily_returns)


def build_selected_with_params(
    candidates_by_date: dict[Any, list[dict[str, Any]]],
    llm_details_by_date: dict[str, dict[str, Any]],
    signal_dates: list[Any],
    preselect_limit: int,
    top_n: int,
    params: ReviewParams,
) -> tuple[dict[Any, list[dict[str, Any]]], float, int]:
    """按指定参数生成保守 LLM 复核选股结果。"""

    selected: dict[Any, list[dict[str, Any]]] = {}
    overlaps: list[int] = []
    changed_days = 0
    for signal_date in signal_dates:
        candidates = candidates_by_date.get(signal_date, [])
        preselected = candidates[:preselect_limit]
        quant_picks = preselected[:top_n]
        llm_detail = llm_details_by_date.get(signal_date.isoformat(), {})
        selected_rows = select_conservative_picks(preselected, llm_detail, top_n, params)
        selected[signal_date] = selected_rows
        overlap = len(
            {item["symbol"] for item in quant_picks}
            & {item["symbol"] for item in selected_rows}
        )
        overlaps.append(overlap)
        if overlap < min(len(quant_picks), top_n):
            changed_days += 1
    average_overlap = round(float(np.mean(overlaps)), 4) if overlaps else 0.0
    return selected, average_overlap, changed_days


def grid_search_params(
    candidates_by_date: dict[Any, list[dict[str, Any]]],
    llm_details_by_date: dict[str, dict[str, Any]],
    signal_dates: list[Any],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    fee_rate: float,
    preselect_limit: int,
    top_n: int,
    baseline_quant: dict[str, Any],
) -> dict[str, Any]:
    """基于缓存的 LLM 判断搜索收益和胜率更优的参数组合。"""

    prepared_days = _prepare_grid_search_days(
        candidates_by_date,
        llm_details_by_date,
        signal_dates,
        all_trade_dates,
        price_map,
        fee_rate,
        preselect_limit,
        top_n,
    )
    candidates: list[dict[str, Any]] = []
    for pool in [2, 3, 4]:
        for weight in [0.04, 0.08, 0.12, 0.16, 0.20]:
            for max_adjust in [0.8, 1.2, 1.6, 2.0, 2.5]:
                for rank_penalty in [0.02, 0.03, 0.05, 0.08, 0.12]:
                    for avoid in [2.0, 3.0, 4.0, 6.0]:
                        for protected in [0, 2, 3, 5, 8]:
                            for entry in [1, 2, 3]:
                                params = ReviewParams(
                                    pool_multiplier=pool,
                                    score_weight=weight,
                                    max_adjustment=max_adjust,
                                    rank_penalty=rank_penalty,
                                    avoid_penalty=avoid,
                                    protected_top_n=protected,
                                    entry_limit_multiplier=entry,
                                )
                                trade, daily, avg_overlap, changed_days = (
                                    _evaluate_grid_params(prepared_days, params, top_n)
                                )
                                candidates.append(
                                    {
                                        "params": params.__dict__,
                                        "trade_win_rate_pct": trade["win_rate_pct"],
                                        "trade_avg_pct": trade["avg_pct"],
                                        "daily_win_rate_pct": daily["daily_win_rate_pct"],
                                        "cumulative_pct": daily["cumulative_pct"],
                                        "max_drawdown_pct": daily["max_drawdown_pct"],
                                        "avg_top10_overlap": avg_overlap,
                                        "changed_days": changed_days,
                                        "objective": _objective_score(
                                            trade["win_rate_pct"],
                                            daily["cumulative_pct"],
                                            daily["max_drawdown_pct"],
                                            baseline_quant,
                                        ),
                                    }
                                )

    candidates.sort(key=lambda item: item["objective"], reverse=True)
    quant_trade = baseline_quant["trade_summary"]
    quant_daily = baseline_quant["daily_portfolio"]
    better_both = [
        item
        for item in candidates
        if item["trade_win_rate_pct"] >= quant_trade["win_rate_pct"]
        and item["cumulative_pct"] >= quant_daily["cumulative_pct"]
    ]
    better_both.sort(
        key=lambda item: (
            item["cumulative_pct"],
            item["trade_win_rate_pct"],
            item["max_drawdown_pct"],
        ),
        reverse=True,
    )
    by_return = sorted(candidates, key=lambda item: item["cumulative_pct"], reverse=True)
    by_win = sorted(candidates, key=lambda item: item["trade_win_rate_pct"], reverse=True)
    return {
        "best_objective": candidates[:10],
        "best_both": better_both[:10],
        "best_return": by_return[:10],
        "best_win_rate": by_win[:10],
        "searched_count": len(candidates),
    }


def _prepare_grid_search_days(
    candidates_by_date: dict[Any, list[dict[str, Any]]],
    llm_details_by_date: dict[str, dict[str, Any]],
    signal_dates: list[Any],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    fee_rate: float,
    preselect_limit: int,
    top_n: int,
) -> list[dict[str, Any]]:
    """预计算网格搜索需要的分数、动作和严格 T+1 收益数组。"""

    trade_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
    prepared_days: list[dict[str, Any]] = []
    for signal_date in signal_dates:
        rows = candidates_by_date.get(signal_date, [])[:preselect_limit]
        llm_detail = llm_details_by_date.get(signal_date.isoformat(), {})
        quant_scores: list[float] = []
        llm_scores: list[float] = []
        action_codes: list[int] = []
        returns: list[float] = []
        buy_date = all_trade_dates[trade_index[signal_date] + 1]
        sell_date = all_trade_dates[trade_index[signal_date] + 2]
        for row in rows:
            detail = llm_detail.get(row["symbol"], {})
            quant_score = float(row["quant_score"])
            quant_scores.append(quant_score)
            llm_scores.append(read_llm_score(detail, quant_score))
            action = str(detail.get("action") or "KEEP").upper()
            action_codes.append({"BOOST": 1, "DOWNRANK": 2, "AVOID": 3}.get(action, 0))
            buy_price = price_map.get((row["symbol"], buy_date))
            sell_price = price_map.get((row["symbol"], sell_date))
            ret = None
            if buy_price and sell_price:
                ret = net_return(buy_price["open"], sell_price["open"], fee_rate)
            returns.append(float("nan") if ret is None else round(float(ret), 4))
        prepared_days.append(
            {
                "quant_scores": np.array(quant_scores, dtype=float),
                "llm_scores": np.array(llm_scores, dtype=float),
                "action_codes": np.array(action_codes, dtype=np.int8),
                "returns": np.array(returns, dtype=float),
                "top_n": min(top_n, len(rows)),
            }
        )
    return prepared_days


def _evaluate_grid_params(
    prepared_days: list[dict[str, Any]],
    params: ReviewParams,
    top_n: int,
) -> tuple[dict[str, Any], dict[str, Any], float, int]:
    """用预计算数组快速评价一组 LLM 复核参数。"""

    trade_returns: list[float] = []
    daily_returns: list[float] = []
    overlaps: list[int] = []
    changed_days = 0
    for day in prepared_days:
        quant_scores = day["quant_scores"]
        if quant_scores.size == 0:
            continue
        llm_scores = day["llm_scores"]
        actions = day["action_codes"]
        ranks = np.arange(1, quant_scores.size + 1, dtype=float)
        adjustment = np.clip(
            (llm_scores - quant_scores) * params.score_weight,
            -params.max_adjustment,
            params.max_adjustment,
        )
        boost_mask = actions == 1
        downrank_mask = actions == 2
        avoid_mask = actions == 3
        adjustment[boost_mask] = np.minimum(
            params.max_adjustment,
            adjustment[boost_mask] + params.max_adjustment * 0.25,
        )
        adjustment[downrank_mask] = np.maximum(
            -params.max_adjustment,
            adjustment[downrank_mask] - params.max_adjustment * 0.5,
        )
        adjustment[avoid_mask] = -params.max_adjustment - params.avoid_penalty
        if params.protected_top_n > 0:
            protected_mask = (ranks <= params.protected_top_n) & ~avoid_mask
            adjustment[protected_mask] = np.maximum(0.0, adjustment[protected_mask])
        review_limit = max(top_n, top_n * params.pool_multiplier)
        outside_review_mask = ranks > review_limit
        adjustment[outside_review_mask] = np.minimum(0.0, adjustment[outside_review_mask])
        final_scores = quant_scores + adjustment - (ranks - 1) * params.rank_penalty
        entry_limit = max(top_n, top_n * params.entry_limit_multiplier)
        final_scores[ranks > entry_limit] -= 100.0
        selected_count = min(top_n, quant_scores.size)
        selected_indices = np.argsort(-final_scores, kind="mergesort")[:selected_count]
        valid_returns = day["returns"][selected_indices]
        valid_returns = valid_returns[np.isfinite(valid_returns)]
        if valid_returns.size:
            trade_returns.extend(float(value) for value in valid_returns)
            daily_returns.append(float(np.mean(valid_returns)))
        quant_top_n = day["top_n"]
        overlap = int(np.sum(selected_indices < quant_top_n))
        overlaps.append(overlap)
        if overlap < quant_top_n:
            changed_days += 1
    average_overlap = round(float(np.mean(overlaps)), 4) if overlaps else 0.0
    return (
        summary(trade_returns),
        portfolio_stats(daily_returns),
        average_overlap,
        changed_days,
    )


def _objective_score(
    trade_win_rate_pct: float | None,
    cumulative_pct: float | None,
    max_drawdown_pct: float | None,
    baseline_quant: dict[str, Any],
) -> float:
    """综合收益、胜率和回撤的参数搜索目标函数。"""

    quant_trade = baseline_quant["trade_summary"]
    quant_daily = baseline_quant["daily_portfolio"]
    win_delta = (trade_win_rate_pct or 0.0) - (quant_trade["win_rate_pct"] or 0.0)
    cumulative_delta = (cumulative_pct or 0.0) - (quant_daily["cumulative_pct"] or 0.0)
    drawdown_delta = (max_drawdown_pct or 0.0) - (quant_daily["max_drawdown_pct"] or 0.0)
    return cumulative_delta + win_delta * 1.2 + drawdown_delta * 0.5


def should_use_adaptive_llm(
    candidates: list[dict[str, Any]], top_n: int, config: AppConfig
) -> tuple[bool, dict[str, Any]]:
    """根据市场候选数量和量化前排热度决定是否启用 LLM 复核。"""

    if not config.t1_llm_adaptive_gate_enabled:
        return True, {"reason": "disabled"}
    if not candidates:
        return False, {"reason": "empty_candidates"}
    gate_top_n = getattr(config, "t1_llm_gate_top_n", top_n) or top_n
    top_rows = candidates[:gate_top_n]
    top_pct_avg = float(np.mean([row["pct_change"] for row in top_rows]))
    candidate_count = len(candidates)
    enabled = (
        candidate_count <= config.t1_llm_adaptive_candidate_count_max
        and top_pct_avg <= config.t1_llm_adaptive_top_pct_max
    )
    return enabled, {
        "candidate_count": candidate_count,
        "top_pct_avg": round(top_pct_avg, 4),
        "candidate_count_max": config.t1_llm_adaptive_candidate_count_max,
        "top_pct_max": config.t1_llm_adaptive_top_pct_max,
        "gate_top_n": gate_top_n,
    }


def apply_llm_strategy_filters(
    rows: list[dict[str, Any]], config: AppConfig, requested_top_n: int
) -> list[dict[str, Any]]:
    """应用生产 T+1 LLM 机会模式过滤：控制追高并限制最终持仓数量。"""

    filtered: list[dict[str, Any]] = []
    max_pct = getattr(config, "t1_llm_candidate_pct_change_max", 0.0)
    for row in rows:
        if max_pct > 0 and float(row.get("pct_change") or 0) > max_pct:
            continue
        filtered.append(row)

    limit = requested_top_n
    final_limit = getattr(config, "t1_llm_final_pick_limit", 0)
    if final_limit > 0:
        limit = min(limit, final_limit)
    return filtered[:limit]


def main() -> None:
    """执行 T+1 量化和 LLM 复核收益对比。"""

    args = parse_args()
    config = AppConfig.from_env()
    frame, range_row, all_trade_dates, signal_dates = load_market_data(config, args.days)
    print(
        f"[backtest] loaded_rows={len(frame)} symbols={frame['symbol'].nunique()} "
        f"signal_days={len(signal_dates)}",
        file=sys.stderr,
        flush=True,
    )
    candidates_by_date = build_candidates(frame, signal_dates)
    price_map, symbols_by_date = build_market_maps(frame)
    fee_rate = float(config.simulation_fee_rate)

    quant_selected: dict[Any, list[dict[str, Any]]] = {}
    llm_selected: dict[Any, list[dict[str, Any]]] = {}
    llm_client = LLMClient(
        base_url=config.llm_base_url,
        model=config.llm_model,
        api_key=config.llm_api_key,
        timeout_seconds=args.llm_timeout,
    )
    llm_fallback_days: list[str] = []
    llm_changed_days = 0
    adaptive_llm_days = 0
    adaptive_quant_days = 0
    adaptive_skip_days = 0
    llm_required_skip_days = 0
    adaptive_gate_records: list[dict[str, Any]] = []
    overlaps: list[int] = []
    llm_elapsed: list[float] = []
    llm_details_by_date: dict[str, dict[str, Any]] = {}
    cached_payload: dict[str, Any] = {}
    if args.cache_input:
        cached_payload = json.loads(Path(args.cache_input).read_text(encoding="utf-8"))
        llm_details_by_date = {
            str(key): value
            for key, value in (cached_payload.get("llm_details_by_date") or {}).items()
            if isinstance(value, dict)
        }
        print(
            f"[cache] loaded_days={len(llm_details_by_date)} from={args.cache_input}",
            file=sys.stderr,
            flush=True,
        )
    default_params = params_from_config(config)

    for index, signal_date in enumerate(signal_dates, start=1):
        candidates = candidates_by_date.get(signal_date, [])
        preselected = candidates[: args.preselect_limit]
        quant_picks = preselected[: args.top_n]
        quant_selected[signal_date] = quant_picks
        final_symbols: list[str] = [item["symbol"] for item in quant_picks]
        cache_key = signal_date.isoformat()
        llm_detail: dict[str, Any] = llm_details_by_date.get(cache_key, {})
        adaptive_use_llm, gate_record = should_use_adaptive_llm(
            candidates, args.top_n, config
        )
        gate_record["signal_date"] = signal_date.isoformat()
        gate_record["reject_action"] = config.t1_llm_adaptive_reject_action
        adaptive_gate_records.append(gate_record)
        skip_by_adaptive_gate = (
            args.use_llm
            and not adaptive_use_llm
            and config.t1_llm_adaptive_reject_action == "skip"
        )
        if args.use_llm and preselected and adaptive_use_llm:
            adaptive_llm_days += 1
            if not llm_detail:
                started = time.perf_counter()
                try:
                    review_candidates = preselected
                    if args.review_mode == "conservative":
                        review_candidates = preselected[
                            : max(
                                args.top_n,
                                args.top_n * default_params.pool_multiplier,
                            )
                        ]
                    final_symbols, llm_detail = ask_llm_for_picks(
                        llm_client,
                        signal_date,
                        review_candidates,
                        args.top_n,
                        args.review_mode,
                    )
                    llm_elapsed.append(time.perf_counter() - started)
                    llm_details_by_date[cache_key] = llm_detail
                except Exception as exc:  # noqa: BLE001
                    llm_fallback_days.append(f"{signal_date.isoformat()}: {exc}")
                    if config.t1_llm_require_review:
                        final_symbols = []
                        llm_required_skip_days += 1
                    else:
                        final_symbols = [item["symbol"] for item in quant_picks]
            else:
                final_symbols = list(llm_detail.keys())
            if config.t1_llm_require_review and not llm_detail and not final_symbols:
                skip_by_adaptive_gate = True
        elif args.use_llm:
            if skip_by_adaptive_gate:
                adaptive_skip_days += 1
                final_symbols = []
            else:
                adaptive_quant_days += 1
            llm_detail = {}
        if skip_by_adaptive_gate:
            selected_rows = []
        elif args.use_llm and args.review_mode == "conservative" and llm_detail:
            selected_rows = select_conservative_picks(
                preselected, llm_detail, args.top_n, default_params
            )
            selected_rows = apply_llm_strategy_filters(
                selected_rows, config, args.top_n
            )
        else:
            candidate_map = {item["symbol"]: item for item in preselected}
            selected_rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for symbol in final_symbols:
                row = candidate_map.get(symbol)
                if row and symbol not in seen:
                    seen.add(symbol)
                    merged = dict(row)
                    merged.update(llm_detail.get(symbol, {}))
                    selected_rows.append(merged)
            for row in quant_picks:
                if len(selected_rows) >= args.top_n:
                    break
                if row["symbol"] not in seen:
                    selected_rows.append(row)
                    seen.add(row["symbol"])
            if args.use_llm:
                selected_rows = apply_llm_strategy_filters(
                    selected_rows, config, args.top_n
                )
        llm_selected[signal_date] = selected_rows[: args.top_n]
        overlap = len(
            {item["symbol"] for item in quant_picks}
            & {item["symbol"] for item in llm_selected[signal_date]}
        )
        overlaps.append(overlap)
        if overlap < min(len(quant_picks), args.top_n):
            llm_changed_days += 1
        print(
            f"[llm] {index}/{len(signal_dates)} {signal_date} "
            f"candidates={len(candidates)} overlap={overlap} "
            f"adaptive_llm={int(adaptive_use_llm)} skip={int(skip_by_adaptive_gate)} "
            f"fallback={len(llm_fallback_days)}",
            file=sys.stderr,
            flush=True,
        )

    if args.cache_output:
        cache_text = json.dumps(
            {
                "parameters": {
                    "signal_start": signal_dates[0].isoformat(),
                    "signal_end": signal_dates[-1].isoformat(),
                    "days": len(signal_dates),
                    "review_mode": args.review_mode,
                    "preselect_limit": args.preselect_limit,
                    "top_n": args.top_n,
                    "model": config.llm_model,
                },
                "llm_details_by_date": llm_details_by_date,
                "fallback_details": llm_fallback_days,
            },
            ensure_ascii=False,
            indent=2,
        )
        Path(args.cache_output).write_text(cache_text, encoding="utf-8")

    quant_result = evaluate_picks(
        quant_selected, signal_dates, all_trade_dates, price_map, fee_rate
    )
    llm_result = evaluate_picks(
        llm_selected, signal_dates, all_trade_dates, price_map, fee_rate
    )
    benchmark = evaluate_benchmark(
        signal_dates, all_trade_dates, price_map, symbols_by_date, fee_rate
    )
    quant_port = quant_result["daily_portfolio"]
    llm_port = llm_result["daily_portfolio"]
    grid_result = None
    if args.grid_search and args.review_mode == "conservative":
        grid_result = grid_search_params(
            candidates_by_date,
            llm_details_by_date,
            signal_dates,
            all_trade_dates,
            price_map,
            fee_rate,
            args.preselect_limit,
            args.top_n,
            quant_result,
        )
    result = {
        "data_range": {
            "stock_daily_min": range_row["min_date"].isoformat(),
            "stock_daily_max": range_row["max_date"].isoformat(),
            "stock_daily_rows": int(range_row["rows_count"]),
            "stock_daily_symbols": int(range_row["symbols_count"]),
            "loaded_rows": int(len(frame)),
            "loaded_symbols": int(frame["symbol"].nunique()),
        },
        "parameters": {
            "signal_days_tested": len(signal_dates),
            "signal_start": signal_dates[0].isoformat(),
            "signal_end": signal_dates[-1].isoformat(),
            "last_buy_date": all_trade_dates[all_trade_dates.index(signal_dates[-1]) + 1].isoformat(),
            "last_sell_date": all_trade_dates[all_trade_dates.index(signal_dates[-1]) + 2].isoformat(),
            "lookback_days": config.analysis_lookback_days,
            "preselect_limit": args.preselect_limit,
            "top_n": args.top_n,
            "fee_rate": fee_rate,
            "llm_used": args.use_llm,
            "llm_review_mode": args.review_mode,
            "t1_llm_review_pool_multiplier": config.t1_llm_review_pool_multiplier,
            "t1_llm_score_weight": config.t1_llm_score_weight,
            "t1_llm_max_adjustment": config.t1_llm_max_adjustment,
            "t1_llm_rank_penalty": config.t1_llm_rank_penalty,
            "t1_llm_avoid_penalty": config.t1_llm_avoid_penalty,
            "t1_llm_protected_top_n": config.t1_llm_protected_top_n,
            "t1_llm_entry_limit_multiplier": config.t1_llm_entry_limit_multiplier,
            "t1_llm_adaptive_gate_enabled": config.t1_llm_adaptive_gate_enabled,
            "t1_llm_adaptive_candidate_count_max": config.t1_llm_adaptive_candidate_count_max,
            "t1_llm_adaptive_top_pct_max": config.t1_llm_adaptive_top_pct_max,
            "t1_llm_adaptive_reject_action": config.t1_llm_adaptive_reject_action,
            "t1_llm_gate_top_n": config.t1_llm_gate_top_n,
            "t1_llm_final_pick_limit": config.t1_llm_final_pick_limit,
            "t1_llm_candidate_pct_change_max": config.t1_llm_candidate_pct_change_max,
            "t1_llm_require_review": config.t1_llm_require_review,
            "t1_llm_allowed_actions": sorted(config.t1_llm_allowed_action_set()),
            "llm_base_url": config.llm_base_url,
            "llm_model": config.llm_model,
            "strict_t1_rule": "D日收盘后出信号，D+1开盘买入，D+2开盘卖出，扣双边手续费",
        },
        "quant": {
            "strict_t1_open_to_open_net": quant_result["trade_summary"],
            "strict_t1_daily_portfolio": quant_port,
        },
        "llm_review": {
            "strict_t1_open_to_open_net": llm_result["trade_summary"],
            "strict_t1_daily_portfolio": llm_port,
            "fallback_days": len(llm_fallback_days),
            "fallback_details": llm_fallback_days[:10],
            "changed_days": llm_changed_days,
            "avg_top10_overlap": round(float(np.mean(overlaps)), 4) if overlaps else None,
            "avg_llm_seconds": round(float(np.mean(llm_elapsed)), 4) if llm_elapsed else None,
            "adaptive_llm_days": adaptive_llm_days,
            "adaptive_quant_days": adaptive_quant_days,
            "adaptive_skip_days": adaptive_skip_days,
            "llm_required_skip_days": llm_required_skip_days,
            "adaptive_gate_records": adaptive_gate_records[:20],
        },
        "benchmark_open_to_open_net": benchmark,
        "delta_llm_minus_quant": {
            "trade_win_rate_pct": _delta(
                llm_result["trade_summary"]["win_rate_pct"],
                quant_result["trade_summary"]["win_rate_pct"],
            ),
            "trade_avg_pct": _delta(
                llm_result["trade_summary"]["avg_pct"],
                quant_result["trade_summary"]["avg_pct"],
            ),
            "daily_win_rate_pct": _delta(
                llm_port["daily_win_rate_pct"],
                quant_port["daily_win_rate_pct"],
            ),
            "cumulative_pct": _delta(
                llm_port["cumulative_pct"], quant_port["cumulative_pct"]
            ),
            "max_drawdown_pct": _delta(
                llm_port["max_drawdown_pct"], quant_port["max_drawdown_pct"]
            ),
        },
        "best_llm_trades": sorted(
            llm_result["trades"], key=lambda item: item["strict_return_pct"], reverse=True
        )[:8],
        "worst_llm_trades": sorted(
            llm_result["trades"], key=lambda item: item["strict_return_pct"]
        )[:8],
    }
    if grid_result:
        result["grid_search"] = grid_result
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


def _delta(left: float | None, right: float | None) -> float | None:
    """计算两个指标的差值。"""

    if left is None or right is None:
        return None
    return round(float(left) - float(right), 4)


if __name__ == "__main__":
    main()
