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
    #: 若给出**解释器路径**，则在宿主用该解释器跑测试（解锁旧 Python：
    #: 镜像仓库不可用时拿不到旧版本基础镜像）。空表示走 docker。
    python: str = ""
    #: 仓库内需要加进 PYTHONPATH 的子目录（flask 是 `src`，sympy 是空）
    pythonpath: str = ""


@dataclass(frozen=True, slots=True)
class SweOutcome:
    instance_id: str
    status: str  # passed | failed | invalid | error
    detail: str = ""
    rounds: int = 0
    cost_usd: float = 0.0
    #: 每个 F2P 名解析到的 node id（多个用 ` | ` 连接；`<无>` 表示找不到）
    targets: tuple[tuple[str, str], ...] = ()
    #: 实际观察到的 `(node id, 状态)`，供审计
    observed: tuple[tuple[str, str], ...] = ()

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
                    "targets": dict(o.targets),
                    "observed": dict(o.observed),
                }
                for o in self.outcomes
            ],
        }


def _expand(value: str) -> str:
    r"""展开 `${VAR}` 占位；未设置时报错，而不是静默传空字符串。"""
    import os

    expanded = os.path.expandvars(value)
    if "$" in expanded:
        raise SwebenchError(
            f"解释器路径未展开（占位变量未设置）: {value}。"
            "请设置环境变量，例如 PP_SWEBENCH_PYTHON=<python3.9 路径>"
        )
    return expanded


#: 解释器自检的回应标记。**必须核对输出**：`cmd.exe -c "..."` 会忽略参数并返回 0
#: （本轮实测），所以"退出码为 0"根本不能证明它是 Python。
_PY_PROBE = "PATCHPILOT-PY-OK"


