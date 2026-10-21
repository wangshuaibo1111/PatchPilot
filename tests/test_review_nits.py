"""第三十二轮复审 NON_BLOCKING 里几条**真实缺陷**的钉子。

它们的共同点是"行为早就偏了，但没人守"：`limit=0` 会返回 1 条、语料逃逸校验只有一条路径有用例、
`_run_git` 是否真把隔离环境传下去只靠源码文本断言（把 `env=env` 删掉仍全绿）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.index.lexical import BM25Index
from patchpilot.index.retrieval import Localizer
from patchpilot.index.symbols import Chunk, index_repo
from patchpilot.index.vectors import VectorIndex


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- limit=0 必须是空


def test_vector_search_limit_zero_returns_nothing() -> None:
    chunks = (Chunk(path="a.py", start_line=1, end_line=2, text="alpha beta"),)

    assert VectorIndex(chunks).search("alpha", limit=0) == []


def test_bm25_limit_zero_returns_nothing() -> None:
    chunks = (Chunk(path="a.py", start_line=1, end_line=2, text="alpha beta"),)

    assert BM25Index(chunks).search("alpha", limit=0) == []


def test_localize_limit_zero_returns_nothing(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1\n")
    index = index_repo(tmp_path)

    assert Localizer(index).localize("handler", limit=0) == []


# ---------------------------------------------------------------- 两条语料路径都要挡住逃逸


def _corpus(tmp_path: Path, rel: str, *, expected: list[str]) -> Path:
    path = tmp_path / "corpus.json"
    body = (
        '{"tasks": [{"id": "t1", "files": {"'
        + rel
        + '": "x = 1\\n", "Issue.md": "坏了"}, '
        + '"issue_file": "Issue.md", "expected_files": ['
        + ", ".join(f'"{item}"' for item in expected)
        + "]}]}"
    )
    path.write_bytes(body.encode("utf-8"))
    return path


def test_cases_from_locate_corpus_rejects_escape(tmp_path: Path) -> None:
    from patchpilot.eval.locate_bench import cases_from_locate_corpus

    corpus = _corpus(tmp_path, "../escaped.py", expected=["../escaped.py"])

    with pytest.raises(ValueError, match="逃逸"):
        cases_from_locate_corpus(corpus)


def test_cases_from_corpus_rejects_escape(tmp_path: Path) -> None:
    """`cases_from_corpus` 的同源校验此前**没有任何用例**（删掉仍全绿）。"""
    from patchpilot.eval.locate_bench import cases_from_corpus

    corpus = _corpus(tmp_path, "../escaped.py", expected=["../escaped.py"])

    with pytest.raises(ValueError, match="逃逸"):
        cases_from_corpus(corpus)


# ---------------------------------------------------------------- git 隔离环境真的传下去了


def test_run_git_passes_the_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 `_run_git` 里的 `env=env` 删掉曾经仍全绿——这里改成行为断言。"""
    import patchpilot.tools.git_server as module

    captured: dict[str, object] = {}

    class _Fake:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return _Fake()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._run_git(tmp_path, ["status"])

    assert "env" in captured, "必须把隔离环境传给子进程"
    env = captured["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] == str(tmp_path)


def test_git_subprocesses_really_succeed_with_isolation(tmp_path: Path) -> None:
    """端到端：隔离环境不影响正常 git 操作（否则隔离项会变成"用不了"）。"""
    from patchpilot.tools.git_server import _run_git

    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "core.autocrlf", "false"],
    ):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    write_lf(tmp_path / "a.py", "x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True, capture_output=True)

    out = _run_git(tmp_path, ["log", "--pretty=format:%s"])

    assert "init" in out
