from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from patchpilot.core.preflight import (
    SKIP_DIR_SUFFIXES,
    SKIP_DIRS,
    SKIP_FILE_SUFFIXES,
    SKIP_FILES,
    _check_skip_rules,
    _check_worktree_matches_index,
    _parse_pytest_collection,
    _pytest_targets,
    iter_scan_files,
    run_preflight,
    scan_secrets,
    sniff_text,
)

# 本文件包含"长得像真密钥"的字符串，专门用于验证扫描器能否正确识别。
# 行尾注释标记会让扫描器豁免该行；而被测的目标文件里没有标记，因此仍会被检出。
ALLOW = "preflight:allow-secret"
FAKE_KEY = "sk-abcdefghijklmnopqrst"  # preflight:allow-secret
PRIVATE_KEY_HEADER = "-----BEGIN RSA PRIVATE KEY-----"  # preflight:allow-secret
# 下面两个"真实密钥"含占位符词，专门用来证明子串匹配式的豁免已被修掉
REAL_KEY_WITH_PLACEHOLDER_WORD = "SampleCorp-2026-Prod-Pass-9f3aK2"
REAL_KEY_WITH_XXX_PREFIX = "xxxCompanyRealToken9f3aK2mQ7pR4"


def _full_gitignore() -> str:
    """由 SKIP 规则反推出「双向一致」的 .gitignore，供测试夹具使用。

    刻意生成而不是手写：手写清单会随 SKIP 规则演进而失配，
    让"干净的仓库"用例因为 skip_rules 的反向检查失败而误报。
    """
    lines = [f"{name}/" for name in sorted(SKIP_DIRS - {".git"})]
    lines += [f"*{suffix}/" for suffix in sorted(SKIP_DIR_SUFFIXES)]
    lines += [f"*{suffix}" for suffix in sorted(SKIP_FILE_SUFFIXES)]
    lines += sorted(SKIP_FILES)
    return "\n".join(lines) + "\n"


IGNORE_ALL = _full_gitignore()


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    (tmp_path / ".gitignore").write_text(IGNORE_ALL, encoding="utf-8", newline="\n")
    return tmp_path


def _git_env(tmp_path: Path) -> dict[str, str]:
    """让临时仓库与**外层仓库彻底隔离**的环境变量。

    门禁自己跑 pytest 时用 `--basetemp <root>/.patchpilot/pytest-tmp`，并在其下建
    **每次运行独占**的子目录 `run-<pid>-<hex>`（避免并发门禁互相删临时目录），
    于是 `tmp_path` 落在 PatchPilot 自己的仓库里：此时 `git init` 不会新建仓库，
    `git rev-parse --show-toplevel` 会向上找到 PatchPilot —— 夹具的 `tmp_path`
    被静默换成了外层仓库（实测表现为 `worktree_matches_index` 报 `skip`）。
    `GIT_CEILING_DIRECTORIES` 是 git 提供的官方手段：向上查找到此为止。
    """
    ceiling = tmp_path.resolve().parent
    return {
        **os.environ,
        # 大小写两种写法都放进去：Windows 上 git 的路径比较不保证大小写无关
        "GIT_CEILING_DIRECTORIES": f"{ceiling}{os.pathsep}{str(ceiling).swapcase()}",
    }


def _git_init(tmp_path: Path) -> None:
    """在临时目录里建一个可控的仓库：LF 原样入库，避免平台换行干扰。

    必须关掉 `core.autocrlf`：否则同一份内容在 Windows 上入库变 LF、
    在 Linux 上保持原样，`worktree_matches_index` 的断言会随平台漂移。
    """
    if shutil.which("git") is None:
        pytest.skip("需要 git 可执行文件")
    env = _git_env(tmp_path)
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True, env=env
    )
    # 自证夹具成立：init 若因外层仓库/权限静默失败，后面的断言会指向错误的方向
    assert (tmp_path / ".git").exists(), "git init 未在本目录创建仓库（外层仓库干扰？）"
    for key, value in (
        ("core.autocrlf", "false"),
        ("user.name", "preflight-test"),
        ("user.email", "preflight@example.invalid"),
    ):
        subprocess.run(
            ["git", "config", key, value],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env=env,
        )


def _git_add_all(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "add", "-A"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=_git_env(tmp_path),
    )


def _check_by_name(report, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in report.checks if c.name == name)