def validate_interpreter(python: str) -> str:
    """校验"这确实是个装了 pytest 的 Python"，挡住把 `cmd.exe` 之类塞进子集。

    判据不是退出码，而是**子进程输出的标记**：只有真的执行了我们给的那行 Python，
    才会打印 `PATCHPILOT-PY-OK <版本>`。
    """
    import os

    candidate = Path(python)
    if not candidate.is_file():
        raise SwebenchError(f"解释器不存在: {python}")
    if not os.access(candidate, os.X_OK):
        raise SwebenchError(f"解释器不可执行: {python}")
    code = (
        "import sys, pytest\n"
        f"sys.stdout.write('{_PY_PROBE} ' + sys.version.split()[0] + ' ' + pytest.__version__)\n"
    )
    try:
        proc = subprocess.run(
            [python, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SwebenchError(f"解释器 {python} 无法执行: {type(exc).__name__}") from exc
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stdout.startswith(_PY_PROBE):
        raise SwebenchError(
            f"{python} 不是可用的 Python+pytest（需要输出 {_PY_PROBE} 标记）: "
            f"rc={proc.returncode} out={stdout[:120]!r} "
            f"err={(proc.stderr or '').strip()[:120]!r}"
        )
    parts = stdout.split()
    return parts[1] if len(parts) > 1 else stdout


def _check_pythonpath(value: str, *, instance_id: str) -> str:
    """`pythonpath` 必须是仓库内的**相对**子目录（`..`、绝对路径、UNC 一律拒绝）。"""
    if not value:
        return ""
    if Path(value).is_absolute() or value.startswith("\\\\"):
        raise SwebenchError(f"{instance_id}: pythonpath 不能是绝对路径/UNC: {value!r}")
    if ".." in Path(value).parts:
        raise SwebenchError(f"{instance_id}: pythonpath 不能含 `..`: {value!r}")
    return value


def load_subset(path: Path, *, validate_interpreters: bool = True) -> list[SweInstance]:
    r"""读子集 JSON。文件里的题必须**已经**通过标定（见模块说明）。

    解释器路径里的 `${VAR}` 会被展开（子集入库时用环境变量占位，
    因为本机绝对路径不该进版本库）；占位变量未设置时报清晰错误。

    `image` 在**宿主解释器模式**下可省略（`python` 非空时不需要镜像）。
    `validate_interpreters=True` 时会真的跑一次该解释器确认它有 pytest ——
    这是为了挡住"子集被改成 `cmd.exe` 就 pass@1 变 100%"那一类失真。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    instances: list[SweInstance] = []
    checked: dict[str, str] = {}
    for item in payload.get("instances", []):
        required = {"instance_id", "repo", "base_commit", "fail_to_pass", "problem"}
        missing = required - set(item)
        if missing:
            raise SwebenchError(f"{item.get('instance_id', '?')}: 缺字段 {sorted(missing)}")
        if not item.get("image") and not item.get("python"):
            raise SwebenchError(
                f"{item.get('instance_id', '?')}: 必须给出 `image` 或 `python` 之一"
            )
        instance_id = str(item["instance_id"])
        python = _expand(str(item.get("python") or ""))
        if python:
            if validate_interpreters and python not in checked:
                checked[python] = validate_interpreter(python)
            pythonpath = _check_pythonpath(
                str(item.get("pythonpath") or ""), instance_id=instance_id
            )
        else:
            pythonpath = ""
        instances.append(
            SweInstance(
                instance_id=instance_id,
                repo=str(item["repo"]),
                base_commit=str(item["base_commit"]),
                image=str(item.get("image") or ""),
                fail_to_pass=tuple(item["fail_to_pass"]),
                problem=str(item["problem"]),
                test_patch=str(item.get("test_patch") or ""),
                version=str(item.get("version") or ""),
                pass_to_pass=tuple(item.get("pass_to_pass") or ()),
                gold_patch=str(item.get("gold_patch") or ""),
                notes=str(item.get("notes") or ""),
                python=python,
                pythonpath=pythonpath,
            )
        )
    if not instances:
        raise SwebenchError(f"子集为空: {path}")
    return instances


def resolve_node_ids(repo_dir: Path, names: tuple[str, ...]) -> tuple[str, ...]:
    """把**裸测试名**解析成 `文件::用例`。

    **歧义时不挑一个**：同名测试出现在多个文件时全部返回（官方 harness 对 sympy 用的是
    sympy 自己的 runner，状态表的键就是裸名，所以"同名"在官方口径里也是同一个键；
    我们这边保守处理——把全部候选都跑，判分时要求"至少一个 PASSED 且没有失败"）。
    解析不到的原样保留，由判分阶段判为"无效题"而不是静默少跑。
    """
    targets = resolve_targets(repo_dir, names)
    resolved: list[str] = []
    for raw in names:
        found = targets.get(raw, ())
        resolved.extend(found or (raw,))
    return tuple(resolved)


def resolve_targets(
    repo_dir: Path, names: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    """`F2P 名 -> 所有可能的 node id`（不静默择一；解析不到给空元组）。"""
    mapping: dict[str, tuple[str, ...]] = {}
    for raw in names:
        if "::" in raw:
            mapping[raw] = (raw,)
            continue
        plain = raw.split("::")[-1]
        pattern = re.compile(_DEF_RE.pattern.format(name=re.escape(plain)), re.MULTILINE)
        found: list[str] = []
        for path in sorted(repo_dir.rglob("test_*.py")):
            try:
                if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                    found.append(f"{path.relative_to(repo_dir).as_posix()}::{plain}")
            except OSError:
                continue
        mapping[raw] = tuple(found)
    return mapping


#: pytest 短摘要里的状态词（与官方 `TestStatus` 对齐；`XFAIL` 与 `PASSED` 同权）。
_PASS_STATUSES = frozenset({"PASSED", "XFAIL"})
_FAIL_STATUSES = frozenset({"FAILED", "ERROR", "SKIPPED"})
_STATUS_WORDS = _PASS_STATUSES | _FAIL_STATUSES | {"XPASS"}


def parse_pytest_status(log: str) -> dict[str, str]:
    """从 `pytest -rA` 的短摘要解析 `{用例: 状态}`。

    **口径照官方 `parse_log_pytest`**（`swebench/harness/log_parsers/python.py`）：
    只看以状态词开头的行，取第二个字段为用例名；`FAILED` 行会带 ` - 原因`，先切掉。
    与官方一致地**不信退出码**——我们的上一版只看 `rc == 0`，`cmd.exe -m pytest` 都能满足它。
    """
    status_map: dict[str, str] = {}
    for line in log.splitlines():
        stripped = line.strip()
        head = stripped.split(" ", 1)[0] if stripped else ""
        if head not in _STATUS_WORDS:
            continue
        body = stripped.replace(" - ", " ") if head == "FAILED" else stripped
        parts = body.split()
        if len(parts) < 2:
            continue
        case = parts[1]
        # `-rA` 也可能打印 `SKIPPED [3] path:line: reason`，`[3]` 不是用例
        if head == "SKIPPED" and case.startswith("[") and case.rstrip("]").lstrip("[").isdigit():
            continue
        status_map[case] = head
    return status_map


def _lookup(status_map: dict[str, str], node_id: str) -> str | None:
    """按完整 node id 查，查不到再按 `::` 之后的末段查（裸名的情形）。"""
    if node_id in status_map:
        return status_map[node_id]
    tail = node_id.split("::")[-1]
    return status_map.get(tail)


@dataclass(frozen=True, slots=True)
class Grade:
    """一次判分结果（可审计：带上目标与每个目标的实际状态）。"""

    resolved: bool
    valid: bool
    reason: str
    targets: tuple[tuple[str, tuple[str, ...]], ...] = ()
    observed: tuple[tuple[str, str], ...] = ()

    def observed_map(self) -> dict[str, str]:
        return dict(self.observed)


def grade_fail_to_pass(
    fail_to_pass: tuple[str, ...],
    targets: dict[str, tuple[str, ...]],
    log: str,
) -> Grade:
    """判断 `FAIL_TO_PASS` 是否全部通过（官方口径 + 保守化）。

    - 一个用例都没解析到 → `valid=False`（"没跑起来"不等于"修好了"；
      官方同样在没有结果且没有 suite 运行痕迹时判 invalid）。
    - 对每个 F2P 名：解析到 0 个候选 → 无效（我们连该跑哪儿都不知道）。
    - 名字出现多次（同名测试在多文件）→ **至少一个 PASSED 且没有任何 FAILED/ERROR/SKIPPED**。
    - 状态缺失按失败处理（与官方 `test_failed` 的 `key is None` 一致）。
    """
    status_map = parse_pytest_status(log)
    if not status_map:
        return Grade(
            resolved=False,
            valid=False,
            reason="日志里解析不到任何用例结果（测试没跑起来或输出被截断）",
        )
    observed: list[tuple[str, str]] = []
    unresolved: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    for case in fail_to_pass:
        candidates = targets.get(case, ())
        if not candidates:
            unresolved.append(case)
            continue
        statuses = [(cand, _lookup(status_map, cand)) for cand in candidates]
        known = [(cand, st) for cand, st in statuses if st is not None]
        observed.extend((cand, st or "MISSING") for cand, st in statuses)
        if not known:
            missing.append(case)
            continue
        if any(st in _FAIL_STATUSES for _cand, st in known):
            failed.append(case)
            continue
        if not any(st in _PASS_STATUSES for _cand, st in known):
            failed.append(case)
    if unresolved:
        return Grade(
            resolved=False,
            valid=False,
            reason=f"有 {len(unresolved)} 个 FAIL_TO_PASS 名在本仓库找不到测试文件: "
            + ", ".join(unresolved[:3]),
            targets=tuple(targets.items()),
            observed=tuple(observed),
        )
    if missing:
        return Grade(
            resolved=False,
            valid=True,
            reason=f"{len(missing)} 个用例没有出现在日志里（按失败计）: "
            + ", ".join(missing[:3]),
            targets=tuple(targets.items()),
            observed=tuple(observed),
        )
    if failed:
        return Grade(
            resolved=False,
            valid=True,
            reason=f"{len(failed)} 个用例未通过: " + ", ".join(failed[:3]),
            targets=tuple(targets.items()),
            observed=tuple(observed),
        )
    return Grade(
        resolved=True,
        valid=True,
        reason=f"{len(fail_to_pass)} 个用例全部通过",
        targets=tuple(targets.items()),
        observed=tuple(observed),
    )


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


def run_tests_on_host(
    python: str,
    repo_dir: Path,
    node_ids: tuple[str, ...],
    *,
    pythonpath: str = "",
    timeout_s: int = 300,
) -> tuple[int, str]:
    """在**宿主**上用指定解释器跑测试（评测专用，见模块说明的安全取舍）。

    - 加 `-rA`：判分要读短摘要（只看退出码会被 `cmd.exe -m pytest` 满足）。
    - `stdin=DEVNULL` + `subprocess` 级 `timeout`：没有它，交互式程序会永久挂住
      （上一轮"跑了半小时没输出"就是这么来的）。
    - `pythonpath` 是仓库内子目录（如 flask 的 `src`）；不做的话 `import flask`
      会拿到宿主装的那个 flask，而不是被测源码。
    """
    import os

    repo_dir = Path(repo_dir).resolve()
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    env["PYTHONPATH"] = str(repo_dir / pythonpath) if pythonpath else str(repo_dir)
    argv = [
        python, "-m", "pytest", "-p", "no:cacheprovider", "-rA", "-q",
        f"--timeout={timeout_s}", *node_ids,
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s + 120,
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        for stream in (exc.stdout, exc.stderr):
            if isinstance(stream, bytes):
                partial += stream.decode("utf-8", "replace")
            elif stream:
                partial += stream
        return 124, partial + f"\n[超时] 测试进程超过 {timeout_s + 120}s 被终止"
    except OSError as exc:
        return 126, f"[无法启动解释器] {type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_tests(
    instance: SweInstance, repo_dir: Path, node_ids: tuple[str, ...]
) -> tuple[int, str]:
    """按实例配置选运行方式：有 `python` 走宿主解释器，否则走 docker 沙箱。"""
    if instance.python:
        return run_tests_on_host(
            instance.python, repo_dir, node_ids, pythonpath=instance.pythonpath
        )
    # docker 模式也要把 pythonpath 带进去（flask 这类 `src/` 布局否则必然 import 失败）
    return run_tests_in_sandbox(
        instance.image,
        repo_dir,
        node_ids,
        container_pythonpath=instance.pythonpath or None,
    )


def _targets_for(
    instance: SweInstance, repo_dir: Path, node_ids: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    if node_ids:
        return {name: (name,) for name in node_ids}
    return resolve_targets(repo_dir, instance.fail_to_pass)


def calibrate(
    instance: SweInstance,
    *,
    work_root: Path,
) -> tuple[bool, str]:
    """**准入标定**：gold patch 必须让 FAIL_TO_PASS 从失败翻成通过。

    判分口径与 `evaluate` 完全一致（读日志状态表，不看退出码），
    否则"标定通过"与"评测通过"说的不是一回事。
    """
    repo_dir = prepare_repo(instance, work_root)
    reset_repo(repo_dir, instance, keep_test_patch=True)
    targets = resolve_targets(repo_dir, instance.fail_to_pass)
    node_ids = tuple(cand for cands in targets.values() for cand in cands)
    if not node_ids:
        return False, "FAIL_TO_PASS 在仓库里找不到任何测试文件（题目无法标定）"

    _, before_log = run_tests(instance, repo_dir, node_ids)
    before = grade_fail_to_pass(instance.fail_to_pass, targets, before_log)
    if not before.valid:
        return False, f"标定前无法判分: {before.reason}"
    if before.resolved:
        return False, "FAIL_TO_PASS 在打补丁前就全绿（题目前提不成立）"
    if not apply_patch_text(repo_dir, instance.gold_patch):
        return False, "gold patch 无法应用"
    _, after_log = run_tests(instance, repo_dir, node_ids)
    after = grade_fail_to_pass(instance.fail_to_pass, targets, after_log)
    reset_repo(repo_dir, instance, keep_test_patch=False)
    if not after.valid:
        return False, f"标定后无法判分: {after.reason}"
    if not after.resolved:
        return False, f"gold patch 后仍失败: {after.reason}"
    return True, f"标定通过（失败 → 通过，{len(node_ids)} 个目标）"


#: 索引缓存已**删除**（第三十二轮复审 BLOCKING）：按"仓库名"跨 commit 复用，
#: 会把第 1 题的索引套到其余 10 题上——实测 gold 文件只命中 4/11、且结果依赖题目顺序。
#: 现在每题各自建索引（慢 ~40s/题，宁慢不可错）。


def build_repair_prompt(
    instance: SweInstance,
    repo_dir: Path,
    *,
    max_files: int = 2,
    snippet_chars: int = 2500,
) -> tuple[str, int]:
    """用 P2 的定位器选候选文件，拼出"请给补丁"的 prompt。返回 `(prompt, 可读候选数)`。

    复用闭环的做法（先定位再让模型写 diff），这样 SWE-bench 评测走的就是**上线那条路**。

    **没有索引缓存**（第三十二轮复审的 BLOCKING）：上一版按"仓库名"缓存索引，
    11 题全是 sympy → 10 题的候选是用第 1 题那个 commit 的索引选出来的，
    实测 gold 文件只命中 4/11，结果还依赖题目顺序。宁慢不可错。
    """
    from patchpilot.index.retrieval import Localizer
    from patchpilot.index.symbols import index_repo

    index = index_repo(repo_dir)
    paths = Localizer(index).localize_paths(instance.problem, limit=max_files)
    snippets: list[str] = []
    readable = 0
    for rel in paths:
        try:
            text = (repo_dir / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        readable += 1
        snippets.append(f"### {rel}\n```\n{text[:snippet_chars]}\n```")
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
    return prompt, readable


def evaluate(
    instances: list[SweInstance],
    *,
    propose_patch,
    work_root: Path,
    max_rounds: int = 1,
) -> SweReport:
    """对每道题跑"模型产补丁 → 应用 → 跑 FAIL_TO_PASS → **按日志判分**"。

    `propose_patch(instance, round_index) -> (patch, cost)`：由调用方提供。

    判分不经退出码（官方 `grading.py` 也不看）：`grade_fail_to_pass` 读 `pytest -rA`
    的状态表逐条比对，`SKIPPED` 计失败，一条结果都没解析到则判 `invalid`，
    而不是塞进"模型失败"里。
    """
    outcomes: list[SweOutcome] = []
    for instance in instances:
        repo_dir = prepare_repo(instance, work_root)
        reset_repo(repo_dir, instance, keep_test_patch=True)
        targets = resolve_targets(repo_dir, instance.fail_to_pass)
        node_ids = tuple(cand for cands in targets.values() for cand in cands)
        target_view = tuple((case, " | ".join(cands) or "<无>") for case, cands in targets.items())
        if not node_ids:
            reset_repo(repo_dir, instance, keep_test_patch=False)
            outcomes.append(
                SweOutcome(
                    instance_id=instance.instance_id,
                    status="invalid",
                    detail="FAIL_TO_PASS 在仓库里找不到任何测试文件",
                    targets=target_view,
                )
            )
            continue
        status = "failed"
        detail = ""
        cost = 0.0
        rounds = 0
        observed: tuple[tuple[str, str], ...] = ()
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
            _rc, log = run_tests(instance, repo_dir, node_ids)
            grade = grade_fail_to_pass(instance.fail_to_pass, targets, log)
            observed = grade.observed
            if not grade.valid:
                status = "invalid"
                detail = grade.reason
                break
            if grade.resolved:
                status = "passed"
                detail = f"{grade.reason}（第 {rounds} 轮）"
                break
            detail = grade.reason
            reset_repo(repo_dir, instance, keep_test_patch=True)
        reset_repo(repo_dir, instance, keep_test_patch=False)
        outcomes.append(
            SweOutcome(
                instance_id=instance.instance_id,
                status=status,
                detail=detail,
                rounds=rounds,
                cost_usd=round(cost, 6),
                targets=target_view,
                observed=observed,
            )
        )
    meta = {
        "instances": [i.instance_id for i in instances],
        "images": sorted({i.image for i in instances if i.image}),
        "grading": "pytest -rA 状态表逐条比对 F2P；SKIPPED 计失败；无结果判 invalid",
        "note": (
            "N 很小，只能当链路验证；并非论文级 pass@1。"
            "受限原因见 eval/swebench_subset.json 的 notes 与 PLAN。"
        ),
    }
    return SweReport(outcomes=tuple(outcomes), meta=meta)


__all__ = [
    "Grade",
    "SweReport",
    "SweInstance",
    "SweOutcome",
    "SwebenchError",
    "apply_patch_text",
    "build_repair_prompt",
    "calibrate",
    "evaluate",
    "grade_fail_to_pass",
    "load_subset",
    "parse_pytest_status",
    "prepare_repo",
    "reset_repo",
    "resolve_node_ids",
    "resolve_targets",
    "run_tests",
    "run_tests_in_sandbox",
    "run_tests_on_host",
]
