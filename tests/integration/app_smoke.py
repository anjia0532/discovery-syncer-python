#!/usr/bin/env python3
"""去 funboost / Redis 后的端到端冒烟：真实启动应用，验证订阅与同步全链路。

    python3 tests/integration/app_smoke.py

覆盖（真实 uvicorn + FastAPI + 本地 TaskBus/APScheduler）：
* 应用能真实启动，不再依赖 funboost / Redis；
* `/health`、`/-/versions`、`/-/subscriptions` 三个接口；
* 快照 diff 轮询产出事件 -> 本地任务总线 -> 网关 upstream 被更新；
* `POST /-/notify` 入站回调触发同步；
* 任务失败时不刷新心跳，`/health` 可感知。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

HERE = Path(__file__).resolve().parent
PROJ = HERE.parents[1]

NACOS_PORT = 18849
APISIX_PORT = 39181
APP_PORT = 9797
API_KEY = "Abcdef1234567890!@#Abcdef1234567890"
# 所有访问应用自身的请求都必须带 SYNCER-API-KEY（SyncerApiKeyMiddleware 强制）
APP_HEADERS = {"SYNCER-API-KEY": API_KEY}

STATE = {
    "nacos": {"svc-a": [{"ip": "10.0.0.1", "port": 8080, "weight": 1, "enabled": True}]},
    "apisix": {},
}


class Handler(BaseHTTPRequestHandler):
    """极简 Nacos v1 + APISIX v3 mock。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        self._route("GET")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def _route(self, method: str) -> None:
        port = self.server.server_port
        if port == NACOS_PORT:
            self._nacos(method)
        elif port == APISIX_PORT:
            self._apisix(method)
        else:
            self._send(404, {})

    def _nacos(self, method: str) -> None:
        path = self.path
        if "console/server/state" in path:
            return self._send(200, {"version": "1.4.6"})
        # 注意：必须精确匹配 v1 前缀。用 "ns/instance/list" in path 这类宽匹配
        # 会把 /nacos/v3/client/ns/instance/list 也吞掉，导致版本探测误判成 3.x。
        if path.startswith("/nacos/v1/ns/catalog/services"):
            return self._send(200, {
                "count": len(STATE["nacos"]),
                "list": [{"serviceName": name, "clusterMap": {"DEFAULT": {"hosts": ins}}}
                         for name, ins in STATE["nacos"].items()],
            })
        if path.startswith("/nacos/v1/ns/catalog/instances"):
            name = parse_qs(urlparse(path).query).get("serviceName", [""])[0]
            return self._send(200, {"list": STATE["nacos"].get(name, []),
                                    "groupName": "DEFAULT_GROUP"})
        if path.startswith("/nacos/v1/ns/instance/list"):
            return self._send(200, {"hosts": []})
        self._send(404, {})

    def _apisix(self, method: str) -> None:
        clean = self.path.split("?")[0]
        if method == "GET" and clean == "/apisix/admin/upstreams":
            return self._send(200, {
                "list": [{"key": f"/apisix/upstreams/{k}", "value": v}
                         for k, v in STATE["apisix"].items()],
                "total": len(STATE["apisix"]),
            })
        match = re.match(r"^/apisix/admin/upstreams/([^/]+)$", clean)
        if match:
            name = match.group(1)
            if method == "GET":
                if name in STATE["apisix"]:
                    return self._send(200, {"key": f"/apisix/upstreams/{name}",
                                            "value": STATE["apisix"][name]})
                return self._send(404, {"error_msg": "not found"})
            if method == "PUT":
                body = self._body()
                STATE["apisix"][name] = body
                return self._send(200, {"key": f"/apisix/upstreams/{name}", "value": body})
        match = re.match(r"^/apisix/admin/upstreams/([^/]+)/nodes$", clean)
        if match and method == "PATCH":
            name = match.group(1)
            STATE["apisix"].setdefault(name, {"name": name})["nodes"] = self._body()
            return self._send(200, {"key": f"/apisix/upstreams/{name}",
                                    "value": STATE["apisix"][name]})
        self._send(404, {})


