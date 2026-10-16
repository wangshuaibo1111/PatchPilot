"""沙箱驱动层测试。

分成两类（与 `p1_sandbox_design.md` §5 一致）：
  - **不需要 Docker**：编排逻辑（复制失败、超时参数、副本清理、命令构造）。
    Docker 不可用时这些用例**仍然要跑**，因为它们是纯逻辑。
  - **需要 Docker**：真在沙箱里跑 pytest、拿到结构化结果、超时被杀且容器清理。
    缺 Docker 或镜像时 `skip`（**刻意不自动 pull**，避免给门禁引入网络依赖）。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from patchpilot.sandbox.manager import (
    JUNIT_REL_PATH,
    SandboxRunError,
    docker_available,
    image_available,
    run_in_sandbox,
    run_pytest_in_sandbox,
)
from patchpilot.sandbox.policy import DEFAULT_IMAGE, SandboxPolicy


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return root


def _docker_ready() -> tuple[bool, str]:
    ok, detail = docker_available()
    if not ok:
        return False, detail
    if not image_available(DEFAULT_IMAGE):
        return False, f"本地无镜像 {DEFAULT_IMAGE}（按 docker/sandbox/Dockerfile 构建）"
    return True, detail


requires_docker = pytest.mark.skipif(
    not _docker_ready()[0], reason=f"Docker 不可用: {_docker_ready()[1]}"
)


# ------------------------------------------------------------ 纯逻辑（无需 Docker）


def test_run_in_sandbox_rejects_copy_failure(tmp_path: Path, sample_repo: Path) -> None:
    """工作区复制失败要归一化成 `SandboxRunError`（而不是漏出底层异常）。"""
    policy = SandboxPolicy(sandbox_root=tmp_path)

    with pytest.raises(SandboxRunError, match="复制失败"):
        run_in_sandbox(
            policy=policy,
            source=tmp_path / "does-not-exist",
            command=["true"],
        )


def test_run_in_sandbox_requires_a_copy_root(sample_repo: Path) -> None:
    """没有 copy_root、policy 也没声明 sandbox_root 时必须显式报错。"""
    policy = SandboxPolicy(sandbox_root=None)

    with pytest.raises(SandboxRunError, match="copy_root"):
        run_in_sandbox(policy=policy, source=sample_repo, command=["true"])


def test_run_in_sandbox_rejects_non_positive_timeout(tmp_path: Path, sample_repo: Path) -> None:
    policy = SandboxPolicy(sandbox_root=tmp_path)

    with pytest.raises(SandboxRunError, match="timeout_s"):
        run_in_sandbox(policy=policy, source=sample_repo, command=["true"], timeout_s=0)


@pytest.mark.parametrize(
    ("why", "overrides"),
    [
        ("源目录不存在", {"source_name": "does-not-exist"}),
        ("命令为空", {"command": []}),
        ("超时非正数", {"timeout_s": 0}),
    ],
)
def test_no_copy_residue_on_any_rejection_path(
    tmp_path: Path, sample_repo: Path, why: str, overrides: dict
) -> None:
    """**任何**拒绝路径都不得留下 `copy-*` 副本。

    为什么用"参数化 + 断言无残留"而不是"构造某个特定异常"：
    复制阶段的失败由 `copy_workspace` 自己回滚，策略阶段的失败由 `run_in_sandbox` 清理，
    两条路径的**共同可观测不变量**就是"副本目录不残留"。

    第二十四轮修正：原先把 `container_name="pp-x"` 传进来，而 `run_in_sandbox`
    **没有这个形参** → 3/4 组参数在进入函数体前就抛 `TypeError`，
    "无残留"断言其实是空转（假绿）。现在只传真实存在的形参。
    """
    policy = SandboxPolicy(sandbox_root=tmp_path)
    call = {
        "policy": policy,
        "source": tmp_path / overrides.pop("source_name", "repo"),
        **overrides,
    }

    with pytest.raises(Exception):  # noqa: B017 - 拒绝类型由 policy/复制层决定
        run_in_sandbox(**call)

    assert list(tmp_path.glob("copy-*")) == [], f"{why}: 拒绝后不得留下副本目录"


def test_copy_residue_is_cleaned_when_container_cannot_start(
    tmp_path: Path, sample_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**启动失败**（`subprocess.run` 抛 OSError）时也必须清理副本。

    第二十四轮指出该分支零覆盖：此前没有任何用例能让 `subprocess.run` 抛 OSError，
    于是 `except OSError` 里的清理逻辑从未被执行过。
    """
    policy = SandboxPolicy(sandbox_root=tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("docker 可执行文件不存在")

    monkeypatch.setattr("patchpilot.sandbox.manager.subprocess.run", boom)

    with pytest.raises(SandboxRunError, match="无法启动 docker"):
        run_in_sandbox(policy=policy, source=sample_repo, command=["true"])

    assert list(tmp_path.glob("copy-*")) == [], "启动失败后副本也要清理"


def test_copy_is_cleaned_up_after_successful_run(tmp_path: Path, sample_repo: Path) -> None:
    """成功跑完也要清理副本（否则每跑一次留一份仓库）。

    Docker 不可用时用"命令不存在"提前失败——但清理逻辑在 `finally` 风格路径上，
    因此本用例在两种环境下都应通过。
    """
    policy = SandboxPolicy(sandbox_root=tmp_path)
    if not _docker_ready()[0]:
        pytest.skip(f"Docker 不可用: {_docker_ready()[1]}")

    run_in_sandbox(policy=policy, source=sample_repo, command=["sh", "-c", "echo ok"])

    assert list(tmp_path.glob("copy-*")) == []


# ------------------------------------------------------------ 真机（需要 Docker）


@requires_docker
def test_pytest_runs_in_sandbox_and_returns_structured_report(
    tmp_path: Path, sample_repo: Path
) -> None:
    """**P1 完成标志 1**：沙箱内跑 pytest 并结构化返回。"""
    policy = SandboxPolicy(sandbox_root=tmp_path, timeout_s=300)

    result = run_pytest_in_sandbox(policy=policy, source=sample_repo)

    assert result.timed_out is False
    assert result.report is not None, result.report_error or result.stderr[-500:]
    assert result.report.passed == 1
    assert result.report.failed == 0
    assert result.ok is True
    assert not (tmp_path / "repo" / JUNIT_REL_PATH).exists(), "报告应写在副本里，不该污染源仓库"


@requires_docker
def test_failing_test_is_reported_structurally(tmp_path: Path, sample_repo: Path) -> None:
    (sample_repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_bad():\n    assert add(2, 2) == 5\n",
        encoding="utf-8",
    )
    policy = SandboxPolicy(sandbox_root=tmp_path, timeout_s=300)

    result = run_pytest_in_sandbox(policy=policy, source=sample_repo)

    assert result.report is not None
    assert (result.report.passed, result.report.failed) == (1, 1)
    assert result.ok is False
    failed = [c.node_id for c in result.report.cases if c.status == "failed"]
    assert any("test_bad" in node for node in failed)


@requires_docker
def test_container_is_isolated_and_mount_is_the_copy(tmp_path: Path, sample_repo: Path) -> None:
    """隔离不变量：非 root、宿主其他目录不可见、挂载源是副本而不是源仓库。"""
    marker = tmp_path / "HOST_MARKER.txt"
    marker.write_text("host-only\n", encoding="utf-8")
    policy = SandboxPolicy(sandbox_root=tmp_path, timeout_s=300)

    result = run_in_sandbox(
        policy=policy,
        source=sample_repo,
        command=[
            "python",
            "-c",
            f"import os;print('UID', os.getuid());"
            f"print('SEEN' if os.path.exists({str(marker)!r}) else 'NOT-SEEN')",
        ],
    )

    assert "UID 1000" in result.stdout
    assert "NOT-SEEN" in result.stdout
    mount = [token for token in result.argv if token.startswith("type=bind")]
    assert len(mount) == 1
    assert "repo" not in mount[0], "挂载源必须是副本目录，而不是源仓库"


@requires_docker
def test_timeout_kills_container_and_cleans_up(tmp_path: Path) -> None:
    """宿主侧超时必须截断，且容器**不能残留**（实测过的真实风险）。"""
    slow = tmp_path / "slow"
    slow.mkdir()
    (slow / "tests").mkdir()
    (slow / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(120)\n", encoding="utf-8"
    )
    policy = SandboxPolicy(sandbox_root=tmp_path, timeout_s=999)

    started = time.perf_counter()
    result = run_pytest_in_sandbox(policy=policy, source=slow, timeout_s=5)
    elapsed = time.perf_counter() - started

    assert result.timed_out is True
    assert result.exit_code == 124
    assert elapsed < 60
    leftover = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name={result.container_name}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert leftover == "", f"超时后容器残留: {leftover}"
    assert list(tmp_path.glob("copy-*")) == [], "超时后副本也要清理"
