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
import json
import pathlib
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
from patchpilot.core.textio import write_text_lf
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
            # 与 `sandbox/manager.py` 同口径：显式 UTF-8 + replace，
            # 否则本地代码页解码失败会让 stdout 静默变成 None。
            text=True,
            encoding="utf-8",
            errors="replace",
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
    return True, f"ServerVersion={(proc.stdout or '').strip()}"


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


@app.command()
def run(
    repo: Path = typer.Option(Path("."), "--repo", help="待修复的本地仓库根"),
    issue_file: Path = typer.Option(..., "--issue-file", help="issue 描述文件（markdown/文本）"),
    tests: list[str] = typer.Option(
        None, "--test", help="目标测试路径（可多次）；不传则跑整个套件"
    ),
    max_rounds: int = typer.Option(3, "--max-rounds", help="最多重试轮数"),
    copy_root: Path | None = typer.Option(
        None, "--copy-root", help="工作副本根（默认系统临时目录）"
    ),
) -> None:
    """**验收项 1**：一条命令在任意本地仓库上完成一次修复。

    流程：读 issue → 定位候选文件 → 模型产出补丁 → 打到**副本**（绝不改用户仓库）
    → 沙箱跑测试 → 失败则带记忆重试 → 产出补丁文件与报告。

    需要 Docker（沙箱测试）与可用模型（`PP_LLM_PROVIDER` + `PP_LLM_API_KEY`）。
    """
    import tempfile

    from patchpilot.core.llm import build_llm
    from patchpilot.graph.repair import RepairOptions, run_repair
    from patchpilot.sandbox.policy import SandboxPolicy

    settings: Settings = get_settings()
    configure_logging(settings)

    resolved_repo = repo.resolve()
    if not resolved_repo.is_dir():
        raise typer.BadParameter(f"--repo 必须是已存在的目录: {resolved_repo}")
    if not issue_file.is_file():
        raise typer.BadParameter(f"--issue-file 不存在: {issue_file}")
    if settings.llm_provider == "stub":
        console.print("[yellow]提示：PP_LLM_PROVIDER=stub，模型不会真的改代码。[/yellow]")

    sandbox_root = (copy_root or Path(tempfile.gettempdir()) / "patchpilot-sandbox").resolve()
    sandbox_root.mkdir(parents=True, exist_ok=True)
    base_policy = SandboxPolicy(sandbox_root=sandbox_root)
    trace_dir = (pathlib.Path(tempfile.gettempdir()) / "patchpilot-traces").resolve()
    options = RepairOptions(
        repo=resolved_repo,
        issue_file=issue_file.resolve(),
        tests=tuple(tests or ()),
        policy=base_policy,
        budget=settings.budget(),
        max_rounds=max_rounds,
        copy_root=sandbox_root,
        trace_dir=trace_dir,
        price_overrides=settings.price_overrides,
    )

    with console.status("修复中…"):
        final = run_repair(options=options, llm=build_llm(settings))

    table = Table(title="pp run 结果")
    table.add_column("字段", style="bold")
    table.add_column("值")
    table.add_row("task_id", str(final.get("task_id", "")))
    table.add_row("轮数", str(final.get("round", 0)))
    table.add_row("候选文件", ", ".join(final.get("candidates", [])[:5]) or "<无>")
    table.add_row("补丁字符数", str(len(final.get("patch", ""))))
    table.add_row("测试通过", "是" if final.get("passed") else "否")
    table.add_row("失败归因", str(final.get("failure_kind", "")))
    table.add_row("预算熔断", str(final.get("budget_exceeded", "")) or "<未触发>")
    table.add_row("补丁文件", str(final.get("final_patch_path", "")) or "<未产出>")
    table.add_row("trace", str(final.get("trace_path", "")) or "<未产出>")
    console.print(table)

    if final.get("budget_exceeded"):
        console.print(f"[yellow]预算终止：{final['budget_exceeded']}[/yellow]")
    if not final.get("passed"):
        raise typer.Exit(code=1)


