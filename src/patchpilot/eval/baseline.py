"""基线对比：把当前指标与提交在版本库里的基线比，退化即失败（验收项 5）。

## 为什么需要它

"回归门禁"要能回答一个问题：**这次改动让效果变差了吗？**
单看一次 `pp eval` 的数字回答不了——必须与一个**已知合格的基线**比。

因此基线是要**提交进版本库**的数据（`eval/baseline.json`），
由一次确认合格的运行 `pp eval --write-baseline eval/baseline.json` 产出；
之后每次 `pp eval --baseline eval/baseline.json` 都会对比：

  - `pass@1` **不得低于**基线（效果退化 = 阻塞）；
  - 单题成本 / P95 耗时 **不得高于**基线太多（成本退化 = 阻塞，阈值可配）。

## 判定必须给出可读的 diff

只喊"回归了"没用——必须列出**哪几题**从通过变成失败、成本涨了多少。
否则维护者无法判断是"模型变差"还是"题目/环境变了"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchpilot.eval.runner import EvalReport

#: 允许的**相对**劣化幅度。
#:
#: 经验值来自一次真实的自我验证（故意改坏 prompt 再还原）：
#: 同样的代码连跑两次，总成本从 0.004268 漂到 0.005673（**+33%**），
#: 原因是"模型这次多花了几轮重试"——这是**采样噪声，不是回归**。
#: 因此默认给成本/延时很宽的容差，并且**默认不阻塞**（见 `strict_cost`）：
#: 一个在未改动代码上也会变红的门禁，维护者很快就会学会忽略它。
DEFAULT_COST_TOLERANCE = 1.00
DEFAULT_LATENCY_TOLERANCE = 1.00


@dataclass(frozen=True, slots=True)
class Regression:
    """一条回归项。"""

    metric: str
    baseline: float
    current: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    """与基线的对比结果。"""

    regressions: tuple[Regression, ...] = ()
    notes: tuple[str, ...] = ()
    lost_tasks: tuple[str, ...] = ()
    gained_tasks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.regressions

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "lost_tasks": list(self.lost_tasks),
            "gained_tasks": list(self.gained_tasks),
            "regressions": [
                {
                    "metric": r.metric,
                    "baseline": r.baseline,
                    "current": r.current,
                    "detail": r.detail,
                }
                for r in self.regressions
            ],
            "notes": list(self.notes),
        }


def baseline_from_report(report: EvalReport) -> dict[str, Any]:
    """从一次合格运行生成基线数据（含逐题通过情况，便于定位退化到哪题）。"""
    return {
        "dataset": report.dataset,
        "pass@1": round(report.pass_at_1, 4),
        "valid": report.valid,
        "total_cost_usd": report.total_cost_usd,
        "mean_latency_s": report.mean_latency(),
        "p95_latency_s": report.latency_p95(),
        "passed_tasks": sorted(o.task_id for o in report.outcomes if o.passed),
        "failed_tasks": sorted(o.task_id for o in report.outcomes if not o.passed),
        "meta": {
            key: report.meta.get(key)
            for key in ("temperature", "model", "image", "commit", "max_rounds")
        },
    }


def write_baseline(report: EvalReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline_from_report(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_baseline(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"基线文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def compare(
    report: EvalReport,
    baseline: dict[str, Any],
    *,
    cost_tolerance: float = DEFAULT_COST_TOLERANCE,
    latency_tolerance: float = DEFAULT_LATENCY_TOLERANCE,
    strict_cost: bool = False,
) -> Comparison:
    """把报告与基线比，返回回归项与逐题得失。

    **只有 `pass@1` 默认阻塞**：它才是"效果有没有变差"的判据。
    成本与延时默认**只提示不阻塞**——它们随"这次模型多转了几轮"大幅波动，
    用它们阻塞会让门禁变成随机红（实测：同码连跑两次成本漂 +33%）。
    需要严格控成本时用 `strict_cost=True`。
    """
    regressions: list[Regression] = []
    notes: list[str] = []

    base_pass = float(baseline.get("pass@1", 0.0))
    if report.pass_at_1 + 1e-9 < base_pass:
        regressions.append(
            Regression(
                metric="pass@1",
                baseline=base_pass,
                current=round(report.pass_at_1, 4),
                detail=f"从 {base_pass:.1%} 降到 {report.pass_at_1:.1%}",
            )
        )

    # 逐题对比：既给出"哪几题退了"，也给出"哪几题新通过"（可能是有意改进）
    base_passed = set(baseline.get("passed_tasks") or [])
    current_passed = {o.task_id for o in report.outcomes if o.passed}
    # **只在本次真正跑过的题目范围内比**：`--limit` 之类的子集运行不能把
    # "没跑"当成"退化"——否则一次冒烟运行就会报出假的回归（实测到过）。
    current_tasks = {o.task_id for o in report.outcomes}
    lost = tuple(sorted(base_passed & current_tasks - current_passed))
    gained = tuple(sorted(current_passed - base_passed))
    skipped = sorted(base_passed - current_tasks)
    if skipped:
        notes.append(
            f"本次未覆盖 {len(skipped)} 道基线题（逐题对比不完整，pass@1 仍按本次有效题算）"
        )
    if lost:
        notes.append(f"从通过变为失败: {', '.join(lost)}")
    if gained:
        notes.append(f"新增通过: {', '.join(gained)}")

    base_cost = float(baseline.get("total_cost_usd", 0.0))
    if base_cost > 0:
        allowed = base_cost * (1.0 + cost_tolerance)
        if report.total_cost_usd > allowed + 1e-12:
            detail = (
                f"成本涨到 {report.total_cost_usd:.6f}"
                f"（允许上限 {allowed:.6f}，容差 {cost_tolerance:.0%}）"
            )
            if strict_cost:
                regressions.append(
                    Regression(
                        metric="total_cost_usd",
                        baseline=base_cost,
                        current=report.total_cost_usd,
                        detail=detail,
                    )
                )
            else:
                notes.append(f"[成本提示/不阻塞] {detail}")

    base_p95 = float(baseline.get("p95_latency_s", 0.0))
    if base_p95 > 0:
        allowed_p95 = base_p95 * (1.0 + latency_tolerance)
        if report.latency_p95() > allowed_p95:
            detail = (
                f"P95 涨到 {report.latency_p95():.1f}s"
                f"（允许上限 {allowed_p95:.1f}s，容差 {latency_tolerance:.0%}）"
            )
            if strict_cost:
                regressions.append(
                    Regression(
                        metric="p95_latency_s",
                        baseline=base_p95,
                        current=report.latency_p95(),
                        detail=detail,
                    )
                )
            else:
                notes.append(f"[延时提示/不阻塞] {detail}")

    # 环境漂移提示：基线与当前运行的模型/镜像/温度不一致时，数字不可直接比较
    base_meta = baseline.get("meta") or {}
    for key in ("model", "image", "temperature"):
        base_value = base_meta.get(key)
        current_value = report.meta.get(key)
        both_present = base_value not in (None, "") and current_value not in (None, "")
        if both_present and str(base_value) != str(current_value):
            notes.append(
                f"**环境不同** {key}: 基线 {base_value} vs 当前 {current_value}"
                "（数字不可直接比较，建议同时更新基线）"
            )

    return Comparison(
        regressions=tuple(regressions),
        notes=tuple(notes),
        lost_tasks=lost,
        gained_tasks=gained,
    )


__all__ = [
    "DEFAULT_COST_TOLERANCE",
    "DEFAULT_LATENCY_TOLERANCE",
    "Comparison",
    "Regression",
    "baseline_from_report",
    "compare",
    "load_baseline",
    "write_baseline",
]
