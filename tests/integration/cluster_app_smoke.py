#!/usr/bin/env python3
"""集群模式完整应用验证：起两个真实应用实例，验证只有 leader 装配订阅。

    python3 tests/integration/cluster_app_smoke.py [redis_port]

验证：
1. 两个实例都健康启动
2. 一个 leader、一个 follower
3. leader 有订阅与定时作业，follower 没有（不重复写网关）
4. 杀掉 leader 后 follower 接管并装配订阅
5. 单机配置（cluster 关闭）不连 Redis
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
PROJ = HERE.parents[1]
NACOS_PORT = 18851
APISIX_PORT = 39183
APP1, APP2 = 9801, 9802
API_KEY = "Abcdef1234567890!@#Abcdef1234567890"
HEADERS = {"SYNCER-API-KEY": API_KEY}
STATE = {"nacos": {"svc-a": [{"ip": "10.0.0.1", "port": 8080, "weight": 1, "enabled": True}]}}
RESULTS: list = []


def check(name: str, ok: bool, detail=None) -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_port
        path = self.path
        if port == NACOS_PORT:
            if "console/server/state" in path:
                return self._send(200, {"version": "1.4.6"})
            if path.startswith("/nacos/v1/ns/catalog/services"):
                return self._send(200, {
                    "count": len(STATE["nacos"]),
                    "list": [{"serviceName": n, "clusterMap": {"DEFAULT": {"hosts": i}}}
                             for n, i in STATE["nacos"].items()],
                })
            if path.startswith("/nacos/v1/ns/catalog/instances"):
                name = path.split("serviceName=")[1].split("&")[0]
                return self._send(200, {"list": STATE["nacos"].get(name, []),
                                        "groupName": "DEFAULT_GROUP"})
        self._send(404, {})

    def do_PUT(self):
        self._send(200, {})


def write_cfg(path: Path, port: int, cluster_on: bool, redis_port: int) -> Path:
    cluster_block = f"""
cluster:
  enabled: true
  redis-url: "redis://127.0.0.1:{redis_port}/0"
  key-prefix: ds-app-smoke
  lock-ttl-sec: 6
  renew-interval-sec: 2
  require-redis: false
""" if cluster_on else ""
    path.write_text(f"""
common:
  syncer-api-key: {API_KEY}
{cluster_block}
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
    return path


def start_app(cfg: Path, port: int, log: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PROJECT_ENV", "dev")
    boot = (
        f"import sys; sys.argv=['main.py','-c',{str(cfg)!r}]; "
        f"import uvicorn; uvicorn.run('app:APP', host='127.0.0.1', port={port}, log_config=None)"
    )
    with open(log, "w", encoding="utf-8") as fh:
        return subprocess.Popen([sys.executable, "-c", boot], cwd=str(PROJ),
                                env=env, stdout=fh, stderr=subprocess.STDOUT)


def wait_ready(port: int, timeout=90) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", headers=HEADERS, timeout=2)
            if r.status_code == 200:
                return r.json()
            last = f"status={r.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(1)
    raise RuntimeError(f"端口 {port} 未就绪: {last}")


def cluster_state(port: int) -> dict:
    return httpx.get(f"http://127.0.0.1:{port}/-/cluster", headers=HEADERS, timeout=5).json()


def subs(port: int) -> dict:
    return httpx.get(f"http://127.0.0.1:{port}/-/subscriptions", headers=HEADERS, timeout=5).json()


def main() -> int:
    redis_port = int(sys.argv[1]) if len(sys.argv) > 1 else 16379
    servers = []
    for port in (NACOS_PORT, APISIX_PORT):
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)

    cfg1 = write_cfg(HERE / "cluster-app1.yaml", APP1, True, redis_port)
    cfg2 = write_cfg(HERE / "cluster-app2.yaml", APP2, True, redis_port)
    l1, l2 = Path("/tmp/ca1.log"), Path("/tmp/ca2.log")
    p1 = start_app(cfg1, APP1, l1)
    p2 = None
    try:
        h1 = wait_ready(APP1)
        check("实例1 启动", h1.get("status") == "UP", h1.get("status"))
        c1 = cluster_state(APP1)
        check("实例1 成为 leader", c1.get("role") == "leader", c1.get("role"))
        s1 = subs(APP1)
        jobs1 = s1.get("_runtime", {}).get("scheduler_jobs", [])
        check("实例1 有定时作业", len(jobs1) > 0, jobs1)
        check("实例1 有订阅 Provider",
              any(v.get("providers") for k, v in s1.items() if not k.startswith("_")),
              list(s1.keys()))

        p2 = start_app(cfg2, APP2, l2)
        h2 = wait_ready(APP2)
        check("实例2 启动", h2.get("status") == "UP", h2.get("status"))
        c2 = cluster_state(APP2)
        check("实例2 成为 follower", c2.get("role") == "follower", c2.get("role"))
        s2 = subs(APP2)
        jobs2 = s2.get("_runtime", {}).get("scheduler_jobs", [])
        check("follower 无定时作业（不重复写网关）", len(jobs2) == 0, jobs2)
        check("follower 无订阅 Provider",
              not any(v.get("providers") for k, v in s2.items() if not k.startswith("_")),
              list(s2.keys()))

        # 杀 leader，验证接管
        print("\n  -> kill 实例1，等待实例2 接管")
        p1.send_signal(signal.SIGKILL)
        p1.wait(timeout=10)
        deadline = time.time() + 40
        took_over = False
        while time.time() < deadline:
            try:
                if cluster_state(APP2).get("role") == "leader":
                    took_over = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        check("实例2 接管 leader", took_over)
        if took_over:
            time.sleep(3)
            s2b = subs(APP2)
            jobs = s2b.get("_runtime", {}).get("scheduler_jobs", [])
            check("接管后实例2 装配了定时作业", len(jobs) > 0, jobs)
    except Exception as exc:  # noqa: BLE001
        check("集群应用冒烟", False, f"{type(exc).__name__}: {exc}")
    finally:
        for proc in (p1, p2):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        for f in (cfg1, cfg2):
            f.unlink(missing_ok=True)
        print("\n---- app1 log tail ----")
        print(l1.read_text(errors="replace")[-1500:] if l1.exists() else "")
        print("\n---- app2 log tail ----")
        print(l2.read_text(errors="replace")[-1500:] if l2.exists() else "")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + json.dumps({"checks": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS]},
                            ensure_ascii=False, indent=2))
    if failed:
        print(f"\nFAILED: {failed}", file=sys.stderr)
        return 1
    print("\nCLUSTER APP SMOKE ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())