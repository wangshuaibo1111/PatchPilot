"""SWE-bench 子集评测（真实 instance，带**准入标定门**）。

## 为什么必须有"标定门"

一道 SWE-bench 题只有在环境正确时才有意义：**把官方 gold patch 打进去，
FAIL_TO_PASS 必须从"失败"翻成"通过"**。若连 gold patch 都过不了，
分数反映的是我们的环境问题，而不是模型能力。

本模块因此把"标定"做成**准入条件**：只有标定通过的题才进评测集，
并且标定结果要被记录下来（含被剔除的题与原因）。

## 本环境的硬限制（已实测，必须如实记录）

1. **镜像仓库不可用**：`docker.nju.edu.cn` 对 `python:3.9-slim` 返回 **403**，
   Docker Hub 也超时；本地只有 `python:3.12-slim`。
   → **拿不到官方 per-instance 镜像**，也换不了基础镜像版本。
2. **旧仓库不兼容 Python 3.12**：`sympy/sympy` 2017 年的版本 `from distutils.version import
   StrictVersion`，而 distutils 已在 3.12 移除 → 直接 ImportError。
3. **依赖需按 instance 钉死**：flask 2.x 的测试用 `_pytest.monkeypatch.notset`，
   pytest 8 已移除（钉 7.4.4 才通过）；pylint 需要特定 `astroid` 版本。

结论：本环境里**只能**评测那些"在 python 3.12 + 钉死依赖"下能通过标定的题。
`eval/swebench_subset.json` 里的题就是这些，数量少，故分数只能当"链路验证"，
不能当论文级 pass@1。

## 判定口径

与 SWE-bench 一致：把 `test_patch` 与"候选补丁"都打进 base_commit 的工作区，
在沙箱里跑 `FAIL_TO_PASS`，全绿即算修好。`PASS_TO_PASS` 保留在数据里，
供后续做"没改坏别的"的检查。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_DEF_RE = re.compile(r"^\s*def\s+{name}\s*\(", re.MULTILINE)


class SwebenchError(RuntimeError):
    """评测层面的失败（不是"题没修好"）。"""


@dataclass(frozen=True, slots=True)
class SweInstance:
    """一道 SWE-bench 题（已标定）。"""

    instance_id: str
    repo: str
    base_commit: str
    image: str
    fail_to_pass: tuple[str, ...]
    problem: str
    test_patch: str
    version: str = ""
    pass_to_pass: tuple[str, ...] = ()
    gold_patch: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SweOutcome:
    instance_id: str
    status: str  # passed | failed | invalid | error
    detail: str = ""
    rounds: int = 0
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True, slots=True)
class SweReport:
    outcomes: tuple[SweOutcome, ...]
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def valid(self) -> int:
        return sum(1 for o in self.outcomes if o.status != "invalid")

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def pass_at_1(self) -> float:
        return self.passed / self.valid if self.valid else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "meta": self.meta,
            "metrics": {
                "total": len(self.outcomes),
                "valid": self.valid,
                "passed": self.passed,
                "pass@1": round(self.pass_at_1, 4),
            },
            "outcomes": [
                {
                    "instance_id": o.instance_id,
                    "status": o.status,
                    "detail": o.detail,
                    "rounds": o.rounds,
                    "cost_usd": o.cost_usd,
                }
                for o in self.outcomes
            ],
        }


def load_subset(path: Path) -> list[SweInstance]:
    """读子集 JSON。文件里的题必须**已经**通过标定（见模块说明）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    instances: list[SweInstance] = []
    for item in payload.get("instances", []):
        required = {"instance_id", "repo", "base_commit", "image", "fail_to_pass", "problem"}
        missing = required - set(item)
        if missing:
            raise SwebenchError(f"{item.get('instance_id', '?')}: 缺字段 {sorted(missing)}")
        instances.append(
            SweInstance(
                instance_id=str(item["instance_id"]),
                repo=str(item["repo"]),
                base_commit=str(item["base_commit"]),
                image=str(item["image"]),
                fail_to_pass=tuple(item["fail_to_pass"]),
                problem=str(item["problem"]),
                test_patch=str(item.get("test_patch") or ""),
                version=str(item.get("version") or ""),
                pass_to_pass=tuple(item.get("pass_to_pass") or ()),
                gold_patch=str(item.get("gold_patch") or ""),
                notes=str(item.get("notes") or ""),
            )
        )
    if not instances:
        raise SwebenchError(f"子集为空: {path}")
    return instances


