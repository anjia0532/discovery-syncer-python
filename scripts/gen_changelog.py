#!/usr/bin/env python3
"""从上一个 tag 到 HEAD 的提交生成 CHANGELOG 条目。

用法：
    python3 scripts/gen_changelog.py --version 3.0.2 [--since-tag v3.0.1] \\
        [--date 2026-11-06] [--body-out release-notes.md] [--skip-changelog]

行为：
* 取 ``since_tag..HEAD`` 的提交，解析 conventional commit 类型；
* 按 feat/fix/refactor/perf/test/docs/build/ci/chore 分组，生成 Keep a Changelog 段落；
* ``--body-out PATH``：把**仅本版本**的段落单独写到 PATH（供 GitHub Release 正文用，
  不累积历史版本）；
* ``--skip-changelog``：不修改累积的 CHANGELOG.md（tag 已存在、仅需刷新正文时用）；
* 默认（未加 ``--skip-changelog``）：把本版本段落**插入 CHANGELOG.md 顶部**，
  CHANGELOG.md 始终保持完整版本历史。

since-tag 选取：显式 ``--since-tag`` 优先；否则自动取「版本号 < 当前版本」的
最大 tag。这样即使当前 tag 已存在，也不会把自己当成 since 导致段落为空。

只依赖标准库 + git，可在 CI 与本地 WSL 直接运行。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# conventional commit 类型 -> 分组标题
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


def ver_tuple(tag: str) -> tuple[int, ...]:
    """把 tag/version 解析成可比较的数字元组。

    去掉前缀 ``v`` 与预发布后缀（``3.0.0-beta.1`` -> ``(3, 0, 0)``），
    非数字段按 0 处理，保证排序与比较不因后缀崩溃。
    """
    core = tag.lstrip("vV").split("-")[0]
    return tuple(int(x) if x.isdigit() else 0 for x in core.split("."))


def get_last_tag(before: tuple[int, ...] | None = None) -> str:
    """返回最新 semver tag；给定 ``before`` 时返回版本号严格小于它的最大 tag。

    Args:
        before: 当前版本的数字元组。提供时排除当前及更高的 tag，
            避免 tag 已存在时把自身当作 since 而生成空段落。

    Returns:
        tag 名（如 ``v3.0.1``）；无匹配则空串。
    """
    out = run_git("tag", "--list", "v*").strip()
    if not out:
        return ""
    tags = [t for t in out.splitlines() if t.startswith("v")]
    if before is not None:
        tags = [t for t in tags if ver_tuple(t) < before]
    if not tags:
        return ""
    return max(tags, key=ver_tuple)


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
    parser.add_argument("--version", required=True, help="新版本号，如 3.0.2")
    parser.add_argument("--since-tag", default=None, help="起始 tag，默认取版本号 < 当前版本的最大 tag")
    parser.add_argument("--date", default=None, help="发布日期 YYYY-MM-DD，默认今天")
    parser.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        help="CHANGELOG 文件路径（默认 CHANGELOG.md）",
    )
    parser.add_argument(
        "--body-out",
        default=None,
        help="把**仅本版本**段落写到该文件（GitHub Release 正文，不累积历史版本）",
    )
    parser.add_argument(
        "--skip-changelog",
        action="store_true",
        help="不修改累积的 CHANGELOG.md（tag 已存在、仅需刷新正文时用）",
    )
    args = parser.parse_args()

    before = ver_tuple(args.version)
    since = args.since_tag or get_last_tag(before=before)
    if since:
        print(f"起始 tag: {since}")
    else:
        print("未找到更早的 v* tag，将统计全部历史提交")

    commits = collect_commits(since)
    print(f"收集到 {len(commits)} 条 conventional commit")
    for ctype, subject in commits:
        print(f"  [{ctype}] {subject}")

    day = args.date or date.today().isoformat()
    section = build_section(args.version, commits, day)

    # 仅本版正文 -> 独立文件（GitHub Release body 用它，不累积）
    if args.body_out:
        Path(args.body_out).write_text(section + "\n", encoding="utf-8")
        print(f"\n已写出版本正文: {args.body_out}")

    # 累积历史 -> CHANGELOG.md
    if not args.skip_changelog:
        insert_into_changelog(section, Path(args.changelog))
        print(f"已写入 {args.changelog}")
    else:
        print("已跳过 CHANGELOG.md（--skip-changelog）")
    return 0


if __name__ == "__main__":
    sys.exit(main())