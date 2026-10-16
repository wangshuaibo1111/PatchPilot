"""全局配置。

约定：所有配置项通过环境变量或 .env 提供，统一使用 PP_ 前缀。
这样可以在不改代码的前提下切换模型、存储后端与预算上限，
也是 CI 与生产环境配置管理的唯一入口。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from patchpilot.core.budget import Budget

LLMProvider = Literal["stub", "deepseek", "qwen", "openai_compatible"]
CheckpointBackend = Literal["memory", "sqlite", "postgres"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 基础
    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    # 编排 / checkpoint
    checkpoint_backend: CheckpointBackend = "memory"
    sqlite_path: Path = Path(".patchpilot/checkpoints.sqlite")
    # 默认值刻意不含凭据：账号密码请写进 .env（见 .env.example）。
    # 代码里的默认凭据既会触发门禁的"URL 内嵌凭据"规则，也是不良实践。
    postgres_dsn: str = "postgresql://localhost:5432/patchpilot"

    # 依赖服务
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # 模型
    llm_provider: LLMProvider = "stub"
    # 与 `.env.example` 保持一致（官方 OpenAI 兼容端点是裸域名，无需 `/v1` 后缀）
    llm_base_url: str = "https://api.deepseek.com"
    # SecretStr：repr / str / model_dump / 日志回显都只显示掩码。
    # `get_secret_value()` 的调用点有两处，都是安全的：`llm.build_llm`（真正建客户端）
    # 与 `safe_summary()`（只判断"是否已配置"，不输出明文）。
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_model: str = "deepseek-flash"
    llm_small_model: str = "deepseek-flash"
    llm_timeout_s: float = 90.0

    # 价格覆盖：形如 {"deepseek-v3": [0.3, 1.2]}，新模型上线时无需改代码
    price_overrides: dict[str, tuple[float, float]] = Field(default_factory=dict)

    # 预算：四重硬上限
    budget_max_tokens: int = 200_000
    budget_max_usd: float = 2.0
    budget_max_seconds: float = 900.0
    budget_max_attempts: int = 3

    # 可观测
    trace_dir: Path = Path(".patchpilot/traces")

    # 门禁：pytest 临时目录（受限环境下指向可写位置，避免误报失败）
    check_basetemp: Path | None = None

    def budget(self) -> Budget:
        return Budget(
            max_tokens=self.budget_max_tokens,
            max_usd=self.budget_max_usd,
            max_seconds=self.budget_max_seconds,
            max_attempts=self.budget_max_attempts,
        )

    def safe_summary(self) -> dict[str, object]:
        """用于日志/doctor 输出的配置摘要，绝不包含 api_key。"""
        return {
            "env": self.env,
            "checkpoint_backend": self.checkpoint_backend,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_api_key": "已配置" if self.llm_api_key.get_secret_value() else "未配置",
            "budget": {
                "max_tokens": self.budget_max_tokens,
                "max_usd": self.budget_max_usd,
                "max_seconds": self.budget_max_seconds,
                "max_attempts": self.budget_max_attempts,
            },
            "price_overrides": sorted(self.price_overrides),
            "trace_dir": str(self.trace_dir),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
