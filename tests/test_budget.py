from __future__ import annotations

import pytest

from patchpilot.core.budget import Budget, BudgetExceeded, BudgetTracker


def test_budget_rejects_config_that_can_never_terminate() -> None:
    with pytest.raises(ValueError, match="不能同时"):
        Budget(max_tokens=0, max_usd=0, max_seconds=0, max_attempts=0)


def test_charge_within_budget() -> None:
    tracker = BudgetTracker(Budget(max_tokens=100, max_usd=1.0, max_seconds=60, max_attempts=3))
    tracker.charge(prompt_tokens=10, completion_tokens=5, cost_usd=0.01)

    assert tracker.usage.total_tokens == 15
    assert tracker.usage.cost_usd == pytest.approx(0.01)
    assert tracker.exhausted is False


def test_token_limit_raises() -> None:
    tracker = BudgetTracker(Budget(max_tokens=10, max_usd=0, max_seconds=0, max_attempts=0))
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.charge(prompt_tokens=11)
    assert excinfo.value.reason.startswith("token")


def test_cost_limit_raises() -> None:
    tracker = BudgetTracker(Budget(max_tokens=0, max_usd=0.5, max_seconds=0, max_attempts=0))
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.charge(cost_usd=0.6)
    assert excinfo.value.reason.startswith("金额")


def test_attempt_limit_has_no_off_by_one() -> None:
    """审查 NON_BLOCKING-3：越界时计数不得被污染。"""
    tracker = BudgetTracker(Budget(max_tokens=0, max_usd=0, max_seconds=0, max_attempts=2))

    assert tracker.start_attempt() == 1
    assert tracker.start_attempt() == 2
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.start_attempt()

    assert tracker.attempts == 2  # 不是 3
    assert excinfo.value.reason.startswith("轮数")


def test_zero_attempts_means_unlimited() -> None:
    tracker = BudgetTracker(Budget(max_tokens=10_000, max_usd=0, max_seconds=0, max_attempts=0))
    for _ in range(50):
        tracker.start_attempt()
    assert tracker.attempts == 50


def test_time_limit_raises_with_fake_clock() -> None:
    now = [0.0]
    tracker = BudgetTracker(
        Budget(max_tokens=0, max_usd=0, max_seconds=10, max_attempts=0),
        clock=lambda: now[0],
    )
    now[0] = 11.0
    with pytest.raises(BudgetExceeded) as excinfo:
        tracker.check()
    assert excinfo.value.reason.startswith("时长")


def test_state_round_trip_preserves_usage() -> None:
    """断点续跑后预算不能被清零，否则熔断等于没做。"""
    budget = Budget(max_tokens=1000, max_usd=1.0, max_seconds=100, max_attempts=5)
    tracker = BudgetTracker(
        budget, attempts=2, prompt_tokens=10, completion_tokens=5, cost_usd=0.01
    )

    restored = BudgetTracker.from_state(budget, tracker.to_state())

    assert restored.attempts == 2
    assert restored.usage.total_tokens == 15
    assert restored.usage.cost_usd == pytest.approx(0.01)
    assert restored.to_state()["prompt_tokens"] == 10


def test_snapshot_is_json_friendly() -> None:
    tracker = BudgetTracker(Budget())
    tracker.charge(prompt_tokens=4, completion_tokens=6, cost_usd=0.0012345)
    snapshot = tracker.snapshot()

    assert snapshot["total_tokens"] == 10
    assert snapshot["cost_usd"] == pytest.approx(0.001234, abs=1e-6)
    assert set(snapshot) == {
        "attempts",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "elapsed_s",
        "unpriced_models",
    }


def test_unpriced_models_are_tracked_and_persisted() -> None:
    """二轮审查 BLOCKING-2：未知成本必须留痕，不能看起来像「没花钱」。"""
    budget = Budget()
    tracker = BudgetTracker(budget)

    tracker.mark_unpriced("mystery-model")
    tracker.mark_unpriced("mystery-model")  # 去重

    assert tracker.unpriced_models == ["mystery-model"]
    assert tracker.snapshot()["unpriced_models"] == ["mystery-model"]

    restored = BudgetTracker.from_state(budget, tracker.to_state())
    assert restored.unpriced_models == ["mystery-model"]


def test_non_usd_limits_exposes_enforceable_alternatives() -> None:
    """Budget 只保证「至少一项为正」；可执行性由 GraphOptions 负责校验。"""
    only_usd = Budget(max_tokens=0, max_usd=2.0, max_seconds=0, max_attempts=0)
    assert only_usd.non_usd_limits == ()

    mixed = Budget(max_tokens=0, max_usd=2.0, max_seconds=0, max_attempts=3)
    assert mixed.non_usd_limits == ("max_attempts",)
