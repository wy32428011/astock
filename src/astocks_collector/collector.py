"""A股采集任务编排。"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from astocks_collector.config import AppConfig
from astocks_collector.db import MySQLRepository
from astocks_collector.market_data import AkshareMarketData

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectResult:
    """采集任务结果摘要。"""

    task_type: str
    total_symbols: int
    success_symbols: int
    failed_symbols: int
    rows_written: int
    status: str


class StockCollector:
    """协调数据源、数据库和采集任务生命周期。"""

    def __init__(
        self,
        config: AppConfig,
        repository: MySQLRepository | None = None,
        market_data: AkshareMarketData | None = None,
    ) -> None:
        """初始化采集器依赖，便于后续替换数据源或数据库实现。"""

        self.config = config
        self.repository = repository or MySQLRepository(config)
        self.market_data = market_data or AkshareMarketData(
            daily_provider=config.daily_provider
        )

    def init_db(self) -> None:
        """初始化 MySQL 数据库和表结构。"""

        self.repository.ensure_schema()

    def sync_basic(self) -> CollectResult:
        """同步沪深京 A股股票基础信息。"""

        self.repository.ensure_schema()
        run_id = self.repository.start_run("sync_basic", "同步股票基础信息")
        try:
            records = self._fetch_basic_with_retry()
            self.repository.upsert_stock_basic(records)
            result = CollectResult(
                task_type="sync_basic",
                total_symbols=len(records),
                success_symbols=len(records),
                failed_symbols=0,
                rows_written=len(records),
                status="success",
            )
            self.repository.finish_run(
                run_id,
                result.status,
                result.total_symbols,
                result.success_symbols,
                result.failed_symbols,
                result.rows_written,
                "股票基础信息同步完成",
            )
            logger.info("股票基础信息同步完成: %s 条", len(records))
            return result
        except Exception as exc:
            self.repository.record_error("sync_basic", str(exc), run_id=run_id)
            self.repository.finish_run(run_id, "failed", message=str(exc))
            raise

    def backfill(
        self,
        start_date: str,
        end_date: str,
        symbols: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> CollectResult:
        """按指定日期范围回填历史日线行情。"""

        return self._collect_daily_range(
            task_type="backfill",
            start_date=_normalize_date_text(start_date),
            end_date=_normalize_date_text(end_date),
            symbols=symbols,
            limit=limit,
        )

    def incremental(
        self,
        days: int | None = None,
        symbols: Iterable[str] | None = None,
        limit: int | None = None,
        end_date: str | None = None,
    ) -> CollectResult:
        """按最近 N 天窗口采集每日增量行情。"""

        window_days = days or self.config.daily_window_days
        if window_days <= 0:
            raise ValueError("增量采集窗口 days 必须是正整数")

        end_dt = (
            datetime.strptime(_normalize_date_text(end_date), "%Y%m%d")
            if end_date
            else datetime.now(ZoneInfo(self.config.timezone))
        )
        start_dt = end_dt - timedelta(days=window_days)
        return self._collect_daily_range(
            task_type="incremental",
            start_date=start_dt.strftime("%Y%m%d"),
            end_date=end_dt.strftime("%Y%m%d"),
            symbols=symbols,
            limit=limit,
        )

    def _collect_daily_range(
        self,
        task_type: str,
        start_date: str,
        end_date: str,
        symbols: Iterable[str] | None,
        limit: int | None,
    ) -> CollectResult:
        """执行日线行情采集的通用流程。"""

        self.repository.ensure_schema()
        selected_symbols = self._resolve_symbols(symbols, limit)
        message = f"{task_type}: {start_date} 至 {end_date}, adjust={self.config.adjust_type}"
        run_id = self.repository.start_run(task_type, message)

        success_symbols = 0
        failed_symbols = 0
        rows_written = 0

        if self.config.max_workers > 1 and len(selected_symbols) > 1:
            return self._collect_daily_parallel(
                run_id=run_id,
                task_type=task_type,
                message=message,
                symbols=selected_symbols,
                start_date=start_date,
                end_date=end_date,
            )

        for index, symbol in enumerate(selected_symbols, start=1):
            try:
                rows = self._fetch_daily_with_retry(symbol, start_date, end_date)
                if rows:
                    self.repository.upsert_stock_daily(rows)
                    rows_written += len(rows)
                success_symbols += 1
                logger.info(
                    "[%s/%s] %s 写入 %s 行",
                    index,
                    len(selected_symbols),
                    symbol,
                    len(rows),
                )
            except Exception as exc:
                failed_symbols += 1
                logger.exception("%s 采集失败: %s", symbol, exc)
                self.repository.record_error(task_type, str(exc), run_id, symbol)

            if index < len(selected_symbols):
                time.sleep(self.config.request_interval_seconds)

        status = "success" if failed_symbols == 0 else "partial"
        result = CollectResult(
            task_type=task_type,
            total_symbols=len(selected_symbols),
            success_symbols=success_symbols,
            failed_symbols=failed_symbols,
            rows_written=rows_written,
            status=status,
        )
        self.repository.finish_run(
            run_id,
            status,
            result.total_symbols,
            result.success_symbols,
            result.failed_symbols,
            result.rows_written,
            message,
        )
        return result

    def _collect_daily_parallel(
        self,
        run_id: int,
        task_type: str,
        message: str,
        symbols: list[str],
        start_date: str,
        end_date: str,
    ) -> CollectResult:
        """使用进程池并发采集日线行情，并在主进程幂等写库。"""

        success_symbols = 0
        failed_symbols = 0
        rows_written = 0
        max_workers = min(self.config.max_workers, len(symbols))

        with self.repository.connection() as conn:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                future_to_symbol = {
                    executor.submit(
                        _fetch_daily_worker,
                        symbol,
                        start_date,
                        end_date,
                        self.config.adjust_type,
                        self.config.daily_provider,
                        self.config.max_retries,
                        self.config.request_interval_seconds,
                    ): symbol
                    for symbol in symbols
                }

                for index, future in enumerate(as_completed(future_to_symbol), start=1):
                    symbol = future_to_symbol[future]
                    try:
                        rows = future.result()
                        if rows:
                            self.repository.upsert_stock_daily(rows, conn=conn)
                            rows_written += len(rows)
                        success_symbols += 1
                        logger.info(
                            "[%s/%s] %s 写入 %s 行",
                            index,
                            len(symbols),
                            symbol,
                            len(rows),
                        )
                    except Exception as exc:
                        failed_symbols += 1
                        logger.exception("%s 采集失败: %s", symbol, exc)
                        self.repository.record_error(task_type, str(exc), run_id, symbol)

        status = "success" if failed_symbols == 0 else "partial"
        result = CollectResult(
            task_type=task_type,
            total_symbols=len(symbols),
            success_symbols=success_symbols,
            failed_symbols=failed_symbols,
            rows_written=rows_written,
            status=status,
        )
        self.repository.finish_run(
            run_id,
            status,
            result.total_symbols,
            result.success_symbols,
            result.failed_symbols,
            result.rows_written,
            message,
        )
        return result

    def _resolve_symbols(
        self, symbols: Iterable[str] | None, limit: int | None
    ) -> list[str]:
        """解析命令行股票代码，未指定时从数据库读取。"""

        if symbols:
            selected = _normalize_symbols(symbols)
            return selected[:limit] if limit else selected

        selected = self.repository.get_symbols(limit=limit)
        if selected:
            return selected

        logger.info("stock_basic 为空，先同步股票基础信息")
        self.sync_basic()
        selected = self.repository.get_symbols(limit=limit)
        if not selected:
            raise RuntimeError("没有可采集的股票代码")
        return selected

    def _fetch_daily_with_retry(
        self, symbol: str, start_date: str, end_date: str
    ) -> list[dict]:
        """带重试地获取单只股票日线行情。"""

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return self.market_data.fetch_daily(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust_type=self.config.adjust_type,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                sleep_seconds = self.config.request_interval_seconds * attempt * 2
                logger.warning(
                    "%s 第 %s 次采集失败，%.2f 秒后重试: %s",
                    symbol,
                    attempt,
                    sleep_seconds,
                    exc,
                )
                time.sleep(sleep_seconds)

        assert last_error is not None
        raise last_error

    def _fetch_basic_with_retry(self) -> list[dict]:
        """带重试地获取股票基础信息。"""

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return self.market_data.fetch_stock_basic()
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                sleep_seconds = self.config.request_interval_seconds * attempt * 2
                logger.warning(
                    "股票基础信息第 %s 次同步失败，%.2f 秒后重试: %s",
                    attempt,
                    sleep_seconds,
                    exc,
                )
                time.sleep(sleep_seconds)

        assert last_error is not None
        raise last_error


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    """规范化股票代码并去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for raw in symbols:
        symbol = "".join(ch for ch in str(raw).strip() if ch.isdigit())
        if not symbol:
            continue
        symbol = symbol[-6:].zfill(6)
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    if not result:
        raise ValueError("未提供有效股票代码")
    return result


def _normalize_date_text(value: str | None) -> str:
    """将日期文本规范化为 YYYYMMDD。"""

    if value is None:
        raise ValueError("日期不能为空")
    text = value.strip().replace("-", "")
    datetime.strptime(text, "%Y%m%d")
    return text


def _fetch_daily_worker(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust_type: str,
    daily_provider: str,
    max_retries: int,
    request_interval_seconds: float,
) -> list[dict]:
    """进程池 worker：独立初始化 AKShare 适配器并采集单只股票。"""

    market_data = AkshareMarketData(daily_provider=daily_provider)
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            rows = market_data.fetch_daily(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust_type=adjust_type,
            )
            time.sleep(request_interval_seconds)
            return rows
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(request_interval_seconds * attempt * 2)

    assert last_error is not None
    raise last_error