@app.command("eval")
def eval_command(
    dataset: Path = typer.Option(
        Path("eval/fast_corpus.json"), "--dataset", help="评测集路径（JSON 容器或 JSONL）"
    ),
    limit: int | None = typer.Option(None, "--limit", help="只跑前 N 题（冒烟用）"),
    max_rounds: int = typer.Option(3, "--max-rounds", help="每题最多重试轮数"),
    out: Path | None = typer.Option(None, "--out", help="报告落盘路径（JSON）"),
    work_root: Path | None = typer.Option(None, "--work-root", help="工作目录（默认系统临时目录）"),
    baseline: Path | None = typer.Option(
        None, "--baseline", help="与基线比，退化则退出码 2（回归门禁）"
    ),
    write_baseline_to: Path | None = typer.Option(
        None, "--write-baseline", help="把本次结果写成基线文件"
    ),
    strict_cost: bool = typer.Option(
        False, "--strict-cost", help="成本/延时也阻塞（默认只提示，避免采样噪声误红）"
    ),
) -> None:
    """**验收项 3/4**：跑评测集，输出 pass@1 + 成本 + P95，并落盘可复现报告。

    判定口径：`fail_to_pass` 修复前必须失败、修复后必须通过；`pass_to_pass` 前后都通过。
    修复前就绿的题会被标成 `invalid_task` 并从分母里排除（否则指标没有意义）。

    **回归门禁**（验收项 5）：`--baseline eval/baseline.json` 会与基线逐项比对，
    效果或成本退化时**退出码 2** 并打印指标 diff——这样"改坏 prompt"能让 CI 变红。
    `--write-baseline` 用于在确认合格后刷新基线。
    """
    import tempfile

    from patchpilot.core.llm import build_llm
    from patchpilot.eval.baseline import compare, load_baseline, write_baseline
    from patchpilot.eval.dataset import DatasetError, load_tasks
    from patchpilot.eval.runner import run_eval, write_report

    settings: Settings = get_settings()
    configure_logging(settings)

    try:
        tasks = load_tasks(dataset)
    except DatasetError as exc:
        console.print(f"[red]评测集错误：{exc}[/red]")
        raise typer.Exit(code=2) from exc

    root = (work_root or Path(tempfile.gettempdir()) / "patchpilot-eval").resolve()
    report = run_eval(
        tasks=tasks,
        llm=build_llm(settings),
        dataset_name=dataset.name,
        work_root=root,
        budget=settings.budget(),
        max_rounds=max_rounds,
        model_name=settings.llm_model,
        limit=limit,
    )

    table = Table(title=f"pp eval：{report.dataset}")
    table.add_column("题目", style="bold")
    table.add_column("状态")
    table.add_column("轮数")
    table.add_column("耗时(s)")
    table.add_column("成本($)")
    table.add_column("说明")
    for outcome in report.outcomes:
        style = {"passed": "green", "failed": "red"}.get(outcome.status, "yellow")
        table.add_row(
            outcome.task_id,
            f"[{style}]{outcome.status}[/{style}]",
            str(outcome.rounds),
            f"{outcome.duration_s:.1f}",
            f"{outcome.cost_usd:.4f}",
            outcome.detail[:60],
        )
    console.print(table)

    metrics = Table(title="指标")
    metrics.add_column("指标", style="bold")
    metrics.add_column("值")
    metrics.add_row("总题数 / 有效题数", f"{report.total} / {report.valid}")
    metrics.add_row("通过", str(report.passed))
    metrics.add_row("**pass@1**", f"{report.pass_at_1:.1%}")
    metrics.add_row("总成本 (USD)", f"{report.total_cost_usd:.4f}")
    metrics.add_row("总 token", str(report.total_tokens))
    metrics.add_row("平均耗时 (s)", f"{report.mean_latency():.1f}")
    metrics.add_row("P95 耗时 (s)", f"{report.latency_p95():.1f}")
    metrics.add_row("失败分布", json.dumps(report.failure_histogram(), ensure_ascii=False))
    metrics.add_row(
        "可复现信息",
        f"commit={str(report.meta.get('commit', ''))[:8]} "
        f"温度={report.meta.get('temperature')} 镜像={report.meta.get('image')}",
    )
    console.print(metrics)

    target = out or (Path("eval") / "report.json")
    path = write_report(report, target)
    console.print(f"报告已写入 [bold]{path}[/bold]")

    if write_baseline_to is not None:
        written = write_baseline(report, write_baseline_to)
        console.print(f"基线已写入 [bold]{written}[/bold]")

    if baseline is not None:
        try:
            base_data = load_baseline(baseline)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from exc
        comparison = compare(report, base_data, strict_cost=strict_cost)
        diff = Table(title=f"与基线对比：{baseline}")
        diff.add_column("项目", style="bold")
        diff.add_column("值")
        diff.add_row("结论", "[green]无回归[/green]" if comparison.ok else "[red]检测到回归[/red]")
        for regression in comparison.regressions:
            diff.add_row(
                regression.metric,
                f"[red]{regression.baseline} → {regression.current}[/red]  {regression.detail}",
            )
        for note in comparison.notes:
            diff.add_row("提示", note)
        console.print(diff)
        if not comparison.ok:
            raise typer.Exit(code=2)


