"""评测层测试：数据集校验、指标计算、`invalid_task` 判定。

**不需要网络、不需要 Docker、不需要模型**：所有外部依赖都可注入。
真机版本由 `tests/test_eval_real.py` 覆盖（缺依赖自动 skip）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchpilot.core.llm import LLMResponse
from patchpilot.eval.dataset import DatasetError, EvalTask, load_tasks, validate_task
from patchpilot.eval.runner import EvalReport, TaskOutcome
from patchpilot.sandbox.policy import DEFAULT_IMAGE


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_task_dir(tmp_path: Path, name: str = "t1") -> Path:
    repo = tmp_path / name
    _write(repo / "mod.py", "def compute():\n    return 1\n")
    _write(
        repo / "tests" / "test_mod.py",
        "from mod import compute\n\n\ndef test_compute():\n    assert compute() == 2\n",
    )
    _write(repo / "Issue.md", "调用 compute() 返回 1，期望 2。")
    return repo


def _task_line(repo: Path, **over: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "t1",
        "repo": repo.name,
        "issue_file": f"{repo.name}/Issue.md",
        "fail_to_pass": ["tests/test_mod.py::test_compute"],
        "image": DEFAULT_IMAGE,
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------- 数据集加载与校验


def test_load_tasks_reads_jsonl(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    path = _write(tmp_path / "d.jsonl", json.dumps(_task_line(repo)) + "\n")

    tasks = load_tasks(path)

    assert len(tasks) == 1
    assert tasks[0].id == "t1"
    assert tasks[0].fail_to_pass == ("tests/test_mod.py::test_compute",)
    assert tasks[0].repo.is_dir()


def test_load_tasks_skips_blank_and_comment_lines(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    content = (
        "# 这是注释\n\n" + json.dumps(_task_line(repo)) + "\n\n"
    )
    path = _write(tmp_path / "d.jsonl", content)

    assert len(load_tasks(path)) == 1


def test_load_tasks_rejects_duplicate_ids(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    line = json.dumps(_task_line(repo))
    path = _write(tmp_path / "d.jsonl", f"{line}\n{line}\n")

    with pytest.raises(DatasetError, match="id 重复"):
        load_tasks(path)


def test_load_tasks_rejects_missing_required_field(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    payload = _task_line(repo)
    del payload["fail_to_pass"]
    path = _write(tmp_path / "d.jsonl", json.dumps(payload) + "\n")

    with pytest.raises(DatasetError, match="缺字段"):
        load_tasks(path)


def test_load_tasks_rejects_bad_json(tmp_path: Path) -> None:
    path = _write(tmp_path / "d.jsonl", "{not json}\n")

    with pytest.raises(DatasetError, match="JSON 解析失败"):
        load_tasks(path)


def test_load_tasks_rejects_empty_dataset(tmp_path: Path) -> None:
    path = _write(tmp_path / "d.jsonl", "\n\n")

    with pytest.raises(DatasetError, match="为空"):
        load_tasks(path)


def test_empty_fail_to_pass_is_rejected(tmp_path: Path) -> None:
    """`fail_to_pass` 为空 → 无法判定"修好了"，必须在加载期拒绝。

    这是度量衡的正确性问题：题目写错会让所有指标失去意义，
    所以宁可拒绝加载也不要"跑完再怀疑数字"。
    """
    repo = _make_task_dir(tmp_path)
    path = _write(tmp_path / "d.jsonl", json.dumps(_task_line(repo, fail_to_pass=[])) + "\n")

    with pytest.raises(DatasetError, match="fail_to_pass 不能为空"):
        load_tasks(path)


def test_latest_image_tag_is_rejected(tmp_path: Path) -> None:
    """镜像必须锁定 tag（可复现纪律）。"""
    repo = _make_task_dir(tmp_path)
    path = _write(
        tmp_path / "d.jsonl", json.dumps(_task_line(repo, image="python:latest")) + "\n"
    )

    with pytest.raises(DatasetError, match="latest"):
        load_tasks(path)


def test_untagged_image_is_rejected(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    path = _write(tmp_path / "d.jsonl", json.dumps(_task_line(repo, image="python")) + "\n")

    with pytest.raises(DatasetError, match="锁定 tag"):
        load_tasks(path)


def test_missing_repo_directory_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "d.jsonl",
        json.dumps(
            {
                "id": "t1",
                "repo": "nope",
                "issue_file": "nope/Issue.md",
                "fail_to_pass": ["tests/x.py::y"],
                "image": DEFAULT_IMAGE,
            }
        )
        + "\n",
    )

    with pytest.raises(DatasetError, match="repo 目录不存在"):
        load_tasks(path)


def test_pytest_args_merges_and_dedupes(tmp_path: Path) -> None:
    task = EvalTask(
        id="t",
        repo=tmp_path,
        issue_file=tmp_path / "Issue.md",
        fail_to_pass=("a::x", "b::y"),
        pass_to_pass=("b::y", "c::z"),
    )

    assert task.pytest_args() == ("a::x", "b::y", "c::z")


def test_validate_task_accepts_a_good_task(tmp_path: Path) -> None:
    repo = _make_task_dir(tmp_path)
    validate_task(
        EvalTask(
            id="ok",
            repo=repo,
            issue_file=repo / "Issue.md",
            fail_to_pass=("tests/test_mod.py::test_compute",),
        )
    )


# ---------------------------------------------------------------- 指标计算


def _outcome(status: str, **kw: object) -> TaskOutcome:
    return TaskOutcome(task_id=f"t-{status}", status=status, **kw)  # type: ignore[arg-type]


def test_pass_at_1_excludes_invalid_tasks() -> None:
    """`invalid_task` 必须从分母里排除——否则"题目写错"会被算成"没修好"。"""
    report = EvalReport(
        dataset="d",
        total=4,
        outcomes=(
            _outcome("passed"),
            _outcome("passed"),
            _outcome("failed", failure_kind="test_fail"),
            _outcome("invalid_task"),
        ),
    )

    assert report.passed == 2
    assert report.valid == 3
    assert report.pass_at_1 == pytest.approx(2 / 3)


def test_pass_at_1_is_zero_for_empty_valid_set() -> None:
    report = EvalReport(dataset="d", total=1, outcomes=(_outcome("invalid_task"),))

    assert report.valid == 0
    assert report.pass_at_1 == 0.0


def test_cost_and_tokens_are_summed() -> None:
    report = EvalReport(
        dataset="d",
        total=3,
        outcomes=(
            _outcome("passed", cost_usd=0.5, prompt_tokens=100, completion_tokens=20),
            _outcome("passed", cost_usd=0.25, prompt_tokens=50, completion_tokens=10),
            _outcome("failed", cost_usd=0.0, prompt_tokens=0, completion_tokens=0),
        ),
    )

    assert report.total_cost_usd == pytest.approx(0.75)
    assert report.total_tokens == 180


def test_p95_falls_back_to_max_for_small_samples() -> None:
    """样本 < 5 时 P95 退化为最大值，避免用插值伪造精度。"""
    report = EvalReport(
        dataset="d",
        total=3,
        outcomes=(
            _outcome("passed", duration_s=1.0),
            _outcome("passed", duration_s=9.0),
            _outcome("passed", duration_s=5.0),
        ),
    )

    assert report.latency_p95() == 9.0


def test_p95_uses_the_upper_tail_for_larger_samples() -> None:
    durations = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    report = EvalReport(
        dataset="d",
        total=len(durations),
        outcomes=tuple(_outcome("passed", duration_s=d) for d in durations),
    )

    p95 = report.latency_p95()

    assert p95 >= 9.0, f"P95 应落在高端而非中位数，实际 {p95}"


def test_failure_histogram_uses_failure_kind_for_failures() -> None:
    report = EvalReport(
        dataset="d",
        total=3,
        outcomes=(
            _outcome("passed"),
            _outcome("failed", failure_kind="test_fail"),
            _outcome("failed", failure_kind="localize"),
        ),
    )

    hist = report.failure_histogram()

    assert hist["passed"] == 1
    assert hist["test_fail"] == 1
    assert hist["localize"] == 1


def test_report_dict_contains_reproducibility_metadata() -> None:
    """验收项 4：报告必须带题目 id / 温度 / commit / 镜像 tag。"""
    report = EvalReport(
        dataset="d",
        total=1,
        outcomes=(_outcome("passed"),),
        meta={"temperature": 0.0, "commit": "abc123", "image": "img:1", "task_ids": ["t"]},
    )

    payload = report.to_dict()

    assert payload["meta"]["temperature"] == 0.0
    assert payload["meta"]["commit"] == "abc123"
    assert payload["meta"]["image"] == "img:1"
    assert payload["meta"]["task_ids"] == ["t"]
    assert payload["metrics"]["pass@1"] == 1.0


# ---------------------------------------------------------------- 图状态通道


def test_repair_state_declares_workspace_channel() -> None:
    """`workspace` 必须是**已声明**的状态通道。

    LangGraph 会**静默丢弃**未声明的键：早先 `apply` 把副本路径写进 `workspace`，
    但 `RepairState` 里没声明 → `run_tests` 拿到的恒为空 → 悄悄回落到**未打补丁**的
    源仓库 → 表现为"每轮都失败，但产出的补丁其实完全正确"。
    真机评测实测：6 道题全 failed，且每轮补丁逐字符相同。
    """
    from patchpilot.graph.repair import RepairState

    assert "workspace" in RepairState.__annotations__
    assert "attempts" in RepairState.__annotations__, (
        "`attempts` 缺失会让预算的 max_attempts 永不触发（实测跑到脚本耗尽）"
    )


class _RecordingLLM:
    def __init__(self, responses: list[str]) -> None:
        self.model = "stub"
        self._responses = list(responses)

    async def complete(self, messages, *, temperature: float = 0.0, model=None):  # noqa: ANN001, ANN202
        text = self._responses.pop(0) if self._responses else ""
        return LLMResponse(
            text=text, prompt_tokens=10, completion_tokens=5, model="stub", cost_usd=0.0
        )


def test_run_tests_receives_the_patched_workspace(tmp_path: Path) -> None:
    """端到端钉住上面的通道问题：`runner` 收到的 `source` 必须是**打了补丁的副本**。

    这是"评测数字可信"的前提——若 `runner` 拿到的是源仓库，那么无论模型修得对不对，
    结果都只会反映"源仓库还有 bug"。
    """
    import subprocess

    from patchpilot.core.budget import Budget
    from patchpilot.graph.repair import RepairOptions, run_repair
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult
    from patchpilot.sandbox.policy import SandboxPolicy

    repo = _make_task_dir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    patch = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def compute():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    seen: list[Path] = []

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        seen.append(source)
        report = parse_junit_text(
            '<testsuites><testsuite name="s" tests="1" failures="0" errors="0">'
            '<testcase classname="tests.test_mod" name="test_compute"/></testsuite></testsuites>'
        )
        ok = "    return 2" in (source / "mod.py").read_text(encoding="utf-8")
        if not ok:
            report = parse_junit_text(
                '<testsuites><testsuite name="s" tests="1" failures="1" errors="0">'
                '<testcase classname="tests.test_mod" name="test_f">'
                '<failure message="x"/></testcase></testsuite></testsuites>'
            )
        return SandboxResult(
            exit_code=0 if ok else 1,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
            container_name="pp-fake",
            report=report,
        )

    final = run_repair(
        options=RepairOptions(
            repo=repo,
            issue_file=repo / "Issue.md",
            tests=("tests/test_mod.py::test_compute",),
            policy=SandboxPolicy(sandbox_root=tmp_path / "sb"),
            budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
            copy_root=tmp_path / "copies",
        ),
        llm=_RecordingLLM([patch]),
        runner=runner,
    )

    assert seen, "runner 必须被调用"
    assert seen[0] != repo, "runner 绝不能拿到源仓库（必须是打了补丁的副本）"
    assert (seen[0] / "mod.py").read_text(encoding="utf-8").count("return 2") == 1, (
        "runner 拿到的副本里必须已包含补丁"
    )
    assert final["passed"] is True
