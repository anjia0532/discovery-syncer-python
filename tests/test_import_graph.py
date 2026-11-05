"""静态回归：确保生产代码不再依赖 funboost / Redis，且项目内 import 目标都存在。

用 AST 而非真实 import，避免测试环境需要安装 nb_log / db_libs / APScheduler。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 允许出现的第三方顶层模块（与 requirements.txt 对应）
THIRD_PARTY = {
    "fastapi", "starlette", "uvicorn", "yaml", "pytest", "httpx", "pydantic",
    "pydantic_settings", "pydantic_yaml", "sqlalchemy", "db_libs", "nb_log",
    "requests", "apscheduler", "cachetools", "dotenv", "async_timeout",
    "elasticsearch", "elasticsearch7", "elasticsearch8", "elasticsearch9",
    "pymysql", "pythonjsonlogger", "BeautifulReport", "typing_extensions",
    # 可选/按需安装：gunicorn 是 main.py 里注释掉的备用部署方式；
    # git(GitPython) 只被 tools/backup.py 使用，README 说明单独安装。
    "gunicorn", "git",
    # 集群模式依赖（cluster.enabled=true 才真正用到），已在 requirements 声明
    "redis",
}
LOCAL_TOPS = {"app", "core", "cfg", "nb_log_config", "main", "misc", "tools"}

# 明确禁止的依赖（funboost / persist_queue 已彻底移除）。
# redis 是例外：v3.0.1 起作为**可选**集群模式依赖，只允许在
# app/runtime/cluster.py 里延迟导入，其余位置一律禁止。
FORBIDDEN = {"funboost", "persist_queue"}
# 允许 import redis 的模块白名单（相对仓库根的 POSIX 路径）
REDIS_ALLOWED = {"app/runtime/cluster.py"}


def _production_files():
    """排除测试、缓存、虚拟环境与 .workbuddy 目录。"""
    for path in sorted(ROOT.rglob("*.py")):
        text = str(path).replace("\\", "/")
        if any(skip in text for skip in (
            "__pycache__", "/tests/", "/.workbuddy/",
            "/.venv/", "/venv/", "/site-packages/", "/node_modules/",
        )):
            continue
        yield path


def _modules(node: ast.AST):
    """从一个 import 节点里取出被引用的模块名。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level and node.module:
        return [node.module]
    return []


def _resolve(module: str) -> bool:
    """模块名是否对应仓库里真实存在的 .py 或包。"""
    rel = Path(*module.split("."))
    return (ROOT / rel.with_suffix(".py")).exists() or (ROOT / rel / "__init__.py").exists()


def test_no_funboost_or_redis_imports():
    """funboost / persist_queue 彻底禁止；redis 仅允许集群模块延迟导入。"""
    violations = []
    for path in _production_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            for module in _modules(node):
                top = module.split(".")[0]
                if top in FORBIDDEN:
                    violations.append((rel, node.lineno, module))
                elif top == "redis" and rel not in REDIS_ALLOWED:
                    violations.append((rel, node.lineno, module))
    assert not violations, f"存在被禁止的依赖 import: {violations}"


def test_all_absolute_project_imports_resolve():
    """所有 app.* / core.* 绝对 import 的目标文件必须存在。"""
    missing = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            for module in _modules(node):
                top = module.split(".")[0]
                if top in LOCAL_TOPS and not _resolve(module):
                    missing.append((str(path.relative_to(ROOT)), node.lineno, module))
    assert not missing, f"import 目标不存在（可能引用了已删除文件）: {missing}"


def test_all_relative_imports_resolve():
    """相对 import（如 from .consume import ...）也要能解析到现存模块。"""
    missing = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            parts = list(path.relative_to(ROOT).parent.parts)
            for _ in range(node.level - 1):
                if parts:
                    parts.pop()
            target = ".".join(x for x in [*parts, node.module or ""] if x)
            if target.split(".")[0] in {"app", "core"} and not _resolve(target):
                missing.append((str(path.relative_to(ROOT)), node.lineno, target))
    assert not missing, f"相对 import 目标不存在: {missing}"


def test_requirements_declared_for_imports():
    """生产代码 import 的第三方顶层模块，要么在标准库，要么已声明/允许。"""
    stdlib = set(sys.stdlib_module_names)
    unknown = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            for module in _modules(node):
                top = module.split(".")[0]
                if top in LOCAL_TOPS or top in THIRD_PARTY or top in stdlib or top in FORBIDDEN:
                    continue
                unknown.append((str(path.relative_to(ROOT)), node.lineno, module))
    assert not unknown, f"未在 requirements.txt 声明的第三方依赖: {unknown}"
