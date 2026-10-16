"""JUnit XML 解析器的测试（**无需 Docker**）。

来源：`.tmp/p1_staging/verify_junit.py`（离线 29/29）按 `MIGRATION.md` 改写为 pytest。

为什么这个模块值得单独测：它是「测试执行 → 结构化判定」的唯一入口，
P1 的完成标志与 P4 的 pass@1 全都依赖它。最关键的一条不变量是
**"一个测试都没收集到"绝不能算成功**——那与 preflight 的「零文件被解析即 fail」同源。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.sandbox.junit import (
    JUnitParseError,
    parse_junit_text,
    parse_junit_xml,
)

PYTEST_OK = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="0" failures="0" skipped="1" tests="3" time="0.123">
    <testcase classname="tests.test_x" name="test_a" time="0.010"/>
    <testcase classname="tests.test_x" name="test_b" time="0.020"/>
    <testcase classname="tests.test_x" name="test_c" time="0.000">
      <skipped type="pytest.skip" message="平台不支持"/>
    </testcase>
  </testsuite>
</testsuites>
"""

PYTEST_FAIL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" tests="2" time="0.5">
    <testcase classname="tests.test_x" name="test_a" time="0.1">
      <failure message="assert 1 == 2">AssertionError</failure>
    </testcase>
    <testcase classname="tests.test_x" name="test_b" time="0.2">
      <error message="RuntimeError: boom">traceback</error>
    </testcase>
  </testsuite>
</testsuites>
"""

NESTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="outer">
    <testsuite name="inner-a">
      <testcase classname="pkg.mod" name="t1" time="0.01"/>
    </testsuite>
    <testsuite name="inner-b">
      <testcase classname="pkg.mod" name="t2" time="0.02"/>
      <testcase classname="pkg.mod" name="t3" time="0.03"/>
    </testsuite>
  </testsuite>
</testsuites>
"""

MULTI_SUITES = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="a" time="0.10"><testcase classname="x" name="t1" time="0.01"/></testsuite>
  <testsuite name="b" time="0.20"><testcase classname="x" name="t2" time="0.02"/></testsuite>