def write_config() -> Path:
    """生成指向本地 mock 的测试配置。"""
    cfg = HERE / "app-smoke-config.yaml"
    cfg.write_text(f"""
common:
  syncer-api-key: {API_KEY}
discovery-servers:
  nacos1:
    type: nacos
    weight: 100
    prefix: /nacos/v1/
    host: "http://127.0.0.1:{NACOS_PORT}"
gateway-servers:
  apisix1:
    type: apisix
    admin-url: http://127.0.0.1:{APISIX_PORT}
    prefix: /apisix/admin/
    config:
      X-API-KEY: edd1c9f034335f136f87ad84b625c8f1
      version: v3
targets:
  -   discovery: nacos1
      gateway: apisix1
      enabled: true
      fetch-interval: "@every 10s"
      maximum-interval-sec: 60
      upstream-prefix: smoke
      name: smoke
      exclude-service: []
      config:
          groupName: DEFAULT_GROUP
          namespaceId: ""
          template: |
              {{"name": "$name", "nodes": $nodes, "type": "roundrobin"}}
      subscribe:
          enabled: true
          mode: poll
          refresh-interval: "@every 3s"
          reconcile-interval: "@every 1m"
          webhook: true
          fallback: true
""", encoding="utf-8")
    return cfg


def start_mocks():
    servers = []
    for port in (NACOS_PORT, APISIX_PORT):
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, name=f"smoke-{port}", daemon=True).start()
        servers.append(server)
    return servers


def start_app(cfg: Path, log_path: Path):
    """启动真实应用（uvicorn 单进程），日志写文件避免 PIPE 阻塞。

    刻意不走 main.py：cfg/dev/uvicorn.json 带了 workers=1 +
    worker_class=UvicornWorker，uvicorn.run() 会改走 multiprocessing，
    fork 之后 startup 阶段（APScheduler/订阅装配）会卡死。
    这里直接单进程 uvicorn.run，并手动构造 sys.argv，
    让 app 模块 startup 里的 argparse 能取到 --config。
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJ)
    env["PYTHONUNBUFFERED"] = "1"
    # core/lib/cfg.py:load_env() 在 PROJECT_ENV 缺失时会 sys.exit 终止进程，
    # 必须显式指定（生产由部署环境注入）。
    env.setdefault("PROJECT_ENV", "dev")
    bootstrap = (
        f"import sys; sys.argv = ['main.py', '-c', {str(cfg)!r}]; "
        f"import uvicorn; "
        f"uvicorn.run('app:APP', host='127.0.0.1', port={APP_PORT}, log_config=None)"
    )
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "-c", bootstrap],
                                cwd=str(PROJ), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    return proc


def wait_ready(timeout: float = 90.0) -> dict:
    """等待 /health 可用。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{APP_PORT}/health", headers=APP_HEADERS, timeout=2)
            if resp.status_code == 200:
                return resp.json()
            last = f"status={resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"应用未在 {timeout}s 内就绪: {last}")


