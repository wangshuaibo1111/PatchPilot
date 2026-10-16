"""Checkpoint 后端工厂。

"任务可断点续跑"是 Agent 平台的硬需求：长任务失败后要能从上一个
节点恢复，而不是从头再来。这里把后端做成可切换的，
让 smoke/单测用内存、PC 开发用 SQLite、生产用 Postgres。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from patchpilot.core.config import Settings, get_settings


def _in_memory_saver_cls() -> Any:
    """兼容 InMemorySaver / MemorySaver 两种命名。"""
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver
    except ImportError:  # pragma: no cover - 老版本兼容
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver


@contextmanager
def open_checkpointer(settings: Settings | None = None) -> Iterator[Any]:
    """按配置打开 checkpoint 后端，退出时自动释放连接。"""
    settings = settings or get_settings()
    backend = settings.checkpoint_backend

    if backend == "memory":
        yield _in_memory_saver_cls()()
        return

    if backend == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise RuntimeError(
                "缺少 SQLite checkpoint 依赖，请执行："
                "pip install langgraph-checkpoint-sqlite"
            ) from exc
        path = Path(settings.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            yield SqliteSaver(conn)
        finally:
            conn.close()
        return

    if backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise RuntimeError(
                "缺少 Postgres checkpoint 依赖，请执行："
                'pip install "langgraph-checkpoint-postgres" "psycopg[binary]"'
            ) from exc
        with PostgresSaver.from_conn_string(settings.postgres_dsn) as saver:
            saver.setup()
            yield saver
        return

    raise ValueError(f"未知的 checkpoint 后端: {backend}")
