"""T+1 盘中质量选股服务。

该模块负责在交易日 14:05 后基于实时行情、历史因子和 LLM 复核生成
T+1 质量候选，并在复核成功后复用 T+1 专属模拟交易账户执行买入。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .llm_client import LLMClient
from .market_data import AkshareMarketData
from .realtime import a_share_market_status

if TYPE_CHECKING:
    from .config import AppConfig
    from .db import MySQLRepository

logger = logging.getLogger(__name__)

QUALITY_EXPECTED_DIRECTION = "T日买入，T+1可卖"
DEFAULT_QUALITY_SCHEDULE_TIME = "14:05"


@dataclass(slots=True)
class T1QualityRunResult:
    """T+1 质量选股单次运行结果。"""

    trade_date: date | None
    snapshot_time: datetime
    trigger_type: str
    status: str
    quote_count: int = 0
    valid_quote_count: int = 0
    candidate_count: int = 0
    pick_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    execute_trades: bool = True
    llm_required: bool = True
    llm_success: bool = False
    market_session: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error_message: str = ""
    run_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可返回的字典。"""
        return {
            "runId": self.run_id,
            "tradeDate": self.trade_date.isoformat() if self.trade_date else None,
            "snapshotTime": _datetime_to_str(self.snapshot_time),
            "triggerType": self.trigger_type,
            "status": self.status,
            "quoteCount": self.quote_count,
            "validQuoteCount": self.valid_quote_count,
            "candidateCount": self.candidate_count,
            "pickCount": self.pick_count,
            "buyCount": self.buy_count,
            "sellCount": self.sell_count,
            "executeTrades": self.execute_trades,
            "llmRequired": self.llm_required,
            "llmSuccess": self.llm_success,
            "marketSession": self.market_session,
            "skipped": self.skipped,
            "skipReason": self.skip_reason,
            "errorMessage": self.error_message,
        }


def parse_quality_schedule_time(value: str | None) -> tuple[int, int]:
    """解析 T+1 质量选股触发时间，要求不早于 14:00。"""
    text = (value or DEFAULT_QUALITY_SCHEDULE_TIME).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError("T1_QUALITY_SCHEDULE_TIME 必须使用 HH:MM 格式") from exc
    if hour not in range(24) or minute not in range(60):
        raise ValueError("T1_QUALITY_SCHEDULE_TIME 的小时或分钟超出范围")
    if (hour, minute) < (14, 0):
        raise ValueError("T1_QUALITY_SCHEDULE_TIME 不能早于 14:00")
    if (hour, minute) > (15, 0):
        raise ValueError("T1_QUALITY_SCHEDULE_TIME 不能晚于 15:00")
    return hour, minute


def is_t1_quality_window_open(
    now: datetime,
    timezone: str = "Asia/Shanghai",
    schedule_time: str | None = DEFAULT_QUALITY_SCHEDULE_TIME,
) -> tuple[bool, str]:
    """判断当前是否允许执行 14 点后 T+1 质量选股。"""
    hour, minute = parse_quality_schedule_time(schedule_time)
    zone = ZoneInfo(timezone)
    local_now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    market_status = a_share_market_status(local_now, timezone)
    if not market_status.get("marketOpen") or market_status.get("session") != "afternoon":
        return False, str(market_status.get("reason") or "非下午连续竞价时段")
    threshold = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now < threshold:
        return False, f"尚未到达 T+1 质量选股时间 {hour:02d}:{minute:02d}"
    return True, "T+1 质量选股窗口已开启"


