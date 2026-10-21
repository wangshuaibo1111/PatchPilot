"""把模型输出落成文件改动：unified diff 优先，失败则用 **search/replace 编辑块**。

## 为什么需要它（真机数据）

SWE-bench 子集里曾出现 9/11 题"候选补丁无法应用"——不是模型没想对，而是裸 unified diff
带不上大文件里的精确上下文。Aider 为此专门提供 `diff`（search/replace 块）格式，并指出
裸 unified diff 失败率更高
（https://aider.chat/docs/more/edit-formats.html 、
https://aider.chat/docs/troubleshooting/edit-errors.html ）。

## 格式（与 Aider 兼容）

```
path/to/file.py
<<<<<<< SEARCH
要替换掉的原样内容
=======
替换成的内容
>>>>>>> REPLACE
```

## 落盘契约（第三十三轮复审 BLOCKING 后重写）

复审实测旧实现有四类**静默数据损坏**，因此这里的规则是硬性的：

1. **整行匹配，不做子串匹配**。旧实现用 `text.find(search)`，会命中行内片段：
   `"    return 10"` 里搜 `"return 1"` 会写出 `"    return 20"`。
2. **不吞换行**。按行数组替换，替换后换行符由周围行决定；旧实现的回退分支会把
   `GAMMA\ndelta` 变成 `GAMMAdelta`。
3. **读写同字节域**：`read_bytes()` → `decode("utf-8")`（失败则**拒绝该块**，绝不 `errors="replace"`
   写入），写回时**保留原有行尾风格**（CRLF 文件仍是 CRLF）。旧实现 `read_text` 归一换行、
   `write_bytes` 整文件写回，会让一次单点编辑把整个 CRLF 文件变成 LF。
4. **必须唯一命中**：0 处或 >1 处都报错并给出候选行号（Aider 的唯一性约定）；
   纯空白搜索块直接拒绝（旧实现会插进文件第一个空行）。
5. **多块原子**：先把所有改动在内存里算完，全部成功才落盘；任一失败则**一字不写**。
6. **落盘前语法自检**：`.py` 文件改完后必须能 `ast.parse`，否则拒绝该次改动。
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SEARCH_MARK = "<<<<<<<"
_DIVIDER = "======="
_REPLACE_MARK = ">>>>>>>"

#: 锚定到行首的 unified diff 头（避免把编辑块里的 `--- ` / `+++ ` 误判成 diff）
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


class EditError(RuntimeError):
    """编辑块解析或应用失败。"""


@dataclass(frozen=True, slots=True)
class EditBlock:
    """一个 search/replace 块。"""

    path: str
    search: str
    replace: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """一次应用尝试的结果（**可审计**：用哪种方式成功/为什么失败）。"""

    applied: bool
    method: str = ""
    detail: str = ""
    files: tuple[str, ...] = ()
    #: 每个块的结果：`(路径, 命中方式, 行区间)`，便于事后核查是否改对了地方
    applied_blocks: tuple[tuple[str, str, str], ...] = ()


def parse_edit_blocks(text: str) -> list[EditBlock]:
    """解析 Aider 风格的 search/replace 块（容忍前后杂文本）。"""
    blocks: list[EditBlock] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith(_SEARCH_MARK):
            index += 1
            continue
        path = ""
        for back in range(index - 1, -1, -1):
            candidate = lines[back].strip().strip("`").strip()
            if not candidate or candidate.startswith(_REPLACE_MARK):
                continue
            path = candidate
            break
        search: list[str] = []
        replace: list[str] = []
        state = "search"
        index += 1
        closed = False
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped == _DIVIDER and state == "search":
                state = "replace"
                index += 1
                continue
            if stripped.startswith(_REPLACE_MARK):
                closed = True
                index += 1
                break
            (search if state == "search" else replace).append(lines[index])
            index += 1
        if path and closed:
            blocks.append(
                EditBlock(path=path, search="\n".join(search), replace="\n".join(replace))
            )
    return blocks


# ---------------------------------------------------------------- 行域操作


@dataclass(frozen=True, slots=True)
class _Lines:
    """按行保存的文本：`bodies` 是不含换行的行内容，`ends` 是各自的原始行尾。"""

    bodies: tuple[str, ...]
    ends: tuple[str, ...]

    @property
    def text(self) -> str:
        return "".join(body + end for body, end in zip(self.bodies, self.ends, strict=True))

    @property
    def default_end(self) -> str:
        """文件的主导行尾（用于插入新行）。"""
        for end in self.ends:
            if end:
                return end
        return "\n"


def _split_lines(raw: str) -> _Lines:
    bodies: list[str] = []
    ends: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\r":
            if index + 1 < len(raw) and raw[index + 1] == "\n":
                bodies.append("".join(current))
                ends.append("\r\n")
                current = []
                index += 2
                continue
            bodies.append("".join(current))
            ends.append("\r")
            current = []
            index += 1
            continue
        if char == "\n":
            bodies.append("".join(current))
            ends.append("\n")
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if current:
        bodies.append("".join(current))
        ends.append("")
    return _Lines(bodies=tuple(bodies), ends=tuple(ends))


def _needle_lines(search: str) -> list[str]:
    return [body for body in _split_lines(search).bodies]


def _find_matches(haystack: _Lines, needle: list[str]) -> tuple[list[int], str]:
    """返回 `(行号列表, 命中方式)`：先精确匹配行内容，再退化到"忽略行尾空白"。"""
    if not needle:
        return [], "empty"
    size = len(needle)
    exact: list[int] = []
    loose: list[int] = []
    trimmed = [row.rstrip() for row in needle]
    for start in range(len(haystack.bodies) - size + 1):
        window = haystack.bodies[start : start + size]
        if list(window) == needle:
            exact.append(start)
            continue
        if [row.rstrip() for row in window] == trimmed:
            loose.append(start)
    if exact:
        return exact, "exact"
    return loose, "whitespace-tolerant"


def _replace_lines(
    haystack: _Lines, start: int, size: int, replace: str
) -> _Lines:
    """用 `replace` 的内容替换 `[start, start+size)` 行，行尾风格向周围看齐。"""
    new_lines = _split_lines(replace)
    context_end = haystack.ends[start + size - 1] if haystack.ends else "\n"
    fallback_end = context_end or haystack.default_end
    bodies = list(haystack.bodies[:start]) + list(new_lines.bodies) + list(
        haystack.bodies[start + size :]
    )
    ends: list[str] = []
    for offset, _body in enumerate(new_lines.bodies):
        original_end = new_lines.ends[offset]
        ends.append(original_end if original_end else fallback_end)
    merged_ends = list(haystack.ends[:start]) + ends + list(haystack.ends[start + size :])
    # 末尾若原本没有换行，替换后也不该凭空加一个
    if haystack.ends and haystack.ends[-1] == "" and merged_ends:
        last_index = len(merged_ends) - 1
        if merged_ends[last_index] == "\n" or merged_ends[last_index] == "\r\n":
            merged_ends[last_index] = ""
    return _Lines(bodies=tuple(bodies), ends=tuple(merged_ends))


# ---------------------------------------------------------------- 应用


#: 模型偶尔把路径包在这些字符里（真机见过 `<sympy/concrete/summations.py>`）
_PATH_WRAPPERS = ("<", ">", "`", '"', "'", "*")


def normalize_path(rel: str) -> str:
    """把模型给的路径归一成仓库相对路径（去包裹符、去 `./`、统一分隔符）。"""
    cleaned = rel.strip()
    for char in _PATH_WRAPPERS:
        cleaned = cleaned.replace(char, "")
    cleaned = cleaned.strip().strip(":").strip()
    cleaned = cleaned.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _resolve_target(root: Path, rel: str) -> tuple[Path | None, str]:
    base = root.resolve()
    cleaned = normalize_path(rel)
    if not cleaned:
        return None, f"{rel}: 路径为空"
    target = (base / cleaned).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None, f"{rel}: 路径逃逸出仓库"
    if not target.is_file():
        name = Path(cleaned).name
        stem = Path(cleaned).stem
        # 先按完整基名找；找不到再按"词干前缀"找（真机案例：c.py 实际叫 ccode.py）
        candidates = [
            path
            for path in base.rglob("*.py")
            if path.is_file() and ".git" not in path.parts
        ]
        exact = [path for path in candidates if path.name == name]
        near = [
            path
            for path in candidates
            if not exact and stem and path.stem.startswith(stem)
        ]
        siblings = sorted(
            path.relative_to(base).as_posix() for path in (exact or near)
        )[:5]
        hint = f"（同名文件: {', '.join(siblings)}）" if siblings else ""
        return None, f"{rel}: 文件不存在{hint}"
    return target, ""


def apply_edit_blocks(
    root: Path, blocks: list[EditBlock], *, require_valid_python: bool = True
) -> tuple[int, list[str], list[tuple[str, str, str]]]:
    """应用编辑块：**全成功才落盘**。返回 `(成功块数, 错误列表, 已应用明细)`。"""
    base = Path(root).resolve()
    pending: dict[Path, str] = {}
    done: list[tuple[str, str, str]] = []
    errors: list[str] = []
    for block in blocks:
        target, problem = _resolve_target(base, block.path)
        if target is None:
            errors.append(problem)
            continue
        raw = pending.get(target)
        if raw is None:
            data = target.read_bytes()
            try:
                raw = data.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"{block.path}: 不是合法 UTF-8，拒绝编辑（不做字节替换）")
                continue
        needle = _needle_lines(block.search)
        if not needle or all(not row.strip() for row in needle):
            errors.append(f"{block.path}: 搜索块为空或纯空白，拒绝应用")
            continue
        haystack = _split_lines(raw)
        matches, method = _find_matches(haystack, needle)
        if not matches:
            head = needle[:2]
            errors.append(f"{block.path}: 搜索块未匹配（前两行 {head!r}）")
            continue
        if len(matches) > 1:
            where = ", ".join(str(start + 1) for start in matches[:5])
            errors.append(f"{block.path}: 搜索块命中 {len(matches)} 处（行 {where}），要求唯一")
            continue
        start = matches[0]
        updated = _replace_lines(haystack, start, len(needle), block.replace)
        text = updated.text
        if require_valid_python and target.suffix == ".py":
            try:
                ast.parse(text)
            except SyntaxError as exc:
                errors.append(
                    f"{block.path}: 改动后无法解析（{exc.msg}，行 {exc.lineno}），拒绝落盘"
                )
                continue
        pending[target] = text
        span = f"{start + 1}-{start + len(needle)}"
        done.append((block.path, method, span))
    if errors:
        # 原子性：只要有失败，就一字不写
        return 0, errors, []
    for target, text in pending.items():
        target.write_bytes(text.encode("utf-8"))
    return len(done), [], done


def _looks_like_diff(text: str) -> bool:
    """行锚定判断是不是 unified diff（编辑块里也可能出现 `--- ` 行）。"""
    return bool(_DIFF_HUNK.search(text))


def _git_apply(root: Path, patch: str, *, extra: tuple[str, ...] = ()) -> tuple[bool, str]:
    argv = ["git", "apply", *extra, "-"]
    proc = subprocess.run(
        argv,
        cwd=root,
        input=patch.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return proc.returncode == 0, stderr


def _touched_files(patch: str) -> tuple[str, ...]:
    """从补丁里取受影响文件（`--- a/x` / `+++ b/x` / `rename to x`）。"""
    names: list[str] = []
    for line in patch.splitlines():
        for prefix in ("--- ", "+++ "):
            if line.startswith(prefix):
                name = line[len(prefix) :].strip()
                if name and name != "/dev/null":
                    names.append(name[2:] if name.startswith(("a/", "b/")) else name)
        if line.startswith("rename to "):
            names.append(line[len("rename to ") :].strip())
    return tuple(dict.fromkeys(names))


def apply_model_output(
    root: Path, output: str, *, patch: str | None = None
) -> ApplyResult:
    """把模型输出落成改动：unified diff（严格 → `-C1`）→ search/replace 块。"""
    candidate = patch if patch is not None else output
    attempts: list[str] = []

    if candidate and _looks_like_diff(candidate):
        ok, stderr = _git_apply(root, candidate)
        if ok:
            return ApplyResult(
                applied=True, method="git-apply", files=_touched_files(candidate)
            )
        attempts.append(f"git apply 失败: {stderr.splitlines()[-1] if stderr else '无输出'}")
        ok, stderr = _git_apply(root, candidate, extra=("-C1",))
        if ok:
            return ApplyResult(
                applied=True,
                method="git-apply -C1",
                files=_touched_files(candidate),
                detail="严格模式失败后用 -C1 成功",
            )
        attempts.append(f"git apply -C1 失败: {stderr.splitlines()[-1] if stderr else '无输出'}")

    blocks = parse_edit_blocks(output)
    if blocks:
        applied, errors, done = apply_edit_blocks(root, blocks)
        if applied and not errors:
            return ApplyResult(
                applied=True,
                method="search-replace",
                files=tuple(dict.fromkeys(block.path for block in blocks)),
                applied_blocks=tuple(done),
            )
        attempts.append(
            f"search/replace 失败（{len(blocks)} 块）"
            + ("；" + "；".join(errors[:3]) if errors else "")
        )
    elif not attempts:
        attempts.append("输出里既没有 unified diff 也没有 search/replace 块")

    return ApplyResult(applied=False, method="", detail=" | ".join(attempts))


__all__ = [
    "ApplyResult",
    "EditBlock",
    "EditError",
    "apply_edit_blocks",
    "apply_model_output",
    "normalize_path",
    "parse_edit_blocks",
]
