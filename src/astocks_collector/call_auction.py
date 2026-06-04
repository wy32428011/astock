"""集合竞价 9:15-9:25 分阶段采集与 LLM 快速选股模块。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .config import AppConfig
from .db import MySQLRepository
from .llm_client import LLMClient
from .market_data import AkshareMarketData

DEFAULT_CALL_AUCTION_START_TIME = "09:15"
DEFAULT_CALL_AUCTION_DECISION_START_TIME = "09:20"
DEFAULT_CALL_AUCTION_END_TIME = "09:25"
CALL_AUCTION_ACTION_BUY = "BUY_CANDIDATE"
CALL_AUCTION_ACTION_WATCH = "WATCH"


@dataclass(frozen=True)
class CallAuctionRunResult:
    """集合竞价选股单次运行结果。"""

    run_id: int | None
    trade_date: Any
    snapshot_time: datetime
    trigger_type: str
    status: str
    quote_count: int = 0
    valid_quote_count: int = 0
    candidate_count: int = 0
    pick_count: int = 0
    llm_required: bool = True
    llm_success: bool = False
    market_session: str = "closed"
    skipped: bool = False
    skip_reason: str = ""
    error_message: str = ""
    summary: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 和 CLI 统一使用的字典结构。"""

        return {
            "runId": self.run_id,
            "tradeDate": _date_text(self.trade_date),
            "snapshotTime": _datetime_text(self.snapshot_time),
            "triggerType": self.trigger_type,
            "status": self.status,
            "quoteCount": self.quote_count,
            "validQuoteCount": self.valid_quote_count,
            "candidateCount": self.candidate_count,
            "pickCount": self.pick_count,
            "llmRequired": self.llm_required,
            "llmSuccess": self.llm_success,
            "marketSession": self.market_session,
            "skipped": self.skipped,
            "skipReason": self.skip_reason,
            "errorMessage": self.error_message,
            "summary": dict(self.summary or {}),
        }


def parse_call_auction_time(value: str | None, default: str) -> tuple[int, int]:
    """解析集合竞价时间配置，要求使用 HH:MM 格式。"""

    text = (value or default).strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError("集合竞价时间必须使用 HH:MM 格式") from exc
    if hour not in range(24) or minute not in range(60):
        raise ValueError("集合竞价时间的小时或分钟超出范围")
    return hour, minute


def call_auction_market_status(
    now: datetime,
    timezone: str = "Asia/Shanghai",
    start_time: str | None = DEFAULT_CALL_AUCTION_START_TIME,
    decision_start_time: str | None = DEFAULT_CALL_AUCTION_DECISION_START_TIME,
    end_time: str | None = DEFAULT_CALL_AUCTION_END_TIME,
) -> dict[str, Any]:
    """判断当前是否处于集合竞价采集期或决策期。"""

    zone = ZoneInfo(timezone)
    local_now = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    start_hour, start_minute = parse_call_auction_time(
        start_time, DEFAULT_CALL_AUCTION_START_TIME
    )
    decision_hour, decision_minute = parse_call_auction_time(
        decision_start_time, DEFAULT_CALL_AUCTION_DECISION_START_TIME
    )
    end_hour, end_minute = parse_call_auction_time(end_time, DEFAULT_CALL_AUCTION_END_TIME)
    start_at = local_now.replace(
        hour=start_hour, minute=start_minute, second=0, microsecond=0
    )
    decision_at = local_now.replace(
        hour=decision_hour, minute=decision_minute, second=0, microsecond=0
    )
    end_at = local_now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    collect_window = (
        f"{start_hour:02d}:{start_minute:02d}-"
        f"{decision_hour:02d}:{decision_minute:02d}"
    )
    decision_window = (
        f"{decision_hour:02d}:{decision_minute:02d}-"
        f"{end_hour:02d}:{end_minute:02d}"
    )
    window_text = f"{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}"
    if start_at >= decision_at:
        raise ValueError("CALL_AUCTION_START_TIME 必须早于 CALL_AUCTION_DECISION_START_TIME")
    if decision_at >= end_at:
        raise ValueError("CALL_AUCTION_DECISION_START_TIME 必须早于 CALL_AUCTION_END_TIME")
    base_status = {
        "now": local_now,
        "marketOpen": False,
        "collectOpen": False,
        "decisionOpen": False,
        "session": "closed",
        "reason": "",
        "window": window_text,
        "collectWindow": collect_window,
        "decisionWindow": decision_window,
    }
    if local_now.weekday() >= 5:
        return {
            **base_status,
            "reason": f"非交易日，仅在工作日 {window_text} 执行集合竞价采集与选股",
        }
    if start_at <= local_now < decision_at:
        return {
            **base_status,
            "marketOpen": True,
            "collectOpen": True,
            "session": "pre_call_auction",
            "reason": "集合竞价采集期已开启，正在融合行情快照",
        }
    if decision_at <= local_now < end_at:
        return {
            **base_status,
            "marketOpen": True,
            "collectOpen": True,
            "decisionOpen": True,
            "session": "call_auction",
            "reason": "集合竞价决策期已开启，基于融合快照快速选股",
        }
    if local_now < start_at:
        reason = f"尚未到达集合竞价采集窗口 {collect_window}"
    else:
        reason = f"集合竞价决策窗口 {decision_window} 已结束"
    return {
        **base_status,
        "reason": reason,
    }


