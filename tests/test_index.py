"""P2 检索层测试：符号索引 / BM25 / 三路召回 / 融合排序 / 定位基准。

**不需要网络、不需要 Docker、不需要模型**——全部纯标准库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.index.lexical import BM25Index, tokenize
from patchpilot.index.retrieval import Localizer, LocalizerConfig
from patchpilot.index.symbols import Chunk, index_repo


def write(root: Path, rel: str, text: str) -> None:
    """写 LF 文件（与仓库一致，避免 Windows 换行翻译干扰行号）。"""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(
        root,
        "config.py",
        '''"""读取配置。"""

import json


class ConfigError(Exception):
    """配置错误。"""


def load_config(text):
    return json.loads(text)


def get_setting(config, key):
    if key not in config:
        raise ConfigError(f"缺少配置项: {key}")
    return config[key]
''',
    )
    write(
        root,
        "app.py",
        '''"""应用入口。"""

from config import get_setting, load_config


def main():
    config = load_config("{}")
    timeout = get_setting(config, "timeout")
    return timeout
''',
    )
    write(
        root,
        "utils.py",
        '''def slugify(name):
    return name.lower().replace(" ", "-")


def truncate(text, width):
    return text[:width]
''',
    )
    write(
        root,
        "tests/test_app.py",
        "from app import main\n\n\ndef test_main():\n    assert main() is None\n",
    )
    return root


# ---------------------------------------------------------------- 符号索引


def test_index_extracts_python_symbols_with_line_ranges(project: Path) -> None:
    index = index_repo(project)

    config = index.file("config.py")
    assert config is not None
    names = [s.name for s in config.symbols]

    assert "ConfigError" in names, f"类未索引到，实际 {names}"
    assert "load_config" in names
    assert "get_setting" in names
    get_setting = next(s for s in config.symbols if s.name == "get_setting")
    assert get_setting.start_line < get_setting.end_line
    assert get_setting.kind == "function"


def test_index_records_methods_with_parent(project: Path) -> None:
    write(
        project,
        "svc.py",
        "class Service:\n    def handle(self):\n        return 1\n",
    )
    index = index_repo(project)

    handle = next(s for s in index.file("svc.py").symbols if s.name == "handle")

    assert handle.kind == "method"
    assert handle.parent == "Service"
    assert handle.qualified == "Service.handle"


def test_index_skips_heavy_dirs_and_links(project: Path) -> None:
    write(project, "__pycache__/junk.py", "def junk():\n    pass\n")
    write(project, "node_modules/dep/index.js", "function dep() {}\n")
    index = index_repo(project)

    assert not any("__pycache__" in p for p in index.paths)
    assert not any("node_modules" in p for p in index.paths)


def test_index_is_deterministic(project: Path) -> None:
    first = index_repo(project)
    second = index_repo(project)

    assert first.paths == second.paths
    assert [(s.name, s.start_line) for s in first.file("config.py").symbols] == [
        (s.name, s.start_line) for s in second.file("config.py").symbols
    ]


def test_index_builds_references_for_call_diffusion(project: Path) -> None:
    index = index_repo(project)

    # `get_setting` 定义在 config.py，被 app.py 使用
    assert "config.py" in index.references.get("get_setting", frozenset())
    assert "app.py" in index.references.get("get_setting", frozenset())


def test_regex_fallback_covers_non_python_files(project: Path) -> None:
    """非 Python 语言用**近似**规则（诚实标注：不是完整语法树）。"""
    write(project, "svc.js", "function doWork(x) {\n  return x;\n}\n")

    index = index_repo(project)

    names = [s.name for s in index.file("svc.js").symbols]
    assert "doWork" in names, f"JS 近似规则未命中，实际 {names}"


# ---------------------------------------------------------------- 分词与 BM25


def test_tokenize_splits_snake_and_camel_case() -> None:
    tokens = tokenize("load_config ConfigError")

    assert "load" in tokens
    assert "config" in tokens
    assert "error" in tokens
    assert "load_config" in tokens, "整词也要保留（精确命名查询仍要高分的）"


def test_tokenize_drops_stopwords_and_keeps_cjk() -> None:
    tokens = tokenize("self.x 读取配置失败")

    assert "self" not in tokens
    assert "读取配置失败" in tokens, "无分词器时按整段保留，比乱切更稳"


def test_bm25_ranks_relevant_chunk_first(project: Path) -> None:
    index = index_repo(project)
    chunks = tuple(c for f in index.files for c in f.chunks)
    bm25 = BM25Index(chunks)

    hits = bm25.search("缺少配置项 ConfigError", limit=3)

    assert hits, "应当有命中"
    assert hits[0].chunk.path == "config.py", f"期望 config.py，实际 {hits[0].chunk.path}"


def test_bm25_is_deterministic_on_ties() -> None:
    chunks = (
        Chunk(path="b.py", start_line=1, end_line=2, text="alpha beta"),
        Chunk(path="a.py", start_line=1, end_line=2, text="alpha beta"),
    )
    bm25 = BM25Index(chunks)

    first = [h.chunk.path for h in bm25.search("alpha", limit=2)]
    second = [h.chunk.path for h in bm25.search("alpha", limit=2)]

    assert first == second
    assert first == ["a.py", "b.py"], "同分时按路径稳定排序"


def test_bm25_returns_empty_for_unmatched_query(project: Path) -> None:
    index = index_repo(project)
    chunks = tuple(c for f in index.files for c in f.chunks)

    assert BM25Index(chunks).search("完全无关的词汇zzz", limit=3) == []


# ---------------------------------------------------------------- 三路召回 + 重排


def test_localizer_finds_the_file_named_in_the_issue(project: Path) -> None:
    localizer = Localizer(index_repo(project))

    paths = localizer.localize_paths("调用 get_setting 时抛出的不是 ConfigError", limit=3)

    assert paths[0] == "config.py", f"期望 config.py 排第一，实际 {paths}"


def test_localizer_explains_why_it_ranked(project: Path) -> None:
    """结果必须**可解释**：至少一条理由/通道能说清为什么排这儿。"""
    localizer = Localizer(index_repo(project))

    hits = localizer.localize("调用 get_setting 报错", limit=3)

    assert hits
    assert all(hit.channels for hit in hits), "每条结果都应至少来自一个通道"
    assert any(hit.reasons for hit in hits), "至少一条应有重排理由"


def test_localizer_deprioritises_tests_unless_issue_mentions_them(project: Path) -> None:
    """issue 不提测试时，测试文件应被降权——修复点通常在源码。"""
    localizer = Localizer(index_repo(project))

    paths = localizer.localize_paths("main 返回了错误的值", limit=4)

    assert paths[0] != "tests/test_app.py", f"不应把测试排第一，实际 {paths}"


def test_localizer_boosts_tests_when_issue_mentions_them(project: Path) -> None:
    localizer = Localizer(index_repo(project))

    hits = localizer.localize("test_app 里的用例失败", limit=5)

    assert any(hit.path.startswith("tests/") for hit in hits), (
        f"issue 明确提到测试时应能命中测试文件，实际 {[h.path for h in hits]}"
    )


def test_ablation_switch_actually_changes_channels(project: Path) -> None:
    """消融开关必须真的生效（否则"消融表"是假的）。"""
    index = index_repo(project)

    only_lexical = Localizer(
        index,
        config=LocalizerConfig(
            use_symbol=False,
            use_lexical=True,
            use_vector=False,
            use_graph=False,
            use_rerank=False,
        ),
    )
    hits = only_lexical.localize("get_setting 报错", limit=3)

    assert hits
    assert all(hit.channels == ("lexical",) for hit in hits), (
        f"关闭符号与图通道后，结果应只来自 lexical，实际 {[h.channels for h in hits]}"
    )


def test_localizer_returns_empty_when_nothing_matches(project: Path) -> None:
    localizer = Localizer(index_repo(project))

    assert localizer.localize("zzz 完全不存在的词", limit=3) == []


def test_graph_channel_diffuses_to_callers(project: Path) -> None:
    """调用图通道：命中定义处后，应能把**使用它的文件**也带出来。"""
    index = index_repo(project)
    localizer = Localizer(
        index,
        config=LocalizerConfig(use_symbol=True, use_lexical=False, use_graph=True),
    )

    paths = localizer.localize_paths("get_setting", limit=5)

    assert "config.py" in paths
    assert "app.py" in paths, f"调用图应把调用方带出来，实际 {paths}"


# ---------------------------------------------------------------- 向量通道（词法哈希）


def test_vectorizer_is_deterministic_across_calls() -> None:
    """**必须跨进程稳定**：用 BLAKE2b 分桶，绝不能用内置 `hash()`（每进程加盐）。"""
    from patchpilot.index.vectors import HashingVectorizer

    vectorizer = HashingVectorizer()
    first = vectorizer.vectorize("load_config ConfigError")
    second = vectorizer.vectorize("load_config ConfigError")

    assert first == second
    assert first, "空向量说明特征抽取失败"


def test_vectorizer_features_include_char_ngrams() -> None:
    """字符 n-gram 是"容忍拼写近似"的来源：`getSetting` 与 `get_setting` 应共享桶。"""
    from patchpilot.index.vectors import HashingVectorizer

    vectorizer = HashingVectorizer()
    camel = set(vectorizer.vectorize("getSetting"))
    snake = set(vectorizer.vectorize("get_setting"))

    assert camel & snake, "近拼写应至少共享一个桶（否则容错能力为零）"


def test_vector_index_ranks_the_relevant_chunk_first(project: Path) -> None:
    from patchpilot.index.vectors import VectorIndex

    index = index_repo(project)
    chunks = tuple(c for f in index.files for c in f.chunks)

    hits = VectorIndex(chunks).search("ConfigError 配置项缺失", limit=3)

    assert hits
    assert hits[0].chunk.path == "config.py", f"期望 config.py，实际 {hits[0].chunk.path}"


def test_vector_index_is_deterministic_on_ties() -> None:
    from patchpilot.index.vectors import VectorIndex

    chunks = (
        Chunk(path="b.py", start_line=1, end_line=2, text="alpha beta gamma"),
        Chunk(path="a.py", start_line=1, end_line=2, text="alpha beta gamma"),
    )
    first = [h.chunk.path for h in VectorIndex(chunks).search("alpha beta", limit=2)]
    second = [h.chunk.path for h in VectorIndex(chunks).search("alpha beta", limit=2)]

    assert first == second
    assert first == ["a.py", "b.py"], "同分时按路径稳定排序"


def test_all_channels_off_yields_nothing(project: Path) -> None:
    """消融必须能全部关闭（否则"消融表"是假的）。"""
    index = index_repo(project)

    without = Localizer(
        index,
        config=LocalizerConfig(
            use_symbol=False, use_lexical=False, use_vector=False, use_graph=False
        ),
    )

    assert without.localize("get_setting", limit=3) == []


def test_channel_cutoff_prevents_long_tail_dilution() -> None:
    """每通道只取前 K 名参与融合——否则低名次小分累加会淹没头名命中。

    这是 RRF 的标准做法；实测不截断时融合结果**低于**单通道（见 PLAN 消融表）。
    """
    from patchpilot.index.retrieval import CHANNEL_CUTOFF

    assert CHANNEL_CUTOFF >= 1
    assert CHANNEL_CUTOFF < 60, "截断太宽等于没截断（融合 pool 默认 60）"


# ---------------------------------------------------------------- 定位基准


def test_locate_bench_reports_both_groups(tmp_path: Path) -> None:
    """基准必须分别报告 corpus 与 history 两组（否则数字不可解释）。"""
    from patchpilot.eval.locate_bench import run_locate_bench

    project = tmp_path / "proj"
    write(project, "mod.py", "def handler():\n    return 1\n")
    write(project, "other.py", "def unrelated():\n    return 2\n")

    import subprocess

    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
    ):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "修复 handler 返回值不对"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    outcome = run_locate_bench(git_root=project, scope=project, history_limit=10)

    groups = {score.group for score in outcome.scores}
    assert "history" in groups
    assert outcome.table(), "消融表不能为空"


def test_locate_bench_expected_paths_share_the_index_coordinate_system(tmp_path: Path) -> None:
    """回归：期望路径与索引路径必须同一坐标系（第一版因此全 0）。"""
    from patchpilot.eval.locate_bench import cases_from_history

    project = tmp_path / "proj"
    write(project, "pkg/mod.py", "def target_fn():\n    return 1\n")

    import subprocess

    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
    ):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "修复 target_fn 的返回值"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    cases = cases_from_history(git_root=project, scope=project / "pkg", limit=5)

    assert cases, "应当有题目"
    for case in cases:
        for expected in case.expected_files:
            assert case.index.file(expected) is not None, (
                f"期望路径 {expected} 必须能在索引里找到（坐标系一致）"
            )


# ---------------------------------------------------------------- 合成语料（第三组）


def _synthetic_task(task_id: str = "syn-01-easy", *, rel: str = "loader.py") -> dict:
    return {
        "id": task_id,
        "files": {
            "loader.py": "def load(text):\n    return text.split('=')\n",
            "Issue.md": "解析 `key=value` 时崩溃。",
        },
        "issue_file": "Issue.md",
        "expected_files": [rel],
        "tags": ["synthetic", "easy"],
    }


def test_synthetic_cases_build_from_corpus_json(tmp_path: Path) -> None:
    import json

    from patchpilot.eval.locate_bench import cases_from_locate_corpus

    corpus = tmp_path / "loc.json"
    corpus.write_bytes(
        json.dumps({"tasks": [_synthetic_task()]}, ensure_ascii=False).encode("utf-8")
    )

    cases = cases_from_locate_corpus(corpus)

    assert len(cases) == 1
    case = cases[0]
    assert case.group == "synthetic"
    assert case.expected_files == {"loader.py"}
    assert case.index.file("loader.py") is not None, "期望文件必须在索引里（坐标系一致）"
    assert "key=value" in case.query


def test_synthetic_cases_reject_path_traversal(tmp_path: Path) -> None:
    """语料路径不得逃逸（与 `EvalTask.materialize` 同口径）。"""
    import json

    from patchpilot.eval.locate_bench import cases_from_locate_corpus

    task = _synthetic_task()
    task["files"] = {"../escaped.py": "x = 1\n", "Issue.md": "坏了"}
    task["expected_files"] = ["../escaped.py"]
    corpus = tmp_path / "loc.json"
    corpus.write_bytes(json.dumps({"tasks": [task]}, ensure_ascii=False).encode("utf-8"))

    with pytest.raises(ValueError, match="逃逸"):
        cases_from_locate_corpus(corpus)


def test_shipped_synthetic_corpus_is_well_formed() -> None:
    """入库的合成语料必须结构自洽（恰好一个期望文件、在 files 里、不是测试、有难度标签）。"""
    import json

    corpus = Path(__file__).resolve().parents[1] / "eval" / "locate_corpus.json"
    assert corpus.is_file(), "合成语料应已入库"

    payload = json.loads(corpus.read_text(encoding="utf-8"))
    tasks = payload["tasks"]
    assert len(tasks) >= 20, f"样本量太少（{len(tasks)}）"

    for task in tasks:
        expected = task["expected_files"]
        files = task["files"]
        assert len(expected) == 1, f"{task['id']}: 期望集合应恰好一个文件"
        assert expected[0] in files, f"{task['id']}: 期望文件不在 files 里"
        assert "test" not in expected[0], f"{task['id']}: 期望文件不该是测试"
        assert task["issue_file"] in files, f"{task['id']}: issue 文件缺失"
        assert set(task["tags"]) & {"easy", "hard"}, f"{task['id']}: 缺难度标签"


def test_locate_bench_covers_three_groups_and_aggregate(tmp_path: Path) -> None:
    """基准必须报告三组 + 汇总（单组样本小，汇总才是可读结论）。"""
    import json
    import subprocess

    from patchpilot.eval.locate_bench import run_locate_bench

    project = tmp_path / "proj"
    write(project, "mod.py", "def handler():\n    return 1\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
    ):
        subprocess.run(cmd, cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "修复 handler 的返回值"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    corpus = tmp_path / "loc.json"
    corpus.write_bytes(
        json.dumps({"tasks": [_synthetic_task()]}, ensure_ascii=False).encode("utf-8")
    )

    outcome = run_locate_bench(
        git_root=project, scope=project, synthetic_path=corpus, history_limit=5
    )

    groups = {score.group for score in outcome.scores}
    assert "synthetic" in groups
    assert "history" in groups
    assert "all" in groups, "必须有跨组汇总行"
    assert len([s for s in outcome.scores if s.group == "all"]) >= 2, (
        "汇总行应对每个配置各有一条"
    )


# ---------------------------------------------------------------- 闭环接入


def test_repair_locate_uses_the_index_by_default(tmp_path: Path) -> None:
    """修复闭环默认走三路召回；`use_index_localizer=False` 时回到旧路径。

    这条守的是"P2 真的接进去了"——只加模块不接线，定位质量不会有任何变化。
    """
    from patchpilot.graph.repair import RepairOptions

    options = RepairOptions(repo=tmp_path, issue_file=tmp_path / "i.md")
    assert options.use_index_localizer is True
    assert (
        RepairOptions(
            repo=tmp_path, issue_file=tmp_path / "i.md", use_index_localizer=False
        ).use_index_localizer
        is False
    )


def test_repair_locate_actually_runs_the_index(project: Path, tmp_path: Path) -> None:
    """端到端：闭环的 `locate` 必须真的走索引路径，并在备注里报出用了哪些通道。

    这里断言的是**接线**（索引确实被调用、通道被记录），**不是**质量差异——
    质量证据在定位基准的消融表里（`eval/locate_bench.py`）。
    只加模块不接线时，定位质量不会有任何变化，所以必须有一条用例守接线。
    """
    import subprocess

    from patchpilot.core.budget import Budget
    from patchpilot.core.llm import LLMResponse
    from patchpilot.graph.repair import RepairOptions, run_repair
    from patchpilot.sandbox.junit import parse_junit_text
    from patchpilot.sandbox.manager import SandboxResult
    from patchpilot.sandbox.policy import SandboxPolicy

    repo = tmp_path / "repo"
    write(
        repo,
        "config.py",
        "class ConfigError(Exception):\n"
        "    pass\n"
        "\n\n"
        "def get_setting(cfg, k):\n"
        "    return cfg[k]\n",
    )
    write(repo, "unrelated.py", "def nothing():\n    return 1\n")
    write(repo, "tests/test_config.py", "def test_x():\n    assert True\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@e.com"],
        ["git", "config", "user.name", "t"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    issue = tmp_path / "bug.md"
    write(tmp_path, "bug.md", "调用 get_setting() 时抛出的不是 ConfigError。")
    patch = (
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,5 +1,5 @@\n"
        " class ConfigError(Exception):\n"
        "     pass\n"
        " \n"
        " \n"
        "-def get_setting(cfg, k):\n"
        "+def get_setting(cfg, k, default=None):\n"
        "     return cfg[k]\n"
    )

    class _LLM:
        model = "stub"

        async def complete(self, messages, *, temperature=0.0, model=None):  # noqa: ANN001, ANN202
            return LLMResponse(
                text=patch, prompt_tokens=10, completion_tokens=5, model="stub", cost_usd=0.0
            )

    def runner(*, source: Path, pytest_args):  # noqa: ANN001, ANN202
        return SandboxResult(
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
            container_name="pp-fake",
            report=parse_junit_text(
                '<testsuites><testsuite name="s" tests="1" failures="0" errors="0">'
                '<testcase classname="tests.test_config" name="test_x"/></testsuite></testsuites>'
            ),
        )

    final = run_repair(
        options=RepairOptions(
            repo=repo,
            issue_file=issue,
            tests=("tests/test_config.py",),
            policy=SandboxPolicy(sandbox_root=tmp_path / "sb"),
            budget=Budget(max_attempts=10, max_tokens=100_000, max_seconds=600, max_usd=0),
            copy_root=tmp_path / "copies",
        ),
        llm=_LLM(),
        runner=runner,
    )

    assert final["candidates"][0] == "config.py", final["candidates"]
    note = next(n for n in final["notes"] if n.startswith("locate:"))
    assert "通道" in note and "fallback" not in note, (
        f"定位应走索引通道而不是兜底路径，实际备注: {note}"
    )


def test_localizing_a_pure_chinese_issue_is_a_known_limitation(project: Path) -> None:
    """**如实记录的已知限制**：纯中文 issue 定位不到（分词器不做翻译）。

    代码里没有中文标识符时，"取配置项失败"这类中文描述与 `get_setting` 之间没有
    任何字面对应关系，纯词法/符号通道无法建立联系。真实用户报告通常带英文报错或
    标识符，故影响有限；但**必须显式登记**，避免被当成"已支持"。
    """
    localizer = Localizer(index_repo(project))

    paths = localizer.localize_paths("取配置项时缺失的键没有按要求抛出错误", limit=3)

    assert paths == [], (
        f"当前实现定位不到纯中文 issue（这是已登记的限制）；若这条变了说明加了翻译/向量，"
        f"请更新本用例与 PLAN 里的登记。实际: {paths}"
    )
