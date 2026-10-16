"""在沙箱里跑 pytest 并返回结构化摘要（`test` server 的实现）。

**命名说明**：本文件叫 `test_server.py` 会让门禁把**源码**当成测试模块收集
（`src/patchpilot/tools/test_server.py` 匹配 `test_*.py`），既让"pytest 范围"多出一个
假文件，又每次产生 `PytestCollectionWarning`。故改名为 `pytest_runner.py`
——即"test server 这一层"的实现文件不再叫 `test_*`。

两层防护：
  1. **命令白名单**：只允许 `python -m pytest`（不是"过滤用户命令"，
     而是根本不接受任意命令）；
  2. **pytest 参数白名单**：只放行只读、不改写仓库、不联网的开关。

刻意**不接受**的参数举例：
  - `--rootdir` / `--confcutdir` / `-c`：能把 pytest 指向仓库之外；
  - `--junitxml` / `--html`：任意写文件（报告路径由 manager 固定）；
  - `-p`（除 `no:cacheprovider` / `no:randomly` 两个显式特例）：可加载任意插件；
  - `--basetemp`：能把临时目录指到仓库外。
"""

from __future__ import annotations

import re
from pathlib import Path

from patchpilot.sandbox.manager import SandboxResult, run_pytest_in_sandbox
from patchpilot.sandbox.policy import SandboxPolicy
from patchpilot.tools import ToolError

# 允许的 pytest 开关（无取值型）
_ALLOWED_FLAGS = frozenset(
    {
        "-q",
        "-v",
        "-x",
        "-s",
        "--quiet",
        "--verbose",
        "--exitfirst",
        "--no-header",
        "--tb=short",
        "--tb=long",
        "--tb=line",
        "--disable-warnings",
    }
)
# 允许的 pytest 开关（取值型）：后一个 token 是该开关的取值
_ALLOWED_VALUE_FLAGS = frozenset({"-k", "-m", "--maxfail", "--deselect", "--ignore"})
# 显式允许的 `-p` 特例（关闭缓存插件，避免往仓库写 .pytest_cache）
_ALLOWED_PLUGIN_DISABLES = frozenset({"no:cacheprovider", "no:randomly"})

_FORBIDDEN_PATTERNS = re.compile(
    r"(^/)|(^[A-Za-z]:[\\/])|(\.\.)|(://)|(--rootdir)|(--confcutdir)|(--basetemp)"
    r"|(--junitxml)|(--html)|(-c\b)"
)


class PytestRunnerError(ToolError):
    """pytest 运行层的错误（参数被拒 / 沙箱不可用）。"""


def validate_pytest_args(args: list[str]) -> list[str]:
    """校验并返回可安全传递给 pytest 的参数列表（默认拒绝）。"""
    if not isinstance(args, (list, tuple)):
        raise PytestRunnerError(f"args 必须是列表，实际是 {type(args).__name__}")
    cleaned: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if not isinstance(arg, str) or not arg:
            raise PytestRunnerError(f"参数必须是非空字符串: {arg!r}")
        if _FORBIDDEN_PATTERNS.search(arg):
            raise PytestRunnerError(f"参数含被禁模式（绝对路径/回溯/URL/改写型开关）: {arg!r}")
        if arg == "-p":
            if index + 1 >= len(args) or args[index + 1] not in _ALLOWED_PLUGIN_DISABLES:
                got = args[index + 1 : index + 2]
                raise PytestRunnerError(
                    f"只允许 `-p no:cacheprovider` / `-p no:randomly`，实际: {got}"
                )
            cleaned.extend([arg, args[index + 1]])
            index += 2
            continue
        if arg in _ALLOWED_VALUE_FLAGS:
            if index + 1 >= len(args):
                raise PytestRunnerError(f"{arg} 缺少取值")
            value = args[index + 1]
            if _FORBIDDEN_PATTERNS.search(value):
                raise PytestRunnerError(f"{arg} 的取值含被禁模式: {value!r}")
            cleaned.extend([arg, value])
            index += 2
            continue
        head = arg.split("=", 1)[0]
        if arg in _ALLOWED_FLAGS or head in _ALLOWED_FLAGS:
            cleaned.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            raise PytestRunnerError(f"不允许的 pytest 参数: {arg!r}")
        # 位置参数（测试文件/节点 id）必须落在仓库内的相对路径
        cleaned.append(arg)
        index += 1
    return cleaned


def run_pytest(
    *,
    policy: SandboxPolicy,
    repo: Path,
    args: list[str] | None = None,
    timeout_s: float | None = None,
    copy_root: Path | None = None,
) -> SandboxResult:
    """在沙箱副本里跑 pytest，返回结构化结果。"""
    cleaned = validate_pytest_args(list(args or []))
    # 固定加上 `-p no:cacheprovider`：避免在仓库里写 `.pytest_cache`
    command_args = ["-p", "no:cacheprovider", *cleaned]
    return run_pytest_in_sandbox(
        policy=policy,
        source=repo,
        pytest_args=command_args,
        timeout_s=timeout_s,
        copy_root=copy_root,
    )


def summarize(result: SandboxResult) -> dict[str, object]:
    """把结果压成给 Agent 看的紧凑摘要（不含完整 stdout，避免灌爆上下文）。"""
    report = result.report
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_s": round(result.duration_s, 2),
        "total": report.total if report else None,
        "passed": report.passed if report else None,
        "failed": report.failed if report else None,
        "errors": report.errors if report else None,
        "skipped": report.skipped if report else None,
        "failed_cases": [
            c.node_id for c in (report.cases if report else ()) if c.status == "failed"
        ][:20],
        "report_error": result.report_error,
    }


__all__ = ["PytestRunnerError", "run_pytest", "summarize", "validate_pytest_args"]
