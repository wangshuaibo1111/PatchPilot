"""执行层：Docker 沙箱、资源限额、安全策略。

设计要点（见 `.tmp/p1_sandbox_design.md` 与 `docs/adr/`）：
  - 把「安全参数怎么拼」（纯函数、可穷举测试）与「容器怎么起」（薄驱动）彻底分开，
    使**安全性的核心部分不依赖 Docker 就能验证**；
  - 每任务一个容器：`--network none`、非 root、只读 rootfs + tmpfs 工作区、
    `--cap-drop ALL --security-opt no-new-privileges`、CPU/内存/pids 限额；
  - 绝不挂载宿主仓库原目录：先导出 base commit 到副本，只挂副本；
    链接类对象（符号链接与 Windows junction）在**复制阶段**就必须处理；
  - 输出只走两条通道：stdout/stderr 与约定的结果文件（JUnit XML），
    不做双向目录同步。

本包已实现（纯逻辑，无需 Docker）：
  - ``policy``    ：安全策略与 `docker run` 参数生成 + 最后一道防线校验
  - ``workspace`` ：工作区安全复制（不复制链接、剥离 hooks/凭据）
  - ``junit``     ：JUnit XML → 结构化测试结果

待 P1 实现：``manager``（真正调用 Docker 的驱动）、4 个 MCP server（fs/git/test/search）。
"""

from __future__ import annotations

from patchpilot.sandbox.junit import (
    JUnitParseError,
    JUnitReport,
    TestCaseOutcome,
    parse_junit_text,
    parse_junit_xml,
)
from patchpilot.sandbox.policy import (
    SandboxPolicy,
    SandboxPolicyError,
    assert_required_invariants,
    assert_safe,
    build_run_args,
    find_escaping_links,
    is_link_like,
    resolve_within,
)
from patchpilot.sandbox.workspace import (
    WorkspaceCopyError,
    WorkspaceCopyResult,
    copy_workspace,
)

__all__ = [
    "JUnitParseError",
    "JUnitReport",
    "SandboxPolicy",
    "SandboxPolicyError",
    "TestCaseOutcome",
    "WorkspaceCopyError",
    "WorkspaceCopyResult",
    "assert_required_invariants",
    "assert_safe",
    "build_run_args",
    "copy_workspace",
    "find_escaping_links",
    "is_link_like",
    "parse_junit_text",
    "parse_junit_xml",
    "resolve_within",
]
