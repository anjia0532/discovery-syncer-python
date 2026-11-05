#!/usr/bin/env python3
"""从 syncer 下载网关配置并提交到 git 仓库。

支持多集群、多套 kubeconfig 的批量备份：
- 顶层 clusters 定义每个集群的连接方式（kubeconfig / context / namespace / service）
- jobs 引用集群名列表，一个 job 可备份多个集群
- 备份路径：base_dir/<job>/<cluster>/<gateway>/<base_name>

配置示例（job.json）：
    {
      "clusters": {
        "a": {
          "kubeconfig": "/root/.kube/config-a",
          "context": "ctx-a",
          "namespace": "syncer",
          "service": "syncer",
          "remote_port": 9797
        },
        "b": {
          "kubeconfig": "/root/.kube/config-b",
          "context": "ctx-b",
          "namespace": "syncer",
          "service": "syncer"
        }
      },
      "jobs": [
        {
          "name": "gateway-backup",
          "clusters": ["a", "b", "c", "d", "e"],
          "gateways": ["apisix1"],
          "syncer_api_key": "xxx",
          "base_name": "apisix.yaml"
        }
      ]
    }

也兼容旧格式（单 job 直连）：
    {
      "apisix1": {
        "syncer": "http://localhost:9797",
        "gateways": ["apisix1"],
        "syncer_api_key": "xxx"
      }
    }

用法：
    export SYNC_JOB_JSON=/path/to/job.json
    python3 backup.py
    python3 backup.py --config job.json --git-repo git@github.com:xxx/backup.git
    python3 backup.py --config job.json --no-push
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("backup")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ClusterSpec:
    """单个集群的连接配置（一套 kubeconfig）。"""

    kubeconfig: str | None = None
    context: str | None = None
    namespace: str = "default"
    service: str = ""
    pod: str = ""
    remote_port: int = 9797
    local_port: int = 0  # 0 = 自动分配
    extra_args: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.service and not self.pod:
            raise ValueError("cluster 必须指定 service 或 pod")

    @property
    def target(self) -> str:
        return self.service or self.pod


@dataclass
class JobSpec:
    """一个备份任务，可包含多个集群。"""

    name: str
    gateways: list[str]
    clusters: list[str] = field(default_factory=list)  # 引用 clusters 字典
    base_name: str = "apisix.yaml"
    syncer_api_key: str = ""
    timeout: float = 10.0
    ssl_verify: bool = False
    direct_url: str | None = None  # 直连模式（无 kubeconfig）


# ---------------------------------------------------------------------------
# kubectl port-forward 管理
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """分配一个空闲的本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    """等待端口可连接。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


class PortForwardManager:
    """管理 kubectl port-forward 子进程的生命周期。

    支持指定 kubeconfig 文件，用于多集群多套 kubeconfig 场景。

    用法::

        with PortForwardManager(cluster_spec) as url:
            httpx.get(f"{url}/health")
    """

    def __init__(self, spec: ClusterSpec) -> None:
        self._spec = spec
        self._proc: subprocess.Popen | None = None
        self._local_port = spec.local_port or _find_free_port()

    def __enter__(self) -> str:
        cmd = ["kubectl"]
        if self._spec.kubeconfig:
            cmd += ["--kubeconfig", self._spec.kubeconfig]
        if self._spec.context:
            cmd += ["--context", self._spec.context]
        cmd += [
            "--namespace", self._spec.namespace,
            "port-forward",
            self._spec.target,
            f"{self._local_port}:{self._spec.remote_port}",
            *self._spec.extra_args,
        ]
        logger.info("启动 port-forward: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not _wait_port("127.0.0.1", self._local_port):
            self._stop()
            raise RuntimeError(
                f"port-forward 端口 {self._local_port} 在 15s 内未就绪，"
                f"请检查 kubeconfig={self._spec.kubeconfig} "
                f"context={self._spec.context} "
                f"namespace={self._spec.namespace} "
                f"target={self._spec.target}"
            )
        return f"http://127.0.0.1:{self._local_port}"

    def __exit__(self, *_exc: Any) -> None:
        self._stop()

    def _stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)
        logger.info("port-forward 已关闭 (pid=%d)", self._proc.pid)
        self._proc = None


# ---------------------------------------------------------------------------
# Git 操作
# ---------------------------------------------------------------------------


def ensure_git_repo(
    base_dir: str,
    git_repo: str | None,
    remote_name: str = "origin",
    branch: str = "main",
):
    """确保 git 仓库存在并拉取最新代码。"""
    try:
        from git import Repo
    except ImportError:
        logger.error("GitPython 未安装，请执行: pip install GitPython")
        sys.exit(1)

    base_path = pathlib.Path(base_dir)
    if not base_path.exists():
        if not git_repo:
            raise ValueError(f"目录 {base_dir} 不存在，且未提供 GIT_REPO")
        logger.info("git clone --branch %s %s %s", branch, git_repo, base_dir)
        base_path.parent.mkdir(parents=True, exist_ok=True)
        return Repo.clone_from(url=git_repo, to_path=base_dir, branch=branch)

    try:
        repo = Repo(base_dir)
    except Exception:
        logger.info("git init --initial-branch=%s %s", branch, base_dir)
        repo = Repo.init(base_dir, initial_branch=branch)

    if not git_repo:
        repo.git.checkout("-B", branch)
        return repo

    try:
        remote = repo.remote(remote_name)
    except ValueError:
        remote = repo.create_remote(name=remote_name, url=git_repo)
        logger.info("git remote add %s %s", remote_name, git_repo)

    logger.info("git fetch %s", remote_name)
    remote.fetch()
    repo.git.checkout("-B", branch)
    try:
        remote.pull(refspec=branch)
        logger.info("git pull %s %s", remote_name, branch)
    except Exception as e:
        logger.warning("git pull 失败（可能是新分支）: %s", e)

    return repo


# ---------------------------------------------------------------------------
# 备份核心
# ---------------------------------------------------------------------------


def _download_gateway_config(
    gateway: str,
    syncer_url: str,
    api_key: str,
    client: httpx.Client,
    out_file: pathlib.Path,
) -> tuple[bool, str]:
    """下载单个网关配置到本地文件。"""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    headers = {"SYNCER-API-KEY": api_key} if api_key else {}

    try:
        resp = client.get(
            f"{syncer_url}/gateway-api-to-file/{gateway}",
            headers=headers,
        )
    except Exception as e:
        return False, f"请求失败: {e}"

    err_msg = resp.headers.get("syncer-err-msg")
    if err_msg:
        try:
            err_msg = base64.b64decode(err_msg.encode()).decode()
        except Exception:
            pass
        return False, f"syncer 返回错误: {err_msg}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    out_file.write_text(resp.text, encoding="utf-8")
    return True, f"成功 ({len(resp.text)} bytes)"


def backup_cluster(
    job_name: str,
    cluster_name: str,
    cluster_spec: ClusterSpec | None,
    job: JobSpec,
    base_dir: str,
) -> list[dict[str, Any]]:
    """备份单个集群下的所有网关配置。"""
    results = []
    client = httpx.Client(timeout=job.timeout, verify=job.ssl_verify)
    pf_manager: PortForwardManager | None = None

    try:
        if cluster_spec is not None:
            pf_manager = PortForwardManager(cluster_spec)
            syncer_url = pf_manager.__enter__()
            logger.info("[%s/%s] kubectl port-forward: %s", job_name, cluster_name, syncer_url)
        else:
            syncer_url = job.direct_url or ""
            logger.info("[%s/%s] 直连: %s", job_name, cluster_name, syncer_url)

        for gateway in job.gateways:
            # 多集群路径：base_dir/job/cluster/gateway/file
            # 单集群直连：base_dir/job/gateway/file（兼容旧格式）
            if cluster_spec is not None:
                config_dir = pathlib.Path(base_dir) / job_name / cluster_name / gateway
            else:
                config_dir = pathlib.Path(base_dir) / job_name / gateway
            out_file = config_dir / job.base_name

            ok, msg = _download_gateway_config(
                gateway, syncer_url, job.syncer_api_key, client, out_file,
            )
            logger.info(
                "[%s/%s/%s] %s : %s",
                job_name, cluster_name, gateway,
                "OK" if ok else "FAIL", msg,
            )
            results.append({
                "job": job_name,
                "cluster": cluster_name,
                "gateway": gateway,
                "base_name": job.base_name,
                "ok": ok,
                "msg": msg,
                "syncer": syncer_url,
                "syncer_api_key": job.syncer_api_key,
            })
    finally:
        if pf_manager is not None:
            pf_manager.__exit__()
        client.close()

    return results


# ---------------------------------------------------------------------------
# restore.py 生成
# ---------------------------------------------------------------------------

_RESTORE_TEMPLATE = '''#!/usr/bin/env python3
"""自动生成的网关配置还原脚本。