def test_scan_finds_real_looking_key(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/app.py": f'KEY = "{FAKE_KEY}"\n'})

    result = scan_secrets(tmp_path)

    assert len(result.findings) == 1
    assert "src/app.py:1" in result.findings[0]
    assert "密钥" in result.findings[0]


def test_scan_ignores_whole_value_placeholders(tmp_path: Path) -> None:
    _make_repo(
        tmp_path,
        {
            "src/app.py": (
                'A = "your-api-key-here-placeholder"\n'
                'B = "sk-your-placeholder-value-000000"\n'
                'C = "changeme-changeme-changeme"\n'
            )
        },
    )

    assert scan_secrets(tmp_path).findings == []


def test_scan_no_longer_waives_real_keys_containing_placeholder_words(tmp_path: Path) -> None:
    """审查 BLOCKING-4：占位符判定必须严格到整值/前缀，不能按子串放行。"""
    _make_repo(
        tmp_path,
        {
            "src/app.py": (
                f'password = "{REAL_KEY_WITH_PLACEHOLDER_WORD}"\n'  # preflight:allow-secret
                f'token = "{REAL_KEY_WITH_XXX_PREFIX}"\n'  # preflight:allow-secret
            )
        },
    )

    assert len(scan_secrets(tmp_path).findings) == 2


def test_scan_flags_private_key_block(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"deploy/id_rsa": f"{PRIVATE_KEY_HEADER}\nMIIE\n"})

    assert any("私钥" in f for f in scan_secrets(tmp_path).findings)


def test_allow_marker_must_be_a_comment(tmp_path: Path) -> None:
    """审查 BLOCKING-6：标记只以裸文本出现时不得生效。"""
    _make_repo(tmp_path, {"src/app.py": f'K = "{FAKE_KEY}"  {ALLOW}\n'})

    assert len(scan_secrets(tmp_path).findings) == 1


def test_allow_marker_records_waived_items(tmp_path: Path) -> None:
    """审查 BLOCKING-6：豁免必须可审计。"""
    _make_repo(tmp_path, {"tests/fixture.py": f'K = "{FAKE_KEY}"  # {ALLOW}\n'})

    result = scan_secrets(tmp_path)

    assert result.findings == []
    assert len(result.waived) == 1
    assert "tests/fixture.py:1" in result.waived[0]


def test_allow_marker_inside_string_literal_does_not_waive(tmp_path: Path) -> None:
    """二轮审查 NON_BLOCKING-3：标记必须是**真注释**，写在字符串字面量里不算。"""
    content = f'K = "{FAKE_KEY}"\n' 'MARKER_TEXT = "preflight:allow-secret"\n'
    _make_repo(tmp_path, {"src/app.py": content})

    findings = scan_secrets(tmp_path).findings

    assert len(findings) == 1
    assert "src/app.py:1" in findings[0]


def test_large_files_are_reported_not_silently_skipped(tmp_path: Path) -> None:
    """审查 BLOCKING-5：被跳过的文件必须在报告里可见。"""
    _make_repo(tmp_path, {"src/big.py": 'K = "' + ("a" * 600_000) + '"\n'})

    result = scan_secrets(tmp_path)

    assert result.findings == []
    assert len(result.skipped_large) == 1
    assert "src/big.py" in result.skipped_large[0]


def test_env_file_is_not_scanned(tmp_path: Path) -> None:
    _make_repo(tmp_path, {".env": f'PP_LLM_API_KEY="{FAKE_KEY}"\n'})

    assert scan_secrets(tmp_path).findings == []
    assert ".env" not in {p.name for p in iter_scan_files(tmp_path)}


def test_gitignore_check_is_not_fooled_by_env_example(tmp_path: Path) -> None:
    """审查 BLOCKING-7：只写 .env.example 不等于忽略了 .env。"""
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    (tmp_path / ".gitignore").write_text(
        ".env.example\n.patchpilot/\n__pycache__/\n.conda/\n.venv/\n", encoding="utf-8"
    )

    report = run_preflight(tmp_path, run_tests=False)

    assert {c.name for c in report.failures()} == {"gitignore", "skip_rules"}
    assert report.ok is False


def test_skip_rules_detects_skip_only_items(tmp_path: Path) -> None:
    """四轮审查阻塞 2：SKIP 独有项目 git **不会**忽略，会绕过摘要与密钥扫描。

    实测后果：`node_modules/pkg/index.py` 里一条 `sk-` 密钥可被提交，
    而门禁四道检查全绿。反向自检必须拦住它。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    kept = [
        line
        for line in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line != "node_modules/"
    ]
    (tmp_path / ".gitignore").write_text("\n".join(kept) + "\n", encoding="utf-8")

    result = _check_skip_rules(tmp_path)

    assert result.status == "fail"
    assert "node_modules" in result.detail


def test_case_variants_match_skip_rules(tmp_path: Path) -> None:
    """四轮审查阻塞 1：Windows 上 git 的忽略匹配大小写无关，SKIP 规则必须一致。

    否则 `Venv/`、`BUILD/` 这类变体会被 git 忽略却被遍历收录 → 摘要分叉。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    if os.name != "nt":
        pytest.skip("大小写无关的忽略匹配是 Windows/git core.ignorecase 的行为")

    base = run_preflight(tmp_path, run_tests=False)
    for artifact in ("Venv/pyvenv.cfg", "BUILD/keep.txt", "DIST/keep.txt", ".ENV"):
        path = tmp_path / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk", encoding="utf-8")

    after = run_preflight(tmp_path, run_tests=False)

    assert after.tree_digest == base.tree_digest
    assert after.file_count == base.file_count