@app.command()
def trace(
    limit: int = typer.Option(5, "--limit", help="显示最近 N 条 trace"),
    trace_dir: Path | None = typer.Option(None, "--trace-dir", help="trace 目录"),
    verbose: bool = typer.Option(False, "--verbose", help="展开每个 span 的耗时"),
) -> None:
    """**验收项 6**：回放最近的任务 trace（LLM / 工具 / 检索 / 成本）。

    trace 是 JSONL，一天一个文件；这里读最近的若干条并展开 span，
    用于回答"这个任务花了多少钱、哪一步慢、检索命中几个文件"。
    """
    import tempfile

    directory = (
        trace_dir or pathlib.Path(tempfile.gettempdir()) / "patchpilot-traces"
    ).resolve()
    if not directory.is_dir():
        console.print(f"[yellow]暂无 trace 目录：{directory}[/yellow]")
        raise typer.Exit(code=1)

    records: list[dict] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        console.print(f"[yellow]{directory} 下没有可读的 trace[/yellow]")
        raise typer.Exit(code=1)

    for record in records[-limit:]:
        table = Table(title=f"trace {record.get('trace_id', '')} · {record.get('name', '')}")
        table.add_column("字段", style="bold")
        table.add_column("值")
        table.add_row("状态", str(record.get("status", "")))
        table.add_row("任务", str(record.get("task_id", "")) or "<无>")
        table.add_row("通过", "是" if record.get("passed") else "否")
        table.add_row("轮数", str(record.get("rounds", "")))
        table.add_row("候选文件", str(record.get("candidates", "")))
        table.add_row("耗时 (ms)", f"{float(record.get('duration_ms', 0.0)):.1f}")
        table.add_row(
            "token",
            f"{record.get('prompt_tokens', 0)} + {record.get('completion_tokens', 0)}",
        )
        cost = float(record.get("cost_usd", 0.0))
        unknown = record.get("cost_unknown")
        suffix = "（**含未定价模型，成本不可信**）" if unknown else ""
        table.add_row("成本 (USD)", f"{cost:.6f}{suffix}")
        table.add_row("模型", ", ".join(record.get("models") or []) or "<无>")
        spans = record.get("spans") or []
        by_name: dict[str, int] = {}
        for span in spans:
            by_name[span["name"]] = by_name.get(span["name"], 0) + 1
        table.add_row(
            "span",
            ", ".join(f"{k}×{v}" for k, v in sorted(by_name.items())) or "<无>",
        )
        console.print(table)
        if verbose:
            for span in spans:
                attrs = span.get("attributes") or {}
                console.print(
                    f"  · {span['name']:<22} "
                    f"{float(span.get('duration_ms', 0.0)):>8.2f}ms  "
                    f"{json.dumps(attrs, ensure_ascii=False)}"
                )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="绑定地址"),
    port: int = typer.Option(8000, "--port", help="端口"),
    trace_dir: Path | None = typer.Option(None, "--trace-dir", help="trace 目录"),
    eval_report: Path | None = typer.Option(
        None, "--eval-report", help="评测报告 JSON（默认 eval/report.json）"
    ),
) -> None:
    """**验收项 7**：起 Web 面板，在浏览器里回放任务轨迹、查看评测指标。

    只读面板：不提供任何执行入口（避免把"远程执行不可信代码"暴露到 Web 上）。
    `docker compose up` 会以本命令启动面板服务。
    """
    import tempfile

    from patchpilot.api.app import create_app

    directory = (
        trace_dir or pathlib.Path(tempfile.gettempdir()) / "patchpilot-traces"
    ).resolve()
    report = (eval_report or (Path("eval") / "report.json")).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的友好提示
        console.print(
            '[red]缺少 server 依赖：pip install -e ".[server]"[/red]'
        )
        raise typer.Exit(code=1) from exc

    console.print(f"面板地址 [bold]http://{host}:{port}[/bold]")
    console.print(f"  trace 目录: {directory}")
    console.print(f"  评测报告  : {report}（{'存在' if report.is_file() else '暂缺'}）")
    uvicorn.run(create_app(trace_dir=directory, eval_report=report), host=host, port=port)


