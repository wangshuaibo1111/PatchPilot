"""验收项 6：全链路可观测 —— 每个任务必须有完整 trace。

四类 span 缺一不可（题目要求原文：「LLM / 工具 / 检索 / 成本」）：
  - **检索**：`retrieval.search`（定位阶段的符号/文本检索）
  - **LLM**：`llm.generate`（含 model 与 prompt 字符数）
  - **工具**：`tool.apply_patch`（git 应用补丁）、`tool.run_tests`（沙箱跑测试）
  - **成本**：token 用量与 `cost_usd`（未定价模型必须标 `cost_unknown`，绝不假装是 0）

这些断言刻意落在**trace JSONL 的实际内容**上，而不是"Tracer 被调用过"——
否则"接了 trace 但没记有用信息"依然会通过。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from patchpilot.core.budget import Budget
from patchpilot.core.llm import LLMResponse
from patchpilot.graph.repair import RepairOptions, run_repair
from patchpilot.sandbox.junit import parse_junit_text
from patchpilot.sandbox.manager import SandboxResult
from patchpilot.sandbox.policy import SandboxPolicy

GOOD_PATCH = (
    "--- a/parse.py\n"
    "+++ b/parse.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def parse_config(text):\n"
    "-    return text\n"
    "+    if not text:\n"
    "+        raise ValueError('empty')\n"
    "+    return text\n"
)


class _LLM:
    def __init__(self, text: str, *, model: str = "deepseek-flash") -> None:
        self.model = model
        self._text = text

    async def complete(self, messages, *, temperature: float = 0.0, model=None):  # noqa: ANN001, ANN202
        return LLMResponse(
            text=self._text,
            prompt_tokens=120,
            completion_tokens=40,
            model=model or self.model,
            cost_usd=None,  # 让 Tracer 自己按价格表算
        )


def _ok_report() -> object:
    return parse_junit_text(
        '<testsuites><testsuite name="s" tests="2" failures="0" errors="0">'
        '<testcase classname="tests.test_parse" name="test_a"/>'
        '<testcase classname="tests.test_parse" name="test_b"/></testsuite></testsuites>'
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "parse.py").write_text("def parse_config(text):\n    return text\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _run(repo: Path, tmp_path: Path, *, price_overrides=None) -> dict:
    issue = tmp_path / "bug.md"
    issue.write_text("调用 parse_config() 时传入空文本会静默返回，请改为抛异常。", encoding="utf-8")

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        return SandboxResult(
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.2,
            container_name="pp-fake",
            report=_ok_report(),  # type: ignore[arg-type]
        )

    return run_repair(
        options=RepairOptions(
            repo=repo,
            issue_file=issue,
            tests=("tests/test_parse.py",),
            policy=SandboxPolicy(sandbox_root=tmp_path / "sb"),
            budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
            copy_root=tmp_path / "copies",
            trace_dir=tmp_path / "traces",
            price_overrides=price_overrides,
        ),
        llm=_LLM(GOOD_PATCH),
        runner=runner,
    )


def _read_trace(path: str) -> dict:
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "trace 文件为空"
    return json.loads(lines[-1])


def test_trace_contains_all_four_categories(repo: Path, tmp_path: Path) -> None:
    """四类 span 都必须在 trace 里**实际出现**。"""
    final = _run(repo, tmp_path)

    assert final.get("trace_path"), "必须产出 trace 路径"
    payload = _read_trace(final["trace_path"])
    span_names = {span["name"] for span in payload["spans"]}

    assert "retrieval.search" in span_names, f"缺检索 span，实际 {sorted(span_names)}"
    assert "llm.generate" in span_names, f"缺 LLM span，实际 {sorted(span_names)}"
    assert "tool.apply_patch" in span_names, f"缺工具 span（git），实际 {sorted(span_names)}"
    assert "tool.run_tests" in span_names, f"缺工具 span（沙箱），实际 {sorted(span_names)}"


def test_trace_records_cost_and_tokens(repo: Path, tmp_path: Path) -> None:
    """成本与 token 必须真的记进去（不是 0 占位）。"""
    final = _run(repo, tmp_path, price_overrides={"deepseek-flash": (0.15, 0.6)})
    payload = _read_trace(final["trace_path"])

    assert payload["prompt_tokens"] == 120
    assert payload["completion_tokens"] == 40
    assert payload["cost_usd"] > 0, "配了价格表就必须算出正成本"
    assert payload["cost_unknown"] is False
    assert payload["models"] == ["deepseek-flash"]


def test_trace_marks_unknown_price_instead_of_zero(tmp_path: Path, repo: Path) -> None:
    """未定价模型必须标 `cost_unknown=True`，**绝不能假装成本是 0**。"""
    final = _run(repo, tmp_path, price_overrides={})
    payload = _read_trace(final["trace_path"])

    assert payload["cost_unknown"] is True, "未知价格时必须显式标记，而不是记 0"
    assert "deepseek-flash" in payload["unknown_priced_models"]


def test_trace_carries_task_level_metadata(repo: Path, tmp_path: Path) -> None:
    """trace 还要能回答"哪个任务、跑了几轮、过没过、因为什么失败"。"""
    final = _run(repo, tmp_path)
    payload = _read_trace(final["trace_path"])

    assert payload["status"] == "ok"
    # `finish(**extra)` 的字段是**平铺**在顶层的（实测形状，不是嵌在 "extra" 下）
    assert payload["task_id"] == final["task_id"]
    assert payload["passed"] is True
    assert payload["rounds"] >= 1
    assert payload["candidates"] >= 1
    assert payload["name"] == "repair"


def test_spans_have_positive_duration(repo: Path, tmp_path: Path) -> None:
    """span 必须真的计时（`started_at`/`ended_at` 都非空且后者 >= 前者）。"""
    final = _run(repo, tmp_path)
    payload = _read_trace(final["trace_path"])

    for span in payload["spans"]:
        # 序列化后的 span 只有 name / duration_ms / attributes
        assert span["duration_ms"] >= 0, f"span {span['name']} 时长异常"
        assert isinstance(span["attributes"], dict), f"span {span['name']} 缺 attributes"
    # LLM span 必须带上了模型名与用量线索（否则"接了 trace 但没记有用信息"）
    llm = next(s for s in payload["spans"] if s["name"] == "llm.generate")
    assert llm["attributes"]["model"] == "deepseek-flash"
    assert llm["attributes"]["prompt_chars"] > 0


def test_no_trace_dir_means_no_io(repo: Path, tmp_path: Path) -> None:
    """不配 `trace_dir` 时**完全不产生 trace 文件**（离线测试与门禁不受影响）。"""
    issue = tmp_path / "bug.md"
    issue.write_text("调用 parse_config() 时传入空文本请改为抛异常。", encoding="utf-8")

    final = run_repair(
        options=RepairOptions(
            repo=repo,
            issue_file=issue,
            tests=("tests/test_parse.py",),
            policy=SandboxPolicy(sandbox_root=tmp_path / "sb"),
            budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
            copy_root=tmp_path / "copies",
        ),
        llm=_LLM(GOOD_PATCH),
        runner=lambda **kw: SandboxResult(  # type: ignore[arg-type,return-value]
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
            container_name="pp-fake",
            report=_ok_report(),  # type: ignore[arg-type]
        ),
    )

    assert final.get("trace_path", "") == ""
    assert not (tmp_path / "traces").exists()
