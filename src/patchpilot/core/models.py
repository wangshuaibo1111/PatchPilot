"""领域模型：任务、定位结果、补丁、验证结果、任务产出。

所有跨模块传递的数据都必须是这里的 Pydantic 模型，
不允许用裸 dict 在层之间传参（保证可校验、可序列化、可落库）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class FailureKind(StrEnum):
    """失败归因分类——评测报告的核心维度，直接对应失败原因分布图。"""

    NONE = "none"
    LOCALIZE = "localize"  # 定位失败：没找到正确的文件/符号
    EDIT = "edit"  # 编辑失败：diff 不合法或无法应用
    TEST_FAIL = "test_fail"  # 改对了地方但测试仍然不过
    ENV = "env"  # 环境问题：依赖装不上、镜像构建失败
    BUDGET = "budget"  # 预算耗尽
    UNKNOWN = "unknown"


class TaskSpec(BaseModel):
    """一个待修复任务的输入。"""

    task_id: str
    repo_path: Path
    issue_text: str
    base_commit: str | None = None
    test_command: str | None = None
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)


class FileHit(BaseModel):
    """检索层返回的一个候选文件。"""

    path: str
    score: float
    reason: str = ""


class LocalizationResult(BaseModel):
    files: list[FileHit] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    latency_ms: int = 0


class PatchCandidate(BaseModel):
    diff: str
    rationale: str = ""
    confidence: float = 0.0
    files_touched: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    passed: bool
    failed_tests: list[str] = Field(default_factory=list)
    passed_tests: list[str] = Field(default_factory=list)
    output_excerpt: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


class TaskOutcome(BaseModel):
    """一个任务的最终产出，也是评测层落库与聚合的单元。"""

    task_id: str
    resolved: bool = False
    attempts: int = 0
    failure_kind: FailureKind = FailureKind.NONE
    patch: PatchCandidate | None = None
    verification: VerificationResult | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    wall_time_s: float = 0.0
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
