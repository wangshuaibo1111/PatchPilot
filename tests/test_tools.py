"""工具层单测（fs / git / test / search）——**不需要 Docker，也不需要 MCP 依赖**。

这 4 个 server 的安全语义是"白名单 + 根之内"，因此可以完全离线穷举验证。
真机部分（沙箱里真跑 pytest）见 `test_sandbox_manager.py` 的 Docker 用例。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.tools import ToolError, fs_server, git_server, pytest_runner, search_server


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(
        "def target_fn():\n    return 1\n\n\nclass Widget:\n    pass\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- fs：路径白名单


def test_fs_read_and_list(repo: Path) -> None:
    assert fs_server.read_file(repo, "pkg/mod.py").startswith("def target_fn()")
    assert fs_server.list_directory(repo) == ["README.md", "pkg/"]


@pytest.mark.parametrize(
    "hostile",
    [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "C:/Windows/system32",
        "pkg/../../outside.txt",
        "~/secret",
    ],
)
def test_fs_rejects_escapes(repo: Path, hostile: str) -> None:
    """逃逸路径必须被拒——这是工具层最核心的不变量。"""
    with pytest.raises(ToolError):
        fs_server.read_file(repo, hostile)
    with pytest.raises(ToolError):
        fs_server.write_file(repo, hostile, "x")


def test_fs_rejects_symlink_escape(repo: Path, tmp_path: Path) -> None:
    """根内的符号链接指向根外 → 必须拒绝（`resolve_within` 用 `resolve()` 归一化）。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("本平台不允许创建符号链接（同一逻辑由 junction / 逃逸路径用例覆盖）")

    with pytest.raises(ToolError):
        fs_server.read_file(repo, "link.txt")


