"""定位基准（P2 验收）：题目集 + 指标 + 消融。

## 两个题目来源（都是自建，必须如实标注）

**A. 语料 issue 集**（`eval/fast_corpus.json`）：自然语言描述症状，期望文件 = 含 bug 的模块。
   每道题**物化到临时目录再索引**（语料是内联数据，没有真实目录可索引）。**用户风格**，但样本少。

**A2. 合成集**（`eval/locate_corpus.json`，20 题）：10 个领域 × 2 个难度层，
   每题 6 个文件、**刻意含名字相近的干扰模块**（`settings`/`schema`/`merger`/`loader`），
   所以"随便命中一个"不会得分。`easy` 层 issue 直接点名函数，`hard` 层只描述**行为**
   （更接近真实用户报告）。**合成代码不是真实项目**，但其标定实测：
   easy 层 Top-1 100%、hard 层 Top-1 20% —— 有真实梯度，不是天花板。

**B. 本仓库提交历史集**：把提交信息当 issue、该提交改动的源码文件当期望。
   真实代码、真实改动，但**信息偏乐观**——提交信息常直接点名函数/文件，比真实用户报告好定位。
   因此 A、B 两组指标**分开报告**，不混成一个数。

## 坐标系统一（第一版曾因此全 0）

索引出来的路径是"相对索引根"的；而 `git show --name-only` 给出的是"相对 git 根"的。
第一版直接把两者比较，导致**所有指标都是 0**。现在显式把期望路径归一到索引根的坐标系。

## 指标

  - **Top-1 / Top-5 命中率**、**MRR**（`1/首个命中排名`，未命中记 0）。

## 基线

基线**直接调用 `graph/repair.py` 当前在用的 locate 逻辑**（`search_symbol` + `search_text`
回退），而不是另造一个弱方法——否则"提升"可能只是换了个对手。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.core.textio import write_text_lf
from patchpilot.index.retrieval import Localizer, LocalizerConfig
from patchpilot.index.symbols import CodeIndex, index_repo
from patchpilot.tools import search_server

_SOURCE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rs", ".rb"}
_TEST_LIKE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[a-z]+$")
_DOC_LIKE = re.compile(r"\.(md|rst|txt|json|jsonl|yml|yaml|toml|cfg|ini)$|(^|/)\.")


@dataclass(frozen=True, slots=True)
class LocateCase:
    """一道已就绪的定位题（含它自己的索引，坐标系已统一）。"""

    id: str
    query: str
    index: CodeIndex
    expected_files: frozenset[str]
    group: str
    #: 基线与索引使用的根目录（语料题是临时物化目录，历史题是 scope 目录）
    root: Path


@dataclass(frozen=True, slots=True)
class LocateScore:
    config: str
    group: str
    total: int
    top1: float
    top5: float
    mrr: float
    misses: tuple[str, ...] = field(default=())

    def row(self) -> tuple[str, str, int, str, str, str]:
        return (
            self.config,
            self.group,
            self.total,
            f"{self.top1:.1%}",
            f"{self.top5:.1%}",
            f"{self.mrr:.3f}",
        )


@dataclass(frozen=True, slots=True)
class BenchOutcome:
    scores: tuple[LocateScore, ...]

    def table(self) -> list[tuple[str, str, int, str, str, str]]:
        return [score.row() for score in self.scores]


# ---------------------------------------------------------------- 题目构建


def cases_from_corpus(corpus_path: Path, *, keep_dirs: bool = False) -> list[LocateCase]:
    """把语料题物化成临时仓库并建索引（期望文件 = 非测试的源码文件）。"""
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    cases: list[LocateCase] = []
    for item in payload.get("tasks", []):
        files = item.get("files") or {}
        issue_rel = str(item.get("issue_file") or "Issue.md")
        issue = files.get(issue_rel)
        if not issue:
            continue
        expected = {
            str(path)
            for path in files
            if Path(str(path)).suffix.lower() in _SOURCE_SUFFIXES
            and not _TEST_LIKE.search(str(path))
        }
        if not expected:
            continue
        root = Path(tempfile.mkdtemp(prefix=f"pp-loc-{item['id']}-"))
        # 与 EvalTask.materialize 同口径：路径必须落在 root 之内（防 ../）
        base = root.resolve()
        for rel, content in files.items():
            target = (root / str(rel)).resolve()
            try:
                target.relative_to(base)
            except ValueError as exc:
                raise ValueError(f"{item['id']}: 语料路径逃逸出目标目录: {rel!r}") from exc
            write_text_lf(target, str(content))
        cases.append(
            LocateCase(
                id=str(item["id"]),
                query=str(issue),
                index=index_repo(root),
                expected_files=frozenset(expected),
                group="corpus",
                root=root,
            )
        )
        if not keep_dirs:
            # 索引已建好；目录留给基线调用（用完在 run 结束时清理）
            continue
    return cases


def cases_from_locate_corpus(corpus_path: Path) -> list[LocateCase]:
    """从"合成语料"建题：每题显式给 `expected_files`（比从文件名推导更准确）。"""
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    cases: list[LocateCase] = []
    for item in payload.get("tasks", []):
        files = {str(k): str(v) for k, v in (item.get("files") or {}).items()}
        issue_rel = str(item.get("issue_file") or "Issue.md")
        issue = files.get(issue_rel)
        expected = {str(path) for path in (item.get("expected_files") or [])}
        if not issue or not expected:
            continue
        root = Path(tempfile.mkdtemp(prefix=f"pp-syn-{item['id']}-"))
        base = root.resolve()
        for rel, content in files.items():
            target = (root / rel).resolve()
            try:
                target.relative_to(base)
            except ValueError as exc:
                raise ValueError(f"{item['id']}: 语料路径逃逸出目标目录: {rel!r}") from exc
            write_text_lf(target, content)
        cases.append(
            LocateCase(
                id=str(item["id"]),
                query=issue,
                index=index_repo(root),
                expected_files=frozenset(expected),
                group="synthetic",
                root=root,
            )
        )
    return cases


def _changed_source_files(repo: Path, commit: str) -> set[str]:
    proc = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", commit],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return set()
    found: set[str] = set()
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if _TEST_LIKE.search(path) or _DOC_LIKE.search(path):
            continue
        found.add(path)
    return found


def _normalize(path: str, git_root: Path, scope: Path) -> str | None:
    """把"相对 git 根"的路径归一到"相对 scope"的坐标系。"""
    try:
        absolute = (git_root / path).resolve()
        return absolute.relative_to(scope.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def cases_from_history(
    *, git_root: Path, scope: Path, limit: int = 40, min_message: int = 12
) -> list[LocateCase]:
    """提交历史 → 定位题。**信息偏乐观**，与 corpus 组分开报告。"""
    proc = subprocess.run(
        ["git", "log", f"-n{limit}", "--pretty=format:%H%x00%s"],
        cwd=git_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return []
    index = index_repo(scope)
    cases: list[LocateCase] = []
    for line in proc.stdout.splitlines():
        if "\x00" not in line:
            continue
        sha, message = line.split("\x00", 1)
        message = message.strip()
        if len(message) < min_message:
            continue
        changed: set[str] = set()
        for raw in _changed_source_files(git_root, sha):
            normalized = _normalize(raw, git_root, scope)
            if normalized:
                changed.add(normalized)
        if not changed:
            continue
        cases.append(
            LocateCase(
                id=sha[:8],
                query=message,
                index=index,
                expected_files=frozenset(changed),
                group="history",
                root=scope,
            )
        )
    return cases


# ---------------------------------------------------------------- 两个"配置"（基线 + P2）

ABLATIONS: tuple[tuple[str, LocalizerConfig | None], ...] = (
    ("基线（P1 现有 locate：符号+子串）", None),
    (
        "仅符号通道",
        LocalizerConfig(use_symbol=True, use_lexical=False, use_vector=False, use_graph=False),
    ),
    (
        "仅词法 BM25",
        LocalizerConfig(use_symbol=False, use_lexical=True, use_vector=False, use_graph=False),
    ),
    (
        "仅向量（词法哈希）",
        LocalizerConfig(use_symbol=False, use_lexical=False, use_vector=True, use_graph=False),
    ),
    (
        "符号+词法（无图无向量无重排）",
        LocalizerConfig(
            use_symbol=True,
            use_lexical=True,
            use_vector=False,
            use_graph=False,
            use_rerank=False,
        ),
    ),
    (
        "符号+词法+向量（无图无重排）",
        LocalizerConfig(
            use_symbol=True,
            use_lexical=True,
            use_vector=True,
            use_graph=False,
            use_rerank=False,
        ),
    ),
    (
        "符号+词法+图（无向量无重排）",
        LocalizerConfig(
            use_symbol=True,
            use_lexical=True,
            use_vector=False,
            use_graph=True,
            use_rerank=False,
        ),
    ),
    ("四路 + 重排（P2 完整）", LocalizerConfig()),
)


def baseline_rank(query: str, root: Path, *, subdir: str = ".") -> list[str]:
    """**现有实现**的定位顺序：先符号检索，没命中再退化为文本检索。"""
    from patchpilot.graph.repair import extract_terms

    terms = extract_terms(query)
    hits: list[str] = []
    for term in terms:
        for match in search_server.search_symbol(root, term, subdir=subdir):
            if match.path not in hits:
                hits.append(match.path)
        if hits:
            break
    if not hits:
        for term in terms:
            for match in search_server.search_text(root, term, subdir=subdir, max_results=10):
                if match.path not in hits:
                    hits.append(match.path)
            if hits:
                break
    return hits


# ---------------------------------------------------------------- 评分


def _score(
    config_name: str, group: str, *, ranked: list[tuple[LocateCase, list[str]]]
) -> LocateScore:
    if not ranked:
        return LocateScore(config_name, group, 0, 0.0, 0.0, 0.0)
    top1 = top5 = 0
    rr_sum = 0.0
    misses: list[str] = []
    for case, candidates in ranked:
        hit_rank = 0
        for position, path in enumerate(candidates, start=1):
            if path in case.expected_files:
                hit_rank = position
                break
        if hit_rank == 1:
            top1 += 1
        if 1 <= hit_rank <= 5:
            top5 += 1
        if hit_rank >= 1:
            rr_sum += 1.0 / hit_rank
        else:
            misses.append(case.id)
    total = len(ranked)
    return LocateScore(
        config=config_name,
        group=group,
        total=total,
        top1=top1 / total,
        top5=top5 / total,
        mrr=rr_sum / total,
        misses=tuple(misses),
    )


def run_locate_bench(
    *,
    git_root: Path,
    scope: Path,
    corpus_path: Path | None = None,
    synthetic_path: Path | None = None,
    history_limit: int = 40,
    limit: int = 10,
) -> BenchOutcome:
    """跑全部消融配置 × 三个题目组（corpus / synthetic / history）。"""
    cases: list[LocateCase] = []
    temp_dirs: list[Path] = []
    if corpus_path is not None and corpus_path.is_file():
        corpus_cases = cases_from_corpus(corpus_path)
        cases.extend(corpus_cases)
        temp_dirs.extend(c.root for c in corpus_cases)
    if synthetic_path is not None and synthetic_path.is_file():
        synthetic_cases = cases_from_locate_corpus(synthetic_path)
        cases.extend(synthetic_cases)
        temp_dirs.extend(c.root for c in synthetic_cases)
    cases.extend(cases_from_history(git_root=git_root, scope=scope, limit=history_limit))

    scores: list[LocateScore] = []
    try:
        for name, config in ABLATIONS:
            # 先逐题算一次候选，再按组与"全部"分别汇总——
            # 汇总行很重要：单组样本仍偏小（6/20/13），跨组加权才看得出整体趋势。
            per_case: dict[str, list[tuple[LocateCase, list[str]]]] = {}
            for case in cases:
                if config is None:
                    candidates = baseline_rank(case.query, case.root)[:limit]
                else:
                    localizer = Localizer(case.index, config=config)
                    candidates = localizer.localize_paths(case.query, limit=limit)
                per_case.setdefault(case.group, []).append((case, candidates))
            for group in ("corpus", "synthetic", "history", "all"):
                if group == "all":
                    ranked = [pair for pairs in per_case.values() for pair in pairs]
                else:
                    ranked = per_case.get(group, [])
                if ranked:
                    scores.append(_score(name, group, ranked=ranked))
    finally:
        for directory in temp_dirs:
            shutil.rmtree(directory, ignore_errors=True)
    return BenchOutcome(scores=tuple(scores))


__all__ = [
    "ABLATIONS",
    "BenchOutcome",
    "LocateCase",
    "LocateScore",
    "baseline_rank",
    "cases_from_corpus",
    "cases_from_locate_corpus",
    "cases_from_history",
    "run_locate_bench",
]
