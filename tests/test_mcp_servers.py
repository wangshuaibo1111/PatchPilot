"""MCP server 的**协议层**测试：真的起进程、走 stdio、列工具、调工具。

为什么值得单独测（而不是只测工具函数）：
  - 工具函数正确 ≠ MCP 接线正确（曾经踩到 `mcp 2.x` 把 `FastMCP` 改名成 `MCPServer`，
    工厂函数会直接抛 `RuntimeError`）；
  - "越界被拒"必须在**协议层**也成立（错误要通过 JSON-RPC 回给客户端，而不是崩掉进程）。

依赖缺失时 `skip`：`mcp` 是可选项（`pip install "patchpilot[mcp]"`）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from patchpilot.tools import servers

mcp = pytest.importorskip("mcp", reason='需要 MCP 依赖：pip install "patchpilot[mcp]"')


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(
        "def target_fn():\n    return 42\n\n\nclass Widget:\n    pass\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def test_server_names_are_pinned() -> None:
    """4 个 server 的名字是 `pp mcp <name>` 的契约，必须**硬编码**钉住。"""
    assert servers.SERVER_NAMES == ("fs", "git", "test", "search")


@pytest.mark.parametrize("name", ["fs", "git", "search"])
def test_each_server_builds_without_docker(name: str, tmp_path: Path) -> None:
    """无 Docker 的 3 个 server 必须能构造（test server 需要 policy，单独测）。"""
    root = _make_repo(tmp_path)
    builders = {
        "fs": servers.build_fs_server,
        "git": servers.build_git_server,
        "search": servers.build_search_server,
    }

    server = builders[name](root)

    assert type(server).__name__ in {"MCPServer", "FastMCP"}, "两代 SDK 都应被适配"


def test_test_server_builds(tmp_path: Path) -> None:
    from patchpilot.sandbox.policy import SandboxPolicy

    root = _make_repo(tmp_path)
    server = servers.build_test_server(SandboxPolicy(sandbox_root=tmp_path), root)

    assert type(server).__name__ in {"MCPServer", "FastMCP"}


def _server_params(name: str, root: Path) -> object:
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "patchpilot.cli", "mcp", name, "--root", str(root)],
        env={"PYTHONIOENCODING": "utf-8"},
    )


async def _call(name: str, root: Path, tool: str, arguments: dict) -> object:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with (
        stdio_client(_server_params(name, root)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(tool, arguments)


async def _list_tools(name: str, root: Path) -> set[str]:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with (
        stdio_client(_server_params(name, root)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        return {tool.name for tool in listed.tools}


EXPECTED_TOOLS: dict[str, set[str]] = {
    "fs": {"read_file", "write_file", "list_dir"},
    "git": {"status", "diff", "log", "apply_patch"},
    "search": {"search_text", "search_symbol"},
}


@pytest.mark.parametrize(("name", "expected"), sorted(EXPECTED_TOOLS.items()))
async def test_servers_expose_their_tools_over_stdio(
    name: str, expected: set[str], tmp_path: Path
) -> None:
    """**硬编码锚点**：3 个 server 暴露的工具集合必须与上表逐项相符。"""
    root = _make_repo(tmp_path)

    assert await _list_tools(name, root) == expected


async def test_fs_tool_roundtrip_over_stdio(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)

    result = await _call("fs", root, "read_file", {"path": "pkg/mod.py"})

    assert result.is_error is False
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    assert "target_fn" in text


async def test_out_of_root_access_is_rejected_over_stdio(tmp_path: Path) -> None:
    """越界必须在**协议层**以错误返回（不能崩进程、也不能读到文件）。"""
    root = _make_repo(tmp_path)

    result = await _call("fs", root, "read_file", {"path": "../../../etc/passwd"})

    assert result.is_error is True
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    assert "passwd" not in text or "拒绝" in text or "路径" in text


def test_mcp_extra_is_declared() -> None:
    """`pyproject` 必须声明 mcp 可选依赖，否则 `pip install "patchpilot[mcp]"` 会失败。"""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'mcp = ["mcp' in pyproject.read_text(encoding="utf-8")


def test_test_server_cli_uses_a_dedicated_sandbox_root(tmp_path: Path, monkeypatch) -> None:
    """`pp mcp test` 的副本根必须落到**专用沙箱根**，而不是仓库父目录。

    第二十四轮非阻塞项：原先把副本根默认设成"仓库父目录"，
    进程被强杀时会把 `copy-*` 残留在用户的工程目录里。
    另外这条把 `cli.py` 的 `test` 分支纳入覆盖（此前零覆盖）。

    第二十五轮修正：先前只断言 `is_relative_to(tempfile.gettempdir())` ——
    而 `tmp_path` 本身就在系统临时目录下，**"仓库父目录"同样满足**，
    于是把实现改回去也全绿。现在直接断言专用根的**名字**。
    """
    from patchpilot import cli as cli_module
    from patchpilot.tools import servers

    captured: dict[str, object] = {}

    def fake_build_test_server(policy: object, repo: Path, copy_root: Path | None = None):
        captured["policy"] = policy
        captured["copy_root"] = copy_root
        raise RuntimeError("stop-here")  # 不必真的起 server

    monkeypatch.setattr(servers, "build_test_server", fake_build_test_server)

    with pytest.raises(RuntimeError, match="stop-here"):
        cli_module.mcp_server(name="test", root=tmp_path, copy_root=None)

    root_used = captured["copy_root"]
    assert root_used is not None
    resolved = Path(root_used).resolve()
    assert resolved.name == "patchpilot-sandbox", (
        f"必须落在专用的沙箱根（第二十五轮：只断言 is_relative_to(tempdir) 钉不住修复——"
        f"仓库父目录同样成立），实际 {resolved}"
    )
    assert resolved != tmp_path.resolve()