def test_fs_write_is_confined_and_rejects_directories(repo: Path) -> None:
    assert fs_server.write_file(repo, "pkg/new.py", "VALUE = 1\n") == "pkg/new.py"
    assert (repo / "pkg" / "new.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    with pytest.raises(ToolError):
        fs_server.write_file(repo, "pkg", "x")


def test_fs_read_rejects_non_utf8_and_oversized(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ToolError):
        fs_server.read_file(repo, "blob.bin")

    (repo / "big.txt").write_text("x" * 100, encoding="utf-8")

    # `read_file` 的公开签名不带 max_bytes，故用底层工具函数验证体积上限
    from patchpilot.tools import read_text_file

    with pytest.raises(ToolError):
        read_text_file(repo, "big.txt", max_bytes=10)


# ---------------------------------------------------------------- git：两层白名单


@pytest.fixture()
def git_repo(repo: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_git_status_log_diff(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    assert "README.md" in git_server.status(git_repo)
    assert "init" in git_server.log(git_repo, 5)
    assert "README.md" in git_server.diff(git_repo, ["--name-only"])


def test_numstat_names_are_unquoted_to_real_paths() -> None:
    """**解析规则直测**：git 引号化的名字必须还原成真实路径。

    为什么需要这条（第二十九轮）：`core.quotePath=false` 时 git 通常直接输出原始路径，
    所以端到端用例几乎碰不到"引号化"这一支，于是"清单道不做 C 反转义"的变异会**存活**。
    但这一支必须正确——一旦 git 配置被改动或遇到控制字符名，git 就会输出
    `"J\\301\\264/victim.txt"`，不做反转义就等于拿一个**不存在的路径**去核对边界，
    第二道网"假装在岗"（静态道能拦，故当时不逃逸，但独立性不成立）。

    这里直接喂 git 的真实输出格式，把解析规则钉死。
    """
    # git 对非 ASCII 名输出的**UTF-8** 八进制转义形态（「链」= E9 93 BE = \351\223\276）
    output = '1\t0\t"J\\351\\223\\276/victim.txt"\n create "J\\351\\223\\276/victim.txt"\n'

    names = git_server._names_from_numstat(output)

    assert names is not None, "可解析的输出不得返回 None"
    assert "J链/victim.txt" in names, f"必须还原成真实路径，实际 {names}"
    assert not any("\\351" in n for n in names), "不得残留八进制转义"


def test_numstat_names_fail_closed_on_undecodable_quoting() -> None:
    """无法还原的引号名 → `None`（无法验证即拒绝），不能当成"没有文件"。"""
    output = '1\t0\t"bad\\qname.txt"\n'

    assert git_server._names_from_numstat(output) is None


def test_numstat_names_handle_plain_and_summary_lines() -> None:
    """普通 numstat 行与 `--summary` 行（rename/copy/create/delete）都要能解析。"""
    output = (
        "1\t1\ta.txt\n"
        "0\t0\trenamed.txt\n"
        " rename a.txt => renamed.txt (100%)\n"
        " create new.txt\n"
        " delete gone.txt\n"
    )

    names = git_server._names_from_numstat(output)

    assert names is not None
    assert "a.txt" in names
    assert "renamed.txt" in names
    assert "new.txt" in names
    assert "gone.txt" in names


def test_git_subprocesses_always_specify_utf8_encoding(git_repo: Path) -> None:
    """**结构化断言**：两处 `subprocess.run` 都必须显式传 `encoding="utf-8"`。

    为什么不写成行为断言：git 在 `core.quotePath=false` 时直接把路径以原始 UTF-8 输出，
    而 Windows 上 git 自己可能先转成本地代码页——于是"不传 encoding 会不会出错"
    **取决于环境**（本机 cp936 下恰好能解码，复审者的机器上则 `stdout is None`）。
    行为断言因此在某些机器上会假绿。

    但**契约是确定的**：跨平台读 git 的路径输出必须显式 UTF-8。
    所以这里直接钉住调用点，任何删掉 `encoding` 的改动都会变红。
    """
    import inspect

    import patchpilot.tools.git_server as module

    sources = {
        "_run_git": inspect.getsource(module._run_git),
        "_files_git_would_write": inspect.getsource(module._files_git_would_write),
    }
    for name, source in sources.items():
        assert 'encoding="utf-8"' in source, f"{name} 的 subprocess 调用缺少显式 UTF-8 编码"
        assert '"core.quotePath=false"' in source, (
            f"{name} 未关闭 quotePath：非 ASCII 路径会被 git 输出成八进制转义形式，"
            "工具层调用方拿到就不是真实路径"
        )
    assert git_repo  # 保持 fixture 使用（保证在真实仓库上下文里跑）


def test_git_status_and_diff_handle_non_ascii_paths(git_repo: Path) -> None:
    """`status` / `diff` 在**非 ASCII 文件名**下必须返回真实路径，而不是乱码或崩溃。

    第二十九轮：`_run_git` 原先 `text=True` 不带 encoding，git 输出的 UTF-8 路径在
    cp936 下解码失败（reader 线程抛 `UnicodeDecodeError`），于是 status/diff/log 的
    输出要么缺失、要么是乱码——而工具层会把这些乱码当成真实路径去用。
    这条用例在 cp936 环境下能钉住该缺陷（纯 ASCII 路径测不出来）。
    """
    (git_repo / "文.txt").write_text("HELLO\n", encoding="utf-8")

    status_out = git_server.status(git_repo)

    # `git diff` 只显示**已跟踪**的改动，所以先入索引再改内容
    subprocess.run(["git", "add", "文.txt"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "文.txt").write_text("HELLO-CHANGED\n", encoding="utf-8")
    diff_out = git_server.diff(git_repo, ["--name-only"])

    assert "文.txt" in status_out, f"status 必须含真实文件名，实际 {status_out!r}"
    assert "文.txt" in diff_out, f"diff 必须含真实文件名，实际 {diff_out!r}"


@pytest.mark.parametrize(
    "subcommand",
    ["push", "remote", "fetch", "pull", "clone", "config", "submodule", "clean", "reset"],
)
def test_git_forbids_network_and_history_rewriting(subcommand: str) -> None:
    """禁止清单是**硬编码**的：remote/push/网络与历史改写一律不在白名单。"""
    with pytest.raises(ToolError):
        git_server._validate(subcommand, [])


@pytest.mark.parametrize(
    "args",
    [
        ["--no-index", "/etc/passwd", "x"],  # 读根之外的文件
        ["--output=/tmp/x"],  # 任意写
        ["--upload-pack=/bin/sh"],  # 触发外部程序
        ["--ext-diff"],
        ["--src-prefix=../../"],
    ],
)
def test_git_forbids_dangerous_flags(args: list[str]) -> None:
    with pytest.raises(ToolError):
        git_server._validate("diff", args)


def test_git_apply_patch_confined(git_repo: Path) -> None:
    patch = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # demo\n"
        "+new line\n"
    )
    git_server.apply_patch(git_repo, patch, check_only=True)  # 先校验
    git_server.apply_patch(git_repo, patch)
    assert "new line" in (git_repo / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "patch",
    [
        "--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-x\n+y\n",
        "diff --git a/../../outside b/../../outside\n--- a/../../outside\n+++ b/../../outside\n",
        "--- a/C:/Windows/x\n+++ b/C:/Windows/x\n",
    ],
)
def test_git_apply_patch_rejects_outside_paths(git_repo: Path, patch: str) -> None:
    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, patch)


def test_git_apply_patch_cannot_write_through_a_directory_link(
    git_repo: Path, tmp_path: Path
) -> None:
    """**链接写穿**必须被拒：junction 的 `is_symlink()` 是 False，纯文本校验看不见它。

    第二十五轮真机逃逸：`git apply` 会**穿过 junction** 写文件。实测三形态都能改写/新建
    根之外的宿主文件（① 改既有 ② 新建 ③ junction 套 junction 两跳），
    而且**协议层可达**（`pp mcp git --root` 调 `apply_patch` 返回 ok）——
    与本模块 `search` 上一轮的漏洞同形。

    这里用 junction 真实验证；平台建不出链接才 skip。
    """
    import os as _os

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL-HOST-CONTENT\n", encoding="utf-8")

    link = git_repo / "linked"
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("本平台无法创建 junction/symlink（无法验证写穿防护）")

    rewrite = (
        "--- a/linked/victim.txt\n+++ b/linked/victim.txt\n@@ -1 +1 @@\n"
        "-ORIGINAL-HOST-CONTENT\n+OVERWRITTEN-BY-AGENT\n"
    )
    create = "--- /dev/null\n+++ b/linked/newfile.txt\n@@ -0,0 +1 @@\n+NEW\n"

    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, rewrite)
    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, create)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL-HOST-CONTENT\n", "宿主文件不得被改写"
    assert not (outside / "newfile.txt").exists(), "根外不得出现新文件"

    # 对照：根内补丁仍须正常工作（防误伤）
    (git_repo / "inside.txt").write_text("INSIDE-ORIGINAL\n", encoding="utf-8")
    git_server.apply_patch(
        git_repo,
        "--- a/inside.txt\n+++ b/inside.txt\n@@ -1 +1 @@\n-INSIDE-ORIGINAL\n+INSIDE-PATCHED\n",
    )
    assert (git_repo / "inside.txt").read_text(encoding="utf-8").strip() == "INSIDE-PATCHED"


