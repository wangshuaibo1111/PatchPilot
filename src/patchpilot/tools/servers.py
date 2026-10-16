"""4 个 MCP server 的 stdio 入口（fs / git / test / search）。

设计取舍：
  - **工具逻辑全在 `fs_server`/`git_server`/`test_server`/`search_server` 里**，
    本模块只负责"把函数挂到 MCP 协议上"。因此 `mcp` 依赖缺失时，
    上面四个模块仍可正常 import 与单测（CI/受限环境不必装 MCP 运行时）；
  - `mcp` 采用**惰性 import**：只有真正 `build_*_server()` 时才需要它；
  - 传输以 **stdio** 为主（本地 / PC 部署），Streamable HTTP 留给远程形态（P6）。

暴露的根目录由调用方决定（默认当前工作目录）：**Agent 与 MCP server 都只能看到这个根**，
这是"工具层不理解宿主布局"的体现——根之外的一切对它不存在。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from patchpilot.sandbox.policy import SandboxPolicy
from patchpilot.tools import fs_server, git_server, pytest_runner, search_server

#: 4 个 server 的名字（也是 `pp mcp <name>` 的子命令名）
SERVER_NAMES = ("fs", "git", "test", "search")


def _require_mcp() -> Any:
    """惰性加载 MCP SDK 的 server 类，并给出可操作的报错。

    兼容两代 SDK（实测踩到过）：
      - **mcp 2.x**：`from mcp.server.mcpserver import MCPServer`（`FastMCP` 已改名）；
      - **mcp 1.x**：`from mcp.server.fastmcp import FastMCP`。
    两代的 `@server.tool()` 与 `server.run(transport="stdio")` 用法一致，
    因此这里只做"取到类"的适配，其余代码共用。
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # mcp < 2

        return FastMCP
    except ImportError as exc:  # pragma: no cover - 取决于可选依赖
        raise RuntimeError(
            '缺少 MCP 依赖，请执行 pip install "patchpilot[mcp]"（或 pip install mcp）'
        ) from exc


def build_fs_server(root: Path) -> Any:
    """只读写 `root` 之内文件的 fs server。"""
    server_class = _require_mcp()
    server = server_class("patchpilot-fs")

    @server.tool()
    def read_file(path: str) -> str:
        """读取根目录内的文本文件。"""
        return fs_server.read_file(root, path)

    @server.tool()
    def write_file(path: str, content: str) -> str:
        """整体写入根目录内的文本文件，返回相对路径。"""
        return fs_server.write_file(root, path, content)

    @server.tool()
    def list_dir(path: str = ".") -> list[str]:
        """列出根目录内的目录条目。"""
        return fs_server.list_directory(root, path)

    return server


def build_git_server(root: Path) -> Any:
    """只读查询 + `apply_patch` 的 git server（禁止 remote/push/网络）。"""
    server_class = _require_mcp()
    server = server_class("patchpilot-git")

    @server.tool()
    def status() -> str:
        """`git status --short`。"""
        return git_server.status(root)

    @server.tool()
    def diff(args: list[str] | None = None) -> str:
        """`git diff`（参数走白名单）。"""
        return git_server.diff(root, args)

    @server.tool()
    def log(limit: int = 20) -> str:
        """`git log --oneline -n <limit>`。"""
        return git_server.log(root, limit)

    @server.tool()
    def apply_patch(patch: str, check_only: bool = False) -> str:
        """应用 unified diff（`check_only=True` 时只校验不落盘）。"""
        return git_server.apply_patch(root, patch, check_only=check_only)

    return server


def build_test_server(policy: SandboxPolicy, repo: Path, copy_root: Path | None = None) -> Any:
    """在沙箱里跑 pytest 的 test server。"""
    server_class = _require_mcp()
    server = server_class("patchpilot-test")

    @server.tool()
    def run_pytest(args: list[str] | None = None, timeout_s: float | None = None) -> dict[str, Any]:
        """在沙箱副本里跑 pytest，返回结构化摘要（含逐用例失败列表）。"""
        result = pytest_runner.run_pytest(
            policy=policy, repo=repo, args=args, timeout_s=timeout_s, copy_root=copy_root
        )
        return pytest_runner.summarize(result)

    return server


def build_search_server(root: Path) -> Any:
    """只读检索的 search server。"""
    server_class = _require_mcp()
    server = server_class("patchpilot-search")

    @server.tool()
    def search_text(
        pattern: str, subdir: str = ".", regex: bool = False, max_results: int = 100
    ) -> list[dict[str, Any]]:
        """在根目录内检索文本（默认字面子串匹配）。"""
        return [
            {"path": m.path, "line": m.line, "text": m.text}
            for m in search_server.search_text(
                root, pattern, subdir=subdir, regex=regex, max_results=max_results
            )
        ]

    @server.tool()
    def search_symbol(name: str, subdir: str = ".") -> list[dict[str, Any]]:
        """按名字查找符号定义（P2 将替换为符号索引实现，接口不变）。"""
        return [
            {"path": m.path, "line": m.line, "text": m.text}
            for m in search_server.search_symbol(root, name, subdir=subdir)
        ]

    return server


__all__ = [
    "SERVER_NAMES",
    "build_fs_server",
    "build_git_server",
    "build_search_server",
    "build_test_server",
]