def resolve_node_ids(repo_dir: Path, names: tuple[str, ...]) -> tuple[str, ...]:
    """把**裸测试名**解析成 `文件::用例`（sympy 等仓库的 F2P 不带路径）。

    解析不到的**原样保留**（让 pytest 自己报错，而不是我们悄悄少跑一条）。
    """
    resolved: list[str] = []
    for raw in names:
        if "::" in raw:
            resolved.append(raw)
            continue
        plain = raw.split("::")[-1]
        pattern = re.compile(_DEF_RE.pattern.format(name=re.escape(plain)), re.MULTILINE)
        found: str | None = None
        for path in sorted(repo_dir.rglob("test_*.py")):
            try:
                if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                    found = f"{path.relative_to(repo_dir).as_posix()}::{plain}"
                    break
            except OSError:
                continue
        resolved.append(found or raw)
    return tuple(resolved)


def prepare_repo(instance: SweInstance, work_root: Path) -> Path:
    """克隆并 checkout 到 base_commit（**宿主**操作：沙箱无网络）。"""
    target = work_root / instance.instance_id
    if (target / ".git").is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        [
            "git", "clone", "--quiet", "--filter=blob:none",
            f"https://github.com/{instance.repo}.git", str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if clone.returncode != 0:
        raise SwebenchError(f"克隆失败 {instance.repo}: {clone.stderr[:200]}")
    checkout = subprocess.run(
        ["git", "checkout", "--quiet", instance.base_commit],
        cwd=target, capture_output=True, check=False,
    )
    if checkout.returncode != 0:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", instance.base_commit],
            cwd=target, capture_output=True, check=False,
        )
        checkout = subprocess.run(
            ["git", "checkout", "--quiet", instance.base_commit],
            cwd=target, capture_output=True, check=False,
        )
    if checkout.returncode != 0:
        raise SwebenchError(f"checkout 失败 {instance.instance_id}")
    return target


def reset_repo(repo_dir: Path, instance: SweInstance, *, keep_test_patch: bool) -> None:
    """把工作区恢复到 base_commit（可选保留 test_patch）。"""
    subprocess.run(["git", "reset", "-q", "--hard"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "clean", "-qfd"], cwd=repo_dir, capture_output=True)
    if keep_test_patch and instance.test_patch:
        subprocess.run(
            ["git", "apply", "-"], cwd=repo_dir,
            input=instance.test_patch.encode("utf-8"), capture_output=True,
        )


def apply_patch_text(repo_dir: Path, patch: str, *, reverse: bool = False) -> bool:
    argv = ["git", "apply", "-"]
    if reverse:
        argv.insert(2, "--reverse")
    proc = subprocess.run(
        argv, cwd=repo_dir, input=patch.encode("utf-8"), capture_output=True, check=False
    )
    return proc.returncode == 0


def run_tests_in_sandbox(
    image: str,
    repo_dir: Path,
    node_ids: tuple[str, ...],
    *,
    timeout_s: int = 1800,
    container_pythonpath: str | None = None,
) -> tuple[int, str]:
    """在沙箱里跑指定 node id（`--network none` + 只读根，与生产沙箱同姿态）。"""
    mount = str(Path(repo_dir).resolve())
    cmd = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,exec", "-v", f"{mount}:/workspace",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-w", "/workspace",
    ]
    if container_pythonpath:
        cmd += ["-e", f"PYTHONPATH={container_pythonpath}"]
    cmd += [image, "python", "-m", "pytest", "-p", "no:cacheprovider", "-q",
            f"--timeout={timeout_s}", *node_ids]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def calibrate(
    instance: SweInstance,
    *,
    work_root: Path,
    container_pythonpath: str | None = None,
) -> tuple[bool, str]:
    """**准入标定**：gold patch 必须让 FAIL_TO_PASS 从失败翻成通过。"""
    repo_dir = prepare_repo(instance, work_root)
    reset_repo(repo_dir, instance, keep_test_patch=True)
    node_ids = resolve_node_ids(repo_dir, instance.fail_to_pass)
    before_rc, before_out = run_tests_in_sandbox(
        instance.image, repo_dir, node_ids, container_pythonpath=container_pythonpath
    )
    if before_rc == 0:
        return False, "FAIL_TO_PASS 在打补丁前就全绿（题目前提不成立）"
    if not apply_patch_text(repo_dir, instance.gold_patch):
        return False, "gold patch 无法应用"
    after_rc, after_out = run_tests_in_sandbox(
        instance.image, repo_dir, node_ids, container_pythonpath=container_pythonpath
    )
    reset_repo(repo_dir, instance, keep_test_patch=False)
    if after_rc != 0:
        tail = "\n".join(after_out.strip().splitlines()[-4:])
        return False, f"gold patch 后仍失败: {tail[:200]}"
    return True, "标定通过（失败 → 通过）"