# `git apply` 会**真的执行**的指令行；漏掉任何一类，解析道就对那一类整体失效。
# 这张表是**硬编码锚点**：将来 git 新增声明行时，元测试会要求同步。
# 断言用**精确等值**（第二十七轮：子串断言对"前缀没剥干净"完全钝感）。
PATCH_DECLARATION_LINES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "diff --git",
        "diff --git a/src.txt b/dst.txt\n",
        ("a/src.txt", "src.txt", "b/dst.txt", "dst.txt"),
    ),
    ("old file", "--- a/src.txt\n", ("a/src.txt", "src.txt")),
    ("new file", "+++ b/dst.txt\n", ("b/dst.txt", "dst.txt")),
    ("rename from", "rename from src.txt\n", ("src.txt",)),
    ("rename to", "rename to dst.txt\n", ("dst.txt",)),
    ("copy from", "copy from src.txt\n", ("src.txt",)),
    ("copy to", "copy to dst.txt\n", ("dst.txt",)),
    ("edit", "edit src.txt\n", ("src.txt",)),
    # 引号形态：git 在 core.quotePath=true（默认）时就会这么输出非 ASCII 名
    ("quoted rename to", 'rename to "dst.txt"\n', ("dst.txt",)),
    ("quoted new file", '+++ "b/dst.txt"\n', ("b/dst.txt", "dst.txt")),
    # 非 a/b 前缀：`-p1` 剥的是"第一段分量"，不管它叫什么
    ("non-ab old file", "--- x/src.txt\n", ("src.txt", "x/src.txt")),
    ("non-ab new file", "+++ y/dst.txt\n", ("dst.txt", "y/dst.txt")),
)


@pytest.mark.parametrize(("label", "line", "expected"), PATCH_DECLARATION_LINES)
def test_patch_paths_parses_every_declaration_line(
    label: str, line: str, expected: tuple[str, ...]
) -> None:
    """**元测试**：每类声明行都必须被解析成**预期的路径集合**。

    第二十六轮 BLOCKING 的根因是"漏了一类声明行"（`rename to`/`copy to` 没解析 →
    `git apply` 老实执行 → 文件写到根外）；第二十七轮又发现"前缀没剥干净"与"引号名"
    两类。这条用**精确等值**断言，让任何一类再出问题都必然变红。

    注意 `-p1` 语义：`x/src.txt` 会同时产出 `x/src.txt`（原始）与 `src.txt`（剥一段），
    因为"git 到底剥哪一段"由 git 决定，我们把所有解释都当候选。
    """
    parsed = git_server._patch_paths(line)

    assert set(parsed) == set(expected), f"{label}: 期望 {expected}，实际 {parsed}"


