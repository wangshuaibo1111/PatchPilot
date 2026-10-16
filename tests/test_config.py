from __future__ import annotations

import pytest

from patchpilot.core.config import Settings

# 备注：该串长度刻意短于密钥模式阈值，仅用于验证 repr 不泄露
FAKE_KEY_FOR_REPR = "sk-super-secret"


def test_defaults_follow_plan() -> None:
    settings = Settings(_env_file=None)
    assert settings.checkpoint_backend == "memory"
    assert settings.llm_provider == "stub"
    assert settings.budget_max_attempts == 3
    assert settings.llm_model == "deepseek-flash"


def test_code_defaults_match_env_example_template() -> None:
    """代码默认值必须与 `.env.example` 模板一致（防"两份互相矛盾的模板"）。

    第二十一轮非阻塞项：`llm_base_url` 统一了裸域名，但 `llm_model` 仍留在 `deepseek-chat`，
    于是"模板说 deepseek-flash、代码默认说 deepseek-chat"，且此前无用例约束这一点。
    这里逐项比对两者的模型相关配置。
    """
    from pathlib import Path

    settings = Settings(_env_file=None)
    template = Path(__file__).resolve().parents[1] / ".env.example"
    values = {}
    for line in template.read_text(encoding="utf-8").splitlines():
        if line.startswith("PP_LLM_") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    assert values["PP_LLM_BASE_URL"] == settings.llm_base_url
    assert values["PP_LLM_MODEL"] == settings.llm_model
    assert values["PP_LLM_SMALL_MODEL"] == settings.llm_small_model


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PP_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("PP_BUDGET_MAX_USD", "0.5")
    monkeypatch.setenv("PP_LLM_PROVIDER", "deepseek")

    settings = Settings(_env_file=None)
    assert settings.checkpoint_backend == "sqlite"
    assert settings.budget_max_usd == pytest.approx(0.5)
    assert settings.llm_provider == "deepseek"


def test_price_overrides_parsed_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PP_PRICE_OVERRIDES", '{"my-model": [1.0, 2.0]}')

    settings = Settings(_env_file=None)

    assert settings.price_overrides["my-model"] == (1.0, 2.0)


def test_budget_view_matches_flat_fields() -> None:
    settings = Settings(_env_file=None, budget_max_tokens=1234, budget_max_attempts=7)
    budget = settings.budget()
    assert budget.max_tokens == 1234
    assert budget.max_attempts == 7


def test_safe_summary_never_leaks_api_key() -> None:
    settings = Settings(_env_file=None, llm_api_key=FAKE_KEY_FOR_REPR)
    summary = settings.safe_summary()
    assert FAKE_KEY_FOR_REPR not in str(summary)
    assert summary["llm_api_key"] == "已配置"


def test_safe_summary_reports_missing_key() -> None:
    settings = Settings(_env_file=None, llm_api_key="")
    assert settings.safe_summary()["llm_api_key"] == "未配置"


def test_repr_and_dump_do_not_leak_api_key() -> None:
    """审查 NON_BLOCKING-1：日志里打 settings 也不能泄露密钥。"""
    settings = Settings(_env_file=None, llm_api_key=FAKE_KEY_FOR_REPR)

    assert FAKE_KEY_FOR_REPR not in repr(settings)
    assert FAKE_KEY_FOR_REPR not in str(settings)
    assert FAKE_KEY_FOR_REPR not in str(settings.model_dump())
