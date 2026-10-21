"""模型接入层。

P0 阶段只启用 StubLLM，保证骨架能在无网络、无密钥、无花费的情况下自测。
OpenAICompatibleLLM 覆盖 DeepSeek / 通义千问 / 任意 OpenAI 兼容端点，
在 P3 接入真实修复节点时启用并验证（当前未经真实调用验证）。

审查修正（NON_BLOCKING-2）：客户端支持显式关闭；上游异常统一脱敏后上抛，
避免 provider 的报错回显 API Key、再经 structlog 的 format_exc_info 落进 JSONL。
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
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

        # 代理客户端常把 `NO_PROXY` 写成 `…,::1,[::1]`，而 httpx 会把 `[::1]` 当 URL、
        # 把 `:1]` 当端口 → 客户端**构造时就抛 InvalidURL**（真机事故，见 docs/PLAN）。
        # 这类环境不是我们的错，但应用不该起不来，故在创建客户端前清理一次。
        from patchpilot.core.netenv import sanitize_no_proxy

        sanitize_no_proxy()

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


# ---------------------------------------------------------------- 同步桥接


_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


def _shared_loop() -> asyncio.AbstractEventLoop:
    """一个**常驻**事件循环（跑在后台守护线程里）。

    为什么不能用 `asyncio.run`（P3 真机实测）：
    `AsyncOpenAI` 内部持有 httpx 连接池，连接**绑定创建它的事件循环**。
    每次调用都 `asyncio.run` 会建新循环、用完即关，第二次调用就会拿到
    已关闭循环里的连接 → `LLMError: 模型调用失败: Event loop is closed`。
    修复闭环要连续调用模型多轮，所以必须复用一个循环。
    """
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever, name="patchpilot-llm-loop", daemon=True
            ).start()
            _LOOP = loop
        return _LOOP


def run_sync(coro: Any) -> Any:
    """在常驻循环里跑一个协程并等结果（可在同步代码里安全多次调用）。"""
    loop = _shared_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def complete_sync(
    llm: LLMClient,
    messages: Sequence[ChatMessage],
    *,
    temperature: float = 0.0,
    model: str | None = None,
) -> LLMResponse:
    """同步调用任意 `LLMClient`（图节点是同步的，模型客户端是异步的）。

    - 若当前线程**没有**运行中的事件循环：走常驻循环（复用连接池）；
    - 若已有运行中的循环（如在异步框架里被调用）：另起线程执行，避免嵌套。
    """
    coro = llm.complete(messages, temperature=temperature, model=model)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_sync(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run_sync, coro).result()
