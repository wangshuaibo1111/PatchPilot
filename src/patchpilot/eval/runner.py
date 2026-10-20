"""评测运行器：跑评测集，产出 pass@1 / 成本 / P95 与失败分类。

## 判定口径（与 SWE-bench 对齐）

一道题算 **通过**，当且仅当：
  1. 修复前 `fail_to_pass` **确实失败**（题目前提成立，否则题目本身无效）；
  2. 修复后 `fail_to_pass` **全部通过**；
  3. `pass_to_pass` 修复前后都通过（没把别的地方改坏）。

第 1 条是刻意加的：如果题目在修复前就是绿的，那"通过"毫无信息量——
这类题必须被标成 `INVALID_TASK` 而不是 `passed`。

## 为什么先独立验证"修复前失败"

顺序是：**先在干净副本上跑一次 fail_to_pass**（应失败）→ 再让修复闭环跑 →
再用闭环产出的补丁跑一次 fail_to_pass（应通过）。两次都用同一个沙箱，
确保"失败→通过"的对比是同一环境下的。

## 可复现性元数据

报告里固定写入 `温度 / commit / 镜像 tag / 题目 id 列表`（验收项 4 的要求），
使第三方可以照着重跑。
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from patchpilot.core.llm import LLMClient
from patchpilot.core.logging import get_logger
from patchpilot.core.textio import write_text_lf
from patchpilot.eval.dataset import EvalTask
from patchpilot.graph.repair import RepairOptions, run_repair
from patchpilot.sandbox.manager import run_pytest_in_sandbox
from patchpilot.sandbox.policy import SandboxPolicy
from patchpilot.sandbox.workspace import copy_workspace
from patchpilot.tools import git_server

#: 采样温度固定为 0（可复现纪律；报告里也会写明）
TEMPERATURE = 0.0


class EvalError(RuntimeError):
    """评测层面的失败（不是"题目没修好"，而是"评测本身跑不起来"）。"""


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """一道题的完整结果。"""

    task_id: str
    status: str  # passed | failed | invalid_task | error
    detail: str = ""
    rounds: int = 0
    duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    failure_kind: str = ""
    patch_path: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True, slots=True)
class EvalReport:
    """整份评测报告（可直接落盘为 JSON）。"""

    dataset: str
    total: int
    outcomes: tuple[TaskOutcome, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------- 指标 ----------
    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def valid(self) -> int:
        """有效题数：排除了 `invalid_task`（题目前提不成立）。"""
        return sum(1 for o in self.outcomes if o.status != "invalid_task")

    @property
    def pass_at_1(self) -> float:
        return self.passed / self.valid if self.valid else 0.0

    @property
    def total_cost_usd(self) -> float:
        return round(sum(o.cost_usd for o in self.outcomes), 6)

    @property
    def total_tokens(self) -> int:
        return sum(o.prompt_tokens + o.completion_tokens for o in self.outcomes)

    def latency_p95(self) -> float:
        """P95 单题耗时（题数少时退化为最大值，并在报告里注明样本量）。"""
        durations = sorted(o.duration_s for o in self.outcomes)
        if not durations:
            return 0.0
        if len(durations) < 5:
            return round(max(durations), 2)
        index = min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))
        return round(durations[index], 2)

    def mean_latency(self) -> float:
        if not self.outcomes:
            return 0.0
        return round(statistics.fmean([o.duration_s for o in self.outcomes]), 2)

    def failure_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for outcome in self.outcomes:
            failed = outcome.status == "failed"
            key = (outcome.failure_kind or "failed") if failed else outcome.status
            hist[key] = hist.get(key, 0) + 1
        return dict(sorted(hist.items(), key=lambda kv: -kv[1]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "meta": self.meta,
            "metrics": {
                "total": self.total,
                "valid": self.valid,
                "passed": self.passed,
                "pass@1": round(self.pass_at_1, 4),
                "total_cost_usd": self.total_cost_usd,
                "total_tokens": self.total_tokens,
                "mean_latency_s": self.mean_latency(),
                "p95_latency_s": self.latency_p95(),
                "failure_histogram": self.failure_histogram(),
            },
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def _git_commit(path: Path) -> str:
    """当前代码版本（可复现性元数据）。失败时返回空串而不是抛错。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except OSError:
        return ""


