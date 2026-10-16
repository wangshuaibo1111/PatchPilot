"""P0 最小 LangGraph 图。

目的：在没有模型、没有网络、没有 Docker 的前提下，验证四件事
  1. 状态机可编译、可 invoke；
  2. 条件边能构成「验证失败 → 回环重试」的闭环；
  3. notes 通道用 reducer 累加（且有上界），验证 reducer 语义；
  4. checkpoint 能落盘并读出历史（断点续跑的基础）。

审查修正记录：
  首轮 BLOCKING-1/2：预算熔断真正接入编排层；删除与 Budget 重复的 max_attempts 通道；
        「何时判定通过」抽成显式 PassPolicy。
  二轮 BLOCKING-2：BLOCKING-10 此前只修到「显式化」，**没修到熔断点**。本轮修：
        - 构造期校验「有效上限非空」——价格未知且金额是唯一上限时直接拒绝运行；
        - `_charge_simulated_call` 去掉 `cost or 0.0`，显式分支处理价格未知；
        - 递归上限绑定到预算，并把 `GraphRecursionError` 归一化为 BUDGET 且照常落 trace；
        - notes 通道加上界，避免长回环把状态撑爆；
        - `plan` 触顶后不再进入 `critique`（"触顶即停"要真的即停）；
        - `analyze` 仅在状态无用量时才初始化，避免续跑时清空已消耗预算。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from patchpilot.core.budget import Budget, BudgetExceeded, BudgetTracker
from patchpilot.core.config import Settings, get_settings
from patchpilot.core.llm import ChatMessage, LLMResponse, build_llm
from patchpilot.core.logging import get_logger
from patchpilot.core.models import FailureKind
from patchpilot.core.pricing import estimate_cost_usd
from patchpilot.graph.checkpointer import open_checkpointer
from patchpilot.observability.tracing import Tracer

DEFAULT_ISSUE = (
    "调用 parse_config() 时，如果配置文件里出现重复的 key，"
    "会静默返回最后一个值而不是抛出异常，导致线上配置错误难以发现。"
)

# notes 通道的上界：无界累加会在长回环里把状态撑爆
NOTES_LIMIT = 200


def _merge_notes(left: list[str] | None, right: list[str] | None) -> list[str]:
    """notes 通道 reducer：累加但保留最近 NOTES_LIMIT 条。"""
    merged = list(left or []) + list(right or [])
    return merged[-NOTES_LIMIT:]


@dataclass(frozen=True, slots=True)
class PassPolicy:
    """决定「何时判定补丁通过」。

    P0 用确定性策略以便离线自测；P3 会替换为沙箱真实测试结果驱动的判定。
    必须 >= 1：否则图可能永不通过，终止将完全依赖预算，
    属于难以推理的配置，直接在构造时拒绝。
    """

    pass_after_attempt: int = 2

    def __post_init__(self) -> None:
        if self.pass_after_attempt < 1:
            raise ValueError("pass_after_attempt 必须 >= 1（P0 不支持永不通过）")


@dataclass(frozen=True, slots=True)
class GraphOptions:
    policy: PassPolicy = field(default_factory=PassPolicy)
    budget: Budget = field(default_factory=Budget)
    model: str = "stub"
    price_overrides: Mapping[str, tuple[float, float]] | None = None
    prompt_tokens_per_attempt: int = 120
    completion_tokens_per_attempt: int = 40

    def __post_init__(self) -> None:
        """校验「有效上限非空」。

        `Budget.__post_init__` 只能保证*至少有一项*为正；但若那唯一一项是金额上限，
        而该模型**每轮成本不是严格正数**，则金额上限不可执行——
        表现为「熔断配置合法但永不触发」，正是二轮审查 BLOCKING-2 实测到的形态。

        判据刻意用「每轮成本是否严格为正」而不是「模型是否在价格表里」：
        后者会漏掉"已定价但单价为 0"的模型（三轮审查 NON_BLOCKING-1 实测）。
        """
        if self.budget.max_usd > 0 and not self.budget.non_usd_limits:
            # 判据必须与 `_charge_simulated_call` 用**同一个计费向量**。
            # 用代理向量（prompt=completion=per_node）会漏掉"单侧零价 + 该侧 token 为 0"
            # 这一支：四轮审查阻塞 3 实测——构造通过、真实每轮成本 0.0，
            # 金额上限一次未触发，最终由框架递归上限收场。
            round_cost = estimate_cost_usd(
                self.model,
                self.prompt_tokens_per_attempt,
                self.completion_tokens_per_attempt,
                self.price_overrides,
            )
            if round_cost is None or round_cost <= 0:
                raise ValueError(
                    f"模型 {self.model} 每轮成本为 {round_cost}，而唯一可用的上限是金额上限"
                    "——熔断不可执行。请用 PP_PRICE_OVERRIDES 配置价格，"
                    "或改用 token/时长/轮数上限。"
                )


class RepairState(TypedDict, total=False):
    task_id: str
    issue: str
    started_at: float
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    unpriced_models: list[str]
    budget_exceeded: str
    approved: bool
    plan: list[str]
    critique: str
    notes: Annotated[list[str], _merge_notes]


def _charge_simulated_call(tracker: BudgetTracker, options: GraphOptions) -> None:
    """P0 用固定用量代表一次模型调用；P3 换成真实 token 与成本。

    价格未知时**绝不写成 `cost or 0.0`**：那会把未知成本折成 0，
    使金额上限永不触发（二轮审查 BLOCKING-2 的判决性证据）。
    """
    prompt_tokens = options.prompt_tokens_per_attempt
    completion_tokens = options.completion_tokens_per_attempt
    cost = estimate_cost_usd(
        options.model, prompt_tokens, completion_tokens, options.price_overrides
    )
    if cost is None:
        tracker.mark_unpriced(options.model)
        tracker.charge(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        return
    tracker.charge(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
    )


def recursion_limit_for(options: GraphOptions) -> int:
    """把 LangGraph 递归上限绑定到预算，使预算是先触发的终止条件。

    否则会出现"预算没到、框架先炸"：未捕获的 `GraphRecursionError`
    既没有 BUDGET 归因、也来不及落 trace（二轮审查 BLOCKING-2 实测）。
    """
    budget = options.budget
    per_node = options.prompt_tokens_per_attempt + options.completion_tokens_per_attempt
    per_round = max(1, per_node * 2)

    caps: list[int] = []
    if budget.max_attempts > 0:
        caps.append(budget.max_attempts)
    if budget.max_tokens > 0:
        caps.append(budget.max_tokens // per_round + 2)
    if budget.max_usd > 0:
        # 与构造期校验、以及 _charge_simulated_call 的扣费保持**同一个计费向量**
        # （五轮审查非阻塞 1：此前仍用代理向量，非对称价下会高估可用轮数）。
        per_call_cost = estimate_cost_usd(
            options.model,
            options.prompt_tokens_per_attempt,
            options.completion_tokens_per_attempt,
            options.price_overrides,
        )
        if per_call_cost and per_call_cost > 0:
            caps.append(int(budget.max_usd / (per_call_cost * 2)) + 2)

    attempts = min(caps) if caps else 100
    # 必须同时封顶在框架默认递归上限之下：否则大额预算（如 1e8 token）会算出 6e5，
    # 反而让"框架先炸"重新出现（三轮审查 NON_BLOCKING-2）。
    # 封顶后若预算仍需更多轮，框架上限成为实际终止者——而这种情形已被
    # run_minimal_demo 的 GraphRecursionError 归一化覆盖，仍会归因为 BUDGET 并落 trace。
    return max(20, min(attempts * 2 + 10, 10_000))


def build_minimal_graph(options: GraphOptions | None = None) -> StateGraph:
    """构建未编译的图。传入 checkpointer 后 compile 即可持久化。"""
    options = options or GraphOptions()
    log = get_logger("graph")

    def _guard(
        state: RepairState, body: Callable[[BudgetTracker], dict[str, Any]]
    ) -> dict[str, Any]:
        """把预算计量与熔断集中在一处，保证每个节点受同一套约束。"""
        tracker = BudgetTracker.from_state(options.budget, state)
        try:
            payload = body(tracker)
        except BudgetExceeded as exc:
            update = tracker.to_state()
            update["budget_exceeded"] = exc.reason
            update["notes"] = [f"budget: 熔断终止（{exc.reason}）"]
            log.warning(
                "node.budget_exceeded", task_id=state.get("task_id"), reason=exc.reason
            )
            return update
        update = tracker.to_state()
        update.update(payload)
        return update

    def analyze(state: RepairState) -> dict[str, Any]:
        issue = state.get("issue", "")
        log.info("node.analyze", task_id=state.get("task_id"), issue_chars=len(issue))
        update: dict[str, Any] = {
            "notes": [f"analyze: 解析 issue（{len(issue)} 字符 / {len(issue.split())} 词）"]
        }
        # 只在状态里还没有这些通道时初始化。
        # 无条件清零会在断点续跑时把已消耗的预算抹掉（二轮审查 NON_BLOCKING-7）。
        if "started_at" not in state:
            update["started_at"] = time.monotonic()
        if "attempts" not in state:
            update["attempts"] = 0
        if "prompt_tokens" not in state:
            update["prompt_tokens"] = 0
        if "completion_tokens" not in state:
            update["completion_tokens"] = 0
        if "cost_usd" not in state:
            update["cost_usd"] = 0.0
        if "approved" not in state:
            update["approved"] = False
        if "budget_exceeded" not in state:
            update["budget_exceeded"] = ""
        return update

    def plan(state: RepairState) -> dict[str, Any]:
        def body(tracker: BudgetTracker) -> dict[str, Any]:
            attempt = tracker.start_attempt()
            _charge_simulated_call(tracker, options)
            steps = [
                f"[第 {attempt} 轮] 定位候选文件与符号（检索层，P2 实现）",
                f"[第 {attempt} 轮] 生成补丁（编辑层，P3 实现）",
                f"[第 {attempt} 轮] 沙箱内运行目标测试（P1 实现）",
            ]
            log.info("node.plan", task_id=state.get("task_id"), attempt=attempt)
            return {"plan": steps, "notes": [f"plan: 第 {attempt} 轮产出 {len(steps)} 个步骤"]}

        return _guard(state, body)

    def critique(state: RepairState) -> dict[str, Any]:
        def body(tracker: BudgetTracker) -> dict[str, Any]:
            attempt = int(state.get("attempts", 0))
            _charge_simulated_call(tracker, options)
            approved = attempt >= options.policy.pass_after_attempt
            text = "补丁通过自检" if approved else "补丁未通过自检，需携带失败记忆重试"
            log.info(
                "node.critique",
                task_id=state.get("task_id"),
                attempt=attempt,
                approved=approved,
            )
            return {
                "critique": text,
                "approved": approved,
                "notes": [f"critique: {text}（第 {attempt} 轮）"],
            }

        return _guard(state, body)

    def after_plan(state: RepairState) -> str:
        """触顶即停：plan 阶段熔断后不再进入 critique（否则会再计费一次）。"""
        return "stop" if state.get("budget_exceeded") else "critique"

    def route(state: RepairState) -> str:
        if state.get("budget_exceeded") or state.get("approved"):
            return "done"
        return "retry"

    graph: StateGraph = StateGraph(RepairState)
    graph.add_node("analyze", analyze)
    graph.add_node("plan", plan)
    graph.add_node("critique", critique)
    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "plan")
    graph.add_conditional_edges("plan", after_plan, {"stop": END, "critique": "critique"})
    graph.add_conditional_edges("critique", route, {"retry": "plan", "done": END})
    return graph


def _run_coro(coro: Any) -> Any:
    """在已有事件循环中也能安全执行协程。

    P6 的 FastAPI / 异步 worker 里直接 ``asyncio.run`` 会 RuntimeError，
    这里在检测到运行中的循环时改用独立线程执行。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _probe_llm(llm: Any, prompt: str) -> LLMResponse:
    """走一次模型调用以验证「用量 → 成本」链路，并确保客户端被关闭。"""
    try:
        return await llm.complete([ChatMessage(role="user", content=prompt)])
    finally:
        aclose = getattr(llm, "aclose", None)
        if callable(aclose):
            await aclose()


