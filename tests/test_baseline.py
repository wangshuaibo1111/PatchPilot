"""回归门禁（验收项 5）的测试：基线对比必须**不误报**且**不漏报**。

两类断言缺一不可：
  - **不漏报**：pass@1 下降必须判为回归（否则门禁形同虚设）；
  - **不误报**：同码重跑的成本漂移、子集运行（`--limit`）、环境漂移
    都**不能**判成回归——一个在未改动代码上也会变红的门禁，维护者会很快学会忽略它。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchpilot.eval.baseline import (
    baseline_from_report,
    compare,
    load_baseline,
    write_baseline,
)
from patchpilot.eval.runner import EvalReport, TaskOutcome


def _outcome(
    task_id: str, status: str, *, cost: float = 0.0, duration: float = 10.0
) -> TaskOutcome:
    return TaskOutcome(
        task_id=task_id, status=status, cost_usd=cost, duration_s=duration
    )


def _report(*outcomes: TaskOutcome, meta: dict | None = None) -> EvalReport:
    return EvalReport(
        dataset="fast",
        total=len(outcomes),
        outcomes=outcomes,
        meta=meta
        or {"temperature": 0.0, "model": "deepseek-flash", "image": "img:1"},
    )


def _baseline(report: EvalReport) -> dict:
    return baseline_from_report(report)


# ---------------------------------------------------------------- 不漏报


def test_pass_at_1_drop_is_a_regression() -> None:
    base = _baseline(
        _report(_outcome("a", "passed"), _outcome("b", "passed", cost=0.002, duration=12.0))
    )
    current = _report(_outcome("a", "passed"), _outcome("b", "failed"))

    result = compare(current, base)

    assert not result.ok, "pass@1 从 100% 掉到 50% 必须判回归"
    assert any(r.metric == "pass@1" for r in result.regressions)
    assert result.lost_tasks == ("b",), "必须点名哪道题从通过变失败"


def test_regression_detail_names_the_tasks() -> None:
    base = _baseline(_report(_outcome("x", "passed"), _outcome("y", "passed")))
    current = _report(_outcome("x", "failed"), _outcome("y", "passed"))

    result = compare(current, base)

    joined = " | ".join(result.notes)
    assert "x" in joined, f"提示里必须点名失败的题，实际 {joined}"


# ---------------------------------------------------------------- 不误报


def test_identical_run_is_not_a_regression() -> None:
    base = _baseline(_report(_outcome("a", "passed"), _outcome("b", "passed")))
    same = _report(_outcome("a", "passed"), _outcome("b", "passed"))

    assert compare(same, base).ok


def test_cost_drift_alone_does_not_block() -> None:
    """成本大幅上涨（远超容差）**也不阻塞**——只提示。

    依据：同码连跑两次，总成本实测从 0.004268 漂到 0.005673（+33%），
    原因是"模型这次多花了几轮重试"。用成本阻塞会让门禁变成随机红。
    这里用 +567% 的极端漂移验证"仍然只提示、不阻塞"。
    """
    base = _baseline(_report(_outcome("a", "passed", cost=0.003)))
    noisier = _report(_outcome("a", "passed", cost=0.02))

    result = compare(noisier, base)

    assert result.ok, "成本漂移不该阻塞"
    assert any("不阻塞" in note for note in result.notes), "但要给出提示"


def test_strict_cost_makes_cost_blocking() -> None:
    base = _baseline(_report(_outcome("a", "passed", cost=0.003)))
    noisier = _report(_outcome("a", "passed", cost=0.05))

    result = compare(noisier, base, strict_cost=True)

    assert not result.ok
    assert any(r.metric == "total_cost_usd" for r in result.regressions)


def test_subset_run_does_not_report_phantom_losses() -> None:
    """`--limit 1` 只跑一题时，其余题"没跑"不能算"退化"（否则冒烟运行必红）。"""
    base = _baseline(
        _report(_outcome("a", "passed"), _outcome("b", "passed"), _outcome("c", "passed"))
    )
    subset = _report(_outcome("a", "passed"))

    result = compare(subset, base)

    assert result.lost_tasks == (), f"子集运行不该报丢失，实际 {result.lost_tasks}"
    assert result.ok, "子集运行 pass@1 仍为 100%，不应判回归"
    assert any("未覆盖" in note for note in result.notes), "但应提示对比不完整"


def test_environment_drift_is_flagged_but_not_blocking() -> None:
    """模型/镜像/温度不同时，数字不可直接比较——提示出来，但不自行判红。"""
    base = _baseline(_report(_outcome("a", "passed")))
    other_model = _report(
        _outcome("a", "passed"),
        meta={"temperature": 0.0, "model": "another-model", "image": "img:1"},
    )

    result = compare(other_model, base)

    assert result.ok
    assert any("环境不同" in note for note in result.notes)


def test_gained_task_is_reported_as_improvement() -> None:
    base = _baseline(_report(_outcome("a", "passed"), _outcome("b", "failed")))
    better = _report(_outcome("a", "passed"), _outcome("b", "passed"))

    result = compare(better, base)

    assert result.ok
    assert result.gained_tasks == ("b",)


# ---------------------------------------------------------------- 基线的存取


def test_baseline_roundtrip_via_disk(tmp_path: Path) -> None:
    report = _report(_outcome("a", "passed"), _outcome("b", "failed"))
    path = write_baseline(report, tmp_path / "baseline.json")

    data = load_baseline(path)

    assert data["pass@1"] == 0.5
    assert data["passed_tasks"] == ["a"]
    assert data["failed_tasks"] == ["b"]
    assert data["meta"]["model"] == "deepseek-flash"
    # 落盘必须是合法 UTF-8 JSON（可提交进版本库）
    json.loads(path.read_text(encoding="utf-8"))


def test_load_baseline_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "nope.json")


def test_comparison_serialises_to_dict() -> None:
    base = _baseline(_report(_outcome("a", "passed")))
    current = _report(_outcome("a", "failed"))

    payload = compare(current, base).summary()

    assert payload["ok"] is False
    assert payload["regressions"][0]["metric"] == "pass@1"
    assert isinstance(payload["lost_tasks"], list)
