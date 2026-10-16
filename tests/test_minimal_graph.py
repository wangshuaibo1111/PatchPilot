from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchpilot.core.budget import Budget
from patchpilot.core.config import Settings
from patchpilot.core.models import FailureKind
from patchpilot.graph.minimal import (
    GraphOptions,
    PassPolicy,
    build_minimal_graph,
    recursion_limit_for,
    run_minimal_demo,
)


def _run(options: GraphOptions, task_id: str = "t-1") -> dict:
    graph = build_minimal_graph(options).compile()
    return graph.invoke(
        {"task_id": task_id, "issue": "示例问题", "notes": []},
        config={"configurable": {"thread_id": task_id}},
    )


def test_loop_retries_until_policy_passes() -> None:
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=2),
        budget=Budget(max_tokens=1_000_000, max_usd=0, max_seconds=0, max_attempts=0),
    )

    final = _run(options)

    assert final["attempts"] == 2
    assert final["approved"] is True
    assert final["budget_exceeded"] == ""
    # 行为断言：走的是 analyze → (plan → critique) × 2
    assert [n.split(":")[0] for n in final["notes"]] == [
        "analyze",
        "plan",
        "critique",
        "plan",
        "critique",
    ]


def test_attempt_budget_actually_stops_the_loop() -> None:
    """审查 BLOCKING-2：轮数上限必须真的生效，而不是被硬编码策略架空。"""
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=0, max_usd=0, max_seconds=0, max_attempts=2),
    )

    final = _run(options)

    assert final["approved"] is False
    assert final["attempts"] == 2  # 恰好两轮，不多不少
    assert final["budget_exceeded"].startswith("轮数超限")


def test_token_budget_stops_the_loop_when_attempts_unlimited() -> None:
    """审查 BLOCKING-1：预算必须真的接入编排层，而不是只在单测里存在。"""
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=250, max_usd=0, max_seconds=0, max_attempts=0),
    )

    final = _run(options)

    assert final["approved"] is False
    assert final["budget_exceeded"].startswith("token 超限")
    used = int(final["prompt_tokens"]) + int(final["completion_tokens"])
    assert used > 250  # 确实是因为超了才停


def test_cost_budget_stops_the_loop() -> None:
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=0, max_usd=0.00005, max_seconds=0, max_attempts=0),
        model="deepseek-chat",
    )

    final = _run(options)

    assert final["budget_exceeded"].startswith("金额超限")
    assert float(final["cost_usd"]) > 0.00005


def test_pass_policy_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="必须 >= 1"):
        PassPolicy(pass_after_attempt=0)


UNPRICED = "definitely-not-in-price-table"


def test_unpriced_model_with_only_usd_limit_is_rejected() -> None:
    """二轮审查 BLOCKING-2：不可执行的预算必须在构造期拒绝，而不是跑飞。

    此前 `Budget(max_tokens=0, max_usd=2.0, ...)` 能通过「至少一项为正」的校验，
    却因模型价格未知而全程 cost=0.0 —— 唯一为正的上限永不触发，
    无界组最终以未捕获的 GraphRecursionError 收场。
    """
    with pytest.raises(ValueError, match="熔断不可执行"):
        GraphOptions(
            policy=PassPolicy(pass_after_attempt=99),
            budget=Budget(max_tokens=0, max_usd=2.0, max_seconds=0, max_attempts=0),
            model=UNPRICED,
        )


def test_unpriced_model_with_token_limit_terminates_and_is_recorded() -> None:
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=250, max_usd=2.0, max_seconds=0, max_attempts=0),
        model=UNPRICED,
    )

    final = _run(options)

    assert final["budget_exceeded"].startswith("token 超限")
    assert final["unpriced_models"] == [UNPRICED]
    # 成本仍为 0，但必须是被显式标记的「未知」，而不是被当成「免费」
    assert float(final["cost_usd"]) == 0.0
    assert final["approved"] is False


def test_zero_cost_model_with_only_usd_limit_is_rejected() -> None:
    """三轮审查 NON_BLOCKING-1：只堵「价格未知」会漏掉「已定价但单价为 0」。"""
    with pytest.raises(ValueError, match="熔断不可执行"):
        GraphOptions(
            policy=PassPolicy(pass_after_attempt=99),
            budget=Budget(max_tokens=0, max_usd=2.0, max_seconds=0, max_attempts=0),
            model="zero-cost-model",
            price_overrides={"zero-cost-model": (0.0, 0.0)},
        )


def test_recursion_limit_is_capped_below_framework_default() -> None:
    """三轮审查 NON_BLOCKING-2：大额预算不得算出超过框架默认的递归上限。"""
    huge = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=100_000_000, max_usd=0, max_seconds=0, max_attempts=0),
    )

    assert recursion_limit_for(huge) == 10_000


def test_recursion_limit_is_bounded_by_budget() -> None:
    """框架默认递归上限（实测 10007）远大于预算，必须由预算反推一个有限值。"""
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=0, max_usd=0, max_seconds=0, max_attempts=5),
    )

    limit = recursion_limit_for(options)

    assert 20 <= limit < 10007
    assert limit == 5 * 2 + 10


def test_plan_stop_does_not_charge_critique() -> None:
    """触顶即停：plan 阶段熔断后不应再执行 critique（否则多计一次费）。"""
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=99),
        budget=Budget(max_tokens=100, max_usd=0, max_seconds=0, max_attempts=0),
    )

    final = _run(options)

    assert final["budget_exceeded"].startswith("token 超限")
    assert final["attempts"] == 1
    assert int(final["prompt_tokens"]) == 120  # 只有 plan 那一次调用
    assert "critique" not in [n.split(":")[0] for n in final["notes"]]


def test_usage_persists_across_checkpoint_state() -> None:
    """使用量必须随状态持久化，否则断点续跑会把预算清零。"""
    options = GraphOptions(
        policy=PassPolicy(pass_after_attempt=3),
        budget=Budget(max_tokens=1_000_000, max_usd=0, max_seconds=0, max_attempts=0),
    )

    final = _run(options)

    assert final["attempts"] == 3
    assert int(final["prompt_tokens"]) == 3 * 120 + 3 * 120
    assert int(final["completion_tokens"]) == 3 * 40 + 3 * 40


def test_demo_writes_replayable_trace(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        checkpoint_backend="memory",
        trace_dir=tmp_path / "traces",
    )

    final, trace_path = run_minimal_demo(settings=settings, issue="演示问题")

    assert final["approved"] is True
    record = json.loads(trace_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["status"] == "ok"
    assert record["failure_kind"] == FailureKind.NONE.value
    assert record["attempts"] == 2
    assert record["graph_total_tokens"] > 0
    assert record["cost_unknown"] is False
    assert {span["name"] for span in record["spans"]} == {
        "graph.invoke",
        "checkpoint.history",
    }


def test_demo_marks_budget_failure_kind(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        checkpoint_backend="memory",
        trace_dir=tmp_path / "traces",
        budget_max_attempts=1,
        budget_max_tokens=0,
        budget_max_usd=0,
        budget_max_seconds=0,
    )

    final, trace_path = run_minimal_demo(settings=settings, issue="演示问题")

    assert final["budget_exceeded"].startswith("轮数超限")
    record = json.loads(trace_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert record["failure_kind"] == FailureKind.BUDGET.value
    assert record["approved"] is False
