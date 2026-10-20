"""三路召回 + 融合排序（P2 的核心）。

## 三路召回

| 通道 | 命中什么 | 为什么需要 |
|---|---|---|
| **符号** | 名字精确/模糊匹配到定义 | issue 里常直接写出函数名 |
| **词法**（BM25） | 标识符拆分后的语义接近 | issue 说"配置读取失败"，代码叫 `load_config` |
| **调用图** | 命中符号**被谁调用 / 调用了谁** | 修复点常在调用方或实现方，而非报错那行 |

## 融合：RRF（Reciprocal Rank Fusion）

```
score(d) = Σ_channel  w_c / (k + rank_c(d))
```

选 RRF 而不是"分数加权求和"的理由：三路的分数量纲完全不同
（BM25 是任意正数、符号匹配是计数、图扩散是跳数衰减），
直接相加等于让量纲最大的那一路说话。RRF 只用**排名**，天然规避量纲问题，
是信息检索里的标准做法（`k=60` 为常用值）。

## 重排（rerank）

在 RRF 之上加三类**可解释**的确定性加权，每一条都能在结果里看到原因：

  1. 符号名与 issue 里的标识符**完全相等** → 强加权（最可信的信号）；
  2. 查询词命中**文件路径** → 中加权（`test_budget.py` 出现在 issue 里）；
  3. 命中测试文件时**降权**（除非 issue 明确提到测试）——修复点通常在源码而非测试。

不做模型重排：那需要额外依赖与网络，且会让"为什么排第一"变得不可解释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from patchpilot.index.lexical import BM25Index, ScoredChunk, tokenize
from patchpilot.index.symbols import Chunk, CodeIndex, chunk_id

#: RRF 的平滑常数（越小越偏向头部）
RRF_K = 60.0

#: 三路默认权重（消融实验会逐路关闭，见 `LocalizerConfig`）
DEFAULT_WEIGHTS = {"symbol": 1.0, "lexical": 1.0, "graph": 0.6}

#: 重排加权的默认值
DEFAULT_RERANK = {"exact_symbol": 2.0, "path_match": 0.5, "test_penalty": 0.5}

_IDENT_IN_TEXT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|\.test\.[jt]sx?$")


@dataclass(frozen=True, slots=True)
class LocalizerConfig:
    """哪几路参与、各自权重、重排开关。消融实验通过改这里实现。"""

    use_symbol: bool = True
    use_lexical: bool = True
    use_graph: bool = True
    use_rerank: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    rerank: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_RERANK))
    graph_hops: int = 1


@dataclass(frozen=True, slots=True)
class LocalizedChunk:
    """一条定位结果（带可解释的分数与理由）。"""

    chunk: Chunk
    score: float
    channels: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.chunk.path

    @property
    def id(self) -> str:
        return chunk_id(self.chunk)


def _query_identifiers(query: str) -> set[str]:
    """从 issue 文本里抽"看起来像标识符"的词（供符号通道与重排使用）。"""
    found = set()
    for raw in _IDENT_IN_TEXT.findall(query):
        found.add(raw)
        found.add(raw.lower())
    # snake_case / camelCase 拆分后的部分也算
    for token in tokenize(query):
        found.add(token)
    return found


class Localizer:
    """对仓库索引做定位。构造一次、查询多次（索引与 BM25 都复用）。"""

    def __init__(
        self,
        index: CodeIndex,
        *,
        config: LocalizerConfig | None = None,
    ) -> None:
        self._index = index
        self._config = config or LocalizerConfig()
        self._chunks: tuple[Chunk, ...] = tuple(c for f in index.files for c in f.chunks)
        self._by_id = {chunk_id(c): c for c in self._chunks}
        self._bm25 = (
            BM25Index(self._chunks) if self._config.use_lexical and self._chunks else None
        )

    @property
    def index(self) -> CodeIndex:
        return self._index

    # ---------------- 三路召回：各自返回 {chunk_id: rank}（rank 从 1 开始）

    def _symbol_ranks(self, query: str, *, pool: int) -> dict[str, int]:
        wanted = _query_identifiers(query)
        if not wanted:
            return {}
        scored: list[tuple[int, str]] = []
        for chunk in self._chunks:
            if not chunk.symbol:
                continue
            name = chunk.symbol
            lowered = name.lower()
            # 精确名 > 整名包含 > 拆分后部分命中
            if name in wanted or lowered in wanted:
                scored.append((0, chunk_id(chunk)))
            elif any(len(part) >= 3 and part in wanted for part in tokenize(name)):
                scored.append((2, chunk_id(chunk)))
        scored.sort(key=lambda pair: (pair[0], pair[1]))
        return {cid: rank for rank, (_tier, cid) in enumerate(scored[:pool], start=1)}

    def _lexical_ranks(self, query: str, *, pool: int) -> dict[str, int]:
        if self._bm25 is None:
            return {}
        hits: list[ScoredChunk] = self._bm25.search(query, limit=pool)
        return {chunk_id(hit.chunk): rank for rank, hit in enumerate(hits, start=1)}

    def _graph_ranks(self, seeds: dict[str, int], *, pool: int) -> dict[str, int]:
        """从种子 chunk 出发，沿"符号定义 ↔ 引用文件"扩一跳。

        规则（确定性、可解释）：
          - 种子里的符号名 → 找**同名定义的其它 chunk**（可能是真正的实现处）；
          - 种子所在文件里的符号名 → 找**引用了该名字的其它文件**里的 chunk
            （修复点常在调用方）。
        """
        if not seeds:
            return {}
        seed_ids = set(seeds)
        wanted_names: set[str] = set()
        seed_paths: set[str] = set()
        for cid in seed_ids:
            chunk = self._by_id.get(cid)
            if chunk is None:
                continue
            seed_paths.add(chunk.path)
            if chunk.symbol:
                wanted_names.add(chunk.symbol)
            else:  # 文件头块：把该文件所有符号名都当种子
                indexed = self._index.file(chunk.path)
                if indexed is not None:
                    wanted_names.update(symbol.name for symbol in indexed.symbols)

        related_paths: set[str] = set(seed_paths)
        for name in wanted_names:
            for path in self._index.references.get(name, frozenset()):
                related_paths.add(path)

        scored: list[tuple[int, str]] = []
        for chunk in self._chunks:
            if chunk_id(chunk) in seed_ids:
                continue
            if chunk.path in related_paths:
                # 同文件优先于跨文件；有符号名的块优先于文件头
                tier = 0 if chunk.path in seed_paths else 1
                if not chunk.symbol:
                    tier += 2
                scored.append((tier, chunk_id(chunk)))
        scored.sort(key=lambda pair: (pair[0], pair[1]))
        return {cid: rank for rank, (_t, cid) in enumerate(scored[:pool], start=1)}

    # ---------------- 融合与重排

    def localize(
        self,
        query: str,
        *,
        limit: int = 5,
        pool: int = 60,
    ) -> list[LocalizedChunk]:
        """返回 Top-limit 定位块（按融合分数降序）。"""
        config = self._config
        channels: dict[str, dict[str, int]] = {}
        if config.use_symbol:
            channels["symbol"] = self._symbol_ranks(query, pool=pool)
        if config.use_lexical:
            channels["lexical"] = self._lexical_ranks(query, pool=pool)
        if config.use_graph:
            seeds = dict(channels.get("symbol") or {})
            # 用符号通道的头部当种子；没有符号命中时退回词法头部
            if not seeds:
                lexical = channels.get("lexical") or {}
                seeds = {cid: rank for cid, rank in lexical.items() if rank <= 5}
            channels["graph"] = self._graph_ranks(seeds, pool=pool)

        fused: dict[str, float] = {}
        origin: dict[str, set[str]] = {}
        for name, ranks in channels.items():
            weight = float(config.weights.get(name, 1.0))
            for cid, rank in ranks.items():
                fused[cid] = fused.get(cid, 0.0) + weight / (RRF_K + rank)
                origin.setdefault(cid, set()).add(name)
        if not fused:
            return []

        reasons: dict[str, list[str]] = {}
        if config.use_rerank:
            wanted = _query_identifiers(query)
            query_tokens = set(tokenize(query))
            mentions_tests = bool(re.search(r"test|测试|用例", query, re.IGNORECASE))
            for cid in list(fused):
                chunk = self._by_id.get(cid)
                if chunk is None:
                    continue
                notes: list[str] = []
                symbol = chunk.symbol
                if symbol and (symbol in wanted or symbol.lower() in wanted):
                    fused[cid] += float(config.rerank.get("exact_symbol", 0.0)) / RRF_K
                    notes.append("符号名与 issue 完全一致")
                path_tokens = set(tokenize(chunk.path))
                if path_tokens & query_tokens:
                    fused[cid] += float(config.rerank.get("path_match", 0.0)) / RRF_K
                    notes.append("查询词命中文件路径")
                if not mentions_tests and _TEST_PATH.search(chunk.path):
                    fused[cid] *= max(0.0, 1.0 - float(config.rerank.get("test_penalty", 0.0)))
                    notes.append("命中测试文件且 issue 未提及测试 → 降权")
                if notes:
                    reasons[cid] = notes

        ordered = sorted(
            fused.items(),
            key=lambda pair: (
                -pair[1],
                self._by_id[pair[0]].path,
                self._by_id[pair[0]].start_line,
            ),
        )
        results: list[LocalizedChunk] = []
        for cid, score in ordered[: max(1, limit)]:
            chunk = self._by_id.get(cid)
            if chunk is None:
                continue
            results.append(
                LocalizedChunk(
                    chunk=chunk,
                    score=round(score, 6),
                    channels=tuple(sorted(origin.get(cid, ()))),
                    reasons=tuple(reasons.get(cid, ())),
                )
            )
        return results

    def localize_paths(self, query: str, *, limit: int = 5, pool: int = 60) -> list[str]:
        """只返回文件路径（去重保序）——给修复闭环的 `locate` 节点用。"""
        paths: list[str] = []
        for hit in self.localize(query, limit=limit * 3, pool=pool):
            if hit.path not in paths:
                paths.append(hit.path)
            if len(paths) >= limit:
                break
        return paths


__all__ = [
    "DEFAULT_RERANK",
    "DEFAULT_WEIGHTS",
    "RRF_K",
    "LocalizedChunk",
    "Localizer",
    "LocalizerConfig",
]
