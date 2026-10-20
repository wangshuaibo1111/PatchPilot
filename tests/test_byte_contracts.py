"""守"字节级写入"与"git 环境隔离"两组守卫（第三十一轮复审点出的两条无钉项）。

复审用变异证明了它们**当前正确但没有任何用例能钉住**：
  - 把 `write_text_lf` 退回 `Path.write_text` → 全套 562 passed（SURVIVED）；
  - 把 `_git_env` 清成 `{"PATH": ...}` → 全套 SURVIVED。

这类"守卫无钉"本身不是缺陷，但会让同一类问题在下一轮无声复发。因此这里**直接断言字节**
（而不是断言"函数被调用过"），并断言环境变量的**取值**。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.core.textio import write_text_lf


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- write_text_lf 本身


def test_write_text_lf_writes_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "a.txt"

    written = write_text_lf(target, "x = 1\ny = 2\n")

    assert target.read_bytes() == b"x = 1\ny = 2\n"
    assert written == 12


def test_write_text_lf_does_not_translate_line_endings(tmp_path: Path) -> None:
    """**这是它存在的理由**：`Path.write_text` 在 Windows 上会把 `\\n` 变成 `\\r\\n`。

    如果退回 `write_text`，本用例会看到 `b'a\\r\\n'` 而失败。
    """
    target = tmp_path / "lf.txt"

    write_text_lf(target, "a\nb\n")

    assert target.read_bytes() == b"a\nb\n"
    assert b"\r\n" not in target.read_bytes()


def test_write_text_lf_preserves_existing_crlf(tmp_path: Path) -> None:
    """它**不做规范化**：调用方给 `\\r\\n` 就写 `\\r\\n`（不是 `\\r\\r\\n`）。"""
    target = tmp_path / "crlf.txt"

    write_text_lf(target, "a\r\nb\n")

    assert target.read_bytes() == b"a\r\nb\n"


# ---------------------------------------------------------------- 三个调用点的字节断言


def test_fs_write_file_writes_exact_bytes(tmp_path: Path) -> None:
    """调用点 1：`fs_server.write_file`。"""
    from patchpilot.tools import fs_server

    root = tmp_path / "repo"
    root.mkdir()

    fs_server.write_file(root, "src/mod.py", "def f():\n    return 1\n")

    assert (root / "src" / "mod.py").read_bytes() == b"def f():\n    return 1\n"


def test_fs_write_file_preserves_caller_crlf(tmp_path: Path) -> None:
    """契约：调用方给的 `\\r\\n` 原样保留（旧的 `write_text` 会变成 `\\r\\r\\n`）。"""
    from patchpilot.tools import fs_server

    root = tmp_path / "repo"
    root.mkdir()

    fs_server.write_file(root, "x.txt", "a\r\nb\n")

    assert (root / "x.txt").read_bytes() == b"a\r\nb\n"


def test_eval_materialize_writes_lf_bytes(tmp_path: Path) -> None:
    """调用点 2：评测语料物化（若变 CRLF，LF 补丁就打不上）。"""
    from patchpilot.eval.dataset import EvalTask

    task = EvalTask(
        id="t1",
        repo=None,
        issue_file=None,
        fail_to_pass=("tests/test_x.py::test_y",),
        files={"mod.py": "x = 1\ny = 2\n", "Issue.md": "坏了\n"},
        raw={"issue_file": "Issue.md"},
    )
    destination = tmp_path / "out"

    _, issue_path = task.materialize(destination)

    assert (destination / "mod.py").read_bytes() == b"x = 1\ny = 2\n"
    assert issue_path.read_bytes() == "坏了\n".encode()


def test_eval_materialize_rejects_path_traversal(tmp_path: Path) -> None:
    """语料里的相对路径必须落在目标目录内（复审点出的包含校验缺口）。"""
    from patchpilot.eval.dataset import DatasetError, EvalTask

    task = EvalTask(
        id="evil",
        repo=None,
        issue_file=None,
        fail_to_pass=("tests/test_x.py::test_y",),
        files={"../escaped.py": "x = 1\n"},
        raw={"issue_file": "../escaped.py"},
    )

    with pytest.raises(DatasetError, match="逃逸"):
        task.materialize(tmp_path / "out")

    assert not (tmp_path / "escaped.py").exists(), "不得写到目标目录之外"


def test_repair_finalize_writes_the_patch_byte_for_byte(tmp_path: Path) -> None:
    """调用点 3：闭环把补丁落盘成 `.diff`——必须与输入逐字节相同。"""
    from patchpilot.core.budget import Budget
    from patchpilot.core.llm import LLMResponse
    from patchpilot.graph.repair import RepairOptions, run_repair
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult
    from patchpilot.sandbox.policy import SandboxPolicy

    repo = tmp_path / "repo"
    write_lf(repo / "mod.py", "def handler():\n    return 1\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    patch = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def handler():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    issue = tmp_path / "bug.md"
    write_lf(issue, "调用 handler() 返回值不对。")

    class _LLM:
        model = "stub"

        async def complete(self, messages, *, temperature=0.0, model=None):  # noqa: ANN001, ANN202
            return LLMResponse(
                text=patch, prompt_tokens=10, completion_tokens=5, model="stub", cost_usd=0.0
            )

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        return SandboxResult(
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
            container_name="pp-fake",
            report=parse_junit_text(
                '<testsuites><testsuite name="s" tests="1" failures="0" errors="0">'
                '<testcase classname="t" name="t"/></testsuite></testsuites>'
            ),
        )

    final = run_repair(
        options=RepairOptions(
            repo=repo,
            issue_file=issue,
            tests=("tests/test_x.py",),
            policy=SandboxPolicy(sandbox_root=tmp_path / "sb"),
            budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
            copy_root=tmp_path / "copies",
            output_dir=tmp_path / "patches",
        ),
        llm=_LLM(),
        runner=runner,
    )

    dumped = Path(final["final_patch_path"]).read_bytes()
    assert dumped == patch.encode("utf-8"), "落盘的 .diff 必须与补丁逐字节相同"
    assert b"\r\n" not in dumped


# ---------------------------------------------------------------- _git_env 的结构断言


def test_git_env_strips_credentials_and_host_config(tmp_path: Path) -> None:
    """`_git_env` 是"剥凭据 / 不交互 / 不吃宿主 gitconfig"的守卫，必须逐项钉住。

    复审把整个函数清成 `{"PATH": ...}` 后全套仍绿（SURVIVED）——说明此前没人守它。
    """
    from patchpilot.tools.git_server import _git_env

    env = _git_env(tmp_path)

    assert env["GIT_TERMINAL_PROMPT"] == "0", "必须禁止交互式索要凭据"
    assert env["GIT_ASKPASS"] == "", "必须禁用外部 askpass"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1", "必须忽略系统级 gitconfig（可能含 insteadOf 改写）"
    assert env["HOME"] == str(tmp_path), "HOME 指向仓库根，让 git 找不到宿主 ~/.gitconfig"
    assert "GIT_CONFIG_GLOBAL" in env, "必须显式指向不存在的全局配置"
    assert "PATH" in env, "仍需继承 PATH 才能找到 git"


def test_all_git_subprocesses_use_the_shared_env(tmp_path: Path) -> None:
    """三处 git 调用必须共用 `_git_env`（避免某处漏掉隔离项）。"""
    import inspect

    import patchpilot.tools.git_server as module

    for name in ("_run_git", "_run_git_with_stdin", "apply_patch", "_files_git_would_write"):
        source = inspect.getsource(getattr(module, name))
        uses_shared = "_git_env(" in source or "_run_git_with_stdin(" in source
        assert uses_shared, f"{name} 没有走 `_git_env` / `_run_git_with_stdin`"


# ---------------------------------------------------------------- manager 的 stdout 兜底


def test_docker_available_survives_unreadable_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stdout is None` 时不能抛 `AttributeError`（诊断路径不该二次崩）。"""
    import patchpilot.sandbox.manager as manager

    class _Fake:
        returncode = 0
        stdout = None
        stderr = None

    monkeypatch.setattr(manager.subprocess, "run", lambda *a, **k: _Fake())

    ok, detail = manager.docker_available()

    assert ok is True
    assert detail == "", f"stdout 不可读时应退化为空字符串，实际 {detail!r}"


def test_docker_available_specifies_utf8_encoding() -> None:
    """结构化断言：`docker info` 必须显式 UTF-8（否则 cp936 下输出会静默清空）。"""
    import inspect

    import patchpilot.sandbox.manager as manager

    source = inspect.getsource(manager.docker_available)
    assert 'encoding="utf-8"' in source
    assert 'errors="replace"' in source


def test_cli_docker_status_survives_unreadable_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchpilot.cli as cli

    class _Fake:
        returncode = 0
        stdout = None
        stderr = None

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _Fake())

    ok, detail = cli._docker_daemon_status()

    assert ok is True
    assert detail == "ServerVersion="