def calculate_quality_score(
    *,
    latest_price: float,
    pct_change: float,
    volume_ratio: float,
    turnover_rate: float,
    amount: float,
    amplitude: float,
    name: str,
    history: Mapping[str, float],
) -> dict[str, float]:
    """计算盘中质量候选的趋势、动量、流动性、风险和综合分。"""
    trend_score = _score_trend(latest_price, history)
    momentum_score = _score_momentum(pct_change, volume_ratio)
    liquidity_score = _score_liquidity(amount, turnover_rate)
    risk_score = _score_risk(pct_change, amplitude, name)
    final_score = _clamp(
        trend_score * 0.32
        + momentum_score * 0.28
        + liquidity_score * 0.18
        + risk_score * 0.22,
        0,
        100,
    )
    return {
        "trendScore": round(trend_score, 4),
        "momentumScore": round(momentum_score, 4),
        "liquidityScore": round(liquidity_score, 4),
        "riskScore": round(risk_score, 4),
        "quantScore": round(final_score, 4),
        "finalScore": round(final_score, 4),
    }


def build_quality_run_row(
    *,
    trade_date: date,
    snapshot_time: datetime,
    trigger_type: str,
    status: str,
    quote_count: int,
    valid_quote_count: int,
    candidate_count: int,
    pick_count: int,
    buy_count: int,
    sell_count: int,
    execute_trades: bool,
    llm_required: bool,
    llm_success: bool,
    market_session: str,
    skip_reason: str,
    error_message: str,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 T+1 质量选股运行记录行。"""
    return {
        "trade_date": trade_date,
        "snapshot_time": snapshot_time.replace(tzinfo=None),
        "trigger_type": trigger_type,
        "status": status,
        "quote_count": int(quote_count),
        "valid_quote_count": int(valid_quote_count),
        "candidate_count": int(candidate_count),
        "pick_count": int(pick_count),
        "buy_count": int(buy_count),
        "sell_count": int(sell_count),
        "execute_trades": bool(execute_trades),
        "llm_required": bool(llm_required),
        "llm_success": bool(llm_success),
        "market_session": market_session,
        "skip_reason": skip_reason,
        "error_message": error_message,
        "summary_json": json.dumps(summary or {}, ensure_ascii=False),
    }


def build_quality_pick_row(
    *,
    run_id: int,
    trade_date: date,
    snapshot_time: datetime,
    rank_no: int,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """构造 T+1 质量选股候选记录行。"""
    return {
        "run_id": int(run_id),
        "trade_date": trade_date,
        "snapshot_time": snapshot_time.replace(tzinfo=None),
        "rank_no": int(rank_no),
        "symbol": candidate.get("symbol"),
        "name": candidate.get("name"),
        "latest_price": _float_value(candidate.get("latestPrice")),
        "pct_change": _float_value(candidate.get("pctChange")),
        "volume_ratio": _float_value(candidate.get("volumeRatio")),
        "turnover_rate": _float_value(candidate.get("turnoverRate")),
        "trend_score": _float_value(candidate.get("trendScore")),
        "momentum_score": _float_value(candidate.get("momentumScore")),
        "liquidity_score": _float_value(candidate.get("liquidityScore")),
        "risk_score": _float_value(candidate.get("riskScore")),
        "quant_score": _float_value(candidate.get("quantScore")),
        "llm_score": candidate.get("llmScore"),
        "final_score": _float_value(candidate.get("finalScore")),
        "signal_action": str(candidate.get("action") or "WATCH").upper(),
        "expected_direction": QUALITY_EXPECTED_DIRECTION,
        "reason": str(candidate.get("reason") or ""),
        "risk": str(candidate.get("risk") or ""),
        "factor_snapshot": json.dumps(candidate.get("factorSnapshot") or {}, ensure_ascii=False),
        "raw_response": json.dumps(candidate.get("rawResponse") or {}, ensure_ascii=False),
    }


class T1IntradayQualitySelector:
    """T+1 盘中质量选股编排器。"""

    def __init__(
        self,
        settings: "AppConfig",
        repository: "MySQLRepository | None" = None,
        market_data: AkshareMarketData | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        """初始化配置、仓储、行情源和 LLM 客户端。"""
        from .db import MySQLRepository

        self.settings = settings
        self.repository = repository or MySQLRepository(settings)
        self.market_data = market_data or AkshareMarketData(daily_provider=getattr(settings, "daily_provider", "sina"))
        self.llm_client = llm_client or LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    def run_once(
        self,
        *,
        execute_trades: bool | None = None,
        final_limit: int | None = None,
        force: bool = False,
        trigger_type: str = "manual",
        now: datetime | None = None,
    ) -> T1QualityRunResult:
        """执行一次 T+1 盘中质量选股。"""
        from .t1_trading import T1TradingEngine

        self.repository.ensure_schema()
        snapshot_time = now or datetime.now(ZoneInfo(getattr(self.settings, "timezone", "Asia/Shanghai")))
        snapshot_time = snapshot_time.replace(microsecond=0)
        trade_date = snapshot_time.date()
        execute = bool(getattr(self.settings, "t1_quality_execute_trades", 1) if execute_trades is None else execute_trades)
        limit = int(final_limit or getattr(self.settings, "t1_quality_final_limit", 20))
        llm_required = bool(getattr(self.settings, "t1_quality_require_llm", 1))

        if not bool(getattr(self.settings, "t1_quality_enabled", 1)):
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                execute_trades=execute,
                llm_required=llm_required,
                skip_reason="T+1 质量选股已关闭",
            )

        if not force:
            open_, reason = is_t1_quality_window_open(
                snapshot_time,
                getattr(self.settings, "timezone", "Asia/Shanghai"),
                getattr(self.settings, "t1_quality_schedule_time", DEFAULT_QUALITY_SCHEDULE_TIME),
            )
            if not open_:
                return self._record_result(
                    trade_date=trade_date,
                    snapshot_time=snapshot_time,
                    trigger_type=trigger_type,
                    status="SKIPPED",
                    execute_trades=execute,
                    llm_required=llm_required,
                    market_session="closed",
                    skip_reason=reason,
                )
            if self.repository.has_successful_t1_quality_run(trade_date):
                return self._record_result(
                    trade_date=trade_date,
                    snapshot_time=snapshot_time,
                    trigger_type=trigger_type,
                    status="SKIPPED",
                    execute_trades=execute,
                    llm_required=llm_required,
                    market_session="afternoon",
                    skip_reason="当天 T+1 质量选股已成功运行，避免重复买入",
                )

        quotes = self.market_data.fetch_realtime_quotes()
        valid_quotes = [item for item in quotes if _float_value(item.get("latest_price")) > 0]
        min_quote_count = int(getattr(self.settings, "t1_quality_min_quote_count", 1000))
        min_valid_quote_count = int(getattr(self.settings, "t1_quality_min_valid_quote_count", 300))
        if len(quotes) < min_quote_count or len(valid_quotes) < min_valid_quote_count:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                execute_trades=execute,
                llm_required=llm_required,
                market_session="afternoon",
                skip_reason="实时行情数量不足，已跳过买入",
            )

        candidates = self._build_candidates(
            valid_quotes,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
        )
        gate_open, gate_status = self._quality_adaptive_gate(candidates, valid_quotes)
        if not gate_open:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=len(candidates),
                execute_trades=execute,
                llm_required=llm_required,
                market_session="afternoon",
                skip_reason=str(gate_status.get("reason") or "质量策略高置信门控未通过"),
                summary={"gate": gate_status},
            )

        review_limit = max(
            limit,
            int(getattr(self.settings, "t1_quality_llm_review_limit", 20)),
            1,
        )
        reviewed, llm_status = self._review_with_llm(candidates[:review_limit])
        if llm_required and not llm_status.get("llm_success"):
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=len(candidates),
                execute_trades=execute,
                llm_required=llm_required,
                market_session="afternoon",
                skip_reason=str(llm_status.get("skip_reason") or "LLM 复核失败，已跳过买入"),
                summary={"llm": llm_status},
            )

        picks = reviewed[:limit]
        run_id = self.repository.insert_t1_quality_run(
            build_quality_run_row(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SUCCESS",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=len(candidates),
                pick_count=len(picks),
                buy_count=0,
                sell_count=0,
                execute_trades=execute,
                llm_required=llm_required,
                llm_success=bool(llm_status.get("llm_success")),
                market_session="afternoon",
                skip_reason="",
                error_message="",
                summary={"llm": llm_status},
            )
        )
        pick_rows = [
            build_quality_pick_row(
                run_id=run_id,
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                rank_no=index + 1,
                candidate=pick,
            )
            for index, pick in enumerate(picks)
        ]
        self.repository.replace_t1_quality_picks(run_id, pick_rows)
        buy_count = T1TradingEngine(self.settings, self.repository).execute_quality_buys(
            trade_date=trade_date,
            candidates=[_to_t1_candidate(item) for item in picks if str(item.get("action") or "").upper() == "BUY"],
            execute_trades=execute,
        )
        self.repository.update_t1_quality_run(run_id, {"buy_count": buy_count, "sell_count": 0})
        return T1QualityRunResult(
            run_id=run_id,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
            trigger_type=trigger_type,
            status="SUCCESS",
            quote_count=len(quotes),
            valid_quote_count=len(valid_quotes),
            candidate_count=len(candidates),
            pick_count=len(picks),
            buy_count=buy_count,
            sell_count=0,
            execute_trades=execute,
            llm_required=llm_required,
            llm_success=bool(llm_status.get("llm_success")),
            market_session="afternoon",
        )

    def _build_candidates(
        self,
        quotes: Sequence[Mapping[str, Any]],
        *,
        trade_date: date | None = None,
        snapshot_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """按实时行情构建量化质量候选。"""
        candidates: list[dict[str, Any]] = []
        max_pct = float(getattr(self.settings, "t1_quality_max_pct_change", 5.8))
        max_amplitude = float(getattr(self.settings, "t1_quality_max_amplitude", 8.0))
        min_volume_ratio = float(getattr(self.settings, "t1_quality_min_volume_ratio", 1.2))
        require_uptrend = bool(getattr(self.settings, "t1_quality_require_uptrend", 1))
        min_score = float(getattr(self.settings, "t1_quality_min_final_score", 66.0))
        min_risk = float(getattr(self.settings, "t1_quality_min_risk_score", 55.0))
        max_volatility_20 = float(getattr(self.settings, "t1_quality_max_volatility_20", 4.5))
        max_drawdown_20 = float(getattr(self.settings, "t1_quality_max_drawdown_20", 12.0))
        history_by_symbol = self._load_quality_history(
            [str(quote.get("symbol") or "").strip() for quote in quotes],
            trade_date,
        )
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").strip()
            name = str(quote.get("name") or "").strip()
            if not symbol or _is_risk_name(name) or name.startswith(("N", "C")):
                continue
            latest_price = _float_value(quote.get("latest_price"))
            pct_change = _float_value(quote.get("pct_change"))
            if latest_price <= 0 or pct_change <= 0 or pct_change > max_pct:
                continue
            history_rows = history_by_symbol.get(symbol, [])
            history = _quality_history_factors(history_rows, latest_price)
            if require_uptrend and not _has_quality_uptrend(latest_price, history):
                continue
            if max_volatility_20 > 0 and history.get("volatility20", 0.0) > max_volatility_20:
                continue
            if max_drawdown_20 > 0 and history.get("maxDrawdown20", 0.0) > max_drawdown_20:
                continue
            amplitude = _float_value(quote.get("amplitude"))
            if max_amplitude > 0 and amplitude > max_amplitude:
                continue
            volume_ratio = _resolve_quality_volume_ratio(
                quote,
                history_rows,
                snapshot_time=snapshot_time,
            )
            if min_volume_ratio > 0 and volume_ratio < min_volume_ratio:
                continue
            turnover_rate = _float_value(quote.get("turnover_rate"))
            amount = _float_value(quote.get("amount"))
            score = calculate_quality_score(
                latest_price=latest_price,
                pct_change=pct_change,
                volume_ratio=volume_ratio,
                turnover_rate=turnover_rate,
                amount=amount,
                amplitude=amplitude,
                name=name,
                history=history,
            )
            if score["finalScore"] < min_score or score["riskScore"] < min_risk:
                continue
            factor_snapshot = _snapshot_mapping(quote)
            factor_snapshot.update(
                {
                    "qualityHistory": history,
                    "qualityFilters": {
                        "maxPctChange": max_pct,
                        "maxAmplitude": max_amplitude,
                        "minVolumeRatio": min_volume_ratio,
                        "requireUptrend": require_uptrend,
                        "maxVolatility20": max_volatility_20,
                        "maxDrawdown20": max_drawdown_20,
                    },
                    "resolvedVolumeRatio": round(volume_ratio, 4),
                }
            )
            candidates.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "latestPrice": latest_price,
                    "price": latest_price,
                    "pctChange": pct_change,
                    "volumeRatio": volume_ratio,
                    "turnoverRate": turnover_rate,
                    "amount": amount,
                    "amplitude": amplitude,
                    **score,
                    "llmScore": None,
                    "action": "BUY",
                    "reason": "14:05盘中质量分达标，历史趋势向上且量能风险达标",
                    "risk": "盘中行情波动，T日买入后当日不可卖",
                    "factorSnapshot": factor_snapshot,
                    "rawResponse": {},
                }
            )
        candidates.sort(key=lambda item: float(item.get("finalScore") or 0), reverse=True)
        return candidates[: int(getattr(self.settings, "t1_quality_preselect_limit", 120))]

    def _load_quality_history(
        self,
        symbols: Sequence[str],
        trade_date: date | None,
    ) -> dict[str, list[dict[str, Any]]]:
        """读取质量策略所需的上一交易日前历史日线。"""

        if trade_date is None:
            return {}
        lookback = int(getattr(self.settings, "t1_quality_history_lookback_days", 30))
        try:
            return self.repository.recent_stock_daily_history(symbols, trade_date, lookback)
        except Exception as exc:
            logger.warning("读取 T+1 质量选股历史因子失败: %s", exc)
            return {}

    def _quality_adaptive_gate(
        self,
        candidates: Sequence[Mapping[str, Any]],
        quotes: Sequence[Mapping[str, Any]],
    ) -> tuple[bool, dict[str, Any]]:
        """根据候选池热度和全市场状态决定是否进入 LLM 复核。"""

        if not bool(getattr(self.settings, "t1_quality_gate_enabled", 1)):
            return True, {"enabled": False, "reason": ""}
        gate_top_n = max(1, int(getattr(self.settings, "t1_quality_gate_top_n", 10)))
        candidate_count = len(candidates)
        top_candidates = list(candidates[:gate_top_n])
        top_pct_avg = _mean_value(item.get("pctChange") for item in top_candidates)
        market_pct_values = [
            _float_value(quote.get("pct_change"))
            for quote in quotes
            if _float_value(quote.get("latest_price")) > 0
        ]
        market_pct_avg = _mean_value(market_pct_values)
        market_positive_ratio = (
            sum(1 for value in market_pct_values if value > 0) / len(market_pct_values) * 100
            if market_pct_values
            else 0.0
        )
        status = {
            "enabled": True,
            "candidateCount": candidate_count,
            "gateTopN": gate_top_n,
            "topPctAvg": round(top_pct_avg, 4),
            "marketPctAvg": round(market_pct_avg, 4),
            "marketPositiveRatio": round(market_positive_ratio, 2),
        }
        if candidate_count <= 0:
            status["reason"] = "高置信门控：无质量候选"
            return False, status
        min_candidates = int(getattr(self.settings, "t1_quality_candidate_count_min", 20))
        if min_candidates > 0 and candidate_count < min_candidates:
            status["reason"] = f"高置信门控：候选池 {candidate_count} 低于下限 {min_candidates}"
            return False, status
        max_candidates = int(getattr(self.settings, "t1_quality_candidate_count_max", 300))
        if max_candidates > 0 and candidate_count > max_candidates:
            status["reason"] = f"高置信门控：候选池 {candidate_count} 超过上限 {max_candidates}"
            return False, status
        top_pct_max = float(getattr(self.settings, "t1_quality_top_pct_avg_max", 3.5))
        if top_pct_max > 0 and top_pct_avg > top_pct_max:
            status["reason"] = f"高置信门控：前排平均涨幅 {top_pct_avg:.2f}% 过热"
            return False, status
        market_min = float(getattr(self.settings, "t1_quality_market_pct_avg_min", 0.5))
        market_max = float(getattr(self.settings, "t1_quality_market_pct_avg_max", 2.0))
        if market_pct_avg < market_min or market_pct_avg > market_max:
            status["reason"] = f"高置信门控：全市场平均涨幅 {market_pct_avg:.2f}% 不在优势区间"
            return False, status
        status["reason"] = "高置信门控通过"
        return True, status

    def _review_with_llm(self, candidates: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """使用 LLM 严格复核盘中质量候选，失败则不允许自动买入。"""
        if not candidates:
            return [], {"llm_success": False, "skip_reason": "无可提交 LLM 复核的质量候选"}
        if not bool(getattr(self.settings, "t1_quality_use_llm", 1)):
            return [dict(item) for item in candidates], {"llm_success": True, "skip_reason": "", "mode": "rules"}
        payload = [
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "latestPrice": item.get("latestPrice"),
                "pctChange": item.get("pctChange"),
                "quantScore": item.get("quantScore"),
                "finalScore": item.get("finalScore"),
                "reason": item.get("reason"),
                "risk": item.get("risk"),
            }
            for item in candidates
        ]
        messages = [
            {
                "role": "system",
                "content": "你是谨慎的A股T+1盘中质量选股复核助手，只能返回JSON数组。",
            },
            {
                "role": "user",
                "content": (
                    "请复核以下候选。策略语义：T日14:05盘中出信号，T日盘中模拟买入，"
                    "T+1下一交易日起可卖，不保证必须卖出。只允许返回候选池内股票。"
                    "每项字段：symbol,name,action(KEEP或AVOID),llmScore,reason,risk。"
                    f"候选JSON：{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            raw_response = self.llm_client.chat_json(
                messages,
                max_tokens=int(getattr(self.settings, "t1_quality_llm_max_tokens", 4096)),
            )
        except Exception as exc:
            logger.warning("T+1质量选股 LLM 复核失败: %s", exc)
            return [], {"llm_success": False, "skip_reason": "LLM 复核失败，已跳过买入", "error": str(exc)}
        items = raw_response if isinstance(raw_response, list) else raw_response.get("picks", []) if isinstance(raw_response, dict) else []
        by_symbol = {str(item.get("symbol")): dict(item) for item in candidates}
        reviewed: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "")
            action = str(item.get("action") or "").upper()
            if symbol not in by_symbol or action != "KEEP":
                continue
            candidate = by_symbol[symbol]
            llm_score = _float_value(item.get("llmScore"), _float_value(candidate.get("finalScore")))
            candidate.update(
                {
                    "llmScore": llm_score,
                    "finalScore": round((float(candidate.get("quantScore") or 0) * 0.8) + (llm_score * 0.2), 4),
                    "action": "BUY",
                    "reason": f"{candidate.get('reason')}; LLM复核: {item.get('reason') or '同意保留'}"[:1000],
                    "risk": f"{candidate.get('risk')}; LLM风险: {item.get('risk') or '注意盘中波动'}"[:1000],
                    "rawResponse": item,
                }
            )
            reviewed.append(candidate)
        reviewed.sort(key=lambda item: float(item.get("finalScore") or 0), reverse=True)
        if not reviewed:
            return [], {"llm_success": False, "skip_reason": "LLM 未保留任何质量候选，已跳过买入", "raw": raw_response}
        return reviewed, {"llm_success": True, "skip_reason": "", "raw": raw_response}

    def _record_result(
        self,
        *,
        trade_date: date,
        snapshot_time: datetime,
        trigger_type: str,
        status: str,
        execute_trades: bool,
        llm_required: bool,
        quote_count: int = 0,
        valid_quote_count: int = 0,
        candidate_count: int = 0,
        pick_count: int = 0,
        buy_count: int = 0,
        sell_count: int = 0,
        market_session: str = "",
        skip_reason: str = "",
        error_message: str = "",
        summary: Mapping[str, Any] | None = None,
    ) -> T1QualityRunResult:
        """写入跳过/失败运行结果并返回结构化结果。"""
        run_id = self.repository.insert_t1_quality_run(
            build_quality_run_row(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status=status,
                quote_count=quote_count,
                valid_quote_count=valid_quote_count,
                candidate_count=candidate_count,
                pick_count=pick_count,
                buy_count=buy_count,
                sell_count=sell_count,
                execute_trades=execute_trades,
                llm_required=llm_required,
                llm_success=False,
                market_session=market_session,
                skip_reason=skip_reason,
                error_message=error_message,
                summary=summary,
            )
        )
        return T1QualityRunResult(
            run_id=run_id,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
            trigger_type=trigger_type,
            status=status,
            quote_count=quote_count,
            valid_quote_count=valid_quote_count,
            candidate_count=candidate_count,
            pick_count=pick_count,
            buy_count=buy_count,
            sell_count=sell_count,
            execute_trades=execute_trades,
            llm_required=llm_required,
            llm_success=False,
            market_session=market_session,
            skipped=status == "SKIPPED",
            skip_reason=skip_reason,
            error_message=error_message,
        )


def _to_t1_candidate(item: Mapping[str, Any]) -> dict[str, Any]:
    """转换为 T1TradingEngine 可消费的候选结构。"""
    return {
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "price": item.get("latestPrice") or item.get("price"),
        "finalScore": item.get("finalScore"),
        "reason": item.get("reason"),
        "risk": item.get("risk"),
    }


def _score_trend(price: float, history: Mapping[str, float]) -> float:
    """计算实时价格相对均线和历史收益的趋势分。"""
    ma5 = float(history.get("ma5") or 0)
    ma20 = float(history.get("ma20") or 0)
    ret5 = float(history.get("ret5") or 0)
    ret20 = float(history.get("ret20") or 0)
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


def _quality_history_factors(
    rows: Sequence[Mapping[str, Any]],
    latest_price: float,
) -> dict[str, float]:
    """根据最近完整日线计算 14:05 质量选股趋势因子。"""

    closes = [_float_value(row.get("close_price")) for row in rows]
    closes = [value for value in closes if value > 0]
    if not closes:
        return {
            "ma5": 0.0,
            "ma20": 0.0,
            "ret5": 0.0,
            "ret20": 0.0,
            "volatility20": 0.0,
            "maxDrawdown20": 0.0,
        }
    ma5_values = closes[-5:]
    ma20_values = closes[-20:]
    base5 = closes[-5] if len(closes) >= 5 else closes[0]
    base20 = closes[-20] if len(closes) >= 20 else closes[0]
    daily_returns = [
        (current / previous - 1) * 100
        for previous, current in zip(closes[-21:-1], closes[-20:])
        if previous > 0
    ]
    return {
        "ma5": round(sum(ma5_values) / len(ma5_values), 4),
        "ma20": round(sum(ma20_values) / len(ma20_values), 4),
        "ret5": round((latest_price / base5 - 1) * 100, 4) if base5 > 0 else 0.0,
        "ret20": round((latest_price / base20 - 1) * 100, 4) if base20 > 0 else 0.0,
        "volatility20": round(_stddev(daily_returns), 4),
        "maxDrawdown20": round(_max_drawdown_magnitude(closes[-20:]), 4),
    }


def _has_quality_uptrend(latest_price: float, history: Mapping[str, float]) -> bool:
    """判断候选是否满足短中期趋势向上。"""

    ma5 = _float_value(history.get("ma5"))
    ma20 = _float_value(history.get("ma20"))
    ret5 = _float_value(history.get("ret5"))
    return ma5 > ma20 > 0 and latest_price > ma5 and ret5 > 0


def _resolve_quality_volume_ratio(
    quote: Mapping[str, Any],
    history_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_time: datetime | None,
) -> float:
    """优先使用实时量比，缺失时按盘中成交量相对近 5 日均量估算。"""

    ratio = _float_value(quote.get("volume_ratio"))
    if ratio > 0:
        return ratio
    current_volume = _float_value(quote.get("volume"))
    recent_volumes = [_float_value(row.get("volume")) for row in history_rows[-5:]]
    recent_volumes = [value for value in recent_volumes if value > 0]
    if current_volume <= 0 or not recent_volumes:
        return 1.0
    avg_volume = sum(recent_volumes) / len(recent_volumes)
    elapsed_ratio = _quality_session_elapsed_ratio(snapshot_time)
    if avg_volume <= 0 or elapsed_ratio <= 0:
        return 1.0
    return current_volume / (avg_volume * elapsed_ratio)


def _quality_session_elapsed_ratio(snapshot_time: datetime | None) -> float:
    """估算 A股交易日内已完成交易时长占比。"""

    if snapshot_time is None:
        return 1.0
    minute_of_day = snapshot_time.hour * 60 + snapshot_time.minute
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60
    if minute_of_day <= morning_start:
        elapsed = 0
    elif minute_of_day <= morning_end:
        elapsed = minute_of_day - morning_start
    elif minute_of_day <= afternoon_start:
        elapsed = morning_end - morning_start
    elif minute_of_day <= afternoon_end:
        elapsed = (morning_end - morning_start) + (minute_of_day - afternoon_start)
    else:
        elapsed = (morning_end - morning_start) + (afternoon_end - afternoon_start)
    return _clamp(elapsed / 240, 0.05, 1.0)


def _mean_value(values: Sequence[Any]) -> float:
    """计算可转换数字的均值，空序列返回 0。"""

    numbers = [_float_value(value) for value in values]
    numbers = [value for value in numbers if math.isfinite(value)]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _stddev(values: Sequence[float]) -> float:
    """计算总体标准差，用于衡量 20 日波动。"""

    numbers = [float(value) for value in values if math.isfinite(float(value))]
    if len(numbers) < 2:
        return 0.0
    mean = sum(numbers) / len(numbers)
    variance = sum((value - mean) ** 2 for value in numbers) / len(numbers)
    return math.sqrt(variance)


def _max_drawdown_magnitude(values: Sequence[float]) -> float:
    """计算窗口最大回撤的正数幅度百分比。"""

    peak = 0.0
    max_drawdown = 0.0
    for raw_value in values:
        value = float(raw_value)
        if value <= 0:
            continue
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    return max_drawdown


def _snapshot_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """把行情快照转换为 JSON 可序列化字典。"""

    return {str(key): _snapshot_value(value) for key, value in values.items()}


def _snapshot_value(value: Any) -> Any:
    """把单个快照字段转换为 JSON 可序列化值。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _snapshot_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_snapshot_value(item) for item in value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _is_risk_name(name: str) -> bool:
    """判断股票名称是否包含高风险标识。"""
    upper = name.upper()
    return "ST" in upper or "退" in name


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """限制数值范围。"""
    return max(minimum, min(maximum, float(value)))


def _float_value(value: Any, default: float = 0.0) -> float:
    """把数据库/行情数字转换为 float。"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _datetime_to_str(value: datetime | None) -> str | None:
    """把 datetime 转为前端展示字符串。"""
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