由 tools/backup.py 生成，请勿手动修改。
执行: python3 restore.py
"""

import os
import sys

import httpx

JOBS = {jobs_json}

CLIENT_KW = {{"timeout": 10, "verify": False}}


def check_file(file_path: str) -> None:
    if not os.path.exists(file_path):
        print(f"文件不存在: {{file_path}}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(file_path):
        print(f"不是文件: {{file_path}}", file=sys.stderr)
        sys.exit(1)


def restore(job: str, cluster: str, gateway: str, base_name: str,
            syncer: str, api_key: str) -> bool:
    # 多集群路径 base_dir/job/cluster/gateway/file，单集群路径 base_dir/job/gateway/file
    candidates = [
        os.path.join(os.path.dirname(__file__), job, cluster, gateway, base_name),
        os.path.join(os.path.dirname(__file__), job, gateway, base_name),
    ]
    file_path = next((p for p in candidates if os.path.isfile(p)), candidates[0])
    check_file(file_path)
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    headers = {{"SYNCER-API-KEY": api_key, "Content-Type": "application/yaml"}}
    try:
        resp = httpx.put(
            f"{{syncer}}/restore/{{gateway}}",
            content=content,
            headers=headers,
            **CLIENT_KW,
        )
        if resp.status_code == 200:
            print(f"还原成功: {{job}}/{{cluster}}/{{gateway}}/{{base_name}} -> {{syncer}}/restore/{{gateway}}")
            return True
        print(f"还原失败: HTTP {{resp.status_code}} {{resp.text}}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"还原异常: {{e}}", file=sys.stderr)
        return False


def main() -> int:
    ok_count = 0
    fail_count = 0
    for item in JOBS:
        if restore(item["job"], item["cluster"], item["gateway"], item["base_name"],
                   item["syncer"], item["syncer_api_key"]):
            ok_count += 1
        else:
            fail_count += 1
    print(f"\\n还原完成: 成功 {{ok_count}}, 失败 {{fail_count}}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def generate_restore_script(results: list[dict[str, Any]], base_dir: str) -> pathlib.Path:
    """根据备份结果生成 restore.py。"""
    restore_file = pathlib.Path(base_dir) / "restore.py"
    jobs = [r for r in results if r["ok"]]
    restore_file.write_text(
        _RESTORE_TEMPLATE.format(jobs_json=json.dumps(jobs, ensure_ascii=False, indent=2)),
        encoding="utf-8",
    )
    logger.info("已生成还原脚本: %s", restore_file)
    return restore_file


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def load_config(config_path: str | None) -> tuple[dict[str, ClusterSpec], list[JobSpec]]:
    """从 JSON 文件或环境变量加载配置。

    Returns:
        (clusters_dict, jobs_list)
    """
    if config_path:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
    elif os.getenv("SYNC_JOB_JSON"):
        with open(os.environ["SYNC_JOB_JSON"], encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = {
            "jobs": [
                {
                    "name": "apisix1",
                    "direct_url": "http://localhost:9797",
                    "gateways": ["apisix1"],
                    "syncer_api_key": "NopU13xRheZng2hqHAwaI0TF5VHNN05G",
                }
            ]
        }

    # --- 解析 clusters ---
    clusters: dict[str, ClusterSpec] = {}
    for name, cfg in (raw.get("clusters") or {}).items():
        clusters[name] = ClusterSpec(
            kubeconfig=cfg.get("kubeconfig"),
            context=cfg.get("context"),
            namespace=cfg.get("namespace", "default"),
            service=cfg.get("service", ""),
            pod=cfg.get("pod", ""),
            remote_port=cfg.get("remote_port", 9797),
            local_port=cfg.get("local_port", 0),
            extra_args=cfg.get("extra_args", []),
        )

    # --- 解析 jobs ---
    jobs: list[JobSpec] = []
    raw_jobs = raw.get("jobs")

    if raw_jobs is not None:
        # 新格式：{"clusters": {...}, "jobs": [...]}
        for j in raw_jobs:
            jobs.append(JobSpec(
                name=j["name"],
                gateways=j.get("gateways", []),
                clusters=j.get("clusters", []),
                base_name=j.get("base_name", "apisix.yaml"),
                syncer_api_key=j.get("syncer_api_key", ""),
                timeout=j.get("timeout", 10.0),
                ssl_verify=j.get("ssl_verify", False),
                direct_url=j.get("direct_url"),
            ))
    else:
        # 旧格式：{"job_name": {...}}，兼容直连
        for name, cfg in raw.items():
            jobs.append(JobSpec(
                name=name,
                gateways=cfg.get("gateways", []),
                base_name=cfg.get("base_name", "apisix.yaml"),
                syncer_api_key=cfg.get("syncer_api_key", ""),
                timeout=cfg.get("timeout", 10.0),
                ssl_verify=cfg.get("ssl_verify", False),
                direct_url=cfg.get("syncer"),
            ))

    return clusters, jobs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_backup(args: argparse.Namespace) -> int:
    """执行备份主流程。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    clusters, jobs = load_config(args.config)
    if not jobs:
        logger.error("没有配置任何备份任务")
        return 1

    logger.info("已加载 %d 个集群配置, %d 个备份任务", len(clusters), len(jobs))

    base_dir = args.base_dir or os.getenv("BASE_DIR", "syncer")
    git_repo = args.git_repo or os.getenv("GIT_REPO")
    remote = args.remote or os.getenv("REMOTE_NAME", "origin")
    branch = args.branch or os.getenv("BRANCH", "main")

    logger.info("备份目录: %s", pathlib.Path(base_dir).absolute())
    logger.info("Git 仓库: %s (remote=%s, branch=%s)", git_repo, remote, branch)

    # 1. 准备 git 仓库
    repo = None
    if git_repo or not args.no_push:
        repo = ensure_git_repo(base_dir, git_repo, remote, branch)

    # 2. 逐个 job -> 逐个集群备份
    all_results: list[dict[str, Any]] = []
    for job in jobs:
        logger.info("开始备份任务: %s (gateways=%s, clusters=%s)",
                    job.name, job.gateways, job.clusters or ["direct"])

        if job.clusters:
            # 多集群模式
            for cluster_name in job.clusters:
                if cluster_name not in clusters:
                    logger.error("集群 '%s' 未在 clusters 中定义，跳过", cluster_name)
                    continue
                results = backup_cluster(
                    job.name, cluster_name, clusters[cluster_name], job, base_dir,
                )
                all_results.extend(results)
        else:
            # 直连模式（兼容旧格式）
            results = backup_cluster(
                job.name, "-", None, job, base_dir,
            )
            all_results.extend(results)

    # 3. 生成 restore.py
    generate_restore_script(all_results, base_dir)

    # 4. 提交并推送
    if repo is not None and not args.no_push:
        has_changes = repo.index.diff(None) or repo.untracked_files
        if has_changes:
            for d in repo.index.diff(None):
                logger.info("变更文件: %s", d.a_path)
            for f in repo.untracked_files:
                logger.info("新增文件: %s", f)
            repo.git.add("*")
            from git import Actor
            repo.index.commit(
                message="Auto Backup Job",
                author=Actor(name=args.git_email, email=args.git_email),
            )
            if any(r.name == remote for r in repo.remotes):
                logger.info("git push %s %s", remote, branch)
                repo.remote(remote).push(branch)
        else:
            logger.info("无变更，跳过提交")

    # 5. 汇总
    ok = sum(1 for r in all_results if r["ok"])
    fail = sum(1 for r in all_results if not r["ok"])
    logger.info("备份完成: 成功 %d, 失败 %d", ok, fail)
    return 0 if fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="从 syncer 备份网关配置到 git（支持多集群多 kubeconfig）")
    parser.add_argument("--config", "-c", default=None, help="备份配置 JSON 文件路径")
    parser.add_argument("--git-repo", default=None, help="Git 仓库地址")
    parser.add_argument("--remote", default=None, help="Git remote 名称 (默认 origin)")
    parser.add_argument("--branch", default=None, help="Git 分支 (默认 main)")
    parser.add_argument("--base-dir", default=None, help="本地备份目录 (默认 syncer)")
    parser.add_argument("--git-email", default=None, help="Git 提交邮箱")
    parser.add_argument("--no-push", action="store_true", help="只下载到本地，不提交推送")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.git_email is None:
        args.git_email = os.getenv(
            "USER_EMAIL", "discovery-syncer-python@syncer.org"
        )
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    return run_backup(args)


if __name__ == "__main__":
    sys.exit(main())
