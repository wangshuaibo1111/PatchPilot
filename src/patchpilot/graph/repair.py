"""P3：真实修复闭环（8 节点图）。

与 P0 的 `minimal.py` 的区别：那个是**骨架自测**（stub 模拟、`PassPolicy` 决定何时"通过"）；
本模块是**真闭环**：定位真实代码、让模型产出补丁、在**沙箱副本**里跑真实测试、
失败则携带记忆重试，并把每一步计入预算与 trace。

## 8 个节点

| 节点 | 职责 | 失败时的去向 |
|---|---|---|
| `load_issue` | 读 issue 文本，抽取"待修复行为"关键词 | 空 issue → 直接终止（fail） |
| `locate` | 用 `tools.search` 找候选文件 | 无候选 → 记 `LOCALIZE`（定位失败） |
| `generate` | 让模型产出 **unified diff** 补丁 | 不可解析 → 记失败记忆并重试 |
| `apply` | 把补丁打到**副本**上（`git apply`，路径受根约束） | 打不上 → 记失败记忆，重试 |
| `run_tests` | 在沙箱里跑 pytest，取 JUnit 结构化结果 | — |
| `evaluate` | 判定是否通过（`JUnitReport.ok`） | 未通过 → `reflect` |
| `reflect` | 把失败用例与输出压成**失败记忆**（供下一轮 prompt） | 预算/轮数耗尽 → 终止 |
| `finalize` | 产出补丁文件 + 报告 + trace | — |

## 可测试性（刻意设计）

- **模型**走 `LLMClient` 协议：测试注入确定性的假模型，无需网络/Key；
- **测试执行**走可注入的 `TestRunner` 协议：默认实现起 Docker 沙箱，
  测试注入"内存假沙箱"，因此**无 Docker 也能跑全图**；
- 图本身是纯函数式装配，`build_repair_graph(runner=..., llm=...)` 可任意替换这两处。

## 与预算/trace 的关系

沿用 `minimal.py` 已验证过的 `_guard` 模式：每个节点都经 `BudgetTracker`，
触顶即写 `budget_exceeded` 并终止；用量与成本照常进 trace。
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from patchpilot.core.budget import Budget, BudgetExceeded, BudgetTracker
from patchpilot.core.llm import ChatMessage, LLMClient, LLMResponse
from patchpilot.core.logging import get_logger
from patchpilot.core.models import FailureKind
from patchpilot.core.pricing import estimate_cost_usd
from patchpilot.core.textio import write_text_lf
from patchpilot.observability.tracing import Tracer
from patchpilot.sandbox.manager import SandboxResult, run_pytest_in_sandbox
from patchpilot.sandbox.policy import SandboxPolicy
from patchpilot.sandbox.workspace import copy_workspace
from patchpilot.tools import git_server, search_server

NOTES_LIMIT = 200
MEMORY_LIMIT = 20


def _merge_notes(left: list[str] | None, right: list[str] | None) -> list[str]:
    return (list(left or []) + list(right or []))[-NOTES_LIMIT:]


def _merge_memory(left: list[str] | None, right: list[str] | None) -> list[str]:
    """失败记忆通道：累加但有上界（长回环不能把状态撑爆）。"""
    return (list(left or []) + list(right or []))[-MEMORY_LIMIT:]


#: 未启用 trace 时的空 span（让节点代码只有一条路径，不必到处写 if tracer）
_NULL_SPAN = contextlib.nullcontext()


# ---------------------------------------------------------------- 可注入的两处依赖


class TestRunner(Protocol):
    """在沙箱（或测试替身）里跑测试。默认实现起 Docker。"""

    def __call__(self, *, source: Path, pytest_args: Sequence[str]) -> SandboxResult: ...


class PatchApplier(Protocol):
    """把补丁应用到**工作副本**（不是用户仓库）。默认实现用 `git apply`。"""

    def __call__(self, *, workspace: Path, patch: str) -> str: ...


@dataclass(frozen=True, slots=True)
class RepairOptions:
    """一次修复任务的配置。

    `workspace_factory(repo) -> 工作副本路径`：**补丁只打在副本上**，
    用户的仓库在任何路径下都不被改动（这是本项目的硬规则之一）。
    """

    repo: Path
    issue_file: Path
    tests: tuple[str, ...] = ()  # 目标测试路径；空表示整个套件
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    budget: Budget = field(default_factory=Budget)
    max_rounds: int = 3
    locate_limit: int = 8
    copy_root: Path | None = None
    # 补丁产物目录。**默认必须与 issue 文件分开**：评测时 issue 文件在语料仓库里，
    # 早先按 `issue_file.parent` 落盘会把 `*.diff` 写进被评测的仓库（实测污染了 6 个语料目录）。
    output_dir: Path | None = None
    # trace 目录。为 None 时**完全不产生 IO**（离线测试与门禁不受影响）。
    trace_dir: Path | None = None
    price_overrides: Mapping[str, tuple[float, float]] | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds 必须 >= 1")


class RepairState(TypedDict, total=False):
    # 输入
    task_id: str
    issue: str
    candidates: list[str]
    # 生成/应用
    patch: str
    apply_ok: bool
    # `workspace` **必须**是声明的通道：LangGraph 会**静默丢弃**未声明的键，
    # 于是 `run_tests` 里 `state.get("workspace")` 恒为空，悄悄回落到未打补丁的
    # 源仓库，表现为「每轮都失败但补丁其实是好的」。与 `attempts` 同一类坑。
    workspace: str
    # 验证
    test_summary: dict[str, Any]
    passed: bool
    # 记忆与轮次
    round: int
    # `attempts` **必须**进状态：`BudgetTracker.to_state()` 写这个键，
    # 少了它 LangGraph 不持久化已有轮数，`from_state` 每次从 0 开始——
    # 于是预算的 `max_attempts` 永不触发、图停不下来
    # （实测：`max_rounds=2` 却跑了十几轮，直到把脚本用完）。
    attempts: int
    memory: Annotated[list[str], _merge_memory]
    failure_kind: str
    final_patch_path: str
    trace_path: str
    # 预算/观测
    started_at: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    unpriced_models: list[str]
    budget_exceeded: str
    notes: Annotated[list[str], _merge_notes]


# ---------------------------------------------------------------- issue 与定位


#: 从 issue 里抽"待修复行为"的关键词：报错片段、`call()`、`Class.method`、回溯行。
_ERROR_LINE = re.compile(r'File "([^"]+)", line (\d+)')
_CALL_LIKE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_IDENT_LIKE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\b")
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "when", "will", "not", "but",
        "into", "from", "have", "has", "was", "were", "are", "should", "would",
        "issue", "bug", "test", "tests", "code", "file", "files", "return",
    }
)


def extract_terms(issue: str, *, limit: int = 12) -> list[str]:
    """从 issue 文本抽出用于定位的检索词（确定性、无需模型）。

    优先级：回溯里的文件名 > `call_like(` 的名字 > 一般标识符。
    去重且保序，便于 prompt 与日志复现。
    """
    found: list[str] = []
    for match in _ERROR_LINE.finditer(issue):
        name = Path(match.group(1)).name
        if name not in found:
            found.append(name)
    for pattern in (_CALL_LIKE, _IDENT_LIKE):
        for match in pattern.finditer(issue):
            token = match.group(1)
            if token.lower() in _STOPWORDS or token in found:
                continue
            found.append(token)
            if len(found) >= limit:
                return found[:limit]
    return found[:limit]


def build_prompt(state: RepairState, options: RepairOptions) -> str:
    """构造生成补丁的 prompt（含候选文件片段与失败记忆）。"""
    parts = [
        "你是代码修复助手。下面是仓库里的一个 issue，请只输出一个 unified diff 补丁，",
        "不要任何解释文字。补丁必须能 `git apply` 到仓库根，路径为仓库相对路径。",
        "",
        "## Issue",
        state.get("issue", "").strip(),
        "",
        "## 候选文件",
    ]
    for name in state.get("candidates", [])[: options.locate_limit]:
        snippet = _read_snippet(options.repo, name)
        parts.append(f"### {name}\n```\n{snippet}\n```")
    memory = state.get("memory", [])
    if memory:
        parts += ["", "## 前几轮失败记忆（务必避免重复同样的错误）"]
        parts += [f"- {item}" for item in memory]
    previous = state.get("patch")
    if previous:
        parts += ["", "## 上一轮失败补丁", "```diff", previous[:2000], "```"]
    return "\n".join(parts)


def _read_snippet(repo: Path, name: str, *, max_chars: int = 2000) -> str:
    try:
        text = (repo / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "<不可读>"
    return text[:max_chars]


# ---------------------------------------------------------------- 补丁解析


_DIFF_START = re.compile(r"^(diff --git |--- |\+\+\+ )", re.MULTILINE)


def extract_patch(text: str) -> str:
    """从模型输出里抽出 unified diff（模型常会在前后加解释或 ```diff 围栏）。"""
    fenced = re.findall(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL)
    for block in fenced:
        if _DIFF_START.search(block):
            return block.strip() + "\n"
    match = _DIFF_START.search(text)
    if match:
        return text[match.start() :].strip() + "\n"
    return ""


# ---------------------------------------------------------------- 图


def build_repair_graph(
    *,
    options: RepairOptions,
    llm: LLMClient,
    runner: TestRunner | None = None,
    applier: PatchApplier | None = None,
) -> StateGraph:
    """装配 8 节点修复图。`runner` / `applier` 可注入（测试用替身，无需 Docker）。"""
    log = get_logger("repair")
    _runner: TestRunner = runner or _default_runner(options)
    _applier: PatchApplier = applier or _default_applier
    tracer = (
        Tracer(options.trace_dir, "repair", price_overrides=options.price_overrides)
        if options.trace_dir is not None
        else None
    )

    def _guard(
        state: RepairState, body: Callable[[BudgetTracker], dict[str, Any]]
    ) -> dict[str, Any]:
        tracker = BudgetTracker.from_state(options.budget, state)
        try:
            payload = body(tracker)
        except BudgetExceeded as exc:
            update = tracker.to_state()
            update["budget_exceeded"] = exc.reason
            update["notes"] = [f"budget: 熔断终止（{exc.reason}）"]
            log.warning("repair.budget_exceeded", reason=exc.reason)
            return update
        update = tracker.to_state()
        update.update(payload)
        return update

    def _charge(tracker: BudgetTracker, response: LLMResponse) -> None:
        cost = response.cost_usd
        if cost is None:
            # 客户端没给成本时**先查价格表**再宣布"未定价"：否则配了价格表的模型也会被
            # 记成 unpriced，导致 trace 里 `cost_usd > 0` 与 `cost_unknown = true` **自相矛盾**
            # （真机 trace 实测到这一组合）。口径必须与 `Tracer.record_usage` 一致。
            cost = estimate_cost_usd(
                response.model,
                response.prompt_tokens,
                response.completion_tokens,
                options.price_overrides,
            )
        if cost is None:
            tracker.mark_unpriced(response.model)
            tracker.charge(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            return
        tracker.charge(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost_usd=cost,
        )

    # ---------------- 节点 1：load_issue
    def load_issue(state: RepairState) -> dict[str, Any]:
        try:
            issue = options.issue_file.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("repair.issue_unreadable", error=str(exc))
            return {
                "issue": "",
                "failure_kind": FailureKind.LOCALIZE.value,
                "notes": [f"load_issue: 读不到 issue 文件（{type(exc).__name__}）"],
            }
        terms = extract_terms(issue)
        log.info("repair.load_issue", chars=len(issue), terms=len(terms))
        update: dict[str, Any] = {
            "issue": issue,
            "notes": [f"load_issue: {len(issue)} 字符，抽取 {len(terms)} 个检索词 {terms[:5]}"],
        }
        if "started_at" not in state:
            # 必须是"现在"：写 0.0 会让预算的时长上限算出天文数字的已用时长，
            # 于是**第一个节点就熔断**（实测 `时长超限 (1.3e6 s > 600 s)`）。
            update["started_at"] = time.monotonic()
        if "round" not in state:
            update["round"] = 0
        if "attempts" not in state:
            update["attempts"] = 0
        if "memory" not in state:
            update["memory"] = []
        if "budget_exceeded" not in state:
            update["budget_exceeded"] = ""
        return update

    # ---------------- 节点 2：locate
    def locate(state: RepairState) -> dict[str, Any]:
        if not state.get("issue"):
            return {"candidates": [], "notes": ["locate: 无 issue，跳过"]}
        terms = extract_terms(state.get("issue", ""))
        hits: list[str] = []
        sink = tracer.span("retrieval.search", terms=len(terms)) if tracer else _NULL_SPAN
        with sink:
            for term in terms:
                for match in search_server.search_symbol(options.repo, term):
                    if match.path not in hits:
                        hits.append(match.path)
                if len(hits) >= options.locate_limit:
                    break
            if not hits:  # 符号找不到就退化为文本检索
                for term in terms:
                    for match in search_server.search_text(
                        options.repo, term, max_results=options.locate_limit
                    ):
                        if match.path not in hits:
                            hits.append(match.path)
                    if len(hits) >= options.locate_limit:
                        break
        log.info("repair.locate", candidates=len(hits))
        update: dict[str, Any] = {
            "candidates": hits[: options.locate_limit],
            "notes": [f"locate: 命中 {len(hits)} 个候选文件 {hits[:3]}"],
        }
        if not hits:
            # 无候选 = 定位失败：必须**显式写归因**，否则收尾时状态里没有 failure_kind
            update["failure_kind"] = FailureKind.LOCALIZE.value
        return update

    # ---------------- 节点 3：generate
    def generate(state: RepairState) -> dict[str, Any]:
        def body(tracker: BudgetTracker) -> dict[str, Any]:
            round_no = tracker.start_attempt()
            prompt = build_prompt(state, options)
            llm_ctx = (
                tracer.span("llm.generate", round=round_no, prompt_chars=len(prompt))
                if tracer
                else _NULL_SPAN
            )
            with llm_ctx as llm_span:
                response = _complete(llm, prompt)
                if llm_span is not None:
                    llm_span.attributes["model"] = response.model
                    llm_span.attributes["text_chars"] = len(response.text)
            _charge(tracker, response)
            if tracer is not None:
                tracer.record_usage(
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
            patch = extract_patch(response.text)
            note = (
                f"generate: 第 {round_no} 轮产出补丁（{len(patch)} 字符）"
                if patch
                else f"generate: 第 {round_no} 轮模型输出里没有可识别的 diff"
            )
            log.info("repair.generate", round=round_no, patch_chars=len(patch))
            update: dict[str, Any] = {
                "patch": patch,
                "round": round_no,
                "notes": [note],
            }
            if not patch:
                update["failure_kind"] = FailureKind.EDIT.value
                update["memory"] = ["模型输出里没有可解析的 diff（要求只输出 unified diff）"]
            return update

        return _guard(state, body)

    # ---------------- 节点 4：apply
    def apply(state: RepairState) -> dict[str, Any]:
        """把补丁打到**工作副本**上（绝不改用户仓库），副本路径写回状态。"""
        patch = state.get("patch", "")
        if not patch:
            return {
                "apply_ok": False,
                "workspace": "",
                "memory": ["补丁为空或无法解析"],
                "notes": ["apply: 跳过（补丁为空）"],
            }
        # 每轮都从**源仓库**重新复制一份干净副本：上一轮的补丁不能叠加到这一轮。
        base = options.copy_root or options.policy.sandbox_root
        if base is None:
            return {
                "apply_ok": False,
                "workspace": "",
                "memory": ["未配置 copy_root/sandbox_root"],
                "notes": ["apply: 跳过（没有副本根）"],
            }
        try:
            Path(base).mkdir(parents=True, exist_ok=True)
            workspace = Path(base) / f"ws-{state.get('round', 0)}-{uuid.uuid4().hex[:6]}"
            tool_ctx = (
                tracer.span("tool.apply_patch", round=state.get("round", 0))
                if tracer
                else _NULL_SPAN
            )
            with tool_ctx:
                copy_workspace(options.repo, workspace)
                detail = _applier(workspace=workspace, patch=patch)
            ok = True
        except Exception as exc:  # noqa: BLE001 - 统一归一化为失败记忆
            ok = False
            # **不要**把 workspace 重置成 "."：那会把真实原因藏起来
            # （真机实测时它让"副本复制失败"看起来像"补丁应用失败"）。
            detail = f"{type(exc).__name__}: {exc}"
        log.info("repair.apply", ok=ok, workspace=str(workspace))
        return {
            "apply_ok": ok,
            "workspace": str(workspace),
            "memory": [] if ok else [f"补丁应用失败：{detail[:200]}"],
            "notes": [f"apply: {'通过' if ok else '失败'}（{str(detail)[:80]}）"],
        }

    # ---------------- 节点 5：run_tests
    def run_tests(state: RepairState) -> dict[str, Any]:
        if not state.get("apply_ok"):
            return {"test_summary": {}, "passed": False, "notes": ["run_tests: 跳过（补丁未应用）"]}

        def body(tracker: BudgetTracker) -> dict[str, Any]:
            test_ctx = (
                tracer.span("tool.run_tests", round=state.get("round", 0))
                if tracer
                else _NULL_SPAN
            )
            with test_ctx as test_span:
                result = _runner(
                    source=Path(state.get("workspace") or options.repo),
                    pytest_args=options.tests,
                )
                if test_span is not None:
                    test_span.attributes["exit_code"] = result.exit_code
                    test_span.attributes["timed_out"] = result.timed_out
            report = result.report
            summary = {
                "ok": result.ok,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_s": round(result.duration_s, 2),
                "total": report.total if report else None,
                "passed": report.passed if report else None,
                "failed": report.failed if report else None,
                "errors": report.errors if report else None,
                "failed_cases": [
                    c.node_id
                    for c in (report.cases if report else ())
                    if c.status == "failed"
                ][:10],
                "report_error": result.report_error,
            }
            log.info("repair.run_tests", ok=summary["ok"], failed=summary["failed"])
            return {
                "test_summary": summary,
                "passed": bool(result.ok),
                "notes": [
                    f"run_tests: total={summary['total']} failed={summary['failed']} "
                    f"ok={summary['ok']}"
                ],
            }

        return _guard(state, body)

    # ---------------- 节点 6：evaluate
    def evaluate(state: RepairState) -> dict[str, Any]:
        passed = bool(state.get("passed"))
        kind = FailureKind.NONE.value if passed else FailureKind.TEST_FAIL.value
        return {
            "failure_kind": kind,
            "notes": [f"evaluate: {'通过' if passed else '未通过'}"],
        }

    # ---------------- 节点 7：reflect
    def reflect(state: RepairState) -> dict[str, Any]:
        summary = state.get("test_summary") or {}
        failed = summary.get("failed_cases") or []
        memory_parts: list[str] = []
        if failed:
            memory_parts.append("失败用例：" + ", ".join(str(x) for x in failed[:5]))
        if summary.get("report_error"):
            memory_parts.append(f"报告异常：{summary['report_error']}")
        if summary.get("timed_out"):
            memory_parts.append("测试超时（可能是死循环或等待输入）")
        if not memory_parts:
            memory_parts.append("测试未通过，但无结构化失败信息（检查测试是否能启动）")
        for item in state.get("memory", []):
            if item.startswith("补丁应用失败"):
                memory_parts.append(item)
        note = f"reflect: 第 {state.get('round', 0)} 轮失败记忆 {len(memory_parts)} 条"
        log.info("repair.reflect", items=len(memory_parts))
        return {"memory": memory_parts, "notes": [note]}

    # ---------------- 节点 8：finalize
    def finalize(state: RepairState) -> dict[str, Any]:
        patch = state.get("patch", "")
        out_dir = options.output_dir or (options.issue_file.parent / "patchpilot-out")
        target = Path(out_dir) / f"patchpilot-{state.get('task_id', 'task')}.diff"
        try:
            write_text_lf(target, patch)
            path = str(target)
        except OSError:
            path = ""
        note = (
            f"finalize: {'通过' if state.get('passed') else '未通过'}，"
            f"补丁写入 {path or '<失败>'}，共 {state.get('round', 0)} 轮"
        )
        trace_path = ""
        if tracer is not None:
            trace_path = str(
                tracer.finish(
                    status="ok" if state.get("passed") else "failed",
                    task_id=state.get("task_id", ""),
                    rounds=int(state.get("round", 0) or 0),
                    passed=bool(state.get("passed")),
                    failure_kind=str(state.get("failure_kind", "")),
                    budget_exceeded=str(state.get("budget_exceeded", "") or ""),
                    candidates=len(state.get("candidates", []) or []),
                    patch_chars=len(patch),
                    graph_prompt_tokens=int(state.get("prompt_tokens", 0) or 0),
                    graph_completion_tokens=int(state.get("completion_tokens", 0) or 0),
                    unpriced_models=list(state.get("unpriced_models", []) or []),
                )
            )
            note += f"，trace {trace_path}"
        log.info("repair.finalize", passed=bool(state.get("passed")), path=path)
        return {"final_patch_path": path, "trace_path": trace_path, "notes": [note]}

    # ---------------- 路由
    def after_load(state: RepairState) -> str:
        if state.get("budget_exceeded") or not state.get("issue"):
            return "stop"
        return "locate"

    def after_locate(state: RepairState) -> str:
        if state.get("budget_exceeded"):
            return "stop"
        if not state.get("candidates"):
            return "stop"
        return "generate"

    def after_run(state: RepairState) -> str:
        if state.get("budget_exceeded"):
            return "stop"
        if state.get("passed"):
            return "finalize"
        # 轮数耗尽 → 直接收尾（保留最后一份补丁与记忆）
        if int(state.get("round", 0)) >= options.max_rounds:
            return "finalize"
        return "reflect"

    def after_apply(state: RepairState) -> str:
        if state.get("budget_exceeded"):
            return "stop"
        return "run_tests" if state.get("apply_ok") else "reflect"

    graph: StateGraph = StateGraph(RepairState)
    graph.add_node("load_issue", load_issue)
    graph.add_node("locate", locate)
    graph.add_node("generate", generate)
    graph.add_node("apply", apply)
    graph.add_node("run_tests", run_tests)
    graph.add_node("evaluate", evaluate)
    graph.add_node("reflect", reflect)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "load_issue")
    graph.add_conditional_edges("load_issue", after_load, {"stop": END, "locate": "locate"})
    graph.add_conditional_edges("locate", after_locate, {"stop": END, "generate": "generate"})
    def after_generate(state: RepairState) -> str:
        if state.get("budget_exceeded"):
            return "stop"
        if state.get("patch"):
            return "apply"
        # 空补丁：还有轮次就带着记忆重试，否则收尾（避免"生成不出东西"时死循环）
        if int(state.get("round", 0)) >= options.max_rounds:
            return "finalize"
        return "generate"

    graph.add_conditional_edges(
        "generate",
        after_generate,
        {"apply": "apply", "generate": "generate", "finalize": "finalize", "stop": END},
    )
    graph.add_conditional_edges(
        "apply", after_apply, {"run_tests": "run_tests", "reflect": "reflect", "stop": END}
    )
    graph.add_edge("run_tests", "evaluate")
    graph.add_conditional_edges(
        "evaluate", after_run, {"finalize": "finalize", "reflect": "reflect", "stop": END}
    )
    graph.add_edge("reflect", "generate")
    graph.add_edge("finalize", END)
    return graph


def _complete(llm: LLMClient, prompt: str) -> LLMResponse:
    """同步调用异步模型客户端，走**常驻事件循环**。

    为什么不能用 `asyncio.run`（P3 真机实测）：`AsyncOpenAI` 的连接池绑定创建它的事件
    循环，而修复闭环要**连续调用模型多轮**——每次都建新循环并关掉，第二轮就会拿到
    已关闭循环里的连接，报 `LLMError: 模型调用失败: Event loop is closed`。
    """
    from patchpilot.core.llm import complete_sync

    return complete_sync(llm, [ChatMessage(role="user", content=prompt)])


def _default_applier(*, workspace: Path, patch: str) -> str:
    """默认补丁应用：先 `--check` 再真应用；路径安全由 `git_server` 把关。

    形参名必须与 `PatchApplier` 协议一致（`workspace`）：真机实测时
    `_default_applier(*, root=...)` 会在第一次调用就抛
    `TypeError: got an unexpected keyword argument 'workspace'`——
    而当时的错误处理把 workspace 重置成 "."，把这条信息掩盖成了"补丁应用失败"。
    """
    git_server.apply_patch(workspace, patch, check_only=True)
    return git_server.apply_patch(workspace, patch)


def _default_runner(options: RepairOptions) -> TestRunner:
    """默认测试执行器：在 Docker 沙箱副本里跑 pytest。"""

    def run(*, source: Path, pytest_args: Sequence[str]) -> SandboxResult:
        return run_pytest_in_sandbox(
            policy=options.policy,
            source=source,
            pytest_args=list(pytest_args),
            copy_root=options.copy_root,
        )

    return run


def run_repair(
    *,
    options: RepairOptions,
    llm: LLMClient,
    runner: TestRunner | None = None,
    applier: PatchApplier | None = None,
) -> RepairState:
    """跑一次修复，返回最终状态。"""
    graph = build_repair_graph(options=options, llm=llm, runner=runner, applier=applier)
    payload: RepairState = {"task_id": f"fix-{uuid.uuid4().hex[:8]}", "memory": [], "notes": []}
    return graph.compile().invoke(payload)  # type: ignore[return-value]


__all__ = [
    "RepairOptions",
    "RepairState",
    "TestRunner",
    "build_prompt",
    "build_repair_graph",
    "extract_patch",
    "extract_terms",
    "run_repair",
]