def _ensure_git(repo: Path) -> None:
    """保证 `repo` 是一个 git 仓库（补丁应用依赖 `git apply`）。

    为什么评测集目录里**不带** `.git`：在仓库里提交嵌套的 git 目录既容易出错也难审查。
    因此语料以纯源码形式入库，运行时在这里就地 `git init` + 首次提交。
    已经在 git 仓库里的题目（例如指向真实开源仓库）不受影响。
    """
    if (repo / ".git").exists():
        return
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "eval@patchpilot.local"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "patchpilot-eval"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "eval corpus baseline", "--allow-empty"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _run_nodes(
    *, policy: SandboxPolicy, workspace: Path, nodes: tuple[str, ...], copy_root: Path
) -> tuple[int, int, int]:
    """在沙箱里跑指定 node id，返回 (total, failed, errors)。"""
    result = run_pytest_in_sandbox(
        policy=policy,
        source=workspace,
        pytest_args=list(nodes),
        copy_root=copy_root,
    )
    report = result.report
    if report is None:
        # 没有结构化报告 = 测试都没跑起来：算作"失败"但把原因带出去
        raise EvalError(result.report_error or "未产出 JUnit 报告")
    return report.total, report.failed, report.errors


def run_task(
    *,
    task: EvalTask,
    llm: LLMClient,
    work_root: Path,
    budget: Any = None,
    max_rounds: int = 3,
) -> TaskOutcome:
    """跑一道题：先验证题目前提，再让修复闭环跑，最后判定。"""
    log = get_logger("eval")
    started = time.perf_counter()
    task_dir = work_root / f"{task.id}-{uuid.uuid4().hex[:6]}"
    task_dir.mkdir(parents=True, exist_ok=True)
    policy = SandboxPolicy(image=task.image, sandbox_root=task_dir, timeout_s=900)

    try:
        # 0) 先把语料复制到工作目录，再对**副本**做 `git init`
        #    （语料目录刻意不带 .git；也不在原目录里 init，避免嵌套 git 目录）
        # 内联语料就地物化；外部语料则复制一份（绝不在原目录上工作）
        repo = task_dir / "repo"
        src_repo, issue_path = task.materialize(repo)
        if src_repo != repo:
            copy_workspace(src_repo, repo)
        _ensure_git(repo)

        # 1) 干净副本上验证"修复前确实失败"（题目前提）
        clean = task_dir / "clean"
        copy_workspace(repo, clean)
        try:
            total, failed, errors = _run_nodes(
                policy=policy, workspace=clean, nodes=task.fail_to_pass, copy_root=task_dir
            )
        except EvalError as exc:
            return TaskOutcome(
                task_id=task.id,
                status="error",
                detail=f"前提验证阶段无法跑测试: {exc}",
                duration_s=round(time.perf_counter() - started, 2),
            )
        if failed == 0 and errors == 0 and total > 0:
            return TaskOutcome(
                task_id=task.id,
                status="invalid_task",
                detail=(
                    f"fail_to_pass 在修复前就是绿的（{total} 个用例全部通过）——"
                    "这道题无法判定'修好了'，请修题"
                ),
                duration_s=round(time.perf_counter() - started, 2),
            )

        # 2) 让修复闭环在**副本**上工作，拿到补丁
        options = RepairOptions(
            repo=repo,
            issue_file=issue_path,
            tests=task.pytest_args(),
            policy=policy,
            budget=budget,
            max_rounds=max_rounds,
            copy_root=task_dir,
            output_dir=task_dir / "patches",
        )
        final = run_repair(options=options, llm=llm)
        outcome_common = {
            "task_id": task.id,
            "rounds": int(final.get("round", 0) or 0),
            "prompt_tokens": int(final.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(final.get("completion_tokens", 0) or 0),
            "cost_usd": float(final.get("cost_usd", 0.0) or 0.0),
            "failure_kind": str(final.get("failure_kind", "")),
            "patch_path": str(final.get("final_patch_path", "")),
        }

        if not final.get("passed"):
            # 闭环自己没通过：状态就是 failed，细节带上归因
            reason = str(final.get("budget_exceeded", "") or final.get("failure_kind", ""))
            return TaskOutcome(
                status="failed",
                detail=f"修复闭环未通过（{reason or '未知'}）",
                duration_s=round(time.perf_counter() - started, 2),
                **outcome_common,
            )

        # 3) **独立复核**：用闭环产出的补丁，在一份全新副本上重跑 fail_to_pass + pass_to_pass
        patch_text = str(final.get("patch", ""))
        verify = task_dir / "verify"
        copy_workspace(repo, verify)
        try:
            git_server.apply_patch(verify, patch_text)
        except Exception as exc:  # noqa: BLE001
            return TaskOutcome(
                status="failed",
                detail=f"补丁无法在干净副本上应用: {type(exc).__name__}: {exc}",
                duration_s=round(time.perf_counter() - started, 2),
                **outcome_common,
            )
        try:
            total, failed, errors = _run_nodes(
                policy=policy,
                workspace=verify,
                nodes=task.pytest_args(),
                copy_root=task_dir,
            )
        except EvalError as exc:
            return TaskOutcome(
                status="failed",
                detail=f"复核阶段无法跑测试: {exc}",
                duration_s=round(time.perf_counter() - started, 2),
                **outcome_common,
            )
        ok = total > 0 and failed == 0 and errors == 0
        return TaskOutcome(
            status="passed" if ok else "failed",
            detail=(
                f"复核：{total} 个用例，failed={failed} errors={errors}"
                if not ok
                else f"复核通过（{total} 个用例）"
            ),
            duration_s=round(time.perf_counter() - started, 2),
            **outcome_common,
        )
    finally:
        log.info("eval.task_done", task=task.id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_eval(
    *,
    tasks: list[EvalTask],
    llm: LLMClient,
    dataset_name: str,
    work_root: Path,
    budget: Any = None,
    max_rounds: int = 3,
    model_name: str = "",
    limit: int | None = None,
) -> EvalReport:
    """跑评测集并产出报告（含可复现性元数据）。"""
    selected = tasks[:limit] if limit else tasks
    work_root.mkdir(parents=True, exist_ok=True)
    outcomes: list[TaskOutcome] = []
    for task in selected:
        outcomes.append(
            run_task(
                task=task,
                llm=llm,
                work_root=work_root,
                budget=budget,
                max_rounds=max_rounds,
            )
        )
    meta = {
        "temperature": TEMPERATURE,
        "model": model_name,
        "image": selected[0].image if selected else "",
        "task_ids": [t.id for t in selected],
        "commit": _git_commit(Path(__file__).resolve().parents[3]),
        "python": os.sys.version.split()[0],
        "max_rounds": max_rounds,
        "note": (
            "pass@1 = passed / valid；valid 排除了 invalid_task"
            "（fail_to_pass 在修复前即为绿的题）。P95 在样本 < 5 时退化为最大值。"
        ),
    }
    return EvalReport(
        dataset=dataset_name, total=len(selected), outcomes=tuple(outcomes), meta=meta
    )


def write_report(report: EvalReport, path: Path) -> Path:
    """报告落盘（JSON，含全部指标与逐题结果）。"""
    write_text_lf(path, json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return path


__all__ = [
    "TEMPERATURE",
    "EvalError",
    "EvalReport",
    "TaskOutcome",
    "run_eval",
    "run_task",
    "write_report",
]
