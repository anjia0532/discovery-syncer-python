#!/usr/bin/env bash
#
# 构建并推送镜像到 Docker Hub / gcr.io。
#
# 用法：
#   ./build.sh <version> [--registry dockerhub|gcr|both] [--no-push] [--backup-only|--main-only]
#
# 示例：
#   ./build.sh 3.0.0                      # 推 Docker Hub + gcr.io（默认 both）
#   ./build.sh 3.0.0 --registry gcr       # 只推 gcr.io
#   ./build.sh 3.0.0 --no-push            # 只构建不推送
#
# 环境变量（gcr.io 需要）：
#   GCP_PROJECT_ID   gcr.io 对应的 GCP 项目 ID
#
# 前置：已安装 docker buildx，且已 docker login。

set -euo pipefail

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
VERSION="${1:-}"
REGISTRY="both"
PUSH=true
BUILD_MAIN=true
BUILD_BACKUP=true

shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry) REGISTRY="$2"; shift 2 ;;
    --no-push)  PUSH=false; shift ;;
    --main-only)   BUILD_BACKUP=false; shift ;;
    --backup-only) BUILD_MAIN=false; shift ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  echo "用法: $0 <version> [--registry dockerhub|gcr|both] [--no-push]" >&2
  exit 1
fi

# 支持 3.0.0 与 v3.0.0 两种写法：统一去掉 v/V 前缀再校验。
# 镜像 tag 不带 v（Docker 惯例），git tag 才带 v。
VERSION="${VERSION#v}"
VERSION="${VERSION#V}"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
  echo "版本号格式错误: $VERSION (需要 3.0.0 或 v3.0.0，可带 -pre 等后缀)" >&2
  exit 1
fi

PLATFORM="${PLATFORM:-linux/amd64,linux/arm64}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="discovery-syncer-python"
DOCKERHUB_USER="${DOCKERHUB_USER:-anjia0532}"
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"

cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# 构建器准备
# ---------------------------------------------------------------------------
BUILDER="ds-builder"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "==> 创建 buildx 构建器: $BUILDER (platforms=$PLATFORM)"
  docker buildx create --name "$BUILDER" --use --platform "$PLATFORM"
else
  docker buildx use "$BUILDER"
fi

# 可选构建参数（国内网络构建时使用）：
#   APT_MIRROR=mirrors.ustc.edu.cn      替换 Debian apt 源
#   KUBECTL_VERSION=v1.31.4             指定 kubectl 版本；置空则跳过安装
#   KUBECTL_BASE=...                    kubectl 下载源
EXTRA_BUILD_ARGS=()
[[ -n "${APT_MIRROR:-}" ]] && EXTRA_BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
[[ -n "${KUBECTL_VERSION:-}" ]] && EXTRA_BUILD_ARGS+=(--build-arg "KUBECTL_VERSION=${KUBECTL_VERSION}")
[[ -n "${KUBECTL_BASE:-}" ]] && EXTRA_BUILD_ARGS+=(--build-arg "KUBECTL_BASE=${KUBECTL_BASE}")

# ---------------------------------------------------------------------------
# 标签生成
# ---------------------------------------------------------------------------
build_tags() {
  local registry="$1"
  local repo_prefix=""
  case "$registry" in
    dockerhub) repo_prefix="$DOCKERHUB_USER" ;;
    gcr)       repo_prefix="gcr.io/$GCP_PROJECT_ID" ;;
  esac
  echo "--tag ${repo_prefix}/${IMAGE_NAME}:${VERSION}"
  echo "--tag ${repo_prefix}/${IMAGE_NAME}:latest"
}

# ---------------------------------------------------------------------------
# 主镜像
# ---------------------------------------------------------------------------
if [[ "$BUILD_MAIN" == true ]]; then
  echo "==> 构建主镜像 ${IMAGE_NAME}:${VERSION}"
  BUILD_ARGS=(--file "$SCRIPT_DIR/Dockerfile" --platform "$PLATFORM" "${EXTRA_BUILD_ARGS[@]}")
  PUSH_ARG="--push"
  [[ "$PUSH" == false ]] && PUSH_ARG="--load"

  case "$REGISTRY" in
    dockerhub)
      docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
        $(build_tags dockerhub) .
      ;;
    gcr)
      if [[ -z "$GCP_PROJECT_ID" ]]; then
        echo "::error::GCP_PROJECT_ID 未设置，无法推送到 gcr.io" >&2
        exit 1
      fi
      docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
        $(build_tags gcr) .
      ;;
    both)
      if [[ -z "$GCP_PROJECT_ID" ]]; then
        echo "::warning::GCP_PROJECT_ID 未设置，跳过 gcr.io"
        docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
          $(build_tags dockerhub) .
      else
        docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
          $(build_tags dockerhub) $(build_tags gcr) .
      fi
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# 备份镜像
# ---------------------------------------------------------------------------
if [[ "$BUILD_BACKUP" == true ]]; then
  echo "==> 构建备份镜像 ${IMAGE_NAME}-backup:${VERSION}"
  BUILD_ARGS=(--file "$SCRIPT_DIR/BackupDockerfile" --platform "$PLATFORM" "${EXTRA_BUILD_ARGS[@]}")
  PUSH_ARG="--push"
  [[ "$PUSH" == false ]] && PUSH_ARG="--load"

  case "$REGISTRY" in
    dockerhub)
      docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
        --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:${VERSION}" \
        --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:latest" .
      ;;
    gcr)
      if [[ -z "$GCP_PROJECT_ID" ]]; then
        echo "::error::GCP_PROJECT_ID 未设置" >&2
        exit 1
      fi
      docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
        --tag "gcr.io/${GCP_PROJECT_ID}/${IMAGE_NAME}-backup:${VERSION}" \
        --tag "gcr.io/${GCP_PROJECT_ID}/${IMAGE_NAME}-backup:latest" .
      ;;
    both)
      if [[ -z "$GCP_PROJECT_ID" ]]; then
        docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
          --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:${VERSION}" \
          --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:latest" .
      else
        docker buildx build "${BUILD_ARGS[@]}" "${PUSH_ARG}" \
          --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:${VERSION}" \
          --tag "${DOCKERHUB_USER}/${IMAGE_NAME}-backup:latest" \
          --tag "gcr.io/${GCP_PROJECT_ID}/${IMAGE_NAME}-backup:${VERSION}" \
          --tag "gcr.io/${GCP_PROJECT_ID}/${IMAGE_NAME}-backup:latest" .
      fi
      ;;
  esac
fi

echo ""
echo "==> 构建完成"
echo "  Docker Hub: https://hub.docker.com/r/${DOCKERHUB_USER}/${IMAGE_NAME}/tags"
if [[ -n "$GCP_PROJECT_ID" ]]; then
  echo "  gcr.io:     gcr.io/${GCP_PROJECT_ID}/${IMAGE_NAME}:${VERSION}"
fi