def build_repair_prompt(instance: SweInstance, repo_dir: Path) -> tuple[str, int]:
    """用 P2 的定位器选候选文件，拼出"请给补丁"的 prompt。返回 `(prompt, 命中文件数)`。

    复用闭环的做法（先定位再让模型写 diff），这样 SWE-bench 评测走的就是**上线那条路**，
    而不是另写一个理想化的输入。
    """
    from patchpilot.index.retrieval import Localizer
    from patchpilot.index.symbols import index_repo

    index = index_repo(repo_dir)
    paths = Localizer(index).localize_paths(instance.problem, limit=3)
    snippets: list[str] = []
    for rel in paths:
        try:
            text = (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        snippets.append(f"### {rel}\n```\n{text[:4000]}\n```")
    prompt = "\n".join(
        [
            "你是代码修复助手。下面是仓库里真实 issue 的描述。",
            "请只输出一个 unified diff 补丁（`git apply -` 可直接应用），不要任何解释。",
            "补丁的路径是仓库相对路径。",
            "",
            "## Issue",
            instance.problem.strip(),
            "",
            "## 候选文件（自动定位）",
            *(snippets or ["<无>"]),
        ]
    )
    return prompt, len(paths)


def evaluate(
    instances: list[SweInstance],
    *,
    propose_patch,
    work_root: Path,
    container_pythonpath: str | None = None,
    max_rounds: int = 1,
) -> SweReport:
    """对每道题跑"模型产补丁 → 应用 → 跑 FAIL_TO_PASS"。

    `propose_patch(instance, round_index) -> str`：由调用方提供（通常包一层 LLM）。
    """
    outcomes: list[SweOutcome] = []
    for instance in instances:
        repo_dir = prepare_repo(instance, work_root)
        reset_repo(repo_dir, instance, keep_test_patch=True)
        node_ids = resolve_node_ids(repo_dir, instance.fail_to_pass)
        status = "failed"
        detail = ""
        cost = 0.0
        rounds = 0
        for attempt in range(max_rounds):
            rounds = attempt + 1
            patch, spent = propose_patch(instance, attempt)
            cost += spent
            if not patch.strip():
                detail = "模型没有产出补丁"
                continue
            if not apply_patch_text(repo_dir, patch):
                detail = "候选补丁无法应用"
                reset_repo(repo_dir, instance, keep_test_patch=True)
                continue
            rc, _out = run_tests_in_sandbox(
                instance.image, repo_dir, node_ids,
                container_pythonpath=container_pythonpath,
            )
            if rc == 0:
                status = "passed"
                detail = f"FAIL_TO_PASS 全绿（第 {rounds} 轮）"
                break
            detail = "FAIL_TO_PASS 仍有失败"
            reset_repo(repo_dir, instance, keep_test_patch=True)
        reset_repo(repo_dir, instance, keep_test_patch=False)
        outcomes.append(
            SweOutcome(
                instance_id=instance.instance_id,
                status=status,
                detail=detail,
                rounds=rounds,
                cost_usd=round(cost, 6),
            )
        )
    meta = {
        "instances": [i.instance_id for i in instances],
        "images": sorted({i.image for i in instances}),
        "note": (
            "N 很小，只能当链路验证；并非论文级 pass@1。"
            "受限原因见 eval/swebench_subset.json 的 notes 与 PLAN。"
        ),
    }
    return SweReport(outcomes=tuple(outcomes), meta=meta)


__all__ = [
    "SweReport",
    "SweInstance",
    "SweOutcome",
    "SwebenchError",
    "apply_patch_text",
    "build_repair_prompt",
    "calibrate",
    "evaluate",
    "load_subset",
    "prepare_repo",
    "reset_repo",
    "resolve_node_ids",
    "run_tests_in_sandbox",
]