def test_skip_dirs_match_relative_path_only(tmp_path: Path) -> None:
    """审查 BLOCKING-3：祖先目录叫 build/ 不能让整个仓库被静默跳过。"""
    root = tmp_path / "build" / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    (root / ".gitignore").write_text(IGNORE_ALL, encoding="utf-8")

    report = run_preflight(root, run_tests=False)

    syntax = next(c for c in report.checks if c.name == "syntax")
    assert syntax.status == "fail"
    assert "broken.py" in syntax.detail


def test_non_utf8_python_file_fails_instead_of_crashing(tmp_path: Path) -> None:
    """审查 BLOCKING-8：非 UTF-8 源码必须变成 fail，而不是让门禁崩溃。"""
    _make_repo(tmp_path, {})
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")

    report = run_preflight(tmp_path, run_tests=False)

    syntax = next(c for c in report.checks if c.name == "syntax")
    assert syntax.status == "fail"
    assert "latin.py" in syntax.detail


def test_report_ok_when_everything_is_clean(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})

    report = run_preflight(tmp_path, run_tests=False)

    assert report.ok is True
    assert report.failures() == []
    assert "preflight=OK" in report.summary_line()
    assert '"ok": true' in report.to_json()


def test_report_fails_on_secret(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/app.py": f'K = "{FAKE_KEY}"\n'})

    report = run_preflight(tmp_path, run_tests=False)

    assert report.ok is False
    assert {c.name for c in report.failures()} == {"secret_scan"}
    assert report.secrets


def test_syntax_check_detects_broken_file(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/broken.py": "def f(:\n    pass\n"})

    syntax = next(
        c for c in run_preflight(tmp_path, run_tests=False).checks if c.name == "syntax"
    )

    assert syntax.status == "fail"
    assert "src/broken.py" in syntax.detail


def test_syntax_check_fails_loudly_when_nothing_was_scanned(tmp_path: Path) -> None:
    """审查 BLOCKING-3：一个文件都没解析到时报通过是危险的静默假通过。"""
    _make_repo(tmp_path, {})
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "not_python.txt").write_text("hello", encoding="utf-8")

    syntax = next(
        c for c in run_preflight(tmp_path, run_tests=False).checks if c.name == "syntax"
    )

    assert syntax.status == "fail"
    assert "未找到任何 Python 文件" in syntax.detail


def test_tree_digest_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    """审查范围必须可唯一确定：APPROVE 要能绑定到具体字节。"""
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})

    first = run_preflight(tmp_path, run_tests=False)
    second = run_preflight(tmp_path, run_tests=False)

    assert first.tree_digest == second.tree_digest
    assert len(first.tree_digest) == 32
    assert first.file_count >= 1

    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

    assert run_preflight(tmp_path, run_tests=False).tree_digest != first.tree_digest


def test_tree_digest_is_independent_of_unreviewed_artifacts(tmp_path: Path) -> None:
    """摘要必须与位置无关：构建产物/缓存的存在不得改变批准对象。

    起因：工作副本里残留的 src/*.egg-info 曾让摘要与部署目录不一致，
    使"审查者批准的是哪一份字节"失去意义。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    before = run_preflight(tmp_path, run_tests=False).tree_digest

    egg = tmp_path / "src" / "patchpilot.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("junk", encoding="utf-8")
    cache = tmp_path / ".ruff_cache"
    cache.mkdir()
    (cache / "junk").write_text("junk", encoding="utf-8")

    after = run_preflight(tmp_path, run_tests=False)

    assert after.tree_digest == before
    assert after.file_count == 2  # src/app.py + .gitignore


def test_tree_digest_changes_when_a_file_is_added(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    before = run_preflight(tmp_path, run_tests=False).tree_digest

    (tmp_path / "src" / "extra.py").write_text("y = 1\n", encoding="utf-8")

    assert run_preflight(tmp_path, run_tests=False).tree_digest != before


def test_summary_line_carries_digest(tmp_path: Path) -> None:
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})

    report = run_preflight(tmp_path, run_tests=False)

    assert report.tree_digest[:12] in report.summary_line()


def test_skip_rules_cover_gitignore_artifacts(tmp_path: Path) -> None:
    """SKIP 规则必须与 .gitignore 保持同步。

    非 git 仓库走目录遍历时只有 SKIP 规则能替代 .gitignore；
    一旦二者不一致，"工作副本（遍历）"与"部署目录（git ls-files）"
    算出的摘要就会分叉，批准绑定随之失效。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    base = run_preflight(tmp_path, run_tests=False)

    for artifact in (
        ".vscode/settings.json",
        ".idea/workspace.xml",
        "cache.sqlite",
        "notes.sqlite3",
        ".DS_Store",
        "src/mod.cpython-312.pyc",
        "venv/pyvenv.cfg",
        "venv/Lib/site-packages/pkg.py",
    ):
        path = tmp_path / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("junk", encoding="utf-8")

    after = run_preflight(tmp_path, run_tests=False)

    assert after.tree_digest == base.tree_digest
    assert after.file_count == base.file_count


