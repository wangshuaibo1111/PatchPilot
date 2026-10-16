"""`fs` server 的工具：只在根目录之内读写。

约束（p1_sandbox_design.md §4）：
  - 路径白名单仅根目录；拒绝 `..`、绝对路径逃逸、symlink 跟随；
  - `write_file` 是**唯一**写操作，且必须是"新建或整体覆盖"——
    刻意不做"按行插入"这类模糊语义，便于审查与回滚。
"""

from __future__ import annotations

from pathlib import Path

from patchpilot.tools import ToolError, list_dir, read_text_file, resolve

MAX_WRITE_BYTES = 2 * 1024 * 1024


def read_file(root: Path, path: str) -> str:
    """读文本文件（上限 2 MiB）。"""
    return read_text_file(root, path)


def write_file(root: Path, path: str, content: str) -> str:
    """写入文本文件（整体覆盖），返回相对路径。

    安全点：
      - 目标必须解析到根之内（`resolve_within` 已拦绝对路径 / `..` / symlink 逃逸）；
      - **拒绝写到已存在的目录**；
      - 拒绝 symlink 目标：写穿 symlink 等于写到根外（`resolve_within` 用 `resolve()`
        归一化后已能发现逃逸，但这里额外显式拒绝"目标是链接"以便给出清晰错误）。
    """
    if not isinstance(content, str):
        raise ToolError(f"content 必须是字符串，实际是 {type(content).__name__}")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        raise ToolError(f"内容过大（> {MAX_WRITE_BYTES} 字节）")

    # 目标可能尚不存在 → must_exist=False；但父目录必须在根之内。
    target = resolve(root, path, must_exist=False)
    if target.absolute.is_dir():
        raise ToolError(f"目标是目录，不能写入: {target.relative}")
    if target.absolute.is_symlink():
        raise ToolError(f"拒绝写入符号链接（可能是逃逸路径）: {target.relative}")

    target.absolute.parent.mkdir(parents=True, exist_ok=True)
    target.absolute.write_text(content, encoding="utf-8")
    return target.relative


def list_directory(root: Path, path: str = ".") -> list[str]:
    """列出目录条目（名字，目录带尾斜杠）。"""
    return list_dir(root, path)


__all__ = ["list_directory", "read_file", "write_file"]
