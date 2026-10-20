"""P3 修复闭环的图测试：**不需要网络、不需要 Docker、不需要 API Key**。

设计上把两处外部依赖做成可注入的（`llm` 与 `runner`/`applier`），所以整条图——
定位 → 生成 → 打补丁 → 测试 → 评估 → 反思 → 重试 → 收尾——都能离线端到端验证。
真机版本（真模型 + 真 Docker 沙箱）由 `tests/test_repair_e2e.py` 覆盖。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.core.budget import Budget
from patchpilot.core.llm import LLMResponse
from patchpilot.core.models import FailureKind
from patchpilot.graph.repair import (
    RepairOptions,
    extract_patch,
    extract_terms,
    run_repair,
)
from patchpilot.sandbox.junit import parse_junit_text
from patchpilot.sandbox.manager import SandboxResult
from patchpilot.sandbox.policy import SandboxPolicy


def write_lf(path: Path, text: str) -> None:
    """写入严格 LF 的文件（`write_text` 在 Windows 上会翻成 CRLF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))



class ScriptedLLM:
    """按脚本逐个返回响应；用于精确控制"第 N 轮模型输出什么"。"""

    def __init__(self, script: list[str], *, model: str = "stub") -> None:
        self.model = model
        self._script = list(script)
        self.prompts: list[str] = []

    async def complete(self, messages, *, temperature: float = 0.0, model: str | None = None):  # noqa: ANN001
        self.prompts.append("\n".join(m.content for m in messages))
        text = self._script.pop(0) if self._script else ""
        return LLMResponse(
            text=text,
            prompt_tokens=100,
            completion_tokens=20,
            model=model or self.model,
            cost_usd=0.0,
        )


def _junit(*, passed: int, failed: int = 0, errors: int = 0) -> object:
    cases = "".join(
        f'<testcase classname="tests.test_a" name="t{i}" time="0"/>' for i in range(passed)
    )
    fails = "".join(
        '<testcase classname="tests.test_a" name="f{i}" time="0">'
        '<failure message="x"/></testcase>'
        for i in range(failed)
    )
    errs = "".join(
        '<testcase classname="tests.test_a" name="e{i}" time="0">'
        '<error message="boom"/></testcase>'
        for i in range(errors)
    )
    xml = (
        f'<testsuites><testsuite name="s" tests="{passed + failed + errors}" '
        f'failures="{failed}" errors="{errors}" skipped="0">'
        f"{cases}{fails}{errs}</testsuite></testsuites>"
    )
    return parse_junit_text(xml)


def _result(report: object) -> SandboxResult:
    return SandboxResult(
        exit_code=0 if getattr(report, "ok", False) else 1,
        stdout="",
        stderr="",
        timed_out=False,
        duration_s=0.5,
        container_name="pp-fake",
        report=report,  # type: ignore[arg-type]
    )


