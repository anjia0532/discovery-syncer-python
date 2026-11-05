#!/usr/bin/env bash
# 集群模式真实 Redis 主备切换验证。
#
#   bash tests/integration/cluster_verify.sh [redis_port]
#
# 验证项：
#   1. 首个副本抢到 leader
#   2. 第二个副本成为 follower（不重复写网关）
#   3. 杀掉 leader（不释放租约）后，follower 在 TTL 内接管
#   4. 优雅停机主动释放租约，其他副本立刻接管
#   5. Redis 不可用时降级为单机模式（require_redis=false）
#
# 需要：python3 + pip install redis

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
PORT="${1:-16379}"
PY="${PYTHON:-python3}"

echo "==> Redis: 127.0.0.1:$PORT"
"$PY" -c "import redis; c=redis.Redis(port=$PORT, socket_timeout=3, decode_responses=True); print('PING:', c.ping())"

cd "$PROJ"
PYTHONPATH="$PROJ" "$PY" "$HERE/cluster_scenario.py" "$PORT"