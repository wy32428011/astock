"""全 A 股未来 3 个交易日涨势候选分析。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from astocks_collector.config import AppConfig
from astocks_collector.db import MySQLRepository
from astocks_collector.llm_client import LLMClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThreeDayAnalysisResult:
    """未来 3 个交易日涨势分析结果摘要。"""

    analysis_date: date
    trade_date: date
    preselect_count: int
    final_count: int
    llm_fallback: bool
    picks: list[dict[str, Any]]


class ThreeDayTrendAnalyzer:
    """执行全 A 股 3 个交易日短线涨势量化预筛和 LLM 复核。"""

    horizon_days = 3

    def __init__(
        self,
        config: AppConfig,
        repository: MySQLRepository | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        """初始化分析依赖。"""

        self.config = config
        self.repository = repository or MySQLRepository(config)
        self.llm_client = llm_client or LLMClient(
            base_url=config.llm_base_url,
            model=config.llm_model,
            api_key=config.llm_api_key,
            timeout_seconds=config.llm_timeout_seconds,
        )

    def analyze(
        self,
        preselect_limit: int | None = None,
        final_limit: int | None = None,
        trade_date: str | None = None,
        use_llm: bool = True,
    ) -> ThreeDayAnalysisResult:
        """生成未来 3 个交易日涨势较好的股票候选。"""

        self.repository.ensure_schema()
        pre_limit = preselect_limit or self.config.three_day_preselect_limit
        out_limit = final_limit or self.config.three_day_final_limit
        latest_trade_date = self._resolve_trade_date(trade_date)

        frame = self._load_history(latest_trade_date)
        candidates = self._build_candidates(frame, latest_trade_date, pre_limit)
        llm_fallback = False
        raw_response: Any
        if use_llm:
            try:
                raw_response = self._ask_llm(candidates, out_limit, latest_trade_date)
            except Exception as exc:
                logger.warning("3日分析 LLM 不可用，降级为量化排序: %s", exc)
                raw_response = self._fallback_payload(candidates, out_limit)
                llm_fallback = True
        else:
            raw_response = self._fallback_payload(candidates, out_limit)

        picks = self._merge_llm(
            candidates=candidates,
            llm_payload=raw_response,
            final_limit=out_limit,
            trade_date=latest_trade_date,
            llm_fallback=llm_fallback,
        )
        self.repository.replace_three_day_picks(
            analysis_date=latest_trade_date.isoformat(),
            picks=picks,
            raw_response=raw_response,
            llm_fallback=llm_fallback,
        )
        return ThreeDayAnalysisResult(
            analysis_date=latest_trade_date,
            trade_date=latest_trade_date,
            preselect_count=len(candidates),
            final_count=len(picks),
            llm_fallback=llm_fallback,
            picks=picks,
        )

    def _resolve_trade_date(self, trade_date: str | None) -> date:
        """解析分析基准交易日，默认取覆盖较完整的最新交易日。"""

        if trade_date:
            return pd.to_datetime(trade_date).date()

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
                row = cursor.fetchone()
                if row:
                    return row["trade_date"]
                cursor.execute(
                    "SELECT MAX(trade_date) AS trade_date FROM stock_daily WHERE adjust_type=%s",
                    (self.config.adjust_type,),
                )
                row = cursor.fetchone()
        if not row or row["trade_date"] is None:
            raise RuntimeError("stock_daily 中没有可分析的交易日")
        return row["trade_date"]

    def _load_history(self, latest_trade_date: date) -> pd.DataFrame:
        """读取全 A 股近似 120 个交易日所需的历史行情。"""

        sql = """
            SELECT
                b.symbol,
                b.name,
                b.exchange,
                d.trade_date,
                d.open_price,
                d.close_price,
                d.high_price,
                d.low_price,
                d.volume,
                d.amount,
                d.pct_change,
                d.turnover_rate
            FROM stock_daily d
            JOIN stock_basic b ON b.symbol=d.symbol
            WHERE d.trade_date <= %s
              AND d.trade_date >= DATE_SUB(%s, INTERVAL %s DAY)
              AND b.is_active=1
              AND b.name NOT LIKE '%%ST%%'
              AND b.name NOT LIKE '%%退%%'
              AND b.name NOT LIKE '%%退市%%'
              AND b.name NOT LIKE 'N%%'
              AND b.name NOT LIKE 'C%%'
              AND d.adjust_type = %s
              AND d.open_price > 0
              AND d.close_price > 0
              AND d.high_price >= GREATEST(d.open_price, d.close_price, d.low_price)
              AND d.low_price <= LEAST(d.open_price, d.close_price, d.high_price)
              AND d.volume > 0
              AND d.amount > 0
            ORDER BY b.symbol, d.trade_date
        """
        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    sql,
                    (
                        latest_trade_date,
                        latest_trade_date,
                        self.config.three_day_analysis_lookback_days * 2,
                        self.config.adjust_type,
                    ),
                )
                frame = pd.DataFrame(cursor.fetchall())

        if frame.empty:
            raise RuntimeError("没有读取到可分析的非 ST 股票行情")
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
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
        return frame

    def _build_candidates(
        self, frame: pd.DataFrame, latest_trade_date: date, limit: int
    ) -> list[dict[str, Any]]:
        """计算 3 日涨势技术因子并生成量化预筛名单。"""

        rows: list[dict[str, Any]] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            group = group.sort_values("trade_date").tail(
                self.config.three_day_analysis_lookback_days
            )
            if len(group) < 30 or group.iloc[-1]["trade_date"] != latest_trade_date:
                continue

            closes = group["close_price"].astype(float)
            highs = group["high_price"].astype(float)
            volumes = group["volume"].astype(float)
            latest = group.iloc[-1]
            close = float(latest["close_price"])
            latest_pct = _finite_float(latest["pct_change"])
            turnover = _finite_float(latest["turnover_rate"])
            if close <= 0 or latest_pct > 9.6 or latest_pct < -8.5:
                continue

            ma5 = float(closes.tail(5).mean())
            ma10 = float(closes.tail(10).mean())
            ma20 = float(closes.tail(20).mean())
            ret_3 = _pct_return(closes, 3)
            ret_5 = _pct_return(closes, 5)
            ret_10 = _pct_return(closes, 10)
            ret_20 = _pct_return(closes, 20)
            volume_ratio = _safe_ratio(
                float(volumes.tail(5).mean()), float(volumes.tail(20).mean())
            )
            volatility_20 = float(closes.pct_change().tail(20).std() * 100)
            max_drawdown_20 = _max_drawdown(closes.tail(20))
            high_20 = float(highs.tail(20).max())
            breakout_strength = _safe_ratio(close - high_20, high_20) * 100
            above_ma20 = _safe_ratio(close - ma20, ma20) * 100

            if volatility_20 > 9.5 or max_drawdown_20 < -28:
                continue

            factors = {
                "ret_3": round(ret_3, 4),
                "ret_5": round(ret_5, 4),
                "ret_10": round(ret_10, 4),
                "ret_20": round(ret_20, 4),
                "ma5": round(ma5, 4),
                "ma10": round(ma10, 4),
                "ma20": round(ma20, 4),
                "above_ma20": round(above_ma20, 4),
                "volume_ratio": round(volume_ratio, 4),
                "volatility_20": round(volatility_20, 4),
                "max_drawdown_20": round(max_drawdown_20, 4),
                "breakout_strength": round(breakout_strength, 4),
                "turnover_rate": round(turnover, 4),
            }
            score = _score_three_day_candidate(
                close=close,
                latest_pct=latest_pct,
                **factors,
            )
            if score < 50:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "name": str(latest["name"]),
                    "trade_date": latest_trade_date.isoformat(),
                    "close_price": round(close, 4),
                    "pct_change": round(latest_pct, 4),
                    "three_day_score": round(score, 4),
                    "quant_reason": _build_quant_reason(factors),
                    "risk": _build_risk(latest_pct, volatility_20, max_drawdown_20),
                    "factor_snapshot": factors,
                }
            )

        rows.sort(key=lambda item: item["three_day_score"], reverse=True)
        return rows[:limit]

    def _ask_llm(
        self, candidates: list[dict[str, Any]], final_limit: int, trade_date: date
    ) -> list[dict[str, Any]]:
        """请求大模型复核未来 3 个交易日涨势候选。"""

        messages = [
            {
                "role": "system",
                "content": (
                    "你是A股短线技术分析助手。只能基于候选池做未来3个交易日涨势排序，"
                    "不得编造候选池之外的股票。输出必须是JSON数组。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "筛选未来3个交易日涨势较好的A股候选",
                        "trade_date": trade_date.isoformat(),
                        "horizon": "未来3个交易日",
                        "final_limit": final_limit,
                        "selection_rules": [
                            "优先趋势向上、均线多头、量能温和放大、突破强度适中",
                            "规避涨幅过高、波动过大、回撤压力大的股票",
                            "只输出候选池中的symbol",
                            "llm_score范围0到100",
                        ],
                        "output_schema": [
                            {
                                "symbol": "股票代码",
                                "llm_score": 0,
                                "expected_direction": "未来3个交易日预期方向",
                                "reason": "入选理由，30字以内",
                                "risk": "主要风险，30字以内",
                            }
                        ],
                        "candidates": candidates,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        payload = self.llm_client.chat_json(messages, max_tokens=4096)
        if isinstance(payload, dict):
            payload = payload.get("picks") or payload.get("data") or []
        if not isinstance(payload, list):
            raise RuntimeError("LLM JSON 响应不是数组")
        return [item for item in payload if isinstance(item, dict)]

    def _fallback_payload(
        self, candidates: list[dict[str, Any]], final_limit: int
    ) -> list[dict[str, Any]]:
        """根据量化结果构造 LLM 降级响应。"""

        return [
            {
                "symbol": row["symbol"],
                "llm_score": row["three_day_score"],
                "expected_direction": "未来3个交易日偏强观察",
                "reason": row["quant_reason"],
                "risk": row["risk"],
            }
            for row in candidates[:final_limit]
        ]

    def _merge_llm(
        self,
        candidates: list[dict[str, Any]],
        llm_payload: list[dict[str, Any]],
        final_limit: int,
        trade_date: date,
        llm_fallback: bool,
    ) -> list[dict[str, Any]]:
        """合并量化评分和 LLM 评分，生成最终 3 日候选。"""

        candidate_map = {row["symbol"]: row for row in candidates}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in llm_payload:
            symbol = str(item.get("symbol", "")).zfill(6)
            if symbol not in candidate_map or symbol in seen:
                continue
            seen.add(symbol)
            base = candidate_map[symbol]
            llm_score = _clamp(
                float(item.get("llm_score") or base["three_day_score"]), 0, 100
            )
            final_score = base["three_day_score"] * 0.6 + llm_score * 0.4
            merged.append(
                _to_pick_row(
                    base=base,
                    rank_no=0,
                    llm_score=llm_score,
                    final_score=final_score,
                    expected_direction=str(
                        item.get("expected_direction") or "未来3个交易日偏强观察"
                    ),
                    reason=str(item.get("reason") or base["quant_reason"]),
                    risk=str(item.get("risk") or base["risk"]),
                    trade_date=trade_date,
                    llm_fallback=llm_fallback,
                )
            )

        if len(merged) < final_limit:
            for base in candidates:
                if base["symbol"] in seen:
                    continue
                seen.add(base["symbol"])
                merged.append(
                    _to_pick_row(
                        base=base,
                        rank_no=0,
                        llm_score=base["three_day_score"],
                        final_score=base["three_day_score"],
                        expected_direction="未来3个交易日偏强观察",
                        reason=base["quant_reason"],
                        risk=base["risk"],
                        trade_date=trade_date,
                        llm_fallback=llm_fallback,
                    )
                )
                if len(merged) >= final_limit:
                    break

        merged.sort(key=lambda item: item["final_score"], reverse=True)
        for index, row in enumerate(merged[:final_limit], start=1):
            row["rank_no"] = index
        return merged[:final_limit]


def _score_three_day_candidate(**factors: float) -> float:
    """根据短线趋势、量能、突破和风险计算 3 日涨势评分。"""

    score = 45.0
    score += _clamp(factors["ret_3"], -6, 7) * 1.4
    score += _clamp(factors["ret_5"], -8, 10) * 0.9
    score += _clamp(factors["ret_10"], -12, 15) * 0.35
    score += _clamp(factors["ret_20"], -18, 22) * 0.15
    if factors["close"] > factors["ma5"] > factors["ma10"] > factors["ma20"]:
        score += 13
    elif factors["close"] > factors["ma5"] > factors["ma20"]:
        score += 8
    elif factors["close"] > factors["ma20"]:
        score += 4
    score += _clamp((factors["volume_ratio"] - 1) * 12, -8, 10)
    if -1.5 <= factors["breakout_strength"] <= 4.5:
        score += 7
    elif factors["breakout_strength"] > 8:
        score -= 6
    score -= max(0.0, factors["volatility_20"] - 5.0) * 1.5
    score += _clamp(factors["max_drawdown_20"] + 10, -8, 4)
    if 0.2 <= factors["latest_pct"] <= 5.8:
        score += 5
    elif factors["latest_pct"] > 7:
        score -= 10
    if 1 <= factors["turnover_rate"] <= 12:
        score += 4
    elif factors["turnover_rate"] > 18:
        score -= 5
    return _clamp(score, 0, 100)


def _pct_return(series: pd.Series, periods: int) -> float:
    """计算指定周期收益率百分比。"""

    if len(series) <= periods:
        return 0.0
    base = float(series.iloc[-periods - 1])
    latest = float(series.iloc[-1])
    if base == 0:
        return 0.0
    return (latest / base - 1) * 100


def _max_drawdown(series: pd.Series) -> float:
    """计算区间最大回撤百分比。"""

    values = series.astype(float)
    running_max = values.cummax()
    drawdowns = values / running_max - 1
    return float(drawdowns.min() * 100)


def _safe_ratio(numerator: float, denominator: float) -> float:
    """安全除法，分母为零时返回零。"""

    if denominator == 0:
        return 0.0
    return numerator / denominator


def _finite_float(value: Any) -> float:
    """将可能为空或 NaN 的数值转换为有限浮点数。"""

    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def _build_quant_reason(factors: dict[str, float]) -> str:
    """生成 3 日量化入选理由。"""

    return (
        f"3日{factors['ret_3']:.2f}%，5日{factors['ret_5']:.2f}%，"
        f"量比{factors['volume_ratio']:.2f}，突破{factors['breakout_strength']:.2f}%"
    )


def _build_risk(latest_pct: float, volatility_20: float, max_drawdown_20: float) -> str:
    """生成 3 日候选风险描述。"""

    risks = []
    if latest_pct > 6:
        risks.append("短线涨幅偏高")
    if volatility_20 > 5.5:
        risks.append("波动偏大")
    if max_drawdown_20 < -15:
        risks.append("回撤压力")
    return "；".join(risks) if risks else "注意市场波动和板块轮动"


def _to_pick_row(
    base: dict[str, Any],
    rank_no: int,
    llm_score: float,
    final_score: float,
    expected_direction: str,
    reason: str,
    risk: str,
    trade_date: date,
    llm_fallback: bool,
) -> dict[str, Any]:
    """转换为 stock_three_day_pick 表行。"""

    return {
        "rank_no": rank_no,
        "symbol": base["symbol"],
        "name": base["name"],
        "trade_date": trade_date.isoformat(),
        "quant_score": Decimal(str(round(base["three_day_score"], 4))),
        "llm_score": Decimal(str(round(llm_score, 4))),
        "final_score": Decimal(str(round(final_score, 4))),
        "expected_direction": expected_direction[:128],
        "reason": reason[:500],
        "risk": risk[:500],
        "factor_snapshot": base.get("factor_snapshot") or {},
        "llm_fallback": llm_fallback,
    }


def _clamp(value: float, low: float, high: float) -> float:
    """将数值限制在指定区间内。"""

    return max(low, min(high, value))
