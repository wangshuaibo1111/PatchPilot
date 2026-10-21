"""按官方口径的判分测试（离线：喂日志，不跑真测试）。

参照 `swebench/harness/grading.py` + `log_parsers/python.py` 的语义：
  - 状态表来自 `pytest -rA` 短摘要，**不看退出码**；
  - `PASSED`/`XFAIL` 算通过；`FAILED`/`ERROR`/**`SKIPPED`** 算失败（对 F2P 而言）；
  - 状态缺失按失败处理；一条结果都解析不到 → **invalid**（不是 failed / 不是 passed）。
"""

from __future__ import annotations

import pytest

from patchpilot.eval.swebench import (
    SweInstance,
    calibrate,
    grade_fail_to_pass,
    parse_pytest_status,
    resolve_targets,
)


def write_lf(path, text: str) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- 日志解析


def test_parse_status_reads_short_summary() -> None:
    log = (
        "PASSED sympy/core/tests/test_a.py::test_ok\n"
        "FAILED sympy/core/tests/test_a.py::test_bad - AssertionError: boom\n"
        "ERROR sympy/core/tests/test_a.py::test_err\n"
        "SKIPPED [3] sympy/core/tests/test_a.py:12: reason\n"
    )

    status = parse_pytest_status(log)

    assert status["sympy/core/tests/test_a.py::test_ok"] == "PASSED"
    assert status["sympy/core/tests/test_a.py::test_bad"] == "FAILED", "` - 原因` 要切掉"
    assert status["sympy/core/tests/test_a.py::test_err"] == "ERROR"
    assert "[3]" not in status, "`SKIPPED [3] path:line` 里的 [3] 不是用例"


def test_parse_status_ignores_non_status_lines() -> None:
    log = "===== 1 passed in 0.10s =====\ncollected 1 item\n"

    assert parse_pytest_status(log) == {}


# ---------------------------------------------------------------- 判分


def _grade(tests: tuple[str, ...], targets: dict[str, tuple[str, ...]], log: str):
    return grade_fail_to_pass(tests, targets, log)


def test_grade_passes_when_all_targets_passed() -> None:
    targets = {"test_a": ("t/test_x.py::test_a",), "test_b": ("t/test_y.py::test_b",)}
    log = "PASSED t/test_x.py::test_a\nPASSED t/test_y.py::test_b\n"

    grade = _grade(("test_a", "test_b"), targets, log)

    assert grade.resolved is True
    assert grade.valid is True
    assert dict(grade.observed)["t/test_x.py::test_a"] == "PASSED", "观察结果要可审计"


def test_grade_treats_skipped_as_failure() -> None:
    """官方对 F2P 明确把 SKIPPED 当失败：跳过的题不算修好。"""
    targets = {"test_a": ("t/test_x.py::test_a",)}
    log = "SKIPPED t/test_x.py::test_a\n"

    grade = _grade(("test_a",), targets, log)

    assert grade.resolved is False
    assert grade.valid is True


def test_grade_treats_missing_status_as_failure() -> None:
    targets = {"test_a": ("t/test_x.py::test_a",), "test_b": ("t/test_y.py::test_b",)}
    log = "PASSED t/test_x.py::test_a\n"

    grade = _grade(("test_a", "test_b"), targets, log)

    assert grade.resolved is False
    assert "没有出现在日志里" in grade.reason


def test_grade_is_invalid_when_no_results_parsed() -> None:
    """**关键**：`rc=0` 但没有任何用例结果（如 `cmd.exe -m pytest`）必须是 invalid。"""
    targets = {"test_a": ("t/test_x.py::test_a",)}

    grade = _grade(("test_a",), targets, "")

    assert grade.valid is False
    assert grade.resolved is False
    assert "解析不到" in grade.reason


def test_grade_is_invalid_when_name_cannot_be_resolved() -> None:
    targets = {"test_absent": ("",)[:0]}

    grade = _grade(("test_absent",), targets, "PASSED t/test_x.py::test_other\n")

    assert grade.valid is False
    assert "找不到测试文件" in grade.reason


def test_grade_ambiguous_name_requires_no_failure() -> None:
    """同名测试在多文件时：一个通过、另一个失败 → **不算修好**（保守，不虚高）。"""
    targets = {"test_sinc": ("a/test_1.py::test_sinc", "b/test_2.py::test_sinc")}
    log = "PASSED a/test_1.py::test_sinc\nFAILED b/test_2.py::test_sinc - x\n"

    grade = _grade(("test_sinc",), targets, log)

    assert grade.resolved is False

    log_ok = "PASSED a/test_1.py::test_sinc\nPASSED b/test_2.py::test_sinc\n"
    assert _grade(("test_sinc",), targets, log_ok).resolved is True


def test_grade_accepts_xfail_as_pass() -> None:
    targets = {"test_a": ("t/test_x.py::test_a",)}

    assert _grade(("test_a",), targets, "XFAIL t/test_x.py::test_a\n").resolved is True


def test_grade_matches_bare_name_against_node_id_tail() -> None:
    """裸名核对：状态表键是完整 node id，F2P 是裸名 → 按 `::` 末段匹配。"""
    targets = {"test_sinc": ("sympy/x/tests/test_t.py::test_sinc",)}
    log = "PASSED sympy/x/tests/test_t.py::test_sinc\n"

    assert _grade(("test_sinc",), targets, log).resolved is True


# ---------------------------------------------------------------- 目标解析


