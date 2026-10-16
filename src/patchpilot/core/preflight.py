"""上传前门禁（preflight）：确定性检查。

与 code-reviewer 子代理的分工：
  - 本模块只做「确定性」检查：结果可复现、可在 CI 重跑、不依赖模型判断；
  - 语义层面的审查（设计缺陷、边界条件、安全推理）由 code-reviewer 承担。
两者都通过，才允许上传。见 PLAN.md 第 8 节。

审查修正记录：
  首轮 BLOCKING 3/4/5/6/7/8：
  - SKIP_DIRS 判定基于「相对 root 的路径」；
  - 语法检查用 tokenize.open 尊重编码声明；
  - 密钥扫描占位符严格匹配、豁免须为注释且可审计、大文件与豁免项进报告；
  - .gitignore 覆盖优先用 git check-ignore 权威判定。

  二轮 BLOCKING 1/3（本文件最重要的一处修正）：
  - **摘要与检查范围统一为 `iter_scan_files`（git 可见文件集）**。
    上一版摘要只覆盖一份显式白名单（src/tests/agents/.github + 7 个根文件），
    于是根级自举文件（conftest.py、pytest.ini、ruff.toml、setup.cfg…）
    既不在摘要内、也不在语法/ruff 检查范围内——**坏代码可以让这份文件
    把门禁由红转绿，而摘要逐字符不变**，TREE_DIGEST 的绑定意义被击穿。
    现在摘要、语法检查、ruff 检查、密钥扫描四者共用同一个文件集合；
  - 枚举与摘要步骤纳入兜底：任何异常都转成 CheckResult，
    保证 `--json` 契约在任何路径下都成立。

  七轮 BLOCKING 1/2/3（三项同源：门禁给出的 pass 不具持久性）：
  - **`worktree_matches_index` 改为绑定索引字节、且覆盖枚举**。旧实现只遍历
    `git ls-files -s` 的索引条目，而摘要覆盖 `iter_scan_files`（含未跟踪未忽略文件）。
    实测：索引干净时仅新增**未跟踪**的 CRLF 文件 → 该项报 pass，而工作区哈希
    ≠ git 实际会提交的字节；`git add` 之后才 fail。且索引为空时该项是 `skip`
    而非 fail，摘要完全无绑定。现在对 `iter_scan_files` 的**每个**文件比对
    `hash-object --no-filters` 与索引 blob，**不在索引里就 fail**，索引为空也 fail；
    并补上反方向：**索引里有而工作区已删除也 fail**（实测 `AD` 状态的文件仍会进提交，
    等于提交里带着一份从未被摘要与密钥扫描覆盖的字节）；
  - **pytest 执行范围受摘要约束**。新增根 `conftest.py`（`collect_ignore`）或
    `pytest.ini` 的 `addopts --ignore` 能把 fail 翻成 pass，而门禁无法区分
    "测试真通过"与"测试被关掉"。现在显式传入摘要集合中的 `test_*.py`，
    并要求 `--collect-only` 收齐这些文件，缺一即 fail；
  - **非 UTF-8 编码不得静默 pass**。一律按 UTF-8 `errors="replace"` 读取时，
    UTF-16 写入的密钥被解成 U+FFFD 后正则失效（非 `.py` 文件整条路径无人把关）。
    现在按 BOM 与空字节模式探测编码：可疑编码转成显式 fail，二进制文件记入
    `skipped_binary_files` 并出现在报告里，绝不静默。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import time
import tokenize
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# pass/fail/skip 是阻塞语义；diff/info 是**提示语义**——它们不参与 ok 判定，
# 只用于表达"两集合有差异""已退化为目录遍历"这类需要人看一眼但不是缺陷的处境。
CheckStatus = Literal["pass", "fail", "skip", "diff", "info"]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".conda",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".patchpilot",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".tmp",
    # 下面这些必须与 .gitignore 保持同步：非 git 仓库走目录遍历时，
    # 只有 SKIP 规则能替代 .gitignore 的作用。一旦二者不一致，
    # "工作副本（遍历）"与"部署目录（git ls-files）"的摘要就会分叉。
    # 三轮审查 BLOCKING 就是漏了 `venv`（.gitignore 有、SKIP 没有）——
    # 现在由 `_check_skip_rules` 自检把这类分叉变成门禁失败，而不是靠人记得同步。
    ".vscode",
    ".idea",
    ".eggs",
    ".tox",
    ".nox",
    "htmlcov",
}

# 构建/元数据产物目录：按后缀识别，保证"工作副本"与"部署目录"的摘要一致
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info")

# 按文件名后缀跳过的产物（对应 .gitignore 的 `*.py[cod]` / `*.sqlite` / `*.egg-link`）
# 注意：`.egg-link` 是**文件**，不能放进 SKIP_DIR_SUFFIXES——
# `_is_skipped` 只作用于目录部分，混放会让自检说"已覆盖"而摘要仍分叉（四轮审查非阻塞 1）。
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".sqlite", ".sqlite3", ".egg-link", ".DS_Store")


def _key(name: str) -> str:
    """忽略规则的名称归一化。

    Windows 上 git 的忽略匹配是**大小写无关**的（core.ignorecase=true），
    SKIP 规则若大小写敏感就会分叉：实测 `Venv/`、`BUILD/`、`DIST/`、`.ENV`
    被 `git check-ignore` 忽略却不被 `_is_skipped` 忽略，
    同一份内容在部署目录与工作副本算出不同摘要（四轮审查阻塞 1）。
    """
    return name.casefold() if os.name == "nt" else name


_SKIP_DIRS_KEY = frozenset(_key(name) for name in SKIP_DIRS)
_SKIP_DIR_SUFFIXES_KEY = tuple(_key(suffix) for suffix in SKIP_DIR_SUFFIXES)
_SKIP_FILE_SUFFIXES_KEY = tuple(_key(suffix) for suffix in SKIP_FILE_SUFFIXES)

# 这些文件按设计就存放密钥，不参与内容扫描；改由「是否被 git 跟踪」专项检查把关
SKIP_FILES = {".env"}
_SKIP_FILES_KEY = frozenset(_key(name) for name in SKIP_FILES)

MAX_SCAN_BYTES = 512 * 1024

# 摘要范围内的测试文件命名（pytest 的默认规则）。
# pytest 执行范围必须由这份清单（= 摘要覆盖的字节）决定，而不是由仓库配置决定：
# 一个 `conftest.py` 里的 `collect_ignore` 就足以让"必然失败的用例"不被收集（七轮阻塞 2）。
TEST_FILE_GLOB = "test_*.py"

# 判定二进制文件的嗅探字节数：用于"去 NUL 还原"的说明与体积提示
BINARY_SNIFF_BYTES = 8 * 1024

# 豁免必须写成注释形式，且会被记录进报告，便于审查者眼检
ALLOW_MARKER_RE = re.compile(r"#\s*preflight:allow-secret\b")

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("私钥文件块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI/DeepSeek 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # URL 内嵌凭据：正是本项目 PP_POSTGRES_DSN 的形态（二轮 NON_BLOCKING-4）。
    # 必须**捕获口令本身**：否则占位符判定会拿到整串 URL，
    # `postgresql://<user>:<password>@…` 这类写法会被误报。
    (
        "URL 内嵌凭据",
        re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s:/@]{6,})@"),
    ),
    (
        "通用密钥赋值（带引号）",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd)\b\s*[:=]\s*['\"]([^'\"]{20,})['\"]"
        ),
    ),
    (
        "通用密钥赋值（无引号）",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd)\b\s*[:=]\s*"
            r"([A-Za-z0-9+/=_\-]{20,})"
        ),
    ),
]

# 占位符必须是「整个值」或「前缀 + 分隔符」，避免 SampleCorp-xxx 这类真实密钥被放行
PLACEHOLDER_RE = re.compile(
    r"^(?:your|xxx|example|placeholder|changeme|dummy|fake|sample|redacted)(?:[-_].*)?$"
    r"|^[<\[].*[>\]]$"
    r"|^\*+$"
    r"|^x+$"
    r"|^\.{3,}$",
    re.IGNORECASE,
)

KEY_PREFIXES = ("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")

# 必需被忽略的条目：标签 -> 用于 git check-ignore 的代表性文件路径
REQUIRED_IGNORES: dict[str, str] = {
    ".env": ".env",
    ".patchpilot/": ".patchpilot/traces/probe.jsonl",
    "__pycache__/": "src/__pycache__/probe.pyc",
    ".conda/": ".conda/probe",
    ".venv/": ".venv/probe",
}


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    detail: str = ""
    duration_s: float = 0.0


class SecretScanResult(BaseModel):
    findings: list[str] = Field(default_factory=list)
    waived: list[str] = Field(default_factory=list)
    skipped_large: list[str] = Field(default_factory=list)
    # 以下两类必须出现在报告里，否则"扫描了什么"不可见（七轮阻塞 3）：
    # 无法以 UTF-8 系编码解读的文本（密钥可能藏在这里）与二进制文件。
    undecodable: list[str] = Field(default_factory=list)
    skipped_binary: list[str] = Field(default_factory=list)


class PreflightReport(BaseModel):
    ok: bool = False
    root: str = ""
    tree_digest: str = ""
    file_count: int = 0
    checks: list[CheckResult] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    waived_secrets: list[str] = Field(default_factory=list)
    skipped_large_files: list[str] = Field(default_factory=list)
    undecodable_files: list[str] = Field(default_factory=list)
    skipped_binary_files: list[str] = Field(default_factory=list)
    # pytest 范围证据：期望执行的测试文件、实际收集到的测试文件、缺失项。
    # 三者都进报告，使"测试真的被收集"可被审查者核对，而不是只看一个 pass（七轮阻塞 2）。
    pytest_expected_files: list[str] = Field(default_factory=list)
    pytest_collected_files: list[str] = Field(default_factory=list)
    pytest_missing_files: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == "fail"]

    def summary_line(self) -> str:
        counts = {"pass": 0, "fail": 0, "skip": 0, "diff": 0, "info": 0}
        for check in self.checks:
            counts[check.status] += 1
        verdict = "OK" if self.ok else "FAIL"
        return (
            f"preflight={verdict} "
            f"(pass={counts['pass']} fail={counts['fail']} skip={counts['skip']} "
            f"diff={counts['diff']} info={counts['info']}) "
            f"files={self.file_count} digest={self.tree_digest[:12]}"
        )


def _run(
    cmd: list[str],
    cwd: Path,
    timeout_s: float,
    *,
    stdin: str | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> tuple[int, str]:
    # 注意：**不要**在这里把 `GIT_CEILING_DIRECTORIES` 无条件透传给所有子进程。
    # 试过，结果是把"上层环境设的界标"强行套到以 `cwd` 为基准的查找上：
    # 门禁跑 `git rev-parse` 时会先撞到那个界标而**认不出真正的仓库根**
    # （实测：`git_scope` 误报、`worktree_matches_index` 连带 fail）。
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        **dict(extra_env or {}),
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=stdin,
            timeout=timeout_s,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return 127, f"命令不存在: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout_s}s）"
    except OSError as exc:
        return 126, f"执行失败: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _rel_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return ()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_tree_digest(root: Path, files: list[Path] | None = None) -> str:
    """对「将被提交的全部字节」计算内容摘要（32 位十六进制）。

    用途：把 code-reviewer 的 APPROVE 绑定到唯一确定的内容上。
    范围**必须**与密钥扫描、语法检查、ruff 检查一致——否则一个不在摘要内的
    根级自举文件（conftest.py / ruff.toml）就能让门禁由红转绿而摘要不变
    （二轮审查 B1 的判决性证据）。

    位置无关性由 `iter_scan_files` + `SKIP_DIRS` / `SKIP_DIR_SUFFIXES` 保证。
    """
    root = Path(root).resolve()
    targets = files if files is not None else iter_scan_files(root)
    entries: list[str] = []
    for path in targets:
        try:
            entries.append(f"{_rel(root, path)}\0{_sha256_file(path)}")
        except OSError:
            entries.append(f"{_rel(root, path)}\0UNREADABLE")
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()[:32]


def _is_skipped(root: Path, path: Path) -> bool:
    """只按「相对 root 的目录部分」判断，避免祖先目录名误伤整个仓库。"""
    parts = _rel_parts(root, path)
    if not parts:
        return True
    return any(
        _key(part) in _SKIP_DIRS_KEY or _key(part).endswith(_SKIP_DIR_SUFFIXES_KEY)
        for part in parts[:-1]
    )


def _git_files(root: Path) -> list[Path] | None:
    """仅当本目录就是 git 仓库根时，返回 git 会跟踪的文件（含未跟踪但未被忽略的）。

    **校验 root 就是仓库根**：`git ls-files` 会向上寻找仓库，
    若 root 只是仓库的子目录（例如 `pp check --basetemp` 把 pytest 临时目录
    放在 `<root>/.patchpilot/pytest-tmp/run-<pid>-<hex>`，而它被 .gitignore 忽略），
    返回的会是**空列表**——门禁于是静默地什么都没扫却报通过。
    这是实测踩到的门禁绕过路径，因此这里显式比对 toplevel。

    非 git 仓库（或 git 不可用）时返回 None，由调用方退化为目录遍历。
    退化原因由 `git_scope_status` 单独给出，避免"git 不可用"与"仓库损坏"
    （或"root 只是子目录"）被混为一谈（七轮非阻塞 2）。
    """
    ok, _ = git_scope_status(root)
    if not ok:
        return None

    # 必须用 `-z`：默认输出会对非 ASCII/含空格的文件名做引号转义
    # （形如 `"src/caf\303\251_secret.py"`），解析出的路径不存在 →
    # `is_file()` 判否 → 文件掉出交集、**密钥与语法错误一并逃检**，
    # 且摘要随 `core.quotepath` 在两种取值间分叉（git 内置默认正是 quotepath=true）。
    # `-z` 以 NUL 分隔，无转义歧义（六轮审查阻塞 1）。
    code, out = _run(["git", "ls-files", "-z", "-co", "--exclude-standard"], root, 30)
    if code != 0:
        return None
    return [root / entry for entry in out.split("\0") if entry]


def git_scope_status(root: Path) -> tuple[bool, str]:
    """判断 `root` 是否能作为 git 仓库根使用，并给出可直接进报告的原因。

    返回值 ``(可用, 原因)``：不可用时原因用于**代替静默退化**，
    否则"git 不可用"与"仓库损坏"在报告里长得一模一样（七轮非阻塞 2）。
    """
    top = find_toplevel(root)
    if top is None:
        return False, f"{Path(root).resolve()} 不在任何 git 仓库内（git rev-parse 失败）"
    if _key(str(top)) != _key(str(Path(root).resolve())):
        return False, (
            f"{Path(root).resolve()} 是仓库子目录而非仓库根（toplevel={top}）"
            "，退化为目录遍历"
        )
    return True, f"toplevel={top}"


def find_toplevel(root: Path) -> Path | None:
    """`git rev-parse --show-toplevel` 的结果；不在仓库内时返回 None。

    为什么单独拆一个函数：`.git` 目录只存在于**仓库根**，所以
    "root 是仓库子目录"与"root 根本不在仓库里"这两个处境
    在 `(root/'.git').exists()` 上**表现相同**（都是 False）。
    只有问 git"toplevel 是谁"才能区分——这正是八轮 BLOCKING 的根因：
    旧代码只看 `.git` 是否存在，于是子目录被判成"非 git 仓库"而 skip。
    """
    code, out = _run(["git", "rev-parse", "--show-toplevel"], root, 30)
    if code != 0:
        return None
    try:
        return Path(out.strip().splitlines()[0]).resolve()
    except (IndexError, OSError):
        return None


def _walk_files(root: Path) -> list[Path]:
    """目录遍历退化路径：逐层剪枝，跳过符号链接避免环。"""
    files: list[Path] = []
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                dir_key = _key(entry.name)
                if dir_key in _SKIP_DIRS_KEY or dir_key.endswith(_SKIP_DIR_SUFFIXES_KEY):
                    continue
                stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files


def _filter_candidates(root: Path, candidates: list[Path]) -> list[Path]:
    """按 SKIP 规则过滤候选文件（目录层级 + 文件名/后缀）。"""
    selected: list[Path] = []
    for path in candidates:
        if not path.is_file() or _is_skipped(root, path):
            continue
        name_key = _key(path.name)
        if name_key in _SKIP_FILES_KEY or name_key.endswith(_SKIP_FILE_SUFFIXES_KEY):
            continue
        selected.append(path)
    return selected


def _rel_set(root: Path, paths: list[Path]) -> set[str]:
    return {_rel(root, path) for path in paths}


def scan_scope_diff(root: Path) -> tuple[list[str], list[str]]:
    """返回 (仅在 git 集合中, 仅在遍历集合中) 的相对路径，已排序。

    git 的忽略来源不止根 `.gitignore`：还有**逐层** `.gitignore`、`.git/info/exclude`、
    全局 `core.excludesFile`。只信任何一边都会让摘要随环境/位置分叉
    （五轮审查阻塞的判决性实测：一个嵌套 `.gitignore` 就够）。
    """
    root = Path(root).resolve()
    git_candidates = _git_files(root)
    if git_candidates is None:
        return [], []
    git_set = _rel_set(root, _filter_candidates(root, git_candidates))
    walk_set = _rel_set(root, _filter_candidates(root, _walk_files(root)))
    return sorted(git_set - walk_set), sorted(walk_set - git_set)


def iter_scan_files(root: Path) -> list[Path]:
    """门禁的统一文件集合（摘要 / 语法 / ruff / 密钥扫描共用）。

    在 git 仓库里取「git 集合」与「遍历集合」的**交集**：
      - 只信 git：嵌套 `.gitignore` / `info/exclude` / 全局 excludesFile 会让
        摘要随环境分叉，并让被本地忽略的密钥逃过扫描；
      - 只信遍历：SKIP 规则与 `.gitignore` 一旦不同步同样分叉（三、四轮阻塞）。

    交集对两者都免疫：任一忽略来源排除了某文件，它就不进摘要——
    而 git 也不会提交它，所以"不扫描"是安全的，且摘要与位置/环境无关。
    """
    root = Path(root).resolve()
    git_candidates = _git_files(root)
    if git_candidates is None:
        return _filter_candidates(root, _walk_files(root))

    common = _rel_set(root, _filter_candidates(root, git_candidates)) & _rel_set(
        root, _filter_candidates(root, _walk_files(root))
    )
    return [root / rel for rel in sorted(common)]


def _looks_like_placeholder(value: str) -> bool:
    """占位符判定必须严格：整值匹配或「前缀 + 分隔符」，否则视为真实密钥。"""
    text = value.strip().strip("'\"")
    lowered = text.lower()
    for prefix in KEY_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    return bool(PLACEHOLDER_RE.match(text))


class SniffResult(BaseModel):
    """`sniff_text` 的结论。

    关键设计（八轮审查 BLOCKING 后定稿）：**不再试图把"含 NUL 的文件"二分成
    'UTF-16 文本'或'二进制'** —— 任何基于 NUL 统计的分类都存在旁路：
      - "可打印比例"：100 个 CJK 字符即可把比例压到阈值下；
      - "某一半全为 NUL"：UTF-16LE 的 CJK 字符两个字节都非 0（`啊` = U+554A → `4A 55`），
        5000 个 CJK 字符可以让整份文件的 NUL 数为 0。
    因此改为"**把可能的明文都还原出来再扫**"：`variants` 收集原文（UTF-8）、
    剥掉 NUL 后的字节（同时覆盖 UTF-16LE / UTF-16BE / UTF-32 的 ASCII 可读形式）
    以及 UTF-16 解码结果（覆盖 CJK 可读形式）。只要**任何**一种还原里出现密钥，
    就会被 `SECRET_PATTERNS` 命中 —— 于是 **不存在"含密钥却报 pass"的分类旁路**。
    """

    text: str | None = None
    reject_reason: str | None = None
    kind: Literal["text", "binary", "undecodable"] = "text"
    variants: list[str] = Field(default_factory=list)
    scanned_encodings: list[str] = Field(default_factory=list)


def sniff_text(path: Path) -> SniffResult:
    """读取文件并给出编码结论。

    | 情形 | kind | 是否阻塞 | 说明 |
    |---|---|---|---|
    | UTF-8（含 BOM） | text | 否 | 正常按行扫描 |
    | 含 NUL | binary | 否 | **但会扫描补充还原形式**（见 `SniffResult`），并记入报告 |
    | 无 NUL 却非 UTF-8 | undecodable | **是** | 可能是 latin-1/GBK 文本，密钥藏在里面就扫不到 |

    `binary` 不阻塞是因为仓库里有图片等二进制资产；但它必须出现在报告里，
    且其"补充还原形式"已经被扫过——所以"未发现密钥"这句话对它是**有依据的**，
    而不是"没扫"。**绝不静默放行**这一条由此落实。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return SniffResult(
            kind="undecodable", reject_reason=f"读取失败（{type(exc).__name__}）"
        )

    variants: list[str] = []
    encodings: list[str] = []

    def add(raw_text: bytes, label: str) -> None:
        try:
            decoded = raw_text.decode("utf-8")
        except UnicodeDecodeError:
            return
        if decoded not in variants:
            variants.append(decoded)
            encodings.append(label)

    # 1) 含 NUL：一律当二进制/可疑编码处理，但先把明文还原出来扫一遍
    if b"\x00" in raw:
        add(raw.replace(b"\x00", b""), "去 NUL 后按 UTF-8")
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                decoded = raw.decode(encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
            if decoded not in variants:
                variants.append(decoded)
                encodings.append(encoding)
        return SniffResult(
            kind="binary",
            reject_reason="含 NUL 字节（非 UTF-8 文本；已按去 NUL 与 UTF-16 还原后扫描）",
            variants=variants,
            scanned_encodings=encodings,
        )

    # 2) 不含 NUL：先 UTF-8（BOM 用 utf-8-sig 交给解码器处理）
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        return SniffResult(kind="text", text=text, variants=[text], scanned_encodings=["utf-8"])

    # 3) 无 NUL 且非 UTF-8：无法扫描 → fail-closed
    return SniffResult(
        kind="undecodable",
        reject_reason="无法以 UTF-8 解码（非 UTF-8 文本编码，密钥无法扫描）",
    )


def _comment_lines(path: Path) -> set[int] | None:
    """返回文件中「真实注释」所在的行号集合。

    `.py` 之外或解析失败时返回 ``None``，调用方退化为正则匹配。
    为什么要用 tokenize：只在行内做 `#` 正则匹配时，
    把标记写进**字符串字面量**同样能豁免（二轮审查 NON_BLOCKING-3 实测）。
    """
    if path.suffix != ".py":
        return None
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            return {
                token.start[0]
                for token in tokenize.generate_tokens(handle.readline)
                if token.type == tokenize.COMMENT
            }
    except (OSError, tokenize.TokenError, IndentationError, SyntaxError):
        return None


def scan_secrets(root: Path, files: list[Path] | None = None) -> SecretScanResult:
    """扫描待提交文件中的疑似凭据。

    返回五类信息：未豁免的发现、显式豁免项、因体积跳过的文件、
    无法解码的文本、二进制文件——后四类都进报告，
    避免"扫描了什么"与"什么没被扫到"不可见（七轮阻塞 3）。

    **对含 NUL 的文件（二进制/UTF-16/UTF-32）扫描 `variants` 里的所有还原形式**：
    只要任何一种还原里出现密钥就会被检出，因此不存在"因为被分类成二进制而漏掉密钥"
    的路径（八轮审查 BLOCKING 的最终修法）。发现项会标注命中的还原方式。
    """
    result = SecretScanResult()
    for path in files if files is not None else iter_scan_files(root):
        rel = _rel(root, path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_SCAN_BYTES:
            result.skipped_large.append(f"{rel} ({size} bytes)")
            continue

        sniffed = sniff_text(path)
        if sniffed.kind == "undecodable":
            result.undecodable.append(f"{rel}（{sniffed.reject_reason}）")
            continue
        if sniffed.kind == "binary":
            result.skipped_binary.append(f"{rel}（{sniffed.reject_reason}）")

        # 逐种还原形式扫描；注释豁免只对原始文本有意义（还原形式里行号会错位）
        for variant_index, variant_text in enumerate(sniffed.variants):
            raw_variant = variant_index == 0 and sniffed.kind == "text"
            comment_lines = _comment_lines(path) if raw_variant else None
            for lineno, line in enumerate(variant_text.splitlines(), start=1):
                if comment_lines is not None:
                    waived_line = lineno in comment_lines
                else:
                    waived_line = bool(ALLOW_MARKER_RE.search(line))
                for name, pattern in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if not match:
                        continue
                    last = match.lastindex or 0
                    # 取「最后一个捕获组」：每个模式都把凭据本身放在最后一组
                    # （URL 模式捕获口令、赋值模式捕获引号内/等号后的值）。
                    # 若只认 >= 2，URL 模式就会拿整串去比对占位符，`<password>` 这类写法会误报。
                    value = match.group(last) if last >= 1 else match.group(0)
                    if _looks_like_placeholder(value):
                        continue
                    encoding = (
                        sniffed.scanned_encodings[variant_index]
                        if variant_index < len(sniffed.scanned_encodings)
                        else "变体"
                    )
                    location = f"{rel}:{lineno}: {name}"
                    if sniffed.kind != "text":
                        location += f"（经 {encoding} 还原）"
                    if waived_line:
                        result.waived.append(location)
                    else:
                        result.findings.append(location)
    return result


def _check_syntax(root: Path, candidates: list[Path]) -> CheckResult:
    """解析文件集合中的全部 `.py`。

    范围必须与摘要/密钥扫描一致：根级 conftest.py 之类只看不查的文件
    足以让 pytest 行为改变而摘要毫无变化（二轮审查 B1）。
    """
    started = time.perf_counter()
    errors: list[str] = []
    files = 0
    for path in sorted(p for p in candidates if p.suffix == ".py"):
        files += 1
        try:
            # tokenize.open 尊重 PEP 263 编码声明，非 UTF-8 源码不会让门禁崩溃
            with tokenize.open(path) as handle:
                source = handle.read()
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{_rel(root, path)}:{exc.lineno}: {exc.msg}")
        except (UnicodeDecodeError, OSError, ValueError) as exc:
            errors.append(f"{_rel(root, path)}: 无法读取或解码 ({type(exc).__name__})")

    duration = round(time.perf_counter() - started, 3)
    if files == 0:
        return CheckResult(
            name="syntax",
            status="fail",
            detail="未找到任何 Python 文件（枚举可能失效，拒绝静默通过）",
            duration_s=duration,
        )
    detail = f"解析 {files} 个文件" if not errors else "; ".join(errors[:10])
    return CheckResult(
        name="syntax",
        status="fail" if errors else "pass",
        detail=detail,
        duration_s=duration,
    )


def _check_ruff(root: Path, timeout_s: float) -> CheckResult:
    """对整个项目跑 ruff——包括根级 conftest.py / setup.py 等自举文件。

    排除项**由 SKIP_DIRS / SKIP_DIR_SUFFIXES 派生**，而不是另写一份：范围只要出现
    第二份描述，就迟早会与摘要范围分叉（第三轮审查阻塞的成因）。
    用 `--extend-exclude` 而非 `--exclude`：后者会**替换** ruff 内置默认排除表，
    导致站内 site-packages 之类突然被 lint（四轮审查非阻塞 3）。
    """
    started = time.perf_counter()
    # 用 find_spec 判定"未安装"，而不是把退出码 127 一律当缺依赖：
    # 127 也会被子进程原样透出（例如被信号/中断打断），那会把真实失败记成 skip 而 ok=True
    # （五轮审查非阻塞 4）。
    if importlib.util.find_spec("ruff") is None:
        return CheckResult(name="ruff", status="skip", detail="ruff 未安装")

    cmd = [sys.executable, "-m", "ruff", "check", "."]
    for name in sorted(SKIP_DIRS):
        cmd += ["--extend-exclude", name]
    for suffix in sorted(SKIP_DIR_SUFFIXES):
        cmd += ["--extend-exclude", f"*{suffix}"]
    code, out = _run(cmd, root, timeout_s)
    return CheckResult(
        name="ruff",
        status="pass" if code == 0 else "fail",
        detail=(out[-1500:] if out else "无问题"),
        duration_s=round(time.perf_counter() - started, 3),
    )


def _pytest_targets(root: Path, candidates: list[Path]) -> list[str]:
    """摘要集合中的测试文件（相对 root 的 posix 路径，已排序）。

    这份清单就是"门禁要求必须被执行"的范围证据：它来自摘要覆盖的字节，
    而不是来自仓库里的 pytest 配置，所以仓库无法把自己从范围里摘出去。
    """
    targets: list[str] = []
    for path in candidates:
        if path.suffix != ".py":
            continue
        if not path.name.startswith("test_") and not path.name.endswith("_test.py"):
            continue
        targets.append(_rel(root, path))
    return sorted(targets)


def _git_climb_env(root: Path) -> dict[str, str]:
    """禁止 git 越过被审查的仓库根去认领**外层仓库**。

    为什么需要（第八轮收口时实测）：门禁自己的 basetemp 位于
    `<root>/.patchpilot/pytest-tmp/run-<pid>-<hex>`，即**在仓库内部**；
    而门禁又会以子进程方式跑仓库里的 pytest。若不做限制，
    子进程里任何"在临时目录中 git init / rev-parse"的行为都会先撞见
    被审查的仓库本身——`tmp_path` 于是看起来像"仓库子目录"，
    使 `worktree_matches_index` 的输出随**运行方式**漂移
    （普通 `pytest` 与被门禁调用的 `pytest` 结论不同 → 假失败）。

    `GIT_CEILING_DIRECTORIES` 是 git 官方手段：向上查找到此为止。
    界标取 **`root` 自身**（不是 root 的父目录）：
      - root 内的任何嵌套仓库/临时目录都不会越界认领 root；
      - 而"root 就是仓库根"这件事仍然成立（界标是 root 的**父**目录才生效，
        因此 root 自己依然被认出来是 toplevel）。
    """
    ceiling = Path(root).resolve()
    return {"GIT_CEILING_DIRECTORIES": f"{ceiling}{os.pathsep}{str(ceiling).swapcase()}"}


def _pytest_env(root: Path) -> dict[str, str]:
    """让 pytest 子进程能 import 待测项目（src 布局），并隔离外层仓库。"""
    env: dict[str, str] = {}
    src = root / "src"
    if src.is_dir():
        existing = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src) if not existing else f"{src}{os.pathsep}{existing}"
    env.update(_git_climb_env(root))
    return env


def _parse_pytest_collection(out: str) -> tuple[list[str], list[str]]:
    """从 `--collect-only -q` 输出解析 ``(实际收集到的测试文件, 收集期报错行)``。

    必须同时认两种形态（pytest 大版本间变了输出格式，实测 9.x 是第一种）：
      - `tests/test_a.py: 6`          ← 文件 + 用例数（pytest ≥ 8/9）
      - `tests/test_a.py::test_x`     ← 逐条 nodeid（旧版 -q 与默认输出）
    只取"文件"这一层：门禁要校验的是**测试文件是否被收集**，
    而不是具体用例数——文件清单才是可跨实现比较的范围证据。
    遇到分隔线（`======` 等）即停止，避免把失败报告里的文件名误当收集结果。

    **收集期错误必须与"范围被抑制"区分开**（八轮非阻塞 2）：`ERROR tests/x.py`
    是导入失败/模块级 skip 等**收集失败**，与 `collect_ignore` 掩盖用例是两回事。
    两者都 fail，但文案要指向正确的原因，否则人会去查错的配置文件。
    """
    collected: set[str] = set()
    errors: list[str] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line.startswith(("<", "=", "!", "no tests ran", "=")):
            continue
        if line.startswith(("ERROR ", "E   ")) and ".py" in line:
            errors.append(line[:160])
            continue
        head = line.split("::", 1)[0]
        count_match = re.match(r"^(?P<file>.+\.py):\s*\d+\s*$", head)
        if count_match:
            collected.add(count_match.group("file").replace("\\", "/").strip())
            continue
        head = head.strip()
        if head.endswith(".py"):
            collected.add(head.replace("\\", "/"))
    return sorted(collected), errors


def collect_pytest_scope(
    root: Path, candidates: list[Path], basetemp: Path | None, timeout_s: float
) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 ``(期望的测试文件, 实际收集到的, 缺失的, 收集期报错行)``。

    缺失非空即意味着"摘要范围内有测试文件没有被执行"——
    可能是 `collect_ignore`、`pytest.ini` 的 `addopts --ignore`、
    或配置文件里的 `testpaths`/`norecursedirs` 把自己的用例摘掉了。
    门禁必须把这种情况变成 fail：它无法区分"测试真通过"与"测试被关掉"（七轮阻塞 2）。

    第四项把"收集期报错"单独带出来，使 fail 文案能区分
    "被抑制"与"导入失败/模块级 skip"（八轮非阻塞 2）。
    """
    expected = _pytest_targets(root, candidates)
    if not expected:
        return [], [], [], []

    collect_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--collect-only",
        *expected,
    ]
    if basetemp:
        collect_cmd += ["--basetemp", str(basetemp)]
    _code, out = _run(collect_cmd, root, timeout_s, extra_env=_pytest_env(root))
    collected, collect_errors = _parse_pytest_collection(out)
    missing = [target for target in expected if target not in set(collected)]
    return expected, collected, missing, collect_errors


def _check_pytest(
    root: Path,
    basetemp: Path | None,
    timeout_s: float,
    candidates: list[Path],
) -> tuple[CheckResult, tuple[list[str], list[str], list[str]]]:
    """跑 pytest，并把「执行范围」钉在摘要集合上。

    两件事缺一不可：
      1. **显式传入**摘要集合中的 `test_*.py` → 收集范围由门禁决定，
         而不是由仓库的 conftest/pytest.ini 决定；
      2. **`--collect-only` 取实际收集清单并要求全覆盖** → 若仍有文件没被收集
         （第二道防线），直接 fail 并把缺失清单写进报告。

    返回值第二项是范围证据，最终进入 PreflightReport 供审查者核对。
    """
    empty_scope: tuple[list[str], list[str], list[str]] = ([], [], [])
    started = time.perf_counter()
    if importlib.util.find_spec("pytest") is None:
        return CheckResult(name="pytest", status="skip", detail="pytest 未安装"), empty_scope

    expected = _pytest_targets(root, candidates)
    if not expected:
        # 一个测试文件都没有：不做静默通过——那正是"什么都没跑"被当成"跑过了"的形态
        return (
            CheckResult(
                name="pytest",
                status="fail",
                detail=(
                    f"摘要范围内没有任何 {TEST_FILE_GLOB}（枚举可能失效，"
                    "拒绝静默通过）"
                ),
            ),
            empty_scope,
        )

    # basetemp 必须**每次门禁运行独占**：默认的 `<root>/.patchpilot/pytest-tmp` 会让
    # 两个并发的门禁（例如审查者跑 `pp check` 的同时开发者也跑一次）互相删掉对方的
    # 临时目录，表现为大量 `tmp_path` 夹具 `FileNotFoundError` 的**假失败**。
    # 实测：并发时 `pytest: fail`，报错发生在夹具建目录时，与用例逻辑无关。
    base_parent = Path(basetemp) if basetemp else root / ".patchpilot" / "pytest-tmp"
    target = base_parent / f"run-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    target.mkdir(parents=True, exist_ok=True)

    # 把"不得越过仓库根认领外层仓库"写进本进程环境：pytest 子进程与其调用链
    # （含仓库自己那些"在临时目录里 git init"的用例）都会继承它。
    # 不这样做时，同一棵树上的用例结论会随"谁在调用 pytest"而漂移
    # （门禁调用 vs 开发者手跑 → 同一用例一红一绿），属实测到的假失败来源。
    os.environ.update(_git_climb_env(root))

    # 先收集、后执行：收集范围是"这次要不要信 pass"的前提，必须先拿到
    _expected, collected, missing, collect_errors = collect_pytest_scope(
        root, candidates, target, timeout_s
    )
    scope = (_expected, collected, missing)

    code, out = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--basetemp",
            str(target),
            *expected,
        ],
        root,
        timeout_s,
        extra_env=_pytest_env(root),
    )

    duration = round(time.perf_counter() - started, 3)
    lowered = out.lower()
    # 只匹配**异常类型串**，不要放开成人类可读的 "access is denied"：
    # 一个真实断言失败若在输出里打印了这句话（例如断言消息里引用了它），
    # 就会被改写成"环境问题"而误导诊断（八轮非阻塞 3）。
    environment_issue = (
        "permissionerror" in lowered
        or "winerror 5" in lowered
        or "internalerror" in lowered
    )
    if environment_issue:
        return (
            CheckResult(
                name="pytest",
                status="fail",
                detail="环境问题：测试临时目录不可写（非用例失败）\n" + out[-1000:],
                duration_s=duration,
            ),
            scope,
        )

    if missing:
        # 收集期报错（导入失败 / 模块级 skip / conftest 出错）与"范围被抑制"是两回事，
        # 都 fail 但文案必须指向正确的原因（八轮非阻塞 2）
        if collect_errors:
            detail = (
                f"pytest 收集期报错：{len(missing)} 个测试文件未被收集 "
                f"{missing[:5]}；收集输出含 {len(collect_errors)} 条错误行："
                f"{collect_errors[:2]}（多半是导入失败/conftest 报错/模块级 skip，"
                "不是被 collect_ignore 抑制）\n"
            )
        else:
            detail = (
                f"执行范围与摘要不一致：{len(missing)} 个测试文件未被收集 "
                f"{missing[:5]}（可能是 collect_ignore / addopts --ignore / "
                "testpaths 把用例摘掉了）；实际收集 "
                f"{len(collected)}/{len(expected)}\n"
            )
        return (
            CheckResult(
                name="pytest", status="fail", detail=detail + out[-600:], duration_s=duration
            ),
            scope,
        )

    detail = out[-1500:] if out else ""
    return (
        CheckResult(
            name="pytest",
            status="pass" if code == 0 else "fail",
            detail=f"范围证据：收集 {len(collected)}/{len(expected)} 个测试文件全覆盖\n{detail}",
            duration_s=duration,
        ),
        scope,
    )


def _gitignore_entries(root: Path) -> set[str]:
    """读取 .gitignore 的有效条目（去注释、去空行、去前导斜杠）。"""
    path = root / ".gitignore"
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lstrip("/")
        if line:
            entries.add(line)
    return entries


_GLOB_CLASS_RE = re.compile(r"\[([^\]]+)\]")


def _expand_simple_glob(entry: str, *, limit: int = 32) -> list[str]:
    """展开**单层**方括号类，如 `*.py[cod]` -> `*.pyc` / `*.pyo` / `*.pyd`。

    只展开第一个类且最多 `limit` 项；多级方括号（`*.[cd][ab]`）、`?`、
    `[ab]uild/` 这类写法**不展开**，于是会被判定为「未覆盖」而让门禁 fail——
    这是刻意的保守方向：宁可要求人工确认，也不放行无法证明等价的范围。
    """
    match = _GLOB_CLASS_RE.search(entry)
    if not match:
        return [entry]
    expanded: list[str] = []
    for char in match.group(1):
        expanded.append(entry[: match.start()] + char + entry[match.end() :])
        if len(expanded) >= limit:
            break
    return expanded


def _covered_by_skip_rules(entry: str) -> bool:
    """正向判断：某条 .gitignore 条目是否被 SKIP 规则覆盖。

    目录条目只比目录后缀，文件条目只比文件后缀——混用会让 `.egg-link`
    这类**文件**被目录后缀规则误判为已覆盖（四轮审查非阻塞 1）。
    """
    text = entry.strip().lstrip("/")
    if not text:
        return True
    if text.endswith("/"):
        name_key = _key(text.rstrip("/"))
        return name_key in _SKIP_DIRS_KEY or name_key.endswith(_SKIP_DIR_SUFFIXES_KEY)
    for candidate in _expand_simple_glob(text):
        candidate_key = _key(candidate)
        if candidate_key in _SKIP_FILES_KEY or candidate_key.endswith(_SKIP_FILE_SUFFIXES_KEY):
            return True
    return False


def _gitignore_names(entries: set[str]) -> tuple[set[str], set[str]]:
    """把 .gitignore 条目归成 (精确名集合, 通配后缀集合)，均按平台归一化。"""
    exact: set[str] = set()
    suffixes: set[str] = set()
    for entry in entries:
        text = _key(entry.strip().lstrip("/").rstrip("/"))
        if not text:
            continue
        for candidate in _expand_simple_glob(text):
            if candidate.startswith("*"):
                suffixes.add(candidate[1:])
            else:
                exact.add(candidate)
    return exact, suffixes


def _check_skip_rules(root: Path) -> CheckResult:
    """门禁自检：`.gitignore` 与 SKIP 规则必须**双向**一致。

    正向（.gitignore ⊆ SKIP）：三轮阻塞的 `venv/` —— git 忽略而遍历收录，
    同一份内容两处算出不同摘要，批准绑定失效。

    反向（SKIP ⊆ .gitignore）：四轮阻塞 —— `node_modules` 等 SKIP 独有项目
    git **并不忽略**，于是"会被提交的字节"既不在摘要也不在密钥扫描范围内。
    实测：`node_modules/pkg/index.py` 里一条 `sk-` 密钥可被提交，而门禁报 `ok=True`。

    这类「两套忽略规则」必须由门禁自己抓住，而不是靠人记得同步两份清单。
    """
    entries = _gitignore_entries(root)
    if not entries:
        return CheckResult(name="skip_rules", status="skip", detail="无 .gitignore 条目")

    exact, suffixes = _gitignore_names(entries)
    problems: list[str] = []

    for entry in sorted(entries):
        if not _covered_by_skip_rules(entry):
            problems.append(f"{entry}（.gitignore 条目未被 SKIP 规则覆盖）")

    for name in sorted(SKIP_DIRS - {".git"}):
        if _key(name) not in exact:
            problems.append(f"{name}（SKIP_DIRS 独有：git 不会忽略，会进提交）")
    for suffix in sorted(set(SKIP_DIR_SUFFIXES) | set(SKIP_FILE_SUFFIXES)):
        suffix_key = _key(suffix)
        if suffix_key not in suffixes and suffix_key not in exact:
            problems.append(f"{suffix}（SKIP 后缀独有：git 不会忽略，会进提交）")
    for name in sorted(SKIP_FILES):
        if _key(name) not in exact:
            problems.append(f"{name}（SKIP_FILES 独有：git 不会忽略，会进提交）")

    if problems:
        return CheckResult(
            name="skip_rules",
            status="fail",
            detail=(
                ".gitignore 与 SKIP 规则不一致，摘要将不再与位置无关、"
                "或会放过可提交的字节: " + "; ".join(problems[:12])
            ),
        )
    return CheckResult(
        name="skip_rules",
        status="pass",
        detail=f"{len(entries)} 条 .gitignore 条目与 SKIP 规则双向一致",
    )


def _check_scope_agreement(root: Path) -> CheckResult:
    """展示 git 集合与遍历集合的差异（**提示性，不阻塞**）。

    摘要已取交集，所以差异本身不影响正确性；但如果两集合长期不一致，
    说明本地忽略来源与 SKIP 规则已漂移，值得人看一眼。

    状态用 ``"diff"`` 而不是 ``"pass"``：两集合确有差异时仍报 pass
    会与 detail 文案自相矛盾（七轮非阻塞 1）。`"diff"` 既不在 ``"fail"``
    之列（不影响 ``ok``），也不再冒充"完全一致"。
    """
    if not (root / ".git").exists():
        return CheckResult(
            name="scope_agreement", status="skip", detail="非 git 仓库：仅用遍历集合"
        )
    git_only, walk_only = scan_scope_diff(root)
    if not git_only and not walk_only:
        return CheckResult(
            name="scope_agreement", status="pass", detail="git 集合与遍历集合完全一致"
        )
    return CheckResult(
        name="scope_agreement",
        status="diff",
        detail=(
            f"两集合有差异（摘要已取交集，仍与位置无关）："
            f"仅 git={git_only[:4]} 仅遍历={walk_only[:4]}"
        ),
    )


def _check_git_scope(root: Path) -> CheckResult:
    """显式报告枚举走的是 git 路径还是目录遍历，及其原因（七轮非阻塞 2）。

    旧实现里 `_git_files` 在 git 命令非 0 时静默退化为遍历，
    于是"git 不可用""仓库损坏""root 只是子目录"三种完全不同的处境
    在报告里长得一模一样——出问题的人无从判断该修哪一个。

    判据分三类，**只有中间一类阻塞**：
      - root 就是仓库根 → pass；
      - root 在仓库内但**不是仓库根**（子目录）→ **fail**：索引绑定不成立，
        而"门禁全绿"的读数会让人以为子树批准可以发布（八轮 BLOCKING）；
      - root 根本不在任何仓库里（普通目录）→ pass（合法用法：非仓库目录没有
        索引绑定可言，`worktree_matches_index` 会如实 skip）。
    """
    scope_ok, reason = git_scope_status(root)
    if scope_ok:
        return CheckResult(name="git_scope", status="pass", detail=reason)
    if find_toplevel(root) is None:
        return CheckResult(
            name="git_scope",
            status="pass",
            detail=f"非 git 仓库，按目录遍历枚举：{reason}",
        )
    return CheckResult(
        name="git_scope",
        status="fail",
        detail=(
            f"退化为目录遍历：{reason}。"
            "本目录不是仓库根，索引绑定不成立——请对仓库根运行，或在仓库根复算摘要"
        ),
    )


def _bulk_blob_hashes(root: Path, rel_paths: list[str], timeout_s: float) -> dict[str, str]:
    """用**一次** `git hash-object --stdin-paths --no-filters` 取全部文件的原始字节哈希。

    批量化的理由有二：
      - 每个文件一次子进程在本仓库是 36 次，文件多了会拖垮门禁（七轮非阻塞 4）；
      - 一次调用天然给出**完整**的枚举结果，不可能出现"部分文件没被比较"的中间态。
    """
    if not rel_paths:
        return {}
    code, out = _run(
        ["git", "hash-object", "--stdin-paths", "--no-filters"],
        root,
        timeout_s,
        stdin="\n".join(rel_paths) + "\n",
    )
    if code != 0:
        return {}
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    # 行数不符即视为"无法证明一致"，调用方按全部文件不一致处理（保守失败）
    if len(lines) != len(rel_paths):
        return {}
    return dict(zip(rel_paths, lines, strict=False))


def _check_worktree_matches_index(root: Path, candidates: list[Path]) -> CheckResult:
    """校验「摘要绑定的字节」==「将被提交的索引字节」，且**覆盖枚举**。

    起因（六轮审查阻塞 2）：`.gitattributes` 里的 `text`/`eol` 会引入 clean 过滤器，
    使工作区字节 ≠ 将被提交的字节，而 `TREE_DIGEST` 按工作区字节计算。
    git 自己把二者视为相同（`git diff` 干净、`hash-object --path` 也相等），
    所以门禁与发布者复算**都发现不了** —— 批准绑定在结构上不成立。

    七轮阻塞 1 又指出旧实现的两个缺口，本版一并修掉：
      1. **只覆盖索引、不覆盖枚举**。旧实现遍历 `git ls-files -s` 的索引条目，
         而摘要覆盖 `iter_scan_files`（含未跟踪未忽略文件）。实测：索引干净时
         仅新增**未跟踪**的 CRLF 文件 → 该项报 pass、摘要与索引都变了却无人发现。
         现在以**枚举集合**为准：每个文件都要在索引里且 blob 一致；
         凡是不在索引里的（未跟踪、被排除、或尚未 `git add`）一律 fail。
      2. **索引为空时 `skip`**。此时摘要完全无绑定，等于"批准了一个不会被提交的东西"。
         现在空索引是 fail，并明文要求先 `git add`。

     **第八轮 BLOCKING（子目录 root 静默丢失绑定）**：判定顺序曾经是"先查 `root/.git`
     是否存在"，于是 `pp check --root <仓库子目录>` 直接得到 `skip`（detail 还写着
     "非 git 仓库"，而它明明在仓库里）——**永远走不到下面的 fail 分支**，
     且 `ok=True`（子树自带合格 `.gitignore` 与可通过的测试时）。
     后果：拿到的是一份只覆盖子树的批准，而 commit 会提交**整个仓库**，
     索引/范围一致性校验一条都没做。"root 是仓库子目录"与"根本不在仓库里"
     必须区分：前者**必须 fail**（请对仓库根运行），只有后者才允许 skip。
    """
    # 顺序很重要：先问"是不是仓库、是不是仓库根"，再决定 skip 还是 fail。
    # 只调用一次 `git_scope_status`（两次调用在 git 不可用时结果可能不同）。
    scope_ok, reason = git_scope_status(root)
    top = find_toplevel(root)
    if not scope_ok and top is None:
        # 真的不在任何仓库里 → 索引绑定不适用，skip 是诚实的
        return CheckResult(
            name="worktree_matches_index", status="skip", detail="非 git 仓库"
        )
    if not scope_ok:
        # 在仓库里、但 root 不是仓库根（子目录）→ **必须 fail**：
        # 此时摘要只覆盖子树，而 commit 提交整个仓库，索引绑定无从谈起
        return CheckResult(
            name="worktree_matches_index", status="fail", detail=f"无法确定索引范围：{reason}"
        )

    rels = sorted(_rel(root, path) for path in candidates)
    if not rels:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail="摘要集合为空：没有任何字节被绑定，拒绝静默通过",
        )

    code, out = _run(["git", "ls-files", "-s", "-z"], root, 90)
    if code != 0:
        return CheckResult(
            name="worktree_matches_index", status="fail", detail="git ls-files -s 失败"
        )

    staged: dict[str, str] = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) >= 2 and path:
            staged[path.replace("\\", "/")] = fields[1]
    if not staged:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail=(
                f"索引为空，而摘要覆盖 {len(rels)} 个文件——批准未绑定到任何将被提交的字节。"
                "请先 `git add -A` 再复算摘要"
            ),
        )

    # 方向一：摘要范围内的文件必须全部在索引里（未跟踪/未暂存/被排除 → fail）
    untracked = [rel for rel in rels if rel not in staged]
    if untracked:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail=(
                f"{len(untracked)} 个摘要范围内的文件不在索引中"
                f"（未被暂存或已被排除，git 不会提交这些字节）: {untracked[:5]}"
            ),
        )

    # 方向二：索引里的文件必须在工作区枚举中（否则它照样进提交，却不在摘要里）。
    # 判决性实测（`git init` + 写文件 + `git add -A` + 删除工作区文件**而不暂存删除**）：
    # `git status` 显示 `AD file`，`git ls-tree HEAD` 里**该文件仍然存在** ——
    # 即"提交里带着一份从未被摘要与密钥扫描覆盖的字节"，正是七轮阻塞 1 的同一类缺陷；
    # 补一次 `git add -A`（变为 `A file`）之后它才真正从提交中消失。
    removed = [rel for rel in staged if rel not in set(rels)]
    if removed:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail=(
                f"{len(removed)} 个已暂存文件在工作区已不存在"
                f"（其字节仍会进提交，但不在摘要范围内——请 `git add -A` 暂存删除）: "
                f"{removed[:5]}"
            ),
        )

    hashes = _bulk_blob_hashes(root, rels, 120)
    if not hashes:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail="git hash-object 批量取值失败或结果行数不符，无法证明字节一致",
        )

    mismatched = [rel for rel in rels if hashes.get(rel) != staged.get(rel)]
    if mismatched:
        return CheckResult(
            name="worktree_matches_index",
            status="fail",
            detail=(
                f"{len(mismatched)} 个文件的工作区字节与索引不一致"
                f"（摘要绑定的是不会被提交的字节，常见于 .gitattributes 的 eol 过滤）: "
                f"{mismatched[:5]}"
            ),
        )
    return CheckResult(
        name="worktree_matches_index",
        status="pass",
        detail=f"摘要范围内 {len(rels)} 个文件的工作区字节与索引 blob 逐一一致",
    )


def _check_gitignore(root: Path) -> CheckResult:
    if not (root / ".gitignore").exists():
        return CheckResult(name="gitignore", status="fail", detail=".gitignore 不存在")

    # 优先用 git 的权威判定（处理 .env vs .env.example 这类差异）
    if (root / ".git").exists():
        missing: list[str] = []
        fallback = False
        for label, probe in REQUIRED_IGNORES.items():
            code, _out = _run(["git", "check-ignore", "-q", probe], root, 30)
            if code == 0:
                continue
            if code == 1:
                missing.append(label)
            else:
                fallback = True
                break
        if not fallback:
            return CheckResult(
                name="gitignore",
                status="fail" if missing else "pass",
                detail=(
                    "以下条目未被忽略: " + ", ".join(missing)
                    if missing
                    else "git check-ignore 判定：必需条目齐全"
                ),
            )

    entries = _gitignore_entries(root)
    missing = [label for label in REQUIRED_IGNORES if label not in entries]
    return CheckResult(
        name="gitignore",
        status="fail" if missing else "pass",
        detail=(
            "以下条目未被忽略: " + ", ".join(missing)
            if missing
            else "按条目精确匹配：必需条目齐全"
        ),
    )


def _check_env_not_tracked(root: Path) -> CheckResult:
    if not (root / ".git").exists():
        return CheckResult(name="env_not_tracked", status="skip", detail="非 git 仓库")
    code, out = _run(["git", "ls-files", ".env"], root, 30)
    if code != 0:
        return CheckResult(name="env_not_tracked", status="skip", detail="git 查询失败")
    if out.strip():
        return CheckResult(name="env_not_tracked", status="fail", detail=".env 已被跟踪")
    return CheckResult(name="env_not_tracked", status="pass", detail=".env 未被跟踪")


def _guarded(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """门禁绝不允许崩溃：任何异常都转成 fail 结果，保住 --json 契约。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - 兜底是这里的全部目的
        return CheckResult(
            name=name,
            status="fail",
            detail=f"检查执行异常: {type(exc).__name__}: {exc}",
        )


def _check_secrets(root: Path, targets: list[Path]) -> tuple[CheckResult, SecretScanResult]:
    """密钥扫描 + **不可解码的文本即 fail**（七轮阻塞 3）。

    fail 的范围刻意限定在"**可能是文本但扫不了**"（`undecodable`）：
    密钥大概率就藏在里面，不能静默放行。二进制文件另行记入 `skipped_binary_files`，
    detail 里也会写明"N 个二进制文件未按文本扫描"——**不阻塞**，
    否则仓库里放一张 PNG 就会让门禁变红，那与密钥泄露无关。
    两者都必须出现在报告里：**"未发现密钥"与"扫不了"是可区分的两种状态**。
    """
    result = scan_secrets(root, targets)
    parts: list[str] = []
    if result.findings:
        parts.append("; ".join(result.findings[:10]))
    else:
        parts.append("未发现疑似凭据")
    if result.undecodable:
        parts.append(
            f"{len(result.undecodable)} 个文件无法以 UTF-8/UTF-16 解码，"
            f"密钥无法被扫描（见 undecodable_files）: {result.undecodable[:3]}"
        )
    if result.waived:
        parts.append(f"已豁免 {len(result.waived)} 项（见 waived_secrets）")
    if result.skipped_large:
        parts.append(f"因体积跳过 {len(result.skipped_large)} 个文件（见 skipped_large_files）")
    if result.skipped_binary:
        parts.append(
            f"二进制文件 {len(result.skipped_binary)} 个未按文本扫描（见 skipped_binary_files）"
        )
    return (
        CheckResult(
            name="secret_scan",
            status="fail" if (result.findings or result.undecodable) else "pass",
            detail=" | ".join(parts),
        ),
        result,
    )


def run_preflight(
    root: Path,
    *,
    run_tests: bool = True,
    timeout_s: float = 900.0,
    basetemp: Path | None = None,
) -> PreflightReport:
    """执行全部确定性检查。ok 为 False 时禁止上传。"""
    root = Path(root).resolve()

    # 枚举与摘要在所有 _guarded 之外，因此必须自己兜底：
    # 目录树里出现不可读目录时，异常绝不能逃逸成 traceback（二轮审查 B3）
    try:
        targets = iter_scan_files(root)
        digest = compute_tree_digest(root, targets)
    except Exception as exc:  # noqa: BLE001
        return PreflightReport(
            ok=False,
            root=str(root),
            checks=[
                CheckResult(
                    name="enumerate",
                    status="fail",
                    detail=f"枚举/摘要失败: {type(exc).__name__}: {exc}",
                )
            ],
        )

    checks: list[CheckResult] = [
        _guarded("syntax", lambda: _check_syntax(root, targets))
    ]

    pytest_scope: tuple[list[str], list[str], list[str]] = ([], [], [])
    if run_tests:
        checks.append(_guarded("ruff", lambda: _check_ruff(root, timeout_s)))
        # pytest 的执行范围必须钉在摘要集合上；异常也不允许逃逸（兜底为空范围）
        try:
            pytest_check, pytest_scope = _check_pytest(root, basetemp, timeout_s, targets)
        except Exception as exc:  # noqa: BLE001 - 门禁绝不允许崩溃
            pytest_check = CheckResult(
                name="pytest",
                status="fail",
                detail=f"检查执行异常: {type(exc).__name__}: {exc}",
            )
        checks.append(pytest_check)
    else:
        checks.append(CheckResult(name="ruff", status="skip", detail="按参数跳过"))
        checks.append(CheckResult(name="pytest", status="skip", detail="按参数跳过"))

    try:
        secret_check, scan = _check_secrets(root, targets)
    except Exception as exc:  # noqa: BLE001 - 门禁绝不允许崩溃
        secret_check = CheckResult(
            name="secret_scan",
            status="fail",
            detail=f"检查执行异常: {type(exc).__name__}: {exc}",
        )
        scan = SecretScanResult()
    checks.append(secret_check)

    checks.append(_guarded("gitignore", lambda: _check_gitignore(root)))
    checks.append(_guarded("skip_rules", lambda: _check_skip_rules(root)))
    checks.append(_guarded("scope_agreement", lambda: _check_scope_agreement(root)))
    checks.append(_guarded("git_scope", lambda: _check_git_scope(root)))
    checks.append(
        _guarded(
            "worktree_matches_index", lambda: _check_worktree_matches_index(root, targets)
        )
    )
    checks.append(_guarded("env_not_tracked", lambda: _check_env_not_tracked(root)))

    pytest_expected, pytest_collected, pytest_missing = pytest_scope
    return PreflightReport(
        ok=all(c.status != "fail" for c in checks),
        root=str(root),
        tree_digest=digest,
        file_count=len(targets),
        checks=checks,
        secrets=scan.findings,
        waived_secrets=scan.waived,
        skipped_large_files=scan.skipped_large,
        undecodable_files=scan.undecodable,
        skipped_binary_files=scan.skipped_binary,
        pytest_expected_files=pytest_expected,
        pytest_collected_files=pytest_collected,
        pytest_missing_files=pytest_missing,
    )
