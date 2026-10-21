"""`tools/edits.py` 的测试：**逐字节**钉住"不许损坏文件"的契约。

第三十三轮复审用可复现实验证明了旧实现会**静默损坏文件**且仍报 `applied=True`：
  ① `"    return 10"` 里搜 `"return 1"` → 写成 `"    return 20"`（子串命中）
  ② 行内起匹配（`alue = 1`）同样会改坏
  ③ 容错分支吞掉匹配末行的换行（`GAMMA\ndelta` → `GAMMAdelta`）
  ④ `read_text` 归一换行 + `write_bytes` 整写 → 一次单点编辑把整个 CRLF 文件变 LF
  ⑤ `errors="replace"` 会把非 UTF-8 字节换成 U+FFFD 再写回（原字节永久丢失）

所以这里的断言一律用 `read_bytes()`，并且**失败时要求文件一字未动**。
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


def block(path: str, search: str, replace: str) -> str:
    return f"{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


# ---------------------------------------------------------------- 解析


def test_parse_single_block() -> None:
    blocks = parse_edit_blocks(block("mod.py", "def handler():\n    return 1", "x"))

    assert len(blocks) == 1
    assert blocks[0].path == "mod.py"
    assert blocks[0].search == "def handler():\n    return 1"
    assert blocks[0].replace == "x"


def test_parse_multiple_blocks() -> None:
    text = block("a.py", "x = 1", "x = 2") + block("b.py", "y = 1", "y = 3")

    assert [b.path for b in parse_edit_blocks(text)] == ["a.py", "b.py"]


def test_parse_ignores_unclosed_block() -> None:
    assert parse_edit_blocks("mod.py\n<<<<<<< SEARCH\ndef f():\n    pass\n") == []


def test_parse_tolerates_surrounding_prose() -> None:
    text = "解释一下：\n" + block("mod.py", "a = 1", "a = 2") + "\n以上。"

    blocks = parse_edit_blocks(text)

    assert len(blocks) == 1 and blocks[0].path == "mod.py"


# ---------------------------------------------------------------- 精确落盘（逐字节）


def test_replace_in_the_middle_keeps_surrounding_bytes(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "alpha\nbeta\ngamma\ndelta\n")

    result = apply_model_output(tmp_path, block("mod.py", "beta", "BETA"))

    assert result.applied is True
    assert result.applied_blocks == (("mod.py", "exact", "2-2"),)
    assert (tmp_path / "mod.py").read_bytes() == b"alpha\nBETA\ngamma\ndelta\n"


def test_last_line_without_trailing_newline_stays_without(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "a = 1\nb = 2")

    result = apply_model_output(tmp_path, block("mod.py", "b = 2", "b = 20"))

    assert result.applied is True
    assert (tmp_path / "mod.py").read_bytes() == b"a = 1\nb = 20"


# ---------------------------------------------------------------- ① 不许子串命中


def test_substring_search_is_refused_not_applied(tmp_path: Path) -> None:
    """`return 1` 出现在 `return 10` 里时**不能**改写成 `return 20`。"""
    original = b"    return 10\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "return 1", "return 2"))

    assert result.applied is False
    assert "未匹配" in result.detail
    assert (tmp_path / "mod.py").read_bytes() == original, "失败时文件必须一字未动"


def test_inline_search_is_refused(tmp_path: Path) -> None:
    """`alue = 1` 不是一整行，不能命中 `value = 1 # keep`。"""
    original = b"value = 1 # keep\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "alue = 1", "value = 99"))

    assert result.applied is False
    assert (tmp_path / "mod.py").read_bytes() == original


# ---------------------------------------------------------------- ③ 不许吞换行


def test_whitespace_tolerant_match_keeps_the_trailing_newline(tmp_path: Path) -> None:
    """复审实测：旧实现 `beta\\ngamma` → `BETA\\nGAMMAdelta`（吞掉了换行）。"""
    write_lf(tmp_path / "mod.py", "alpha\nbeta \ngamma\ndelta\n")

    result = apply_model_output(tmp_path, block("mod.py", "beta\ngamma", "BETA\nGAMMA"))

    assert result.applied is True
    assert result.applied_blocks == (("mod.py", "whitespace-tolerant", "2-3"),)
    assert (tmp_path / "mod.py").read_bytes() == b"alpha\nBETA\nGAMMA\ndelta\n"


# ---------------------------------------------------------------- ④ 行尾风格必须保留


def test_crlf_file_stays_crlf(tmp_path: Path) -> None:
    """复审实测：旧实现会让整个 CRLF 文件变成 LF（整文件 diff，用户无法接受）。"""
    original = b"a = 1\r\nb = 2\r\nc = 3\r\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "b = 2", "b = 20"))

    assert result.applied is True
    assert (tmp_path / "mod.py").read_bytes() == b"a = 1\r\nb = 20\r\nc = 3\r\n"


def test_lf_file_stays_lf(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "a = 1\nb = 2\n")

    apply_model_output(tmp_path, block("mod.py", "b = 2", "b = 20"))

    assert (tmp_path / "mod.py").read_bytes() == b"a = 1\nb = 20\n"


# ---------------------------------------------------------------- ⑤ 非 UTF-8 必须拒绝


def test_non_utf8_file_is_refused_not_rewritten(tmp_path: Path) -> None:
    original = b"a = 1\n\xff\xfe\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "a = 1", "a = 2"))

    assert result.applied is False
    assert "UTF-8" in result.detail
    assert (tmp_path / "mod.py").read_bytes() == original, "原字节必须原样保留"


# ---------------------------------------------------------------- 唯一性与空块


def test_ambiguous_whole_line_is_refused(tmp_path: Path) -> None:
    original = b"x = 1\ny = 0\nx = 1\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "x = 1", "x = 2"))

    assert result.applied is False
    assert "命中 2 处" in result.detail
    assert (tmp_path / "mod.py").read_bytes() == original


def test_whitespace_only_search_is_refused(tmp_path: Path) -> None:
    original = b"a = 1\n\nb = 2\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "   ", "INSERTED"))

    assert result.applied is False
    assert "纯空白" in result.detail
    assert (tmp_path / "mod.py").read_bytes() == original


# ---------------------------------------------------------------- 原子性与语法门


def test_multi_block_is_atomic(tmp_path: Path) -> None:
    """第 2 块失败时，第 1 块也不能落盘（否则留下半应用仓库）。"""
    original = b"a = 1\nb = 2\n"
    (tmp_path / "mod.py").write_bytes(original)
    text = block("mod.py", "a = 1", "a = 10") + block("mod.py", "not_there", "x")

    result = apply_model_output(tmp_path, text)

    assert result.applied is False
    assert (tmp_path / "mod.py").read_bytes() == original, "原子性：一块失败则全不写"


def test_syntax_error_is_refused_for_python(tmp_path: Path) -> None:
    original = b"def f():\n    return 1\n"
    (tmp_path / "mod.py").write_bytes(original)

    result = apply_model_output(tmp_path, block("mod.py", "    return 1", "    return ("))

    assert result.applied is False
    assert "无法解析" in result.detail
    assert (tmp_path / "mod.py").read_bytes() == original


def test_syntax_gate_can_be_disabled(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "def f():\n    return 1\n")
    blocks = parse_edit_blocks(block("mod.py", "    return 1", "    return ("))

    applied, errors, _done = apply_edit_blocks(
        tmp_path, blocks, require_valid_python=False
    )

    assert applied == 1 and errors == []


# ---------------------------------------------------------------- 路径约束


def test_refuses_to_escape_repo(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    write_lf(outside, "x = 1\n")
    root = tmp_path / "repo"
    write_lf(root / "mod.py", "x = 1\n")

    result = apply_model_output(root, block("../outside.py", "x = 1", "x = 2"))

    assert result.applied is False
    assert "逃逸" in result.detail
    assert outside.read_bytes() == b"x = 1\n"


def test_missing_file_is_reported(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "x = 1\n")

    result = apply_model_output(tmp_path, block("nope.py", "x = 1", "x = 2"))

    assert result.applied is False
    assert "文件不存在" in result.detail


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


def test_edit_block_with_diff_like_lines_still_uses_blocks(tmp_path: Path) -> None:
    """编辑块内容里出现 `--- ` / `+++ ` 时不能被误判成 diff（改用行锚定判断）。"""
    write_lf(tmp_path / "mod.md", "---\ntitle: a\n---\n")
    text = block("mod.md", "title: a", "title: b")

    result = apply_model_output(tmp_path, text)

    assert result.applied is True
    assert result.method == "search-replace"


def test_reports_when_output_has_neither_format(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "x = 1\n")

    result = apply_model_output(tmp_path, "我觉得应该改一下 mod.py 的返回值。")

    assert result.applied is False
    assert "既没有 unified diff 也没有 search/replace" in result.detail


# ---------------------------------------------------------------- 报告细节


def test_touched_files_covers_new_delete_and_rename() -> None:
    from patchpilot.tools.edits import _touched_files

    new_file = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    delete = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    rename = "diff --git a/old.py b/new2.py\nsimilarity index 100%\nrename to new2.py\n"

    assert _touched_files(new_file) == ("new.py",)
    assert _touched_files(delete) == ("gone.py",)
    assert _touched_files(rename) == ("new2.py",)


@pytest.mark.parametrize("suffix", [".py", ".txt"])
def test_applied_blocks_are_auditable(tmp_path: Path, suffix: str) -> None:
    write_lf(tmp_path / f"mod{suffix}", "a = 1\nb = 2\n")

    result = apply_model_output(tmp_path, block(f"mod{suffix}", "b = 2", "b = 20"))

    assert result.applied is True
    path, method, span = result.applied_blocks[0]
    assert path == f"mod{suffix}"
    assert method in {"exact", "whitespace-tolerant"}
    assert span == "2-2"
