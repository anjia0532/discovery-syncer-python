#!/usr/bin/env python3
"""无 Docker 环境下的等价验证：启动本地 mock，复用 matrix_check.py 跑完整契约。

    python3 tests/integration/run_local_matrix.py

覆盖与 run_matrix.sh test 完全相同的检查项（版本探测、APISIX 2/3 响应形态与
nodes 写入、Kong 2/3 的 POST-vs-PUT 与 409 行为），只是后端换成本地协议 mock。
"""

from __future__ import annotations

import sys

import matrix_check
import mock_servers


def main() -> int:
    mock_servers.start_all()
    try:
        return matrix_check.run("test")
    finally:
        mock_servers.stop_all()


if __name__ == "__main__":
    sys.exit(main())
