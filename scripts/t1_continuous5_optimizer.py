"""连续 5 日 T+1 LLM 辅助策略优化脚本。

本脚本复用 t1_llm_backtest.py 的候选生成、LLM 保守合并和 T+1 开盘到
开盘收益计算，用于比较少量可上线的策略族，避免为了单个样本过度网格搜索。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategySpec:
    """描述一组可上线的 LLM 辅助 T+1 策略。"""

    name: str
    count_max: int
    top_pct_max: float
    reject: str
    top_n: int
    fallback_n: int
    min_positions: int = 1
    min_final: float | None = None
    max_latest_pct: float | None = None
    score_weight: float = 0.20
    require_llm_review: bool = False
    allowed_actions: tuple[str, ...] = ()


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=120, help="用于搜索的完整信号日数量")
    parser.add_argument("--initial-cash", type=float, default=10000.0, help="连续样本资金模拟本金")
    parser.add_argument("--sample-start", default="2025-12-22", help="重点连续 5 日样本起始信号日")
    parser.add_argument("--sample-days", type=int, default=5, help="重点连续样本交易日数量")
    parser.add_argument("--cache-input", required=True, help="LLM 复核缓存 JSON 路径")
    parser.add_argument("--output", required=True, help="输出报告 JSON 路径")
    return parser.parse_args()


def load_backtest_module() -> Any:
    """动态加载同目录的 t1_llm_backtest.py。"""

    module_path = Path(__file__).with_name("t1_llm_backtest.py")
    spec = importlib.util.spec_from_file_location("t1bt", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["t1bt"] = module
    spec.loader.exec_module(module)
    return module


def make_params(base_params: Any, score_weight: float) -> Any:
    """复制回测参数并覆盖 LLM 分数权重。"""

    class Params:
        pass

    params = Params()
    for name in [
        "pool_multiplier",
        "score_weight",
        "max_adjustment",
        "rank_penalty",
        "avoid_penalty",
        "protected_top_n",
        "entry_limit_multiplier",
        "allowed_actions",
    ]:
        setattr(params, name, getattr(base_params, name))
    params.score_weight = score_weight
    return params


def build_strategy_specs() -> list[StrategySpec]:
    """生成少量重点策略族，覆盖当前、收益优先和连续 5 日机会模式。"""

    specs = [
        StrategySpec(
            "current_keep_top3_5000_skip_pct3.5_require",
            5000,
            5.0,
            "skip",
            3,
            3,
            max_latest_pct=3.5,
            require_llm_review=True,
            allowed_actions=("KEEP",),
        ),
        StrategySpec(
            "previous_opportunity_4500_skip_top5_pct3.5",
            4500,
            4.0,
            "skip",
            5,
            5,
            max_latest_pct=3.5,
        ),
        StrategySpec("previous_winrate_2500_skip_top10", 2500, 4.0, "skip", 10, 10),
        StrategySpec("return_mode_4080_quant_top10", 4080, 3.25, "quant", 10, 10),
        StrategySpec("full_llm_top10", 99999, 99.0, "skip", 10, 10),
    ]
    for count_max, top_pct_max in [(4500, 4.0), (4500, 5.0), (5000, 5.0)]:
        for top_n in [3, 5, 8, 10]:
            specs.append(
                StrategySpec(
                    f"opportunity_llm_{count_max}_{top_pct_max:g}_top{top_n}",
                    count_max,
                    top_pct_max,
                    "skip",
                    top_n,
                    top_n,
                )
            )
            for min_final in [92.0, 93.0, 94.0]:
                specs.append(
                    StrategySpec(
                        f"opportunity_llm_{count_max}_{top_pct_max:g}_top{top_n}_min{min_final:g}",
                        count_max,
                        top_pct_max,
                        "skip",
                        top_n,
                        top_n,
                        min_final=min_final,
                    )
                )
            for max_latest_pct in [5.5, 4.5, 3.5]:
                specs.append(
                    StrategySpec(
                        f"opportunity_llm_{count_max}_{top_pct_max:g}_top{top_n}_pct{max_latest_pct:g}",
                        count_max,
                        top_pct_max,
                        "skip",
                        top_n,
                        top_n,
                        max_latest_pct=max_latest_pct,
                    )
                )
                for actions, suffix in [
                    (("BOOST",), "boost"),
                    (("KEEP",), "keep"),
                    (("BOOST", "KEEP"), "boost_keep"),
                ]:
                    specs.append(
                        StrategySpec(
                            f"opportunity_llm_{count_max}_{top_pct_max:g}_top{top_n}_pct{max_latest_pct:g}_{suffix}_require",
                            count_max,
                            top_pct_max,
                            "skip",
                            top_n,
                            top_n,
                            max_latest_pct=max_latest_pct,
                            require_llm_review=True,
                            allowed_actions=actions,
                        )
                    )
    return specs


def select_picks(
    bt: Any,
    spec: StrategySpec,
    precomputed: dict[Any, dict[str, Any]],
    trade_date: Any,
    base_params: Any,
) -> tuple[list[dict[str, Any]], str]:
    """按策略规则选择某个信号日的候选。"""

    item = precomputed[trade_date]
    enabled = item["count"] <= spec.count_max and item["top_pct_avg"] <= spec.top_pct_max
    if not enabled:
        if spec.reject == "quant":
            return item["quant"][: spec.fallback_n], "quant_fallback"
        return [], "skip"

    if spec.require_llm_review and not item["llm_cached"]:
        return [], "skip_missing_llm"

    rows = list(item["llm"].get(spec.score_weight) or item["quant"])
    rows = [row for row in rows if str(row.get("action") or "KEEP").upper() != "AVOID"]
    if spec.allowed_actions:
        allowed = {action.upper() for action in spec.allowed_actions}
        rows = [row for row in rows if str(row.get("action") or "KEEP").upper() in allowed]
    if spec.min_final is not None:
        rows = [
            row
            for row in rows
            if float(row.get("final_score") or row.get("quant_score") or 0) >= spec.min_final
        ]
    if spec.max_latest_pct is not None:
        rows = [
            row
            for row in rows
            if float(row.get("pct_change") or 0) <= spec.max_latest_pct
        ]
    rows = rows[: spec.top_n]
    if len(rows) < spec.min_positions:
        return [], "skip_min_positions"
    return rows, "llm_cached" if item["llm_cached"] else "llm_missing_quant"


def open_to_open_returns(
    bt: Any,
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    signal_date: Any,
    picks: list[dict[str, Any]],
    fee_rate: float,
) -> tuple[float, list[float]]:
    """计算某个信号日的等权 T+1 开盘到开盘收益。"""

    if not picks:
        return 0.0, []
    trade_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
    index = trade_index[signal_date]
    buy_date = all_trade_dates[index + 1]
    sell_date = all_trade_dates[index + 2]
    returns: list[float] = []
    for pick in picks:
        buy_price = price_map.get((pick["symbol"], buy_date))
        sell_price = price_map.get((pick["symbol"], sell_date))
        if not buy_price or not sell_price:
            continue
        value = bt.net_return(buy_price["open"], sell_price["open"], fee_rate)
        if value is not None:
            returns.append(float(value))
    return (float(np.mean(returns)), returns) if returns else (0.0, [])


def simulate_cash_window(
    bt: Any,
    spec: StrategySpec,
    precomputed: dict[Any, dict[str, Any]],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    signal_dates: list[Any],
    base_params: Any,
    fee_rate: float,
    initial_cash: float,
) -> dict[str, Any]:
    """用整股买入方式模拟连续窗口资金曲线。"""

    trade_index = {trade_date: index for index, trade_date in enumerate(all_trade_dates)}
    equity = float(initial_cash)
    peak = equity
    max_drawdown = 0.0
    day_rows = []
    trade_returns = []
    active_returns = []
    for day_no, signal_date in enumerate(signal_dates, start=1):
        picks, source = select_picks(bt, spec, precomputed, signal_date, base_params)
        index = trade_index[signal_date]
        buy_date = all_trade_dates[index + 1]
        sell_date = all_trade_dates[index + 2]
        start_asset = equity
        profit = 0.0
        trades = []
        if picks:
            allocation = start_asset / len(picks)
            for rank, pick in enumerate(picks, start=1):
                buy_price = price_map.get((pick["symbol"], buy_date))
                sell_price = price_map.get((pick["symbol"], sell_date))
                if not buy_price or not sell_price:
                    continue
                buy_open = float(buy_price["open"])
                sell_open = float(sell_price["open"])
                quantity = int(allocation / (buy_open * (1 + fee_rate)))
                if quantity <= 0:
                    continue
                invested_cash = buy_open * quantity * (1 + fee_rate)
                net_sell_cash = sell_open * quantity * (1 - fee_rate)
                trade_profit = allocation - invested_cash + net_sell_cash - allocation
                trade_return = trade_profit / invested_cash * 100 if invested_cash else 0.0
                profit += trade_profit
                trade_returns.append(trade_return)
                trades.append(
                    {
                        "rank": rank,
                        "symbol": pick["symbol"],
                        "name": pick.get("name"),
                        "profit": round(trade_profit, 2),
                        "return_pct": round(trade_return, 4),
                        "quant_score": pick.get("quant_score"),
                        "final_score": pick.get("final_score"),
                        "action": pick.get("action"),
                    }
                )
        equity = start_asset + profit
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
        day_return = (equity / start_asset - 1) * 100 if start_asset else 0.0
        if picks:
            active_returns.append(day_return)
        day_rows.append(
            {
                "day_no": day_no,
                "signal_date": signal_date.isoformat(),
                "buy_date": buy_date.isoformat(),
                "sell_date": sell_date.isoformat(),
                "source": source,
                "candidate_count": precomputed[signal_date]["count"],
                "top_pct_avg": round(precomputed[signal_date]["top_pct_avg"], 4),
                "start_asset": round(start_asset, 2),
                "end_asset": round(equity, 2),
                "profit": round(profit, 2),
                "daily_return_pct": round(day_return, 4),
                "selected_count": len(picks),
                "symbols": [pick["symbol"] for pick in picks],
                "best_trades": sorted(trades, key=lambda item: item["profit"], reverse=True)[:3],
                "worst_trades": sorted(trades, key=lambda item: item["profit"])[:3],
            }
        )
    return {
        "initial_cash": round(float(initial_cash), 2),
        "final_asset": round(equity, 2),
        "total_profit": round(equity - float(initial_cash), 2),
        "total_return_pct": round((equity / float(initial_cash) - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "active_day_count": len(active_returns),
        "active_day_win_rate_pct": _win_rate(active_returns),
        "trade_count": len(trade_returns),
        "trade_win_rate_pct": _win_rate(trade_returns),
        "day_rows": day_rows,
    }


def evaluate_strategy(
    bt: Any,
    spec: StrategySpec,
    precomputed: dict[Any, dict[str, Any]],
    all_trade_dates: list[Any],
    price_map: dict[tuple[str, Any], dict[str, float]],
    signal_dates: list[Any],
    sample_dates: list[Any],
    recent_dates: list[Any],
    base_params: Any,
    fee_rate: float,
    initial_cash: float,
) -> dict[str, Any]:
    """汇总全窗口、滚动 5 日、重点 5 日和最近 10 日表现。"""

    daily_returns: list[float] = []
    active_returns: list[float] = []
    trade_returns: list[float] = []
    for signal_date in signal_dates:
        picks, _source = select_picks(bt, spec, precomputed, signal_date, base_params)
        day_return, returns = open_to_open_returns(
            bt, all_trade_dates, price_map, signal_date, picks, fee_rate
        )
        daily_returns.append(day_return)
        if picks:
            active_returns.append(day_return)
            trade_returns.extend(returns)

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in daily_returns:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)

    rolling5 = []
    for index in range(len(daily_returns) - 4):
        window_equity = 1.0
        for value in daily_returns[index : index + 5]:
            window_equity *= 1 + value / 100
        rolling5.append((window_equity - 1) * 100)

    sample_cash = simulate_cash_window(
        bt,
        spec,
        precomputed,
        all_trade_dates,
        price_map,
        sample_dates,
        base_params,
        fee_rate,
        initial_cash,
    )
    recent_cash = simulate_cash_window(
        bt,
        spec,
        precomputed,
        all_trade_dates,
        price_map,
        recent_dates,
        base_params,
        fee_rate,
        initial_cash,
    )
    return {
        "strategy": spec.__dict__,
        "active_days": len(active_returns),
        "skip_days": len(signal_dates) - len(active_returns),
        "daily_win_rate_all_pct": _win_rate(daily_returns),
        "daily_win_rate_active_pct": _win_rate(active_returns),
        "trade_win_rate_pct": _win_rate(trade_returns),
        "cumulative_pct": round((equity - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "rolling5_win_rate_pct": _win_rate(rolling5),
        "rolling5_avg_pct": round(float(np.mean(rolling5)), 4) if rolling5 else None,
        "rolling5_median_pct": round(float(np.median(rolling5)), 4) if rolling5 else None,
        "rolling5_min_pct": round(float(np.min(rolling5)), 4) if rolling5 else None,
        "sample5_cash": sample_cash,
        "recent10_cash": recent_cash,
    }


def _win_rate(values: list[float]) -> float | None:
    """计算正收益占比。"""

    return round(sum(1 for value in values if value > 0) / len(values) * 100, 4) if values else None


def main() -> None:
    """运行连续 5 日策略优化。"""

    args = parse_args()
    bt = load_backtest_module()
    config = bt.AppConfig.from_env()
    frame, range_row, all_trade_dates, signal_dates = bt.load_market_data(config, args.days)
    candidates_by_date = bt.build_candidates(frame, signal_dates)
    price_map, _symbols_by_date = bt.build_market_maps(frame)
    fee_rate = float(config.simulation_fee_rate)
    cache = json.loads(Path(args.cache_input).read_text(encoding="utf-8"))
    llm_details = {
        str(key): value
        for key, value in (cache.get("llm_details_by_date") or {}).items()
        if isinstance(value, dict)
    }
    base_params = bt.params_from_config(config)

    precomputed: dict[Any, dict[str, Any]] = {}
    for signal_date in signal_dates:
        candidates = candidates_by_date.get(signal_date, [])
        preselected = candidates[:80]
        top_rows = candidates[:10]
        top_pct_avg = float(np.mean([row["pct_change"] for row in top_rows])) if top_rows else 0.0
        detail = llm_details.get(signal_date.isoformat(), {})
        precomputed[signal_date] = {
            "count": len(candidates),
            "top_pct_avg": top_pct_avg,
            "quant": preselected[:20],
            "llm_cached": bool(detail),
            "llm": {
                0.20: bt.select_conservative_picks(preselected, detail, 20, make_params(base_params, 0.20))
                if detail
                else preselected[:20],
            },
        }

    sample_start = pd.to_datetime(args.sample_start).date()
    try:
        start_index = signal_dates.index(sample_start)
    except ValueError as exc:
        raise RuntimeError(f"样本起始日不在信号日内：{args.sample_start}") from exc
    sample_dates = signal_dates[start_index : start_index + args.sample_days]
    recent_dates = signal_dates[-10:]

    results = [
        evaluate_strategy(
            bt,
            spec,
            precomputed,
            all_trade_dates,
            price_map,
            signal_dates,
            sample_dates,
            recent_dates,
            base_params,
            fee_rate,
            args.initial_cash,
        )
        for spec in build_strategy_specs()
    ]
    best_by_sample5 = sorted(
        results,
        key=lambda item: (
            item["sample5_cash"]["total_return_pct"],
            item["sample5_cash"]["active_day_win_rate_pct"] or -1,
            item["rolling5_win_rate_pct"] or -1,
            item["recent10_cash"]["total_return_pct"],
        ),
        reverse=True,
    )[:10]
    best_balanced = sorted(
        [
            item
            for item in results
            if item["sample5_cash"]["total_return_pct"] >= 0
            and item["recent10_cash"]["total_return_pct"] > 0
        ],
        key=lambda item: (
            item["rolling5_win_rate_pct"] or -1,
            item["sample5_cash"]["total_return_pct"],
            item["recent10_cash"]["total_return_pct"],
        ),
        reverse=True,
    )[:10]

    report = {
        "parameters": {
            "days": args.days,
            "initial_cash": args.initial_cash,
            "sample_start": args.sample_start,
            "sample_days": args.sample_days,
            "signal_start": signal_dates[0].isoformat(),
            "signal_end": signal_dates[-1].isoformat(),
            "recent10_start": recent_dates[0].isoformat(),
            "recent10_end": recent_dates[-1].isoformat(),
            "fee_rate": fee_rate,
            "llm_cache_days": len(llm_details),
            "data_range": {
                "stock_daily_min": range_row["min_date"].isoformat(),
                "stock_daily_max": range_row["max_date"].isoformat(),
                "stock_daily_rows": int(range_row["rows_count"]),
            },
        },
        "baseline": {
            item["strategy"]["name"]: item
            for item in results
            if item["strategy"]["name"]
            in {
                "current_winrate_2500_skip_top10",
                "current_opportunity_4500_skip_top5_pct3.5",
                "previous_opportunity_4500_skip_top5_pct3.5",
                "previous_winrate_2500_skip_top10",
                "return_mode_4080_quant_top10",
                "full_llm_top10",
            }
        },
        "best_by_sample5": best_by_sample5,
        "best_balanced": best_balanced,
        "all_results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ["parameters", "baseline", "best_by_sample5", "best_balanced"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