def test_run_preflight_survives_enumeration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """三轮审查 B3：枚举/摘要异常必须转成 fail 结果，不能逃逸成 traceback。

    刻意不用 `tmp_path`：这条分支不依赖真实目录，应当在任何环境都能执行。
    """
    import patchpilot.core.preflight as preflight

    def boom(_root: Path) -> list[Path]:
        raise PermissionError("injected enumeration failure")

    monkeypatch.setattr(preflight, "iter_scan_files", boom)

    report = preflight.run_preflight(Path("."), run_tests=False)

    assert report.ok is False
    assert [c.name for c in report.failures()] == ["enumerate"]
    assert report.file_count == 0
    assert json.loads(report.to_json())["ok"] is False


def test_nested_gitignore_does_not_fork_scope(tmp_path: Path) -> None:
    """五轮审查阻塞：git 还会读**逐层** `.gitignore`。

    只解析根 `.gitignore` 时：git 路径漏掉被嵌套规则排除的文件（含密钥、语法错误），
    而遍历路径保留它们 → 摘要分叉 + 密钥逃检。
    枚举取交集后，两路必须一致，且差异要被显式展示而不是静默。
    """
    if shutil.which("git") is None:
        pytest.skip("需要 git 可执行文件")

    _make_repo(tmp_path, {"src/app.py": "x = 1\n", "deploy/app.py": "y = 1\n"})
    (tmp_path / "deploy" / ".gitignore").write_text("conf_local.py\n", encoding="utf-8")
    (tmp_path / "deploy" / "conf_local.py").write_text(
        'K = "sk-' + "a" * 20 + '"\n', encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True
    )

    selected = {path.name for path in iter_scan_files(tmp_path)}
    report = run_preflight(tmp_path, run_tests=False)

    # 被嵌套 .gitignore 排除的文件不进摘要、也不被扫描（git 也不会提交它）
    assert "conf_local.py" not in selected
    assert report.secrets == []
    # 差异必须被显式展示，而不是静默取交集
    scope = next(c for c in report.checks if c.name == "scope_agreement")
    assert "仅遍历" in scope.detail and "conf_local.py" in scope.detail


def test_project_gitignore_entries_are_all_covered() -> None:
    """.gitignore 与 SKIP 规则必须同步，否则 git 与遍历两套范围会分叉。

    回归对象：`venv/` 曾在 `.gitignore` 里却不在 `SKIP_DIRS` 里，
    导致同一份内容在部署目录与工作副本算出**不同摘要**（批准绑定失效）。
    这条自检让该类分叉由门禁自己抓住，而不是靠人记得同步两份清单。
    """
    project_root = Path(__file__).resolve().parents[1]

    result = _check_skip_rules(project_root)

    assert result.status == "pass", result.detail


def test_git_files_returns_none_for_repo_subdirectory() -> None:
    """`git ls-files` 会向上找仓库；root 只是子目录时必须退化为遍历。

    否则：`pp check` 把 pytest 的 basetemp 放在
    `<root>/.patchpilot/pytest-tmp/run-<pid>-<hex>`（被 .gitignore 忽略）时，
    `git ls-files` 返回空列表 → 门禁静默地什么都没扫却报通过，
    而且所有依赖 tmp_path 的用例都会失败。
    """
    from patchpilot.core.preflight import _git_files

    project_root = Path(__file__).resolve().parents[1]
    if not (project_root / ".git").exists():
        pytest.skip("工作副本不是 git 仓库（CI 上是），该分支仅在仓库内可测")

    assert _git_files(project_root) is not None
    assert _git_files(project_root / "src") is None


def test_subdirectory_root_must_fail_not_skip(tmp_path: Path) -> None:
    """八轮 BLOCKING：`pp check --root <仓库子目录>` 不得静默丢失索引绑定。

    旧判定顺序先查 `root/.git` 是否存在 → 子目录里没有 `.git` → 直接 `skip`
    （detail 还写"非 git 仓库"），**永远走不到** `git_scope_status` 的 fail 分支。
    后果：拿到的批准只覆盖子树，而 commit 会提交整个仓库，索引/范围一致性一条没做。
    现在子目录 root 必须 **fail**（提示对仓库根运行），只有"真的不在仓库里"才 skip。
    """
    sub = tmp_path / "pkg"
    _make_repo(
        tmp_path,
        {
            "pkg/mod.py": "x = 1\n",
            "pkg/test_ok.py": "def test_a():\n    assert True\n",
            # 子树自带一份同样合格的 .gitignore，排除"因为 skip_rules 失败才没 ok"的干扰
            "pkg/.gitignore": IGNORE_ALL,
        },
    )
    _git_init(tmp_path)
    _git_add_all(tmp_path)

    # 仓库根：正常 pass
    assert _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path)).status == "pass"

    report = run_preflight(sub, run_tests=True)
    index_check = _check_by_name(report, "worktree_matches_index")
    scope_check = _check_by_name(report, "git_scope")

    assert index_check.status == "fail", index_check.detail
    assert "仓库子目录" in index_check.detail or "索引范围" in index_check.detail
    assert scope_check.status == "fail", "退化枚举必须阻塞，而不是只给一条 info"
    assert report.ok is False, "子树 root 下的批准不得显示为通过"


