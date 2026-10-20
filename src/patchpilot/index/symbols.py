"""符号索引：从仓库里抽出**函数 / 类 / 方法**及其行号范围。

## 为什么先做纯标准库版本

P0/P1 的纪律是"门禁不需要网络、不需要 Docker、不引入重依赖"。`tree-sitter` 会带来
编译型依赖，故 P2 的**默认路径**用标准库：

  - **Python**：`ast`（精确，能拿到 `end_lineno` 与调用关系）；
  - **其它语言**：保守的行级正则（够用于定位，不声称是完整语法树）。

`tree_sitter` 若可用则走它（保留扩展点），但**不是**必需项——这条边界必须诚实标注：
非 Python 语言的符号表是"近似"的。

## 索引能回答什么

  - `symbols`：名字 → 定义位置（文件、起止行、种类）；
  - `references`：名字 → 出现过该标识符的**其它**文件（用于调用图扩散）；
  - `chunks`：可检索的文本块（符号体 + 文件头），带行号范围。

所有遍历都**排序后进行**，保证同一仓库两次索引得到逐字节相同的结构（可复现）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.sandbox.policy import is_link_like

#: 参与索引的扩展名 → 语言名
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".rb": "ruby",
}

#: 与 `tools/search_server` 保持一致的跳过目录
SKIP_DIRS = frozenset(
    {
        ".git",
        ".patchpilot",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        "dist",
        "build",
    }
)

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 4000

#: 非 Python 语言的**近似**符号规则（诚实标注：不是完整语法树）
_REGEX_SYMBOLS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "javascript": (
        (
            "function",
            re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        ),
        ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("const", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=")),
    ),
    "go": (
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)")),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:public\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)")),
        (
            "method",
            re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\]]+\s+([A-Za-z_]\w*)\s*\("),
        ),
    ),
    "rust": (
        ("function", re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)")),
        ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)")),
    ),
    "ruby": (
        ("method", re.compile(r"^\s*def\s+([A-Za-z_]\w*[?!=]?)")),
        ("class", re.compile(r"^\s*(?:class|module)\s+([A-Za-z_]\w*)")),
    ),
}
_REGEX_SYMBOLS["typescript"] = _REGEX_SYMBOLS["javascript"]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Symbol:
    """一个定义。行号从 1 开始。"""

    name: str
    kind: str  # function | method | class | const | type | struct
    path: str  # 仓库相对路径（posix 分隔）
    start_line: int
    end_line: int
    parent: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass(frozen=True, slots=True)
class Chunk:
    """可检索的文本块。"""

    path: str
    start_line: int
    end_line: int
    text: str
    symbol: str = ""  # 关联的符号名（文件头块为空）


@dataclass(frozen=True, slots=True)
class IndexedFile:
    path: str
    language: str
    symbols: tuple[Symbol, ...]
    chunks: tuple[Chunk, ...]
    lines: int


@dataclass(frozen=True, slots=True)
class CodeIndex:
    """整个仓库的索引。构造后**不可变**（便于缓存与并发读）。"""

    root: Path
    files: tuple[IndexedFile, ...]
    #: 名字 -> 定义列表（同名可能多处定义）
    definitions: dict[str, tuple[Symbol, ...]] = field(default_factory=dict)
    #: 名字 -> 出现过该标识符的路径集合（用于调用图扩散）
    references: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    @property
    def symbol_count(self) -> int:
        return sum(len(f.symbols) for f in self.files)

    def file(self, path: str) -> IndexedFile | None:
        for indexed in self.files:
            if indexed.path == path:
                return indexed
        return None

    def chunks_for(self, path: str) -> tuple[Chunk, ...]:
        indexed = self.file(path)
        return indexed.chunks if indexed else ()

    def chunk_by_id(self, chunk_id: str) -> Chunk | None:
        """`chunk_id` 形如 `path::start-end`（在检索结果里用于回指）。"""
        try:
            path, span = chunk_id.split("::", 1)
            start = int(span.split("-", 1)[0])
        except ValueError:
            return None
        for chunk in self.chunks_for(path):
            if chunk.start_line == start:
                return chunk
        return None


def chunk_id(chunk: Chunk) -> str:
    return f"{chunk.path}::{chunk.start_line}-{chunk.end_line}"


def _iter_source_files(root: Path, *, limit: int = MAX_FILES) -> list[tuple[Path, str]]:
    """枚举参与索引的源文件（跳过重目录与链接类对象，与 search 同口径）。"""
    resolved_root = root.resolve()
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if len(found) >= limit:
            break
        try:
            parts = path.relative_to(resolved_root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in parts):
            continue
        if is_link_like(path) or not path.is_file():
            continue
        language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language is None:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append((path, language))
    return found


def _python_symbols(tree: ast.AST, path: str) -> tuple[list[Symbol], set[str]]:
    """用 `ast` 抽 Python 符号，并收集被调用的名字（供调用图使用）。"""
    symbols: list[Symbol] = []
    called: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    path=path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    parent=self.stack[-1] if self.stack else None,
                )
            )
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            symbols.append(
                Symbol(
                    name=node.name,
                    kind="method" if self.stack else "function",
                    path=path,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    parent=self.stack[-1] if self.stack else None,
                )
            )
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._record_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._record_function(node)

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
            self.generic_visit(node)

    Visitor().visit(tree)
    return symbols, called


def _regex_symbols(text: str, path: str, language: str) -> list[Symbol]:
    """非 Python 语言的**近似**符号抽取（不声称是完整语法树）。"""
    rules = _REGEX_SYMBOLS.get(language, ())
    lines = text.splitlines()
    symbols: list[Symbol] = []
    for index, line in enumerate(lines, start=1):
        for kind, pattern in rules:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            # 近似：符号范围取到下一个"顶格且更空"的行之前，简单用文件末尾兜底
            end = index
            indent = len(line) - len(line.lstrip())
            for probe in range(index, len(lines)):
                nxt = lines[probe]
                dedented = nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent
                if dedented and probe + 1 > index:
                    end = probe
                    break
                end = probe + 1
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    path=path,
                    start_line=index,
                    end_line=max(end, index),
                )
            )
            break
    return symbols


def _build_chunks(path: str, text: str, symbols: tuple[Symbol, ...]) -> tuple[Chunk, ...]:
    """把文件切成"文件头 + 每个符号体"的块，供 BM25/向量检索。"""
    lines = text.splitlines()
    if not lines:
        return ()
    chunks: list[Chunk] = []
    spans = sorted((s.start_line, s.end_line, s.name) for s in symbols)
    first_symbol_line = spans[0][0] if spans else len(lines) + 1
    # 文件头：第一个符号之前的导入/常量
    if first_symbol_line > 1:
        header = "\n".join(lines[: first_symbol_line - 1])
        if header.strip():
            chunks.append(
                Chunk(path=path, start_line=1, end_line=first_symbol_line - 1, text=header)
            )
    for start, end, name in spans:
        body = "\n".join(lines[start - 1 : end])
        if body.strip():
            chunks.append(
                Chunk(path=path, start_line=start, end_line=end, text=body, symbol=name)
            )
    if not chunks:  # 没有符号的文件整体作为一块
        chunks.append(Chunk(path=path, start_line=1, end_line=len(lines), text=text))
    return tuple(chunks)


def index_repo(root: Path, *, limit: int = MAX_FILES) -> CodeIndex:
    """构建仓库索引。**纯标准库、无网络、无外部依赖。**"""
    root = Path(root)
    files: list[IndexedFile] = []
    definitions: dict[str, list[Symbol]] = {}
    references: dict[str, set[str]] = {}

    for path, language in _iter_source_files(root, limit=limit):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.name
        symbols: list[Symbol] = []
        called: set[str] = set()
        if language == "python":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                symbols, called = _python_symbols(tree, rel)
        if not symbols:  # 非 Python，或 Python 解析失败 → 近似规则
            symbols = _regex_symbols(text, rel, language)
        ordered = tuple(sorted(symbols, key=lambda s: (s.start_line, s.name)))
        for symbol in ordered:
            definitions.setdefault(symbol.name, []).append(symbol)

        # 引用：文件里出现过的所有标识符（用于 1-hop 扩散），排除自身
        seen = set(_IDENT.findall(text)) | called
        for name in seen:
            references.setdefault(name, set()).add(rel)

        files.append(
            IndexedFile(
                path=rel,
                language=language,
                symbols=ordered,
                chunks=_build_chunks(rel, text, ordered),
                lines=len(text.splitlines()),
            )
        )

    return CodeIndex(
        root=root,
        files=tuple(files),
        definitions={k: tuple(v) for k, v in definitions.items()},
        references={k: frozenset(v) for k, v in references.items()},
    )


__all__ = [
    "LANGUAGE_BY_SUFFIX",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "SKIP_DIRS",
    "Chunk",
    "CodeIndex",
    "IndexedFile",
    "Symbol",
    "chunk_id",
    "index_repo",
]
