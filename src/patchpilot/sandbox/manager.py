"""沙箱薄驱动：把"安全参数怎么拼"（`policy.py`，纯函数）真正跑成一个容器。

设计要点（与 `policy.py` 的分工）：
  - **所有安全语义仍由 `policy.build_run_args` 独占决定**，本模块**不拼任何参数**；
  - 本模块只做四件事：复制工作区到副本、起容器、双通道超时、把结果读回来；
  - 因此"安全性"依然可以在没有 Docker 的环境里被完整验证，本模块的测试只需要覆盖
    "编排是否正确"（真机用例覆盖"隔离是否成立"）。

真机踩过的三个坑（都有实测记录，直接体现在代码里）：
  1. **宿主侧超时后容器仍在跑** → 必须显式 `docker rm -f`，不能只靠 `--rm`；
  2. **结果不能走 stdout 解析**（用户的测试会往 stdout 打任何东西）→ 走 JUnit XML 文件通道；
  3. **JUnit 文件在只读 rootfs 上写不出来** → 容器内写 `/tmp`（tmpfs），
     再由 `docker cp` 取回，或直接让 pytest 写到挂载的 `/workspace` 下的相对路径。
     本实现选择**写到挂载的副本目录**（唯一可持久化的可写位置），随后在宿主侧解析。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.sandbox.junit import JUnitParseError, JUnitReport, parse_junit_xml
from patchpilot.sandbox.policy import (
    SandboxPolicy,
    SandboxPolicyError,
    build_run_args,
    find_escaping_links,
)
from patchpilot.sandbox.workspace import WorkspaceCopyResult, copy_workspace

# 容器内 JUnit 报告的相对路径（相对 `/workspace`，即挂载的副本根）。
# 放在副本里而不是 `/tmp`：`/tmp` 是 tmpfs，容器退出即消失，`docker cp` 也拿不到。
JUNIT_REL_PATH = ".patchpilot-junit.xml"


class SandboxRunError(RuntimeError):
    """沙箱编排层面的失败（复制失败 / Docker 不可用 / 结果不可读）。"""


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """一次沙箱执行的完整结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    container_name: str
    report: JUnitReport | None = None
    report_error: str | None = None
    workspace_copy: WorkspaceCopyResult | None = None
    argv: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """容器正常退出**且**测试全绿。

        直接**委托** `JUnitReport.ok`（它要求 `total > 0` 且 `failed == errors == 0`）：
        第二十四轮指出本属性原先只看 `failed == 0`，比报告本身**弱**——
        探针显示 `errors=1, failed=0, exit=0` 时 `report.ok=False` 而本属性为 True，
        等于"测试报错"被当成成功。委托后两者语义一致，不会再有分歧。
        """
        if self.timed_out or self.exit_code != 0:
            return False
        return self.report.ok if self.report is not None else False


def docker_available() -> tuple[bool, str]:
    """Docker 守护进程是否可用（供测试与 CLI 决定 skip / 报错）。"""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            # 显式 UTF-8：`text=True` 不带 encoding 时按**本地代码页**解码，
            # 输出含非法字节会让 reader 线程抛错并静默得到 `stdout is None`
            # （第三十一轮 cp936 实测：`rc=0` 而 stdout 为 None）。
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker 不可用：{type(exc).__name__}"
    if proc.returncode != 0:
        return False, "docker 守护进程不可达"
    return True, (proc.stdout or "").strip()


def image_available(image: str) -> bool:
    """本地是否已有该镜像（**刻意不自动 pull**，避免给门禁引入网络依赖）。"""
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode == 0