def test_non_git_directory_still_skips_index_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：**真的不在仓库里**时仍应 skip（不要把 skip 分支误删成一律 fail）。

    用 monkeypatch 把"在不在仓库内"固定为"不在"，因此本用例**与测试运行环境无关**
    （直接依赖当前目录时，它会在门禁自己跑 pytest 的场景下漂移：
    `tmp_path` 落在 PatchPilot 的 `.patchpilot/pytest-tmp` 里，于是看起来像外层仓库的子目录）。
    """
    import patchpilot.core.preflight as preflight

    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    monkeypatch.setattr(preflight, "find_toplevel", lambda _root: None)
    monkeypatch.setattr(
        preflight, "git_scope_status", lambda _root: (False, "不在任何 git 仓库内")
    )

    result = _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path))

    assert result.status == "skip"
    assert "非 git 仓库" in result.detail


def test_assertion_text_mentioning_access_denied_is_not_reclassified(tmp_path: Path) -> None:
    """环境判据换成写探针后，输出文本**不得**再影响分类。

    历史：`access is denied`（人类可读文案）→ 收窄到异常类型串 → 仍会误判
    `print("PermissionError: …")`。现在判据是"basetemp 能不能写"，与输出无关，
    所以本用例恒应成立，作为反例之一保留。
    """
    _make_repo(
        tmp_path,
        {
            "tests/test_msg.py": (
                "def test_msg():\n"
                "    assert False, 'access is denied'\n"
                "\n"
                "def test_msg2():\n"
                "    assert False, 'PermissionError: [WinError 5] access is denied'\n"
            )
        },
    )

    report = run_preflight(tmp_path, run_tests=True)
    check = _check_by_name(report, "pytest")

    assert check.status == "fail"
    assert not check.detail.startswith("环境问题"), check.detail[:160]


def test_is_writable_dir_true_for_writable_path(tmp_path: Path) -> None:
    """写探针正例：可写目录返回 True，且不留探针残留文件。

    探针**不负责创建目录**（第十轮复审：`mkdir(parents=True)` 会把"目录不存在"
    这种失败态自己修好再报成功），所以这里先用测试夹具把目录建出来。
    """
    from patchpilot.core.preflight import _is_writable_dir

    target = tmp_path / "existing"
    target.mkdir()

    assert _is_writable_dir(target) is True
    assert list(target.iterdir()) == [], "探针文件必须被清理掉"


def test_is_writable_dir_false_for_missing_dir(tmp_path: Path) -> None:
    """目录不存在 → False（而不是被探针自己 mkdir 出来再报 True）。"""
    from patchpilot.core.preflight import _is_writable_dir

    assert _is_writable_dir(tmp_path / "does" / "not" / "exist") is False


def test_is_writable_dir_false_when_write_fails(tmp_path: Path, monkeypatch) -> None:
    """写探针反例：写入抛 OSError 时返回 False（这是"环境问题"分类的唯一判据）。"""
    from patchpilot.core import preflight as preflight_module

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(Path, "write_text", boom)

    assert preflight_module._is_writable_dir(tmp_path) is False


def test_unwritable_basetemp_is_reported_as_environment_issue(tmp_path: Path, monkeypatch) -> None:
    """端到端反例：basetemp 不可写时，`pytest` 项必须报"环境问题"而不是假装用例失败。

    注意顺序：先建好仓库（`_make_repo` 也要写文件），**再**把 `Path.write_text` 打坏——
    否则夹具自己就先炸了。同时确保仓库里有 `test_*.py`，否则会先撞上
    "摘要范围内没有任何测试文件"那条 fail（另一条独立断言，不是本用例要测的）。
    """
    _make_repo(
        tmp_path,
        {
            "src/app.py": "x = 1\n",
            "tests/test_ok.py": "def test_ok():\n    assert True\n",
        },
    )

    def deny(self: Path, *args: object, **kwargs: object) -> int:
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(Path, "write_text", deny)

    report = run_preflight(tmp_path, run_tests=True)
    check = _check_by_name(report, "pytest")

    assert check.status == "fail"
    assert check.detail.startswith("环境问题"), check.detail[:160]


def test_report_carries_pytest_collect_errors(tmp_path: Path) -> None:
    """八轮非阻塞 1：收集期报错必须**进报告字段**，不能只活在 detail 文案里。

    此前 `collect_pytest_scope` 已返回四元组，但 `_check_pytest` 把它截断成三元组，
    于是"收集期报错"这条新证据永远到不了 `PreflightReport`——
    审查者/CI 只能读文案，无法机器消费。

    夹具用"导入不存在的模块"制造**真正的收集期错误**；
    注意模块级 `pytest.skip(allow_module_level=True)` **不会**产生 ERROR 行
    （它只是把该文件静默摘掉），所以那种写法测不到这条路径。
    """
    _make_repo(
        tmp_path,
        {
            "tests/test_broken_import.py": (
                "import this_module_does_not_exist_xyz\n"
                "\n"
                "def test_never():\n"
                "    assert True\n"
            )
        },
    )

    report = run_preflight(tmp_path, run_tests=True)
    check = _check_by_name(report, "pytest")

    assert check.status == "fail"
    assert "tests/test_broken_import.py" in report.pytest_missing_files
    # 关键：证据进了报告字段（而不是只拼接在 detail 里）
    assert report.pytest_collect_errors, "收集期报错必须出现在 pytest_collect_errors 字段"
    assert any("test_broken_import.py" in line for line in report.pytest_collect_errors)
    assert "收集期报错" in check.detail


# ============================================================================
# 七轮复审 3 项阻塞的回归用例（R6 两项修复此前零回归，是本轮阻塞能存活的直接原因）
# ============================================================================


def test_secret_scan_fails_closed_on_utf16_file(tmp_path: Path) -> None:
    """七轮阻塞 3：UTF-16 写入的密钥此前被解成 `a\\x00p\\x00i\\x00…` → `secret_scan: pass`。

    真正的根因不是正则，而是**解码前没有嗅探 NUL 字节**：UTF-16 的 ASCII 文本
    在 UTF-8 眼里是合法字节序列，`errors="replace"` 一个替换字符都不会产生。
    非 `.py` 文件是重灾区——`.py` 还能被 syntax 的 fail 兜住，配置文件没人管。

    八轮审查进一步要求：**不能只做 fail-closed，而要真的把密钥找出来**——
    现在会把"去 NUL 后按 UTF-8"等还原形式一并扫描，所以本用例断言 findings 非空。
    """
    _make_repo(tmp_path, {})
    (tmp_path / "notes.txt").write_bytes(
        # 本行的字面量形如真密钥，但它是被测输入（且被写成 UTF-16 以复现假阴性）
        'api_key = "sk-live-abcdefghijklmnopqrst".\n'.encode("utf-16-le")  # preflight:allow-secret
    )

    sniffed = sniff_text(tmp_path / "notes.txt")
    result = scan_secrets(tmp_path)
    report = run_preflight(tmp_path, run_tests=False)

    assert sniffed.kind == "binary", "UTF-16 不是 UTF-8 文本，必须走二进制/还原扫描分支"
    assert sniffed.variants, "必须提供可扫描的还原形式"
    assert any("notes.txt" in finding for finding in result.findings), result.findings
    assert _check_by_name(report, "secret_scan").status == "fail"
    assert report.ok is False


def test_secret_scan_reports_binary_files_instead_of_silence(tmp_path: Path) -> None:
    """七轮阻塞 3 的另一半：二进制文件不能凭空消失在扫描结果里。

    刻意**不阻塞**（否则放一张 PNG 就让门禁变红），但必须在报告里可见，
    且 `secret_scan` 的 detail 要说明"有文件没按文本扫"。
    """
    _make_repo(tmp_path, {})
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03PK\x03\x04binary")

    result = scan_secrets(tmp_path)
    report = run_preflight(tmp_path, run_tests=False)
    check = _check_by_name(report, "secret_scan")

    assert result.findings == []
    assert not result.undecodable, "二进制不该被记成'编码可疑'"
    assert result.skipped_binary
    assert "blob.bin" in result.skipped_binary[0]
    assert "二进制文件" in check.detail
    assert check.status == "pass", "二进制不阻塞，但必须可见"
    assert report.skipped_binary_files


def test_secret_scan_flags_nul_that_only_appears_late(tmp_path: Path) -> None:
    """NUL 判定必须覆盖**整个文件**，不能只看开头若干字节。

    只嗅开头窗口时，"前面全是纯文本、尾端才出现 NUL" 的文件会被当成普通文本
    继续扫描，于是 byte-offset 拼出来的密钥列会被静默漏掉。
    """
    _make_repo(tmp_path, {})
    payload = b"harmless = 1\n" * 900 + b"\x00secret-ish-binary-tail"
    assert payload.index(b"\x00") > 8 * 1024, "构造前提：NUL 必须落在嗅探窗口之外"
    (tmp_path / "tail.bin").write_bytes(payload)

    result = scan_secrets(tmp_path)

    assert result.skipped_binary, "尾端含 NUL 的文件必须被认成二进制并出现在报告里"
    assert "tail.bin" in result.skipped_binary[0]


def test_secret_scan_still_finds_key_in_plain_utf8(tmp_path: Path) -> None:
    """对照组：修编码探测不得把正常的 UTF-8 检出能力弄丢。"""
    _make_repo(tmp_path, {"conf.json": f'{{"api_key": "{FAKE_KEY}"}}\n'})

    result = scan_secrets(tmp_path)

    assert len(result.findings) == 1
    assert "conf.json:1" in result.findings[0]


def test_secret_scan_does_not_mistake_small_binary_for_utf16(tmp_path: Path) -> None:
    """短小的二进制不得被误判成"无 BOM 的 UTF-16 文本"而与密钥无关地 fail。

    自查实测：`b"\\x89PNG\\r\\n\\x1a\\n\\x00\\x00binary"`（17 字节）奇数位恰好全为 NUL，
    "每隔一字节是 NUL" 的宽松判据会把它归入不可解码文本。现在按"含 NUL 即二进制 +
    扫描还原形式"处理：不阻塞、但在报告里可见。
    """
    _make_repo(tmp_path, {})
    png_like = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRbinary\x00tail"
    (tmp_path / "tiny.png").write_bytes(png_like)

    result = scan_secrets(tmp_path)
    report = run_preflight(tmp_path, run_tests=False)

    assert result.skipped_binary, "短二进制应归入二进制而不是'不可解码文本'"
    assert not result.undecodable
    assert result.findings == []
    assert _check_by_name(report, "secret_scan").status == "pass"


def test_secret_scan_still_flags_long_bomless_utf16(tmp_path: Path) -> None:
    """无 BOM 的 UTF-16 文本（纯 ASCII 内容）里的密钥必须被检出。"""
    _make_repo(tmp_path, {})
    body = "".join(f"line{i:03d} = 'value'\n" for i in range(40))
    payload = (
        body + '\napi_key = "sk-live-abcdefghijklmnopqrst"\n'  # preflight:allow-secret
    ).encode("utf-16-le")
    (tmp_path / "wide.conf").write_bytes(payload)

    result = scan_secrets(tmp_path)
    report = run_preflight(tmp_path, run_tests=False)

    assert any("wide.conf" in finding for finding in result.findings), result.findings
    assert _check_by_name(report, "secret_scan").status == "fail"


def test_bomless_utf16_with_cjk_prefix_cannot_be_downgraded_to_binary(tmp_path: Path) -> None:
    """八轮审查 BLOCKING：不得用 CJK 前缀把 UTF-16 密钥文件"降级"成静默 pass。

    审查者的判决性实测：无 BOM 的 UTF-16LE **纯 ASCII** 文件高位恒 0、低位恒可打印，
    "可打印比例"判据 100% 通过；只要在开头塞 100 个 CJK 字符，非 NUL 的那一半就变成
    满量高位字节，比例塌到 ~0% → 判成二进制 → 当时二进制**不阻塞** →
    `secret_scan: pass`，而文件里确实躺着一条 UTF-16 编码的密钥。

    最终修法不再是"把分类判据调得更准"（任何基于 NUL 统计的分类都能被旁路：
    `啊` = U+554A，UTF-16LE 两个字节都非 0，5000 个 CJK 就能让整份文件 NUL 数为 0），
    而是**对还原形式一律扫描**：含 NUL 的文件会额外扫描"去 NUL 后按 UTF-8"与 UTF-16 解码，
    于是密钥必然被检出。本用例断言 findings 非空（而不是仅仅 fail-closed）。
    """
    _make_repo(tmp_path, {})
    secret = "sk-live-abcdefghijklmnopqrst"  # preflight:allow-secret
    payload = ("啊" * 100 + f'\napi_key = "{secret}"\n').encode("utf-16-le")
    (tmp_path / "app.cfg").write_bytes(payload)
    assert secret.encode("utf-16-le") in payload, "构造前提：密钥确实以 UTF-16 形式在文件里"

    sniffed = sniff_text(tmp_path / "app.cfg")
    result = scan_secrets(tmp_path)
    report = run_preflight(tmp_path, run_tests=False)

    assert sniffed.variants, "凡是含 NUL 的文件都必须提供可扫描的还原形式"
    assert any("app.cfg" in finding for finding in result.findings), result.findings
    assert _check_by_name(report, "secret_scan").status == "fail"
    assert report.ok is False


def test_utf32_with_cjk_prefix_is_also_detected(tmp_path: Path) -> None:
    """UTF-32LE（每 4 字节 3 个 NUL）同样必须被还原后扫出密钥。"""
    _make_repo(tmp_path, {})
    payload = (
        'key = "sk-live-abcdefghijklmnopqrst"\n'  # preflight:allow-secret
    ).encode("utf-32-le")
    (tmp_path / "deep.cfg").write_bytes(payload)

    result = scan_secrets(tmp_path)

    assert any("deep.cfg" in finding for finding in result.findings), result.findings


def test_worktree_matches_index_fails_when_index_has_deleted_file(tmp_path: Path) -> None:
    """反方向：索引里有、工作区已删除的文件会让它**照样进提交**，必须 fail。

    判决性实测（本用例复现的形态）：`git init` + 写文件 + `git add -A` + 删除工作区文件
    而**不暂存删除** → `git status` 显示 `AD file`、`git ls-tree HEAD` 里该文件仍然存在。
    即提交里带着一份从未被摘要与密钥扫描覆盖的字节（可能就是一条密钥）。
    补一次 `git add -A`（暂存删除）后它才真正从提交中消失。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n", "src/doomed.py": "y = 1\n"})
    _git_init(tmp_path)
    _git_add_all(tmp_path)
    assert _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path)).status == "pass"

    (tmp_path / "src" / "doomed.py").unlink()

    removed = _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path))

    assert removed.status == "fail"
    assert "doomed.py" in removed.detail

    _git_add_all(tmp_path)

    assert _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path)).status == "pass"


