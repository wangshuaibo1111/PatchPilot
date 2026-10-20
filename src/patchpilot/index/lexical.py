"""BM25 词法检索（纯标准库）。

## 为什么用 BM25 而不是"子串匹配"

`tools/search_server` 的子串匹配够用但有个硬伤：**它不理解标识符的构成**。
issue 里写"配置读取失败"，而代码里叫 `load_config` / `ConfigError`，
子串匹配一条都命中不了。BM25 配合**标识符拆分**（`load_config` → `load` + `config`）
就能匹配上——这是定位质量的第一层提升。

## 标识符拆分规则

  1. 按非字母数字切分：`load_config` → `load`, `config`
  2. 驼峰再切：`ConfigError` → `Config`, `Error`
  3. 全小写并去停用词（`self`、`the` 之类）
  4. 保留原始整词（`configerror`），这样"精确写全名"的查询仍然能拿到高分

## 参数

`k1=1.5`、`b=0.75` 是 BM25 的常用默认值；这里**不做调参**——P2 的目标是拿到
基线数字与消融表，调参属后续工作（会在报告里注明未调参）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from patchpilot.index.symbols import Chunk

#: 极常见、对定位没有区分度的词
STOPWORDS = frozenset(
    {
        "self",
        "cls",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "import",
        "return",
        "none",
        "true",
        "false",
        "def",
        "class",
        "async",
        "await",
        "elif",
        "else",
        "while",
        "yield",
        "lambda",
        "raise",
        "except",
        "try",
        "pass",
        "not",
        "is",
        "in",
        "if",
        "or",
        "as",
        "of",
        "to",
        "a",
        "an",
        "it",
        "be",
        "on",
        "at",
        "by",
        "we",
        "can",
        "should",
        "would",
    }
)

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """把代码/自然语言切成检索用 token（小写、拆标识符、去停用词）。"""
    tokens: list[str] = []
    for raw in _WORD.findall(text):
        # 原始整词（保留 "configerror" 这种写法）
        whole = raw.lower()
        if whole and whole not in STOPWORDS:
            tokens.append(whole)
        # 按 `_` 与驼峰再拆
        for piece in re.split(r"_+", raw):
            for part in _CAMEL.findall(piece):
                lowered = part.lower()
                if len(lowered) >= 2 and lowered not in STOPWORDS and lowered != whole:
                    tokens.append(lowered)
    # 中文按连续片段整体保留（无分词器时，"按整段匹配"比乱切更稳）
    for run in _CJK.findall(text):
        if len(run) >= 2:
            tokens.append(run)
    return tokens


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk: Chunk
    score: float


class BM25Index:
    """对 `Chunk` 集合建 BM25 索引。不可变，可重复查询。"""

    def __init__(
        self,
        chunks: tuple[Chunk, ...],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._chunks = chunks
        self._k1 = k1
        self._b = b
        self._docs: list[dict[str, int]] = []
        self._lengths: list[int] = []
        doc_freq: dict[str, int] = {}

        for chunk in chunks:
            counts: dict[str, int] = {}
            # 符号名权重加倍：定位时"名字对上了"比"正文出现过"更有指示性
            tokens = tokenize(chunk.text)
            if chunk.symbol:
                tokens = tokens + tokenize(chunk.symbol)
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self._docs.append(counts)
            self._lengths.append(sum(counts.values()))
            for token in counts:
                doc_freq[token] = doc_freq.get(token, 0) + 1

        self._doc_freq = doc_freq
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        total = len(self._docs)
        self._idf = {
            token: math.log(1.0 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in doc_freq.items()
        }

    @property
    def size(self) -> int:
        return len(self._docs)

    def search(self, query: str, *, limit: int = 10) -> list[ScoredChunk]:
        """按 BM25 打分返回 Top-limit 块（同分时按路径/行号稳定排序）。"""
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        limit = max(1, min(limit, len(self._docs)))
        scores: list[tuple[float, int]] = []
        for index, counts in enumerate(self._docs):
            score = 0.0
            length = self._lengths[index]
            for term in terms:
                freq = counts.get(term)
                if not freq:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = freq + self._k1 * (
                    1.0 - self._b + self._b * (length / self._avg_len if self._avg_len else 1.0)
                )
                score += idf * (freq * (self._k1 + 1.0)) / denom
            if score > 0:
                scores.append((score, index))
        scores.sort(
            key=lambda pair: (
                -pair[0],
                self._chunks[pair[1]].path,
                self._chunks[pair[1]].start_line,
            )
        )
        return [ScoredChunk(chunk=self._chunks[i], score=round(s, 6)) for s, i in scores[:limit]]


__all__ = ["BM25Index", "STOPWORDS", "ScoredChunk", "tokenize"]
