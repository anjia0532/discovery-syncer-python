#!/usr/bin/env bash
# 多版本镜像验证入口。在可运行 docker 的 WSL / Linux 环境执行。
#
#   ./run_matrix.sh up     拉起矩阵并等待所有端口就绪
#   ./run_matrix.sh probe  仅做版本探测对照
#   ./run_matrix.sh test   探测 + 真实读写契约验证（推荐）
#   ./run_matrix.sh down   清理容器与数据卷
#
# 前置: python3 + pip install httpx   (matrix_check.py 只依赖 httpx 与标准库)
# 注意: Nacos 3.x 若强制鉴权，请在 matrix_check.py 中补充 accessToken header。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$HERE/docker-compose.matrix.yml")
PY="${PYTHON:-python3}"

case "${1:-help}" in
  up)
    "${COMPOSE[@]}" up -d
    echo "==> 等待服务就绪（首次拉取镜像可能较久）..."
    "$PY" "$HERE/matrix_check.py" wait
    echo "==> 就绪。接着运行: ./run_matrix.sh test"
    ;;
  probe)
    "$PY" "$HERE/matrix_check.py" probe
    ;;
  test)
    # 容器端口就绪 != 服务就绪：Nacos/APISIX/Kong 还需要连上 etcd/postgres，
    # 这里再等一段时间，避免契约阶段出现 Connection refused。
    "$PY" "$HERE/matrix_check.py" wait
    sleep 45
    "$PY" "$HERE/matrix_check.py" test
    ;;
  down)
    "${COMPOSE[@]}" down -v
    ;;
  logs)
    "${COMPOSE[@]}" logs -f
    ;;
  *)
    echo "用法: $0 {up|probe|test|down|logs}"
    exit 1
    ;;
esac
