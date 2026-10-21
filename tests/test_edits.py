"""`tools/edits.py` 的测试：search/replace 块与降级应用顺序。

对应真机数据：11 题里 9 题卡在"补丁无法应用"。这里的用例守三件事：
  1. 编辑块能被正确解析与应用（含多个块、含前后杂文本）；
  2. 逐字节精确匹配失败时能退化到"忽略行尾空白"匹配，但仍然**保守**（不确定就不改）；
  3. 全都失败时给出**可读原因**，而不是笼统的"打不上"。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.tools.edits import (
    apply_edit_blocks,
    apply_model_output,
    parse_edit_blocks,
)


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


BLOCK = """```python
mod.py
<<<<<<< SEARCH
def handler():
    return 1
=======
def handler():
    return 2
>>>>>>> REPLACE
```
"""


# ---------------------------------------------------------------- 解析


def test_parse_single_block() -> None:
    blocks = parse_edit_blocks(BLOCK)

    assert len(blocks) == 1
    assert blocks[0].path == "mod.py"
    assert blocks[0].search == "def handler():\n    return 1"
    assert blocks[0].replace == "def handler():\n    return 2"


def test_parse_multiple_blocks() -> None:
    text = (
        "a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
        "b.py\n<<<<<<< SEARCH\ny = 1\n=======\ny = 3\n>>>>>>> REPLACE\n"
    )

    blocks = parse_edit_blocks(text)

    assert [b.path for b in blocks] == ["a.py", "b.py"]


def test_parse_ignores_unclosed_block() -> None:
    text = "mod.py\n<<<<<<< SEARCH\ndef f():\n    pass\n"

    assert parse_edit_blocks(text) == []


# ---------------------------------------------------------------- 应用


def test_apply_exact_match(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1\n")

    applied, errors = apply_edit_blocks(tmp_path, parse_edit_blocks(BLOCK))

    assert (applied, errors) == (1, [])
    assert (tmp_path / "mod.py").read_bytes() == b"def handler():\n    return 2\n"


def test_apply_tolerates_trailing_whitespace_drift(tmp_path: Path) -> None:
    """模型常多打行尾空格；按字节匹配会失败，退化匹配应救回来。"""
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1   \n")

    applied, errors = apply_edit_blocks(tmp_path, parse_edit_blocks(BLOCK))

    assert (applied, errors) == (1, [])
    content = (tmp_path / "mod.py").read_text(encoding="utf-8")
    assert "return 2" in content


def test_apply_reports_unmatched_block_precisely(tmp_path: Path) -> None:
    """匹配不上时必须报出文件与搜索块开头，而不是笼统失败。"""
    write_lf(tmp_path / "mod.py", "def other():\n    return 9\n")

    applied, errors = apply_edit_blocks(tmp_path, parse_edit_blocks(BLOCK))

    assert applied == 0
    assert len(errors) == 1
    assert "mod.py" in errors[0]
    assert "搜索块未匹配" in errors[0]
    assert "return 1" in errors[0]


def test_apply_refuses_to_escape_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    write_lf(outside, "x = 1\n")
    root = tmp_path / "repo"
    write_lf(root / "mod.py", "x = 1\n")
    text = "../outside.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"

    applied, errors = apply_edit_blocks(root, parse_edit_blocks(text))

    assert applied == 0
    assert "逃逸" in errors[0]
    assert outside.read_bytes() == b"x = 1\n", "仓库外文件不得被改动"


# ---------------------------------------------------------------- 降级顺序


def _git_init(root: Path) -> None:
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "core.autocrlf", "false"],
    ):
        subprocess.run(cmd, cwd=root, check=True, capture_output=True)


def test_unified_diff_is_preferred(tmp_path: Path) -> None:
    _git_init(tmp_path)
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1\n")
    patch = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n"
        " def handler():\n-    return 1\n+    return 2\n"
    )

    result = apply_model_output(tmp_path, patch)

    assert result.applied is True
    assert result.method == "git-apply"
    assert result.files == ("mod.py",)


def test_falls_back_to_search_replace_when_diff_absent(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1\n")

    result = apply_model_output(tmp_path, BLOCK)

    assert result.applied is True
    assert result.method == "search-replace"
    assert "return 2" in (tmp_path / "mod.py").read_text(encoding="utf-8")


def test_reports_both_failure_reasons(tmp_path: Path) -> None:
    """diff 打不上、编辑块也匹配不了时，两边的失败原因都要留下。"""
    _git_init(tmp_path)
    write_lf(tmp_path / "mod.py", "def other():\n    return 9\n")
    bad_patch = (
        "--- a/mod.py\n+++ b/mod.py\n@@ -1,2 +1,2 @@\n"
        " def handler():\n-    return 1\n+    return 2\n"
    )

    result = apply_model_output(tmp_path, BLOCK, patch=bad_patch)

    assert result.applied is False
    assert "git apply" in result.detail
    assert "search/replace" in result.detail
    assert "搜索块未匹配" in result.detail


def test_reports_when_output_has_neither_format(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "x = 1\n")

    result = apply_model_output(tmp_path, "我觉得应该改一下 mod.py 的返回值。")

    assert result.applied is False
    assert "既没有 unified diff 也没有 search/replace" in result.detail


@pytest.mark.parametrize("extra", ["", "解释如下：\n"])
def test_apply_ignores_surrounding_prose(tmp_path: Path, extra: str) -> None:
    write_lf(tmp_path / "mod.py", "def handler():\n    return 1\n")

    result = apply_model_output(tmp_path, extra + BLOCK + "\n以上。")

    assert result.applied is True
