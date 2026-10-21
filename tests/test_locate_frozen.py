"""冻结题集的测试：这是 BLOCKING 4 的钉子。

复审的指控是"落盘数字没有任何已提交状态能复现"——根因是题源读 `git log`（分母漂）
且索引读活工作区（分子漂）。这里守三件事：
  1. 冻结文件读回来必须与文件内容逐一相等；
  2. 出题必须按**该题自己的 commit** 取代码树，而不是读当前工作区；
  3. 在活工作区里改东西，**不影响**基准结果。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from patchpilot.eval.locate_bench import (
    cases_from_frozen,
    freeze_history_cases,
    run_locate_bench,
)


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """两个提交的小仓库：每个提交改一个不同的文件。"""
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    write_lf(root / "src" / "pkg" / "alpha.py", "def alpha_fn():\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "添加 alpha_fn 的实现")
    write_lf(root / "src" / "pkg" / "beta.py", "def beta_fn():\n    return 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "添加 beta_fn 的实现")
    return root


def freeze(repo: Path) -> dict:
    scope = repo / "src" / "pkg"
    return freeze_history_cases(git_root=repo, scope=scope, limit=20)


def test_freeze_captures_query_and_expected(repo: Path) -> None:
    payload = freeze(repo)

    assert len(payload["cases"]) == 2
    for case in payload["cases"]:
        assert case["commit"], "必须钉住 commit"
        assert case["expected_files"], "必须记录期望文件"
        assert "fn" in case["query"], "issue 文本必须一起冻结（否则还要读 git log）"
    assert payload["scope"] == "src/pkg", "scope 要落成相对路径，便于携带"


def test_frozen_cases_round_trip_exactly(repo: Path, tmp_path: Path) -> None:
    """读回来的 id 与期望集必须与文件逐项相等（防"每次跑题集都不一样"）。"""
    payload = freeze(repo)
    frozen = tmp_path / "cases.json"
    write_lf(frozen, json.dumps(payload, ensure_ascii=False))

    cases = cases_from_frozen(frozen, git_root=repo)

    assert [c.id for c in cases] == [c["id"] for c in payload["cases"]]
    by_id = {c.id: c for c in cases}
    for entry in payload["cases"]:
        assert by_id[entry["id"]].expected_files == frozenset(entry["expected_files"])
        assert by_id[entry["id"]].query == entry["query"]


def test_frozen_case_indexes_its_own_commit_not_the_worktree(
    repo: Path, tmp_path: Path
) -> None:
    """**核心钉子**：第 1 题（alpha）的索引里不该出现第 2 个提交才加的 `beta.py`。"""
    payload = freeze(repo)
    frozen = tmp_path / "cases.json"
    write_lf(frozen, json.dumps(payload, ensure_ascii=False))

    cases = cases_from_frozen(frozen, git_root=repo)
    first, second = cases[0], cases[1]  # git log 新→旧：先 beta 后 alpha

    assert first.index.file("beta.py") is not None
    assert first.index.file("alpha.py") is not None
    assert second.index.file("alpha.py") is not None
    assert second.index.file("beta.py") is None, (
        "alpha 那个 commit 里还没有 beta.py；若出现说明又读了活工作区"
    )


def test_benchmark_result_ignores_worktree_changes(repo: Path, tmp_path: Path) -> None:
    """工作区改动（未提交或已提交）都不得改变冻结题集跑出来的数字。"""
    scope = repo / "src" / "pkg"
    payload = freeze(repo)
    frozen = tmp_path / "cases.json"
    write_lf(frozen, json.dumps(payload, ensure_ascii=False))

    def run() -> tuple:
        outcome = run_locate_bench(
            git_root=repo, scope=scope, frozen_path=frozen, limit=5
        )
        return tuple(
            (s.config, s.group, s.total, round(s.top1, 6), round(s.mrr, 6))
            for s in outcome.scores
        )

    before = run()

    # 未提交的改动 + 已提交的改动都试一遍
    write_lf(scope / "gamma.py", "def gamma_fn():\n    return 3\n")
    assert run() == before, "未提交改动不应该影响冻结基准"

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "添加 gamma_fn 的实现")
    assert run() == before, "新提交不应该影响冻结基准"


def test_shipped_frozen_cases_are_well_formed() -> None:
    """入库的冻结题集必须结构自洽，并且 scope 指向真实目录。"""
    repo_root = Path(__file__).resolve().parents[1]
    frozen = repo_root / "eval" / "locate_cases.json"
    assert frozen.is_file(), "冻结题集应已入库"

    payload = json.loads(frozen.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) >= 10, f"题数太少（{len(cases)}）"
    assert (repo_root / payload["scope"]).is_dir(), "scope 必须是真实存在的目录"
    assert "冻结" in str(payload.get("note") or ""), "要在文件里写明这是冻结题集"

    for case in cases:
        assert len(case["commit"]) == 40, f"{case['id']}: commit 必须是完整 sha"
        assert case["expected_files"], f"{case['id']}: 期望集不能为空"
        assert case["query"].strip(), f"{case['id']}: issue 文本不能为空"
