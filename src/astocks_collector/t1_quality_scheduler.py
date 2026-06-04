"""T+1 质量选股自动调度器。

该模块把 14:05 质量选股任务封装为 API 进程和 scheduler 进程都可复用的
调度入口。真正的幂等与跳过逻辑由 T1IntradayQualitySelector 负责。
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .db import MySQLRepository
from .t1_quality import T1IntradayQualitySelector, parse_quality_schedule_time

logger = logging.getLogger(__name__)

_api_scheduler: BackgroundScheduler | None = None


def run_t1_quality_job(settings: AppConfig, trigger_type: str = "scheduler") -> dict[str, Any]:
    """执行一次 T+1 质量选股自动任务。"""
    logger.info("开始执行 T+1 质量选股任务: trigger_type=%s", trigger_type)
    repository = MySQLRepository(settings)
    result = T1IntradayQualitySelector(settings, repository).run_once(
        execute_trades=bool(settings.t1_quality_execute_trades),
        final_limit=settings.t1_quality_final_limit,
        force=False,
        trigger_type=trigger_type,
    )
    payload = result.to_dict()
    logger.info("T+1 质量选股任务结束: %s", payload)
    return payload


class ApiT1QualityScheduler:
    """FastAPI 进程内 T+1 质量选股后台调度器。"""

    def __init__(self, settings: AppConfig) -> None:
        """保存配置并创建后台调度器。"""
        self.settings = settings
        self.scheduler = BackgroundScheduler(timezone=settings.timezone)

    def start(self) -> None:
        """按配置启动每日 14:05 质量选股任务。"""
        if not bool(self.settings.t1_quality_enabled) or not bool(self.settings.t1_quality_autostart_api_scheduler):
            logger.info("API T+1质量选股调度器未启用")
            return
        hour, minute = parse_quality_schedule_time(self.settings.t1_quality_schedule_time)
        self.scheduler.add_job(
            lambda: run_t1_quality_job(self.settings, trigger_type="api_scheduler"),
            trigger=CronTrigger(hour=hour, minute=minute, timezone=self.settings.timezone),
            id="api_t1_quality_daily",
            name="API进程T+1 14:05质量选股",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
        self.scheduler.start()
        logger.info("API T+1质量选股调度器已启动: %s %02d:%02d", self.settings.timezone, hour, minute)

    def stop(self) -> None:
        """停止 API 进程内调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("API T+1质量选股调度器已停止")


def start_api_t1_quality_scheduler(settings: AppConfig) -> None:
    """启动全局 API T+1 质量选股调度器。"""
    global _api_scheduler
    if _api_scheduler and _api_scheduler.scheduler.running:
        return
    _api_scheduler = ApiT1QualityScheduler(settings)
    _api_scheduler.start()


def stop_api_t1_quality_scheduler() -> None:
    """停止全局 API T+1 质量选股调度器。"""
    global _api_scheduler
    if _api_scheduler:
        _api_scheduler.stop()
        _api_scheduler = None