def run_in_sandbox(
    *,
    policy: SandboxPolicy,
    source: Path,
    command: list[str],
    timeout_s: float | None = None,
    keep_workspace: bool = False,
    copy_root: Path | None = None,
) -> SandboxResult:
    """在一份**副本**上跑一次命令，返回结构化结果。

    参数：
      - `source`：待测仓库（**绝不**直接挂载它，只挂复制出来的副本）；
      - `command`：容器内要执行的命令（例如 `["python","-m","pytest","-q"]`）；
      - `timeout_s`：宿主侧超时（默认取 `policy.timeout_s`），到点强制 `docker rm -f`；
      - `keep_workspace`：调试用，保留副本目录以便人工检查。
    """
    effective_timeout = policy.timeout_s if timeout_s is None else timeout_s
    if effective_timeout <= 0:
        raise SandboxRunError("timeout_s 必须为正数")

    # 副本必须放在**声明的沙箱根**之下（policy 的挂载源守卫会校验这一点）。
    base = copy_root or policy.sandbox_root
    if base is None:
        raise SandboxRunError("必须提供 copy_root 或 policy.sandbox_root")
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    destination = base / f"copy-{stamp}"
    try:
        copied = copy_workspace(source, destination)
    except Exception as exc:  # noqa: BLE001 - 统一归一化为 SandboxRunError
        raise SandboxRunError(f"工作区复制失败: {exc}") from exc

    # `copy_workspace` 声称"复制完成后调用方**必须**再跑一次链接逃逸检查"，
    # 而此前**没有任何生产调用点**（只有测试调用）——第二十五轮非阻塞项。
    # 现在真的调用它：它是复制阶段的最后一道网，若副本里仍存在指向外部的链接，
    # 就不该起容器（宁可拒绝任务，也不要把宿主暴露面交给不可信代码）。
    escaping = find_escaping_links(destination)
    if escaping:
        if not keep_workspace:
            shutil.rmtree(destination, ignore_errors=True)
        raise SandboxRunError(f"副本中存在指向外部的链接，拒绝执行: {escaping[:5]}")

    container_name = f"pp-{uuid.uuid4().hex[:10]}"
    # JUnit 报告写到**副本内**（容器里唯一可持久写的位置是挂载目录）。
    # 只有 pytest 命令才追加 `--junitxml`：对任意命令强行追加会把它变成无意义的位置参数。
    junit_host_path = destination / JUNIT_REL_PATH
    junit_container_path = f"{policy.workdir}/{JUNIT_REL_PATH}"
    full_command = list(command)
    if _looks_like_pytest(command):
        full_command.append(f"--junitxml={junit_container_path}")

    try:
        args = build_run_args(
            policy,
            container_name=container_name,
            workspace=destination,
            command=full_command,
        )
    except SandboxPolicyError:
        if not keep_workspace:
            shutil.rmtree(destination, ignore_errors=True)
        raise

    started = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            # 容器里测试输出的编码不可控；不显式指定时在 cp936 上会解码失败，
            # 于是 `rc=0` 但 `stdout` 静默变 None（本轮实测复现）。
            # 判定本身不依赖 stdout（`SandboxResult.ok` 看退出码 + 落盘 JUnit），
            # 所以过去只是"可观测性丢失"，但那是同族缺陷里最难发现的一种。
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
        )
        exit_code = proc.returncode
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        # **必须显式清理**：宿主侧超时后容器仍在运行（实测结论）
        timed_out = True
        exit_code = 124
        stdout = _decode(exc.stdout)
        stderr = _decode(exc.stderr)
        _force_remove(container_name)
    except OSError as exc:
        if not keep_workspace:
            shutil.rmtree(destination, ignore_errors=True)
        raise SandboxRunError(f"无法启动 docker: {type(exc).__name__}: {exc}") from exc

    duration = time.perf_counter() - started

    report: JUnitReport | None = None
    report_error: str | None = None
    if junit_host_path.is_file():
        try:
            report = parse_junit_xml(junit_host_path)
        except JUnitParseError as exc:
            report_error = str(exc)
    else:
        report_error = "容器未产出 JUnit 报告（测试可能未能启动）"

    if not keep_workspace:
        shutil.rmtree(destination, ignore_errors=True)

    return SandboxResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_s=duration,
        container_name=container_name,
        report=report,
        report_error=report_error,
        workspace_copy=copied,
        argv=tuple(args),
    )


def run_pytest_in_sandbox(
    *,
    policy: SandboxPolicy,
    source: Path,
    pytest_args: list[str] | None = None,
    timeout_s: float | None = None,
    keep_workspace: bool = False,
    copy_root: Path | None = None,
) -> SandboxResult:
    """在沙箱里跑 pytest 并返回结构化结果（P1 的完成标志 1）。

    命令固定为 `python -m pytest`（白名单，见 `tools/pytest_runner.py`），
    额外参数只接受调用方显式给出的一小段（由该模块做白名单校验）。
    """
    command = ["python", "-m", "pytest", "-q", *(pytest_args or [])]
    return run_in_sandbox(
        policy=policy,
        source=source,
        command=command,
        timeout_s=timeout_s,
        keep_workspace=keep_workspace,
        copy_root=copy_root,
    )


def _looks_like_pytest(command: list[str]) -> bool:
    """命令是否是 pytest（决定能否追加 `--junitxml`）。"""
    joined = " ".join(command)
    return "pytest" in joined


def _force_remove(container_name: str) -> None:
    """超时后强制删除容器（`--rm` 来不及生效）。失败不抛：清理是尽力而为。"""
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