def build_call_auction_run_row(
    *,
    trade_date: Any,
    snapshot_time: datetime,
    trigger_type: str,
    status: str,
    quote_count: int = 0,
    valid_quote_count: int = 0,
    candidate_count: int = 0,
    pick_count: int = 0,
    llm_required: bool = True,
    llm_success: bool = False,
    market_session: str = "closed",
    skip_reason: str = "",
    error_message: str = "",
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造集合竞价运行记录行。"""

    return {
        "trade_date": trade_date,
        "snapshot_time": snapshot_time,
        "trigger_type": trigger_type,
        "status": status,
        "quote_count": quote_count,
        "valid_quote_count": valid_quote_count,
        "candidate_count": candidate_count,
        "pick_count": pick_count,
        "llm_required": llm_required,
        "llm_success": llm_success,
        "market_session": market_session,
        "skip_reason": skip_reason,
        "error_message": error_message,
        "summary_json": json.dumps(summary or {}, ensure_ascii=False),
    }


def build_call_auction_pick_row(
    *,
    run_id: int,
    trade_date: Any,
    snapshot_time: datetime,
    rank_no: int,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """构造集合竞价候选记录行。"""

    return {
        "run_id": run_id,
        "trade_date": trade_date,
        "snapshot_time": snapshot_time,
        "rank_no": rank_no,
        "symbol": candidate["symbol"],
        "name": candidate["name"],
        "latest_price": candidate["latestPrice"],
        "pct_change": candidate["pctChange"],
        "volume": candidate["volume"],
        "amount": candidate["amount"],
        "volume_ratio": candidate["volumeRatio"],
        "turnover_rate": candidate["turnoverRate"],
        "price_score": candidate["priceScore"],
        "volume_score": candidate["volumeScore"],
        "trend_score": candidate["trendScore"],
        "risk_score": candidate["riskScore"],
        "quant_score": candidate["quantScore"],
        "llm_score": candidate.get("llmScore"),
        "final_score": candidate["finalScore"],
        "signal_action": candidate["action"],
        "reason": candidate.get("reason") or "",
        "risk": candidate.get("risk") or "",
        "factor_snapshot": json.dumps(
            candidate.get("factorSnapshot") or {}, ensure_ascii=False
        ),
        "raw_response": json.dumps(candidate.get("rawResponse") or {}, ensure_ascii=False),
    }


class CallAuctionSelector:
    """集合竞价 9:20-9:25 快速选股器。"""

    def __init__(
        self,
        settings: AppConfig,
        repository: MySQLRepository | None = None,
        market_data: AkshareMarketData | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        """初始化配置、数据库仓储、行情源和 LLM 客户端。"""

        self.settings = settings
        self.repository = repository or MySQLRepository(settings)
        self.market_data = market_data or AkshareMarketData(settings.daily_provider)
        self.llm_client = llm_client or LLMClient(
            settings.llm_base_url,
            settings.llm_model,
            settings.llm_api_key,
            int(getattr(settings, "call_auction_llm_timeout_seconds", 15)),
        )

    def run_once(
        self,
        *,
        final_limit: int | None = None,
        force: bool = False,
        trigger_type: str = "manual",
        now: datetime | None = None,
    ) -> CallAuctionRunResult:
        """执行一次集合竞价快照选股。"""

        self.repository.ensure_schema()
        snapshot_time = now or datetime.now(
            ZoneInfo(getattr(self.settings, "timezone", "Asia/Shanghai"))
        )
        snapshot_time = snapshot_time.replace(microsecond=0)
        trade_date = snapshot_time.date()
        limit = int(final_limit or getattr(self.settings, "call_auction_final_limit", 5))
        llm_required = bool(getattr(self.settings, "call_auction_require_llm", 1))
        if not bool(getattr(self.settings, "call_auction_enabled", 1)):
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                llm_required=llm_required,
                skip_reason="集合竞价选股已关闭",
            )

        status = call_auction_market_status(
            snapshot_time,
            getattr(self.settings, "timezone", "Asia/Shanghai"),
            getattr(
                self.settings,
                "call_auction_start_time",
                DEFAULT_CALL_AUCTION_START_TIME,
            ),
            getattr(self.settings, "call_auction_end_time", DEFAULT_CALL_AUCTION_END_TIME),
        )
        if not force and not status["marketOpen"]:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                llm_required=llm_required,
                market_session=str(status["session"]),
                skip_reason=str(status["reason"]),
                summary={"marketStatus": _json_safe_status(status)},
            )

        try:
            quotes = self.market_data.fetch_realtime_quotes()
        except Exception as exc:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                llm_required=llm_required,
                market_session="call_auction",
                skip_reason=f"实时行情获取失败，已跳过集合竞价选股: {exc}",
                error_message=str(exc),
                summary={"marketStatus": _json_safe_status(status)},
            )
        valid_quotes = [item for item in quotes if _resolve_auction_price(item) > 0]
        if not valid_quotes:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=0,
                llm_required=llm_required,
                market_session="call_auction",
                skip_reason="实时行情没有有效价格，已跳过集合竞价选股",
                summary={"marketStatus": _json_safe_status(status)},
            )

        strategy_mode = "standard"
        candidates = self._build_candidates(
            valid_quotes,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
            strategy_mode=strategy_mode,
        )
        if not candidates:
            strategy_mode = "relaxed"
            candidates = self._build_candidates(
                valid_quotes,
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                strategy_mode=strategy_mode,
            )
        if not candidates:
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=0,
                llm_required=llm_required,
                market_session="call_auction",
                skip_reason="规则预筛没有符合条件的集合竞价候选",
                summary={
                    "marketStatus": _json_safe_status(status),
                    "strategyMode": strategy_mode,
                },
            )

        review_limit = max(1, int(getattr(self.settings, "call_auction_llm_review_limit", 20)))
        reviewed, llm_status = self._review_with_llm(candidates[:review_limit])
        if llm_required and not llm_status.get("llmSuccess"):
            return self._record_result(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SKIPPED",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=len(candidates),
                llm_required=llm_required,
                llm_success=False,
                market_session="call_auction",
                skip_reason=str(llm_status.get("skipReason") or "LLM 复核失败"),
                summary={
                    "marketStatus": _json_safe_status(status),
                    "llm": llm_status,
                    "strategyMode": strategy_mode,
                },
            )

        picks = [
            item
            for item in reviewed
            if str(item.get("action") or "").upper() == CALL_AUCTION_ACTION_BUY
        ][:limit]
        run_id = self.repository.insert_call_auction_run(
            build_call_auction_run_row(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status="SUCCESS",
                quote_count=len(quotes),
                valid_quote_count=len(valid_quotes),
                candidate_count=len(candidates),
                pick_count=len(picks),
                llm_required=llm_required,
                llm_success=bool(llm_status.get("llmSuccess")),
                market_session="call_auction",
                summary={
                    "marketStatus": _json_safe_status(status),
                    "llm": llm_status,
                    "strategyMode": strategy_mode,
                },
            )
        )
        pick_rows = [
            build_call_auction_pick_row(
                run_id=run_id,
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                rank_no=index + 1,
                candidate=pick,
            )
            for index, pick in enumerate(picks)
        ]
        self.repository.replace_call_auction_picks(run_id, pick_rows)
        return CallAuctionRunResult(
            run_id=run_id,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
            trigger_type=trigger_type,
            status="SUCCESS",
            quote_count=len(quotes),
            valid_quote_count=len(valid_quotes),
            candidate_count=len(candidates),
            pick_count=len(picks),
            llm_required=llm_required,
            llm_success=bool(llm_status.get("llmSuccess")),
            market_session="call_auction",
            summary={
                "marketStatus": _json_safe_status(status),
                "llm": llm_status,
                "strategyMode": strategy_mode,
            },
        )

    def market_status(self, now: datetime | None = None) -> dict[str, Any]:
        """返回当前集合竞价窗口状态，供 API 和前端展示。"""

        current = now or datetime.now(ZoneInfo(getattr(self.settings, "timezone", "Asia/Shanghai")))
        return _json_safe_status(
            call_auction_market_status(
                current,
                getattr(self.settings, "timezone", "Asia/Shanghai"),
                getattr(
                    self.settings,
                    "call_auction_start_time",
                    DEFAULT_CALL_AUCTION_START_TIME,
                ),
                getattr(
                    self.settings,
                    "call_auction_end_time",
                    DEFAULT_CALL_AUCTION_END_TIME,
                ),
            )
        )

    def _build_candidates(
        self,
        quotes: Sequence[Mapping[str, Any]],
        *,
        trade_date: Any,
        snapshot_time: datetime,
        strategy_mode: str = "standard",
    ) -> list[dict[str, Any]]:
        """按集合竞价实时行情构建量化预筛候选。"""

        min_pct = float(getattr(self.settings, "call_auction_min_pct_change", 0.0))
        max_pct = float(getattr(self.settings, "call_auction_max_pct_change", 6.8))
        min_volume_ratio = float(getattr(self.settings, "call_auction_min_volume_ratio", 0.5))
        min_amount = float(getattr(self.settings, "call_auction_min_amount", 3000000.0))
        min_score = float(getattr(self.settings, "call_auction_min_final_score", 55.0))
        relaxed_mode = strategy_mode == "relaxed"
        if relaxed_mode:
            min_pct = min(min_pct, -0.3)
            max_pct = max(max_pct, 7.5)
            min_volume_ratio = min(min_volume_ratio, 0.2)
            min_amount = min(min_amount, 1000000.0)
            min_score = min(min_score, 52.0)
        history = self.repository.recent_stock_daily_history(
            [str(item.get("symbol") or "") for item in quotes],
            trade_date,
            lookback_days=30,
        )
        candidates: list[dict[str, Any]] = []
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").strip()
            name = str(quote.get("name") or "").strip()
            if not symbol or _is_risk_name(name) or name.startswith(("N", "C")):
                continue
            latest_price = _resolve_auction_price(quote)
            pct_change = _resolve_pct_change(quote, latest_price)
            if latest_price <= 0 or pct_change < min_pct:
                continue
            overheat = pct_change > max_pct
            amount = _float_value(quote.get("amount"))
            history_rows = history.get(symbol, [])
            volume_ratio = _resolve_volume_ratio(quote, history_rows)
            volume = _float_value(quote.get("volume"))
            turnover_rate = _float_value(quote.get("turnover_rate"))
            amplitude = _float_value(quote.get("amplitude"))
            liquidity_missing = amount <= 0 and volume <= 0 and volume_ratio <= 0
            liquidity_weak = (
                not liquidity_missing
                and (
                    (min_amount > 0 and amount > 0 and amount < min_amount)
                    or (min_volume_ratio > 0 and volume_ratio > 0 and volume_ratio < min_volume_ratio)
                )
            )
            history_factors = _history_factors(history_rows, latest_price)
            score = _calculate_call_auction_score(
                pct_change=pct_change,
                amount=amount,
                volume_ratio=volume_ratio,
                turnover_rate=turnover_rate,
                amplitude=amplitude,
                history=history_factors,
                min_pct=min_pct,
                max_pct=max_pct,
                min_amount=min_amount,
                liquidity_missing=liquidity_missing,
                liquidity_weak=liquidity_weak,
                overheat=overheat,
            )
            if score["quantScore"] < min_score:
                continue
            factor_snapshot = _snapshot_mapping(quote)
            factor_snapshot.update(
                {
                    "auctionWindow": {
                        "start": getattr(
                            self.settings,
                            "call_auction_start_time",
                            DEFAULT_CALL_AUCTION_START_TIME,
                        ),
                        "end": getattr(
                            self.settings,
                            "call_auction_end_time",
                            DEFAULT_CALL_AUCTION_END_TIME,
                        ),
                    },
                    "history": history_factors,
                    "filters": {
                        "minPctChange": min_pct,
                        "maxPctChange": max_pct,
                        "minVolumeRatio": min_volume_ratio,
                        "minAmount": min_amount,
                        "minFinalScore": min_score,
                    },
                    "strategyMode": strategy_mode,
                    "resolvedVolumeRatio": round(volume_ratio, 4),
                    "liquidityMissing": liquidity_missing,
                    "liquidityWeak": liquidity_weak,
                    "overheat": overheat,
                }
            )
            candidates.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "latestPrice": latest_price,
                    "pctChange": pct_change,
                    "volume": volume,
                    "amount": amount,
                    "volumeRatio": volume_ratio,
                    "turnoverRate": turnover_rate,
                    "amplitude": amplitude,
                    **score,
                    "llmScore": None,
                    "action": CALL_AUCTION_ACTION_WATCH,
                    "reason": _build_rule_reason(strategy_mode),
                    "risk": _build_rule_risk(
                        liquidity_missing,
                        liquidity_weak,
                        strategy_mode,
                        overheat,
                    ),
                    "factorSnapshot": factor_snapshot,
                    "rawResponse": {},
                }
            )
        candidates.sort(key=lambda item: float(item.get("quantScore") or 0), reverse=True)
        return candidates[: int(getattr(self.settings, "call_auction_preselect_limit", 120))]

    def _review_with_llm(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """调用 LLM 复核集合竞价候选，并返回通过后的动作。"""

        if not candidates:
            return [], {"llmSuccess": False, "skipReason": "没有候选可供 LLM 复核"}
        try:
            payload = self.llm_client.chat_json(
                _build_llm_messages(candidates),
                max_tokens=1536,
            )
            decisions = _normalize_llm_payload(payload)
        except Exception as exc:
            return [], {
                "llmSuccess": False,
                "skipReason": f"LLM 复核失败: {exc}",
                "error": str(exc),
            }
        if not decisions:
            return [], {"llmSuccess": False, "skipReason": "LLM 未返回有效候选动作"}

        decisions_by_symbol = {item["symbol"]: item for item in decisions}
        reviewed: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            decision = decisions_by_symbol.get(str(item.get("symbol") or ""))
            if not decision:
                item["action"] = CALL_AUCTION_ACTION_WATCH
                item["rawResponse"] = {"missingDecision": True}
                reviewed.append(item)
                continue
            llm_score = _float_value(decision.get("llmScore"), 0.0)
            action = str(decision.get("action") or CALL_AUCTION_ACTION_WATCH).upper()
            item["action"] = action
            item["llmScore"] = round(llm_score, 4)
            item["finalScore"] = round(
                float(item.get("quantScore") or 0) * 0.7 + llm_score * 0.3,
                4,
            )
            if decision.get("reason"):
                item["reason"] = str(decision["reason"]).strip()
            if decision.get("risk"):
                item["risk"] = str(decision["risk"]).strip()
            item["rawResponse"] = decision
            reviewed.append(item)
        reviewed.sort(key=lambda row: float(row.get("finalScore") or 0), reverse=True)
        return reviewed, {
            "llmSuccess": True,
            "reviewCount": len(candidates),
            "decisionCount": len(decisions),
            "buyCount": sum(
                1
                for item in reviewed
                if str(item.get("action") or "").upper() == CALL_AUCTION_ACTION_BUY
            ),
        }

    def _record_result(
        self,
        *,
        trade_date: Any,
        snapshot_time: datetime,
        trigger_type: str,
        status: str,
        quote_count: int = 0,
        valid_quote_count: int = 0,
        candidate_count: int = 0,
        pick_count: int = 0,
        llm_required: bool = True,
        llm_success: bool = False,
        market_session: str = "closed",
        skip_reason: str = "",
        error_message: str = "",
        summary: Mapping[str, Any] | None = None,
    ) -> CallAuctionRunResult:
        """写入跳过或失败运行记录，并返回统一结果。"""

        run_id = self.repository.insert_call_auction_run(
            build_call_auction_run_row(
                trade_date=trade_date,
                snapshot_time=snapshot_time,
                trigger_type=trigger_type,
                status=status,
                quote_count=quote_count,
                valid_quote_count=valid_quote_count,
                candidate_count=candidate_count,
                pick_count=pick_count,
                llm_required=llm_required,
                llm_success=llm_success,
                market_session=market_session,
                skip_reason=skip_reason,
                error_message=error_message,
                summary=summary,
            )
        )
        return CallAuctionRunResult(
            run_id=run_id,
            trade_date=trade_date,
            snapshot_time=snapshot_time,
            trigger_type=trigger_type,
            status=status,
            quote_count=quote_count,
            valid_quote_count=valid_quote_count,
            candidate_count=candidate_count,
            pick_count=pick_count,
            llm_required=llm_required,
            llm_success=llm_success,
            market_session=market_session,
            skipped=status != "SUCCESS",
            skip_reason=skip_reason,
            error_message=error_message,
            summary=summary,
        )


def _build_llm_messages(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """构建集合竞价 LLM 复核提示词。"""

    payload = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "latestPrice": item.get("latestPrice"),
            "pctChange": item.get("pctChange"),
            "amount": item.get("amount"),
            "volumeRatio": item.get("volumeRatio"),
            "turnoverRate": item.get("turnoverRate"),
            "quantScore": item.get("quantScore"),
            "priceScore": item.get("priceScore"),
            "volumeScore": item.get("volumeScore"),
            "trendScore": item.get("trendScore"),
            "riskScore": item.get("riskScore"),
            "risk": item.get("risk"),
            "strategyMode": (item.get("factorSnapshot") or {}).get("strategyMode"),
            "overheat": (item.get("factorSnapshot") or {}).get("overheat"),
            "liquidityMissing": (item.get("factorSnapshot") or {}).get("liquidityMissing"),
            "liquidityWeak": (item.get("factorSnapshot") or {}).get("liquidityWeak"),
            "history": (item.get("factorSnapshot") or {}).get("history"),
        }
        for item in candidates
    ]
    system_prompt = (
        "你是A股集合竞价短线风控助手。只能基于输入数据做候选复核，"
        "不要输出真实下单指令。9:20-9:25申报不可撤单，必须保守但不能机械全否。"
    )
    user_prompt = (
        "请复核以下集合竞价候选，只返回JSON数组。每项字段必须包含"
        "symbol、action、llmScore、reason、risk。action 只能是 "
        f"{CALL_AUCTION_ACTION_BUY} 或 {CALL_AUCTION_ACTION_WATCH}。"
        "若候选中存在价格强度、历史趋势和量能风险相对更优的标的，"
        "请给出1到3只 BUY_CANDIDATE；只有明显过热、数据严重缺失或风险偏高时才全部 WATCH。"
        "llmScore 使用0到100分，BUY_CANDIDATE 通常不低于70分；reason 和 risk 各不超过24个汉字。\n"
        f"候选数据：{json.dumps(payload, ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_llm_payload(payload: Any) -> list[dict[str, Any]]:
    """校验并标准化 LLM 返回的集合竞价动作。"""

    if isinstance(payload, dict):
        rows = (
            payload.get("candidates")
            or payload.get("data")
            or payload.get("picks")
            or payload.get("results")
            or payload.get("items")
            or []
        )
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("LLM 响应必须是数组或包含候选数组的对象")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("LLM 候选项必须是对象")
        symbol = str(row.get("symbol") or "").strip()
        action = _normalize_llm_action(row.get("action") or row.get("decision"))
        if not symbol:
            raise ValueError("LLM 候选项缺少 symbol")
        if action not in {CALL_AUCTION_ACTION_BUY, CALL_AUCTION_ACTION_WATCH}:
            action = CALL_AUCTION_ACTION_WATCH
        llm_score_value = row.get("llmScore", row.get("score", row.get("confidence", 0.0)))
        normalized.append(
            {
                "symbol": symbol,
                "action": action,
                "llmScore": _clamp(_float_value(llm_score_value, 0.0), 0.0, 100.0),
                "reason": str(row.get("reason") or "").strip(),
                "risk": str(row.get("risk") or "").strip(),
            }
        )
    return normalized


def _normalize_llm_action(value: Any) -> str:
    """兼容模型常见动作词，统一为集合竞价允许动作。"""

    action = str(value or "").strip().upper()
    if action in {
        "BUY",
        "BUY_CANDIDATE",
        "CANDIDATE",
        "SELECT",
        "KEEP",
        "BOOST",
        "买入",
        "候选",
        "入选",
        "保留",
        "通过",
    }:
        return CALL_AUCTION_ACTION_BUY
    if action in {
        "WATCH",
        "HOLD",
        "SKIP",
        "AVOID",
        "DOWNRANK",
        "SELL",
        "观察",
        "关注",
        "跳过",
        "回避",
        "卖出",
        "",
    }:
        return CALL_AUCTION_ACTION_WATCH
    return action


def _calculate_call_auction_score(
    *,
    pct_change: float,
    amount: float,
    volume_ratio: float,
    turnover_rate: float,
    amplitude: float,
    history: Mapping[str, float],
    min_pct: float,
    max_pct: float,
    min_amount: float,
    liquidity_missing: bool = False,
    liquidity_weak: bool = False,
    overheat: bool = False,
) -> dict[str, float]:
    """计算集合竞价候选的价格、量能、趋势、风险和综合分。"""

    pct_span = max(max_pct - min_pct, 0.01)
    price_score = _clamp(55.0 + (pct_change - min_pct) / pct_span * 35.0, 0.0, 100.0)
    if pct_change > 4.8:
        price_score -= min(12.0, (pct_change - 4.8) * 6.0)
    if overheat:
        price_score -= min(25.0, max(0.0, pct_change - max_pct) * 1.6)
    if amount <= 0 and volume_ratio <= 0:
        amount_score = 48.0
        volume_score = 48.0
    else:
        amount_score = _clamp((amount / max(min_amount, 1.0)) * 18.0 + 50.0, 0.0, 100.0)
        volume_score = _clamp(amount_score * 0.55 + min(volume_ratio, 5.0) * 9.0, 0.0, 100.0)
    if turnover_rate > 0:
        volume_score = _clamp(volume_score + min(turnover_rate, 8.0) * 1.4, 0.0, 100.0)
    trend_score = _score_history_trend(history)
    risk_score = 82.0
    if pct_change > 4.5:
        risk_score -= (pct_change - 4.5) * 5.0
    if overheat:
        risk_score -= min(35.0, max(0.0, pct_change - max_pct) * 2.0 + 8.0)
    if amplitude > 0:
        risk_score -= max(0.0, amplitude - 4.0) * 4.0
    if history.get("volatility20", 0.0) > 4.5:
        risk_score -= (history["volatility20"] - 4.5) * 4.0
    if liquidity_missing:
        risk_score -= 10.0
    elif liquidity_weak:
        risk_score -= 5.0
    risk_score = _clamp(risk_score, 0.0, 100.0)
    quant_score = _clamp(
        price_score * 0.30 + volume_score * 0.25 + trend_score * 0.25 + risk_score * 0.20,
        0.0,
        100.0,
    )
    if liquidity_missing:
        quant_score -= 6.0
    elif liquidity_weak:
        quant_score -= 3.0
    if overheat:
        quant_score -= min(18.0, max(0.0, pct_change - max_pct) * 0.8 + 5.0)
    quant_score = _clamp(quant_score, 0.0, 100.0)
    return {
        "priceScore": round(price_score, 4),
        "volumeScore": round(volume_score, 4),
        "trendScore": round(trend_score, 4),
        "riskScore": round(risk_score, 4),
        "quantScore": round(quant_score, 4),
        "finalScore": round(quant_score, 4),
    }


def _score_history_trend(history: Mapping[str, float]) -> float:
    """按历史均线和近期涨幅计算趋势分。"""

    score = 55.0
    latest = history.get("latestClose", 0.0)
    if latest > 0:
        if history.get("ma5", 0.0) and latest >= history["ma5"]:
            score += 10.0
        if history.get("ma10", 0.0) and latest >= history["ma10"]:
            score += 10.0
        if history.get("ma20", 0.0) and latest >= history["ma20"]:
            score += 8.0
    score += _clamp(history.get("return5", 0.0), -8.0, 8.0) * 1.4
    score += _clamp(history.get("return20", 0.0), -12.0, 12.0) * 0.8
    return _clamp(score, 0.0, 100.0)


def _history_factors(rows: Sequence[Mapping[str, Any]], latest_price: float) -> dict[str, float]:
    """从最近日线计算集合竞价所需历史因子。"""

    closes = [_float_value(row.get("close_price")) for row in rows if _float_value(row.get("close_price")) > 0]
    volumes = [_float_value(row.get("volume")) for row in rows if _float_value(row.get("volume")) > 0]
    pct_changes = [_float_value(row.get("pct_change")) for row in rows]
    latest_close = closes[-1] if closes else latest_price
    return {
        "latestClose": latest_close,
        "ma5": _mean(closes[-5:]),
        "ma10": _mean(closes[-10:]),
        "ma20": _mean(closes[-20:]),
        "avgVolume20": _mean(volumes[-20:]),
        "return5": _pct_return(closes, 5),
        "return20": _pct_return(closes, 20),
        "volatility20": _stddev(pct_changes[-20:]),
    }


def _resolve_volume_ratio(
    quote: Mapping[str, Any], history_rows: Sequence[Mapping[str, Any]]
) -> float:
    """优先使用行情量比，缺失时用当前成交量和 20 日均量兜底估算。"""

    volume_ratio = _float_value(quote.get("volume_ratio"))
    if volume_ratio > 0:
        return volume_ratio
    volume = _float_value(quote.get("volume"))
    avg_volume = _mean(
        [_float_value(row.get("volume")) for row in history_rows[-20:] if _float_value(row.get("volume")) > 0]
    )
    if volume <= 0 or avg_volume <= 0:
        return 0.0
    return round((volume / avg_volume) * 240.0, 4)


def _resolve_auction_price(quote: Mapping[str, Any]) -> float:
    """优先取最新价，集合竞价阶段缺失时用今开、买价或卖价兜底。"""

    for key in ("latest_price", "open_price", "bid_price", "ask_price", "buy_price", "sell_price"):
        value = _float_value(quote.get(key))
        if value > 0:
            return value
    return 0.0


def _resolve_pct_change(quote: Mapping[str, Any], latest_price: float) -> float:
    """优先取行情涨跌幅，缺失时用昨收和参考价计算。"""

    pct_change = _float_value(quote.get("pct_change"))
    if pct_change != 0:
        return pct_change
    pre_close = _float_value(quote.get("pre_close"))
    if latest_price > 0 and pre_close > 0:
        return (latest_price / pre_close - 1.0) * 100.0
    return pct_change


def _build_rule_reason(strategy_mode: str) -> str:
    """按预筛模式生成候选进入 LLM 复核前的规则原因。"""

    if strategy_mode == "relaxed":
        return "标准预筛未命中，已启用二次宽松预筛，等待 LLM 复核确认"
    return "集合竞价规则预筛达标，价格强度进入候选池，等待 LLM 复核确认"


def _build_rule_risk(
    liquidity_missing: bool,
    liquidity_weak: bool,
    strategy_mode: str,
    overheat: bool = False,
) -> str:
    """按规则预筛状态生成风险提示。"""

    prefix_parts: list[str] = []
    if strategy_mode == "relaxed":
        prefix_parts.append("二次宽松预筛候选，需降低仓位并重点核验")
    if overheat:
        prefix_parts.append("涨幅超过标准上限，追高风险已大幅扣分")
    prefix = "；".join(prefix_parts)
    if prefix:
        prefix = f"{prefix}；"
    if liquidity_missing:
        return f"{prefix}竞价量能字段缺失，LLM 需重点复核；9:20-9:25 申报后不可撤单"
    if liquidity_weak:
        return f"{prefix}竞价量能低于默认强度，只作为备选候选；9:20-9:25 申报后不可撤单"
    return f"{prefix}9:20-9:25 接受申报但不可撤单，候选不代表一定成交"


def _json_safe_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """把窗口状态转换为 JSON 友好结构。"""

    return {
        key: _datetime_text(value) if isinstance(value, datetime) else value
        for key, value in status.items()
    }


def _snapshot_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """转换行情快照中的 Decimal 和日期对象，便于写入 JSON。"""

    return {key: _snapshot_value(value) for key, value in values.items()}


def _snapshot_value(value: Any) -> Any:
    """转换单个快照字段为 JSON 友好值。"""

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return _datetime_text(value)
    if isinstance(value, Mapping):
        return {key: _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    return value


def _is_risk_name(name: str) -> bool:
    """判断股票名称是否包含 ST、退市等风险标识。"""

    normalized = name.upper()
    return "ST" in normalized or "退" in name


def _float_value(value: Any, default: float = 0.0) -> float:
    """安全转换数值字段为 float。"""

    if value is None:
        return default
    try:
        if isinstance(value, str) and value.strip() in {"", "-", "--"}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: Sequence[float]) -> float:
    """计算均值，空序列返回 0。"""

    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _stddev(values: Sequence[float]) -> float:
    """计算简单标准差。"""

    numbers = [float(value) for value in values if value is not None]
    if len(numbers) < 2:
        return 0.0
    mean = _mean(numbers)
    variance = sum((value - mean) ** 2 for value in numbers) / len(numbers)
    return variance ** 0.5


def _pct_return(values: Sequence[float], periods: int) -> float:
    """计算最近 N 期收益率百分比。"""

    if len(values) <= periods or values[-periods - 1] == 0:
        return 0.0
    return (values[-1] / values[-periods - 1] - 1.0) * 100.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """把数值限制在指定区间内。"""

    return max(minimum, min(maximum, value))


def _date_text(value: Any) -> str | None:
    """把日期值转换为字符串。"""

    return value.isoformat() if hasattr(value, "isoformat") else str(value) if value else None


def _datetime_text(value: Any) -> str | None:
    """把时间值转换为字符串。"""

    return value.isoformat() if hasattr(value, "isoformat") else str(value) if value else None
