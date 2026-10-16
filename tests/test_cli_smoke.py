"""`pp smoke` 的门禁语义：**不随 `.env` 漂移**。

第二十二轮 BLOCKING：`cli.smoke` 里"默认强制 stub provider"那段逻辑此前**零覆盖**——
删掉整块或删掉 `--real-llm` 选项，全套仍是 325 passed + 3 skipped。
而它恰是"改 `.env` 就能让零依赖门禁变红"这个隐患的唯一实现点。
"""

from __future__ import annotations

from typing import Any

import pytest

from patchpilot.cli import smoke
from patchpilot.core.config import Settings


def _spy_settings(monkeypatch: pytest.MonkeyPatch, provider: str) -> dict[str, Any]:
    """把 `get_settings` 换成返回指定 provider 的配置，并截获传给 demo 的 settings。"""
    from patchpilot import cli as cli_module

    captured: dict[str, Any] = {}
    settings = Settings(_env_file=None).model_copy(update={"llm_provider": provider})
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "configure_logging", lambda *_a, **_k: None)

    def fake_demo(*, settings: Settings, issue: str | None = None):
        captured["provider"] = settings.llm_provider
        captured["issue"] = issue
        return {"prompt_tokens": 1, "completion_tokens": 1, "budget_exceeded": ""}, "trace.jsonl"

    monkeypatch.setattr(cli_module, "run_minimal_demo", fake_demo)
    return captured


def test_smoke_forces_stub_even_when_env_asks_for_a_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**核心断言**：`.env` 配了真实 provider 时，`pp smoke` 仍必须走 stub。

    否则未装 `openai` 的环境里 `pp smoke` 会抛 `RuntimeError`、退出码 1——
    而它是 README/PLAN 宣称的 P0 零依赖门禁（真机实测过该崩溃）。
    """
    captured = _spy_settings(monkeypatch, provider="deepseek")

    smoke(issue="", real_llm=False)

    assert captured["provider"] == "stub", "smoke 默认必须强制 stub，不随 .env 漂移"


def test_smoke_real_llm_keeps_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 `--real-llm` 时必须**保留**配置里的 provider（否则该选项形同虚设）。"""
    captured = _spy_settings(monkeypatch, provider="deepseek")

    smoke(issue="", real_llm=True)

    assert captured["provider"] == "deepseek"


def test_smoke_keeps_stub_when_already_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _spy_settings(monkeypatch, provider="stub")

    smoke(issue="x", real_llm=False)

    assert captured["provider"] == "stub"
    assert captured["issue"] == "x", "issue 选项必须透传"
