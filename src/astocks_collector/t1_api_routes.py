"""T+1 专属 API 路由注册。

该模块在 Web 应用创建时自动注册 T+1 页面所需接口，避免和既有实时交易接口
耦合。接口只服务模拟交易和策略研究，不连接真实券商。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .analysis import T1StockAnalyzer
from .config import AppConfig
from .db import MySQLRepository
from .t1_task_manager import (
    DEFAULT_PRESELECT_LIMIT,
    T1TaskConfig,
    normalize_interval_seconds,
    t1_task_manager,
)
from .t1_quality import T1IntradayQualitySelector
from .t1_trading import T1TradingEngine


class T1TradingRunRequest(BaseModel):
    """T+1 模拟运行请求。"""

    execute_trades: bool | None = None
    executeTrades: bool | None = None
    final_limit: int | None = None
    finalLimit: int | None = None

    def should_execute_trades(self) -> bool:
        """兼容前端 camelCase 和后端 snake_case 字段。"""
        if self.executeTrades is not None:
            return bool(self.executeTrades)
        if self.execute_trades is not None:
            return bool(self.execute_trades)
        return True

    def limit(self) -> int:
        """读取最终候选数量。"""
        value = self.finalLimit if self.finalLimit is not None else self.final_limit
        return int(value or 20)


class T1TradingResetRequest(BaseModel):
    """T+1 账户重置请求。"""

    initial_cash: float | None = None
    initialCash: float | None = None

    def cash(self) -> float | None:
        """兼容前端 camelCase 和后端 snake_case 初始资金字段。"""
        return self.initialCash if self.initialCash is not None else self.initial_cash


class T1AnalysisRunRequest(BaseModel):
    """T+1 分析运行请求。"""

    preselect_limit: int | None = None
    preselectLimit: int | None = None
    final_limit: int | None = None
    finalLimit: int | None = None
    no_llm: bool | None = None
    noLlm: bool | None = None

    def preselect(self) -> int:
        """读取量化预筛数量。"""
        value = self.preselectLimit if self.preselectLimit is not None else self.preselect_limit
        return int(value or 120)

    def final(self) -> int:
        """读取最终输出数量。"""
        value = self.finalLimit if self.finalLimit is not None else self.final_limit
        return int(value or 20)

    def use_llm(self) -> bool:
        """读取是否启用 LLM 复核。"""
        value = self.noLlm if self.noLlm is not None else self.no_llm
        return not bool(value)


class T1QualityRunRequest(BaseModel):
    """T+1 质量选股手动运行请求。"""

    execute_trades: bool | None = None
    executeTrades: bool | None = None
    final_limit: int | None = None
    finalLimit: int | None = None
    force: bool | None = None

    def should_execute_trades(self) -> bool:
        """兼容前端 camelCase 和后端 snake_case 字段。"""
        if self.executeTrades is not None:
            return bool(self.executeTrades)
        if self.execute_trades is not None:
            return bool(self.execute_trades)
        return True

    def limit(self, default: int = 20) -> int:
        """读取质量候选最终数量。"""
        value = self.finalLimit if self.finalLimit is not None else self.final_limit
        return int(value or default)

    def should_force(self) -> bool:
        """读取是否强制跳过时间窗和当天成功运行检查。"""
        return bool(self.force)


class T1TaskStartRequest(BaseModel):
    """T+1 长期模型任务启动请求。"""

    interval_seconds: int | None = None
    intervalSeconds: int | None = None
    preselect_limit: int | None = None
    preselectLimit: int | None = None
    final_limit: int | None = None
    finalLimit: int | None = None
    execute_trades: bool | None = None
    executeTrades: bool | None = None
    use_llm: bool | None = None
    useLlm: bool | None = None
    no_llm: bool | None = None
    noLlm: bool | None = None
    run_analysis: bool | None = None
    runAnalysis: bool | None = None

    def to_task_config(self) -> T1TaskConfig:
        """转换为任务配置。"""
        interval_value = self.intervalSeconds if self.intervalSeconds is not None else self.interval_seconds
        preselect_value = self.preselectLimit if self.preselectLimit is not None else self.preselect_limit
        final_value = self.finalLimit if self.finalLimit is not None else self.final_limit
        execute_value = self.executeTrades if self.executeTrades is not None else self.execute_trades
        use_llm_value = self.useLlm if self.useLlm is not None else self.use_llm
        no_llm_value = self.noLlm if self.noLlm is not None else self.no_llm
        run_analysis_value = self.runAnalysis if self.runAnalysis is not None else self.run_analysis
        if use_llm_value is None:
            use_llm = not bool(no_llm_value) if no_llm_value is not None else True
        else:
            use_llm = bool(use_llm_value)
        return T1TaskConfig(
            interval_seconds=normalize_interval_seconds(interval_value),
            preselect_limit=_positive_int(preselect_value, DEFAULT_PRESELECT_LIMIT, "preselectLimit"),
            final_limit=_positive_int(final_value, 20, "finalLimit"),
            use_llm=use_llm,
            run_analysis=True if run_analysis_value is None else bool(run_analysis_value),
            execute_trades=True if execute_value is None else bool(execute_value),
        )


def install_t1_routes(app: FastAPI) -> None:
    """向 FastAPI 应用注册 T+1 专属接口。"""
    if getattr(app.state, "astocks_t1_routes_installed", False):
        return
    app.state.astocks_t1_routes_installed = True

    @app.get("/api/t1-trading")
    def get_t1_trading(finalLimit: int = 20) -> dict[str, Any]:
        """返回 T+1 专属模拟交易仪表盘。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        data = T1TradingEngine(settings, repository).dashboard(final_limit=finalLimit)
        data["task"] = t1_task_manager.status(settings)
        return data

    @app.post("/api/t1-trading/run")
    def run_t1_trading(payload: T1TradingRunRequest | None = None) -> dict[str, Any]:
        """执行一次 T+1 模拟交易。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        result = T1TradingEngine(settings, repository).run_once(
            execute_trades=payload.should_execute_trades() if payload else True,
            final_limit=payload.limit() if payload else 20,
        )
        return result.to_dict()

    @app.post("/api/t1-trading/realtime-run")
    def run_t1_realtime_trading(payload: T1TradingRunRequest | None = None) -> dict[str, Any]:
        """按真实实时行情执行一次 T+1 模拟交易。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        result = T1TradingEngine(settings, repository).run_realtime_once(
            execute_trades=payload.should_execute_trades() if payload else True,
            final_limit=payload.limit() if payload else 20,
        )
        return result.to_dict()

    @app.post("/api/t1-trading/reset")
    def reset_t1_trading(payload: T1TradingResetRequest | None = None) -> dict[str, Any]:
        """重置 T+1 模拟账户。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        account = T1TradingEngine(settings, repository).reset_account(payload.cash() if payload else None)
        return {"account": account}

    @app.get("/api/t1-trading/task")
    def get_t1_task() -> dict[str, Any]:
        """返回 T+1 长期模型任务状态。"""
        settings = AppConfig.from_env()
        return t1_task_manager.status(settings)

    @app.get("/api/t1-quality")
    def get_t1_quality(finalLimit: int = 20) -> dict[str, Any]:
        """返回最新 14:05 T+1 质量选股结果。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        repository.ensure_schema()
        return {
            "latestRun": repository.latest_t1_quality_run(),
            "picks": repository.latest_t1_quality_picks(limit=finalLimit),
        }

    @app.post("/api/t1-quality/run")
    def run_t1_quality(payload: T1QualityRunRequest | None = None) -> dict[str, Any]:
        """手动运行一次 14:05 T+1 质量选股。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        request = payload or T1QualityRunRequest()
        result = T1IntradayQualitySelector(settings, repository).run_once(
            execute_trades=request.should_execute_trades(),
            final_limit=request.limit(int(getattr(settings, "t1_quality_final_limit", 3))),
            force=request.should_force(),
            trigger_type="manual",
        )
        return result.to_dict()

    @app.post("/api/t1-trading/task/start")
    def start_t1_task(payload: T1TaskStartRequest | None = None) -> dict[str, Any]:
        """启动或更新 T+1 长期模型任务。"""
        settings = AppConfig.from_env()
        request = payload or T1TaskStartRequest()
        return t1_task_manager.start(settings, request.to_task_config())

    @app.post("/api/t1-trading/task/stop")
    def stop_t1_task() -> dict[str, Any]:
        """停止 T+1 长期模型任务。"""
        settings = AppConfig.from_env()
        return t1_task_manager.stop(settings)

    @app.post("/api/t1-analysis/run")
    def run_t1_analysis(payload: T1AnalysisRunRequest | None = None) -> dict[str, Any]:
        """手动运行一次 T+1 分析。"""
        settings = AppConfig.from_env()
        repository = MySQLRepository(settings)
        request = payload or T1AnalysisRunRequest()
        analyzer = _build_t1_analyzer(settings, repository)
        result = _call_t1_analyzer(
            analyzer,
            preselect_limit=request.preselect(),
            final_limit=request.final(),
            use_llm=request.use_llm(),
        )
        return _analysis_result_to_dict(result)


def _patch_fastapi_init() -> None:
    """在 Web 应用构造时自动安装 T+1 路由。"""
    if getattr(FastAPI, "_astocks_t1_patched", False):
        return
    original_init = FastAPI.__init__

    def patched_init(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        """调用原始构造函数后注册 T+1 路由。"""
        original_init(self, *args, **kwargs)
        install_t1_routes(self)

    FastAPI.__init__ = patched_init  # type: ignore[method-assign]
    FastAPI._astocks_t1_patched = True  # type: ignore[attr-defined]


_patch_fastapi_init()


def _build_t1_analyzer(settings: Any, repository: MySQLRepository) -> T1StockAnalyzer:
    """兼容不同构造签名创建 T+1 分析器。"""
    for args, kwargs in (
        ((settings, repository), {}),
        ((repository, settings), {}),
        ((), {"settings": settings, "repository": repository}),
        ((), {"repository": repository, "settings": settings}),
    ):
        try:
            return T1StockAnalyzer(*args, **kwargs)
        except TypeError:
            continue
    return T1StockAnalyzer(settings, repository)


def _call_t1_analyzer(
    analyzer: T1StockAnalyzer,
    preselect_limit: int,
    final_limit: int,
    use_llm: bool,
) -> Any:
    """兼容不同 analyze 签名运行 T+1 分析。"""
    for kwargs in (
        {"preselect_limit": preselect_limit, "final_limit": final_limit, "use_llm": use_llm},
        {"preselect_limit": preselect_limit, "final_limit": final_limit, "no_llm": not use_llm},
        {"preselect_limit": preselect_limit, "final_limit": final_limit},
        {"final_limit": final_limit},
        {},
    ):
        try:
            return analyzer.analyze(**kwargs)
        except TypeError:
            continue
    return analyzer.analyze()


def _analysis_result_to_dict(result: Any) -> dict[str, Any]:
    """把 T+1 分析结果转换为 API 响应。"""
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if isinstance(result, dict):
        return result
    data = getattr(result, "__dict__", {})
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in data.items()
        if not key.startswith("_")
    }


def _positive_int(value: int | None, default: int, field_name: str) -> int:
    """校验任务配置中的正整数字段。"""
    normalized = default if value is None else int(value)
    if normalized <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return normalized
