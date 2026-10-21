"""`load_subset` 的解释器校验：挡住"把非 Python 塞进 python 字段"这类失真。

复审实测过：`cmd.exe -m pytest -q` 返回 **rc=0**。若判分只看退出码，pass@1 能凭空变 100%；
现在判分已改成读日志，但入口仍要挡住——否则只是把"假通过"变成"假 invalid"。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from patchpilot.eval.swebench import SwebenchError, load_subset, validate_interpreter


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _payload(python: str) -> dict:
    return {
        "instances": [
            {
                "instance_id": "x__y-1",
                "repo": "x/y",
                "base_commit": "0" * 40,
                "python": python,
                "fail_to_pass": ["test_a"],
                "problem": "坏了",
            }
        ]
    }


def test_validate_interpreter_accepts_current_python() -> None:
    version = validate_interpreter(sys.executable)

    assert version and version[0].isdigit(), f"应返回版本号，实际 {version!r}"


def test_validate_interpreter_rejects_missing_path(tmp_path: Path) -> None:
    with pytest.raises(SwebenchError, match="不存在"):
        validate_interpreter(str(tmp_path / "nope.exe"))


def test_validate_interpreter_rejects_non_python_executable(tmp_path: Path) -> None:
    """用 `cmd.exe` 冒充解释器必须被拒（它 `-c` 也会返回 0，光看退出码挡不住）。"""
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if not cmd:
        pytest.skip("本平台没有 cmd.exe")

    with pytest.raises(SwebenchError, match="pytest"):
        validate_interpreter(cmd)


def test_load_subset_validates_by_default(tmp_path: Path) -> None:
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps(_payload(str(tmp_path / "ghost")), ensure_ascii=False))

    with pytest.raises(SwebenchError, match="不存在"):
        load_subset(path)


def test_load_subset_can_skip_validation_for_parsing(tmp_path: Path) -> None:
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps(_payload(str(tmp_path / "ghost")), ensure_ascii=False))

    instances = load_subset(path, validate_interpreters=False)

    assert instances[0].python.endswith("ghost")


def test_load_subset_rejects_pythonpath_escape(tmp_path: Path) -> None:
    for bad in ("..", "../outside", "src/../../x"):
        payload = _payload(sys.executable)
        payload["instances"][0]["pythonpath"] = bad
        path = tmp_path / "subset.json"
        write_lf(path, json.dumps(payload, ensure_ascii=False))
        with pytest.raises(SwebenchError, match="pythonpath"):
            load_subset(path, validate_interpreters=False)


def test_load_subset_rejects_absolute_pythonpath(tmp_path: Path) -> None:
    payload = _payload(sys.executable)
    payload["instances"][0]["pythonpath"] = str(tmp_path)
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps(payload, ensure_ascii=False))

    with pytest.raises(SwebenchError, match="绝对路径"):
        load_subset(path, validate_interpreters=False)
