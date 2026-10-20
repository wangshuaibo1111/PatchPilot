"""代码向量通道：**确定性哈希向量**（纯标准库，无外部 embedding 依赖）。

## 这是什么，不是什么（必须说清）

它是**词法哈希向量**：把 token 与字符 n-gram 用 **BLAKE2b** 哈希到固定维度的桶里，
再做 TF-IDF 加权与 L2 归一化，用余弦相似度检索。

**它不是语义 embedding**：`load_config` 与 `read_settings` 在它看来毫不相干。
真正的语义召回需要模型（`sentence-transformers` / 云端 embedding），那会引入
编译依赖与网络，与"门禁不需要网络"的纪律冲突，故**默认不启用**、也不假装有。

那为什么还要这一路？三个实际理由：

1. **模糊匹配**：BM25 要求 token 精确相等；哈希向量的字符 n-gram 能容忍拼写近似
   （`getSettings` vs `get_setting`、`parseYaml` vs `parse_yaml` 有共享 n-gram）。
2. **可插拔**：`Vectorizer` 是协议，将来接真实 embedding 只需换实现，融合与重排不用动。
3. **可作为对照**：消融表里能回答"多加一路词法通道还有用吗"——实测**与 BM25 高度冗余**，
   这个结论本身有价值（见 PLAN 的消融表）。

## 确定性

Python 内置 `hash()` 对 str 是**每进程随机加盐**的（`PYTHONHASHSEED`），
用它做分桶会导致同一份代码两次索引得到不同向量。因此这里用 `hashlib.blake2b`
取摘要的前 4 字节做桶号——跨进程、跨平台稳定。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from patchpilot.index.lexical import tokenize
from patchpilot.index.symbols import Chunk

#: 默认桶数。512 在"几百个 chunk"规模上碰撞很少，且计算量小。
DEFAULT_DIM = 1024

#: 字符 n-gram 的长度（用于容忍拼写近似）
NGRAM = 3

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Vectorizer(Protocol):
    """可插拔的向量化接口：将来接真实 embedding 只需实现它。"""

    dim: int

    def vectorize(self, text: str) -> dict[int, float]: ...


def _bucket(token: str, dim: int) -> int:
    """稳定分桶：用 BLAKE2b 摘要的前 4 字节。**不能用内置 hash()**（每进程加盐）。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % dim


#: 只有像代码标识符的特征才进向量空间。
#: 理由：中文等非 ASCII token 被哈希进桶后**只产生碰撞噪声**——实测纯中文 issue 会
#: 误命中无关文件（取配置项时缺失的键… → utils.py）。过滤掉它们比调阈值更本质：
#: 少掉一整类噪声，而不是把噪声压到某个数值以下。
_CODEISH = re.compile(r"^[A-Za-z0-9_]+$")


def _features(text: str) -> list[str]:
    """抽取特征：普通 token + 标识符的字符 n-gram（容忍拼写近似）。"""
    features = [token for token in tokenize(text) if _CODEISH.match(token)]
    for ident in _IDENT.findall(text):
        lowered = ident.lower()
        if len(lowered) <= NGRAM:
            features.append(lowered)
            continue
        for start in range(len(lowered) - NGRAM + 1):
            features.append(lowered[start : start + NGRAM])
    return features


@dataclass(frozen=True, slots=True)
class HashingVectorizer:
    """词法哈希向量器（确定性、无依赖）。"""

    dim: int = DEFAULT_DIM

    def vectorize(self, text: str) -> dict[int, float]:
        counts: dict[int, float] = {}
        for feature in _features(text):
            bucket = _bucket(feature, self.dim)
            counts[bucket] = counts.get(bucket, 0.0) + 1.0
        return counts


@dataclass(frozen=True, slots=True)
class ScoredVectorChunk:
    chunk: Chunk
    score: float


class VectorIndex:
    """对 `Chunk` 集合建 TF-IDF 哈希向量索引并做余弦检索。"""

    def __init__(self, chunks: tuple[Chunk, ...], *, vectorizer: Vectorizer | None = None) -> None:
        self._chunks = chunks
        self._vectorizer: Vectorizer = vectorizer or HashingVectorizer()
        raw: list[dict[int, float]] = []
        doc_freq: dict[int, int] = {}
        for chunk in chunks:
            counts = self._vectorizer.vectorize(
                chunk.text + (" " + chunk.symbol if chunk.symbol else "")
            )
            raw.append(counts)
            for bucket in counts:
                doc_freq[bucket] = doc_freq.get(bucket, 0) + 1

        total = len(raw) or 1
        # 平滑 IDF，与 BM25 的 idf 同族，避免除零
        idf = {b: math.log(1.0 + total / (f + 1.0)) for b, f in doc_freq.items()}
        self._vectors: list[dict[int, float]] = []
        for counts in raw:
            weighted = {b: (1.0 + math.log(c)) * idf.get(b, 0.0) for b, c in counts.items()}
            self._vectors.append(_normalize(weighted))

    @property
    def size(self) -> int:
        return len(self._vectors)

    def search(self, query: str, *, limit: int = 10) -> list[ScoredVectorChunk]:
        if not self._vectors:
            return []
        counts = self._vectorizer.vectorize(query)
        if not counts:
            return []
        weighted = {b: (1.0 + math.log(c)) for b, c in counts.items()}
        query_vector = _normalize(weighted)
        scored: list[tuple[float, int]] = []
        for index, vector in enumerate(self._vectors):
            score = _cosine(query_vector, vector)
            if score > 0:
                scored.append((score, index))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                self._chunks[pair[1]].path,
                self._chunks[pair[1]].start_line,
            )
        )
        return [
            ScoredVectorChunk(chunk=self._chunks[i], score=round(s, 6))
            for s, i in scored[: max(1, min(limit, len(self._vectors)))]
        ]


def _normalize(vector: dict[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0:
        return {}
    return {bucket: value / norm for bucket, value in vector.items()}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(bucket, 0.0) for bucket, value in left.items())


__all__ = [
    "DEFAULT_DIM",
    "NGRAM",
    "HashingVectorizer",
    "ScoredVectorChunk",
    "VectorIndex",
    "Vectorizer",
]
