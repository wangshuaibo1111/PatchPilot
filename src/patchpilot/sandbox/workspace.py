"""工作区安全复制：把目标仓库导出成"可以放心挂进容器"的副本。

设计依据（见 .tmp/p1_sandbox_design.md）：
  1. **绝不挂载宿主仓库原目录**——否则容器内代码可改写宿主源码，
     或通过 symlink 指向宿主任意路径；
  2. symlink 防护必须在**复制阶段**做，容器内已无校验能力。

本模块的三条硬规则：
  - **不复制任何链接类对象**（符号链接与 Windows junction 都不复制，最省心的强策略）；
  - **剥离 `.git/hooks/`**——否则容器内执行 git 操作会触发宿主的钩子脚本；
  - **剥离 `.git/config` / credentials**——宿主的 config 里可能带代理地址与凭据
    （本项目全局配置里就有 `http.proxy`），复制进容器等于把它们交给不可信代码。

被剥离的路径会**逐条记录**在结果里，而不是静默丢弃。

位置：`src/patchpilot/sandbox/workspace.py`（原 `.tmp/p1_staging/workspace_copy.py`），
断言已迁为 `tests/test_workspace_copy.py`。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# 与 policy 共享链接判定（junction 也必须被认出来）：
# `os.walk(followlinks=False)` 对 junction 仍会进入，只有剪枝才能阻止它被整棵复制。
from patchpilot.sandbox.policy import is_link_like

DEFAULT_MAX_BYTES = 2 * 1024**3  # 2 GiB

# 构建/缓存噪音：跳过但不必逐条上报
DEFAULT_EXCLUDES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        "node_modules",
        ".venv",
        "venv",
        ".conda",
        ".tmp",
    }
)

# 安全敏感的目录：不复制且必须上报
GIT_STRIP_DIRS = frozenset({".git/hooks"})

# 安全敏感的文件：不复制且必须上报（顶层精确路径）
GIT_STRIP_FILES = frozenset(
    {
        ".git/config",
        ".git/config.worktree",
        ".git/credentials",
        ".git-credentials",
    }
)

# `.git` 下**任意层级**都必须剥离的文件名（子模块与 worktree 的配置常带令牌 URL）。
#
# 缺陷记录（第八轮审查者真机判决性证据）：`GIT_STRIP_FILES` 只匹配顶层精确路径，于是
# `.git/modules/<sub>/config`（子模块配置，常含 `https://user:ghp_…@github.com/...`）
# 与 `.git/worktrees/<wt>/config.worktree` **会被复制进沙箱副本**——
# 不可信代码在容器里读得到，还能把它写进补丁或结果里。本模块的硬规则之一是
# "剥离 .git/config / credentials，且必须在复制阶段做（容器内已无校验能力）"。
#
# 后续两轮又各发现一个同族缺口：bare/mirror 的 `deps/lib.git/config`（段名以 `.git`
# 结尾）与**任意层级**的 `.git-credentials`（`vendor/.git-credentials`、
# `nested/.git/.git-credentials`，实测都带明文令牌进了副本）。
_GIT_CONFIG_BASENAMES = frozenset(
    {"config", "config.worktree", "credentials", "git-credentials"}
)

# 整目录丢弃：这些目录只服务多仓库布局，沙箱内不需要，且内部结构复杂
# （modules 下每个子模块一个对象库 + config；worktrees 下每个工作树一个配置）。
# 注意嵌套仓库里同样存在 `<...>/.git/modules`，因此判定用"祖先段是 git 目录"而非精确匹配。
_GIT_STRIP_TREES = ("modules", "worktrees")


def _git_dir_index(parts: list[str]) -> int | None:
    """返回**最靠后**的 git 仓库目录段的下标（`.git` 或以 `.git` 结尾），没有则 None。"""
    for index in range(len(parts) - 1, -1, -1):
        if _is_git_dir_name(parts[index]):
            return index
    return None


def _is_git_dir_name(segment: str) -> bool:
    """某一路径段是否代表 git 的仓库目录。

    两种命名都要认：
      - `.git`：普通仓库的元数据目录；
      - **以 `.git` 结尾**：bare/mirror 仓库的约定命名（`deps/lib.git`）——
        第三轮审查的真机证据是 `deps/lib.git/config` 连同明文 `ghp_` 令牌进了副本。

    刻意**不**把所有 `*.git` 目录整棵丢弃（那会误删业务数据），只按下面的规则剥具体文件。
    """
    return segment == ".git" or segment.endswith(".git")


def _is_git_config_like(rel: str) -> bool:
    """相对路径里**任意祖先段**是 git 仓库目录，且该文件是必须剥离的配置类文件。

    四种命名都要覆盖（三轮审查各补一种）：
      `.git/config`、`vendor/.git/config`、`deps/lib.git/config`、
      `vendor/.git-credentials`（裸仓库把凭据文件放在仓库目录**旁边**）。
    """
    parts = rel.replace("\\", "/").split("/")
    if _git_dir_index(parts[:-1]) is None:
        return False
    return parts[-1] in _GIT_CONFIG_BASENAMES


def _is_git_credentials_sidecar(rel: str) -> bool:
    """`<任意>.git-credentials`：git 的凭据文件可以直接躺在任意目录下（不在 `.git` 内）。

    实测泄漏路径：`vendor/.git-credentials`、`nested/.git/.git-credentials`
    （后者在 `.git` 目录内，由 `_is_git_config_like` 覆盖；前者靠本函数）。
    """
    name = rel.replace("\\", "/").split("/")[-1]
    return name == ".git-credentials"


def _is_git_internal_tree(rel: str) -> bool:
    """git 的 `modules` / `worktrees` 目录（任意 git 目录之下，含嵌套仓库）。

    git 对子模块正是用 `<gitdir>/modules/<name>`，因此不能只匹配顶层 `.git/modules`。
    """
    parts = rel.replace("\\", "/").split("/")
    index = _git_dir_index(parts)
    if index is None:
        return False
    return len(parts) > index + 1 and parts[index + 1] in _GIT_STRIP_TREES


def _is_git_hooks_dir(rel: str) -> bool:
    """路径是否为 git 仓库目录（含嵌套仓库、含 `*.git` 命名）下的 `hooks` 目录。

    覆盖三种层级：
      - `.git/hooks`、`foo.git/hooks`；
      - `vendor/.git/hooks`；
      - **子模块**：`vendor/.git/modules/sub/hooks`（git 对子模块用的正是 `<gitdir>/hooks`，
        第二轮的非阻塞项实测它会漏）。
    """
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[-1] != "hooks":
        return False
    return _git_dir_index(parts[:-1]) is not None


class WorkspaceCopyError(RuntimeError):
    """复制前置条件不满足或复制过程失败。"""


@dataclass(frozen=True, slots=True)
class WorkspaceCopyResult:
    source: Path
    destination: Path
    files_copied: int
    bytes_copied: int
    skipped_symlinks: tuple[str, ...] = ()
    stripped_paths: tuple[str, ...] = ()
    excluded_dirs: tuple[str, ...] = ()

    def summary_line(self) -> str:
        return (
            f"copied={self.files_copied} files / {self.bytes_copied} bytes "
            f"symlinks_skipped={len(self.skipped_symlinks)} "
            f"stripped={len(self.stripped_paths)} excluded={len(self.excluded_dirs)}"
        )


@dataclass(slots=True)
class _Scan:
    files: list[tuple[Path, str]] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def _validate_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    src = Path(source).resolve()
    dst = Path(destination).resolve()

    if not src.exists():
        raise WorkspaceCopyError(f"源目录不存在: {src}")
    if not src.is_dir():
        raise WorkspaceCopyError(f"源路径不是目录: {src}")
    if src == dst:
        raise WorkspaceCopyError("源目录与目标目录相同")
    if dst.is_relative_to(src):
        raise WorkspaceCopyError(f"目标目录位于源目录之内，会导致递归复制: {dst}")
    if src.is_relative_to(dst):
        raise WorkspaceCopyError(f"源目录位于目标目录之内: {src}")
    if dst.exists() and any(dst.iterdir()):
        raise WorkspaceCopyError(f"目标目录必须为空或不存在: {dst}")
    return src, dst


def _scan(source: Path) -> _Scan:
    """遍历源目录。不跟随符号链接，也不复制它们。"""
    scan = _Scan()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)

        kept: list[str] = []
        for name in dirnames:
            path = current / name
            rel = path.relative_to(source).as_posix()
            if is_link_like(path):
                # 符号链接/junction 一概不跟随、不复制。
                # 必须在这里从 dirnames 中摘掉：os.walk(followlinks=False) 对 junction
                # 仍会进入（实测），只有剪枝才能阻止它被整棵复制进来。
                scan.symlinks.append(rel)
                continue
            if (
                rel in GIT_STRIP_DIRS
                or _is_git_internal_tree(rel)
                or _is_git_hooks_dir(rel)
            ):
                scan.stripped.append(rel)
                continue
            if name in DEFAULT_EXCLUDES:
                scan.excluded.append(rel)
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            path = current / name
            rel = path.relative_to(source).as_posix()
            if is_link_like(path):
                scan.symlinks.append(rel)
                continue
            # 顶层精确匹配 + git 目录内的配置类文件 + 任意层级旁的凭据文件
            if (
                rel in GIT_STRIP_FILES
                or _is_git_config_like(rel)
                or _is_git_credentials_sidecar(rel)
            ):
                scan.stripped.append(rel)
                continue
            scan.files.append((path, rel))
    return scan


def copy_workspace(
    source: Path,
    destination: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> WorkspaceCopyResult:
    """把 `source` 复制成可安全挂载的 `destination`。

    复制完成后调用方**必须**再跑一次 `find_escaping_links(destination)` 复核；
    本模块的策略是"不复制任何 symlink"，因此正常情况下该函数必返回空列表。
    """
    src, dst = _validate_paths(source, destination)
    scan = _scan(src)

    total = 0
    for path, rel in scan.files:
        try:
            total += path.stat().st_size
        except OSError as exc:
            raise WorkspaceCopyError(f"无法读取 {rel}: {exc}") from exc
    if total > max_bytes:
        raise WorkspaceCopyError(f"源目录过大：{total} > {max_bytes} 字节上限")

    created_here = not dst.exists()
    dst.mkdir(parents=True, exist_ok=True)

    bytes_copied = 0
    try:
        for path, rel in scan.files:
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            bytes_copied += path.stat().st_size
    except OSError as exc:
        if created_here:
            shutil.rmtree(dst, ignore_errors=True)
        raise WorkspaceCopyError(f"复制失败，已回滚目标目录: {exc}") from exc

    return WorkspaceCopyResult(
        source=src,
        destination=dst,
        files_copied=len(scan.files),
        bytes_copied=bytes_copied,
        skipped_symlinks=tuple(sorted(scan.symlinks)),
        stripped_paths=tuple(sorted(scan.stripped)),
        excluded_dirs=tuple(sorted(scan.excluded)),
    )
