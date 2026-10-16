from __future__ import annotations

import pytest

from patchpilot.core.pricing import estimate_cost_usd, is_priced, price_for


def test_known_model_cost() -> None:
    assert estimate_cost_usd("deepseek-chat", 1_000_000, 0) == pytest.approx(0.27)
    assert estimate_cost_usd("deepseek-chat", 0, 1_000_000) == pytest.approx(1.10)


def test_stub_is_explicitly_free() -> None:
    assert estimate_cost_usd("stub", 10_000, 10_000) == 0.0
    assert is_priced("stub") is True


def test_unknown_model_is_none_not_free() -> None:
    """审查 BLOCKING-10：未收录模型绝不能静默按 0 计费。"""
    assert price_for("totally-unknown-model") is None
    assert estimate_cost_usd("totally-unknown-model", 1_000_000, 1_000_000) is None
    assert is_priced("totally-unknown-model") is False


def test_family_fallback_covers_newer_releases() -> None:
    # deepseek-v3 属于已收录族，必须取到价格而不是 None
    assert estimate_cost_usd("deepseek-v3", 1_000_000, 0) == pytest.approx(0.27)
    assert estimate_cost_usd("qwen3-max", 1_000_000, 0) == pytest.approx(1.60)


def test_prefix_match_for_dated_versions() -> None:
    assert price_for("deepseek-chat-0324") == price_for("deepseek-chat")
    assert price_for("qwen-max-2025-01-25") == price_for("qwen-max")


def test_overrides_take_precedence_and_enable_new_models() -> None:
    overrides = {"brand-new-model": (1.0, 2.0)}
    assert is_priced("brand-new-model") is False
    assert is_priced("brand-new-model", overrides) is True
    assert estimate_cost_usd(
        "brand-new-model", 1_000_000, 1_000_000, overrides
    ) == pytest.approx(3.0)
