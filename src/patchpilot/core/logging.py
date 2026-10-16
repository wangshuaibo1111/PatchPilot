"""结构化日志。

统一走 structlog：本地开发输出彩色可读文本，CI/生产输出单行 JSON，
便于日志采集系统直接索引 trace_id / task_id 等字段。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from patchpilot.core.config import Settings

_configured = False


def configure_logging(settings: Settings | None = None) -> None:
    """初始化日志。重复调用是安全的（幂等）。"""
    global _configured

    from patchpilot.core.config import get_settings

    settings = settings or get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> Any:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
