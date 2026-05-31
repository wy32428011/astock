from datetime import datetime, timedelta

from astocks_collector.t1_task_manager import next_due_time, normalize_interval_seconds


def test_normalize_interval_seconds_keeps_reasonable_value() -> None:
    """T+1 任务间隔在合理范围内时保持原值。"""
    assert normalize_interval_seconds(30) == 30


def test_normalize_interval_seconds_rejects_invalid_value() -> None:
    """T+1 任务间隔必须是正整数。"""
    try:
        normalize_interval_seconds(0)
    except ValueError:
        return
    raise AssertionError("间隔为 0 时必须抛出 ValueError")


def test_next_due_time_uses_interval_seconds() -> None:
    """下一次到期时间由当前时间和间隔秒数决定。"""
    now = datetime(2026, 5, 30, 10, 0, 0)

    assert next_due_time(now, 60) == now + timedelta(seconds=60)
