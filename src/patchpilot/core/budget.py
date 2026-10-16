"""预算熔断：token / 金额 / 时长 / 轮数 四重硬上限。

设计原则（对应 PLAN.md 第 2 节的「关键工程决策」）：
生产环境的 Agent 必须能在任何一项资源触顶时立刻停下，
否则一次跑飞的任务就可能烧掉整天的预算。

审查修正记录：
  首轮：``start_attempt`` 改为「先判定后自增」；新增 ``from_state`` / ``to_state``
        让用量随 checkpoint 持久化；明确 ``max_* <= 0`` 表示不限制。
  二轮 BLOCKING-2：新增 ``mark_unpriced`` —— 未定价模型的成本**绝不能折成 0**，
        否则金额上限会静默失效（"唯一为正的上限不可执行 → 熔断失效"）。
        并由 ``non_usd_limits`` 支撑「有效上限非空」的构造期校验。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class BudgetExceeded(RuntimeError):
    """任一预算项触顶时抛出，调用方应终止任务并归类为 BUDGET 失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"预算耗尽: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Budget:
    """四重上限。取 0 或负数表示该项不限制。"""

    max_tokens: int = 200_000
    max_usd: float = 2.0
    max_seconds: float = 900.0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        # 至少要有一种终止条件，否则「不限制」会变成无限循环
        if not self.non_usd_limits and self.max_usd <= 0:
            raise ValueError("四重上限不能同时为 0/负数，否则任务无法终止")

    @property
    def non_usd_limits(self) -> tuple[str, ...]:
        """除金额之外的可用上限名称。

        调用方用它判断「金额上限是否是可执行的唯一上限」——
        若模型价格未知且这是唯一上限，熔断实际上不存在。
        """
        names: list[str] = []
        if self.max_tokens > 0:
            names.append("max_tokens")
        if self.max_seconds > 0:
            names.append("max_seconds")
        if self.max_attempts > 0:
            names.append("max_attempts")
        return tuple(names)


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BudgetTracker:
    """累计用量并在每次变更后校验预算。

    跨进程恢复的已知限制：``started_at`` 使用 ``time.monotonic``，
    因此恢复后时长预算会重新计时（P3 改为 wall clock 并以状态字段持久化）。
    """

    def __init__(
        self,
        budget: Budget,
        *,
        clock: Callable[[], float] = time.monotonic,
        started_at: float | None = None,
        attempts: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
        unpriced_models: Sequence[str] | None = None,
    ) -> None:
        self.budget = budget
        self._clock = clock
        self._started_at = clock() if started_at is None else started_at
        self.usage = Usage(
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            cost_usd=max(0.0, cost_usd),
        )
        self.attempts = max(0, attempts)
        # 记录无法计价的模型：这些调用只计 token、不计金额，
        # 必须显式留痕，否则"成本恒为 0"会被误读成"没花钱"
        self.unpriced_models: list[str] = list(unpriced_models or [])

    # ---------- 状态持久化 ----------

    @classmethod
    def from_state(
        cls,
        budget: Budget,
        state: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> BudgetTracker:
        return cls(
            budget,
            clock=clock,
            started_at=state.get("started_at"),
            attempts=int(state.get("attempts", 0) or 0),
            prompt_tokens=int(state.get("prompt_tokens", 0) or 0),
            completion_tokens=int(state.get("completion_tokens", 0) or 0),
            cost_usd=float(state.get("cost_usd", 0.0) or 0.0),
            unpriced_models=list(state.get("unpriced_models", []) or []),
        )

    def to_state(self) -> dict[str, Any]:
        """可写回 LangGraph 状态的字段。"""
        return {
            "started_at": self._started_at,
            "attempts": self.attempts,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "cost_usd": self.usage.cost_usd,
            "unpriced_models": list(self.unpriced_models),
        }

    # ---------- 计量与熔断 ----------

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started_at

    def mark_unpriced(self, model: str) -> None:
        """记录一个无法计价的模型。

        **绝不把未知成本当作 0**：那会让金额上限永不触发，
        而它可能是唯一为正的上限（二轮审查 BLOCKING-2）。
        """
        if model and model not in self.unpriced_models:
            self.unpriced_models.append(model)

    def start_attempt(self) -> int:
        """开始新一轮尝试。先判定后自增，越界时状态不被污染。"""
        if self.budget.max_attempts > 0 and self.attempts >= self.budget.max_attempts:
            raise BudgetExceeded(f"轮数超限 ({self.attempts} >= {self.budget.max_attempts})")
        self.attempts += 1
        return self.attempts

    def charge(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """记录一次消耗并立即校验预算。"""
        self.usage.prompt_tokens += max(0, prompt_tokens)
        self.usage.completion_tokens += max(0, completion_tokens)
        self.usage.cost_usd += max(0.0, cost_usd)
        self.check()

    def check(self) -> None:
        if self.budget.max_tokens > 0 and self.usage.total_tokens > self.budget.max_tokens:
            raise BudgetExceeded(
                f"token 超限 ({self.usage.total_tokens} > {self.budget.max_tokens})"
            )
        if self.budget.max_usd > 0 and self.usage.cost_usd > self.budget.max_usd:
            raise BudgetExceeded(f"金额超限 (${self.usage.cost_usd:.4f} > ${self.budget.max_usd})")
        if self.budget.max_seconds > 0 and self.elapsed_s > self.budget.max_seconds:
            raise BudgetExceeded(f"时长超限 ({self.elapsed_s:.1f}s > {self.budget.max_seconds}s)")
        if self.budget.max_attempts > 0 and self.attempts > self.budget.max_attempts:
            raise BudgetExceeded(f"轮数超限 ({self.attempts} > {self.budget.max_attempts})")

    @property
    def exhausted(self) -> bool:
        try:
            self.check()
        except BudgetExceeded:
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "cost_usd": round(self.usage.cost_usd, 6),
            "elapsed_s": round(self.elapsed_s, 3),
            "unpriced_models": list(self.unpriced_models),
        }
