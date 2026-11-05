#!/usr/bin/env python3
"""本地协议级 mock：按 docker-compose.matrix.yml 的端口模拟各版本真实响应。

用途：在没有 Docker 的环境里，仍能对「版本探测 + 响应归一化 + 写入方法」
做端到端验证。真实镜像验证请用 run_matrix.sh。

每个端口对应一个版本：
  18848 Nacos 1.x / 28848 Nacos 2.x / 38848 Nacos 3.x
  29080 APISIX 2.x / 39180 APISIX 3.x
  28001 Kong 2.x / 38001 Kong 3.x
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = defaultdict(lambda: {"upstreams": {}, "targets": {}})

NACOS_VERSIONS = {18848: "1.4.6", 28848: "2.5.5", 38848: "3.3.0"}
APISIX_MAJOR = {29080: 2, 39180: 3}
KONG_VERSIONS = {28001: "2.8.1", 38001: "3.7.1"}


class Handler(BaseHTTPRequestHandler):
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
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _nacos(self, port: int, path: str) -> None:
        if port in NACOS_VERSIONS:
            version = NACOS_VERSIONS[port]
            major = int(version.split(".")[0])
            # 版本探测判据必须精确：3.x 的 v3 路径只有真 3.x 才应返回非 404，
            # 否则 1.x 会被 "ns/instance/list" 之类的宽匹配误判成 3.x。
            if "/v3/" in path:
                if major != 3:
                    return self._send(404, {})
                return self._send(200, {"list": [], "total": 0})
            if "v1/console/server/state" in path or "v2/console/server/state" in path:
                if major == 3:
                    return self._send(404, {})       # 3.x 不再兼容 v1/v2 OpenAPI
                return self._send(200, {"version": version})
            if "ns/catalog/services" in path:
                return self._send(200, {"count": 0, "list": []})
            if "ns/service/list" in path or "ns/instance/list" in path:
                return self._send(200, {"list": [], "total": 0})
        self._send(404, {})

    def _apisix(self, port: int, path: str, method: str, body: dict) -> None:
        major = APISIX_MAJOR[port]
        state = STATE[port]["upstreams"]
        parts = [p for p in path.split("?")[0].split("/") if p and p not in ("apisix", "admin")]

        def wrap_single(value: dict, key: str):
            if major == 2:
                return {"action": "get", "count": 1, "node": {"key": key, "value": value}}
            return {"key": key, "value": value}

        def wrap_list(items):
            nodes = [{"key": f"/apisix/upstreams/{k}", "value": v} for k, v in items.items()]
            if major == 2:
                return {"action": "get", "count": len(nodes),
                        "node": {"key": "/apisix/upstreams", "nodes": nodes, "dir": True}}
            return {"list": nodes, "total": len(nodes)}

        if method == "GET" and (not parts or parts == ["upstreams"]):
            return self._send(200, wrap_list(state))
        name = parts[-1] if parts else ""
        if method == "PUT" and parts and parts[0] == "upstreams":
            state[name] = body
            return self._send(200, wrap_single(body, f"/apisix/upstreams/{name}"))
        if method == "PATCH" and path.endswith("/nodes"):
            upstream_name = parts[-2] if len(parts) >= 2 else name
            state.setdefault(upstream_name, {})["nodes"] = body
            return self._send(200, wrap_single(state[upstream_name], f"/apisix/upstreams/{upstream_name}"))
        if method == "GET" and parts and parts[0] == "upstreams":
            return self._send(200, wrap_single(state.get(name, {}), f"/apisix/upstreams/{name}"))
        self._send(404, {"error_msg": "not found"})

    def _kong(self, port: int, path: str, method: str, body: dict) -> None:
        version = KONG_VERSIONS[port]
        major = int(version.split(".")[0])
        clean = path.split("?")[0].strip("/")
        if method == "GET" and clean == "":
            return self._send(200, {"version": version, "hostname": "mock"})
        parts = clean.split("/") if clean else []
        state = STATE[port]
        if parts[:1] == ["upstreams"] and len(parts) >= 2:
            name = parts[1]
            if method == "PUT" and len(parts) == 2:
                state["targets"].setdefault(name, {})
                return self._send(200, {"name": name})
            if len(parts) >= 3 and parts[2] == "targets":
                targets = state["targets"].setdefault(name, {})
                target = body.get("target") or (parts[3] if len(parts) > 3 else "")
                if method == "GET":
                    return self._send(200, {"data": [{"target": t, "weight": w} for t, w in targets.items()]})
                if method == "POST":
                    if major >= 3 and target in targets:
                        return self._send(409, {"message": "UNIQUE violation detected"})  # 3.x 关键差异
                    targets[target] = body.get("weight", 100)
                    return self._send(201, {"target": target})
                if method == "PUT":
                    # 真实 Kong 3.x：PUT 只更新已存在的 target，不存在则 404（无 upsert）。
                    # mock 必须忠实反映这一点，否则会掩盖「3.x 直接 PUT 会失败」的真实缺陷。
                    if major >= 3 and target not in targets:
                        return self._send(404, {"message": "Not found"})
                    targets[target] = body.get("weight", 100)
                    return self._send(200, {"target": target})
                if method == "DELETE":
                    targets.pop(target, None)
                    return self._send(204, {})
        self._send(404, {"message": "not found"})

    def _dispatch(self, method: str) -> None:
        port = self.server.server_port
        path = self.path
        body = self._body() if method in ("PUT", "PATCH", "POST") else {}
        if port in NACOS_VERSIONS:
            return self._nacos(port, path)
        if port in APISIX_MAJOR:
            return self._apisix(port, path, method, body)
        if port in KONG_VERSIONS:
            return self._kong(port, path, method, body)
        self._send(404, {})

    def do_GET(self):
        self._dispatch("GET")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


_SERVERS = []


def start_all() -> None:
    """在 7 个固定端口启动 mock 服务（线程内，daemon）。"""
    for port in list(NACOS_VERSIONS) + list(APISIX_MAJOR) + list(KONG_VERSIONS):
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, name=f"mock-{port}", daemon=True)
        thread.start()
        _SERVERS.append(server)


def stop_all() -> None:
    """关闭所有 mock 服务。"""
    for server in list(_SERVERS):
        server.shutdown()
        server.server_close()
    _SERVERS.clear()


if __name__ == "__main__":
    start_all()
    print("mock servers listening:", sorted(list(NACOS_VERSIONS) + list(APISIX_MAJOR) + list(KONG_VERSIONS)))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop_all()