@pytest.mark.parametrize(
    ("label", "patch"),
    [
        (
            "引号 rename to 越界",
            'diff --git a/src.txt b/src.txt\nsimilarity index 100%\n'
            'rename from src.txt\nrename to "linked/ESCAPED.txt"\n',
        ),
        (
            "引号 ---/+++ 越界",
            '--- "a/linked/victim.txt"\n+++ "b/linked/victim.txt"\n@@ -1 +1 @@\n'
            "-ORIGINAL-HOST-CONTENT\n+OVERWRITTEN-QUOTED\n",
        ),
        (
            "非 a/b 前缀 x/ 越界",
            "--- x/linked/victim.txt\n+++ x/linked/victim.txt\n@@ -1 +1 @@\n"
            "-ORIGINAL-HOST-CONTENT\n+OVERWRITTEN-BY-X-PREFIX\n",
        ),
        (
            "非 a/b 前缀且新旧不同",
            "--- x/linked/victim.txt\n+++ y/linked/victim.txt\n@@ -1 +1 @@\n"
            "-ORIGINAL-HOST-CONTENT\n+OVERWRITTEN-XY\n",
        ),
        (
            "非 a/b 前缀新建",
            "--- /dev/null\n+++ x/linked/ESCAPED.txt\n@@ -0,0 +1 @@\n+NEW\n",
        ),
        (
            "制表符分隔（可疑行 fail-closed）",
            "diff --git a/src.txt b/src.txt\nrename to\tlinked/ESCAPED.txt\n",
        ),
        (
            "行首空格（可疑行 fail-closed）",
            "diff --git a/src.txt b/src.txt\n rename to linked/ESCAPED.txt\n",
        ),
        ("未知转义 fail-closed", '--- "a/linked/\\qbad.txt"\n+++ "b/linked/\\qbad.txt"\n'),
    ],
)
def test_git_apply_patch_rejects_every_escape_form(
    git_repo: Path, tmp_path: Path, label: str, patch: str
) -> None:
    """越界形态矩阵必须**全部拒绝**且根外字节不变。

    第二十七轮审查者用 21 例矩阵证明：只修"解析哪些行"是不够的——
    **候选路径与 git 实际落盘路径不一致**就会放行。这里覆盖引号名、八进制转义、
    非 `a/`/`b/` 前缀、以及"形态可疑→fail-closed"。
    """
    import os as _os

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL-HOST-CONTENT\n", encoding="utf-8")

    link = git_repo / "linked"
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("本平台无法创建 junction/symlink（无法验证越界防护）")

    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, patch)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL-HOST-CONTENT\n", (
        f"{label}: 根外文件不得被改写"
    )
    assert not (outside / "ESCAPED.txt").exists(), f"{label}: 根外不得新建文件"


def test_git_apply_patch_allows_legitimate_in_root_patches(git_repo: Path) -> None:
    """**防误伤**：根内的改写 / 新建 / rename 必须仍然成功。

    只做"拒绝"断言容易把校验写成"一律拒绝"——那是把功能改坏了。
    """
    (git_repo / "a.txt").write_text("A\n", encoding="utf-8")

    git_server.apply_patch(
        git_repo, "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-A\n+A2\n"
    )
    assert (git_repo / "a.txt").read_text(encoding="utf-8").strip() == "A2"

    git_server.apply_patch(git_repo, "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1 @@\n+B\n")
    assert (git_repo / "b.txt").exists()

    git_server.apply_patch(
        git_repo,
        "diff --git a/b.txt b/c.txt\nsimilarity index 100%\n"
        "rename from b.txt\nrename to c.txt\n",
    )
    assert (git_repo / "c.txt").exists()


# ---------------------------------------------------------------- fail-closed（第二十八轮）


def test_git_cannot_parse_patch_is_rejected(git_repo: Path) -> None:
    """`git --numstat` 解析不了的补丁必须**拒绝**，而不是"让后面的 apply 去报错"。

    这是第二十八轮登记的残余风险：早先 `_files_git_would_write` 在 git 解析失败时
    返回**空列表**，于是"git 说没有文件要写"和"git 根本解析不了"混为一谈，
    静态校验（`_patch_touches_outside`）成了唯一防线——
    而静态道已被引号名 / 非 `a/b` 前缀绕过过两轮。

    现在两者严格区分：`None`（解析失败）→ 必须拒绝。
    """
    import patchpilot.tools.git_server as module

    original = module._files_git_would_write
    try:
        module._files_git_would_write = lambda *a, **k: None  # type: ignore[assignment]
        with pytest.raises(ToolError) as excinfo:
            git_server.apply_patch(
                git_repo, "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-OLD\n+NEW\n"
            )
        assert "无法让 git 枚举" in str(excinfo.value)
    finally:
        module._files_git_would_write = original  # type: ignore[assignment]


