"""把模型输出落成文件改动：unified diff 优先，失败则用 **search/replace 编辑块**。

## 为什么需要它（真机数据）

SWE-bench 子集 11 题里 **9 题的失败原因是"候选补丁无法应用"**——不是模型没想对，
而是它写的 unified diff 带不上 sympy 大文件里的精确上下文/行号，`git apply` 直接拒。

这不是我们独有的问题：Aider 专门为此提供了 `diff`（search/replace 块）格式，
并明确指出裸 unified diff 的失败率更高
（https://aider.chat/docs/more/edit-formats.html 、
https://aider.chat/docs/troubleshooting/edit-errors.html ）；
SWE-agent 一侧也常出现 patch 生成/应用失败（
https://github.com/SWE-agent/SWE-agent/issues/1119 ）。

## 格式（与 Aider 兼容）

```
path/to/file.py
<<<<<<< SEARCH
要替换掉的原样内容
=======
替换成的内容
>>>>>>> REPLACE
```

## 应用顺序

1. `git apply`（严格）
2. `git apply -C1`（放宽上下文行数——官方 harness 之外常见的 fuzz 手段）
3. search/replace 块：先逐字节精确匹配，再退化到"忽略行尾空白 + CRLF 归一"

第 3 步仍然失败时**报出具体原因**（哪个文件、搜索块前两行、错误类型），
而不是笼统说"打不上"——上一轮就是因为丢了 git stderr，9 题失败无法再细分。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_SEARCH_MARK = "<<<<<<<"
_DIVIDER = "======="
_REPLACE_MARK = ">>>>>>>"


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


def parse_edit_blocks(text: str) -> list[EditBlock]:
    """解析 Aider 风格的 search/replace 块（容忍前后杂文本）。"""
    blocks: list[EditBlock] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith(_SEARCH_MARK):
            index += 1
            continue
        # 往上找最近一行"像路径"的内容作为文件名
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
            current = lines[index]
            stripped = current.strip()
            if stripped == _DIVIDER and state == "search":
                state = "replace"
                index += 1
                continue
            if stripped.startswith(_REPLACE_MARK):
                closed = True
                index += 1
                break
            (search if state == "search" else replace).append(current)
            index += 1
        if path and closed:
            blocks.append(
                EditBlock(path=path, search="\n".join(search), replace="\n".join(replace))
            )
    return blocks


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _match_span(text: str, search: str) -> tuple[int, int] | None:
    """先精确匹配；再退化到"忽略行尾空白 + 换行归一"的逐行匹配。"""
    if not search:
        return None
    exact = text.find(search)
    if exact >= 0:
        return exact, exact + len(search)

    haystack = _normalize_newlines(text).splitlines()
    needle = _normalize_newlines(search).splitlines()
    if not needle:
        return None
    stripped_needle = [row.rstrip() for row in needle]
    for start in range(len(haystack) - len(needle) + 1):
        window = [row.rstrip() for row in haystack[start : start + len(needle)]]
        if window == stripped_needle:
            # 换算回原文本的偏移：用原始行边界重建
            offset = 0
            for row in _normalize_newlines(text).splitlines(keepends=True)[:start]:
                offset += len(row)
            matched = "".join(
                _normalize_newlines(text).splitlines(keepends=True)[start : start + len(needle)]
            )
            return offset, offset + len(matched)
    return None


def apply_edit_blocks(
    root: Path, blocks: list[EditBlock]
) -> tuple[int, list[str]]:
    """逐个应用编辑块，返回 `(成功数, 失败描述)`。"""
    applied = 0
    errors: list[str] = []
    base = Path(root).resolve()
    for block in blocks:
        target = (base / block.path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            errors.append(f"{block.path}: 路径逃逸出仓库")
            continue
        if not target.is_file():
            errors.append(f"{block.path}: 文件不存在")
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        span = _match_span(text, block.search)
        if span is None:
            head = block.search.splitlines()[:2]
            errors.append(f"{block.path}: 搜索块未匹配（前两行 {head!r}）")
            continue
        start, end = span
        updated = text[:start] + block.replace + text[end:]
        # 按字节写，避免平台换行翻译（见 core/textio.py 的说明）
        target.write_bytes(updated.encode("utf-8"))
        applied += 1
    return applied, errors


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


def _touched_files(root: Path, patch: str) -> tuple[str, ...]:
    """从补丁里粗略取文件名（`--- a/x` / `+++ b/x`），仅供报告。"""
    names: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name and name != "/dev/null":
                names.append(name[2:] if name.startswith(("a/", "b/")) else name)
    return tuple(dict.fromkeys(names))


def apply_model_output(
    root: Path, output: str, *, patch: str | None = None
) -> ApplyResult:
    """把模型输出落成改动：unified diff（严格 → `-C1`）→ search/replace 块。

    `patch` 可显式给出已抽取好的 diff（调用方通常已经抽过）。
    """
    candidate = patch if patch is not None else output
    attempts: list[str] = []

    if candidate and ("--- " in candidate and "+++ " in candidate):
        ok, stderr = _git_apply(root, candidate)
        if ok:
            return ApplyResult(
                applied=True, method="git-apply", files=_touched_files(root, candidate)
            )
        attempts.append(f"git apply 失败: {stderr.splitlines()[-1] if stderr else '无输出'}")
        ok, stderr = _git_apply(root, candidate, extra=("-C1",))
        if ok:
            return ApplyResult(
                applied=True,
                method="git-apply -C1",
                files=_touched_files(root, candidate),
                detail="严格模式失败后用 -C1 成功",
            )
        attempts.append(f"git apply -C1 失败: {stderr.splitlines()[-1] if stderr else '无输出'}")

    blocks = parse_edit_blocks(output)
    if blocks:
        applied, errors = apply_edit_blocks(root, blocks)
        if applied and applied == len(blocks):
            return ApplyResult(
                applied=True,
                method="search-replace",
                files=tuple(dict.fromkeys(block.path for block in blocks)),
            )
        attempts.append(
            f"search/replace 成功 {applied}/{len(blocks)}"
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
    "parse_edit_blocks",
]
