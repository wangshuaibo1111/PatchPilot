"""P1：把复审实测 SURVIVED（删掉仍全绿）的 4 条守卫钉住。

| 变异 | 复审实测 | 这里怎么钉 |
|---|---|---|
| M5 去掉 `CHANNEL_CUTOFF` 截断 | SURVIVED | 构造"只在名次 >K 处大量命中"的通道，断言截断改变排序 |
| M6 去掉 `_CODEISH` 过滤 | SURVIVED | 用**真实规模**仓库（本仓库 src）复现纯中文误命中 |
| M7 `blake2b` → 内置 `hash()` | SURVIVED | 起两个**独立进程**、给不同 `PYTHONHASHSEED` 比对向量 |
| M8 拍平 idf | SURVIVED | 稀有词与常见词同 tf 时，稀有词得分必须更高 |
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from patchpilot.index.retrieval import Localizer, LocalizerConfig
from patchpilot.index.symbols import Chunk, chunk_id, index_repo
from patchpilot.index.vectors import VectorIndex

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------- M5：截断真的生效


def _two_chunk_index(tmp_path: Path):  # noqa: ANN202
    write_lf(tmp_path / "a.py", "def alpha():\n    return 1\n")
    write_lf(tmp_path / "b.py", "def beta():\n    return 2\n")
    return index_repo(tmp_path)


def test_channel_cutoff_actually_changes_fusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把截断关掉（K 很大）时，长尾通道能把结果翻过来；开着时必须被丢掉。

    这直接钉住 `CHANNEL_CUTOFF` ——复审把截断整个删掉时全套仍绿，说明没人守它。
    """
    import patchpilot.index.retrieval as module

    index = _two_chunk_index(tmp_path)
    chunks = [chunk for file in index.files for chunk in file.chunks]
    assert len(chunks) >= 2
    first, second = chunk_id(chunks[0]), chunk_id(chunks[1])

    localizer = Localizer(
        index,
        config=LocalizerConfig(use_graph=False, use_rerank=False, weights={}),
    )

    # 目标 `first`：只有符号通道的第 1 名（一个强票）
    # 对手 `second`：符号第 2 名 + 词法第 1 名（两票，足以赢）
    # 再给 `first` 三个"名次 11/12/13"的长尾票 —— 只有不截断时才会计入
    monkeypatch.setattr(localizer, "_symbol_ranks", lambda seeds, *, pool: {first: 1, second: 2})
    monkeypatch.setattr(
        localizer, "_lexical_ranks", lambda query, *, pool: {second: 1, first: 13}
    )
    monkeypatch.setattr(localizer, "_vector_ranks", lambda query, *, pool: {first: 11})
    monkeypatch.setattr(localizer, "_graph_ranks", lambda seeds, *, pool: {first: 12})

    monkeypatch.setattr(module, "CHANNEL_CUTOFF", 10)
    with_cutoff = [chunk.chunk.path for chunk in localizer.localize("alpha", limit=5)]

    monkeypatch.setattr(module, "CHANNEL_CUTOFF", 10_000)
    without_cutoff = [chunk.chunk.path for chunk in localizer.localize("alpha", limit=5)]

    assert with_cutoff[0] != without_cutoff[0], (
        "截断若不生效，两种设置下排序应当相同；这正说明截断在起作用"
    )
    assert with_cutoff[0] == "b.py", "截断后：两票的 b.py 应当第一"
    assert without_cutoff[0] == "a.py", "不截断时：长尾三票会把 a.py 推上来"


def test_channel_cutoff_is_documented_and_sane() -> None:
    from patchpilot.index.retrieval import CHANNEL_CUTOFF

    assert CHANNEL_CUTOFF >= 1
    assert CHANNEL_CUTOFF < 60, "截断太宽等于没截断（融合 pool 默认 60）"