def test_git_reports_no_files_but_patch_declares_paths_is_rejected(git_repo: Path) -> None:
    """git 解析成功却说"不写任何文件"，而补丁里明明声明了路径 → 自相矛盾，必须拒绝。

    这种"两侧不一致"是最危险的状态：放行等于在未经验证的情况下落盘。
    """
    import patchpilot.tools.git_server as module

    original = module._files_git_would_write
    try:
        module._files_git_would_write = lambda *a, **k: []  # type: ignore[assignment]
        with pytest.raises(ToolError) as excinfo:
            git_server.apply_patch(
                git_repo, "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-OLD\n+NEW\n"
            )
        assert "不写任何文件" in str(excinfo.value)
    finally:
        module._files_git_would_write = original  # type: ignore[assignment]


def test_files_git_would_write_distinguishes_failure_from_empty(git_repo: Path) -> None:
    """两个返回值语义必须不同：`None` = 解析失败；`[]` = 成功且无文件。"""
    import os

    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(git_repo),
        "PATH": os.environ.get("PATH", ""),
    }

    good = git_server._files_git_would_write(
        git_repo, "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-OLD\n+NEW\n", env
    )
    assert good == ["a.txt"], f"正常补丁应枚举出文件，实际 {good}"

    bad = git_server._files_git_would_write(git_repo, "这不是补丁\n", env)
    assert bad is None, f"git 解析失败必须返回 None（而不是空列表），实际 {bad}"
    assert bad != [], "None 与 [] 必须可区分——这正是本轮的修复点"


def test_files_git_would_write_treats_missing_stdout_as_unverifiable(git_repo: Path) -> None:
    """`stdout is None`（解码失败等）必须判为**无法枚举** → `None`。

    第二十九轮真机踩到：`text=True` 不带 encoding 时按本地代码页（cp936）解码 git 的
    UTF-8 输出，reader 线程抛 `UnicodeDecodeError`，于是 **rc=0 但 stdout 不可用**。
    旧代码用 `(proc.stdout or "")` 把它当成空清单 → "无法验证"被静默当成"已验证"，
    进而由守卫 3 误拒**合法**补丁（错误信息还是"不写任何文件"，完全误导）。
    """
    import subprocess as sp

    import patchpilot.tools.git_server as module

    class _FakeCompleted:
        returncode = 0
        stdout = None
        stderr = None

    original = module.subprocess.run
    try:
        module.subprocess.run = lambda *a, **k: _FakeCompleted()  # type: ignore[assignment]
        result = module._files_git_would_write(git_repo, "diff --git a/x b/x\n", {})
    finally:
        module.subprocess.run = original  # type: ignore[assignment]

    assert result is None, f"stdout 不可用时必须返回 None，实际 {result}"
    assert sp  # 保持导入可见


def test_files_git_would_write_unquotes_c_style_names(git_repo: Path) -> None:
    """git 引号化的名字必须**还原成真实路径**，否则清单道核对的是不存在的路径。

    `core.quotePath=true`（默认）时 git 把非 ASCII 名输出成 `"\\346\\226\\207.txt"`
    这种八进制转义形式。只 `strip('"')` 会得到乱码路径 →
    清单道对非 ASCII 名的 junction 完全失效（静态道能拦，第二道网却"假装在岗"）。
    """
    import os

    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(git_repo),
        "PATH": os.environ.get("PATH", ""),
    }
    patch = "--- /dev/null\n+++ b/文.txt\n@@ -0,0 +1 @@\n+HELLO\n"

    listed = git_server._files_git_would_write(git_repo, patch, env)

    assert listed == ["文.txt"], f"必须还原成真实路径 ['文.txt']，实际 {listed}"


def test_non_ascii_legitimate_patch_applies(git_repo: Path) -> None:
    """**防误伤**：新建非 ASCII 文件名必须能正常应用（本轮修复的正面用例）。

    修复前在本机（cp936）这条会被守卫 3 误拒，报"git 报告该补丁不写任何文件"。
    """
    patch = "--- /dev/null\n+++ b/文.txt\n@@ -0,0 +1 @@\n+HELLO\n"

    git_server.apply_patch(git_repo, patch)

    assert (git_repo / "文.txt").read_text(encoding="utf-8").strip() == "HELLO"