def upstream_nodes() -> list | None:
    """读取网关里 smoke-svc-a 的 nodes。"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{APISIX_PORT}/apisix/admin/upstreams/smoke-svc-a", timeout=3)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    value = resp.json().get("value", {})
    return value.get("nodes")


def wait_for_nodes(count: int, timeout: float = 30.0) -> list | None:
    """等待网关 nodes 达到指定数量。"""
    deadline = time.time() + timeout
    nodes = None
    while time.time() < deadline:
        nodes = upstream_nodes()
        if nodes and len(nodes) == count:
            return nodes
        time.sleep(1)
    return nodes


def read_proc_status(pid: int) -> dict:
    """读取 /proc/<pid>/status 的内存与线程数（Linux 原生，无需 psutil）。"""
    info = {}
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return info
    for line in text.splitlines():
        if line.startswith(("VmRSS:", "VmSize:", "VmPeak:", "Threads:")):
            key, _, value = line.partition(":")
            info[key.strip()] = value.strip()
    return info


def _kb(value) -> int:
    """把 '123456 kB' 解析成整数 KB。"""
    try:
        return int(str(value).split()[0])
    except (IndexError, ValueError):
        return 0


def stress_and_sample(base: str, pid: int, events: int = 60, reloads: int = 5) -> list:
    """反复触发事件与 reload，采样内存/线程，用于暴露泄漏。

    事件路径会经过 EventBus 去抖 -> TaskBus 线程池 -> 网关写入；
    reload 路径会 stop_all + rebuild 订阅管理器与 EventBus 线程，
    是最容易累积线程/回调的地方。
    """
    samples = []
    for index in range(events):
        try:
            httpx.post(f"{base}/-/notify?discovery=nacos1",
                       json={"serviceName": "svc-a"}, headers=APP_HEADERS, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if index % 10 == 0:
            samples.append({"phase": "event", "i": index, **read_proc_status(pid)})
    for index in range(reloads):
        try:
            httpx.get(f"{base}/-/reload", headers=APP_HEADERS, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        samples.append({"phase": "reload", "i": index, **read_proc_status(pid)})
    return samples


def analyse_memory(samples: list) -> dict:
    """判断 RSS 与线程数是否呈持续增长趋势。"""
    rss = [_kb(s.get("VmRSS")) for s in samples if s.get("VmRSS")]
    threads = [int(s.get("Threads", 0) or 0) for s in samples if s.get("Threads")]
    head_avg = sum(rss[:3]) / len(rss[:3]) if rss[:3] else 0
    tail_avg = sum(rss[-3:]) / len(rss[-3:]) if rss[-3:] else 0
    growth_pct = 0.0 if head_avg == 0 else (tail_avg - head_avg) / head_avg * 100
    return {
        "rss_kb_head_avg": round(head_avg),
        "rss_kb_tail_avg": round(tail_avg),
        "rss_kb_peak": max(rss) if rss else 0,
        "growth_pct": round(growth_pct, 1),
        "threads_head": threads[0] if threads else 0,
        "threads_tail": threads[-1] if threads else 0,
        "thread_growth": (threads[-1] - threads[0]) if threads else 0,
        "sample_count": len(samples),
    }


def main() -> int:
    results = []
    servers = start_mocks()
    cfg = write_config()
    log_path = Path("/tmp/app-smoke.log")
    proc = start_app(cfg, log_path)
    try:
        # 1) 应用真实启动
        health = wait_ready()
        results.append(("app_started", health.get("status") == "UP", health))

        # 2) 版本探测
        versions = httpx.get(f"http://127.0.0.1:{APP_PORT}/-/versions", headers=APP_HEADERS, timeout=5).json()
        nacos_ok = versions.get("discovery", {}).get("nacos1", {}).get("version") == "nacos-1.x"
        apisix_ok = versions.get("gateway", {}).get("apisix1", {}).get("version") == "apisix-3.x"
        results.append(("version_detection", nacos_ok and apisix_ok, versions))

        # 3) 订阅状态与本地运行时统计
        subs = httpx.get(f"http://127.0.0.1:{APP_PORT}/-/subscriptions", headers=APP_HEADERS, timeout=5).json()
        runtime = subs.get("_runtime", {})
        has_taskbus = "taskbus" in runtime and "scheduler_jobs" in runtime
        results.append(("subscription_status", bool(subs) and has_taskbus, runtime))

        # 4) 首次同步（快照 diff 轮询 3s）
        nodes = wait_for_nodes(1, timeout=40)
        results.append(("initial_sync", bool(nodes), nodes))

        # 5) 上游新增实例 -> 快照 diff 产出事件 -> 网关更新
        STATE["nacos"]["svc-a"].append({"ip": "10.0.0.2", "port": 8080, "weight": 1, "enabled": True})
        nodes = wait_for_nodes(2, timeout=30)
        results.append(("event_driven_sync", bool(nodes) and len(nodes) == 2, nodes))

        # 6) 入站 webhook 回调
        resp = httpx.post(f"http://127.0.0.1:{APP_PORT}/-/notify?discovery=nacos1",
                          json={"serviceName": "svc-a"}, headers=APP_HEADERS, timeout=5)
        accepted = resp.status_code == 200 and resp.json().get("accepted") == 1
        results.append(("webhook_notify", accepted, resp.json()))

        # 7) 任务总线统计：事件已产生投递
        runtime2 = httpx.get(f"http://127.0.0.1:{APP_PORT}/-/subscriptions", headers=APP_HEADERS, timeout=5).json()["_runtime"]
        results.append(("taskbus_stats", runtime2["taskbus"]["submitted"] > 0, runtime2["taskbus"]))

        # 8) 内存/线程泄漏压力：60 次事件 + 5 次 reload（覆盖 EventBus/TaskBus/订阅重建）
        samples = stress_and_sample(f"http://127.0.0.1:{APP_PORT}", proc.pid)
        memory = analyse_memory(samples)
        results.append((
            "memory_stability",
            memory["growth_pct"] < 30 and memory["thread_growth"] <= 2,
            memory,
        ))
    except Exception as exc:  # noqa: BLE001
        results.append(("smoke_failed", False, f"{type(exc).__name__}: {exc}"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for server in servers:
            server.shutdown()
            server.server_close()
        print("---- app log tail ----")
        try:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
        except Exception:  # noqa: BLE001
            pass

    print(json.dumps({
        "checks": [{"name": n, "ok": bool(o), "detail": d} for n, o, d in results],
    }, ensure_ascii=False, indent=2))
    failed = [r[0] for r in results if not r[1]]
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print("\nAPP SMOKE ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
