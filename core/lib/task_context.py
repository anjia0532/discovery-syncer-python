"""任务上下文：替代 ``funboost_current_task`` 与 ``TaskIdLogger``。

放在 ``core/lib`` 而非 ``app/runtime`` 是为了让 ``core.lib.logger`` 可以用相对
import 引用，避免 ``core`` -> ``app`` -> ``core`` 的循环依赖。
本模块只依赖标准库。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "TaskContext", "new_task_id", "current_task", "bind_task", "reset_task", "TaskIdFilter",
]


@dataclass(frozen=True)
class TaskContext:
    """一次任务执行的上下文。

    Attributes:
        task_id: 任务唯一 ID，用于串联父子任务与日志。
        submit_time: 任务提交时间（Unix 秒），替代 funboost 的 publish_time。
    """

    task_id: str
    submit_time: float


_CTX: ContextVar[Optional[TaskContext]] = ContextVar("syncer_task_context", default=None)


def new_task_id() -> str:
    """生成任务 ID。"""
    return uuid.uuid4().hex


def current_task() -> Optional[TaskContext]:
    """取当前任务上下文；不在任务线程中时返回 ``None``。"""
    return _CTX.get()


def bind_task(context: TaskContext) -> Token:
    """绑定上下文，返回用于还原的 token。"""
    return _CTX.set(context)


def reset_task(token: Token) -> None:
    """还原上下文。"""
    _CTX.reset(token)


class TaskIdFilter(logging.Filter):
    """把 ``task_id`` 注入 ``LogRecord``。

    历史背景：``core/lib/logger.py`` 的 ``DingTalkHandler`` 使用 ``%(task_id)s``，
    ``ElasticHandler`` 直接读 ``record.task_id``。原先靠 funboost 的
    ``TaskIdLogger`` 注入，去掉 funboost 后必须由本 Filter 补齐，
    否则这两个 handler 会因缺少字段抛异常。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _CTX.get()
        record.task_id = context.task_id if context else "-"
        return True