@pytest.mark.parametrize(
    ("link_name", "label"),
    [
        ("linked", "ASCII 链接名"),
        ("J链", "非 ASCII 链接名（git 会把它引号化 + 八进制转义）"),
    ],
)
def test_git_list_layer_holds_even_when_static_layer_is_blind(
    git_repo: Path, tmp_path: Path, link_name: str, label: str
) -> None:
    """**深防御分层验证**：把静态道人为致盲后，git 清单道必须独立挡住 junction 写穿。

    为什么需要这条（§8.5 第 5 条）：实测把"逐文件解析级校验"删掉后**全套测试仍绿**
    ——因为静态道（`_patch_touches_outside`）也拦得住同样的形态，两层重叠。
    重叠本身是好事，但"重叠"不等于"第二层有用"：一旦静态道被将来的某种新形态绕过，
    第二层必须真的在岗。

    因此这里**显式把静态道置为致盲**（`return None`），只留 git 清单道，
    验证它仍能拒绝写穿。这样任何删掉第二层的改动都会让本用例变红。

    第二十九轮补充：**参数化非 ASCII 链接名**。此前只用 ASCII 名，
    于是"git 清单道不做 C 反转义"这个盲区永远不会变红
    （`J链` 会被 git 输出成 `"J\301\264/…"`，不做反转义就核对了一个不存在的路径）。
    """
    import os as _os

    import patchpilot.tools.git_server as module

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    link = git_repo / link_name
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip(f"本平台无法创建 junction/symlink（{label}）")

    original_static = module._patch_touches_outside
    try:
        # 静态道致盲：假装它什么也没发现
        module._patch_touches_outside = lambda *a, **k: None  # type: ignore[assignment]
        with pytest.raises(ToolError) as excinfo:
            git_server.apply_patch(
                git_repo,
                f"--- a/{link_name}/victim.txt\n+++ b/{link_name}/victim.txt\n"
                "@@ -1 +1 @@\n-ORIGINAL\n+OVERWRITTEN\n",
            )
        message = str(excinfo.value)
        assert "根之外" in message, f"{label}: 应由 git 清单道拒绝，实际: {message}"
    finally:
        module._patch_touches_outside = original_static  # type: ignore[assignment]

    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n", f"{label}: 根外文件不得被改写"


def test_git_apply_patch_rejects_rename_and_copy_through_a_link(
    git_repo: Path, tmp_path: Path
) -> None:
    """`rename to` / `copy to` 也必须走解析道——它们不出现在 `---`/`+++` 里。

    真机证据（第二十六轮）：只解析 `diff --git`/`---`/`+++` 时，下面这条 4 行补丁
    让 `apply_patch` **返回 ok**、根外新建 `ESCAPED.txt`（内容 = 根内源文件字节）、
    根内源文件被移走；协议层 `pp mcp git --root` 同样可达。
    """
    import os as _os

    outside = tmp_path / "outside"
    outside.mkdir()
    link = git_repo / "linked"
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("本平台无法创建 junction/symlink（无法验证 rename/copy 防护）")

    (git_repo / "src.txt").write_text("RENAME-ME\n", encoding="utf-8")
    rename_patch = (
        "diff --git a/src.txt b/src.txt\n"
        "similarity index 100%\n"
        "rename from src.txt\n"
        "rename to linked/ESCAPED.txt\n"
    )
    copy_patch = (
        "diff --git a/src.txt b/src.txt\n"
        "similarity index 100%\n"
        "copy from src.txt\n"
        "copy to linked/COPIED.txt\n"
    )

    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, rename_patch)
    with pytest.raises(ToolError):
        git_server.apply_patch(git_repo, copy_patch)

    assert not (outside / "ESCAPED.txt").exists(), "rename 不得在根外落盘"
    assert not (outside / "COPIED.txt").exists(), "copy 不得在根外落盘"
    assert (git_repo / "src.txt").exists(), "根内源文件不得被移走"

    # 防误伤：根内的 rename 仍须成功
    git_server.apply_patch(
        git_repo,
        "diff --git a/src.txt b/renamed.txt\n"
        "similarity index 100%\n"
        "rename from src.txt\n"
        "rename to renamed.txt\n",
    )
    assert (git_repo / "renamed.txt").exists(), "根内 rename 必须仍能成功"


# ------------------------------------------------- test：pytest 参数白名单（硬编码锚点）