def test_parse_pytest_collection_accepts_both_output_shapes() -> None:
    """收集清单的解析必须同时认 pytest 新旧两种 `-q --collect-only` 输出。

    返回值是 ``(已收集文件, 收集期报错行)``：报错行单独带出来，
    使 fail 文案能区分"被 collect_ignore 抑制"与"导入失败/模块级 skip"（八轮非阻塞 2）。
    """
    new_style = "tests/test_a.py: 6\ntests/sub/test_b.py: 2\n\nno tests ran in 0.01s\n"
    old_style = "tests/test_a.py::test_x\ntests/sub/test_b.py::test_y\n"

    expected_files = ["tests/sub/test_b.py", "tests/test_a.py"]
    assert _parse_pytest_collection(new_style) == (expected_files, [])
    assert _parse_pytest_collection(old_style) == (expected_files, [])
    # 失败报告里的文件名不得被当成"已收集"
    assert _parse_pytest_collection("=========== FAILURES ===========\n") == ([], [])
    # 收集期报错单独成列，不混进"已收集"
    files, errors = _parse_pytest_collection("ERROR tests/test_x.py - ImportError: boom\n")
    assert files == []
    assert errors and "test_x.py" in errors[0]


def test_declared_pytest_scope_covers_every_test_file_of_this_project() -> None:
    """门禁声明要执行的测试文件，必须覆盖摘要集合里所有 `test_*.py`。

    这条用例防的是"范围证据本身失真"：若 `_pytest_targets` 漏掉了某个文件，
    门禁就会在少跑用例的情况下报 pass。
    """
    project_root = Path(__file__).resolve().parents[1]
    scanned = {path.name for path in iter_scan_files(project_root) if path.suffix == ".py"}
    expected = _pytest_targets(project_root, iter_scan_files(project_root))
    expected_names = {Path(target).name for target in expected}

    on_disk = {p.name for p in (project_root / "tests").glob("test_*.py")}
    assert on_disk, "测试目录为空说明用例枚举失效"
    assert on_disk.issubset(expected_names), on_disk - expected_names
    assert expected_names.issubset(scanned), expected_names - scanned


