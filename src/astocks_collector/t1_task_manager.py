"""T+1 长期模型任务管理器。

该模块负责在 FastAPI 进程内维护一个可启停的 T+1 长期任务。任务按固定
间隔确保 T+1 模型结果已刷新，并在 A 股开盘时执行实时模拟撮合。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import AppConfig

logger = logging.getLogger(__name__)


DEFAULT_TASK_ID = "default"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_PRESELECT_LIMIT = 120


@dataclass(slots=True)
class T1TaskConfig:
    """T+1 长期模型任务配置。"""

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    preselect_limit: int = DEFAULT_PRESELECT_LIMIT
    final_limit: int = 20
    execute_trades: bool = True
    use_llm: bool = True
    run_analysis: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可返回的字典。"""
        return {
            "intervalSeconds": self.interval_seconds,
            "preselectLimit": self.preselect_limit,
            "finalLimit": self.final_limit,
            "executeTrades": self.execute_trades,
            "useLlm": self.use_llm,
            "runAnalysis": self.run_analysis,
        }


def normalize_interval_seconds(value: int | None) -> int:
    """规范化任务间隔秒数。"""
    interval = DEFAULT_INTERVAL_SECONDS if value is None else int(value)
    if interval <= 0:
        raise ValueError("任务间隔必须大于 0 秒")
    return interval


def next_due_time(now: datetime, interval_seconds: int) -> datetime:
    """计算下一次循环执行时间。"""
    return now + timedelta(seconds=normalize_interval_seconds(interval_seconds))


