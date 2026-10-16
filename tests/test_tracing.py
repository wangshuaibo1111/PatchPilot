from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchpilot.observability.tracing import Tracer


def _last_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_cost_unknown_is_recorded_not_zeroed(tmp_path: Path) -> None:
    """审查 BLOCKING-10：价格未知必须显式标记，不能假装成本是 0。"""
    tracer = Tracer(tmp_path, "t")

    cost = tracer.record_usage(model="who-knows", prompt_tokens=1000, completion_tokens=1000)

    assert cost is None
    assert tracer.trace.cost_unknown is True
    assert tracer.trace.unknown_priced_models == ["who-knows"]

    record = _last_record(tracer.finish())
    assert record["cost_unknown"] is True
    assert record["cost_usd"] == 0.0


def test_price_overrides_enable_cost_tracking(tmp_path: Path) -> None:
    tracer = Tracer(tmp_path, "t", price_overrides={"who-knows": (1.0, 2.0)})

    cost = tracer.record_usage(
        model="who-knows", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )

    assert cost == pytest.approx(3.0)
    assert tracer.trace.cost_unknown is False
    assert _last_record(tracer.finish())["cost_usd"] == pytest.approx(3.0)


def test_span_records_name_attributes_and_duration(tmp_path: Path) -> None:
    tracer = Tracer(tmp_path, "t")

    with tracer.span("step", foo="bar"):
        pass

    record = _last_record(tracer.finish())
    assert record["spans"][0]["name"] == "step"
    assert record["spans"][0]["attributes"] == {"foo": "bar"}
    assert record["spans"][0]["duration_ms"] >= 0


def test_trace_appends_to_same_daily_file(tmp_path: Path) -> None:
    first = Tracer(tmp_path, "a").finish()
    second = Tracer(tmp_path, "b").finish()

    assert first == second
    lines = second.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_finish_marks_cost_unknown_when_graph_reports_unpriced_models(tmp_path: Path) -> None:
    """四轮审查阻塞 4：`unpriced_models` 非空时必须同步 `cost_unknown`。

    否则会产出 `cost_unknown=false` 而 `unpriced_models` 非空的组合，
    读 trace 的人会误以为成本可信。
    """
    record = _last_record(Tracer(tmp_path, "t").finish(unpriced_models=["ghost-model"]))

    assert record["cost_unknown"] is True
    assert record["unknown_priced_models"] == ["ghost-model"]
    assert record["unpriced_models"] == ["ghost-model"]


def test_finish_filters_falsy_unpriced_models(tmp_path: Path) -> None:
    """四轮审查非阻塞 6：falsy 项不该留在 trace 里制造矛盾组合。"""
    record = _last_record(Tracer(tmp_path, "t").finish(unpriced_models=["", None]))

    assert record["unpriced_models"] == []
    assert record["cost_unknown"] is False
