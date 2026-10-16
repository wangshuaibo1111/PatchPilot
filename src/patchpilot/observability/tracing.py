"""轻量可观测：span 记录 + 用量核算 + JSONL 落盘。

设计取舍：P0 不引入 Langfuse/OpenTelemetry 服务端
（clickhouse + minio + web 镜像体积大、启动慢），
先用零依赖的 JSONL trace 打通「每个任务都可回放、可算成本」这条链路；
P5 阶段在此基础上按 OTel GenAI 语义约定导出到 Langfuse。

审查修正（BLOCKING-10）：价格未知的模型不再静默按 0 计费，
而是记录 ``cost_unknown`` 与 ``unknown_priced_models``，
使评测报告能暴露"成本不可信"这一事实。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from patchpilot.core.pricing import estimate_cost_usd


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class Span:
    name: str
    started_at: float
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        if self.ended_at is None:
            self.ended_at = time.perf_counter()

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.perf_counter()
        return round((end - self.started_at) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "attributes": self.attributes,
        }


@dataclass(slots=True)
class Trace:
    trace_id: str
    name: str
    started_at: str
    spans: list[Span] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    cost_unknown: bool = False
    unknown_priced_models: list[str] = field(default_factory=list)
    status: str = "running"
    ended_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def duration_ms(self) -> float:
        return round(sum(s.duration_ms for s in self.spans), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_unknown": self.cost_unknown,
            "unknown_priced_models": self.unknown_priced_models,
            "models": self.models,
            "spans": [s.to_dict() for s in self.spans],
            **self.extra,
        }


class Tracer:
    """一次任务运行对应一个 Tracer，结束时追加写入当天的 JSONL 文件。"""

    def __init__(
        self,
        trace_dir: Path,
        name: str,
        *,
        price_overrides: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        self.trace_dir = Path(trace_dir)
        self._price_overrides = price_overrides
        self.trace = Trace(trace_id=uuid.uuid4().hex[:16], name=name, started_at=_now_iso())

    @property
    def trace_id(self) -> str:
        return self.trace.trace_id

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(name=name, started_at=time.perf_counter(), attributes=dict(attributes))
        self.trace.spans.append(span)
        try:
            yield span
        finally:
            span.close()

    def record_usage(
        self, *, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float | None:
        """记录一次模型调用的用量。

        返回本次估算成本；价格未知时返回 ``None`` 并把 trace 标记为
        ``cost_unknown``——绝不假装成本是 0。
        """
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens, self._price_overrides)
        self.trace.prompt_tokens += max(0, prompt_tokens)
        self.trace.completion_tokens += max(0, completion_tokens)
        if cost is None:
            self.trace.cost_unknown = True
            if model and model not in self.trace.unknown_priced_models:
                self.trace.unknown_priced_models.append(model)
        else:
            self.trace.cost_usd += cost
        if model and model not in self.trace.models:
            self.trace.models.append(model)
        return cost

    def finish(self, *, status: str = "ok", **extra: Any) -> Path:
        """落盘并返回 trace 文件路径。"""
        self.trace.status = status
        self.trace.ended_at = _now_iso()

        # 图内出现未定价模型时，整条 trace 的成本就不可信：
        # 否则会产生 cost_unknown=false 而 unpriced_models 非空的自相矛盾组合，
        # 读 trace 的人会误以为成本可信（三轮审查 NON_BLOCKING-4）。
        # 顺带把 falsy 项过滤掉再写回，避免留下 `unpriced_models: ['']`（四轮审查非阻塞 6）。
        if "unpriced_models" in extra:
            graph_unpriced = [model for model in (extra["unpriced_models"] or []) if model]
            extra["unpriced_models"] = graph_unpriced
            if graph_unpriced:
                self.trace.cost_unknown = True
                for model in graph_unpriced:
                    if model not in self.trace.unknown_priced_models:
                        self.trace.unknown_priced_models.append(model)

        self.trace.extra.update(extra)

        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.trace.to_dict(), ensure_ascii=False) + "\n")
        return path
