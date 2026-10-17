"""验收项 1 的真机端到端：**真模型 + 真 Docker 沙箱**在本地仓库上完成一次修复。

与 `test_repair_graph.py` 的分工：那边用假模型/假沙箱验证**图逻辑**（离线可跑）；
这边验证**整条链路真的通**：模型产出补丁 → 打到副本 → 容器里跑 pytest → 回归绿。

依赖（缺一则 skip，**不引入网络依赖给门禁**）：
  - `openai` 包（`pip install -e ".[llm]"`）；
  - `.env` 里配好 `PP_LLM_PROVIDER` / `PP_LLM_API_KEY`；
  - Docker 守护进程 + 沙箱镜像（`docker/sandbox/Dockerfile`）。

断言刻意**不要求一定修好**（真实模型的产出本来就非确定），而是断言：
  1. 整条链路跑完并给出结构化结果（不崩、不超预算）；
  2. **用户仓库一个字节都没被改**（补丁只打在副本上）；
  3. 若模型确实交出了可应用补丁，则它必须通过沙箱测试才算 `passed=True`。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from patchpilot.core.config import Settings
from patchpilot.core.llm import build_llm
from patchpilot.graph.repair import RepairOptions, run_repair
from patchpilot.sandbox.manager import docker_available, image_available
from patchpilot.sandbox.policy import DEFAULT_IMAGE, SandboxPolicy

BUGGY = 'def add(a, b):\n    return a - b\n'
FIXED = 'def add(a, b):\n    return a + b\n'
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
ISSUE = (
    "`calc.add(2, 3)` 返回 -1 而不是 5。\n\n"
    "复现：\n```python\nfrom calc import add\nadd(2, 3)  # 期望 5，实际 -1\n```\n\n"
    "看起来 `add` 的实现写错了，请修正它，使 `tests/test_calc.py::test_add` 通过。\n"
)


def _ready() -> tuple[bool, str]:
    settings = Settings()
    if settings.llm_provider == "stub":
        return False, "PP_LLM_PROVIDER=stub（未配置真实模型）"
    if not settings.llm_api_key.get_secret_value():
        return False, "未配置 PP_LLM_API_KEY"
    try:
        import openai  # noqa: F401
    except ImportError:
        return False, '缺少 openai 依赖（pip install -e ".[llm]"）'
    ok, detail = docker_available()
    if not ok:
        return False, detail
    if not image_available(DEFAULT_IMAGE):
        return False, f"本地无沙箱镜像 {DEFAULT_IMAGE}"
    return True, detail


READY, REASON = _ready()

pytestmark = pytest.mark.skipif(not READY, reason=f"真机端到端条件不足: {REASON}")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(TEST, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_real_model_repairs_a_small_repo_end_to_end(tmp_path: Path) -> None:
    """**验收项 1**：一条命令在本地仓库上完成一次修复。"""
    settings = Settings()
    repo = _make_repo(tmp_path)
    issue_file = tmp_path / "bug.md"
    issue_file.write_text(ISSUE, encoding="utf-8")
    sandbox_root = Path(tempfile.mkdtemp(prefix="pp-e2e-"))

    options = RepairOptions(
        repo=repo,
        issue_file=issue_file,
        tests=("tests/test_calc.py",),
        policy=SandboxPolicy(sandbox_root=sandbox_root),
        budget=settings.budget(),
        max_rounds=3,
        copy_root=sandbox_root,
    )

    final = run_repair(options=options, llm=build_llm(settings))

    # 1) 链路必须跑完并给出结构化结果
    assert final.get("task_id"), "必须产出 task_id"
    assert final.get("round", 0) >= 1, "至少要跑一轮"
    assert final.get("candidates"), "必须定位到候选文件（calc.py / test_calc.py）"
    assert not final.get("budget_exceeded"), f"不应触发预算熔断：{final.get('budget_exceeded')}"

    # 2) **用户仓库一个字节都不能变**
    assert (repo / "calc.py").read_text(encoding="utf-8") == BUGGY, "用户仓库被改动了！"
    assert (repo / "tests" / "test_calc.py").read_text(encoding="utf-8") == TEST

    # 3) 若模型给出可应用补丁并跑通测试，则必须真的修对了
    if final.get("passed"):
        assert final["test_summary"]["total"] >= 1
        assert final["test_summary"]["failed"] == 0
        assert Path(final["final_patch_path"]).is_file(), "通过时必须落盘补丁"
        # 把补丁应用到用户仓库的**副本**上验证它真能把 add 修对
        import shutil

        verify = tmp_path / "verify"
        shutil.copytree(repo, verify)
        subprocess.run(
            ["git", "apply", str(final["final_patch_path"])], cwd=verify, check=True
        )
        assert (verify / "calc.py").read_text(encoding="utf-8") == FIXED, (
            "补丁没有把 add 改对"
        )
    else:
        # 没修好也要留下可诊断的信息（补丁 + 记忆），而不是静默失败
        assert final.get("final_patch_path"), "即使未通过也要落盘最后一份补丁"
        assert final.get("memory"), "未通过时必须留下失败记忆"
