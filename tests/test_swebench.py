"""SWE-bench 子集评测的测试：**离线**验证纯逻辑（不 clone、不跑 docker）。

真机部分（克隆 + 沙箱 + 标定）由 `pp swebench` 与 `.tmp/` 的标定脚本承担；
这里守的是"口径与数据结构"，尤其是三件容易错的事：
  1. 裸测试名必须能解析成 `文件::用例`（sympy 那类仓库的 F2P 不带路径）；
  2. `pass@1` 的分母只算有效题；
  3. 入库子集必须结构自洽，且**每个 instance 都带标定结论**（否则"准入"是假的）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from patchpilot.eval.swebench import (
    SwebenchError,
    SweInstance,
    SweOutcome,
    SweReport,
    load_subset,
    resolve_node_ids,
)


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _instance(**over: object) -> dict:
    payload = {
        "instance_id": "pallets__flask-5063",
        "repo": "pallets/flask",
        "base_commit": "182ce3dd15",
        "image": "patchpilot-py312-deps:flask-pytest7",
        "fail_to_pass": ["tests/test_cli.py::TestRoutes::test_host"],
        "problem": "flask routes 不显示子域名",
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------- 子集加载


def test_load_subset_reads_instances(tmp_path: Path) -> None:
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [_instance()]}, ensure_ascii=False))

    instances = load_subset(path)

    assert len(instances) == 1
    assert instances[0].instance_id == "pallets__flask-5063"
    assert instances[0].fail_to_pass == ("tests/test_cli.py::TestRoutes::test_host",)


def test_load_subset_rejects_missing_field(tmp_path: Path) -> None:
    payload = _instance()
    del payload["base_commit"]
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [payload]}, ensure_ascii=False))

    with pytest.raises(SwebenchError, match="缺字段"):
        load_subset(path)


def test_load_subset_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": []}))

    with pytest.raises(SwebenchError, match="为空"):
        load_subset(path)


def test_shipped_subset_is_calibrated_and_documents_limits() -> None:
    """入库子集必须：每题都有 image、且文件里写明**受限原因**。

    「准入标定」是本模块唯一的可信度来源：如果子集里混进没标定过的题，
    pass@1 就会反映环境问题而不是模型能力。
    """
    subset_path = Path(__file__).resolve().parents[1] / "eval" / "swebench_subset.json"
    assert subset_path.is_file(), "子集应已入库"

    payload = json.loads(subset_path.read_text(encoding="utf-8"))
    instances = payload["instances"]
    assert instances, "子集不能为空"

    for item in instances:
        # 二选一：docker 模式给镜像，宿主解释器模式给 python（旧 Python 用后者）
        assert item.get("image") or item.get("python"), (
            f"{item['instance_id']}: 必须记录运行方式（image 或 python）"
        )
        assert item["fail_to_pass"], f"{item['instance_id']}: 必须有判据测试"
        assert item["problem"].strip(), f"{item['instance_id']}: 必须有 issue 文本"

    # 限制必须写清（否则读者会把它当论文级数字）
    notes = str(payload.get("notes") or "")
    assert "标定" in notes
    assert "不是论文级" in notes or "链路验证" in notes
    # 被剔除的题与原因要留档
    assert "rejected" in payload


# ---------------------------------------------------------------- 裸测试名解析


def test_resolve_node_ids_finds_bare_test_names(tmp_path: Path) -> None:
    """sympy 那类仓库的 F2P 是裸名，必须解析成 `文件::用例`。"""
    write_lf(
        tmp_path / "sympy" / "core" / "tests" / "test_basic.py",
        "def test_ccode_Relational():\n    assert True\n",
    )
    write_lf(
        tmp_path / "sympy" / "functions" / "tests" / "test_trig.py",
        "def test_sinc():\n    assert True\n",
    )

    resolved = resolve_node_ids(tmp_path, ("test_ccode_Relational", "test_sinc"))

    assert resolved[0] == "sympy/core/tests/test_basic.py::test_ccode_Relational"
    assert resolved[1] == "sympy/functions/tests/test_trig.py::test_sinc"


def test_resolve_node_ids_keeps_paths_and_unknown_names(tmp_path: Path) -> None:
    """已带路径的原样保留；解析不到的**原样保留**（让 pytest 自己报错，不悄悄少跑）。"""
    resolved = resolve_node_ids(
        tmp_path, ("tests/test_x.py::test_y", "definitely_absent_name")
    )

    assert resolved == ("tests/test_x.py::test_y", "definitely_absent_name")


# ---------------------------------------------------------------- 指标


def _outcome(status: str, *, instance_id: str = "x") -> SweOutcome:
    return SweOutcome(instance_id=instance_id, status=status)


def test_pass_at_1_excludes_invalid() -> None:
    report = SweReport(
        outcomes=(
            _outcome("passed", instance_id="a"),
            _outcome("failed", instance_id="b"),
            _outcome("invalid", instance_id="c"),
        )
    )

    assert report.valid == 2
    assert report.passed == 1
    assert report.pass_at_1 == pytest.approx(0.5)


def test_pass_at_1_is_zero_when_nothing_valid() -> None:
    report = SweReport(outcomes=(_outcome("invalid"),))

    assert report.valid == 0
    assert report.pass_at_1 == 0.0


def test_report_dict_carries_note_about_sample_size() -> None:
    """报告必须自带"样本小、非论文级"的说明（避免被单独摘出去当结论）。"""
    report = SweReport(
        outcomes=(_outcome("passed"),), meta={"note": "N 很小，只能当链路验证"}
    )

    payload = report.to_dict()

    assert payload["metrics"]["pass@1"] == 1.0
    assert "note" in payload["meta"]


# ---------------------------------------------------------------- 实例/子集的形状


def test_swe_instance_is_frozen() -> None:
    instance = SweInstance(
        instance_id="x",
        repo="a/b",
        base_commit="deadbeef",
        image="img:1",
        fail_to_pass=("t::x",),
        problem="p",
        test_patch="",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.instance_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------- 运行方式与占位展开


def test_load_subset_accepts_host_python_without_image(tmp_path: Path) -> None:
    """宿主解释器模式不需要镜像（旧 Python 走这条路）。"""
    payload = _instance()
    payload.pop("image")
    payload["python"] = "python3.9"
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [payload]}, ensure_ascii=False))

    instances = load_subset(path, validate_interpreters=False)

    assert instances[0].python == "python3.9"
    assert instances[0].image == ""


def test_load_subset_rejects_instance_without_any_runner(tmp_path: Path) -> None:
    payload = _instance()
    payload.pop("image")
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [payload]}, ensure_ascii=False))

    with pytest.raises(SwebenchError, match="image"):
        load_subset(path)


def test_load_subset_expands_env_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入库的子集用 `${VAR}` 占位（本机绝对路径不该进版本库）。"""
    monkeypatch.setenv("PP_TEST_PY39", "/opt/py39/bin/python")
    payload = _instance()
    payload.pop("image")
    payload["python"] = "${PP_TEST_PY39}"
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [payload]}, ensure_ascii=False))

    instances = load_subset(path, validate_interpreters=False)

    assert instances[0].python == "/opt/py39/bin/python"


def test_load_subset_reports_unexpanded_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PP_TEST_MISSING", raising=False)
    payload = _instance()
    payload.pop("image")
    payload["python"] = "${PP_TEST_MISSING}"
    path = tmp_path / "subset.json"
    write_lf(path, json.dumps({"instances": [payload]}, ensure_ascii=False))

    with pytest.raises(SwebenchError, match="未展开"):
        load_subset(path)
