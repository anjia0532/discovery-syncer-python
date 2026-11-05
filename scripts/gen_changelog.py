#!/usr/bin/env python3
"""从上一个 tag 到 HEAD 的提交生成 CHANGELOG 条目。

用法：
    python3 scripts/gen_changelog.py [--version 3.0.0] [--since-tag v2.7.8] [--date 2026-11-05]

行为：
* 用 ``git log --pretty`` 取 ``since_tag..HEAD`` 的提交，解析 conventional commit 类型；
* 按 feat/fix/refactor/perf/test/docs/build/ci/chore 分组，生成 Keep a Changelog 格式；
* 新条目插入 CHANGELOG.md 顶部（首次运行则创建文件）。

只依赖标准库 + git，可在 CI 与本地 WSL 直接运行。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# conventional commit 类型 -> (分组标题, emoji 可选)
GROUPS = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("refactor", "Refactoring"),
    ("perf", "Performance Improvements"),
    ("test", "Tests"),
    ("docs", "Documentation"),
    ("build", "Build System / CI"),
    ("chore", "Chores"),
]

# 匹配 type(scope)!: subject 或 type: subject
COMMIT_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?!?:\s*(?P<subject>.+)$")


def run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败: {result.stderr.strip()}")
    return result.stdout


def get_last_tag() -> str:
    """返回最新的 semver tag，没有则返回空串。"""
    out = run_git("tag", "--list", "v*").strip()
    if not out:
        return ""
    tags = sorted(
        (t for t in out.splitlines() if t.startswith("v")),
        key=lambda t: tuple(int(x) if x.isdigit() else 0 for x in t.lstrip("v").split(".")),
    )
    return tags[-1]


def parse_commit(subject: str) -> tuple[str, str] | None:
    """解析 conventional commit，返回 (type, subject) 或 None。"""
    m = COMMIT_RE.match(subject.strip())
    if not m:
        return None
    return m.group("type").lower(), m.group("subject").strip()


def collect_commits(since_tag: str) -> list[tuple[str, str]]:
    """取 since_tag..HEAD 的提交 subject 列表。"""
    range_arg = f"{since_tag}..HEAD" if since_tag else "HEAD"
    out = run_git("log", "--pretty=format:%s", range_arg)
    commits = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_commit(line)
        if parsed:
            commits.append(parsed)
    return commits


def build_section(version: str, commits: list[tuple[str, str]], day: str) -> str:
    """生成单个版本的 Keep a Changelog 段落。"""
    lines = [f"## [{version}] - {day}", ""]
    by_type: dict[str, list[str]] = {}
    for ctype, subject in commits:
        by_type.setdefault(ctype, []).append(subject)

    found_any = False
    for ctype, title in GROUPS:
        if ctype not in by_type:
            continue
        found_any = True
        lines.append(f"### {title}")
        lines.append("")
        for subject in by_type[ctype]:
            lines.append(f"* {subject}")
        lines.append("")

    # 未识别的 type 单独归到 Other
    other = [s for t, s in commits if t not in {g[0] for g in GROUPS}]
    if other:
        found_any = True
        lines.append("### Other")
        lines.append("")
        for subject in other:
            lines.append(f"* {subject}")
        lines.append("")

    if not found_any:
        lines.append("* 无 conventional commit 记录")
        lines.append("")

    return "\n".join(lines)


def insert_into_changelog(section: str, path: Path) -> None:
    """把新段落插入 CHANGELOG.md 顶部（Keep a Changelog 头部之后）。"""
    header = "# Changelog\n\n"
    intro = (
        "All notable changes to this project will be documented in this file.\n\n"
        "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
        "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n\n"
    )

    if not path.exists():
        path.write_text(header + intro + section + "\n", encoding="utf-8")
        return

    existing = path.read_text(encoding="utf-8")
    # 保留已有头部（如果存在）
    if existing.startswith(header):
        # 找到 intro 之后的位置（第一个 "## [" 之前）
        idx = existing.find("## [")
        if idx == -1:
            new_content = existing.rstrip() + "\n\n" + section + "\n"
        else:
            head = existing[:idx]
            rest = existing[idx:]
            new_content = head + section + "\n" + rest
    else:
        # 没有标准头部，直接插到最前
        new_content = header + intro + section + "\n\n" + existing

    path.write_text(new_content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 CHANGELOG 条目")
    parser.add_argument("--version", required=True, help="新版本号，如 3.0.0")
    parser.add_argument("--since-tag", default=None, help="起始 tag，默认取最新 v* tag")
    parser.add_argument("--date", default=None, help="发布日期 YYYY-MM-DD，默认今天")
    parser.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="CHANGELOG 文件路径（默认 CHANGELOG.md）",
    )
    args = parser.parse_args()

    since = args.since_tag or get_last_tag()
    if since:
        print(f"起始 tag: {since}")
    else:
        print("未找到 v* tag，将统计全部历史提交")

    commits = collect_commits(since)
    print(f"收集到 {len(commits)} 条 conventional commit")
    for ctype, subject in commits:
        print(f"  [{ctype}] {subject}")

    day = args.date or date.today().isoformat()
    section = build_section(args.version, commits, day)

    changelog = Path(args.changelog)
    insert_into_changelog(section, changelog)
    print(f"\n已写入 {changelog}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