def test_pytest_scope_check_catches_collect_ignore_suppression(tmp_path: Path) -> None:
    """七轮阻塞 2 的判决性场景：`conftest.py` 里的 `collect_ignore` 关掉必失败用例。

    旧实现只跑 `pytest`，于是"测试被关掉"与"测试真通过"在报告里完全一样。
    新实现显式传入摘要集合中的 `test_*.py`，所以这条用例必须仍然 fail。
    """
    _make_repo(
        tmp_path,
        {
            "tests/test_always_fails.py": "def test_nope():\n    assert False\n",
            "tests/test_ok.py": "def test_ok():\n    assert True\n",
            "tests/conftest.py": 'collect_ignore = ["test_always_fails.py"]\n',
        },
    )

    report = run_preflight(tmp_path, run_tests=True)

    assert _check_by_name(report, "pytest").status == "fail"
    assert report.ok is False


def test_worktree_matches_index_requires_every_scanned_file_staged(tmp_path: Path) -> None:
    """七轮阻塞 1 前半：摘要覆盖枚举，所以**未跟踪**的文件也必须让该项 fail。

    旧实现只遍历索引条目 → 索引干净时报 pass，而摘要覆盖的字节
    与 git 将要提交的字节已经不同（未跟踪文件永远不会被提交）。
    """
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    _git_init(tmp_path)
    _git_add_all(tmp_path)

    candidates = iter_scan_files(tmp_path)
    assert _check_worktree_matches_index(tmp_path, candidates).status == "pass"

    (tmp_path / "src" / "untracked.py").write_text("y = 1\n", encoding="utf-8", newline="\n")

    after = _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path))

    assert after.status == "fail"
    assert "untracked.py" in after.detail

    _git_add_all(tmp_path)

    assert _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path)).status == "pass"


