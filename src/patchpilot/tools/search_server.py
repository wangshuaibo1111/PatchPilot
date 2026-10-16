"""`search` server 的工具：只读的文本/符号检索。

P1 阶段用**纯标准库**实现（`re` + 逐行扫描），不引入向量库：
  - `search_text`：子串或正则检索，返回 `路径:行号:内容`；
  - `search_symbol`：按语言无关的**定义模式**（`def`/`class`/`function`/`const`…）找符号。

为什么不用 tree-sitter / BM25：那是 **P2** 的范围（`PLAN` §5 的 P2 = 符号表 + 三路召回）。
本阶段只需要"Agent 能在仓库里找到东西"，把接口先定下来，P2 再替换实现——
接口不变是刻意的（`search_symbol` 的返回结构 P2 直接复用）。

安全：所有读取都走 `tools.resolve`（根之内 + 拒绝逃逸）；跳过二进制文件与常见重目录。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from patchpilot.sandbox.policy import is_link_like
from patchpilot.tools import ToolError, resolve

# 默认跳过的目录（与 workspace 复制阶段的排除项保持一致）
SKIP_DIRS = frozenset(
    {
        ".git",
        ".patchpilot",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
    }
)

# 常见语言的"定义"形态；P2 换成 tree-sitter 后这套模式会被替换
_SYMBOL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)"),
    re.compile(r"^\s*class\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*="),
    re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+(?P<name>\w+)"),
)

MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Match:
    """一条命中：相对路径 + 1 基行号 + 该行内容（截断到 300 字符）。"""

    path: str
    line: int
    text: str


def _iter_files(root: Path, subdir: str, *, limit_files: int) -> list[Path]:
    """在根之内枚举文件，**不跟随任何链接类对象**。

    为什么不能直接用 `rglob("*")`（第二十四轮真机逃逸）：
    Windows **junction** 的 `is_symlink()` 是 `False`，`rglob` 会**递归进入**它；
    于是仓库里一个指向外部的 junction 就能把 `~/.ssh/id_rsa` 这类文件带进
    Agent 上下文 / 日志 / 模型输入。实测：`search_text(root, "PRIVATE KEY")`
    返回了 `<root>/linked/id_rsa`（`linked` 是指向宿主目录的 junction），
    而同一个路径经 `fs_server.read_file` 是**正确被拒**的——只有 search 漏。
    本模块的 docstring 声称"所有读取都走 `tools.resolve`（根之内 + 拒绝逃逸）"，
    枚举路径也必须守同一条规矩。

    做法（照抄 `workspace._scan` / `policy.find_escaping_links` 的成熟口径）：
      - 手工 `os.walk` 并**按目录剪枝**：遇到链接类目录（symlink **与 junction**，
        用 `policy.is_link_like` 判定）就整棵跳过；
      - 每个候选文件再用 `resolve()` 复核一次"仍在根内"——纵深防御，
        即使将来枚举逻辑被改坏也不会读穿。
    """
    base = resolve(root, subdir, must_exist=True)
    if not base.absolute.is_dir():
        raise ToolError(f"不是目录: {base.relative}")
    resolved_root = root.resolve()
    files: list[Path] = []

    for current, dirnames, filenames in os.walk(base.absolute, followlinks=False):
        current_path = Path(current)
        # **剪枝**：把链接类目录（symlink / junction）与重目录从遍历中移除
        kept: list[str] = []
        for name in sorted(dirnames):
            child = current_path / name
            if name in SKIP_DIRS or is_link_like(child):
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            if len(files) >= limit_files:
                return files
            path = current_path / name
            # 只比对**相对根**的那部分路径：比对绝对路径会把宿主目录名也算进来
            # （门禁 basetemp 在 `<repo>/.patchpilot/` 下，含 `.patchpilot`，
            # 会导致整个仓库被跳过、搜索恒空——这个 bug 是门禁自己抓出来的）。
            try:
                parts = path.relative_to(resolved_root).parts
            except ValueError:
                continue
            if any(part in SKIP_DIRS for part in parts):
                continue
            if is_link_like(path):
                continue
            try:
                if not path.is_file():
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                # 纵深防御：解析后必须仍在根内（不跟随链接，故正常情况恒真）
                if not path.resolve().is_relative_to(resolved_root):
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def search_text(
    root: Path,
    pattern: str,
    *,
    subdir: str = ".",
    regex: bool = False,
    max_results: int = 100,
) -> list[Match]:
    """在根之内的 `subdir` 下检索文本。

    `regex=False`（默认）按**字面子串**匹配，避免用户模式里的元字符意外生效；
    `regex=True` 时按正则匹配（调用方需自负模式风险——只在沙箱副本上读，不会执行内容）。
    """
    if not pattern:
        raise ToolError("pattern 不能为空")
    if max_results <= 0:
        raise ToolError("max_results 必须为正数")
    matcher = re.compile(pattern) if regex else None

    matches: list[Match] = []
    for path in _iter_files(root, subdir, limit_files=5000):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制/不可读文件直接跳过
        relative = path.relative_to(root.resolve()).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            hit = matcher.search(line) if matcher else (pattern in line)
            if hit:
                matches.append(Match(path=relative, line=number, text=line.strip()[:300]))
                if len(matches) >= max_results:
                    return matches
    return matches


def search_symbol(root: Path, name: str, *, subdir: str = ".") -> list[Match]:
    """按名字找**定义**（大小写不敏感的子串匹配），P2 会被符号表替换。"""
    if not name:
        raise ToolError("name 不能为空")
    needle = name.lower()
    matches: list[Match] = []
    for path in _iter_files(root, subdir, limit_files=5000):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(root.resolve()).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern in _SYMBOL_PATTERNS:
                found = pattern.match(line)
                if found and needle in found.group("name").lower():
                    matches.append(Match(path=relative, line=number, text=line.strip()[:300]))
                    break
    return matches


__all__ = ["Match", "search_symbol", "search_text"]