def run_minimal_demo(
    *,
    settings: Settings | None = None,
    issue: str | None = None,
    options: GraphOptions | None = None,
) -> tuple[RepairState, Path]:
    """跑通最小图并落一份 trace，返回 (最终状态, trace 文件路径)。

    不依赖网络、Docker 或 API Key：默认模型走 stub。
    """
    settings = settings or get_settings()
    options = options or GraphOptions(
        budget=settings.budget(),
        model=settings.llm_model if settings.llm_provider != "stub" else "stub",
        price_overrides=settings.price_overrides or None,
    )
    tracer = Tracer(
        settings.trace_dir, name="smoke", price_overrides=options.price_overrides
    )
    task_id = f"smoke-{uuid.uuid4().hex[:8]}"

    payload: RepairState = {
        "task_id": task_id,
        "issue": issue or DEFAULT_ISSUE,
        "notes": [],
        "started_at": time.monotonic(),
    }

    with open_checkpointer(settings) as checkpointer:
        graph = build_minimal_graph(options).compile(checkpointer=checkpointer)
        run_config: dict[str, Any] = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": recursion_limit_for(options),
        }

        with tracer.span(
            "graph.invoke",
            backend=settings.checkpoint_backend,
            task_id=task_id,
        ) as span:
            try:
                final: RepairState = graph.invoke(payload, config=run_config)  # type: ignore[assignment]
            except GraphRecursionError as exc:
                # 兜底：框架级失控归一化为预算终止，且**仍要落 trace**。
                # 否则一次失控既不归因也不会留下任何记录（二轮审查 BLOCKING-2）。
                span.attributes["error"] = "GraphRecursionError"
                latest = graph.get_state(run_config).values
                final = dict(latest) if latest else dict(payload)  # type: ignore[assignment]
                final["budget_exceeded"] = f"递归上限触发（{exc}）"
                final["notes"] = [f"budget: 递归上限触发 → 归一化为 BUDGET（{exc}）"]

        with tracer.span("checkpoint.history", task_id=task_id):
            history = list(graph.get_state_history(run_config))
        steps = len(history)

    response = _run_coro(_probe_llm(build_llm(settings), final.get("issue", "")))
    tracer.record_usage(
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )

    if final.get("budget_exceeded"):
        failure = FailureKind.BUDGET
    elif final.get("approved"):
        failure = FailureKind.NONE
    else:
        failure = FailureKind.TEST_FAIL

    prompt_tokens = int(final.get("prompt_tokens", 0) or 0)
    completion_tokens = int(final.get("completion_tokens", 0) or 0)

    trace_path = tracer.finish(
        status="ok",
        steps=steps,
        attempts=int(final.get("attempts", 0) or 0),
        approved=bool(final.get("approved")),
        budget_exceeded=str(final.get("budget_exceeded", "") or ""),
        failure_kind=failure.value,
        graph_prompt_tokens=prompt_tokens,
        graph_completion_tokens=completion_tokens,
        graph_total_tokens=prompt_tokens + completion_tokens,
        graph_cost_usd=round(float(final.get("cost_usd", 0.0) or 0.0), 6),
        unpriced_models=list(final.get("unpriced_models", []) or []),
    )
    return final, trace_path
