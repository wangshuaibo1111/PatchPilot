"""PatchPilot 命令行入口。

命令设计对应 PLAN.md 的「完成标志」：
  pp doctor    环境自检
  pp dev-up    启动依赖服务
  pp dev-down  停止依赖服务
  pp smoke     跑通最小图并输出 trace
  pp check     上传前确定性门禁（与 CI 使用同一套检查）
后续阶段补充：pp index / pp run / pp eval / pp report / pp serve。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from patchpilot import __version__
from patchpilot.core.config import Settings, get_settings
from patchpilot.core.logging import configure_logging
from patchpilot.core.preflight import run_preflight
from patchpilot.core.pricing import is_priced
from patchpilot.graph.minimal import run_minimal_demo

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="PatchPilot - 代码修复 Agent + 评测闭环",
)
console = Console()

REQUIRED_DEPS = {
    "pydantic": "领域模型与配置",
    "pydantic_settings": "配置加载",
    "structlog": "结构化日志",
    "typer": "命令行",
    "rich": "终端输出",
    "langgraph": "状态机编排",
}

OPTIONAL_DEPS = {
    "openai": "P3 接入真实模型",
    "fastapi": "P6 提供 API 服务",
    "uvicorn": "P6 提供 API 服务",
    "psycopg": "Postgres checkpoint",
    "langgraph.checkpoint.sqlite": "SQLite checkpoint",
    "langgraph.checkpoint.postgres": "Postgres checkpoint",
    "qdrant_client": "P2 向量检索",
    "pytest": "测试",
    "ruff": "代码检查",
}


def _docker_daemon_status() -> tuple[bool, str]:
    """检查 Docker 守护进程是否可用，返回 (是否可用, 详情)。"""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except FileNotFoundError:
        return False, "未找到 docker 命令"
    except subprocess.TimeoutExpired:
        return False, "docker info 超时（25s）"
    except OSError as exc:
        return False, f"调用 docker 失败: {exc}"

    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
        return False, lines[0].strip() if lines else f"docker info 退出码 {proc.returncode}"
    return True, f"ServerVersion={proc.stdout.strip()}"


def _run_compose(args: list[str]) -> None:
    ok, detail = _docker_daemon_status()
    if not ok:
        console.print(f"[bold red]Docker 守护进程不可达：[/bold red]{detail}")
        console.print(
            "请先启动 Docker Desktop（Windows）或 dockerd（WSL2）后再重试。\n"
            "提示：pp smoke 与 pp check 不依赖 Docker，可以先跑它们验证骨架。"
        )
        raise typer.Exit(code=1)
    console.print(f"[dim]docker compose {' '.join(args)}[/dim]")
    proc = subprocess.run(["docker", "compose", *args], check=False)
    if proc.returncode != 0:
        raise typer.Exit(code=proc.returncode)


@app.command()
def version() -> None:
    """打印版本。"""
    console.print(f"PatchPilot {__version__}")


@app.command()
def doctor() -> None:
    """环境自检：Python / 依赖 / Docker / 配置 / 价格表。"""
    settings = get_settings()
    configure_logging(settings)

    table = Table(title="PatchPilot 环境自检")
    table.add_column("检查项", style="bold")
    table.add_column("结果")
    table.add_column("说明", style="dim")

    py_version = ".".join(str(v) for v in sys.version_info[:3])
    py_ok = sys.version_info >= (3, 11)
    table.add_row("Python", py_version, "OK" if py_ok else "需要 >= 3.11")

    for module, purpose in REQUIRED_DEPS.items():
        found = importlib.util.find_spec(module) is not None
        table.add_row(
            f"依赖 {module}",
            "[green]已安装[/green]" if found else "[red]缺失[/red]",
            purpose,
        )

    for module, purpose in OPTIONAL_DEPS.items():
        found = importlib.util.find_spec(module) is not None
        table.add_row(
            f"可选 {module}",
            "[green]已安装[/green]" if found else "[yellow]未安装[/yellow]",
            purpose,
        )

    docker_ok, docker_detail = _docker_daemon_status()
    table.add_row(
        "Docker 守护进程",
        "[green]可用[/green]" if docker_ok else "[red]不可用[/red]",
        docker_detail,
    )

    model = settings.llm_model if settings.llm_provider != "stub" else "stub"
    if is_priced(model, settings.price_overrides):
        table.add_row("价格表", "[green]已收录[/green]", f"{model} 可参与金额熔断")
    else:
        table.add_row(
            "价格表",
            "[yellow]未收录[/yellow]",
            f"{model} 成本将标记为未知，金额熔断对该模型不生效；"
            "请用 PP_PRICE_OVERRIDES 配置价格",
        )

    console.print(table)

    console.print("\n[bold]配置摘要[/bold]（不含密钥）")
    for key, value in settings.safe_summary().items():
        console.print(f"  {key} = {value}")

    if not py_ok:
        raise typer.Exit(code=1)


@app.command("dev-up")
def dev_up(
    wait: bool = typer.Option(True, "--wait/--no-wait", help="等待健康检查通过"),
) -> None:
    """启动依赖服务：postgres + redis + qdrant。"""
    args = ["up", "-d"]
    if wait:
        args.append("--wait")
    _run_compose(args)
    console.print("[green]依赖服务已启动[/green]")


@app.command("dev-down")
def dev_down(
    volumes: bool = typer.Option(False, "--volumes", help="同时删除数据卷"),
) -> None:
    """停止依赖服务。"""
    args = ["down"]
    if volumes:
        args.append("-v")
    _run_compose(args)
    console.print("[green]依赖服务已停止[/green]")


@app.command()
def smoke(
    issue: str = typer.Option("", help="覆盖默认的模拟 issue 文本"),
    real_llm: bool = typer.Option(
        False,
        "--real-llm",
        help="使用 .env 配置的真实模型（需要装 patchpilot[llm]，会联网并计费）",
    ),
) -> None:
    """跑通最小 LangGraph 图并输出 trace（**默认无需网络 / Docker / API Key**）。

    第二十一轮非阻塞项：`.env` 里把 `PP_LLM_PROVIDER` 切成 `deepseek` 之后，
    本命令会因缺少 `openai` 依赖直接抛 `RuntimeError`、退出码 1——而 README/PLAN 把
    `pp smoke` 当作 P0 的零依赖门禁。这是**配置影响门禁**的隐患：换 `.env` 就能让门禁变红。

    修法：本命令**默认强制 stub provider**（骨架自测语义），要连真实模型必须显式加
    `--real-llm`。这样 `pp smoke` 的语义不再随 `.env` 漂移。
    """
    settings = get_settings()
    if not real_llm and settings.llm_provider != "stub":
        settings = settings.model_copy(update={"llm_provider": "stub"})
    configure_logging(settings)

    final, trace_path = run_minimal_demo(settings=settings, issue=issue or None)

    prompt_tokens = int(final.get("prompt_tokens", 0) or 0)
    completion_tokens = int(final.get("completion_tokens", 0) or 0)
    budget_exceeded = str(final.get("budget_exceeded", "") or "")

    table = Table(title="smoke 结果")
    table.add_column("字段", style="bold")
    table.add_column("值")
    table.add_row("task_id", str(final.get("task_id", "")))
    table.add_row("attempts", str(final.get("attempts", 0)))
    table.add_row("approved", str(final.get("approved", False)))
    table.add_row("critique", str(final.get("critique", "")))
    table.add_row("budget_exceeded", budget_exceeded or "无")
    used_tokens = prompt_tokens + completion_tokens
    table.add_row("已用 token", f"{prompt_tokens} + {completion_tokens} = {used_tokens}")
    table.add_row("已用成本", f"${float(final.get('cost_usd', 0.0) or 0.0):.6f}")
    table.add_row("预算上限", str(settings.budget()))
    table.add_row("checkpoint 后端", settings.checkpoint_backend)
    table.add_row("trace 文件", str(trace_path))
    console.print(table)

    console.print("\n[bold]执行轨迹（notes）[/bold]")
    for line in final.get("notes", []):
        console.print(f"  · {line}")

    console.print(
        "\n[green]smoke 通过[/green]：状态机 + 回环 + 预算熔断 + checkpoint + trace 全链路可用"
    )


@app.command()
def check(
    root: Path = typer.Option(Path("."), "--root", "-r", help="项目根目录"),
    json_out: bool = typer.Option(False, "--json", help="输出 JSON（供子代理与 CI 解析）"),
    skip_tests: bool = typer.Option(False, "--skip-tests", help="跳过 ruff 与 pytest"),
    basetemp: Path | None = typer.Option(
        None,
        "--basetemp",
        help="pytest 临时目录的**父目录**（每次门禁运行会在其下建独占子目录 run-<pid>-<hex>）",
    ),
    timeout_s: float = typer.Option(900.0, "--timeout", help="单步检查超时（秒）"),
) -> None:
    """上传前确定性门禁：语法 / ruff / pytest / 密钥扫描 / .gitignore 覆盖。

    与 PLAN.md 第 8 节的门禁协议配套：本命令失败时禁止进入审查与上传环节。
    pytest 一项额外给出**执行范围证据**（期望执行的测试文件 vs 实际收集到的），
    使"测试真的被收集"可被审查者核对，而不是只看一个 pass。
    """
    settings = get_settings()
    configure_logging(settings)
    report = run_preflight(
        root,
        run_tests=not skip_tests,
        timeout_s=timeout_s,
        basetemp=basetemp or settings.check_basetemp,
    )

    if json_out:
        # 只往 stdout 打印 JSON，日志走 stderr，保证可被机器解析
        print(report.to_json())
    else:
        table = Table(title=f"上传前门禁 · {report.root}")
        table.add_column("检查项", style="bold")
        table.add_column("结果")
        table.add_column("耗时", justify="right")
        table.add_column("详情", style="dim")

        style = {
            "pass": "[green]通过[/green]",
            "fail": "[red]失败[/red]",
            "skip": "[yellow]跳过[/yellow]",
            # 提示语义：不阻塞上传，但需要人看一眼
            "diff": "[yellow]有差异[/yellow]",
            "info": "[dim]提示[/dim]",
        }
        for item in report.checks:
            detail = item.detail.replace("\n", " ⏎ ")
            if len(detail) > 160:
                detail = detail[:160] + "…"
            table.add_row(
                item.name, style.get(item.status, item.status), f"{item.duration_s:.2f}s", detail
            )
        console.print(table)

        if report.secrets:
            console.print("\n[bold red]疑似凭据泄露（必须先处理）[/bold red]")
            for finding in report.secrets:
                console.print(f"  · {finding}")
        if report.undecodable_files:
            console.print(
                "\n[bold red]无法解码的文件（密钥无法被扫描，按 fail 处理）[/bold red]"
            )
            for finding in report.undecodable_files:
                console.print(f"  · {finding}")
        if report.waived_secrets:
            console.print("\n[bold yellow]已豁免的疑似凭据（需人工确认）[/bold yellow]")
            for finding in report.waived_secrets:
                console.print(f"  · {finding}")
        if report.skipped_large_files:
            console.print("\n[bold yellow]因体积被跳过的文件（未被扫描）[/bold yellow]")
            for finding in report.skipped_large_files:
                console.print(f"  · {finding}")
        if report.skipped_binary_files:
            console.print("\n[bold yellow]二进制文件（未按文本扫描）[/bold yellow]")
            for finding in report.skipped_binary_files:
                console.print(f"  · {finding}")
        if report.pytest_expected_files:
            console.print(
                f"\n[bold]pytest 执行范围证据[/bold]：期望 {len(report.pytest_expected_files)} "
                f"个测试文件，实际收集 {len(report.pytest_collected_files)} 个，"
                f"缺失 {len(report.pytest_missing_files)} 个"
            )
            for missing in report.pytest_missing_files:
                console.print(f"  [red]· 未被执行: {missing}[/red]")
            # 采集期报错也要在人类可读报告里可见（此前只有 --json 才有）
            for error in report.pytest_collect_errors[:5]:
                console.print(f"  [red]· 收集期报错: {error}[/red]")

        console.print(f"\n{report.summary_line()}")
        console.print(
            f"[bold]TREE_DIGEST[/bold] = {report.tree_digest}\n"
            f"[dim]覆盖 {report.file_count} 个文件"
            f"（摘要 / 语法 / ruff / 密钥扫描四者共用同一集合）；"
            f"code-reviewer 的 APPROVE 必须原样引用该值，"
            f"git-publisher 提交前复算比对。[/dim]"
        )

    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def info() -> None:
    """打印当前有效配置（不含密钥），便于排查环境差异。"""
    settings: Settings = get_settings()
    for key, value in settings.safe_summary().items():
        console.print(f"{key} = {value}")


@app.command("mcp")
def mcp_server(
    name: str = typer.Argument(..., help="server 名：fs | git | test | search"),
    root: Path = typer.Option(Path("."), help="只允许该目录之内的访问（默认当前目录）"),
    copy_root: Path | None = typer.Option(
        None, help="test server 复制副本的父目录（默认取沙箱根/临时目录）"
    ),
) -> None:
    """以 **stdio** 方式启动一个 MCP server（供 Agent 客户端接入）。

    安全语义：`root` 是工具层能看到的**全部世界**——根之外的一切对它不存在。
    `test` server 会额外起 Docker 沙箱（需要沙箱镜像，见 `docker/sandbox/Dockerfile`）。
    """
    from patchpilot.sandbox.policy import SandboxPolicy
    from patchpilot.tools import servers

    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter(f"root 必须是已存在的目录: {resolved_root}")
    if name == "test":
        # 副本根默认落到**系统临时目录**下的专用沙箱根，而不是"仓库父目录"：
        # 后者在进程被强杀时会把 `copy-*` 残留在用户的工程目录里（第二十四轮非阻塞项）。
        sandbox_root = (copy_root or Path(tempfile.gettempdir()) / "patchpilot-sandbox").resolve()
        sandbox_root.mkdir(parents=True, exist_ok=True)
        policy = SandboxPolicy(sandbox_root=sandbox_root)
        server = servers.build_test_server(policy, resolved_root, copy_root=sandbox_root)
    elif name == "fs":
        server = servers.build_fs_server(resolved_root)
    elif name == "git":
        server = servers.build_git_server(resolved_root)
    elif name == "search":
        server = servers.build_search_server(resolved_root)
    else:
        raise typer.BadParameter(f"未知 server: {name}；可选: {list(servers.SERVER_NAMES)}")
    server.run()  # stdio 传输


if __name__ == "__main__":  # pragma: no cover
    app()
