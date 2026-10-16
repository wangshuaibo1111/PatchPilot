"""JUnit XML 解析：把沙箱里的 pytest 结果变成结构化数据。

为什么值得单独成模块：这是「测试执行 → 结构化判定」的唯一入口，
P1 的完成标志与 P4 的 pass@1 全都依赖它。纯逻辑、零外部依赖，
可在无 Docker 环境穷举测试。

位置：`src/patchpilot/sandbox/junit.py`（原 `.tmp/p1_staging/junit.py`），
断言已迁为 `tests/test_junit.py`。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TestStatus = Literal["passed", "failed", "error", "skipped"]

# 不可信 XML 的体积上限（8 MiB）：正常的 JUnit 报告远小于此，
# 而沙箱内的不可信代码可以写出任意大的文件。
MAX_JUNIT_BYTES = 8 * 1024 * 1024

# 拒绝 DTD/实体声明：ElementTree 官方明示不防恶意构造数据。
_DOCTYPE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


class JUnitParseError(ValueError):
    """XML 缺失、损坏或结构不符。

    调用方**必须**把它归类为 ENV/工具链失败，绝不允许被当成"测试通过"。
    """


@dataclass(frozen=True, slots=True)
class TestCaseOutcome:
    node_id: str
    status: TestStatus
    duration_s: float = 0.0
    message: str = ""


@dataclass(frozen=True, slots=True)
class JUnitReport:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_s: float
    cases: tuple[TestCaseOutcome, ...] = ()
    source: str = ""

    @property
    def ok(self) -> bool:
        """**必须要求 total > 0**：一个测试都没收集到不算成功。

        与 preflight 中「零文件被解析时报 fail」同源——
        "什么都没跑"被当成"跑过了没问题"是同一类静默假通过缺陷。
        """
        return self.total > 0 and self.failed == 0 and self.errors == 0

    def failed_node_ids(self) -> list[str]:
        return [c.node_id for c in self.cases if c.status in {"failed", "error"}]

    def passed_node_ids(self) -> list[str]:
        return [c.node_id for c in self.cases if c.status == "passed"]

    def summary_line(self) -> str:
        return (
            f"tests={self.total} passed={self.passed} failed={self.failed} "
            f"errors={self.errors} skipped={self.skipped} "
            f"duration={self.duration_s:.2f}s ok={self.ok}"
        )


def _float_attr(element: ET.Element, name: str) -> float:
    raw = element.get(name)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _status_of(case: ET.Element) -> TestStatus:
    for child in case:
        tag = child.tag.lower()
        if tag == "failure":
            return "failed"
        if tag == "error":
            return "error"
        if tag == "skipped":
            return "skipped"
    return "passed"


def _message_of(case: ET.Element) -> str:
    for child in case:
        if child.tag.lower() in {"failure", "error", "skipped"}:
            message = (child.get("message") or "").strip()
            if message:
                return message
            return (child.text or "").strip()[:200]
    return ""


def _node_id(case: ET.Element) -> str:
    classname = (case.get("classname") or "").strip()
    name = (case.get("name") or "").strip() or "<unnamed>"
    return f"{classname}::{name}" if classname else name


def parse_junit_text(text: str, *, source: str = "<memory>") -> JUnitReport:
    """解析 JUnit XML 文本。`root.iter("testcase")` 会递归收集嵌套 testsuite。

    两道不可信输入闸门对**内存入口与文件入口都生效**：体积上限与 DOCTYPE/ENTITY 拒绝
    （P1 从文件解析，但内存入口同样可能被喂入沙箱回来的字符串，不能只在一处设防）。
    """
    if len(text) > MAX_JUNIT_BYTES:
        raise JUnitParseError(
            f"测试报告过大（{len(text)} > {MAX_JUNIT_BYTES} 字符），拒绝解析不可信输入"
        )
    if _DOCTYPE_RE.search(text):
        raise JUnitParseError("拒绝含 DOCTYPE/ENTITY 的文档（不可信输入不展开实体）")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise JUnitParseError(f"XML 解析失败: {exc}") from exc

    if root.tag not in {"testsuites", "testsuite"}:
        raise JUnitParseError(f"根元素必须是 testsuites/testsuite，实际为 {root.tag!r}")

    cases = tuple(
        TestCaseOutcome(
            node_id=_node_id(case),
            status=_status_of(case),
            duration_s=_float_attr(case, "time"),
            message=_message_of(case),
        )
        for case in root.iter("testcase")
    )

    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for case in cases:
        counts[case.status] += 1

    # 耗时优先取 testsuite 的聚合值：pytest 的 `<testsuites>` 只是包装元素、没有 time 属性，
    # 真正的聚合耗时写在内层 `<testsuite>` 上。只取 root 的**直接子** testsuite——
    # 嵌套 testsuite 的 time 已包含子项，一并求和会翻倍。
    # （缺陷记录：初版读 root 的 time，导致耗时被静默降级为"子项求和"。）
    if root.tag == "testsuite":
        suite_elements = [root]
    else:
        suite_elements = [child for child in root if child.tag == "testsuite"]
    suite_times = [_float_attr(element, "time") for element in suite_elements]
    if any(value > 0 for value in suite_times):
        duration = round(sum(suite_times), 3)
    else:
        duration = round(sum(c.duration_s for c in cases), 3)

    return JUnitReport(
        total=len(cases),
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"],
        skipped=counts["skipped"],
        duration_s=duration,
        cases=cases,
        source=source,
    )


def parse_junit_xml(path: Path) -> JUnitReport:
    """从文件解析 JUnit XML。

    **不可信输入的两道闸**（P1 接上 Docker 后，这份 XML 由沙箱内运行的测试产出，
    也就是不可信代码可以完全控制它的内容）：
      1. 体积上限：超限直接拒绝，不把整个文件读进内存；
      2. 外部实体：`ElementTree` 官方明示不防恶意构造数据，这里显式拒绝带
         DOCTYPE/ENTITY 的文档（防实体展开类的资源耗尽）。
    """
    target = Path(path)
    if not target.is_file():
        raise JUnitParseError(f"未找到测试报告: {target}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise JUnitParseError(f"读取测试报告失败: {exc}") from exc
    if size > MAX_JUNIT_BYTES:
        raise JUnitParseError(
            f"测试报告过大（{size} > {MAX_JUNIT_BYTES} 字节），拒绝解析不可信输入"
        )

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise JUnitParseError(f"读取测试报告失败: {exc}") from exc
    return parse_junit_text(text, source=str(target))
