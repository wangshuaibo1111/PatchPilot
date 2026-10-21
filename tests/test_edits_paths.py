"""路径容错与上下文供给的测试（"改动打不上"的两类真因）。

真机证据（第三十三轮）：
  - `sympy__sympy-12419` 的失败是 `<sympy/concrete/summations.py>: 文件不存在`
    —— 模型把路径包在尖括号里；
  - 另外 3 题是 `搜索块未匹配`，因为我们只给候选文件的**前 2500 字符**，
    改动位置在窗口外，模型无从逐字照抄。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.tools.edits import apply_model_output, normalize_path


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def block(path: str, search: str, replace: str) -> str:
    return f"{path}\n<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


# ---------------------------------------------------------------- 路径归一化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<sympy/concrete/summations.py>", "sympy/concrete/summations.py"),
        ("`mod.py`", "mod.py"),
        ('"mod.py"', "mod.py"),
        ("./src/mod.py", "src/mod.py"),
        ("src\\mod.py", "src/mod.py"),
        ("  mod.py  ", "mod.py"),
    ],
)
def test_normalize_path_strips_model_wrappers(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


def test_angle_bracketed_path_now_applies(tmp_path: Path) -> None:
    """真机失败原样复现：`<mod.py>` 曾经直接报"文件不存在"。"""
    write_lf(tmp_path / "mod.py", "a = 1\n")

    result = apply_model_output(tmp_path, block("<mod.py>", "a = 1", "a = 2"))

    assert result.applied is True
    assert (tmp_path / "mod.py").read_bytes() == b"a = 2\n"


def test_missing_file_hint_lists_same_name_files(tmp_path: Path) -> None:
    """路径猜错时至少要把同名文件指出来（否则调用方无法自救）。"""
    write_lf(tmp_path / "sympy" / "printing" / "ccode.py", "x = 1\n")

    result = apply_model_output(
        tmp_path, block("sympy/printing/c.py", "x = 1", "x = 2")
    )

    assert result.applied is False
    assert "文件不存在" in result.detail
    assert "同名文件" in result.detail


def test_empty_path_is_rejected(tmp_path: Path) -> None:
    write_lf(tmp_path / "mod.py", "a = 1\n")

    result = apply_model_output(tmp_path, block("<>", "a = 1", "a = 2"))

    assert result.applied is False
    assert "路径为空" in result.detail


# ---------------------------------------------------------------- prompt 给片段而非文件头


def test_prompt_shows_line_ranges_instead_of_file_head(tmp_path: Path) -> None:
    """prompt 必须带行号区间与命中片段，而不是"文件前 2500 字符"。"""
    from patchpilot.eval.swebench import SweInstance, build_repair_prompt

    body = "".join(f"def filler_{i}():\n    return {i}\n" for i in range(150))
    tail = "def target_handler(config):\n    return config['missing_key']\n"
    write_lf(tmp_path / "mod.py", body + tail)
    instance = SweInstance(
        instance_id="x__y-1",
        repo="x/y",
        base_commit="0" * 40,
        image="",
        fail_to_pass=("test_x",),
        problem="target_handler 取缺失的配置键时抛 KeyError，应该返回默认值",
        test_patch="",
        python="python",
    )

    prompt, readable = build_repair_prompt(instance, tmp_path, max_files=2)

    assert readable >= 1
    assert "（行 " in prompt, "片段必须带行号区间"
    assert "target_handler" in prompt, "命中片段必须出现在 prompt 里（而不是只有文件头）"
    assert "逐字" in prompt, "必须明确要求 SEARCH 逐字复制"


def test_prompt_does_not_emit_file_head_only(tmp_path: Path) -> None:
    """反向验证：改动点在文件末尾时，文件头那 2500 字符里根本不含它。"""
    from patchpilot.eval.swebench import SweInstance, build_repair_prompt

    body = "".join(f"def filler_{i}():\n    return {i}\n" for i in range(150))
    write_lf(tmp_path / "mod.py", body + "def tail_marker():\n    return 42\n")
    instance = SweInstance(
        instance_id="x__y-2",
        repo="x/y",
        base_commit="0" * 40,
        image="",
        fail_to_pass=("test_x",),
        problem="tail_marker 的返回值不对",
        test_patch="",
        python="python",
    )

    head = (tmp_path / "mod.py").read_text(encoding="utf-8")[:2500]
    assert "tail_marker" not in head, "前提：文件头里确实没有它"

    prompt, _ = build_repair_prompt(instance, tmp_path, max_files=2)

    assert "tail_marker" in prompt, "靠窗口截断是拿不到它的，必须靠定位片段"
