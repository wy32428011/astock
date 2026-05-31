"""基于历史行情和大模型的 T+1 选股分析。"""

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
class AnalysisResult:
    """T+1 选股分析结果摘要。"""

    analysis_date: date
    trade_date: date
    preselect_count: int
    final_count: int
    picks: list[dict[str, Any]]


class T1StockAnalyzer:
    """执行 ST 剔除、技术因子预筛和大模型复核。"""

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
    ) -> AnalysisResult:
        """生成 T+1 可能上涨股票候选结果。"""

        self.repository.ensure_schema()
        pre_limit = preselect_limit or self.config.analysis_preselect_limit
        out_limit = final_limit or self.config.analysis_final_limit
        latest_trade_date = self._resolve_trade_date(trade_date)

        frame = self._load_history(latest_trade_date)
        all_candidates = self._build_candidates(frame, latest_trade_date, None)
        candidates = all_candidates[:pre_limit]
        llm_payload: list[dict[str, Any]]
        picks: list[dict[str, Any]]
        adaptive_use_llm = use_llm and self._should_use_llm_adaptive(
            all_candidates, out_limit
        )
        skip_by_adaptive_gate = (
            use_llm
            and not adaptive_use_llm
            and self.config.t1_llm_adaptive_reject_action == "skip"
        )
        if adaptive_use_llm:
            try:
                llm_payload = self._ask_llm(candidates, out_limit, latest_trade_date)
                if self.config.t1_llm_require_review and not llm_payload:
                    llm_payload = [
                        self._build_llm_skip_payload(
                            all_candidates,
                            out_limit,
                            "LLM 返回空复核结果，严格模式跳过本次T+1交易",
                        )
                    ]
                    picks = []
                else:
                    picks = self._merge_llm(
                        candidates, llm_payload, out_limit, latest_trade_date
                    )
            except Exception as exc:
                if self.config.t1_llm_require_review:
                    logger.warning("T+1 LLM 复核不可用，严格模式跳过交易：%s", exc)
                    llm_payload = [
                        self._build_llm_skip_payload(
                            all_candidates,
                            out_limit,
                            f"LLM 复核不可用，严格模式跳过本次T+1交易：{exc}",
                        )
                    ]
                    picks = []
                else:
                    logger.warning("T+1 LLM 复核不可用，已降级为量化候选：%s", exc)
                    llm_payload = self._build_quant_payload(
                        candidates, out_limit, "llm_fallback_quant"
                    )
                    picks = self._merge_llm(
                        candidates, llm_payload, out_limit, latest_trade_date
                    )
        elif skip_by_adaptive_gate:
            llm_payload = [self._build_adaptive_skip_payload(all_candidates, out_limit)]
            picks = []
        else:
            llm_payload = self._build_quant_payload(
                candidates,
                out_limit,
                "quant_only" if use_llm else "llm_disabled",
            )
            picks = self._merge_llm(candidates, llm_payload, out_limit, latest_trade_date)

        self.repository.replace_analysis_picks(
            analysis_date=latest_trade_date.isoformat(),
            picks=picks,
            raw_response=llm_payload,
        )
        return AnalysisResult(
            analysis_date=latest_trade_date,
            trade_date=latest_trade_date,
            preselect_count=len(candidates),
            final_count=len(picks),
            picks=picks,
        )

    def _resolve_trade_date(self, trade_date: str | None) -> date:
        """解析分析使用的交易日，默认取数据库最新交易日。"""

        if trade_date:
            return pd.to_datetime(trade_date).date()

        with self.repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(trade_date) AS trade_date FROM stock_daily")
                row = cursor.fetchone()
        if not row or row["trade_date"] is None:
            raise RuntimeError("stock_daily 中没有可分析的交易日")
        return row["trade_date"]

    def _load_history(self, latest_trade_date: date) -> pd.DataFrame:
        """读取非 ST 股票最近窗口日线数据。"""

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
                        self.config.analysis_lookback_days * 2,
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
        self, frame: pd.DataFrame, latest_trade_date: date, limit: int | None
    ) -> list[dict[str, Any]]:
        """计算技术因子并生成量化预筛名单。"""

        rows: list[dict[str, Any]] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            group = group.sort_values("trade_date").tail(self.config.analysis_lookback_days)
            if len(group) < 30 or group.iloc[-1]["trade_date"] != latest_trade_date:
                continue

            closes = group["close_price"]
            volumes = group["volume"]
            latest = group.iloc[-1]
            close = float(latest["close_price"])
            if close <= 0:
                continue

            ret_5 = _pct_return(closes, 5)
            ret_10 = _pct_return(closes, 10)
            ret_20 = _pct_return(closes, 20)
            ma5 = float(closes.tail(5).mean())
            ma10 = float(closes.tail(10).mean())
            ma20 = float(closes.tail(20).mean())
            volume_ratio = _safe_ratio(
                float(volumes.tail(5).mean()), float(volumes.tail(20).mean())
            )
            volatility_20 = float(closes.pct_change().tail(20).std() * 100)
            max_drawdown_20 = _max_drawdown(closes.tail(20))
            latest_pct = _finite_float(latest["pct_change"])
            turnover = _finite_float(latest["turnover_rate"])
            above_ma20 = _safe_ratio(close - ma20, ma20) * 100

            if latest_pct > 9.6 or latest_pct < -8.5:
                continue
            if volatility_20 > 8.5 or max_drawdown_20 < -25:
                continue

            quant_score = _score_candidate(
                ret_5=ret_5,
                ret_10=ret_10,
                ret_20=ret_20,
                close=close,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                volume_ratio=volume_ratio,
                volatility_20=volatility_20,
                max_drawdown_20=max_drawdown_20,
                latest_pct=latest_pct,
                turnover=turnover,
            )
            if quant_score < 45:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "name": str(latest["name"]),
                    "trade_date": latest_trade_date.isoformat(),
                    "close_price": round(close, 4),
                    "pct_change": round(latest_pct, 4),
                    "turnover_rate": round(turnover, 4),
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
                    "quant_score": round(quant_score, 4),
                    "quant_reason": _build_quant_reason(
                        ret_5, ret_20, volume_ratio, above_ma20
                    ),
                    "risk": _build_risk(latest_pct, volatility_20, max_drawdown_20),
                }
            )

        rows.sort(key=lambda item: item["quant_score"], reverse=True)
        return rows[:limit] if limit is not None else rows

    def _should_use_llm_adaptive(
        self, all_candidates: list[dict[str, Any]], final_limit: int
    ) -> bool:
        """按 120 日回测得到的市场状态门控决定是否启用 LLM 复核。"""

        if not self.config.t1_llm_adaptive_gate_enabled:
            return True
        if not all_candidates:
            return False
        top_rows = all_candidates[: self.config.t1_llm_gate_top_n or final_limit]
        top_pct_avg = sum(float(row["pct_change"]) for row in top_rows) / max(
            1, len(top_rows)
        )
        candidate_count = len(all_candidates)
        enabled = (
            candidate_count <= self.config.t1_llm_adaptive_candidate_count_max
            and top_pct_avg <= self.config.t1_llm_adaptive_top_pct_max
        )
        logger.info(
            "T+1 LLM 自适应门控：enabled=%s candidate_count=%s top_pct_avg=%.4f",
            enabled,
            candidate_count,
            top_pct_avg,
        )
        return enabled

    def _build_adaptive_skip_payload(
        self, all_candidates: list[dict[str, Any]], final_limit: int
    ) -> dict[str, Any]:
        """生成胜率优先门控跳过交易的审计信息。"""

        top_rows = all_candidates[: self.config.t1_llm_gate_top_n or final_limit]
        top_pct_avg = (
            sum(float(row["pct_change"]) for row in top_rows) / len(top_rows)
            if top_rows
            else 0.0
        )
        return {
            "adaptive_gate": "skip",
            "reason": "候选池过热或候选数量过多，胜率优先模式跳过本次T+1交易",
            "candidate_count": len(all_candidates),
            "candidate_count_max": self.config.t1_llm_adaptive_candidate_count_max,
            "top_pct_avg": round(top_pct_avg, 4),
            "top_pct_max": self.config.t1_llm_adaptive_top_pct_max,
            "reject_action": self.config.t1_llm_adaptive_reject_action,
        }

    def _build_llm_skip_payload(
        self, all_candidates: list[dict[str, Any]], final_limit: int, reason: str
    ) -> dict[str, Any]:
        """生成 LLM 严格模式跳过交易的审计信息。"""

        payload = self._build_adaptive_skip_payload(all_candidates, final_limit)
        payload["adaptive_gate"] = "llm_required_skip"
        payload["reason"] = reason[:500]
        payload["require_review"] = self.config.t1_llm_require_review
        return payload

    def _build_quant_payload(
        self, candidates: list[dict[str, Any]], final_limit: int, adaptive_gate: str
    ) -> list[dict[str, Any]]:
        """生成量化候选载荷，用于禁用 LLM 或非严格模式降级。"""

        return [
            {
                "symbol": row["symbol"],
                "llm_score": row["quant_score"],
                "reason": row["quant_reason"],
                "risk": row["risk"],
                "adaptive_gate": adaptive_gate,
            }
            for row in candidates[:final_limit]
        ]

    def _ask_llm(
        self, candidates: list[dict[str, Any]], final_limit: int, trade_date: date
    ) -> list[dict[str, Any]]:
        """请求大模型对量化前排候选做保守复核。"""

        review_limit = min(
            len(candidates),
            max(final_limit, final_limit * self.config.t1_llm_review_pool_multiplier),
        )
        review_candidates = candidates[:review_limit]

        messages = [
            {
                "role": "system",
                "content": (
                    "你是A股T+1短线风险复核助手。量化排序是主决策，你只做小幅加减分"
                    "和风险否决，不得大幅重排，不得编造候选池之外的股票。"
                    "历史模拟显示BOOST容易放大回撤，只有极少数低风险共振才可BOOST；"
                    "默认把可交易候选标为KEEP，把追高、放量过猛、回撤恶化标为DOWNRANK或AVOID。"
                    "输出必须是JSON数组。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "对量化前排候选做T+1保守复核",
                        "trade_date": trade_date.isoformat(),
                        "final_limit": final_limit,
                        "review_limit": review_limit,
                        "execution_rule": "D日收盘后出信号，D+1开盘买入，D+2开盘卖出",
                        "selection_rules": [
                            "剔除ST和退市股已经由程序完成",
                            "量化分是主依据，只有明显风险或明显共振时才调整",
                            "请尽量覆盖每个候选并给出action，便于程序做风险加权",
                            "优先保留量化前10，不要为了多样性替换强势候选",
                            "对短线过热、回撤恶化、量价背离的候选输出DOWNRANK或AVOID",
                            "KEEP表示可进入实盘模拟候选，BOOST只用于极高置信且风险更低的例外情形",
                            "如果上涨来自单日脉冲、波动突然放大或20日回撤压力较大，不要输出BOOST",
                            "只输出候选池中的symbol",
                            "llm_score范围0到100，建议围绕quant_score小幅调整",
                            "action只能是BOOST、KEEP、DOWNRANK、AVOID",
                        ],
                        "output_schema": [
                            {
                                "symbol": "股票代码",
                                "llm_score": 0,
                                "action": "BOOST|KEEP|DOWNRANK|AVOID",
                                "reason": "看涨理由，30字以内",
                                "risk": "主要风险，30字以内",
                            }
                        ],
                        "candidates": review_candidates,
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

    def _merge_llm(
        self,
        candidates: list[dict[str, Any]],
        llm_payload: list[dict[str, Any]],
        final_limit: int,
        trade_date: date,
    ) -> list[dict[str, Any]]:
        """用量化锚定方式合并大模型小幅复核结果。"""

        llm_map = _index_llm_payload(llm_payload)
        scored: list[dict[str, Any]] = []
        allowed_actions = self.config.t1_llm_allowed_action_set()
        review_limit = max(final_limit, final_limit * self.config.t1_llm_review_pool_multiplier)
        entry_limit = max(
            final_limit,
            final_limit * self.config.t1_llm_entry_limit_multiplier,
        )
        max_adjustment = self.config.t1_llm_max_adjustment
        for item in llm_payload:
            if "symbol" in item:
                item["symbol"] = str(item.get("symbol", "")).zfill(6)

        for rank_index, base in enumerate(candidates, start=1):
            if (
                self.config.t1_llm_candidate_pct_change_max > 0
                and float(base.get("pct_change") or 0)
                > self.config.t1_llm_candidate_pct_change_max
            ):
                continue
            item = llm_map.get(base["symbol"], {})
            llm_score = _read_llm_score(item, base["quant_score"])
            action = str(item.get("action") or "KEEP").upper()
            if action not in allowed_actions:
                continue
            adjustment = _clamp(
                (llm_score - base["quant_score"]) * self.config.t1_llm_score_weight,
                -max_adjustment,
                max_adjustment,
            )
            if action == "BOOST":
                adjustment = min(max_adjustment, adjustment + max_adjustment * 0.25)
            elif action == "DOWNRANK":
                adjustment = max(-max_adjustment, adjustment - max_adjustment * 0.5)
            elif action == "AVOID":
                adjustment = -max_adjustment - self.config.t1_llm_avoid_penalty

            if rank_index <= self.config.t1_llm_protected_top_n and action != "AVOID":
                adjustment = max(0.0, adjustment)
            rank_penalty = (rank_index - 1) * self.config.t1_llm_rank_penalty
            if rank_index > review_limit:
                adjustment = min(0.0, adjustment)
            final_score = base["quant_score"] + adjustment - rank_penalty
            if rank_index > entry_limit:
                final_score -= 100.0
            scored.append(
                _to_pick_row(
                    base=base,
                    rank_no=0,
                    llm_score=llm_score,
                    final_score=final_score,
                    reason=str(item.get("reason") or base["quant_reason"]),
                    risk=str(item.get("risk") or base["risk"]),
                    trade_date=trade_date,
                )
            )

        scored.sort(key=lambda item: item["final_score"], reverse=True)
        result_limit = final_limit
        if self.config.t1_llm_final_pick_limit > 0:
            result_limit = min(result_limit, self.config.t1_llm_final_pick_limit)
        for index, row in enumerate(scored[:result_limit], start=1):
            row["rank_no"] = index
        return scored[:result_limit]


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


def _index_llm_payload(llm_payload: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按股票代码索引大模型复核结果，忽略重复和异常项。"""

    indexed: dict[str, dict[str, Any]] = {}
    for item in llm_payload:
        symbol = str(item.get("symbol", "")).zfill(6)
        if len(symbol) != 6 or symbol in indexed:
            continue
        indexed[symbol] = item
    return indexed


def _read_llm_score(item: dict[str, Any], fallback: float) -> float:
    """读取大模型分数，异常时退回量化分。"""

    try:
        return _clamp(float(item.get("llm_score")), 0, 100)
    except (TypeError, ValueError):
        return fallback


def _score_candidate(**factors: float) -> float:
    """根据技术因子计算 0 到 100 的量化评分。"""

    score = 45.0
    score += _clamp(factors["ret_5"], -8, 8) * 1.0
    score += _clamp(factors["ret_10"], -12, 12) * 0.55
    score += _clamp(factors["ret_20"], -18, 18) * 0.25
    if factors["close"] > factors["ma5"] > factors["ma10"] > factors["ma20"]:
        score += 10
    elif factors["close"] > factors["ma20"]:
        score += 5
    score += _clamp((factors["volume_ratio"] - 1) * 10, -6, 8)
    score -= max(0.0, factors["volatility_20"] - 4.5) * 1.8
    score += _clamp(factors["max_drawdown_20"] + 12, -6, 4)
    if 0.5 <= factors["latest_pct"] <= 5.5:
        score += 5
    elif factors["latest_pct"] > 7:
        score -= 8
    if 1 <= factors["turnover"] <= 12:
        score += 3
    return _clamp(score, 0, 100)


def _build_quant_reason(
    ret_5: float, ret_20: float, volume_ratio: float, above_ma20: float
) -> str:
    """生成量化入选理由。"""

    return (
        f"5日{ret_5:.2f}%，20日{ret_20:.2f}%，"
        f"量比{volume_ratio:.2f}，较20日线{above_ma20:.2f}%"
    )


def _build_risk(latest_pct: float, volatility_20: float, max_drawdown_20: float) -> str:
    """生成候选股票主要风险描述。"""

    risks = []
    if latest_pct > 6:
        risks.append("短线涨幅偏高")
    if volatility_20 > 5:
        risks.append("波动偏大")
    if max_drawdown_20 < -15:
        risks.append("回撤压力")
    return "；".join(risks) if risks else "注意市场和板块波动"


def _to_pick_row(
    base: dict[str, Any],
    rank_no: int,
    llm_score: float,
    final_score: float,
    reason: str,
    risk: str,
    trade_date: date,
) -> dict[str, Any]:
    """转换为 stock_analysis_pick 表行。"""

    return {
        "rank_no": rank_no,
        "symbol": base["symbol"],
        "name": base["name"],
        "trade_date": trade_date.isoformat(),
        "quant_score": Decimal(str(round(base["quant_score"], 4))),
        "llm_score": Decimal(str(round(llm_score, 4))),
        "final_score": Decimal(str(round(final_score, 4))),
        "expected_direction": "T+1上涨",
        "reason": reason[:500],
        "risk": risk[:500],
    }


def _clamp(value: float, low: float, high: float) -> float:
    """将数值限制在指定区间内。"""

    return max(low, min(high, value))
