"""工具层共同约定：错误类型与"根目录之内"的路径解析。

4 个 MCP server（fs / git / test / search）共享两条铁律：
  1. **路径只能落在给定的根之内**——复用 `sandbox.policy.resolve_within`，
     它已覆盖绝对路径、`..` 回溯与 symlink 逃逸（并被单测穷举过）；
  2. **只读工具绝不写盘**，写工具必须显式声明其作用范围。

本模块刻意**不依赖 Docker 也不需要 MCP 运行时**：工具函数是纯逻辑，
MCP server 只是它们的 stdio 外壳（见 `servers.py`）。这样 4 个 server 的行为
可以在没有 MCP 依赖的环境里被完整测试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from patchpilot.sandbox.policy import SandboxPolicyError, resolve_within


class ToolError(RuntimeError):
    """工具层统一异常：参数非法、越界、命令被拒。"""


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """解析后的路径及其相对根的 POSIX 形式（用于输出与日志，避免泄露宿主绝对路径）。"""

    absolute: Path
    relative: str


def resolve(root: Path, candidate: str, *, must_exist: bool = False) -> ResolvedPath:
    """把 `candidate` 解析到 `root` 之内，越界即抛 `ToolError`。

    这里把 `SandboxPolicyError` 归一化成 `ToolError`：工具层的调用方（MCP / Agent）
    只需要捕获一种异常。

    **额外拒绝 `~` 开头**：`resolve_within` 用的是 `Path.resolve()`，
    它**不做** shell 的 `~` 展开，于是 `"~/secret"` 会被当成仓库内的字面目录名
    `root/~`。虽然结果仍落在根内（不算逃逸），但"`~` 被静默当成普通目录名"是
    易误用的语义陷阱——调用方以为在指家目录。显式拒绝，让语义只有一种解释。
    """
    if isinstance(candidate, str) and candidate.startswith("~"):
        raise ToolError(f"路径不得以 `~` 开头（不做 shell 展开，语义易误用）: {candidate!r}")
    try:
        absolute = resolve_within(root, candidate, must_exist=must_exist)
    except SandboxPolicyError as exc:
        raise ToolError(str(exc)) from exc
    relative = os.path.relpath(absolute, Path(root).resolve()).replace("\\", "/")
    return ResolvedPath(absolute=absolute, relative=relative)


def read_text_file(root: Path, candidate: str, *, max_bytes: int = 2 * 1024 * 1024) -> str:
    """读一个文本文件（限制体积，避免把大文件灌进上下文）。"""
    if max_bytes <= 0:
        raise ToolError("max_bytes 必须为正数")
    target = resolve(root, candidate, must_exist=True)
    if not target.absolute.is_file():
        raise ToolError(f"不是文件: {target.relative}")
    size = target.absolute.stat().st_size
    if size > max_bytes:
        raise ToolError(f"文件过大（{size} > {max_bytes} 字节）: {target.relative}")
    try:
        return target.absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"不是 UTF-8 文本文件: {target.relative}") from exc


def list_dir(root: Path, candidate: str = ".", *, limit: int = 500) -> list[str]:
    """列目录（只返回名字，按字典序，带 limit 防止巨型目录）。"""
    if limit <= 0:
        raise ToolError("limit 必须为正数")
    target = resolve(root, candidate, must_exist=True)
    if not target.absolute.is_dir():
        raise ToolError(f"不是目录: {target.relative}")
    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "") for entry in target.absolute.iterdir()
    )
    if len(entries) > limit:
        raise ToolError(f"目录条目过多（{len(entries)} > {limit}），请缩小范围: {target.relative}")
    return entries