GOOD_PATCH = (
    "```diff\n"
    "--- a/parse.py\n"
    "+++ b/parse.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def parse_config(text):\n"
    "-    return text\n"
    "+    if text.count('=') == 0:\n"
    "+        raise ValueError('bad config')\n"
    "+    return text\n"
    "```\n"
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    write_lf(root / "parse.py", "def parse_config(text):\n    return text\n")
    return root


@pytest.fixture()
def issue_file(tmp_path: Path) -> Path:
    path = tmp_path / "bug.md"
    path.write_text(
        "调用 parse_config() 时，配置里出现重复 key 会静默返回最后一个值，"
        "希望抛出异常。\n",
        encoding="utf-8",
    )
    return path


def _options(repo: Path, issue_file: Path, tmp_path: Path, **kw: object) -> RepairOptions:
    return RepairOptions(
        repo=repo,
        issue_file=issue_file,
        policy=SandboxPolicy(sandbox_root=tmp_path / "sandbox"),
        budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
        copy_root=tmp_path / "copies",
        **kw,  # type: ignore[arg-type]
    )


def _real_applier(**kw: object) -> str:
    """把补丁真的打到副本（用 git，需要一个 git 仓库）。"""
    from patchpilot.tools import git_server

    workspace = Path(str(kw["workspace"]))
    patch = str(kw["patch"])
    git_server.apply_patch(workspace, patch, check_only=True)
    return git_server.apply_patch(workspace, patch)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    # 空仓库也要能提交（否则 "nothing to commit" 会让夹具失败）
    subprocess.run(["git", "commit", "-qm", "init", "--allow-empty"], cwd=path, check=True)


# ---------------------------------------------------------------- 纯函数：检索词与补丁抽取


def test_extract_terms_prioritises_traceback_and_calls() -> None:
    terms = extract_terms(
        '报错：File "/app/src/parser.py", line 12\n调用 parse_config() 时重复 key'
    )

    assert terms[0] == "parser.py"
    assert "parse_config" in terms


def test_extract_terms_is_deterministic_and_bounded() -> None:
    text = " ".join(f"ident{i}()" for i in range(50))

    first = extract_terms(text, limit=5)
    second = extract_terms(text, limit=5)

    assert first == second
    assert len(first) == 5


@pytest.mark.parametrize(
    "raw",
    [
        "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```",
        "```patch\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```",
        "解释一下\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_extract_patch_handles_fences_and_prose(raw: str) -> None:
    patch = extract_patch(raw)

    assert patch.startswith(("--- a/x.py", "diff --git a/x.py"))
    assert "+b" in patch


def test_extract_patch_returns_empty_when_no_diff() -> None:
    assert extract_patch("我不确定该怎么改。") == ""


# ---------------------------------------------------------------- 图：端到端（离线）


def test_repair_passes_on_first_round(repo: Path, issue_file: Path, tmp_path: Path) -> None:
    """一轮就通过：候选定位 → 生成 → 打补丁（真 git apply）→ 测试通过 → 收尾。"""
    _git_init(repo)
    llm = ScriptedLLM([GOOD_PATCH])
    calls: list[Path] = []

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        calls.append(source)
        return _result(_junit(passed=3))

    final = run_repair(
        options=_options(repo, issue_file, tmp_path),
        llm=llm,
        runner=runner,
        applier=_real_applier,
    )

    assert final["passed"] is True
    assert final["failure_kind"] == FailureKind.NONE.value
    assert final["round"] == 1
    assert "parse.py" in final["candidates"]
    assert calls, "runner 必须被调用"
    assert (repo / "parse.py").read_text(encoding="utf-8") == (
        "def parse_config(text):\n    return text\n"
    ), "**用户仓库绝不能被改动**"
    assert Path(final["final_patch_path"]).is_file()


def test_repair_retries_with_failure_memory(repo: Path, issue_file: Path, tmp_path: Path) -> None:
    """第一轮失败 → 反思产出记忆 → 第二轮通过；且第二轮 prompt 里必须带记忆。"""
    _git_init(repo)
    llm = ScriptedLLM(["不是补丁", GOOD_PATCH, GOOD_PATCH])
    # 说明：第 1 轮输出不是 diff → 记 EDIT 记忆并**立即重试**（不跑测试）；
    # 第 2 轮才带着"没有可解析 diff"的记忆产出真补丁 → 跑测试（第一次失败）→
    # 第 3 轮带失败记忆再产出补丁 → 通过。
    rounds: list[int] = []

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        rounds.append(len(rounds) + 1)
        # 第一次失败（带失败用例名），第二次通过
        return _result(_junit(passed=1, failed=1) if len(rounds) == 1 else _junit(passed=2))

    final = run_repair(
        options=_options(repo, issue_file, tmp_path),
        llm=llm,
        runner=runner,
        applier=_real_applier,
    )

    assert final["passed"] is True
    assert final["round"] == 3, f"应在第 3 轮通过，实际 {final['round']}"
    joined = "\n".join(llm.prompts)
    assert "可解析的 diff" in joined, "空补丁的失败记忆必须进下一轮 prompt"
    assert "失败用例" in joined, "测试失败的记忆必须进下一轮 prompt"
    assert final["memory"], "记忆通道应有内容"


def test_repair_gives_up_after_max_rounds(repo: Path, issue_file: Path, tmp_path: Path) -> None:
    """一直失败时必须在 max_rounds 停下，且归因为 TEST_FAIL。"""
    _git_init(repo)
    llm = ScriptedLLM([GOOD_PATCH] * 10)

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        return _result(_junit(passed=1, failed=1))

    final = run_repair(
        options=_options(repo, issue_file, tmp_path, max_rounds=2),
        llm=llm,
        runner=runner,
        applier=_real_applier,
    )

    assert final["passed"] is False
    assert final["round"] == 2, "必须在 max_rounds 停下而不是无限重试"
    assert final["failure_kind"] == FailureKind.TEST_FAIL.value


def test_repair_stops_when_patch_cannot_be_applied(
    repo: Path, issue_file: Path, tmp_path: Path
) -> None:
    """补丁打不上（畸形 diff）时：不进入测试，且记忆里记录原因。"""
    _git_init(repo)
    llm = ScriptedLLM(
        ["--- a/parse.py\n+++ b/parse.py\n@@ -1 +1 @@\n-完全不存在的内容\n+新\n"] * 3
    )
    ran: list[int] = []

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        ran.append(1)
        return _result(_junit(passed=1))

    final = run_repair(
        options=_options(repo, issue_file, tmp_path, max_rounds=1),
        llm=llm,
        runner=runner,
        applier=_real_applier,
    )

    assert final["apply_ok"] is False
    assert not ran, "补丁打不上时不应跑测试"
    assert any("补丁应用失败" in item for item in final["memory"])


def test_repair_stops_when_nothing_located(repo: Path, issue_file: Path, tmp_path: Path) -> None:
    """issue 与仓库毫无交集 → 不调用模型，直接以 LOCATE_FAIL 结束。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    _git_init(empty)
    llm = ScriptedLLM([GOOD_PATCH])

    final = run_repair(
        options=_options(empty, issue_file, tmp_path),
        llm=llm,
        runner=lambda **kw: _result(_junit(passed=1)),  # type: ignore[arg-type,return-value]
        applier=_real_applier,
    )

    assert final["failure_kind"] == FailureKind.LOCALIZE.value
    assert not llm.prompts, "定位失败时不应调用模型（省钱）"


def test_repair_records_errors_as_failure_not_success(
    repo: Path, issue_file: Path, tmp_path: Path
) -> None:
    """JUnit 里 `errors>0` 必须判为失败（夹具/收集炸掉 ≠ 通过）。"""
    _git_init(repo)
    llm = ScriptedLLM([GOOD_PATCH] * 3)

    final = run_repair(
        options=_options(repo, issue_file, tmp_path, max_rounds=1),
        llm=llm,
        runner=lambda **kw: _result(_junit(passed=1, errors=1)),  # type: ignore[arg-type,return-value]
        applier=_real_applier,
    )

    assert final["passed"] is False
    assert final["test_summary"]["errors"] == 1


def test_missing_issue_file_terminates_cleanly(tmp_path: Path, repo: Path) -> None:
    """issue 文件不存在时图要正常终止（不抛异常）。"""
    final = run_repair(
        options=_options(repo, tmp_path / "nope.md", tmp_path),
        llm=ScriptedLLM([GOOD_PATCH]),
        runner=lambda **kw: _result(_junit(passed=1)),  # type: ignore[arg-type,return-value]
        applier=_real_applier,
    )

    assert final["failure_kind"] == FailureKind.LOCALIZE.value
    assert final["issue"] == ""


def test_budget_is_enforced_across_rounds(repo: Path, issue_file: Path, tmp_path: Path) -> None:
    """token 预算触顶必须终止并写 `budget_exceeded`（不能无限重试）。"""
    _git_init(repo)
    llm = ScriptedLLM([GOOD_PATCH] * 20)

    options = RepairOptions(
        repo=repo,
        issue_file=issue_file,
        policy=SandboxPolicy(sandbox_root=tmp_path / "sandbox"),
        budget=Budget(max_tokens=250, max_attempts=0, max_seconds=0, max_usd=0),
        copy_root=tmp_path / "copies",
        max_rounds=50,
    )
    final = run_repair(
        options=options,
        llm=llm,
        runner=lambda **kw: _result(_junit(passed=1, failed=1)),  # type: ignore[arg-type,return-value]
        applier=_real_applier,
    )

    assert final["budget_exceeded"], "token 预算必须触发熔断"
    assert final["round"] < 50, "熔断后不应继续跑满 50 轮"
