"""模型接入层。

P0 阶段只启用 StubLLM，保证骨架能在无网络、无密钥、无花费的情况下自测。
OpenAICompatibleLLM 覆盖 DeepSeek / 通义千问 / 任意 OpenAI 兼容端点，
在 P3 接入真实修复节点时启用并验证（当前未经真实调用验证）。

审查修正（NON_BLOCKING-2）：客户端支持显式关闭；上游异常统一脱敏后上抛，
避免 provider 的报错回显 API Key、再经 structlog 的 format_exc_info 落进 JSONL。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from patchpilot.core.config import Settings
from patchpilot.core.pricing import estimate_cost_usd

_SECRET_IN_TEXT = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})\b")


class LLMError(RuntimeError):
    """模型调用失败的统一异常，消息已脱敏。"""


def sanitize_text(text: str) -> str:
    """抹掉文本中疑似密钥的片段，用于异常与日志。"""
    return _SECRET_IN_TEXT.sub("[已脱敏]", text)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    cost_usd: float | None = 0.0


@runtime_checkable
class LLMClient(Protocol):
    """模型客户端协议：换模型只换实现，不动编排层。"""

    model: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> LLMResponse: ...


class StubLLM:
    """确定性假模型：不联网、不花钱，用于骨架自测与 CI。"""

    def __init__(self, model: str = "stub") -> None:
        self.model = model

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> LLMResponse:
        prompt = "\n".join(m.content for m in messages)
        name = model or self.model
        text = f"[stub:{name}] 已接收 {len(messages)} 条消息 / {len(prompt)} 字符"
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(1, len(text) // 4)
        return LLMResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=name,
            cost_usd=estimate_cost_usd(name, prompt_tokens, completion_tokens),
        )


class OpenAICompatibleLLM:
    """OpenAI 兼容协议的异步客户端（DeepSeek / Qwen / 自建网关）。

    P3 阶段接入真实节点时验证。需要安装 openai 依赖：
        pip install "patchpilot[llm]"
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 90.0,
        price_overrides: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 API Key：请在 .env 中设置 PP_LLM_API_KEY")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - 取决于可选依赖
            raise RuntimeError('缺少 openai 依赖，请执行 pip install "patchpilot[llm]"') from exc

        self.model = model
        self._price_overrides = price_overrides
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> LLMResponse:
        name = model or self.model
        try:
            resp = await self._client.chat.completions.create(
                model=name,
                messages=[{"role": m.role, "content": m.content} for m in messages],  # type: ignore[misc]
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 - 统一归一化，避免上游报文原样外泄
            raise LLMError(f"模型调用失败: {sanitize_text(str(exc))}") from None

        text: str = resp.choices[0].message.content or ""
        usage: Any = resp.usage
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return LLMResponse(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=name,
            cost_usd=estimate_cost_usd(
                name, prompt_tokens, completion_tokens, self._price_overrides
            ),
        )

    async def aclose(self) -> None:
        """显式关闭底层 HTTP 连接池，避免长跑时连接泄漏。"""
        await self._client.close()

    async def __aenter__(self) -> OpenAICompatibleLLM:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


def build_llm(settings: Settings, *, small: bool = False) -> LLMClient:
    """按配置构建模型客户端。默认返回 StubLLM，不产生任何外部调用。"""
    if settings.llm_provider == "stub":
        return StubLLM(model="stub")
    return OpenAICompatibleLLM(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_small_model if small else settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        price_overrides=settings.price_overrides,
    )