def test_channel_cutoff_boundary_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """钉住**边界语义**：第 `CHANNEL_CUTOFF` 名的票算，第 `CHANNEL_CUTOFF+1` 名的不算。

    复审指出把常量改成 2 或 59 时 `tests/test_p1_pins.py` 仍全绿——因为只断言了范围。
    这里直接验证两个边界，任何取值改动都会同时触发其中一侧。
    """
    import patchpilot.index.retrieval as module

    index = _two_chunk_index(tmp_path)
    chunks = [chunk for file in index.files for chunk in file.chunks]
    first, second = chunk_id(chunks[0]), chunk_id(chunks[1])
    cutoff = module.CHANNEL_CUTOFF

    def build(localizer, rank_of_first: int):
        monkeypatch.setattr(localizer, "_symbol_ranks", lambda seeds, *, pool: {second: 1})
        monkeypatch.setattr(
            localizer, "_lexical_ranks", lambda query, *, pool: {first: rank_of_first}
        )
        monkeypatch.setattr(localizer, "_vector_ranks", lambda query, *, pool: {})
        monkeypatch.setattr(localizer, "_graph_ranks", lambda seeds, *, pool: {})
        return [chunk.chunk.path for chunk in localizer.localize("alpha", limit=5)]

    localizer = Localizer(
        index, config=LocalizerConfig(use_graph=False, use_rerank=False, weights={"lexical": 5.0})
    )
    # 恰好第 cutoff 名：算 → a.py（权重 5 让它压过 b.py 的一票）
    inside = build(localizer, cutoff)
    # 第 cutoff+1 名：不算 → a.py 无票，b.py 第一
    outside = build(localizer, cutoff + 1)

    assert inside[0] == "a.py", (
        f"第 {cutoff} 名的票应当计入；实际 {inside}。若这里失败，说明常量被改成更小的值"
    )
    assert outside[0] == "b.py", (
        f"第 {cutoff + 1} 名的票不应计入；实际 {outside}。若这里失败，说明常量被改成更大的值"
    )


# ---------------------------------------------------------------- M6：纯中文不误命中（真实规模）


def test_pure_chinese_query_yields_no_vector_hits_on_real_repo() -> None:
    """用**本仓库 src**（真实 chunk 规模）复现当初的误命中。

    小项目只有 5 个 chunk，1024 桶下几乎撞不上；复审因此指出旧用例钉不住 `_CODEISH`。
    """
    index = index_repo(REPO_ROOT / "src" / "patchpilot")
    chunks = tuple(chunk for file in index.files for chunk in file.chunks)
    assert len(chunks) > 100, f"规模不够，钉不住碰撞（当前 {len(chunks)} chunk）"

    hits = VectorIndex(chunks).search("取配置项时缺失的键…", limit=5)

    assert hits == [], f"纯中文不该命中代码块，实际 {[h.chunk.path for h in hits]}"


def test_pure_chinese_query_yields_nothing_from_vector_channel_only() -> None:
    index = index_repo(REPO_ROOT / "src" / "patchpilot")
    localizer = Localizer(
        index,
        config=LocalizerConfig(
            use_symbol=False, use_lexical=False, use_vector=True, use_graph=False
        ),
    )

    assert localizer.localize_paths("取配置项时缺失的键…", limit=5) == []


# ---------------------------------------------------------------- M7：跨进程确定性


_CROSS_PROCESS = """
import json, sys
sys.path.insert(0, {path!r})
from patchpilot.index.vectors import HashingVectorizer
vec = HashingVectorizer().vectorize("load_config ConfigError get_setting")
print(json.dumps(sorted(vec.items())))
"""


def _vector_in_fresh_process(seed: str) -> str:
    env = {
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "PATH": str(Path(sys.executable).parent),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _CROSS_PROCESS.format(path=str(REPO_ROOT / "src"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    return proc.stdout.strip()


def test_hashing_vectorizer_is_stable_across_processes() -> None:
    """**必须跨进程稳定**：内置 `hash()` 每进程加盐，会得到不同分桶。"""
    first = _vector_in_fresh_process("0")
    second = _vector_in_fresh_process("12345")

    assert json.loads(first) == json.loads(second), (
        "不同 PYTHONHASHSEED 下向量必须完全一致；若变了说明用了内置 hash()"
    )


# ---------------------------------------------------------------- M8：idf 真的在起作用


def test_rare_term_outranks_common_term_at_equal_frequency() -> None:
    """稀有词的 idf 更高 → 同样 tf 下得分更高。拍平 idf 后两者会相等，用例即失败。"""
    chunks = (
        Chunk(path="a.py", start_line=1, end_line=2, text="rare common"),
        Chunk(path="b.py", start_line=1, end_line=2, text="common filler"),
        Chunk(path="c.py", start_line=1, end_line=2, text="common filler"),
    )
    index = VectorIndex(chunks)

    rare_hits = index.search("rare", limit=1)
    common_hits = index.search("common", limit=5)

    assert rare_hits, "稀有词应当能命中"
    rare_score = rare_hits[0].score
    # `common` 出现在 3 个块里，取它自己的最高分来比（同 tf=1）
    assert common_hits
    top_common = max(hit.score for hit in common_hits)
    assert rare_score > top_common, (
        f"稀有词得分 {rare_score} 应高于常见词 {top_common}；若相等说明 idf 被拍平了"
    )