class T1RealtimeTaskManager:
    """管理 T+1 长期模型后台任务的启动、停止和状态。"""

    def __init__(self) -> None:
        """初始化任务管理器。"""
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._config = T1TaskConfig()
        self._last_result: dict[str, Any] | None = None
        self._last_analysis_result: dict[str, Any] | None = None
        self._last_analysis_trade_date: date | None = None
        self._last_error: str | None = None
        self._last_run_at: datetime | None = None
        self._next_run_at: datetime | None = None
        self._run_count = 0

    def start(self, settings: "AppConfig", config: T1TaskConfig) -> dict[str, Any]:
        """启动 T+1 长期模型任务；已运行时只更新配置。"""
        with self._lock:
            self._config = config
            self._stop_event.clear()
            self._ensure_task_schema(settings)
            db_status = self._read_task_status(settings)
            self._run_count = int(db_status.get("run_count") or self._run_count)
            self._write_task_status(settings, "RUNNING", "长期任务已启动或更新")
            if self._thread and self._thread.is_alive():
                return self.status(settings)
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(settings,),
                name="astocks-t1-realtime-task",
                daemon=True,
            )
            self._thread.start()
            return self.status(settings)

    def stop(self, settings: "AppConfig") -> dict[str, Any]:
        """停止 T+1 长期模型任务。"""
        with self._lock:
            self._stop_event.set()
            self._safe_write_task_status(settings, "STOPPED", "用户手动停止")
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        return self.status(settings)

    def status(self, settings: "AppConfig") -> dict[str, Any]:
        """返回当前任务状态。"""
        self._ensure_task_schema(settings)
        db_status = self._read_task_status(settings)
        with self._lock:
            running = bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())
            return {
                "taskId": DEFAULT_TASK_ID,
                "status": "RUNNING" if running else str(db_status.get("status") or "STOPPED"),
                "running": running,
                "intervalSeconds": int(db_status.get("interval_seconds") or self._config.interval_seconds),
                "preselectLimit": int(db_status.get("preselect_limit") or self._config.preselect_limit),
                "finalLimit": int(db_status.get("final_limit") or self._config.final_limit),
                "executeTrades": bool(db_status.get("execute_trades") if db_status else self._config.execute_trades),
                "useLlm": bool(db_status.get("use_llm") if db_status else self._config.use_llm),
                "runAnalysis": bool(db_status.get("run_analysis") if db_status else self._config.run_analysis),
                "runCount": int(db_status.get("run_count") or self._run_count),
                "lastRunAt": _datetime_to_str(db_status.get("last_run_at") or self._last_run_at),
                "nextRunAt": _datetime_to_str(db_status.get("next_run_at") or self._next_run_at),
                "lastMessage": db_status.get("last_message") or self._last_error or "",
                "lastResult": self._last_result,
            }

    def _run_loop(self, settings: "AppConfig") -> None:
        """后台循环执行 T+1 模型分析和实时模拟。"""
        from .db import MySQLRepository
        from .t1_trading import T1TradingEngine

        while not self._stop_event.is_set():
            with self._lock:
                config = self._config
            now = datetime.now().replace(microsecond=0)
            self._last_run_at = now
            analysis_result: dict[str, Any] | None = None
            trading_result: dict[str, Any] | None = None
            try:
                repository = MySQLRepository(settings)
                analysis_message, analysis_result = self._refresh_analysis_if_needed(
                    settings,
                    repository,
                    config,
                )
                result = T1TradingEngine(settings, repository).run_realtime_once(
                    execute_trades=config.execute_trades,
                    final_limit=config.final_limit,
                )
                trading_result = result.to_dict()
                self._last_result = {
                    "analysis": analysis_result or self._last_analysis_result,
                    "trading": trading_result,
                }
                self._last_error = ""
                self._run_count += 1
                trading_message = "实时模拟完成" if not result.skipped else str(result.skip_reason or "实时模拟已跳过")
                message = f"{analysis_message}；{trading_message}"
                logger.info("T+1长期模型任务完成: %s", self._last_result)
            except Exception as exc:
                self._last_error = str(exc)
                message = f"执行失败: {exc}"
                logger.exception("T+1长期模型任务失败")
            self._next_run_at = next_due_time(datetime.now().replace(microsecond=0), config.interval_seconds)
            self._safe_write_task_status(settings, "RUNNING", message)
            if self._stop_event.wait(config.interval_seconds):
                break
        self._safe_write_task_status(settings, "STOPPED", "用户手动停止")

    def _refresh_analysis_if_needed(
        self,
        settings: "AppConfig",
        repository: Any,
        config: T1TaskConfig,
    ) -> tuple[str, dict[str, Any] | None]:
        """按最新交易日刷新 T+1 模型结果，避免同一交易日重复消耗 LLM。"""
        from .analysis import T1StockAnalyzer

        if not config.run_analysis:
            return "模型分析已关闭", None
        repository.ensure_schema()
        latest_trade_date = self._latest_daily_trade_date(repository)
        if latest_trade_date is None:
            return "尚无可分析交易日", None
        latest_analysis_date = self._latest_analysis_date(repository)
        if latest_analysis_date == latest_trade_date:
            with self._lock:
                self._last_analysis_trade_date = latest_trade_date
            return f"模型结果已是最新({latest_trade_date.isoformat()})", None
        with self._lock:
            if self._last_analysis_trade_date == latest_trade_date:
                return f"模型已在本任务内处理({latest_trade_date.isoformat()})", self._last_analysis_result
        result = T1StockAnalyzer(settings, repository=repository).analyze(
            preselect_limit=config.preselect_limit,
            final_limit=config.final_limit,
            use_llm=config.use_llm,
        )
        result_dict = _analysis_result_to_dict(result)
        with self._lock:
            self._last_analysis_trade_date = _date_from_value(result_dict.get("analysisDate"))
            self._last_analysis_result = result_dict
        return (
            f"模型分析完成({result_dict.get('analysisDate')})，候选{result_dict.get('finalCount')}只",
            result_dict,
        )

    def _latest_daily_trade_date(self, repository: Any) -> date | None:
        """读取日线表中的最新交易日。"""
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(trade_date) AS trade_date FROM stock_daily")
                row = cursor.fetchone() or {}
        return _date_from_value(row.get("trade_date"))

    def _latest_analysis_date(self, repository: Any) -> date | None:
        """读取已经落库的最新 T+1 分析日期。"""
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT MAX(analysis_date) AS analysis_date FROM stock_analysis_pick")
                row = cursor.fetchone() or {}
        return _date_from_value(row.get("analysis_date"))

    def _safe_write_task_status(self, settings: "AppConfig", status: str, message: str) -> None:
        """安全写入任务状态，避免状态表异常中断长期循环。"""
        try:
            self._write_task_status(settings, status, message)
        except Exception:
            logger.exception("写入 T+1 长期模型任务状态失败")

    def _ensure_task_schema(self, settings: "AppConfig") -> None:
        """确保任务状态表存在。"""
        from .db import MySQLRepository

        repository = MySQLRepository(settings)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS t1_simulation_task (
                        task_id VARCHAR(64) PRIMARY KEY,
                        status VARCHAR(16) NOT NULL DEFAULT 'STOPPED',
                        interval_seconds INT NOT NULL DEFAULT 60,
                        preselect_limit INT NOT NULL DEFAULT 120,
                        final_limit INT NOT NULL DEFAULT 20,
                        execute_trades TINYINT(1) NOT NULL DEFAULT 1,
                        use_llm TINYINT(1) NOT NULL DEFAULT 1,
                        run_analysis TINYINT(1) NOT NULL DEFAULT 1,
                        run_count INT NOT NULL DEFAULT 0,
                        last_run_at DATETIME NULL,
                        next_run_at DATETIME NULL,
                        last_message VARCHAR(512) NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                self._ensure_task_column(cursor, "preselect_limit", "INT NOT NULL DEFAULT 120")
                self._ensure_task_column(cursor, "use_llm", "TINYINT(1) NOT NULL DEFAULT 1")
                self._ensure_task_column(cursor, "run_analysis", "TINYINT(1) NOT NULL DEFAULT 1")
                cursor.execute(
                    """
                    INSERT IGNORE INTO t1_simulation_task
                        (task_id, status, interval_seconds, preselect_limit, final_limit,
                         execute_trades, use_llm, run_analysis)
                    VALUES (%s, 'STOPPED', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        DEFAULT_TASK_ID,
                        self._config.interval_seconds,
                        self._config.preselect_limit,
                        self._config.final_limit,
                        int(self._config.execute_trades),
                        int(self._config.use_llm),
                        int(self._config.run_analysis),
                    ),
                )
            conn.commit()

    def _ensure_task_column(self, cursor: Any, column_name: str, definition: str) -> None:
        """为旧版本任务状态表补齐新增字段。"""
        cursor.execute("SHOW COLUMNS FROM t1_simulation_task LIKE %s", (column_name,))
        if not cursor.fetchone():
            cursor.execute(f"ALTER TABLE t1_simulation_task ADD COLUMN {column_name} {definition}")

    def _read_task_status(self, settings: "AppConfig") -> dict[str, Any]:
        """读取任务持久化状态。"""
        from .db import MySQLRepository

        repository = MySQLRepository(settings)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM t1_simulation_task WHERE task_id=%s",
                    (DEFAULT_TASK_ID,),
                )
                return cursor.fetchone() or {}

    def _write_task_status(self, settings: "AppConfig", status: str, message: str) -> None:
        """写入任务持久化状态。"""
        from .db import MySQLRepository

        repository = MySQLRepository(settings)
        with repository.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t1_simulation_task
                        (task_id, status, interval_seconds, preselect_limit, final_limit,
                         execute_trades, use_llm, run_analysis, run_count,
                         last_run_at, next_run_at, last_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        status=VALUES(status),
                        interval_seconds=VALUES(interval_seconds),
                        preselect_limit=VALUES(preselect_limit),
                        final_limit=VALUES(final_limit),
                        execute_trades=VALUES(execute_trades),
                        use_llm=VALUES(use_llm),
                        run_analysis=VALUES(run_analysis),
                        run_count=VALUES(run_count),
                        last_run_at=VALUES(last_run_at),
                        next_run_at=VALUES(next_run_at),
                        last_message=VALUES(last_message)
                    """,
                    (
                        DEFAULT_TASK_ID,
                        status,
                        self._config.interval_seconds,
                        self._config.preselect_limit,
                        self._config.final_limit,
                        int(self._config.execute_trades),
                        int(self._config.use_llm),
                        int(self._config.run_analysis),
                        self._run_count,
                        self._last_run_at,
                        self._next_run_at,
                        message[:512],
                    ),
                )
            conn.commit()


def _datetime_to_str(value: Any) -> str | None:
    """把数据库时间转换为字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _date_from_value(value: Any) -> date | None:
    """把数据库日期值转换为 date。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _analysis_result_to_dict(result: Any) -> dict[str, Any]:
    """把 T+1 分析结果转换为任务状态可返回的字典。"""
    return {
        "analysisDate": _date_to_str(getattr(result, "analysis_date", None)),
        "tradeDate": _date_to_str(getattr(result, "trade_date", None)),
        "preselectCount": int(getattr(result, "preselect_count", 0) or 0),
        "finalCount": int(getattr(result, "final_count", 0) or 0),
    }


def _date_to_str(value: Any) -> str | None:
    """把日期值转换为 ISO 字符串。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


t1_task_manager = T1RealtimeTaskManager()