</testsuites>
"""


# ------------------------------------------------------------- A. 正常路径


def test_parses_counts_node_ids_and_duration() -> None:
    report = parse_junit_text(PYTEST_OK)

    assert report.total == 3
    assert report.skipped == 1
    assert report.ok is True, "有跳过但不影响整体 ok"
    assert report.cases[0].node_id == "tests.test_x::test_a"
    assert report.cases[1].duration_s == 0.02
    assert report.cases[2].message == "平台不支持"
    # 聚合耗时取自内层 testsuite（`<testsuites>` 只是包装元素、没有 time）
    assert report.duration_s == 0.123


def test_passed_node_ids_excludes_skipped() -> None:
    report = parse_junit_text(PYTEST_OK)

    assert report.passed_node_ids() == ["tests.test_x::test_a", "tests.test_x::test_b"]


def test_failures_and_errors_are_both_counted_and_reported() -> None:
    report = parse_junit_text(PYTEST_FAIL)

    assert report.failed == 1
    assert report.errors == 1
    assert report.ok is False
    assert report.failed_node_ids() == ["tests.test_x::test_a", "tests.test_x::test_b"]
    assert report.cases[0].message == "assert 1 == 2"


# ------------------------------------- B. 静默假通过：零用例绝不能算成功


def test_empty_testsuites_is_not_success() -> None:
    report = parse_junit_text('<?xml version="1.0"?><testsuites></testsuites>')

    assert report.total == 0
    assert report.ok is False, "一个测试都没收集到不能算通过"


def test_empty_testsuite_is_not_success() -> None:
    report = parse_junit_text(
        '<?xml version="1.0"?><testsuite name="pytest" tests="0" failures="0" errors="0"/>'
    )

    assert report.ok is False


def test_all_skipped_counts_as_total_but_ok_true_needs_caller_judgement() -> None:
    report = parse_junit_text(
        '<?xml version="1.0"?><testsuite><testcase classname="a" name="t">'
        '<skipped message="全部跳过"/></testcase></testsuite>'
    )

    # 解析层只保证 total > 0；"全跳过是否算通过"由上层策略决定
    assert report.total == 1
    assert report.ok is True


# ------------------------------------------- C. 嵌套与多 suite（真实形态）


def test_nested_testsuites_are_collected_recursively() -> None:
    report = parse_junit_text(NESTED)

    assert report.total == 3
    assert report.passed == 3
    assert report.ok is True
    # 没有聚合 time 时由子项求和
    assert report.duration_s == 0.06


def test_multiple_top_level_suites_have_durations_summed() -> None:
    report = parse_junit_text(MULTI_SUITES)

    assert report.duration_s == 0.3
    assert report.total == 2


# --------------------------------- D. 异常输入必须显式报错，不得当成通过


@pytest.mark.parametrize(
    "payload",
    [
        "<testsuites><oops>",  # 损坏
        "<report><testcase/></report>",  # 根元素不对
        "",  # 空字符串
    ],
)
def test_malformed_input_raises(payload: str) -> None:
    with pytest.raises(JUnitParseError):
        parse_junit_text(payload)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(JUnitParseError):
        parse_junit_xml(tmp_path / "nope.xml")


def test_truncated_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "truncated.xml"
    bad.write_text('<?xml version="1.0"?><testsuites><testsuite>', encoding="utf-8")

    with pytest.raises(JUnitParseError):
        parse_junit_xml(bad)


# ------------------------------------------------- E. 边界：缺属性 / 非法值


def test_sparse_attributes_are_tolerated() -> None:
    report = parse_junit_text(
        '<?xml version="1.0"?><testsuite><testcase name="bare"/>'
        '<testcase classname="c" name="n" time="not-a-number"/></testsuite>'
    )

    assert report.cases[0].node_id == "bare", "无 classname 时 node_id 就是 name"
    assert report.cases[1].duration_s == 0.0, "非法 time 归零而不是抛异常"
    assert report.total == 2


def test_reads_from_file_and_records_source(tmp_path: Path) -> None:
    good = tmp_path / "ok.xml"
    good.write_text(PYTEST_OK, encoding="utf-8")

    report = parse_junit_xml(good)

    assert report.total == 3
    assert report.source.endswith("ok.xml")


# ------------------------------- F. 不可信输入的两道闸（P1 接上 Docker 后必需）


def test_oversized_report_is_rejected_without_reading_it_all(tmp_path: Path) -> None:
    """沙箱内的不可信代码可以写出任意大的"测试报告"，必须在读入前就拒绝。"""
    huge = tmp_path / "huge.xml"
    huge.write_bytes(b"<testsuites>" + b" " * (8 * 1024 * 1024 + 1))

    with pytest.raises(JUnitParseError, match="过大"):
        parse_junit_xml(huge)


@pytest.mark.parametrize(
    "payload",
    [
        '<!DOCTYPE foo [<!ENTITY x "y">]><testsuites/>',
        '<!ENTITY x "y"><testsuites/>',
    ],
)
def test_doctype_and_entities_are_rejected(payload: str) -> None:
    """ElementTree 官方明示不防恶意构造数据 → 不展开实体，直接拒绝。"""
    with pytest.raises(JUnitParseError):
        parse_junit_text(payload)


def test_memory_entry_enforces_size_limit() -> None:
    """**内存入口**的体积闸也要有用例（第十一轮变异实验 M32 曾存活）。

    只测文件入口时，`parse_junit_text` 的上限被删掉也不会有断言变红——
    而 P1 之后完全可能把沙箱回来的字符串直接喂给内存入口。
    """
    huge = "<testsuites>" + " " * (8 * 1024 * 1024 + 1)

    with pytest.raises(JUnitParseError, match="过大"):
        parse_junit_text(huge)
