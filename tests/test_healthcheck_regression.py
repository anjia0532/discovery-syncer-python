"""健康检查链路修复的回归测试（v3.0.4）。

覆盖三个线上缺陷，任何一个复发都应让本文件变红：

1. ``DiscoveryInstance.save_or_update`` 写日志时 ``json.dumps(datetime)`` 抛
   ``TypeError``，而这一步在 ``ss.add_all/commit`` **之前** —— 结果实例永远入不了库，
   ``instances`` 表恒为空，主动健康检查与「不健康实例自动下线」整体失效，
   线上表现为每 5 分钟一条 ``健康检查下线实例失败, %s , %s`` 且无堆栈。
2. ``ElasticHandler`` 用 ``record.msg``（模板原文）入库，``%s`` 参数全丢、也不写堆栈。
3. ``DingTalkHandler`` 同样不渲染 ``%s``，并且会就地改写共享的 ``record.msg``
   （污染后续 handler 的输出）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(module_name: str, rel_path: str, package: str | None = None) -> types.ModuleType:
    """按文件路径加载模块（与 conftest.load_module 等价，这里自持一份避免耦合）。"""
    spec = importlib.util.spec_from_file_location(module_name, ROOT / rel_path)
    assert spec and spec.loader, f"无法加载 {rel_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if package:
        module.__package__ = package
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_model(stub_environment):
    """把 conftest 的轻量 syncer_model 换成真实实现（含 SQLAlchemy 模型）。

    conftest 为了让 test_sync_core 免依赖，注册了一个只有 ``Instance`` 的假模块；
    这里临时换成真实模块，用例结束后还原，避免影响其它测试。
    """
    original = sys.modules.get("app.model.syncer_model")
    sys.modules.pop("app.model.syncer_model", None)
    module = _load("app.model.syncer_model", "app/model/syncer_model.py", package="app.model")
    try:
        yield module
    finally:
        if original is not None:
            sys.modules["app.model.syncer_model"] = original
        else:
            sys.modules.pop("app.model.syncer_model", None)


@pytest.fixture()
def real_logger(stub_environment):
    """加载真实 core/lib/logger.py（用独立模块名，不覆盖 conftest 的 stub）。

    用独立模块名 + 用例结束后清理，避免把真实的 ``core.lib.task_context`` 等
    永久留在 ``sys.modules`` 里，影响其它对导入顺序敏感的用例。
    """
    _load("core.lib.task_context", "core/lib/task_context.py", package="core.lib")
    module = _load("core.lib._logger_under_test", "core/lib/logger.py", package="core.lib")
    try:
        yield module
    finally:
        sys.modules.pop("core.lib._logger_under_test", None)


@pytest.fixture()
def task_syncer(real_model):
    """加载真实 app.tasks.task_syncer。

    conftest 为了让轻量用例免依赖，把 ``app.compat`` / ``app.runtime`` /
    ``app.subscription`` 注册成了空壳包（只有 __path__）。这里临时换成真实模块，
    用例结束后**精确还原**：恢复原空壳，并清掉本次新导入的子模块，避免污染其它测试。
    """
    real_packages = (
        ("app.compat", "app/compat/__init__.py"),
        ("app.subscription", "app/subscription/__init__.py"),
        ("app.runtime", "app/runtime/__init__.py"),
    )
    before = set(sys.modules)
    saved = {}
    for name, rel_path in real_packages:
        current = sys.modules.get(name)
        if current is not None and getattr(current, "__file__", None) is not None:
            continue  # 已经是真实模块，无需处理
        saved[name] = current
        sys.modules.pop(name, None)
        _load(name, rel_path, package=name)   # 包 __init__ 的 __package__ 就是包名本身

    sys.modules.pop("app.tasks.task_syncer", None)
    module = _load("app.tasks.task_syncer", "app/tasks/task_syncer.py", package="app.tasks")
    try:
        yield module
    finally:
        sys.modules.pop("app.tasks.task_syncer", None)
        for name in set(sys.modules) - before:
            if name.startswith(("app.compat", "app.subscription", "app.runtime",
                                "app.service", "core.")):
                sys.modules.pop(name, None)
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _sqlite_helper(model, tmp_path):
    """独立 sqlite 文件 + SqlaReflectHelper，避免污染仓库目录的 syncer-jobs.db。"""
    from db_libs.sqla_lib import SqlaReflectHelper
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'instances.db'}")
    model.Base.metadata.create_all(engine)
    return engine, SqlaReflectHelper(engine)


def _alive(model, *ips, port=8080):
    return [model.Instance(ip=ip, port=port, enabled=True) for ip in ips]


# ---------------------------------------------------------------------------
# 1. 根因：save_or_update 不能因为 json.dumps(datetime) 中断写库
# ---------------------------------------------------------------------------

def test_save_or_update_persists_instances(real_model, tmp_path, caplog):
    """修复前：这里抛 TypeError，随后实例一条都进不了库。"""
    tracker = real_model.DiscoveryInstance({"target_id": "t-1", "service": "svc-a"})
    _engine, helper = _sqlite_helper(real_model, tmp_path)

    with caplog.at_level(logging.INFO):
        tracker.save_or_update(_alive(real_model, "10.0.0.1", "10.0.0.2"), helper)

    rows = tracker.get_target_service_all_instance(0, helper)
    assert sorted(r.instance for r in rows) == ["10.0.0.1:8080", "10.0.0.2:8080"]

    messages = [r.getMessage() for r in caplog.records]
    added = [m for m in messages if m.startswith("新增的实例")]
    assert added, f"缺少「新增的实例」日志: {messages}"
    # create_time 是 datetime，必须被 default=str 兜住而不是让日志调用抛异常
    assert "create_time" in added[0] and "10.0.0.1:8080" in added[0]


def test_save_or_update_is_idempotent(real_model, tmp_path):
    """重复对账不能重复插行（每 10s / 每 5m 都会走这条路径）。"""
    tracker = real_model.DiscoveryInstance({"target_id": "t-2", "service": "svc-b"})
    _engine, helper = _sqlite_helper(real_model, tmp_path)
    alive = _alive(real_model, "10.0.0.1", "10.0.0.2")

    tracker.save_or_update(alive, helper)
    tracker.save_or_update(alive, helper)

    assert len(tracker.get_target_service_all_instance(0, helper)) == 2


def test_disabled_instance_is_not_tracked(real_model, tmp_path):
    """enabled=False 的实例（注册中心侧下线）不应被纳入健康检查跟踪。"""
    tracker = real_model.DiscoveryInstance({"target_id": "t-3", "service": "svc-c"})
    _engine, helper = _sqlite_helper(real_model, tmp_path)

    tracker.save_or_update(
        [real_model.Instance(ip="10.0.0.9", port=8080, enabled=False)], helper)
    assert tracker.get_target_service_all_instance(0, helper) == []


def test_unhealthy_row_survives_query_and_can_be_deleted(real_model, tmp_path):
    """健康检查依赖的「查出来 -> 删掉」链路必须真的能拿到行。"""
    tracker = real_model.DiscoveryInstance({"target_id": "t-4", "service": "svc-d"})
    _engine, helper = _sqlite_helper(real_model, tmp_path)
    tracker.save_or_update(_alive(real_model, "10.0.0.1", "10.0.0.2"), helper)

    row = [r for r in tracker.get_target_service_all_instance(0, helper)
           if r.instance == "10.0.0.1:8080"][0]
    with helper.session as ss:
        ss.execute(real_model.SQL_UPDATE_INSTANCES, {
            "id": row.id, "successes": 0, "failures": 3, "timeouts": 0,
            "status": "unhealthy", "last_time": datetime.now(),
        })
        ss.commit()

    tracked = tracker.get_target_service_all_instance(0, helper)
    assert [r.status for r in tracked if r.instance == "10.0.0.1:8080"] == ["unhealthy"]

    real_model.DiscoveryInstance({}).delete_by_instances(["10.0.0.1:8080"], helper)
    assert [r.instance for r in tracker.get_target_service_all_instance(0, helper)] == ["10.0.0.2:8080"]


# ---------------------------------------------------------------------------
# 2. _apply_healthcheck 端到端：不再吞异常、能真正下线不健康实例
# ---------------------------------------------------------------------------

class _FakeDiscovery:
    def __init__(self):
        self.modified = []

    def modify_registration(self, registration, instances):
        self.modified.append((registration.service_name, sorted(i.ip for i in instances)))


def _seed_and_mark_unhealthy(model, helper, target_id, service, bad_instance):
    """先把实例落库，再把指定实例标记为 unhealthy（模拟健康检查结果）。"""
    with helper.session as ss:
        row = [r for r in model.DiscoveryInstance(
            {"target_id": target_id, "service": service})
            .get_target_service_all_instance(0, helper) if r.instance == bad_instance][0]
        ss.execute(model.SQL_UPDATE_INSTANCES, {
            "id": row.id, "successes": 0, "failures": 3, "timeouts": 0,
            "status": "unhealthy", "last_time": datetime.now(),
        })
        ss.commit()


def test_apply_healthcheck_offlines_unhealthy_instance(
        task_syncer, real_model, tmp_path, caplog, monkeypatch):
    """3 个实例、min-hosts=1、其中 1 个 unhealthy：应过滤它、回写 nacos 并清掉跟踪行。"""
    engine, helper = _sqlite_helper(real_model, tmp_path)
    fake = _FakeDiscovery()
    monkeypatch.setattr(task_syncer, "get_discovery_client", lambda _name: fake)
    monkeypatch.setattr(task_syncer.db, "get_sqla_helper", lambda: (engine, helper))

    target = {
        "id": "t-9", "discovery": "d-1", "config": {},
        "healthcheck": {"type": "http", "uri": "/", "min-hosts": 1,
                        "healthy": {"http_statuses": [200], "successes": 1},
                        "unhealthy": {"http_statuses": [500], "failures": 3}},
    }
    instances = _alive(real_model, "10.0.0.1", "10.0.0.2", "10.0.0.3")
    tracker = real_model.DiscoveryInstance({"target_id": "t-9", "service": "svc-x"})

    with caplog.at_level(logging.INFO):
        # 第一轮：全部健康，实例应当落库（修复前这一步就抛 TypeError 并被吞掉）
        assert len(task_syncer._apply_healthcheck(target, "svc-x", instances)) == 3
        assert len(tracker.get_target_service_all_instance(0, helper)) == 3

        _seed_and_mark_unhealthy(real_model, helper, "t-9", "svc-x", "10.0.0.3:8080")

        # 第二轮：应把 unhealthy 的摘掉
        kept = task_syncer._apply_healthcheck(target, "svc-x", instances)

    assert [i.ip for i in kept] == ["10.0.0.1", "10.0.0.2"]
    assert fake.modified == [("svc-x", ["10.0.0.3"])]
    assert "10.0.0.3:8080" not in [r.instance for r in
                                   tracker.get_target_service_all_instance(0, helper)]

    # 关键回归：修复前这里必然出现「健康检查下线实例失败」WARNING
    assert not [r for r in caplog.records if "健康检查下线实例失败" in r.getMessage()]


def test_apply_healthcheck_respects_min_hosts(
        task_syncer, real_model, tmp_path, monkeypatch):
    """min-hosts 保护：跟踪行数不足以越过 skip 时不应下线任何实例。"""
    engine, helper = _sqlite_helper(real_model, tmp_path)
    fake = _FakeDiscovery()
    monkeypatch.setattr(task_syncer, "get_discovery_client", lambda _name: fake)
    monkeypatch.setattr(task_syncer.db, "get_sqla_helper", lambda: (engine, helper))

    target = {"id": "t-10", "discovery": "d-1", "config": {},
              "healthcheck": {"type": "http", "uri": "/", "min-hosts": 2,
                              "healthy": {"http_statuses": [200], "successes": 1},
                              "unhealthy": {"http_statuses": [500], "failures": 3}}}
    instances = _alive(real_model, "10.0.0.1", "10.0.0.2")
    tracker = real_model.DiscoveryInstance({"target_id": "t-10", "service": "svc-y"})

    kept = task_syncer._apply_healthcheck(target, "svc-y", instances)
    assert len(kept) == 2
    assert fake.modified == []

    # 即使 2 个都被判 unhealthy，min-hosts=2 也只允许跟踪 0 个，不会下线
    _seed_and_mark_unhealthy(real_model, helper, "t-10", "svc-y", "10.0.0.1:8080")
    _seed_and_mark_unhealthy(real_model, helper, "t-10", "svc-y", "10.0.0.2:8080")
    kept = task_syncer._apply_healthcheck(target, "svc-y", instances)
    assert fake.modified == []
    assert len(kept) == 2
    assert len(tracker.get_target_service_all_instance(0, helper)) == 2


# ---------------------------------------------------------------------------
# 3. 日志：%s 必须被渲染，异常必须有堆栈，且不得污染共享 record
# ---------------------------------------------------------------------------

class _FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def _new_es_handler(real_logger, queue):
    handler = object.__new__(real_logger.ElasticHandler)
    handler._index_prefix = "pylog-"
    return handler


def test_elastic_handler_renders_args(real_logger, monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(real_logger.ElasticHandler, "task_queue", queue)

    handler = _new_es_handler(real_logger, queue)
    record = logging.LogRecord(
        "t", logging.WARNING, "/opt/app/task_syncer.py", 247,
        "健康检查下线实例失败, 作业: %s, service: %s, err: %s",
        ("0-nacos-gw", "mall-admin", "boom"), None)
    record.task_id = "-"
    handler.emit(record)

    doc = queue.items[0]["_source"]
    assert doc["msg"] == "健康检查下线实例失败, 作业: 0-nacos-gw, service: mall-admin, err: boom"
    assert "stack_trace" not in doc


def test_elastic_handler_captures_stack_trace(real_logger, monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(real_logger.ElasticHandler, "task_queue", queue)

    handler = _new_es_handler(real_logger, queue)
    try:
        1 / 0
    except ZeroDivisionError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        "t", logging.WARNING, "/opt/app/task_syncer.py", 247, "炸了: %s", ("svc-a",), exc_info)
    record.task_id = "-"
    handler.emit(record)

    doc = queue.items[0]["_source"]
    assert doc["msg"] == "炸了: svc-a"
    assert "ZeroDivisionError" in doc["stack_trace"]
    assert "Traceback" in doc["stack_trace"]


def test_dingtalk_handler_renders_args_without_mutating_record(real_logger, monkeypatch):
    sent = {}

    class _Resp:
        text = "ok"

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None):
            sent["payload"] = json
            return _Resp()

    monkeypatch.setattr(real_logger, "DING_TALK_MSG_TEMPLATE", None)
    # 直接替换模块全局里的 requests，确保绝不打真实外网（也避免动到共享的 requests 模块）
    monkeypatch.setattr(real_logger, "requests", _FakeRequests)

    handler = object.__new__(real_logger.DingTalkHandler)
    handler._ding_talk_url = "http://example.invalid/robot"
    handler._ding_talk_secret = None
    handler.ding_talk_secret = None   # sign() 读的是无下划线属性（__init__ 里设置的）
    handler._msg_template = '{"msgtype":"markdown","markdown":{"title":"t","text":"信息: %(msg)s"}}'

    record = logging.LogRecord("t", logging.WARNING, "/x/a.py", 1, "服务 %s 异常", ("svc-a",), None)
    record.task_id = "-"
    handler._DingTalkHandler__emit(record)

    assert record.msg == "服务 %s 异常"           # 不再被就地改写
    assert sent["payload"]["markdown"]["text"] == "信息: 服务 svc-a 异常"


# ---------------------------------------------------------------------------
# 4. 时序与类型回归：写库修好后才第一次真正执行的路径
# ---------------------------------------------------------------------------

def test_check_timeout_accepts_datetime_and_str(real_model):
    """last_time 可能是 datetime（ORM）或 str（text() 原始行），两种都要能判超时。"""
    tracker = real_model.DiscoveryInstance({"target_id": "t-13", "service": "svc-v"})
    now = datetime.now()

    assert tracker.check_timeout(now - timedelta(minutes=1)) is False
    assert tracker.check_timeout(now - timedelta(minutes=15)) is True
    assert tracker.check_timeout((now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S.%f")) is True
    assert tracker.check_timeout((now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")) is False
    assert tracker.check_timeout(None) is False
    assert tracker.check_timeout("not-a-time") is False


def test_health_check_failure_marks_unhealthy_without_raising(
        real_model, tmp_path, monkeypatch, caplog):
    """探测失败时的「实例状态」日志含 datetime，不能因此抛 TypeError 把状态写入带崩。"""
    M = real_model
    _engine, helper = _sqlite_helper(M, tmp_path)
    tracker = M.DiscoveryInstance({"target_id": "t-11", "service": "svc-z"})
    tracker.save_or_update(_alive(M, "10.0.0.1"), helper)

    class _Resp:
        status_code = 500
        text = ""

    monkeypatch.setattr(M.httpx, "request", lambda **_kw: _Resp())
    healthcheck = {"type": "http", "uri": "/", "timeout-sec": 1,
                   "healthy": {"http_statuses": [200], "successes": 1},
                   "unhealthy": {"http_statuses": [500], "failures": 1, "timeouts": 1}}
    row = tracker.get_target_service_all_instance(0, helper)[0]

    with caplog.at_level(logging.INFO):
        row.health_check(healthcheck, helper)      # 修复前：json.dumps(datetime) 抛 TypeError

    after = tracker.get_target_service_all_instance(0, helper)
    assert [r.status for r in after] == ["unhealthy"]
    assert any("实例状态" in r.getMessage() for r in caplog.records)


def test_apply_healthcheck_offlines_even_if_marked_long_ago(
        task_syncer, real_model, tmp_path, monkeypatch):
    """时序回归：unhealthy 行的 last_time 已超过 60s 时，仍必须被下线。

    save_or_update 会把「unhealthy 且超过 60s」的行重置成 unknown；如果它先于
    unhealthy 读取执行，同步间隔 >60s（1m/5m）时自动下线就永远不会触发。
    """
    engine, helper = _sqlite_helper(real_model, tmp_path)
    fake = _FakeDiscovery()
    monkeypatch.setattr(task_syncer, "get_discovery_client", lambda _name: fake)
    monkeypatch.setattr(task_syncer.db, "get_sqla_helper", lambda: (engine, helper))

    target = {"id": "t-12", "discovery": "d-1", "config": {},
              "healthcheck": {"type": "http", "uri": "/", "min-hosts": 1,
                              "healthy": {"http_statuses": [200], "successes": 1},
                              "unhealthy": {"http_statuses": [500], "failures": 3}}}
    instances = _alive(real_model, "10.0.0.1", "10.0.0.2", "10.0.0.3")
    tracker = real_model.DiscoveryInstance({"target_id": "t-12", "service": "svc-w"})

    assert len(task_syncer._apply_healthcheck(target, "svc-w", instances)) == 3

    # 标记 10.0.0.3 为 unhealthy，并把 last_time 退到 5 分钟前（模拟同步间隔 > 60s）
    row = [r for r in tracker.get_target_service_all_instance(0, helper)
           if r.instance == "10.0.0.3:8080"][0]
    with helper.session as ss:
        ss.execute(real_model.SQL_UPDATE_INSTANCES, {
            "id": row.id, "successes": 0, "failures": 3, "timeouts": 0,
            "status": "unhealthy", "last_time": datetime.now() - timedelta(minutes=5),
        })
        ss.commit()

    kept = task_syncer._apply_healthcheck(target, "svc-w", instances)

    assert [i.ip for i in kept] == ["10.0.0.1", "10.0.0.2"]
    assert fake.modified == [("svc-w", ["10.0.0.3"])]


# ---------------------------------------------------------------------------
# 5. 3.0.5：作用域删除 / min-hosts 语义 / 唯一约束 / 写失败不删行
# ---------------------------------------------------------------------------

@pytest.fixture()
def real_nacos(real_model):
    """加载真实 nacos 客户端（不建连接池、不探测版本，测试里替换 _http）。"""
    module = _load("app.service.discovery.nacos", "app/service/discovery/nacos.py",
                   package="app.service.discovery")
    try:
        yield module
    finally:
        sys.modules.pop("app.service.discovery.nacos", None)


def test_delete_by_instances_is_scoped_to_target_and_service(real_model, tmp_path):
    """同一个 ip:port 出现在别的 target 下时，不能被连带删掉。"""
    _engine, helper = _sqlite_helper(real_model, tmp_path)
    a = real_model.DiscoveryInstance({"target_id": "t-a", "service": "svc-1"})
    b = real_model.DiscoveryInstance({"target_id": "t-b", "service": "svc-1"})
    a.save_or_update(_alive(real_model, "10.0.0.1"), helper)
    b.save_or_update(_alive(real_model, "10.0.0.1"), helper)

    a.delete_by_instances(["10.0.0.1:8080"], helper)

    assert a.get_target_service_all_instance(0, helper) == []
    assert [r.instance for r in b.get_target_service_all_instance(0, helper)] == ["10.0.0.1:8080"]


def test_min_hosts_counts_healthy_instances(task_syncer, real_model, tmp_path, monkeypatch):
    """min-hosts 保护的是「健康实例数」，不是 SQL OFFSET 跳过的前 N 行。"""
    engine, helper = _sqlite_helper(real_model, tmp_path)
    fake = _FakeDiscovery()
    monkeypatch.setattr(task_syncer, "get_discovery_client", lambda _name: fake)
    monkeypatch.setattr(task_syncer.db, "get_sqla_helper", lambda: (engine, helper))

    target = {"id": "t-20", "discovery": "d-1", "config": {},
              "healthcheck": {"type": "http", "uri": "/", "min-hosts": 3,
                              "healthy": {"http_statuses": [200], "successes": 1},
                              "unhealthy": {"http_statuses": [500], "failures": 3}}}
    instances = _alive(real_model, "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4")
    tracker = real_model.DiscoveryInstance({"target_id": "t-20", "service": "svc-m"})
    assert len(task_syncer._apply_healthcheck(target, "svc-m", instances)) == 4

    # 1 个不健康 -> 健康数 3 <= min-hosts 3，不许下线（旧 OFFSET 实现会摘掉它）
    _seed_and_mark_unhealthy(real_model, helper, "t-20", "svc-m", "10.0.0.4:8080")
    assert len(task_syncer._apply_healthcheck(target, "svc-m", instances)) == 4
    assert fake.modified == []
    assert "10.0.0.4:8080" in [r.instance for r in
                               tracker.get_target_service_all_instance(0, helper)]

    # 再挂一个 -> 健康数 2 <= 3，仍然不许下线
    _seed_and_mark_unhealthy(real_model, helper, "t-20", "svc-m", "10.0.0.3:8080")
    assert len(task_syncer._apply_healthcheck(target, "svc-m", instances)) == 4
    assert fake.modified == []

    # 阈值降到 1 -> 健康数 2 > 1，允许把两个 unhealthy 摘掉
    target["healthcheck"]["min-hosts"] = 1
    kept = task_syncer._apply_healthcheck(target, "svc-m", instances)
    assert [i.ip for i in kept] == ["10.0.0.1", "10.0.0.2"]
    assert fake.modified[-1][1] == ["10.0.0.3", "10.0.0.4"]


def test_unique_constraint_rejects_duplicate_row(real_model, tmp_path):
    """并发重复插入由 (target_id, service, instance) 唯一约束兜住。"""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    _engine, helper = _sqlite_helper(real_model, tmp_path)
    row = {"target_id": "t-u", "service": "svc-u", "instance": "1.1.1.1:80"}
    with helper.session as ss:
        ss.add(real_model.DiscoveryInstance({**row, "id": "id-a"}))
        ss.commit()

    with pytest.raises(SAIntegrityError):
        with helper.session as ss:
            ss.add(real_model.DiscoveryInstance({**row, "id": "id-b"}))
            ss.commit()


def test_modify_failure_keeps_tracking_row(task_syncer, real_model, tmp_path, monkeypatch, caplog):
    """nacos 下线失败时不能删跟踪行，否则下一轮又会把它当健康节点加回网关（flap）。"""
    class _FailingDiscovery(_FakeDiscovery):
        def modify_registration(self, registration, instances):
            raise RuntimeError("nacos 返回 HTTP 500")

    engine, helper = _sqlite_helper(real_model, tmp_path)
    fake = _FailingDiscovery()
    monkeypatch.setattr(task_syncer, "get_discovery_client", lambda _name: fake)
    monkeypatch.setattr(task_syncer.db, "get_sqla_helper", lambda: (engine, helper))

    target = {"id": "t-30", "discovery": "d-1", "config": {},
              "healthcheck": {"type": "http", "uri": "/", "min-hosts": 1,
                              "healthy": {"http_statuses": [200], "successes": 1},
                              "unhealthy": {"http_statuses": [500], "failures": 3}}}
    instances = _alive(real_model, "10.0.0.1", "10.0.0.2", "10.0.0.3")
    tracker = real_model.DiscoveryInstance({"target_id": "t-30", "service": "svc-f"})
    assert len(task_syncer._apply_healthcheck(target, "svc-f", instances)) == 3
    _seed_and_mark_unhealthy(real_model, helper, "t-30", "svc-f", "10.0.0.3:8080")

    with caplog.at_level(logging.WARNING):
        kept = task_syncer._apply_healthcheck(target, "svc-f", instances)

    # 网关侧照常摘除（返回已过滤），但跟踪行必须保留，下一轮继续重试下线
    assert [i.ip for i in kept] == ["10.0.0.1", "10.0.0.2"]
    assert "10.0.0.3:8080" in [r.instance for r in
                               tracker.get_target_service_all_instance(0, helper)]
    assert any("健康检查下线实例失败" in r.getMessage() for r in caplog.records)


def test_nacos_write_failures_raise(real_nacos, real_model):
    """HTTP 4xx 与响应体 false 都必须显式失败，不能被当成下线成功。"""
    client = real_nacos.Nacos.__new__(real_nacos.Nacos)
    client._config = types.SimpleNamespace(host="http://nacos.example",
                                           prefix="/nacos/v1/", declared_version="")
    client._capability = types.SimpleNamespace(major=1, api_prefix="/nacos/v1/",
                                               default_namespace="")
    client._version_evidence = ""

    class _Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

    client._http = types.SimpleNamespace(
        request=lambda *_a, **_kw: _Resp(400, "namespace not found"))
    with pytest.raises(RuntimeError):
        client.nacos_execute("PUT", "ns/instance", {"ip": "1.1.1.1"}, raise_on_error=True)

    # 200 + false：nacos 用 false 表示更新失败
    client._http = types.SimpleNamespace(request=lambda *_a, **_kw: _Resp(200, "false"))
    instance = real_model.Instance(ip="1.1.1.1", port=80, enabled=False)
    instance.change = True
    with pytest.raises(RuntimeError):
        client.modify_registration(real_model.Registration(service_name="svc", ext_data={}),
                                   [instance])

    # 200 + true：正常返回
    client._http = types.SimpleNamespace(request=lambda *_a, **_kw: _Resp(200, "true"))
    client.modify_registration(real_model.Registration(service_name="svc", ext_data={}),
                               [instance])


def test_save_or_update_tolerates_duplicate_insert(real_model, tmp_path, caplog):
    """并发重复插入命中唯一约束时，save_or_update 不能抛（回滚忽略即可）。"""
    _engine, helper = _sqlite_helper(real_model, tmp_path)
    # 用固定主键在另一个 service 下先占位，模拟「另一线程已经写入同一行」
    real_model.DiscoveryInstance(
        {"id": "dup-id", "target_id": "t-d", "service": "svc-d1"}
    ).save_or_update(_alive(real_model, "10.0.0.1"), helper)

    collided = real_model.DiscoveryInstance(
        {"id": "dup-id", "target_id": "t-d", "service": "svc-d2"})
    with caplog.at_level(logging.INFO):
        collided.save_or_update(_alive(real_model, "10.0.0.1"), helper)   # 不应抛

    assert any("并发重复插入已忽略" in r.getMessage() for r in caplog.records)


def test_stale_row_is_pruned_but_present_unhealthy_row_is_kept(
        real_model, tmp_path, caplog):
    """清理只针对「已从注册中心消失」的行；仍然存在但不健康的行必须保留。"""
    M = real_model
    _engine, helper = _sqlite_helper(M, tmp_path)
    tracker = M.DiscoveryInstance({"target_id": "t-s", "service": "svc-s"})
    tracker.save_or_update(_alive(M, "10.0.0.1", "10.0.0.2"), helper)

    # 10.0.0.2 从注册中心消失（只剩 10.0.0.1）-> 只清理它
    tracker.save_or_update(_alive(M, "10.0.0.1"), helper)
    assert [r.instance for r in tracker.get_target_service_all_instance(0, helper)] == ["10.0.0.1:8080"]

    # 再把 10.0.0.1 标记为 unhealthy 且「超过 10 分钟」：health_check 不能再删它
    row = tracker.get_target_service_all_instance(0, helper)[0]
    with helper.session as ss:
        ss.execute(M.SQL_UPDATE_INSTANCES, {
            "id": row.id, "successes": 0, "failures": 3, "timeouts": 0,
            "status": "unhealthy", "last_time": datetime.now() - timedelta(minutes=15),
        })
        ss.commit()

    unhealthy_row = tracker.get_target_service_all_instance(0, helper)[0]
    with caplog.at_level(logging.WARNING):
        unhealthy_row.health_check({"type": "http", "uri": "/"}, helper)

    assert [r.instance for r in tracker.get_target_service_all_instance(0, helper)] == ["10.0.0.1:8080"]
    assert any("跟踪行保留" in r.getMessage() for r in caplog.records)


def test_eureka_write_failure_raises(real_model, monkeypatch):
    """eureka 写失败（4xx/5xx）也要显式抛错，否则下线静默失败后照样删跟踪行。"""
    eureka = _load("app.service.discovery.eureka", "app/service/discovery/eureka.py",
                   package="app.service.discovery")
    client = eureka.Eureka.__new__(eureka.Eureka)
    client._config = types.SimpleNamespace(host="http://eureka.example", prefix="/eureka/v2/")

    class _Resp:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(eureka.httpx, "request", lambda *_a, **_kw: _Resp())
    instance = real_model.Instance(ip="1.1.1.1", port=80, enabled=False)
    instance.change = True
    instance.ext = {"instanceId": "i-1"}
    with pytest.raises(RuntimeError):
        client.modify_registration(
            real_model.Registration(service_name="svc", ext_data={}), [instance])


def test_nacos_v3_result_wrapper_failure_raises(real_nacos, real_model):
    """nacos 3.x 返回 HTTP 200 + Result 包装体（code!=0）也算失败。"""
    client = real_nacos.Nacos.__new__(real_nacos.Nacos)
    client._config = types.SimpleNamespace(host="http://nacos.example",
                                          prefix="/nacos/v3/admin/", declared_version="3")
    client._capability = types.SimpleNamespace(major=3, api_prefix="/nacos/v3/admin/",
                                               default_namespace="public")
    client._version_evidence = "test"

    class _Resp:
        status_code = 200
        text = '{"code":500,"message":"namespace not found","data":null}'

    client._http = types.SimpleNamespace(request=lambda *_a, **_kw: _Resp())
    instance = real_model.Instance(ip="1.1.1.1", port=80, enabled=False)
    instance.change = True
    with pytest.raises(RuntimeError):
        client.modify_registration(real_model.Registration(service_name="svc", ext_data={}),
                                   [instance])


def test_reload_build_step_keeps_rows_and_ensures_unique_index(real_model, tmp_path):
    """reload 的建表步骤不能抹掉 instances（旧实现 drop_all 会把健康状态一起删掉）。"""
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    M = real_model
    _engine, helper = _sqlite_helper(M, tmp_path)
    tracker = M.DiscoveryInstance({"target_id": "t-c", "service": "svc-c"})
    tracker.save_or_update(_alive(M, "10.0.0.1"), helper)

    # 模拟 clear_client() 里的两步（Jobs 建表 + 清空），以及 instances 的建表
    M.Jobs.create_table_if_not_exists(helper)
    M.DiscoveryInstance.create_table_if_not_exists(helper)

    assert [r.instance for r in tracker.get_target_service_all_instance(0, helper)] == ["10.0.0.1:8080"]

    # 补上的唯一索引必须生效
    with pytest.raises(SAIntegrityError):
        with helper.session as ss:
            ss.add(M.DiscoveryInstance({"id": "dup-row", "target_id": "t-c",
                                        "service": "svc-c", "instance": "10.0.0.1:8080"}))
            ss.commit()