@app.command()
def locate(
    scope: Path = typer.Option(
        Path("src/patchpilot"), "--scope", help="索引范围（相对 git 根）"
    ),
    corpus: Path | None = typer.Option(
        None, "--corpus", help="语料 JSON（默认 eval/fast_corpus.json）"
    ),
    synthetic: Path | None = typer.Option(
        None, "--synthetic", help="合成语料 JSON（默认 eval/locate_corpus.json）"
    ),
    history: int = typer.Option(60, "--history", help="从提交历史取多少条提交流程作题"),
    git_root: Path = typer.Option(Path("."), "--git-root", help="git 仓库根"),
    out: Path | None = typer.Option(None, "--out", help="把消融表写成 JSON"),
) -> None:
    """**P2 验收**：定位基准 + 消融表（Top-1 / Top-5 / MRR）。

    两组题目分开报告，**不混成一个数**：
      - `corpus`：语料 issue 集（自然语言，用户风格，样本少）；
      - `history`：把提交信息当 issue、改动文件当期望（真实代码，但信息偏乐观）。
    """
    import json

    from patchpilot.eval.locate_bench import run_locate_bench

    resolved_root = git_root.resolve()
    resolved_scope = (resolved_root / scope).resolve()
    if not resolved_scope.is_dir():
        raise typer.BadParameter(f"--scope 不是目录: {resolved_scope}")
    corpus_path = corpus or Path("eval/fast_corpus.json")
    synthetic_path = synthetic or Path("eval/locate_corpus.json")

    with console.status("建索引并跑消融…"):
        outcome = run_locate_bench(
            git_root=resolved_root,
            scope=resolved_scope,
            corpus_path=corpus_path if corpus_path.is_file() else None,
            synthetic_path=synthetic_path if synthetic_path.is_file() else None,
            history_limit=history,
        )

    table = Table(title="P2 定位基准（消融表）")
    table.add_column("配置", style="bold")
    table.add_column("题目组")
    table.add_column("题数", justify="right")
    table.add_column("Top-1", justify="right")
    table.add_column("Top-5", justify="right")
    table.add_column("MRR", justify="right")
    for row in outcome.table():
        table.add_row(*[str(cell) for cell in row])
    console.print(table)

    # 相对基线的提升（看"all"汇总行，比单组更有意义）
    by_key = {(score.config, score.group): score for score in outcome.scores}
    baseline_all = by_key.get(("基线（P1 现有 locate：符号+子串）", "all"))
    full_key = next((k for k in by_key if "四路" in k[0] and k[1] == "all"), None)
    if baseline_all is not None and full_key is not None:
        full_all = by_key[full_key]
        console.print(
            f"[bold]全部 {full_all.total} 题汇总[/bold]：Top-1 "
            f"{baseline_all.top1:.1%} → [green]{full_all.top1:.1%}[/green]"
            f"（+{(full_all.top1 - baseline_all.top1) * 100:.1f}pp），"
            f"Top-5 {baseline_all.top5:.1%} → {full_all.top5:.1%}，"
            f"MRR {baseline_all.mrr:.3f} → {full_all.mrr:.3f}"
        )
    console.print(
        "[dim]注：三组来源不同，**不要混谈**——"
        "corpus 是自然语言 issue（样本少、天花板）；synthetic 是合成代码"
        "（easy 层点名函数、hard 层只描述行为）；"
        "history 的提交信息常直接点名函数（偏乐观）。"
        "单组 1 题 = 5~17pp，故看汇总行更稳。[/dim]"
    )
    if out is not None:
        payload = [
            {
                "config": score.config,
                "group": score.group,
                "total": score.total,
                "top1": round(score.top1, 4),
                "top5": round(score.top5, 4),
                "mrr": round(score.mrr, 4),
                "misses": list(score.misses),
            }
            for score in outcome.scores
        ]
        write_text_lf(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        console.print(f"消融表已写入 [bold]{out}[/bold]")


if __name__ == "__main__":  # pragma: no cover
    app()
