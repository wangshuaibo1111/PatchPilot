from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from patchpilot.core.config import Settings
from patchpilot.graph.checkpointer import open_checkpointer
from patchpilot.graph.minimal import GraphOptions, PassPolicy, build_minimal_graph


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _invoke(checkpointer: object, thread_id: str) -> tuple[object, dict, dict]:
    graph = build_minimal_graph(
        GraphOptions(policy=PassPolicy(pass_after_attempt=2))
    ).compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    final = graph.invoke({"task_id": thread_id, "issue": "x", "notes": []}, config=config)
    return graph, config, final


def test_memory_backend_runs() -> None:
    settings = Settings(_env_file=None, checkpoint_backend="memory")

    with open_checkpointer(settings) as saver:
        graph, config, final = _invoke(saver, "mem-1")
        history = list(graph.get_state_history(config))  # type: ignore[attr-defined]

    assert final["approved"] is True
    assert len(history) >= 1


def _enter(settings: Settings) -> None:
    """进入 checkpoint 上下文后立刻退出，用于断言构造期的错误。"""
    with open_checkpointer(settings):
        pass


def test_unknown_backend_is_rejected() -> None:
    settings = Settings.model_construct(checkpoint_backend="nope")

    with pytest.raises(ValueError, match="未知的 checkpoint 后端"):
        _enter(settings)


@pytest.mark.skipif(
    _installed("langgraph.checkpoint.postgres"),
    reason="已安装 postgres 后端，缺依赖路径不适用",
)
def test_postgres_without_dependency_is_actionable() -> None:
    settings = Settings(_env_file=None, checkpoint_backend="postgres")

    with pytest.raises(RuntimeError, match="langgraph-checkpoint-postgres"):
        _enter(settings)


@pytest.mark.skipif(
    not _installed("langgraph.checkpoint.sqlite"),
    reason="未安装 langgraph-checkpoint-sqlite",
)
def test_sqlite_backend_persists_history_across_reopen(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, checkpoint_backend="sqlite", sqlite_path=tmp_path / "cp.sqlite"
    )

    with open_checkpointer(settings) as saver:
        graph, config, final = _invoke(saver, "sqlite-1")
        history = list(graph.get_state_history(config))  # type: ignore[attr-defined]

    assert final["approved"] is True
    assert len(history) >= 1
    assert (tmp_path / "cp.sqlite").exists()

    # 重新打开同一个库：历史必须还在，证明 checkpoint 真的落盘
    with open_checkpointer(settings) as saver2:
        graph2 = build_minimal_graph(GraphOptions()).compile(checkpointer=saver2)
        history2 = list(
            graph2.get_state_history({"configurable": {"thread_id": "sqlite-1"}})
        )

    assert len(history2) == len(history)
