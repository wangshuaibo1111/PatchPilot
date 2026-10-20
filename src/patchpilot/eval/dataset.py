"""评测集：题目格式、加载与校验。

## 题目格式（JSONL，一行一题）

```json
{"id": "fast-001", "repo": "corpus/add_off_by_one", "issue_file": "Issue.md",
 "fail_to_pass": ["tests/test_calc.py::test_add"],
 "pass_to_pass": ["tests/test_calc.py::test_identity"],
 "image": "patchpilot-sandbox:0.1.0-py312", "tags": ["fast"]}
```

字段说明（对齐 SWE-bench 的口径，便于后续直接接 Lite 集）：
  - `fail_to_pass`：**修复前必须失败、修复后必须通过**的用例——这是"真的修好了"的判据；
  - `pass_to_pass`：修复前后都必须通过的用例——防"改对了但改坏了别的"；
  - `image`：镜像 tag 必须锁定（可复现纪律）。

## 为什么要有 `validate`

评测集本身是"度量衡"：如果题目写错（比如 `fail_to_pass` 在修复前就是绿的），
那么所有指标的解读都会失效。`validate_task` 在**加载期**就检查这类结构性错误，
而不是等到跑完才怀疑数字。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.core.textio import write_text_lf
from patchpilot.sandbox.policy import DEFAULT_IMAGE


class DatasetError(ValueError):
    """评测集格式或结构错误。"""


@dataclass(frozen=True, slots=True)
class EvalTask:
    """一道评测题。

    `repo` 与 `files` 二选一：
      - `repo`：指向一个真实仓库目录（SWE-bench 这类外部语料用它）；
      - `files`：**内联语料**（`相对路径 -> 内容`），运行时物化到工作目录。
        自建 FastEval 用这种——语料里含故意写坏的 `tests/test_*.py`，
        放进仓库会被门禁当成项目自己的测试收集。
    """

    id: str
    repo: Path | None
    issue_file: Path | None
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...] = ()
    image: str = DEFAULT_IMAGE
    tags: tuple[str, ...] = ()
    files: dict[str, str] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)

    def materialize(self, destination: Path) -> tuple[Path, Path]:
        """把题目内容写进 `destination`，返回 `(repo 根, issue 文件路径)`。

        内联语料就地展开；外部语料返回它的真实路径。
        """
        if self.files:
            destination.mkdir(parents=True, exist_ok=True)
            for rel, content in self.files.items():
                write_text_lf(destination / rel, content)
            issue_rel = self.raw.get("issue_file") or "Issue.md"
            return destination, destination / str(issue_rel)
        if self.repo is None or self.issue_file is None:
            raise DatasetError(f"{self.id}: 既没有 files 也没有 repo")
        return self.repo, self.issue_file

    def pytest_args(self) -> tuple[str, ...]:
        """本题要跑的测试（fail_to_pass + pass_to_pass，去重保序）。

        只跑相关用例而不是整个套件：既省时间，也让"是否修好"的判据更精确。
        """
        seen: list[str] = []
        for node in (*self.fail_to_pass, *self.pass_to_pass):
            if node not in seen:
                seen.append(node)
        return tuple(seen)


def validate_task(task: EvalTask) -> None:
    """加载期校验：题目结构不自洽就直接拒绝（避免用坏度量衡得出错数字）。"""
    if not task.id:
        raise DatasetError("题目缺少 id")
    if task.files:
        issue_rel = str(task.raw.get("issue_file") or "Issue.md")
        if issue_rel not in task.files:
            raise DatasetError(f"{task.id}: files 里缺少 issue 文件 {issue_rel}")
    else:
        if task.repo is None or not task.repo.is_dir():
            raise DatasetError(f"{task.id}: repo 目录不存在: {task.repo}")
        if task.issue_file is None or not task.issue_file.is_file():
            raise DatasetError(f"{task.id}: issue_file 不存在: {task.issue_file}")
    if not task.fail_to_pass:
        raise DatasetError(f"{task.id}: fail_to_pass 不能为空（否则无法判定'修好了'）")
    if ":" in task.image:
        tag = task.image.rsplit(":", 1)[1]
        if tag == "latest":
            raise DatasetError(f"{task.id}: 镜像不得用 latest（可复现纪律）")
    else:
        raise DatasetError(f"{task.id}: 镜像必须锁定 tag: {task.image}")


def load_tasks(path: Path) -> list[EvalTask]:
    """从 JSONL 加载评测集；每行一题，相对路径以**该文件所在目录**为基准。"""
    if not path.is_file():
        raise DatasetError(f"评测集文件不存在: {path}")
    base = path.parent
    tasks: list[EvalTask] = []
    raw_text = path.read_text(encoding="utf-8")
    default_image = DEFAULT_IMAGE
    if path.suffix == ".json":
        # 顶层容器：{"image": "...", "tasks": [...]}
        container = json.loads(raw_text)
        default_image = str(container.get("image") or DEFAULT_IMAGE)
        lines = [json.dumps(t, ensure_ascii=False) for t in container.get("tasks", [])]
    else:
        lines = raw_text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{lineno} JSON 解析失败: {exc}") from exc
        required = {"id", "fail_to_pass"}
        missing = required - set(payload)
        if missing:
            raise DatasetError(f"{path.name}:{lineno} 缺字段: {sorted(missing)}")
        if not payload.get("files"):
            for field_name in ("repo", "issue_file"):
                if field_name not in payload:
                    raise DatasetError(f"{path.name}:{lineno} 缺字段: ['{field_name}']")
        files = payload.get("files")
        if files:
            task = EvalTask(
                id=str(payload["id"]),
                repo=None,
                issue_file=None,
                fail_to_pass=tuple(payload["fail_to_pass"]),
                pass_to_pass=tuple(payload.get("pass_to_pass") or ()),
                image=str(payload.get("image") or default_image),
                tags=tuple(payload.get("tags") or ()),
                files={str(k): str(v) for k, v in files.items()},
                raw=payload,
            )
        else:
            task = EvalTask(
                id=str(payload["id"]),
                repo=(base / str(payload["repo"])).resolve(),
                issue_file=(base / str(payload["issue_file"])).resolve(),
                fail_to_pass=tuple(payload["fail_to_pass"]),
                pass_to_pass=tuple(payload.get("pass_to_pass") or ()),
                image=str(payload.get("image") or default_image),
                tags=tuple(payload.get("tags") or ()),
                raw=payload,
            )
        validate_task(task)
        tasks.append(task)
    if not tasks:
        raise DatasetError(f"评测集为空: {path}")
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise DatasetError(f"题目 id 重复: {sorted({i for i in ids if ids.count(i) > 1})}")
    return tasks


__all__ = ["DatasetError", "EvalTask", "load_tasks", "validate_task"]