def test_worktree_matches_index_fails_on_empty_index(tmp_path: Path) -> None:
    """七轮阻塞 1 后半：索引为空时旧实现报 `skip`，摘要其实毫无绑定。"""
    _make_repo(tmp_path, {"src/app.py": "x = 1\n"})
    _git_init(tmp_path)

    result = _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path))

    assert result.status == "fail"
    assert "索引为空" in result.detail


def test_worktree_matches_index_detects_clean_filter_divergence(tmp_path: Path) -> None:
    """工作区字节 ≠ 索引字节时必须 fail（`.gitattributes` 的 eol 过滤）。

    git 自己认为两者"相同"（`git diff` 干净），所以只有 `hash-object --no-filters`
    取原始字节才能发现——否则 APPROVE 绑定在一份永远不会被提交的字节上。
    """
    _make_repo(tmp_path, {})
    (tmp_path / ".gitattributes").write_text(
        "* text=auto eol=lf\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    _git_init(tmp_path)
    _git_add_all(tmp_path)

    result = _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path))

    assert result.status == "fail"
    assert "app.py" in result.detail

    # 把工作区也改成 LF（即提交后的形态）后必须转 pass
    (tmp_path / "src" / "app.py").write_bytes(b"x = 1\ny = 2\n")

    assert _check_worktree_matches_index(tmp_path, iter_scan_files(tmp_path)).status == "pass"
