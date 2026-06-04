"""定时采集任务入口。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from astocks_collector.collector import StockCollector
from astocks_collector.config import AppConfig
from astocks_collector.t1_quality import parse_quality_schedule_time
from astocks_collector.t1_quality_scheduler import run_t1_quality_job

logger = logging.getLogger(__name__)


class CollectorScheduler:
    """基于 APScheduler 的常驻每日增量调度器。"""

    def __init__(self, config: AppConfig) -> None:
        """保存调度配置，并初始化阻塞式调度器。"""

        self.config = config
        self.scheduler = BlockingScheduler(timezone=config.timezone)

    def start(self) -> None:
        """启动每日增量采集调度。"""

        hour, minute = self.config.schedule_hour_minute()
        trigger = CronTrigger(hour=hour, minute=minute, timezone=self.config.timezone)
        self.scheduler.add_job(
            self.run_incremental,
            trigger=trigger,
            id="daily_incremental",
            name="A股每日增量采集",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        logger.info(
            "A股每日增量调度已启动: %s %02d:%02d",
            self.config.timezone,
            hour,
            minute,
        )
        self._add_t1_quality_job()
        self.scheduler.start()

    def _add_t1_quality_job(self) -> None:
        """按配置增加 T+1 14:05 质量选股任务。"""
        if not bool(self.config.t1_quality_enabled):
            logger.info("T+1 质量选股调度未启用")
            return
        hour, minute = parse_quality_schedule_time(self.config.t1_quality_schedule_time)
        self.scheduler.add_job(
            lambda: run_t1_quality_job(self.config, trigger_type="scheduler"),
            trigger=CronTrigger(hour=hour, minute=minute, timezone=self.config.timezone),
            id="t1_quality_daily",
            name="T+1 14:05质量选股",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
        logger.info(
            "T+1 质量选股调度已启动: %s %02d:%02d",
            self.config.timezone,
            hour,
            minute,
        )

    def run_incremental(self) -> None:
        """执行一次每日增量采集任务。"""

        logger.info("开始执行定时增量采集")
        collector = StockCollector(self.config)
        result = collector.incremental(days=self.config.daily_window_days)
        logger.info(
            "定时增量采集结束: status=%s total=%s success=%s failed=%s rows=%s",
            result.status,
            result.total_symbols,
            result.success_symbols,
            result.failed_symbols,
            result.rows_written,
        )


def run_scheduler(config: AppConfig) -> None:
    """创建并启动采集器调度服务。"""

    CollectorScheduler(config).start()
