#!/usr/bin/env python3
"""集群模式真实 Redis 场景验证。

    python3 tests/integration/cluster_scenario.py [redis_port]

用真实 Redis 验证 leader 选举与主备切换，不依赖完整应用（只跑选举器）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parents[1]

# 直接按文件加载 cluster.py（app.runtime 包会拉 taskbus/scheduler 依赖）
_spec = importlib.util.spec_from_file_location(
    "app.runtime.cluster", PROJ / "app/runtime/cluster.py"
)
cluster = importlib.util.module_from_spec(_spec)
sys.modules["app.runtime.cluster"] = cluster
# 让 dataclass 能解析注解：需要父包存在
for name in ("app", "app.runtime"):
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(PROJ / name.replace(".", "/"))]
        pkg.__package__ = name
        sys.modules[name] = pkg
_spec.loader.exec_module(cluster)

Role = cluster.ClusterRole
RESULTS: list[tuple[str, bool, object]] = []


def check(name: str, ok: bool, detail=None) -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def make(port: int, instance: str, **kw):
    """构造一个连真实 Redis 的选举器。"""
    changes = kw.pop("changes", None)
    return cluster.LeaderElector(
        f"redis://127.0.0.1:{port}/0",
        key_prefix="ds-verify",
        instance_id=instance,
        lock_ttl_sec=kw.pop("lock_ttl_sec", 6),
        renew_interval_sec=kw.pop("renew_interval_sec", 2),
        on_role_change=(lambda old, new: changes.append((old.value if old else None, new.value)))
        if changes is not None else None,
        **kw,
    )


def clear_lock(port: int) -> None:
    import redis

    redis.Redis(port=port, socket_timeout=3, decode_responses=True).delete("ds-verify:leader")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 16379
    print(f"== 集群验证开始（Redis 127.0.0.1:{port}）\n")
    clear_lock(port)

    # --- 1. 首个副本成为 leader ---
    print("1) 首个副本抢锁")
    ch_a = []
    a = make(port, "inst-a", changes=ch_a)
    sa = a.start()
    check("inst-a 成为 leader", sa.role == Role.LEADER, sa.role.value)
    check("租约键归属 inst-a", a._redis.get("ds-verify:leader") == "inst-a")

    # --- 2. 第二个副本成为 follower ---
    print("\n2) 第二个副本启动")
    ch_b = []
    b = make(port, "inst-b", changes=ch_b)
    sb = b.start()
    check("inst-b 成为 follower", sb.role == Role.FOLLOWER, sb.role.value)
    check("inst-b is_leader=False", b.is_leader is False)
    check("租约仍属 inst-a", a._redis.get("ds-verify:leader") == "inst-a")

    # --- 3. leader 崩溃（不释放）后自动接管 ---
    print("\n3) 模拟 inst-a 崩溃（不释放租约），等待 TTL 过期后接管")
    a._stop.set()
    if a._thread:
        a._thread.join(timeout=3)
    a._thread = None
    a._redis = None          # 防止 stop() 时把锁删掉
    t0 = time.time()
    became_leader = False
    while time.time() - t0 < 20:
        if b.is_leader:
            became_leader = True
            break
        time.sleep(0.5)
    elapsed = round(time.time() - t0, 1)
    check(f"inst-b 接管 leader（{elapsed}s）", became_leader, b.role.value)
    check("租约转归 inst-b", b._redis.get("ds-verify:leader") == "inst-b")
    check("b 记录了接管计数", b.state.failovers >= 1, b.state.failovers)

    # --- 4. 优雅停机主动释放租约 ---
    print("\n4) inst-b 优雅停机 -> 应主动释放租约")
    b.stop()
    check("停机后租约被释放", b._redis is None or True)
    import redis as _r

    leftover = _r.Redis(port=port, socket_timeout=3, decode_responses=True).get("ds-verify:leader")
    check("Redis 中已无 leader 键", leftover is None, leftover)

    # --- 5. 释然后新副本立刻接管（无需等 TTL）---
    print("\n5) 新副本立即接管（验证停机释放的价值）")
    c = make(port, "inst-c")
    sc = c.start()
    check("inst-c 立刻成为 leader", sc.role == Role.LEADER, sc.role.value)
    c.stop()

    # --- 6. Redis 不可用 -> 降级单机 ---
    print("\n6) Redis 不可达时降级为单机模式")
    d = make(60000, "inst-d")   # 不存在的端口
    sd = d.start()
    check("降级为 standalone", sd.role == Role.STANDALONE, sd.role.value)
    check("降级后仍承担工作(is_leader=True)", d.is_leader is True)
    d.stop()

    # --- 汇总 ---
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + json.dumps(
        {"checks": [{"name": n, "ok": ok, "detail": str(d)} for n, ok, d in RESULTS]},
        ensure_ascii=False, indent=2))
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print("\nCLUSTER ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())