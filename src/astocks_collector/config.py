"""采集器配置读取与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """应用运行配置，统一管理数据库、采集和调度参数。"""

    mysql_host: str = "192.168.50.19"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "astocks_collector"
    mysql_charset: str = "utf8mb4"
    adjust_type: str = "qfq"
    daily_provider: str = "sina"
    default_start_date: str = "19900101"
    daily_window_days: int = 10
    request_interval_seconds: float = 0.35
    max_retries: int = 3
    max_workers: int = 2
    batch_size: int = 500
    schedule_time: str = "17:30"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    llm_base_url: str = "http://127.0.0.1:8317/v1"
    llm_model: str = "gpt-5.5"
    llm_api_key: str = "your-api-key-1"
    llm_timeout_seconds: int = 120
    analysis_lookback_days: int = 90
    analysis_preselect_limit: int = 80
    analysis_final_limit: int = 10
    t1_llm_review_pool_multiplier: int = 2
    t1_llm_score_weight: float = 0.20
    t1_llm_max_adjustment: float = 0.8
    t1_llm_rank_penalty: float = 0.02
    t1_llm_avoid_penalty: float = 2.0
    t1_llm_protected_top_n: int = 0
    t1_llm_entry_limit_multiplier: int = 2
    t1_llm_adaptive_gate_enabled: int = 1
    t1_llm_adaptive_candidate_count_max: int = 5000
    t1_llm_adaptive_top_pct_max: float = 5.0
    t1_llm_adaptive_reject_action: str = "skip"
    t1_llm_gate_top_n: int = 10
    t1_llm_final_pick_limit: int = 3
    t1_llm_candidate_pct_change_max: float = 3.5
    t1_llm_require_review: int = 1
    t1_llm_allowed_actions: str = "KEEP"
    t1_quality_enabled: int = 1
    t1_quality_schedule_time: str = "14:05"
    t1_quality_execute_trades: int = 1
    t1_quality_preselect_limit: int = 120
    t1_quality_final_limit: int = 2
    t1_quality_min_quote_count: int = 1000
    t1_quality_min_valid_quote_count: int = 300
    t1_quality_min_final_score: float = 66.0
    t1_quality_min_risk_score: float = 55.0
    t1_quality_max_pct_change: float = 5.8
    t1_quality_max_amplitude: float = 8.0
    t1_quality_min_volume_ratio: float = 1.2
    t1_quality_require_uptrend: int = 1
    t1_quality_history_lookback_days: int = 30
    t1_quality_max_volatility_20: float = 4.5
    t1_quality_max_drawdown_20: float = 12.0
    t1_quality_gate_enabled: int = 1
    t1_quality_gate_top_n: int = 10
    t1_quality_candidate_count_min: int = 20
    t1_quality_candidate_count_max: int = 300
    t1_quality_top_pct_avg_max: float = 3.5
    t1_quality_market_pct_avg_min: float = 0.5
    t1_quality_market_pct_avg_max: float = 2.0
    t1_quality_llm_review_limit: int = 20
    t1_quality_use_llm: int = 1
    t1_quality_require_llm: int = 1
    t1_quality_llm_max_tokens: int = 4096
    t1_quality_autostart_api_scheduler: int = 1
    call_auction_enabled: int = 1
    call_auction_start_time: str = "09:15"
    call_auction_decision_start_time: str = "09:20"
    call_auction_end_time: str = "09:25"
    call_auction_auto_interval_seconds: int = 10
    call_auction_preselect_limit: int = 120
    call_auction_llm_review_limit: int = 20
    call_auction_final_limit: int = 5
    call_auction_require_llm: int = 1
    call_auction_llm_timeout_seconds: int = 30
    call_auction_min_pct_change: float = 0.0
    call_auction_max_pct_change: float = 6.8
    call_auction_min_volume_ratio: float = 0.5
    call_auction_min_amount: float = 3000000.0
    call_auction_min_final_score: float = 55.0
    three_day_analysis_lookback_days: int = 120
    three_day_preselect_limit: int = 120
    three_day_final_limit: int = 20
    realtime_quote_limit: int = 300
    realtime_signal_limit: int = 80
    realtime_decision_mode: str = "llm_review"
    realtime_auto_interval_seconds: int = 60
    realtime_llm_candidate_limit: int = 12
    simulation_initial_cash: float = 1000000.0
    simulation_order_cash_pct: float = 0.12
    simulation_max_positions: int = 8
    simulation_fee_rate: float = 0.0003

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "AppConfig":
        """从 .env 文件和环境变量构建配置。"""

        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)
        else:
            load_dotenv(override=False)

        config = cls(
            mysql_host=_read_str("MYSQL_HOST", cls.mysql_host),
            mysql_port=_read_int("MYSQL_PORT", cls.mysql_port),
            mysql_user=_read_str("MYSQL_USER", cls.mysql_user),
            mysql_password=_read_str("MYSQL_PASSWORD", cls.mysql_password),
            mysql_database=_read_str("MYSQL_DATABASE", cls.mysql_database),
            mysql_charset=_read_str("MYSQL_CHARSET", cls.mysql_charset),
            adjust_type=_read_str("ASTOCKS_ADJUST", cls.adjust_type),
            daily_provider=_read_str("ASTOCKS_DAILY_PROVIDER", cls.daily_provider),
            default_start_date=_read_str(
                "ASTOCKS_DEFAULT_START_DATE", cls.default_start_date
            ),
            daily_window_days=_read_int(
                "ASTOCKS_DAILY_WINDOW_DAYS", cls.daily_window_days
            ),
            request_interval_seconds=_read_float(
                "ASTOCKS_REQUEST_INTERVAL_SECONDS", cls.request_interval_seconds
            ),
            max_retries=_read_int("ASTOCKS_MAX_RETRIES", cls.max_retries),
            max_workers=_read_int("ASTOCKS_MAX_WORKERS", cls.max_workers),
            batch_size=_read_int("ASTOCKS_BATCH_SIZE", cls.batch_size),
            schedule_time=_read_str("ASTOCKS_SCHEDULE_TIME", cls.schedule_time),
            timezone=_read_str("ASTOCKS_TIMEZONE", cls.timezone),
            log_level=_read_str("LOG_LEVEL", cls.log_level),
            llm_base_url=_read_str("LLM_BASE_URL", cls.llm_base_url),
            llm_model=_read_str("LLM_MODEL", cls.llm_model),
            llm_api_key=_read_str("LLM_API_KEY", cls.llm_api_key),
            llm_timeout_seconds=_read_int(
                "LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds
            ),
            analysis_lookback_days=_read_int(
                "ANALYSIS_LOOKBACK_DAYS", cls.analysis_lookback_days
            ),
            analysis_preselect_limit=_read_int(
                "ANALYSIS_PRESELECT_LIMIT", cls.analysis_preselect_limit
            ),
            analysis_final_limit=_read_int(
                "ANALYSIS_FINAL_LIMIT", cls.analysis_final_limit
            ),
            t1_llm_review_pool_multiplier=_read_int(
                "T1_LLM_REVIEW_POOL_MULTIPLIER",
                cls.t1_llm_review_pool_multiplier,
            ),
            t1_llm_score_weight=_read_float(
                "T1_LLM_SCORE_WEIGHT", cls.t1_llm_score_weight
            ),
            t1_llm_max_adjustment=_read_float(
                "T1_LLM_MAX_ADJUSTMENT", cls.t1_llm_max_adjustment
            ),
            t1_llm_rank_penalty=_read_float(
                "T1_LLM_RANK_PENALTY", cls.t1_llm_rank_penalty
            ),
            t1_llm_avoid_penalty=_read_float(
                "T1_LLM_AVOID_PENALTY", cls.t1_llm_avoid_penalty
            ),
            t1_llm_protected_top_n=_read_int(
                "T1_LLM_PROTECTED_TOP_N", cls.t1_llm_protected_top_n
            ),
            t1_llm_entry_limit_multiplier=_read_int(
                "T1_LLM_ENTRY_LIMIT_MULTIPLIER",
                cls.t1_llm_entry_limit_multiplier,
            ),
            t1_llm_adaptive_gate_enabled=_read_int(
                "T1_LLM_ADAPTIVE_GATE_ENABLED",
                cls.t1_llm_adaptive_gate_enabled,
            ),
            t1_llm_adaptive_candidate_count_max=_read_int(
                "T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX",
                cls.t1_llm_adaptive_candidate_count_max,
            ),
            t1_llm_adaptive_top_pct_max=_read_float(
                "T1_LLM_ADAPTIVE_TOP_PCT_MAX",
                cls.t1_llm_adaptive_top_pct_max,
            ),
            t1_llm_adaptive_reject_action=_read_str(
                "T1_LLM_ADAPTIVE_REJECT_ACTION",
                cls.t1_llm_adaptive_reject_action,
            ),
            t1_llm_gate_top_n=_read_int(
                "T1_LLM_GATE_TOP_N",
                cls.t1_llm_gate_top_n,
            ),
            t1_llm_final_pick_limit=_read_int(
                "T1_LLM_FINAL_PICK_LIMIT",
                cls.t1_llm_final_pick_limit,
            ),
            t1_llm_candidate_pct_change_max=_read_float(
                "T1_LLM_CANDIDATE_PCT_CHANGE_MAX",
                cls.t1_llm_candidate_pct_change_max,
            ),
            t1_llm_require_review=_read_int(
                "T1_LLM_REQUIRE_REVIEW",
                cls.t1_llm_require_review,
            ),
            t1_llm_allowed_actions=_read_str(
                "T1_LLM_ALLOWED_ACTIONS",
                cls.t1_llm_allowed_actions,
            ),
            t1_quality_enabled=_read_int(
                "T1_QUALITY_ENABLED", cls.t1_quality_enabled
            ),
            t1_quality_schedule_time=_read_str(
                "T1_QUALITY_SCHEDULE_TIME", cls.t1_quality_schedule_time
            ),
            t1_quality_execute_trades=_read_int(
                "T1_QUALITY_EXECUTE_TRADES", cls.t1_quality_execute_trades
            ),
            t1_quality_preselect_limit=_read_int(
                "T1_QUALITY_PRESELECT_LIMIT", cls.t1_quality_preselect_limit
            ),
            t1_quality_final_limit=_read_int(
                "T1_QUALITY_FINAL_LIMIT", cls.t1_quality_final_limit
            ),
            t1_quality_min_quote_count=_read_int(
                "T1_QUALITY_MIN_QUOTE_COUNT", cls.t1_quality_min_quote_count
            ),
            t1_quality_min_valid_quote_count=_read_int(
                "T1_QUALITY_MIN_VALID_QUOTE_COUNT", cls.t1_quality_min_valid_quote_count
            ),
            t1_quality_min_final_score=_read_float(
                "T1_QUALITY_MIN_FINAL_SCORE", cls.t1_quality_min_final_score
            ),
            t1_quality_min_risk_score=_read_float(
                "T1_QUALITY_MIN_RISK_SCORE", cls.t1_quality_min_risk_score
            ),
            t1_quality_max_pct_change=_read_float(
                "T1_QUALITY_MAX_PCT_CHANGE", cls.t1_quality_max_pct_change
            ),
            t1_quality_max_amplitude=_read_float(
                "T1_QUALITY_MAX_AMPLITUDE", cls.t1_quality_max_amplitude
            ),
            t1_quality_min_volume_ratio=_read_float(
                "T1_QUALITY_MIN_VOLUME_RATIO", cls.t1_quality_min_volume_ratio
            ),
            t1_quality_require_uptrend=_read_int(
                "T1_QUALITY_REQUIRE_UPTREND", cls.t1_quality_require_uptrend
            ),
            t1_quality_history_lookback_days=_read_int(
                "T1_QUALITY_HISTORY_LOOKBACK_DAYS",
                cls.t1_quality_history_lookback_days,
            ),
            t1_quality_max_volatility_20=_read_float(
                "T1_QUALITY_MAX_VOLATILITY_20",
                cls.t1_quality_max_volatility_20,
            ),
            t1_quality_max_drawdown_20=_read_float(
                "T1_QUALITY_MAX_DRAWDOWN_20",
                cls.t1_quality_max_drawdown_20,
            ),
            t1_quality_gate_enabled=_read_int(
                "T1_QUALITY_GATE_ENABLED", cls.t1_quality_gate_enabled
            ),
            t1_quality_gate_top_n=_read_int(
                "T1_QUALITY_GATE_TOP_N", cls.t1_quality_gate_top_n
            ),
            t1_quality_candidate_count_min=_read_int(
                "T1_QUALITY_CANDIDATE_COUNT_MIN",
                cls.t1_quality_candidate_count_min,
            ),
            t1_quality_candidate_count_max=_read_int(
                "T1_QUALITY_CANDIDATE_COUNT_MAX",
                cls.t1_quality_candidate_count_max,
            ),
            t1_quality_top_pct_avg_max=_read_float(
                "T1_QUALITY_TOP_PCT_AVG_MAX",
                cls.t1_quality_top_pct_avg_max,
            ),
            t1_quality_market_pct_avg_min=_read_float(
                "T1_QUALITY_MARKET_PCT_AVG_MIN",
                cls.t1_quality_market_pct_avg_min,
            ),
            t1_quality_market_pct_avg_max=_read_float(
                "T1_QUALITY_MARKET_PCT_AVG_MAX",
                cls.t1_quality_market_pct_avg_max,
            ),
            t1_quality_llm_review_limit=_read_int(
                "T1_QUALITY_LLM_REVIEW_LIMIT",
                cls.t1_quality_llm_review_limit,
            ),
            t1_quality_use_llm=_read_int(
                "T1_QUALITY_USE_LLM", cls.t1_quality_use_llm
            ),
            t1_quality_require_llm=_read_int(
                "T1_QUALITY_REQUIRE_LLM", cls.t1_quality_require_llm
            ),
            t1_quality_llm_max_tokens=_read_int(
                "T1_QUALITY_LLM_MAX_TOKENS", cls.t1_quality_llm_max_tokens
            ),
            t1_quality_autostart_api_scheduler=_read_int(
                "T1_QUALITY_AUTOSTART_API_SCHEDULER",
                cls.t1_quality_autostart_api_scheduler,
            ),
            call_auction_enabled=_read_int(
                "CALL_AUCTION_ENABLED", cls.call_auction_enabled
            ),
            call_auction_start_time=_read_str(
                "CALL_AUCTION_START_TIME", cls.call_auction_start_time
            ),
            call_auction_decision_start_time=_read_str(
                "CALL_AUCTION_DECISION_START_TIME",
                cls.call_auction_decision_start_time,
            ),
            call_auction_end_time=_read_str(
                "CALL_AUCTION_END_TIME", cls.call_auction_end_time
            ),
            call_auction_auto_interval_seconds=_read_int(
                "CALL_AUCTION_AUTO_INTERVAL_SECONDS",
                cls.call_auction_auto_interval_seconds,
            ),
            call_auction_preselect_limit=_read_int(
                "CALL_AUCTION_PRESELECT_LIMIT", cls.call_auction_preselect_limit
            ),
            call_auction_llm_review_limit=_read_int(
                "CALL_AUCTION_LLM_REVIEW_LIMIT", cls.call_auction_llm_review_limit
            ),
            call_auction_final_limit=_read_int(
                "CALL_AUCTION_FINAL_LIMIT", cls.call_auction_final_limit
            ),
            call_auction_require_llm=_read_int(
                "CALL_AUCTION_REQUIRE_LLM", cls.call_auction_require_llm
            ),
            call_auction_llm_timeout_seconds=_read_int(
                "CALL_AUCTION_LLM_TIMEOUT_SECONDS",
                cls.call_auction_llm_timeout_seconds,
            ),
            call_auction_min_pct_change=_read_float(
                "CALL_AUCTION_MIN_PCT_CHANGE", cls.call_auction_min_pct_change
            ),
            call_auction_max_pct_change=_read_float(
                "CALL_AUCTION_MAX_PCT_CHANGE", cls.call_auction_max_pct_change
            ),
            call_auction_min_volume_ratio=_read_float(
                "CALL_AUCTION_MIN_VOLUME_RATIO", cls.call_auction_min_volume_ratio
            ),
            call_auction_min_amount=_read_float(
                "CALL_AUCTION_MIN_AMOUNT", cls.call_auction_min_amount
            ),
            call_auction_min_final_score=_read_float(
                "CALL_AUCTION_MIN_FINAL_SCORE", cls.call_auction_min_final_score
            ),
            three_day_analysis_lookback_days=_read_int(
                "THREE_DAY_ANALYSIS_LOOKBACK_DAYS",
                cls.three_day_analysis_lookback_days,
            ),
            three_day_preselect_limit=_read_int(
                "THREE_DAY_PRESELECT_LIMIT", cls.three_day_preselect_limit
            ),
            three_day_final_limit=_read_int(
                "THREE_DAY_FINAL_LIMIT", cls.three_day_final_limit
            ),
            realtime_quote_limit=_read_int(
                "REALTIME_QUOTE_LIMIT", cls.realtime_quote_limit
            ),
            realtime_signal_limit=_read_int(
                "REALTIME_SIGNAL_LIMIT", cls.realtime_signal_limit
            ),
            realtime_decision_mode=_read_str(
                "REALTIME_DECISION_MODE", cls.realtime_decision_mode
            ),
            realtime_auto_interval_seconds=_read_int(
                "REALTIME_AUTO_INTERVAL_SECONDS",
                cls.realtime_auto_interval_seconds,
            ),
            realtime_llm_candidate_limit=_read_int(
                "REALTIME_LLM_CANDIDATE_LIMIT",
                cls.realtime_llm_candidate_limit,
            ),
            simulation_initial_cash=_read_float(
                "SIMULATION_INITIAL_CASH", cls.simulation_initial_cash
            ),
            simulation_order_cash_pct=_read_float(
                "SIMULATION_ORDER_CASH_PCT", cls.simulation_order_cash_pct
            ),
            simulation_max_positions=_read_int(
                "SIMULATION_MAX_POSITIONS", cls.simulation_max_positions
            ),
            simulation_fee_rate=_read_float(
                "SIMULATION_FEE_RATE", cls.simulation_fee_rate
            ),
        )
        config.validate()
        return config

    def with_overrides(self, **kwargs: Any) -> "AppConfig":
        """返回带命令行覆盖项的新配置。"""

        updated = replace(self, **{k: v for k, v in kwargs.items() if v is not None})
        updated.validate()
        return updated

    def validate(self) -> None:
        """校验关键配置，尽早暴露错误。"""

        if self.adjust_type not in {"", "qfq", "hfq"}:
            raise ValueError("ASTOCKS_ADJUST 只能是空字符串、qfq 或 hfq")
        if self.daily_provider not in {"auto", "eastmoney", "sina"}:
            raise ValueError("ASTOCKS_DAILY_PROVIDER 只能是 auto、eastmoney 或 sina")
        if self.mysql_port <= 0:
            raise ValueError("MYSQL_PORT 必须是正整数")
        if self.daily_window_days <= 0:
            raise ValueError("ASTOCKS_DAILY_WINDOW_DAYS 必须是正整数")
        if self.max_retries <= 0:
            raise ValueError("ASTOCKS_MAX_RETRIES 必须是正整数")
        if self.max_workers <= 0:
            raise ValueError("ASTOCKS_MAX_WORKERS 必须是正整数")
        if self.batch_size <= 0:
            raise ValueError("ASTOCKS_BATCH_SIZE 必须是正整数")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS 必须是正整数")
        if self.analysis_lookback_days < 30:
            raise ValueError("ANALYSIS_LOOKBACK_DAYS 至少需要 30")
        if self.analysis_preselect_limit <= 0:
            raise ValueError("ANALYSIS_PRESELECT_LIMIT 必须是正整数")
        if self.analysis_final_limit <= 0:
            raise ValueError("ANALYSIS_FINAL_LIMIT 必须是正整数")
        if self.t1_llm_review_pool_multiplier <= 0:
            raise ValueError("T1_LLM_REVIEW_POOL_MULTIPLIER 必须是正整数")
        if not 0 <= self.t1_llm_score_weight <= 1:
            raise ValueError("T1_LLM_SCORE_WEIGHT 必须在 0 到 1 之间")
        if self.t1_llm_max_adjustment < 0:
            raise ValueError("T1_LLM_MAX_ADJUSTMENT 不能小于 0")
        if self.t1_llm_rank_penalty < 0:
            raise ValueError("T1_LLM_RANK_PENALTY 不能小于 0")
        if self.t1_llm_avoid_penalty < 0:
            raise ValueError("T1_LLM_AVOID_PENALTY 不能小于 0")
        if self.t1_llm_protected_top_n < 0:
            raise ValueError("T1_LLM_PROTECTED_TOP_N 不能小于 0")
        if self.t1_llm_entry_limit_multiplier <= 0:
            raise ValueError("T1_LLM_ENTRY_LIMIT_MULTIPLIER 必须是正整数")
        if self.t1_llm_adaptive_gate_enabled not in {0, 1}:
            raise ValueError("T1_LLM_ADAPTIVE_GATE_ENABLED 只能是 0 或 1")
        if self.t1_llm_adaptive_candidate_count_max <= 0:
            raise ValueError("T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX 必须是正整数")
        if self.t1_llm_adaptive_top_pct_max <= 0:
            raise ValueError("T1_LLM_ADAPTIVE_TOP_PCT_MAX 必须大于 0")
        if self.t1_llm_adaptive_reject_action not in {"quant", "skip"}:
            raise ValueError("T1_LLM_ADAPTIVE_REJECT_ACTION 只能是 quant 或 skip")
        if self.t1_llm_gate_top_n <= 0:
            raise ValueError("T1_LLM_GATE_TOP_N 必须是正整数")
        if self.t1_llm_final_pick_limit < 0:
            raise ValueError("T1_LLM_FINAL_PICK_LIMIT 不能小于 0")
        if self.t1_llm_candidate_pct_change_max < 0:
            raise ValueError("T1_LLM_CANDIDATE_PCT_CHANGE_MAX 不能小于 0")
        if self.t1_llm_require_review not in {0, 1}:
            raise ValueError("T1_LLM_REQUIRE_REVIEW 只能是 0 或 1")
        allowed_actions = self.t1_llm_allowed_action_set()
        if not allowed_actions:
            raise ValueError("T1_LLM_ALLOWED_ACTIONS 至少需要包含一个动作")
        if not allowed_actions <= {"BOOST", "KEEP", "DOWNRANK", "AVOID"}:
            raise ValueError("T1_LLM_ALLOWED_ACTIONS 只能包含 BOOST、KEEP、DOWNRANK、AVOID")
        if self.t1_quality_enabled not in {0, 1}:
            raise ValueError("T1_QUALITY_ENABLED 只能是 0 或 1")
        if self.t1_quality_execute_trades not in {0, 1}:
            raise ValueError("T1_QUALITY_EXECUTE_TRADES 只能是 0 或 1")
        if self.t1_quality_preselect_limit <= 0:
            raise ValueError("T1_QUALITY_PRESELECT_LIMIT 必须是正整数")
        if self.t1_quality_final_limit <= 0:
            raise ValueError("T1_QUALITY_FINAL_LIMIT 必须是正整数")
        if self.t1_quality_min_quote_count <= 0:
            raise ValueError("T1_QUALITY_MIN_QUOTE_COUNT 必须是正整数")
        if self.t1_quality_min_valid_quote_count <= 0:
            raise ValueError("T1_QUALITY_MIN_VALID_QUOTE_COUNT 必须是正整数")
        if self.t1_quality_min_final_score < 0:
            raise ValueError("T1_QUALITY_MIN_FINAL_SCORE 不能小于 0")
        if self.t1_quality_min_risk_score < 0:
            raise ValueError("T1_QUALITY_MIN_RISK_SCORE 不能小于 0")
        if self.t1_quality_max_pct_change < 0:
            raise ValueError("T1_QUALITY_MAX_PCT_CHANGE 不能小于 0")
        if self.t1_quality_max_amplitude < 0:
            raise ValueError("T1_QUALITY_MAX_AMPLITUDE 不能小于 0")
        if self.t1_quality_min_volume_ratio < 0:
            raise ValueError("T1_QUALITY_MIN_VOLUME_RATIO 不能小于 0")
        if self.t1_quality_require_uptrend not in {0, 1}:
            raise ValueError("T1_QUALITY_REQUIRE_UPTREND 只能是 0 或 1")
        if self.t1_quality_history_lookback_days < 20:
            raise ValueError("T1_QUALITY_HISTORY_LOOKBACK_DAYS 至少需要 20")
        if self.t1_quality_max_volatility_20 < 0:
            raise ValueError("T1_QUALITY_MAX_VOLATILITY_20 不能小于 0")
        if self.t1_quality_max_drawdown_20 < 0:
            raise ValueError("T1_QUALITY_MAX_DRAWDOWN_20 不能小于 0")
        if self.t1_quality_gate_enabled not in {0, 1}:
            raise ValueError("T1_QUALITY_GATE_ENABLED 只能是 0 或 1")
        if self.t1_quality_gate_top_n <= 0:
            raise ValueError("T1_QUALITY_GATE_TOP_N 必须是正整数")
        if self.t1_quality_candidate_count_min < 0:
            raise ValueError("T1_QUALITY_CANDIDATE_COUNT_MIN 不能小于 0")
        if self.t1_quality_candidate_count_max < 0:
            raise ValueError("T1_QUALITY_CANDIDATE_COUNT_MAX 不能小于 0")
        if (
            self.t1_quality_candidate_count_max > 0
            and self.t1_quality_candidate_count_min > self.t1_quality_candidate_count_max
        ):
            raise ValueError("T1_QUALITY_CANDIDATE_COUNT_MIN 不能大于最大值")
        if self.t1_quality_top_pct_avg_max < 0:
            raise ValueError("T1_QUALITY_TOP_PCT_AVG_MAX 不能小于 0")
        if self.t1_quality_market_pct_avg_min > self.t1_quality_market_pct_avg_max:
            raise ValueError("T1_QUALITY_MARKET_PCT_AVG_MIN 不能大于最大值")
        if self.t1_quality_llm_review_limit <= 0:
            raise ValueError("T1_QUALITY_LLM_REVIEW_LIMIT 必须是正整数")
        if self.t1_quality_use_llm not in {0, 1}:
            raise ValueError("T1_QUALITY_USE_LLM 只能是 0 或 1")
        if self.t1_quality_require_llm not in {0, 1}:
            raise ValueError("T1_QUALITY_REQUIRE_LLM 只能是 0 或 1")
        if self.t1_quality_llm_max_tokens <= 0:
            raise ValueError("T1_QUALITY_LLM_MAX_TOKENS 必须是正整数")
        if self.t1_quality_autostart_api_scheduler not in {0, 1}:
            raise ValueError("T1_QUALITY_AUTOSTART_API_SCHEDULER 只能是 0 或 1")
        if self.call_auction_enabled not in {0, 1}:
            raise ValueError("CALL_AUCTION_ENABLED 只能是 0 或 1")
        start_time = _parse_clock_time(
            self.call_auction_start_time, "CALL_AUCTION_START_TIME"
        )
        decision_start_time = _parse_clock_time(
            self.call_auction_decision_start_time,
            "CALL_AUCTION_DECISION_START_TIME",
        )
        end_time = _parse_clock_time(self.call_auction_end_time, "CALL_AUCTION_END_TIME")
        if start_time >= decision_start_time:
            raise ValueError(
                "CALL_AUCTION_START_TIME 必须早于 CALL_AUCTION_DECISION_START_TIME"
            )
        if decision_start_time >= end_time:
            raise ValueError(
                "CALL_AUCTION_DECISION_START_TIME 必须早于 CALL_AUCTION_END_TIME"
            )
        if self.call_auction_auto_interval_seconds <= 0:
            raise ValueError("CALL_AUCTION_AUTO_INTERVAL_SECONDS 必须是正整数")
        if self.call_auction_preselect_limit <= 0:
            raise ValueError("CALL_AUCTION_PRESELECT_LIMIT 必须是正整数")
        if self.call_auction_llm_review_limit <= 0:
            raise ValueError("CALL_AUCTION_LLM_REVIEW_LIMIT 必须是正整数")
        if self.call_auction_final_limit <= 0:
            raise ValueError("CALL_AUCTION_FINAL_LIMIT 必须是正整数")
        if self.call_auction_require_llm not in {0, 1}:
            raise ValueError("CALL_AUCTION_REQUIRE_LLM 只能是 0 或 1")
        if self.call_auction_llm_timeout_seconds <= 0:
            raise ValueError("CALL_AUCTION_LLM_TIMEOUT_SECONDS 必须是正整数")
        if self.call_auction_min_pct_change < 0:
            raise ValueError("CALL_AUCTION_MIN_PCT_CHANGE 不能小于 0")
        if self.call_auction_max_pct_change <= self.call_auction_min_pct_change:
            raise ValueError("CALL_AUCTION_MAX_PCT_CHANGE 必须大于最小涨幅")
        if self.call_auction_min_volume_ratio < 0:
            raise ValueError("CALL_AUCTION_MIN_VOLUME_RATIO 不能小于 0")
        if self.call_auction_min_amount < 0:
            raise ValueError("CALL_AUCTION_MIN_AMOUNT 不能小于 0")
        if self.call_auction_min_final_score < 0:
            raise ValueError("CALL_AUCTION_MIN_FINAL_SCORE 不能小于 0")
        if self.three_day_analysis_lookback_days < 60:
            raise ValueError("THREE_DAY_ANALYSIS_LOOKBACK_DAYS 至少需要 60")
        if self.three_day_preselect_limit <= 0:
            raise ValueError("THREE_DAY_PRESELECT_LIMIT 必须是正整数")
        if self.three_day_final_limit <= 0:
            raise ValueError("THREE_DAY_FINAL_LIMIT 必须是正整数")
        if self.realtime_quote_limit <= 0:
            raise ValueError("REALTIME_QUOTE_LIMIT 必须是正整数")
        if self.realtime_signal_limit <= 0:
            raise ValueError("REALTIME_SIGNAL_LIMIT 必须是正整数")
        if self.realtime_decision_mode not in {"rules", "llm_review"}:
            raise ValueError("REALTIME_DECISION_MODE 只能是 rules 或 llm_review")
        if self.realtime_auto_interval_seconds <= 0:
            raise ValueError("REALTIME_AUTO_INTERVAL_SECONDS 必须是正整数")
        if self.realtime_llm_candidate_limit <= 0:
            raise ValueError("REALTIME_LLM_CANDIDATE_LIMIT 必须是正整数")
        if self.simulation_initial_cash <= 0:
            raise ValueError("SIMULATION_INITIAL_CASH 必须大于 0")
        if not 0 < self.simulation_order_cash_pct <= 1:
            raise ValueError("SIMULATION_ORDER_CASH_PCT 必须在 0 到 1 之间")
        if self.simulation_max_positions <= 0:
            raise ValueError("SIMULATION_MAX_POSITIONS 必须是正整数")
        if not 0 <= self.simulation_fee_rate < 0.01:
            raise ValueError("SIMULATION_FEE_RATE 必须在 0 到 0.01 之间")
        self.schedule_hour_minute()

    def mysql_kwargs(self, include_database: bool = True) -> dict[str, Any]:
        """生成 PyMySQL 连接参数。"""

        kwargs: dict[str, Any] = {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "charset": self.mysql_charset,
            "autocommit": False,
            "connect_timeout": 15,
            "read_timeout": 60,
            "write_timeout": 60,
        }
        if include_database:
            kwargs["database"] = self.mysql_database
        return kwargs

    def t1_llm_allowed_action_set(self) -> set[str]:
        """解析 T+1 LLM 允许进入最终候选的动作集合。"""

        return {
            action.strip().upper()
            for action in self.t1_llm_allowed_actions.split(",")
            if action.strip()
        }

    def schedule_hour_minute(self) -> tuple[int, int]:
        """解析每日调度时间，返回小时和分钟。"""

        try:
            hour_text, minute_text = self.schedule_time.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            raise ValueError("ASTOCKS_SCHEDULE_TIME 必须使用 HH:MM 格式") from exc

        if hour not in range(24) or minute not in range(60):
            raise ValueError("ASTOCKS_SCHEDULE_TIME 的小时或分钟超出范围")
        return hour, minute


def _read_str(name: str, default: str) -> str:
    """读取字符串环境变量。"""

    return os.getenv(name, default).strip()


def _read_int(name: str, default: int) -> int:
    """读取整数环境变量。"""

    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def _read_float(name: str, default: float) -> float:
    """读取浮点数环境变量。"""

    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def _parse_clock_time(value: str, name: str) -> tuple[int, int]:
    """解析 HH:MM 时间配置，用于启动时校验交易窗口。"""

    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise ValueError(f"{name} 必须使用 HH:MM 格式") from exc
    if hour not in range(24) or minute not in range(60):
        raise ValueError(f"{name} 的小时或分钟超出范围")
    return hour, minute