def test_resolve_targets_keeps_all_candidates_on_ambiguity(tmp_path) -> None:  # noqa: ANN001
    write_lf(tmp_path / "a" / "test_1.py", "def test_sinc():\n    pass\n")
    write_lf(tmp_path / "b" / "test_2.py", "def test_sinc():\n    pass\n")
    write_lf(tmp_path / "c" / "test_3.py", "def test_other():\n    pass\n")

    targets = resolve_targets(tmp_path, ("test_sinc", "test_other"))

    assert len(targets["test_sinc"]) == 2, "歧义时必须保留全部候选，不能静默挑一个"
    assert targets["test_other"] == ("c/test_3.py::test_other",)


def test_resolve_targets_returns_empty_for_unknown_name(tmp_path) -> None:  # noqa: ANN001
    write_lf(tmp_path / "test_a.py", "def test_x():\n    pass\n")

    assert resolve_targets(tmp_path, ("test_missing",))["test_missing"] == ()


# ---------------------------------------------------------------- calibrate（此前零用例）


class _FakeRunner:
    """按顺序返回预设日志，模拟"打补丁前失败 / 打补丁后通过"。"""

    def __init__(self, logs: list[str]) -> None:
        self._logs = logs
        self.calls = 0

    def __call__(self, instance, repo_dir, node_ids):  # noqa: ANN001, ANN202
        log = self._logs[min(self.calls, len(self._logs) - 1)]
        self.calls += 1
        return 0, log


def _instance(tmp_path) -> SweInstance:  # noqa: ANN001
    write_lf(tmp_path / "test_a.py", "def test_target():\n    pass\n")
    return SweInstance(
        instance_id="x__y-1",
        repo="x/y",
        base_commit="0" * 7,
        image="",
        fail_to_pass=("test_target",),
        problem="坏了",
        test_patch="",
        python="python",
    )


def test_calibrate_accepts_gold_patch_that_flips_failure_to_pass(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """标定门的正向用例：前一次 FAILED、打 gold 后 PASSED → 通过。"""
    import patchpilot.eval.swebench as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_a.py").write_bytes(b"def test_target():\n    pass\n")
    instance = SweInstance(
        instance_id="x__y-1",
        repo="x/y",
        base_commit="0" * 7,
        image="",
        fail_to_pass=("test_target",),
        problem="坏了",
        test_patch="",
        python="python",
        gold_patch="--- a/f\n+++ b/f\n",
    )
    runner = _FakeRunner(
        ["FAILED test_a.py::test_target - x\n", "PASSED test_a.py::test_target\n"]
    )
    monkeypatch.setattr(module, "prepare_repo", lambda inst, root: repo)
    monkeypatch.setattr(module, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(module, "apply_patch_text", lambda *a, **k: True)
    monkeypatch.setattr(module, "run_tests", runner)

    ok, detail = calibrate(instance, work_root=tmp_path)

    assert ok is True, detail
    assert "失败 → 通过" in detail
    assert runner.calls == 2


def test_calibrate_rejects_when_already_green(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打补丁前就全绿 → 题目前提不成立，拒绝（否则 gold patch 贡献为零也"通过"）。"""
    import patchpilot.eval.swebench as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_a.py").write_bytes(b"def test_target():\n    pass\n")
    instance = SweInstance(
        instance_id="x__y-2",
        repo="x/y",
        base_commit="0" * 7,
        image="",
        fail_to_pass=("test_target",),
        problem="坏了",
        test_patch="",
        python="python",
        gold_patch="x",
    )
    monkeypatch.setattr(module, "prepare_repo", lambda inst, root: repo)
    monkeypatch.setattr(module, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(
        module, "run_tests", _FakeRunner(["PASSED test_a.py::test_target\n"])
    )

    ok, detail = calibrate(instance, work_root=tmp_path)

    assert ok is False
    assert "前提不成立" in detail


def test_calibrate_rejects_when_gold_patch_does_not_flip(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import patchpilot.eval.swebench as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_a.py").write_bytes(b"def test_target():\n    pass\n")
    instance = SweInstance(
        instance_id="x__y-3",
        repo="x/y",
        base_commit="0" * 7,
        image="",
        fail_to_pass=("test_target",),
        problem="坏了",
        test_patch="",
        python="python",
        gold_patch="x",
    )
    monkeypatch.setattr(module, "prepare_repo", lambda inst, root: repo)
    monkeypatch.setattr(module, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(module, "apply_patch_text", lambda *a, **k: True)
    monkeypatch.setattr(
        module, "run_tests", _FakeRunner(["FAILED test_a.py::test_target - x\n"])
    )

    ok, detail = calibrate(instance, work_root=tmp_path)

    assert ok is False
    assert "仍失败" in detail


def test_calibrate_rejects_when_gold_patch_cannot_apply(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import patchpilot.eval.swebench as module

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_a.py").write_bytes(b"def test_target():\n    pass\n")
    instance = SweInstance(
        instance_id="x__y-4",
        repo="x/y",
        base_commit="0" * 7,
        image="",
        fail_to_pass=("test_target",),
        problem="坏了",
        test_patch="",
        python="python",
        gold_patch="x",
    )
    monkeypatch.setattr(module, "prepare_repo", lambda inst, root: repo)
    monkeypatch.setattr(module, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(module, "apply_patch_text", lambda *a, **k: False)
    monkeypatch.setattr(
        module, "run_tests", _FakeRunner(["FAILED test_a.py::test_target - x\n"])
    )

    ok, detail = calibrate(instance, work_root=tmp_path)

    assert ok is False
    assert "无法应用" in detail