ALLOWED_PYTEST_ARGS_LITERALS: tuple[tuple[str, ...], ...] = (
    ("-q",),
    ("-x",),
    ("-v",),
    ("-k", "smoke"),
    ("-m", "not slow"),
    ("--maxfail", "1"),
    ("--tb=short",),
    ("-p", "no:cacheprovider"),
    ("tests/test_a.py",),
    ("tests/test_a.py::test_one",),
)


@pytest.mark.parametrize("args", ALLOWED_PYTEST_ARGS_LITERALS)
def test_pytest_arg_whitelist_accepts_expected(args: tuple[str, ...]) -> None:
    """**硬编码锚点**：这 10 组必须放行（删白名单条目会让本用例变红）。"""
    assert pytest_runner.validate_pytest_args(list(args)) == list(args)


FORBIDDEN_PYTEST_ARGS_LITERALS: tuple[tuple[str, ...], ...] = (
    ("--junitxml=/tmp/x",),
    ("--rootdir=/",),
    ("--confcutdir=/etc",),
    ("--basetemp=/tmp",),
    ("-c", "evil.ini"),
    ("--html=report.html",),
    ("-p", "evil_plugin"),
    ("--import-mode=importlib", "/etc"),
    ("/etc/passwd",),
    ("../outside",),
    ("-k", "../../etc"),
)


@pytest.mark.parametrize("args", FORBIDDEN_PYTEST_ARGS_LITERALS)
def test_pytest_arg_whitelist_rejects_dangerous(args: tuple[str, ...]) -> None:
    """**硬编码锚点**：这 11 组必须拒绝（这是"命令白名单"的实际内容）。"""
    with pytest.raises(ToolError):
        pytest_runner.validate_pytest_args(list(args))


def test_summarize_shape_without_report() -> None:
    """没有报告时（容器没跑起来）字段仍要齐、且 ok=False。"""
    from patchpilot.sandbox.manager import SandboxResult

    summary = pytest_runner.summarize(
        SandboxResult(
            exit_code=1,
            stdout="",
            stderr="boom",
            timed_out=False,
            duration_s=1.234,
            container_name="pp-x",
            report_error="容器未产出 JUnit 报告（测试可能未能启动）",
        )
    )

    assert set(summary) == {
        "ok",
        "exit_code",
        "timed_out",
        "duration_s",
        "total",
        "passed",
        "failed",
        "errors",
        "skipped",
        "failed_cases",
        "report_error",
    }
    assert summary["ok"] is False
    assert summary["total"] is None, "无报告时统计字段必须是 None（而不是伪装成 0）"
    assert summary["failed_cases"] == []
    assert "容器未产出 JUnit 报告" in str(summary["report_error"])


def test_summarize_extracts_failed_cases() -> None:
    """有报告时：统计与**失败用例列表**必须逐项正确（Agent 靠它决定重试方向）。"""
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult

    xml = (
        '<testsuites><testsuite name="s" tests="3" failures="1" errors="0" skipped="1">'
        '<testcase classname="tests.test_a" name="test_ok" time="0.1"/>'
        '<testcase classname="tests.test_a" name="test_bad" time="0.2">'
        '<failure message="assert 1 == 2"/></testcase>'
        '<testcase classname="tests.test_a" name="test_skip" time="0.0"><skipped/></testcase>'
        "</testsuite></testsuites>"
    )
    report = parse_junit_text(xml)
    summary = pytest_runner.summarize(
        SandboxResult(
            exit_code=1,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.5,
            container_name="pp-x",
            report=report,
        )
    )

    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["ok"] is False
    assert summary["failed_cases"] == ["tests.test_a::test_bad"]


def test_errors_without_failures_still_not_ok() -> None:
    """`errors=1, failed=0, exit=0` 必须判为**不成功**。

    第二十五轮 §8.5：`SandboxResult.ok` 委托 `JUnitReport.ok` 的改动此前**零用例钉住**
    （把 `ok` 退回只看 `failed == 0` 后全量套件仍绿）。JUnit 里 `errors` 代表
    收集/夹具阶段炸掉——比断言失败更严重，绝不能被当成成功。
    """
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult

    xml = (
        '<testsuites><testsuite name="s" tests="1" failures="0" errors="1" skipped="0">'
        '<testcase classname="tests.test_a" name="test_boom" time="0.1">'
        '<error message="fixture exploded"/></testcase>'
        "</testsuite></testsuites>"
    )
    report = parse_junit_text(xml)
    assert (report.failed, report.errors) == (0, 1)

    result = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        duration_s=0.1,
        container_name="pp-x",
        report=report,
    )

    assert result.ok is False, "有 errors 时不得判为成功（否则夹具/收集错误被当成通过）"


