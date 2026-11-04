"""定时表达式解析（从 task_syncer.time_parser 抽出，保持旧配置 100% 兼容）。

订阅化之后 cron 只用于「兜底全量对账」与健康检查，不再是取差异的主路径。
本模块为纯函数，不依赖 funboost / apscheduler，便于单元测试。
"""

from __future__ import annotations

import datetime
import re
from typing import Optional, Tuple

__all__ = ["time_parser", "cron_kwargs"]

# 秒 分 时 日 月 周, next_run_time
Parsed = Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str],
               Optional[datetime.datetime]]


def time_parser(fetch_interval: str = "") -> Parsed:
    """解析 crontab / ``@every`` 表达式，兼容旧 ``fetch-interval`` 全部写法。

    支持：
    * 5 位（分 时 日 月 周）自动补秒为 ``*``
    * 6 位（秒 分 时 日 月 周）
    * ``@yearly/@annually/@monthly/@weekly/@daily/@midnight/@hourly``
    * ``@reboot``（启动时执行一次）
    * ``@every 10s`` / ``@every 5m`` / ``@every 1h``

    Args:
        fetch_interval: 配置里的定时表达式。

    Returns:
        ``(second, minute, hour, day, month, day_of_week, next_run_time)``；
        ``@reboot`` 时前六项为 ``None``，``next_run_time`` 为当前时间。

    Raises:
        AssertionError: 表达式为空或格式非法。
    """
    assert len(fetch_interval) > 0, "作业表达式不能为空"
    values = fetch_interval.split()
    next_run_time: Optional[datetime.datetime] = None
    if "@" in fetch_interval:
        match values[0]:
            case "@yearly" | "@annually":
                fetch_interval = "0 0 0 1 1 *"
            case "@monthly":
                fetch_interval = "0 0 0 1 * *"
            case "@weekly":
                fetch_interval = "0 0 0 * * 0"
            case "@daily" | "@midnight":
                fetch_interval = "0 0 0 * * *"
            case "@hourly":
                fetch_interval = "0 0 * * * *"
            case "@reboot":
                next_run_time = datetime.datetime.now()
            case "@every":
                matches = re.findall(r"(\d+)([a-zA-Z]+)", values[1])
                # 兼容 1h/1hour/1hours/1min/1minute/1minutes/1sec/1second/1seconds
                times = [(int(num), unit.lower()[0]) for num, unit in matches]
                assert len(times) == 1, f"fetch_interval格式错误: {fetch_interval}"
                match times[0][1]:
                    case "h":
                        fetch_interval = f"0 0 */{times[0][0]} * * *"
                    case "m":
                        fetch_interval = f"0 */{times[0][0]} * * * *"
                    case "s":
                        fetch_interval = f"*/{times[0][0]} * * * * *"
    if next_run_time:
        return None, None, None, None, None, None, next_run_time

    values = fetch_interval.split()
    if len(values) == 5:
        fetch_interval = f"* {fetch_interval}"
        values = fetch_interval.split()
    assert len(values) == 6, f"fetch_interval格式错误: {fetch_interval}"
    return values[0], values[1], values[2], values[3], values[4], values[5], None


def cron_kwargs(fetch_interval: str) -> dict:
    """把表达式转成 ``CronTrigger(**kwargs)`` 或 ``next_run_time`` 参数字典。

    Args:
        fetch_interval: 定时表达式。

    Returns:
        ``{"trigger": CronTrigger(...)}`` 或 ``{"next_run_time": datetime}`` 所需的参数字典。
        调用方自行 import CronTrigger，保持本模块零第三方依赖。
    """
    second, minute, hour, day, month, day_of_week, next_run_time = time_parser(fetch_interval)
    if next_run_time is not None:
        return {"next_run_time": next_run_time}
    return {"trigger_fields": {
        "second": second, "minute": minute, "hour": hour,
        "day": day, "month": month, "day_of_week": day_of_week,
    }}
