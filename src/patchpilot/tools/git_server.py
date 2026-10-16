"""`git` server 的工具：只读查询 + 受限的补丁应用。

约束（p1_sandbox_design.md §4）：**禁止 `remote` / `push` / 任何网络操作**。
实现方式不是"过滤参数"，而是：

  1. 把允许的**子命令**写成白名单（`status` / `diff` / `log` / `show` / `apply`）；
  2. 每个子命令的**参数**再走一次白名单（见 `_ALLOWED_ARGS`），默认拒绝；
  3. 子进程显式注入 `GIT_TERMINAL_PROMPT=0`、`GIT_ASKPASS` 与
     `GIT_CONFIG_NOSYSTEM=1`，并由 `policy` 的 `--network none` 兜底。

"两层白名单"是刻意的：只做子命令白名单会漏掉 `git diff --no-index /etc/passwd x`
这类**读根之外文件**的用法（`--no-index` 允许比较任意两个路径）。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from patchpilot.sandbox.policy import is_link_like
from patchpilot.tools import ToolError

# 允许的 git 子命令
ALLOWED_SUBCOMMANDS = frozenset({"status", "diff", "log", "show", "apply", "rev-parse"})

# 每个子命令允许的"安全开关"（前缀匹配，`=` 续接也认）；其余一律拒绝。
# **注意**：`--no-index`、`--output`、`--ext-diff`、`--upload-pack` 等一律不在表内，
# 因为它们能读写仓库之外的文件或触发外部程序。
_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    "status": frozenset(
{
        "--short",
        "--porcelain",
        "--porcelain=v1",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=no",
        "--untracked-files=normal",
        "-s",
        "-b",
    }),
    "diff": frozenset(
{
        "--stat",
        "--numstat",
        "--name-only",
        "--name-status",
        "--cached",
        "--staged",
        "--no-color",
        "--unified=0",
        "--unified=1",
        "--unified=3",
        "-U0",
        "-U1",
        "-U3",
        "--",
    }),
    "log": frozenset(
{
        "--oneline",
        "--stat",
        "--no-color",
        "-n",
        "--max-count",
        "--format=%H",
        "--format=%h",
        "--format=%s",
        "--",
    }),
    "show": frozenset(
{
        "--stat",
        "--no-color",
        "--oneline",
        "--format=%H",
        "--format=%s",
        "--",
    }),
    "apply": frozenset(
{
        "--check",
        "--stat",
        "--verbose",
        "--whitespace=nowarn",
        "--3way",
        "-p0",
        "-p1",
        "-p2",
        "--",
    }),
    "rev-parse": frozenset(
{
        "--short",
        "HEAD",
        "--verify",
        "--abbrev-ref",
    }),
}

# 拒绝出现在**任何位置**的模式（参数值级别的兜底）：
# 绝对路径、`..` 回溯、协议/传输相关、以及任何形如 URL 的东西。
_FORBIDDEN_VALUE = re.compile(
    r"(^/)|(^[A-Za-z]:[\\/])|(\.\.)|(://)|(^git@)|(^--upload-pack)|(^--exec)",
)


class GitError(ToolError):
    """git 工具的错误（子命令或参数被拒 / 执行失败）。"""


def _validate(subcommand: str, args: list[str]) -> None:
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise GitError(
            f"不允许的 git 子命令: {subcommand!r}；允许: {sorted(ALLOWED_SUBCOMMANDS)}"
        )
    allowed = _ALLOWED_ARGS[subcommand]
    for arg in args:
        if not isinstance(arg, str):
            raise GitError(f"git 参数必须是字符串: {arg!r}")
        if _FORBIDDEN_VALUE.search(arg):
            raise GitError(
            f"git 参数含被禁模式（绝对路径/回溯/URL）: {arg!r}"
        )
        # 开关必须精确在白名单里；非开关（修订号、路径）必须**不含**路径分隔符逃逸
        if arg.startswith("-"):
            head = arg.split("=", 1)[0]
            if arg not in allowed and head not in allowed:
                raise GitError(
                    f"git {subcommand} 不允许的参数: {arg!r}；允许: {sorted(allowed)}"
                )


def _run_git(root: Path, argv: list[str], *, timeout_s: float = 60.0) -> str:
    env = {
        "GIT_TERMINAL_PROMPT": "0",  # 绝不交互式索要凭据
        "GIT_ASKPASS": "",  # 不调用外部 askpass
        "GIT_CONFIG_NOSYSTEM": "1",  # 忽略系统级配置（可能含 insteadOf 改写）
        "GIT_CONFIG_GLOBAL": str(root / ".git" / "patchpilot-gitconfig-none"),
        "HOME": str(root),  # 让 git 找不到宿主 ~/.gitconfig 与 ~/.git-credentials
        "PATH": os.environ.get("PATH", ""),
    }
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {argv[0]} 超时") from exc
    except OSError as exc:
        raise GitError(f"无法执行 git: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise GitError(
            f"git {argv[0]} 失败（rc={proc.returncode}）: {(proc.stderr or '').strip()[:300]}"
        )
    return proc.stdout


def status(root: Path) -> str:
    """`git status --short`。"""
    return _run_git(root, ["status", "--short"])


def diff(root: Path, args: list[str] | None = None) -> str:
    """`git diff`（可选参数走白名单）。"""
    extra = list(args or [])
    _validate("diff", extra)
    return _run_git(root, ["diff", "--no-color", *extra])


def log(root: Path, limit: int = 20) -> str:
    """`git log --oneline -n <limit>`。"""
    if not isinstance(limit, int) or limit <= 0 or limit > 1000:
        raise GitError(f"limit 必须在 1..1000 之间: {limit!r}")
    return _run_git(root, ["log", f"-n{limit}", "--oneline", "--no-color"])


def apply_patch(root: Path, patch: str, *, check_only: bool = False) -> str:
    """把 unified diff 应用到工作区。

    - `check_only=True` 时只做 `--check`（不落盘），供 Agent 先验证补丁可应用；
    - 补丁里每个路径 token 都过**两道**校验（文本 + 解析）：见 `_patch_touches_outside`。

    **为什么必须有"解析"这道**（第二十五轮真机逃逸）：`git apply` 会**穿过 junction**
    写文件，而 junction 的 `is_symlink()` 是 `False`——纯文本校验（只看 `/`、盘符、`..`）
    完全看不见它。实测三种形态都能改写/新建根之外的宿主文件：
    ① 改根外既有文件；② 在根外新建；③ junction 套 junction 两跳。
    而且**协议层可达**：`pp mcp git --root <repo>` 调 `apply_patch` 返回 ok，
    根外文件被写成 `WRITTEN-THROUGH-JUNCTION-BY-MCP`；同一路径经 `fs_server.write_file`
    是正确拒绝的——与本模块 `search` 上一轮的漏洞同形。
    """
    if not isinstance(patch, str) or not patch.strip():
        raise GitError("补丁内容不能为空")
    outside = _patch_touches_outside(patch, root)
    if outside:
        raise GitError(f"补丁引用了根目录之外的路径（含绝对路径/`..`/链接逃逸）: {outside}")
    argv = ["apply", "--whitespace=nowarn", "--verbose"]
    if check_only:
        argv.append("--check")
    proc_argv = ["git", *argv]
    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(root),
        "PATH": os.environ.get("PATH", ""),
    }

    # **独立于"猜路径"的最后一道网**（第二十七轮结论）：
    # 文本/解析两道都在"我们理解 git 会怎么解析路径"这个前提上——而实测反复被
    # 引号名、非 a/b 前缀等形态绕过。这里改成**让 git 自己算**：
    # 先用 `--check` + `--numstat` 让 git 列出它**将要写**的文件，
    # 对 git 给出的这份真实清单逐个跑解析级校验；只有全部落在根内才真正落盘。
    # 这样"我们的解析"与"git 的解析"不一致时，以 **git 的清单**为准（fail-closed）。
    pending = _files_git_would_write(root, patch, env)
    for name in pending:
        if _path_escapes(root, name):
            raise GitError(f"git 报告将要写根之外的路径，拒绝执行: {name}")

    proc = subprocess.run(
        proc_argv,
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        raise GitError(f"补丁应用失败（rc={proc.returncode}）: {detail}")
    return proc.stdout or "ok"


def _unquote_c_style(token: str) -> str | None:
    r"""按 git 的 **C 风格**规则解引号；不是引号名则原样返回。

    为什么必须有这一步（第二十七轮 BLOCKING）：`git apply` 会对 `"..."` 里的名字做
    C 反转义后执行，而此前我们只 `strip()`——引号被当成路径的一部分，
    于是 `Path('"linked/X"').parts[0] == '"linked'` 既不是链接、`resolve()` 也仍在根内，
    两道校验全部放行 → 越界写成功。**真机最小复现**：
    `rename to "linked/ESCAPED.txt"` → `apply_patch` 返回 ok、根外新建文件。
    而且这不是人造形态：`core.quotePath=true`（默认）时 git 自己就会把非 ASCII 名字
    输出成 `"a/linked/\346\226\207.txt"` 这种八进制转义形式。

    支持 git 会产出的转义：`\\` `\"` `\t` `\n` `\r` `\a` `\b` `\f` `\v` 与八进制 `\NNN`。
    解析失败返回 `None`（调用方 **fail-closed** 拒绝）。
    """
    if not (token.startswith('"') and token.endswith('"') and len(token) >= 2):
        return token
    body = token[1:-1]
    out = bytearray()
    index = 0
    simple = {
        "a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
        "\\": 92, '"': 34,
    }
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None  # 反斜杠结尾：非法
        escape = body[index]
        if escape in simple:
            out.append(simple[escape])
            index += 1
            continue
        if escape.isdigit():
            octal = body[index : index + 3]
            if len(octal) < 3 or not all(c.isdigit() and c < "8" for c in octal):
                return None
            out.append(int(octal, 8) & 0xFF)
            index += 3
            continue
        return None  # 未知转义：fail-closed
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


#: 以这些前缀开头即"声明了一个路径"；若解析不出路径 → **fail-closed 拒绝**。
_DECLARES_PATH_PREFIXES = (
    "diff --git ",
    "--- ",
    "+++ ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "edit ",
)


class PatchPathError(GitError):
    """补丁里的路径无法被安全解析（fail-closed）。"""


def _patch_path_tokens(patch: str) -> list[str]:
    """抽出补丁声明的每个路径 token（**未剥前缀、未解引号**）。

    对"看起来在处理路径、但不符合任何已知形态"的行 **fail-closed**：
    第二十七轮实测 `rename to\\tlinked/X`（制表符）与 ` rename to ...`（行首空格）
    会**静默抽不出候选**，只是侥幸被 git 自己的 rc=128 挡住——
    静默跳过等于"校验没跑但以为跑了"，必须拒绝。
    """
    tokens: list[str] = []
    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\r")  # 补丁可能来自 Windows（CRLF 行尾）
        if line.startswith("diff --git "):
            tokens.extend(line[len("diff --git ") :].split())
            continue
        if line.startswith(("--- ", "+++ ")):
            tokens.append(line[4:].split("\t")[0].strip())
            continue
        stripped = line.lstrip()
        directive = next((d for d in _DECLARES_PATH_PREFIXES[3:] if stripped.startswith(d)), None)
        if directive is not None:
            tokens.append(stripped[len(directive) :].strip())
            continue
        # **可疑行**：提到 rename/copy/edit 但不符合已知形态 → 拒绝（不静默跳过）
        if stripped.startswith(("rename ", "copy ", "edit ")):
            raise PatchPathError(f"无法解析的补丁声明行（fail-closed）: {line!r}")
    return tokens


def _patch_paths(patch: str) -> list[str]:
    """抽出补丁里声明的**所有**路径，并展开 `-p1` 的等价解释。

    覆盖 5 类声明行 + 引号形态（见 `_unquote_c_style`）。

    **关键设计（第二十七轮定稿）**：不再"猜 git 会剥掉 `a/` 还是 `b/` 前缀"，
    而是承认 **`-p1` 的语义是"剥掉第一段路径分量，不管它叫什么"**——
    因此对每个名字生成**所有"去掉前 k 段"的形式**，逐个交给解析道；
    任何一种解释越界即拒绝。这样 `--- x/linked/victim.txt`（`x/` 不是 `a/`）也拦得住，
    因为"只去掉 `x/`"这一种解释正是 git 实际采用的。
    """
    expanded: list[str] = []
    for token in _patch_path_tokens(patch):
        candidate = token.split("\t")[0].strip()
        if candidate in {"a/", "b/", "/dev/null", "---", "+++", ""}:
            continue
        unquoted = _unquote_c_style(candidate)
        if unquoted is None:
            raise PatchPathError(f"无法解析的引号路径（fail-closed）: {token!r}")
        parts = Path(unquoted).parts
        # 原始名 + 所有"去 k 段"形式（覆盖 -p0 / -p1 / -p2 … 的等价解释）
        for start in range(len(parts)):
            suffix = Path(*parts[start:]).as_posix()
            if suffix and suffix not in expanded:
                expanded.append(suffix)
    return expanded


def _path_escapes(root: Path, candidate: str) -> bool:
    """单个路径是否逃逸根（**解析级**，与 fs/search 同口径）。"""
    resolved_root = Path(root).resolve()
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", candidate):
        return True
    parts = Path(candidate).parts
    if ".." in parts:
        return True
    probe = resolved_root
    for part in parts[:-1]:
        probe = probe / part
        if is_link_like(probe):
            return True
    target = resolved_root / candidate
    if is_link_like(target):
        return True
    try:
        return not target.resolve().is_relative_to(resolved_root)
    except OSError:  # pragma: no cover - 不可解析时保守拒绝
        return True


def _files_git_would_write(root: Path, patch: str, env: dict[str, str]) -> list[str]:
    """让 **git 自己**报告这条补丁将要写哪些文件（含 rename/copy 的目标）。

    用 `git apply --numstat`（只解析、不落盘）：它输出的第三列是**解析后的目标路径**，
    也就是 git 真正会去写的名字。这样就不必让我们的解析器去复刻 git 的路径语法
    （引号 C 转义、`-p1` 剥段、rename/copy 目标……），**不会因为漏一种形态而失守**。

    解析不出任何名字时返回**空列表**——但调用方在拿到空列表时会继续走
    `_patch_touches_outside` 的静态校验，且 `git apply` 本身失败会 rc!=0；
    这里刻意不把"空"当"安全"的充分条件（见 `apply_patch` 的 docstring）。
    """
    proc = subprocess.run(
        ["git", "apply", "--numstat", "--summary", "-"],
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        # git 都解析不了的补丁：让后续真正的 apply 去报错（同样会 rc!=0）
        return []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # `--numstat` 行形如 `1\t0\tpath`；`--summary` 行形如 `rename x => y (100%)`
        fields = line.split("\t")
        if len(fields) >= 3:
            names.append(fields[2])
            continue
        match = re.search(
            r"(?:rename|copy|create|delete)\s+(.+?)(?:\s+=>\s+(.+?))?\s*\(\d+%\)$", line
        )
        if match:
            names.extend([g for g in match.groups() if g])
    return [n.strip().strip('"') for n in names if n.strip()]


def _patch_touches_outside(patch: str, root: Path) -> str | None:
    """返回第一个越界路径（无则 `None`）。**文本 + 解析**两道校验。

    文本道：绝对路径、盘符、`..` 回溯——便宜且能挡掉明显写法；
    解析道（关键）：对 `root/candidate` 的**每一级祖先**检查 `is_link_like`
    （覆盖 symlink **与** junction），并要求 `resolve()` 之后仍在 `root` 之内。

    `_patch_paths` 已把每个名字展开成"原始名 + 去 k 段"的全部解释，
    所以这里**逐个解释**跑两道校验：只要**任何一种**解释可能越界就拒绝
    （`-p1` 到底剥掉哪一段由 git 决定，我们不猜——把所有可能都当危险处理）。

    解析失败（`PatchPathError`）向上抛，由 `apply_patch` 归一化为拒绝（fail-closed）。
    """
    resolved_root = Path(root).resolve()
    for candidate in _patch_paths(patch):
        if candidate.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", candidate):
            return candidate
        parts = Path(candidate).parts
        if ".." in parts:
            return candidate
        # 解析道：逐级祖先不得是链接类对象（junction 会绕过文本判定）
        probe = resolved_root
        for part in parts[:-1]:
            probe = probe / part
            if is_link_like(probe):
                return candidate
        target = resolved_root / candidate
        if is_link_like(target):
            return candidate
        try:
            if not target.resolve().is_relative_to(resolved_root):
                return candidate
        except OSError:  # pragma: no cover - 路径不可解析时保守拒绝
            return candidate
    return None


__all__ = ["GitError", "apply_patch", "diff", "log", "status"]