def test_zero_tests_is_not_ok() -> None:
    """一个测试都没收集到也不算成功（`JUnitReport.ok` 要求 `total > 0`）。"""
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult

    report = parse_junit_text('<testsuites><testsuite name="s" tests="0"/></testsuites>')
    result = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        duration_s=0.1,
        container_name="pp-x",
        report=report,
    )

    assert result.ok is False


# ---------------------------------------------------------------- search：只读检索


def test_search_text_and_symbol(repo: Path) -> None:
    text_hits = search_server.search_text(repo, "target_fn")
    assert [(m.path, m.line) for m in text_hits] == [("pkg/mod.py", 1)]

    symbol_hits = search_server.search_symbol(repo, "widget")
    assert [(m.path, m.line) for m in symbol_hits] == [("pkg/mod.py", 5)]

    assert search_server.search_text(repo, r"def \w+", regex=True)


def test_search_skips_heavy_dirs_and_binaries(repo: Path) -> None:
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("SECRET_TOKEN = 1\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    (repo / "bin.dat").write_bytes(b"\xff\xfe\x00")

    hits = search_server.search_text(repo, "SECRET_TOKEN")
    assert hits == [], "`.git` 必须被跳过（工具层不应看到仓库元数据）"


def test_search_rejects_escape_and_empty_pattern(repo: Path) -> None:
    with pytest.raises(ToolError):
        search_server.search_text(repo, "x", subdir="../")
    with pytest.raises(ToolError):
        search_server.search_text(repo, "")


def test_search_never_follows_directory_links(repo: Path, tmp_path: Path) -> None:
    """**链接类目录必须被剪枝**——search 的根约束不能只靠 `is_symlink()`。

    第二十四轮真机逃逸：Windows **junction** 的 `is_symlink()` 是 `False`，
    `Path.rglob("*")` 会递归进入它。实测仓库里一个指向外部的 junction 就能让
    `search_text(root, "PRIVATE KEY")` 返回 `<root>/linked/id_rsa`——
    宿主 `~/.ssh` 内容进了 Agent 上下文，而 `fs_server.read_file` 对同一路径是
    **正确拒绝**的（只有 search 漏）。

    junction 在 Windows 上无需管理员即可创建；建不出时才退到 symlink，两者都不行才 skip。
    """
    import os as _os

    outside = tmp_path / "outside"
    outside.mkdir()
    # 刻意写出"形如私钥"的内容：它是**被测输入**（要验证这类文件绝不会被搜到），
    # 按仓库约定加行尾豁免标记并进 waived_secrets 审计栏。
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET-BODY-A\n"  # preflight:allow-secret
    (outside / "id_rsa").write_text(private_key, encoding="utf-8")
    (outside / "mod_outside.py").write_text("def leaked_symbol():\n    pass\n", encoding="utf-8")

    link = repo / "linked"
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("本平台无法创建 junction/symlink（无法验证这条剪枝逻辑）")

    assert search_server.search_text(repo, "PRIVATE KEY") == [], "链接目录内的文件不得被搜到"
    assert search_server.search_text(repo, "SECRET-BODY-A") == []
    assert search_server.search_symbol(repo, "leaked") == []
    # 对照：fs 对同一路径本来就是拒绝的（两条工具的根约束必须一致）
    with pytest.raises(ToolError):
        fs_server.read_file(repo, "linked/id_rsa")


def test_search_does_not_double_visit_an_inside_target_via_link(repo: Path) -> None:
    """链接指向**根之内**时，也必须靠**目录剪枝**跳过——否则同一文件被重复命中。

    这条专门钉住 `os.walk` 的**目录剪枝**（第二十五轮 §8.5：删掉剪枝的变异此前 SURVIVED，
    因为指向根外的链接会被文件级 `is_link_like` 兜住）。指向根内时，
    文件级检查也拦得住"读"，但**拦不住重复枚举**——只有剪枝能让它只被数一次。
    """
    import os as _os

    link = repo / "alias"
    created = False
    if _os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(repo / "pkg")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        created = result.returncode == 0
    if not created:
        try:
            link.symlink_to(repo / "pkg", target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("本平台无法创建 junction/symlink（无法验证目录剪枝）")

    hits = search_server.search_text(repo, "target_fn")

    assert [(m.path, m.line) for m in hits] == [("pkg/mod.py", 1)], (
        f"链接指向根内时不得重复命中，实际 {[(m.path, m.line) for m in hits]}"
    )
