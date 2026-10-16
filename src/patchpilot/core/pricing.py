"""模型价格表与成本估算。

设计决策（来自 code-reviewer 首轮审查 BLOCKING-10）：
未收录价格的模型**不会**被静默按 0 计费——那会让金额硬上限永久失效，
并污染评测报告里的成本数字。``price_for`` 返回 ``None`` 表示「价格未知」，
由调用方显式处理：Tracer 标记 ``cost_unknown``，doctor 给出告警。
新模型可通过 ``PP_PRICE_OVERRIDES`` 以 JSON 注入，无需改代码。

单位为「美元 / 每 100 万 token」，格式 ``(输入, 输出)``。
单价随厂商调整，此处仅用于成本估算与预算熔断，不构成准确账单。
"""

from __future__ import annotations

from collections.abc import Mapping

PriceOverrides = Mapping[str, tuple[float, float]]

PRICE_TABLE: dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # 通义千问
    "qwen-max": (1.60, 6.40),
    "qwen-plus": (0.40, 1.20),
    "qwen-turbo": (0.05, 0.20),
    # 自测用的假模型：显式免费
    "stub": (0.0, 0.0),
}

# 同族兜底：型号带日期后缀时按族取价（如 deepseek-chat-0324）
FAMILY_FALLBACK: dict[str, str] = {
    "deepseek-v3": "deepseek-chat",
    "qwen3-max": "qwen-max",
    "qwen3-plus": "qwen-plus",
}


def price_for(
    model: str, overrides: PriceOverrides | None = None
) -> tuple[float, float] | None:
    """查价。返回 ``None`` 表示价格未知（不要当成免费）。"""
    table: dict[str, tuple[float, float]] = dict(PRICE_TABLE)
    if overrides:
        table.update({k: tuple(v) for k, v in overrides.items()})

    if model in table:
        return table[model]

    for name, price in table.items():
        if model.startswith(name):
            return price

    for family, canonical in FAMILY_FALLBACK.items():
        if model.startswith(family) and canonical in table:
            return table[canonical]

    return None


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    overrides: PriceOverrides | None = None,
) -> float | None:
    """估算单次调用成本（美元）。价格未知时返回 ``None``，绝不静默返回 0。"""
    price = price_for(model, overrides)
    if price is None:
        return None
    price_in, price_out = price
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


def is_priced(model: str, overrides: PriceOverrides | None = None) -> bool:
    return price_for(model, overrides) is not None
