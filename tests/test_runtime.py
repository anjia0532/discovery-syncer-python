"""本地运行时单测：任务上下文、TaskBus 重试/统计、本地调度器编排。"""

import logging
from datetime import datetime

import pytest


def _record(msg="hello") -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, (), None)


# ---------------------------------------------------------------------------
# 任务上下文
# ---------------------------------------------------------------------------
def test_task_context_bind_and_reset(task_context):
    assert task_context.current_task() is None
    ctx = task_context.TaskContext(task_id="abc", submit_time=1.0)
    token = task_context.bind_task(ctx)
    assert task_context.current_task().task_id == "abc"
    assert task_context.current_task().submit_time == 1.0
    task_context.reset_task(token)
    assert task_context.current_task() is None


def test_task_id_filter_injects_record(task_context):
    """去掉 funboost TaskIdLogger 后，%(task_id)s 与 record.task_id 必须仍可用。"""
    flt = task_context.TaskIdFilter()
    rec = _record()
    assert flt.filter(rec) is True
    assert rec.task_id == "-"  # 无上下文时的默认值

    token = task_context.bind_task(task_context.TaskContext("tid-1", 0.0))
    try:
        rec2 = _record()
        assert flt.filter(rec2) is True
        assert rec2.task_id == "tid-1"
    finally:
        task_context.reset_task(token)


def test_new_task_id_is_unique(task_context):
    assert task_context.new_task_id() != task_context.new_task_id()


# ---------------------------------------------------------------------------
# TaskBus
# ---------------------------------------------------------------------------
def _bus(runtime_taskbus, **overrides):
    params = dict(max_workers=2, max_retries=0, retry_interval=0.0, sleep=lambda _s: None)
    params.update(overrides)
    return runtime_taskbus.TaskBus(**params)


def test_taskbus_executes_and_tracks(runtime_taskbus):
    calls = []
    bus = _bus(runtime_taskbus)
    bus.submit(lambda value: calls.append(value), value=42).result(timeout=3)
    assert calls == [42]
    stats = bus.stats
    assert stats["submitted"] == 1 and stats["succeeded"] == 1
    assert stats["failed"] == 0 and stats["retries"] == 0
    bus.shutdown(wait=True)


def test_taskbus_kwargs_are_forwarded(runtime_taskbus):
    seen = {}
    bus = _bus(runtime_taskbus)
    bus.submit(lambda a, b=0: seen.update(a=a, b=b), a=1, b=2).result(timeout=3)
    assert seen == {"a": 1, "b": 2}
    bus.shutdown(wait=True)


def test_taskbus_retries_then_succeeds(runtime_taskbus):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("boom")

    bus = _bus(runtime_taskbus, max_retries=2)
    bus.submit(flaky).result(timeout=3)
    assert len(attempts) == 3
    stats = bus.stats
    assert stats["retries"] == 2 and stats["succeeded"] == 1 and stats["failed"] == 0
    bus.shutdown(wait=True)


def test_taskbus_final_failure_is_counted(runtime_taskbus):
    def always_fail():
        raise RuntimeError("nacos down")

    bus = _bus(runtime_taskbus, max_retries=1)
    bus.submit(always_fail).result(timeout=3)
    stats = bus.stats
    assert stats["failed"] == 1 and stats["succeeded"] == 0 and stats["retries"] == 1
    bus.shutdown(wait=True)


def test_taskbus_context_propagates_to_worker(runtime_taskbus, task_context):
    seen = {}

    def fn():
        ctx = task_context.current_task()
        seen["id"] = ctx.task_id if ctx else None
        seen["has_submit_time"] = bool(ctx and ctx.submit_time)

    bus = _bus(runtime_taskbus, max_workers=1)
    bus.submit(fn, task_id="parent-1").result(timeout=3)
    assert seen["id"] == "parent-1" and seen["has_submit_time"] is True
    # 工作线程里绑定/还原后，主协程上下文不受污染
    assert task_context.current_task() is None
    bus.shutdown(wait=True)


def test_taskbus_shutdown_rejects_new_tasks(runtime_taskbus):
    bus = _bus(runtime_taskbus, max_workers=1)
    bus.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        bus.submit(lambda: None)


# ---------------------------------------------------------------------------
# LocalScheduler（纯逻辑，不启动 APScheduler）
# ---------------------------------------------------------------------------
def _cron():
    return {"second": "*/10", "minute": "*", "hour": "*",
            "day": "*", "month": "*", "day_of_week": "*"}


def test_local_scheduler_add_replace_remove(runtime_scheduler):
    sch = runtime_scheduler.LocalScheduler()

    def job(**_kwargs):
        return None

    sch.add_cron(job_id="reconcile-a", func=job, trigger_fields=_cron(), kwargs={"target": {"id": "a"}})
    sch.add_cron(job_id="health-b", func=job, next_run_time=datetime.now(), kwargs={"target": {"id": "b"}})
    assert sch.job_ids == ["health-b", "reconcile-a"]
    assert sch.started is False

    # 同 id 覆盖，不产生重复作业
    sch.add_cron(job_id="reconcile-a", func=job, trigger_fields=_cron(), kwargs={"target": {"id": "a2"}})
    assert sch.job_ids == ["health-b", "reconcile-a"]
    assert sch._jobs["reconcile-a"]["kwargs"]["target"]["id"] == "a2"

    sch.remove_all()
    assert sch.job_ids == []


def test_local_scheduler_no_apscheduler_import_at_load(runtime_scheduler):
    """未安装 APScheduler 时也能完成编排（start 才延迟导入）。"""
    import sys

    assert "apscheduler" not in sys.modules
    sch = runtime_scheduler.LocalScheduler()
    sch.add_cron(job_id="x", func=lambda **_kw: None, trigger_fields=_cron(), kwargs={})
    assert sch.job_ids == ["x"]
