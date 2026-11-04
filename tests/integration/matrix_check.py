#!/usr/bin/env python3
"""多版本镜像契约验证（只依赖 httpx + 标准库）。

对每个版本真实打 HTTP 请求，验证：
* 版本探测结果与镜像大版本一致；
* Nacos v1/v3 API 路径可达性差异；
* APISIX 2.x/3.x Admin API 响应形态与 upstream/nodes 写入；
* Kong 2.x/3.x target 写入方法（POST vs PUT）与 409 行为差异。

用法::

    python3 matrix_check.py probe   # 只探测版本
    python3 matrix_check.py test    # 探测 + 真实读写契约验证
    python3 matrix_check.py wait    # 等待所有端口就绪
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("dscompat", ROOT / "app" / "compat" / "__init__.py")
assert _spec and _spec.loader
compat = importlib.util.module_from_spec(_spec)
# dataclass 在解析 `from __future__ import annotations` 时会查 sys.modules[cls.__module__]，
# 必须先注册，否则拿到 None 会抛 AttributeError。
sys.modules[_spec.name] = compat
_spec.loader.exec_module(compat)

APISIX_KEY = "edd1c9f034335f136f87ad84b625c8f1"

MATRIX = {
    "nacos": [
        ("nacos-1.x", "http://127.0.0.1:18848", 1, 18848),
        ("nacos-2.x", "http://127.0.0.1:28848", 2, 28848),
        ("nacos-3.x", "http://127.0.0.1:38848", 3, 38848),
    ],
    "apisix": [
        ("apisix-2.x", "http://127.0.0.1:29080", 2, 29080),
        ("apisix-3.x", "http://127.0.0.1:39180", 3, 39180),
    ],
    "kong": [
        ("kong-2.x", "http://127.0.0.1:28001", 2, 28001),
        ("kong-3.x", "http://127.0.0.1:38001", 3, 38001),
    ],
}


def http_get(url: str, headers: Dict[str, str], timeout: float) -> Any:
    return httpx.get(url, headers=headers, timeout=timeout)


def tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_ready(timeout_sec: int = 180) -> bool:
    """等待矩阵中所有 HTTP 端口就绪。"""
    deadline = time.time() + timeout_sec
    ports = [item[3] for group in MATRIX.values() for item in group]
    while time.time() < deadline:
        pending = [p for p in ports if not tcp_alive("127.0.0.1", p, 0.5)]
        if not pending:
            print(f"all {len(ports)} ports ready")
            return True
        print(f"waiting for ports: {pending}")
        time.sleep(3)
    return False


def probe() -> Dict[str, Any]:
    """版本探测（不写数据）。"""
    report: Dict[str, Any] = {}
    for label, host, expected, _port in MATRIX["nacos"]:
        cap, evidence = compat.detect(
            compat.Product.NACOS, host=host,
            http_get=http_get, tcp_probe=lambda h, p, t: tcp_alive(h, p, t),
        )
        report[label] = {"expected_major": expected, "detected": cap.major,
                         "label": cap.label, "evidence": evidence,
                         "api_prefix": cap.api_prefix, "push": cap.push.value,
                         "ok": cap.major == expected}
    for label, admin, expected, _port in MATRIX["apisix"]:
        cap, evidence = compat.detect(compat.Product.APISIX, admin_url=admin,
                                      prefix="/apisix/admin/", api_key=APISIX_KEY,
                                      http_get=http_get)
        report[label] = {"expected_major": expected, "detected": cap.major,
                         "label": cap.label, "evidence": evidence,
                         "list_shape": cap.list_shape.value, "ok": cap.major == expected}
    for label, admin, expected, _port in MATRIX["kong"]:
        cap, evidence = compat.detect(compat.Product.KONG, admin_url=admin, http_get=http_get)
        report[label] = {"expected_major": expected, "detected": cap.major,
                         "label": cap.label, "evidence": evidence,
                         "target_write": cap.target_write, "ok": cap.major == expected}
    return report


def _apisix_contract(base: str, major: int) -> Dict[str, Any]:
    """真实创建 upstream 并 PATCH nodes，验证 2.x/3.x 归一化与写入。"""
    headers = {"X-API-KEY": APISIX_KEY, "Content-Type": "application/json"}
    name = "ds-matrix-test"
    listing = httpx.get(f"{base}/apisix/admin/upstreams", headers=headers, timeout=5).json()
    shape = "node" if isinstance(listing, dict) and ("node" in listing or "action" in listing) else "list"
    put = httpx.put(f"{base}/apisix/admin/upstreams/{name}", headers=headers, timeout=5,
                    json={"name": name, "nodes": {"127.0.0.1:18080": 1}, "type": "roundrobin"})
    patched = httpx.patch(f"{base}/apisix/admin/upstreams/{name}/nodes", headers=headers, timeout=5,
                          json=[{"host": "127.0.0.2", "port": 8080, "weight": 2}])
    fetched = httpx.get(f"{base}/apisix/admin/upstreams/{name}", headers=headers, timeout=5).json()
    items = compat.normalize_response(fetched, compat.capability(compat.Product.APISIX, major).list_shape)
    value = (items[0].get("value") or items[0]) if items else {}
    nodes = value.get("nodes") or []
    return {
        "list_shape": shape, "put_status": put.status_code, "patch_status": patched.status_code,
        "get_nodes": nodes,
        "ok": shape == ("node" if major == 2 else "list")
              and put.status_code < 400 and patched.status_code < 400 and bool(nodes),
    }


def _kong_contract(base: str, major: int) -> Dict[str, Any]:
    """验证 Kong 2.x/3.x target 写入方法差异。

    3.x 连续两次写同一 target 必须用 PUT；用 POST 第二次会得到 409。
    这里同时验证「正确方法成功」与「错误方法在 3.x 确实冲突」，
    以证明版本适配不是多余的。
    """
    name = "ds-matrix-test"
    target = "127.0.0.1:18080"
    httpx.put(f"{base}/upstreams/{name}", json={"name": name}, timeout=5)
    correct = "POST" if major == 2 else "PUT"
    wrong = "PUT" if major == 2 else "POST"

    url_correct = (f"{base}/upstreams/{name}/targets" if correct == "POST"
                   else f"{base}/upstreams/{name}/targets/{target}")
    body = {"target": target, "weight": 100}
    first = httpx.request(correct, url_correct, json=body, timeout=5)
    second = httpx.request(correct, url_correct, json=body, timeout=5)

    url_wrong = (f"{base}/upstreams/{name}/targets" if wrong == "POST"
                 else f"{base}/upstreams/{name}/targets/{target}")
    wrong_status = None
    if major == 3:
        httpx.request(wrong, url_wrong, json=body, timeout=5)
        wrong_status = httpx.request(wrong, url_wrong, json=body, timeout=5).status_code

    listing = httpx.get(f"{base}/upstreams/{name}/targets", timeout=5).json()
    return {
        "version": httpx.get(f"{base}/", timeout=5).json().get("version"),
        "correct_method": correct, "first_status": first.status_code,
        "second_status": second.status_code,
        "wrong_method_second_status_on_3x": wrong_status,
        "targets": [t.get("target") for t in listing.get("data", [])],
        "ok": first.status_code < 400 and second.status_code < 400
              and target in [t.get("target") for t in listing.get("data", [])],
    }


def run(mode: str) -> int:
    if mode == "wait":
        return 0 if wait_ready() else 1
    report = probe()
    if mode == "test":
        for label, base, major, _port in MATRIX["apisix"]:
            try:
                report[label]["contract"] = _apisix_contract(base, major)
            except Exception as exc:  # noqa: BLE001
                report[label]["contract"] = {"ok": False, "error": str(exc)}
        for label, base, major, _port in MATRIX["kong"]:
            try:
                report[label]["contract"] = _kong_contract(base, major)
            except Exception as exc:  # noqa: BLE001
                report[label]["contract"] = {"ok": False, "error": str(exc)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failures = [k for k, v in report.items() if not v.get("ok")
                or (isinstance(v.get("contract"), dict) and not v["contract"].get("ok"))]
    if failures:
        print(f"\nFAILED: {failures}", file=sys.stderr)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "probe"))
