import base64
import importlib
import json
import os
import pathlib
import subprocess

import httpx
from git import Actor, InvalidGitRepositoryError

DEFAULT_BASE_DIR = os.getenv("BASE_DIR", "syncer")
DEFAULT_USER_EMAIL = os.getenv("USER_EMAIL", "discovery-syncer-python@syncer.org")

GIT_REPO = os.getenv("GIT_REPO", None)
REMOTE = os.getenv("REMOTE_NAME", "origin")
BRANCH = os.getenv("BRANCH", "main")
SYNC_JOB_JSON = os.getenv("SYNC_JOB_JSON", None)


def ensure_git_executable_and_clone(base_dir=DEFAULT_BASE_DIR, git_repo=GIT_REPO, remote=REMOTE, branch_name=BRANCH):
    try:
        from git import Repo
    except ImportError:
        print(
            f"尝试安装 git 包: GitPython 注意，请确保系统已经安装了Git 1.7.0+\n当前 Git 版本为: {subprocess.getoutput('git version')}")
        os.system('python -m pip install GitPython')
        Repo = importlib.import_module('Repo', 'git')
    # 如果不存在，直接clone并返回
    if not os.path.isdir(base_dir):
        print(f"{base_dir}不存在，执行 git clone --branch {branch_name} {git_repo} {base_dir}")
        try:
            return Repo.clone_from(url=git_repo, to_path=base_dir, branch=branch_name)
        except:
            pathlib.Path(base_dir).mkdir(parents=True, exist_ok=True)
    try:
        # 判断 base_dir 是否存在
        repo = Repo(base_dir)
    except InvalidGitRepositoryError:
        # 判断是否 git init
        repo = Repo.init(base_dir, initial_branch=branch_name)
        print(f"执行 git init --initial-branch={branch_name}")
    if not git_repo:
        repo.git.checkout("-B", branch_name)
        print(f"执行 git checkout -B {branch_name}")
        return repo
    try:
        # 判断 remote 是否存在
        remote = repo.remote(remote)
    except ValueError:
        # 不存在则添加 remote
        remote = repo.create_remote(name=remote, url=git_repo)
        print(f"执行 git remote add {remote} {git_repo}")

    # 提取
    remote.fetch()
    print(f"执行 git fetch")
    repo.git.checkout("-B", branch_name)
    print(f"执行 git checkout -B {branch_name}")
    # 拉取
    try:
        remote.pull(refspec=branch_name)
        print(f"执行 git pull {remote} {branch_name}")
    except Exception as e:
        print(f"执行 git pull {remote} {branch_name} 报错, {e}")
    return repo


