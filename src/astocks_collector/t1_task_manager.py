"""T+1 实时模拟任务管理器。

该模块负责在 FastAPI 进程内维护一个可启停的 T+1 到期执行任务。任务按固定
间隔触发 T+1 实时模拟交易，只在 A 股开盘时真正撮合，非开盘时记录跳过状态。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import AppConfig

logger = logging.getLogger(__name__)


DEFAULT_TASK_ID = "default"
DEFAULT_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class T1TaskConfig:
    """T+1 到期执行任务配置。"""

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    final_limit: int = 20
    execute_trades: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可返回的字典。"""
        return {
            "intervalSeconds": self.interval_seconds,
            "finalLimit": self.final_limit,
            "executeTrades": self.execute_trades,
        }


def normalize_interval_seconds(value: int | None) -> int:
    """规范化任务间隔秒数。"""
    interval = DEFAULT_INTERVAL_SECONDS if value is None else int(value)
    if interval <= 0:
        raise ValueError("任务间隔必须大于 0 秒")
    return interval


def next_due_time(now: datetime, interval_seconds: int) -> datetime:
    """计算下一次到期执行时间。"""
    return now + timedelta(seconds=normalize_interval_seconds(interval_seconds))


class T1RealtimeTaskManager:
    """管理 T+1 实时模拟后台任务的启动、停止和状态。"""

    def __init__(self) -> None:
        """初始化任务管理器。"""
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._config = T1TaskConfig()
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._last_run_at: datetime | None = None
        self._next_run_at: datetime | None = None
        self._run_count = 0

    def start(self, settings: "AppConfig", config: T1TaskConfig) -> dict[str, Any]:
        """启动 T+1 实时模拟任务；已运行时只更新配置。"""
        with self._lock:
            self._config = config
            self._stop_event.clear()
            self._ensure_task_schema(settings)
            self._write_task_status(settings, "RUNNING", "任务已启动或更新")
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
        """停止 T+1 实时模拟任务。"""
        with self._lock:
            self._stop_event.set()
            self._write_task_status(settings, "STOPPED", "用户手动停止")
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
                "finalLimit": int(db_status.get("final_limit") or self._config.final_limit),
                "executeTrades": bool(db_status.get("execute_trades") if db_status else self._config.execute_trades),
                "runCount": int(db_status.get("run_count") or self._run_count),
                "lastRunAt": _datetime_to_str(db_status.get("last_run_at") or self._last_run_at),
                "nextRunAt": _datetime_to_str(db_status.get("next_run_at") or self._next_run_at),
                "lastMessage": db_status.get("last_message") or self._last_error or "",
                "lastResult": self._last_result,
            }

    def _run_loop(self, settings: "AppConfig") -> None:
        """后台循环执行 T+1 实时模拟。"""
        from .db import MySQLRepository
        from .t1_trading import T1TradingEngine

        while not self._stop_event.is_set():
            with self._lock:
                config = self._config
            now = datetime.now().replace(microsecond=0)
            self._last_run_at = now
            try:
                repository = MySQLRepository(settings)
                result = T1TradingEngine(settings, repository).run_realtime_once(
                    execute_trades=config.execute_trades,
                    final_limit=config.final_limit,
                )
                self._last_result = result.to_dict()
                self._last_error = ""
                self._run_count += 1
                message = "执行完成" if not result.skipped else str(result.skip_reason or "已跳过")
                logger.info("T+1实时模拟任务完成: %s", self._last_result)
            except Exception as exc:
                self._last_error = str(exc)
                message = f"执行失败: {exc}"
                logger.exception("T+1实时模拟任务失败")
            self._next_run_at = next_due_time(datetime.now().replace(microsecond=0), config.interval_seconds)
            self._write_task_status(settings, "RUNNING", message)
            if self._stop_event.wait(config.interval_seconds):
                break
        self._write_task_status(settings, "STOPPED", "任务已停止")

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
                        final_limit INT NOT NULL DEFAULT 20,
                        execute_trades TINYINT(1) NOT NULL DEFAULT 1,
                        run_count INT NOT NULL DEFAULT 0,
                        last_run_at DATETIME NULL,
                        next_run_at DATETIME NULL,
                        last_message VARCHAR(512) NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cursor.execute(
                    """
                    INSERT IGNORE INTO t1_simulation_task
                        (task_id, status, interval_seconds, final_limit, execute_trades)
                    VALUES (%s, 'STOPPED', %s, %s, %s)
                    """,
                    (
                        DEFAULT_TASK_ID,
                        self._config.interval_seconds,
                        self._config.final_limit,
                        int(self._config.execute_trades),
                    ),
                )
            conn.commit()

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
                        (task_id, status, interval_seconds, final_limit, execute_trades,
                         run_count, last_run_at, next_run_at, last_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        status=VALUES(status),
                        interval_seconds=VALUES(interval_seconds),
                        final_limit=VALUES(final_limit),
                        execute_trades=VALUES(execute_trades),
                        run_count=VALUES(run_count),
                        last_run_at=VALUES(last_run_at),
                        next_run_at=VALUES(next_run_at),
                        last_message=VALUES(last_message)
                    """,
                    (
                        DEFAULT_TASK_ID,
                        status,
                        self._config.interval_seconds,
                        self._config.final_limit,
                        int(self._config.execute_trades),
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


t1_task_manager = T1RealtimeTaskManager()
