"""工作区安全复制的测试（**无需 Docker**）。

来源：`.tmp/p1_staging/verify_workspace_copy.py`（离线 29/29）按 `MIGRATION.md` 改写为 pytest。

被验证的三条硬规则（安全性的核心，都不依赖 Docker）：
  1. **绝不挂载宿主仓库原目录**：只复制副本；
  2. **不复制任何链接类对象**（符号链接与 Windows junction 都不复制）——
     必须在**复制阶段**做，容器内已无校验能力；
  3. **剥离 `.git/hooks/` 与 `.git/config`/credentials**：宿主的 config 里可能带
     代理地址与凭据（本项目全局配置里就有 `http.proxy`），复制进容器等于交给不可信代码。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from patchpilot.sandbox.policy import find_escaping_links
from patchpilot.sandbox.workspace import WorkspaceCopyError, copy_workspace

EXPECTED_FILES = 4  # pkg/mod.py + README.md + .git/HEAD + .git/objects/abc


def make_repo(root: Path) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "pkg" / "__pycache__").mkdir(exist_ok=True)
    (root / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    (root / ".pytest_cache").mkdir(exist_ok=True)
    (root / ".pytest_cache" / "junk").write_text("junk", encoding="utf-8")
    # 模拟真实仓库的 .git：hooks 与 config 都必须被剥离
    (root / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "hooks" / "pre-commit").write_text(
        "#!/bin/sh\necho pwned\n", encoding="utf-8"
    )
    (root / ".git" / "config").write_text(
        '[http]\n\tproxy = http://127.0.0.1:7897\n[remote "origin"]\n'
        "\turl = https://u:p@github.com/x/y.git\n",
        encoding="utf-8",
    )
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "objects").mkdir(exist_ok=True)
    (root / ".git" / "objects" / "abc").write_bytes(b"obj")


def make_git_secrets(root: Path) -> None:
    """铺开**所有**已知会泄露凭据/执行钩子的 git 路径（每一轮审查都从这类路径进来）。

    分三类，覆盖两轮审查各自发现的缺口：
      1. 顶层：`.git/config`、`.git/config.worktree`、`.git/credentials`、
         `.git-credentials`、`.git/hooks/*`；
      2. 多仓库布局：`.git/modules/<sub>/config`（子模块，常带令牌 URL）、
         `.git/worktrees/<wt>/config.worktree`；
      3. **vendored 嵌套仓库**：`vendor/.git/config`、`vendor/.git/hooks/*`
         （`.git` 不在路径首段，是最新发现的一类）。
    """
    # 下面几行刻意写出"形如真凭据"的 URL：它们是被测输入（要验证这些字节
    # 绝不会被复制进沙箱副本），按本仓库约定加行尾豁免标记并进 waived_secrets 审计栏。
    token = "ghp_" + "a" * 20  # noqa: S105  # preflight:allow-secret
    (root / ".git").mkdir(parents=True, exist_ok=True)

    def write_raw(rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_git_config(rel: str, url: str) -> None:
        write_raw(rel, f'[remote "origin"]\n\turl = {url}\n')

    # 每条 URL 都形如真凭据，**标记必须与密钥字面量同一行**（扫描器按行豁免）
    write_git_config(".git/config", f"https://a:{token}@g.com/o/r.git")  # preflight:allow-secret
    write_raw(".git/config.worktree", "[core]\n\tbare = false\n")
    write_raw(".git/credentials", f"https://b:{token}@g.com\n")  # preflight:allow-secret
    write_raw(".git-credentials", f"https://c:{token}@g.com\n")  # preflight:allow-secret
    write_raw(".git/hooks/post-checkout", "#!/bin/sh\necho nested-pwned\n")
    # 子模块配置：SWE 类仓库里常带令牌 URL
    sub_url = f'[remote "origin"]\n\turl = https://a:{token}@g.co/s\n'  # preflight:allow-secret
    write_raw(".git/modules/sub/config", sub_url)
    write_raw(".git/modules/sub/objects/ab/cdef", "obj")
    write_raw(".git/worktrees/wt/config.worktree", "[core]\n\tbare = false\n")
    # vendored 嵌套仓库：`.git` 不在路径首段
    vendor = f'[remote "origin"]\n\turl = https://b:{token}@g.co/v\n'  # preflight:allow-secret
    write_raw("vendor/.git/config", vendor)
    write_raw("vendor/.git/hooks/post-checkout", "#!/bin/sh\necho vendored-pwned\n")
    # 子模块的 hooks：git 对子模块用的正是 <gitdir>/hooks
    write_raw("vendor/.git/modules/sub/hooks/pre-commit", "#!/bin/sh\necho sub-pwned\n")
    # **bare/mirror 命名**：目录名以 .git 结尾（第十轮真机证据：明文令牌曾进副本）
    bare = f'[remote "origin"]\n\turl = https://c:{token}@g.co/lib\n'  # preflight:allow-secret
    write_raw("deps/lib.git/config", bare)
    write_raw("deps/lib.git/hooks/pre-commit", "#!/bin/sh\necho bare-pwned\n")
    # 裸仓库把凭据文件放在仓库目录**旁边**（第十一轮实测：这些路径连同明文令牌进过副本）
    side = f"https://d:{token}@g.co\n"  # preflight:allow-secret
    write_raw("vendor/.git-credentials", side)
    write_raw("nested/.git/.git-credentials", side)
    # 对照：业务上"名字里含 .git"但并非仓库目录的路径**不得**被误剥
    write_raw("my.git/config", "[core]\n\tbusiness = true\n")
    write_raw("app/config", "business = 1\n")
    write_raw(".gitignore", "*.pyc\n")
    write_raw("vendor/lib.py", "VALUE = 1\n")


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    make_repo(src)
    return src


# ---------------------------------------------------------- A/B/C. 正常复制


def test_copies_business_files_and_counts_them(source: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst" / "repo"  # 不存在的目标

    result = copy_workspace(source, dst)

    assert dst.is_dir()
    assert (dst / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (dst / "README.md").is_file()
    assert result.files_copied == EXPECTED_FILES
    assert result.bytes_copied > 0


def test_cache_directories_are_excluded_and_recorded(source: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"

    result = copy_workspace(source, dst)

    assert not (dst / "pkg" / "__pycache__").exists()
    assert not (dst / ".pytest_cache").exists()
    assert len(result.excluded_dirs) == 2, result.excluded_dirs


def test_git_hooks_and_config_are_stripped_and_recorded(source: Path, tmp_path: Path) -> None:
    dst = tmp_path / "dst"

    result = copy_workspace(source, dst)

    assert not (dst / ".git" / "hooks").exists(), "hooks 会在容器内被 git 触发"
    assert not (dst / ".git" / "config").exists(), "config 可能带凭据与代理"
    assert set(result.stripped_paths) == {".git/hooks", ".git/config"}
    # 副本里不得残留宿主代理地址
    assert "7897" not in (dst / ".git" / "HEAD").read_text(encoding="utf-8")
    # .git 其余内容保留，容器内 `git diff` 仍可用
    assert (dst / ".git" / "HEAD").is_file()


def test_all_git_credential_paths_are_stripped(tmp_path: Path) -> None:
    """**任意层级**的 git 配置/凭据/钩子都不得进副本（两轮审查发现的全部路径）。

    这是本轮新增逻辑（`GIT_STRIP_TREES` 与 `_is_git_config_like`）的回归用例——
    上一轮的阻塞正是这条路径，修完若没有用例，套件全绿等于没验证过，
    后续任何重构都能把令牌静默放回沙箱。
    """
    src = tmp_path / "src"
    src.mkdir()
    make_git_secrets(src)
    (src / "keep.txt").write_text("k", encoding="utf-8")

    dst = tmp_path / "dst"

    result = copy_workspace(src, dst)

    forbidden = [
        ".git/config",
        ".git/config.worktree",
        ".git/credentials",
        ".git-credentials",
        ".git/hooks",
        ".git/modules",
        ".git/worktrees",
        "vendor/.git/config",
        "vendor/.git/hooks",
        "vendor/.git/modules",
        "vendor/.git/modules/sub/hooks",
        "deps/lib.git/config",
        "deps/lib.git/hooks",
        "vendor/.git-credentials",
        "nested/.git/.git-credentials",
    ]
    for rel in forbidden:
        assert not (dst / rel).exists(), f"{rel} 不得出现在沙箱副本里"
        # 记录粒度说明：`os.walk` 在父目录被剥离时**不会进入**其子目录，
        # 所以 `vendor/.git/modules/sub/hooks` 这类更深路径不会单独出现在
        # `stripped_paths` 里（它被 `vendor/.git/modules` 整棵挡掉）。
        # 这里断言"要么自己被记录，要么某个祖先被记录"。
        ancestors = ["/".join(rel.split("/")[: i + 1]) for i in range(len(rel.split("/")))]
        assert any(item in result.stripped_paths for item in ancestors), (
            f"{rel} 必须被剥离且其自身或某个祖先出现在 stripped_paths 中"
        )

    # 业务文件不得被误剥。
    # 注意 `my.git/config` **不在**此列：判定规则是"段名以 `.git` 结尾即视为 git 仓库目录"，
    # 因此 `my.git` 这类命名会（保守地）被当成仓库、其 config 被剥离。
    # 这是刻意的取舍——**假阳性的代价只是少复制一个文件并记入 stripped_paths**，
    # 而假阴性（漏剥自带令牌的 bare 仓库）是把宿主凭据交给不可信沙箱。
    for rel in ("app/config", ".gitignore", "vendor/lib.py"):
        assert (dst / rel).is_file(), f"{rel} 是业务文件，不得被剥离"

    # 副本内任何位置都不得再有令牌或钩子脚本
    leaked = [
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*")
        if p.is_file() and "ghp_" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert leaked == [], f"副本内仍含令牌: {leaked}"

    hooks = [
        p.relative_to(dst).as_posix()
        for p in dst.rglob("*")
        if p.is_file() and p.read_text(encoding="utf-8", errors="replace").startswith("#!/bin/sh")
    ]
    assert hooks == [], f"副本内仍含钩子脚本: {hooks}"

    # 正常业务文件（含 vendored 目录下的非 .git 内容）仍然复制
    assert (dst / "keep.txt").is_file()
    assert (dst / "vendor" / "lib.py").is_file(), "vendored 目录本身要保留，只剥离其中的 .git"


# ------------------------------- D. 链接类对象：不复制，且不泄露宿主内容


def _make_dir_link(link: Path, target: Path) -> bool:
    """优先 Windows junction（无需管理员权限），失败再退回 symlink。"""
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def _make_file_link(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
        return True
    except OSError:
        return False


def test_link_like_objects_are_never_copied_and_host_secret_never_leaks(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    make_repo(src)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST SECRET", encoding="utf-8")

    created: list[str] = []
    if _make_dir_link(src / "escape_dir", outside):
        created.append("escape_dir")
    if _make_dir_link(src / "inside_dir", src / "pkg"):
        created.append("inside_dir")
    if _make_file_link(src / "escape_file", outside / "secret.txt"):
        created.append("escape_file")
    if _make_file_link(src / "inside_file", src / "README.md"):
        created.append("inside_file")

    if not created:
        # 刻意 fail 而不是 skip：原型的注释写得很对——"跳过的检查同样是缺陷"。
        # 本用例验证的是"链接类对象一个都不复制"这条硬规则；平台造不出链接时
        # 套件若静默全绿，就等于链接策略从未被验证过。
        # （Windows 上 junction 不需要管理员权限，正常都能造出来，所以这条不会被随便触发。）
        pytest.fail(
            "本环境无法创建任何链接（junction 需要 Windows、symlink 需要权限）——"
            "链接策略未被验证，拒绝静默通过"
        )

    dst = tmp_path / "dst" / "repo"

    result = copy_workspace(src, dst)

    assert not (dst / "escape_dir").exists()
    assert not (dst / "inside_dir").exists(), "工作区内部的链接同样不复制（强策略）"
    assert not (dst / "escape_file").exists()
    assert set(created) <= set(result.skipped_symlinks), result.skipped_symlinks
    # 关键泄露检查：副本里绝不能出现宿主的 secret.txt
    leaked = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("secret.txt"))
    assert leaked == [], leaked
    assert find_escaping_links(dst) == []
    assert result.files_copied == EXPECTED_FILES, "链接的存在不影响普通文件复制"


# ------------------------------------------- E. 前置条件：不该复制的必须拒绝


def test_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceCopyError):
        copy_workspace(tmp_path / "nope", tmp_path / "d1")


def test_rejects_same_source_and_destination(source: Path) -> None:
    with pytest.raises(WorkspaceCopyError):
        copy_workspace(source, source)


def test_rejects_destination_inside_source(source: Path) -> None:
    with pytest.raises(WorkspaceCopyError):
        copy_workspace(source, source / "sub")


def test_rejects_source_inside_destination(source: Path) -> None:
    with pytest.raises(WorkspaceCopyError):
        copy_workspace(source / "pkg", source)


def test_rejects_non_empty_destination(source: Path, tmp_path: Path) -> None:
    non_empty = tmp_path / "non-empty"
    non_empty.mkdir()
    (non_empty / "existing.txt").write_text("x", encoding="utf-8")

    with pytest.raises(WorkspaceCopyError):
        copy_workspace(source, non_empty)


def test_accepts_empty_existing_destination(source: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty-target"
    empty.mkdir()

    result = copy_workspace(source, empty)

    assert result.files_copied == EXPECTED_FILES


# ------------------------------------------------------------ F. 体积上限


def test_size_limit_rejects_and_leaves_no_residue(source: Path, tmp_path: Path) -> None:
    dst = tmp_path / "limited" / "repo"

    with pytest.raises(WorkspaceCopyError):
        copy_workspace(source, dst, max_bytes=1)

    assert not dst.exists(), "超限后目标目录必须被回滚，不留残留"


def test_midway_copy_failure_rolls_back_the_destination(
    source: Path, tmp_path: Path, monkeypatch
) -> None:
    """**复制中途失败**的回滚分支必须有用例（第十一轮变异实验 M28 曾存活）。

    `max_bytes` 是在复制**之前**判掉的，所以"超限不留残留"那条用例从不进入这个分支。
    这里让第二个文件复制时抛 OSError，断言：抛 WorkspaceCopyError、
    且**目标目录被整棵回滚**（不留半份副本）。
    """
    dst = tmp_path / "midway" / "repo"
    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def flaky(src, target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("注入的复制失败")
        return real_copy2(src, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", flaky)

    with pytest.raises(WorkspaceCopyError, match="已回滚"):
        copy_workspace(source, dst)

    assert calls["n"] >= 2, "必须真的复制到第二个文件才触发该分支"
    assert not dst.exists(), "中途失败必须回滚目标目录，不留半份副本"