def backup_syncer_jobs_to_local(syncer_jobs=None, base_dir: str = DEFAULT_BASE_DIR, repo=None):
    restore_jobs = []
    with open(f"{base_dir}/restore.py", 'w', encoding='UTF-8') as script_file:
        script_file.write(f"""
import os

import httpx


def check_file(file_path):
    assert os.path.exists(file_path), f"文件 {{file_path}} 不存在"
    assert os.path.isfile(file_path), f"文件 {{file_path}} 不存在"
    assert os.access(file_path, os.R_OK), f"文件 {{file_path}} 无法访问"

""")
        for job, syncer in syncer_jobs.items():
            for gateway in syncer["gateways"]:
                status_code = 500
                syncer_err_msg = ""
                status_msg = None
                config_dir = f"{base_dir}/{job}/{gateway}"
                restore_jobs.append(f"restore_{job}_{gateway}()")
                try:
                    pathlib.Path(f"{config_dir}").mkdir(parents=True, exist_ok=True)
                    with open(f"{config_dir}/{syncer['base_name']}", 'w') as f:
                        try:
                            resp = httpx.get(f"{syncer['syncer']}/gateway-api-to-file/{gateway}",
                                             headers={"SYNCER-API-KEY": f"{syncer['syncer_api_key']}"})
                            status_code = resp.status_code
                            syncer_err_msg = resp.headers.get("syncer-err-msg", None)
                            if status_code == 200:
                                status_msg = "成功"
                                f.write(resp.text)
                        except Exception as e:
                            status_msg = str(e.args)
                        if syncer_err_msg:
                            status_msg = base64.b64decode(syncer_err_msg.encode('utf-8')).decode('utf-8')
                except Exception as e:
                    status_msg = str(e.args)
                print(
                    f"\n\n下载 {syncer['syncer']}/gateway-api-to-file/{gateway} 到 {config_dir}/{syncer['base_name']} ,status_code={status_code}, status_msg={status_msg}")
                script_file.write(f"""
def restore_{job.replace("-","_")}_{gateway.replace("-","_")}():
    check_file("{job}/{gateway}/{syncer['base_name']}")
    with open("{job}/{gateway}/{syncer['base_name']}", 'r', encoding='UTF-8') as f:
        resp = httpx.put("{syncer['syncer']}/restore/{gateway}", content="\\n".join(f.readlines()),
                         headers={{"SYNCER-API-KEY": "{syncer['syncer_api_key']}"}}).text
        print(f"还原 {job}/{gateway}/{syncer['base_name']} 到 {syncer['syncer']}/restore/{gateway} , 结果为: {{resp}}")

""")
                if status_code != 200:
                    print(
                        f"\n\n下载 {syncer['syncer']}/gateway-api-to-file/{gateway} 失败, 删除空文件 {config_dir}/{syncer['base_name']}")
                    os.remove(f"{config_dir}/{syncer['base_name']}")
                    try:
                        repo.git.checkout(f"HEAD -- {job}/{gateway}/{syncer['base_name']}", force=True)
                        print(f"执行 git checkout --force HEAD -- {config_dir}/{syncer['base_name']}")
                    except Exception:
                        pass
        script_file.write(f"""
if __name__ == '__main__':
    print("执行 python restore.py 还原网关配置")
""")
        for restore_job in restore_jobs:
            items = restore_job.split("_")
            script_file.write(f"""
    # 还原 {items[1]} 下的 {items[2]}
    # {restore_job.replace("-","_")}
    """)

    print("执行 python restore.py 还原网关配置")


if __name__ == '__main__':
    if SYNC_JOB_JSON:
        with open(SYNC_JOB_JSON, encoding='utf-8') as fh:
            syncer_jobs = json.load(fh)
    else:
        syncer_jobs = {
            "apisix1": {
                "syncer": "http://localhost:9797",
                "gateways": ["apisix1","apisix2"],
                "base_name": "apisix.yaml",
                "syncer_api_key": "NopU13xRheZng2hqHAwaI0TF5VHNN05G"
            }
        }

    base_dir = DEFAULT_BASE_DIR
    print(f"备份目录为: base_dir={pathlib.Path(base_dir).absolute()}")

    # git init base_dir
    print(f"初始化并从git库: {GIT_REPO} 拉取最新变更")
    repo = ensure_git_executable_and_clone(base_dir)

    # download backup config file
    print(f"调用网关 gateway-api-to-file 接口下载最新配置文件")
    backup_syncer_jobs_to_local(syncer_jobs, base_dir, repo)

    # git add base_dir
    if repo.index.diff(None) or repo.untracked_files:
        diffs = repo.index.diff(None)
        for d in diffs:
            print(f"\n\n变更文件: {d.a_path}")
        for untracked_file in repo.untracked_files:
            print(f"\n\n新增文件: {untracked_file}")

        repo.git.add("*")
        print(f"执行 git add .")
        # git commit -m
        repo.index.commit(message="Auto Backup Job",
                          author=Actor(name=DEFAULT_USER_EMAIL, email=DEFAULT_USER_EMAIL))
        print(f"执行 git commit --author='{DEFAULT_USER_EMAIL} <{DEFAULT_USER_EMAIL}> --message= 'Auto Backup Job' ")

        if REMOTE in repo.remotes:
            # git push -u origin master
            push = repo.remote(REMOTE).push(BRANCH)
            print(f"执行 git push -u {REMOTE} {BRANCH}")
    else:
        print("\n\n无变更")
