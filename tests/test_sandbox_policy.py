"""P1 沙箱安全策略的穷举测试（**无需 Docker**）。

来源：`.tmp/p1_staging/verify_policy.py`（离线 43/43）按 `MIGRATION.md` 改写为 pytest。
改写要点：
  - `check(name, cond)` → `assert cond`（用例名即断言意图）；
  - `expect_error(name, fn)` → `with pytest.raises(SandboxPolicyError)`；
  - 自建 `_ws` 目录 → `tmp_path` 夹具；
  - **Windows junction 分支保留**（`os.name == "nt"`），Linux 上用 symlink 覆盖同一逻辑。

为什么这些用例重要：把"安全策略怎么拼"写成纯函数、再把它生成的参数作为被测对象，
**安全性的核心部分就不依赖 Docker 也能测**——这是本设计最重要的一点。
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from pathlib import Path

import pytest

from patchpilot.sandbox.policy import (
    DEFAULT_IMAGE,
    NEVER_ALLOWED_TOKENS,
    REQUIRED_INVARIANTS,
    SandboxPolicy,
    SandboxPolicyError,
    assert_required_invariants,
    assert_safe,
    build_run_args,
    find_escaping_links,
    is_link_like,
    resolve_within,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return repo


@pytest.fixture()
def sandbox(tmp_path: Path) -> SandboxPolicy:
    """测试用策略：**显式声明**沙箱根为 pytest 的 `tmp_path`。

    第二十二轮新增的挂载源守卫要求 bind mount 的宿主源落在"显式声明的沙箱根"之下。
    生产默认是系统临时目录，但 pytest 的 basetemp 可能在别处（门禁把 basetemp 放在
    `.patchpilot/pytest-tmp` 之下），所以测试必须**声明自己的根**——
    这恰好也是该字段存在的意义：让"容器能挂哪些目录"变成可审计的显式配置，
    而不是靠一个全局默认值碰巧覆盖。
    """
    return SandboxPolicy(sandbox_root=tmp_path)


def _with(policy: SandboxPolicy, **overrides: object) -> SandboxPolicy:
    """在**保留 `sandbox_root`** 的前提下覆盖若干字段。

    为什么需要它（第二十二轮）：挂载源守卫要求 bind mount 的宿主源落在声明的沙箱根之下，
    而 pytest 的 `tmp_path` 未必在系统临时目录里（门禁把 basetemp 放在 `.patchpilot/` 下）。
    所以测试必须从 `sandbox` 夹具**派生**出各种策略，而不是重新 `SandboxPolicy(...)`
    ——否则一构造就把沙箱根丢回默认值，用例会因为"挂载源不在根下"而失败。
    """
    return dataclasses.replace(policy, **overrides)


def _args(policy: SandboxPolicy, workspace: Path, command: list[str] | None = None) -> list[str]:
    # 注意不能用 `command or [...]`：那会把**空命令**（本用例的输入）换成默认值，
    # 使"拒绝空命令"这条断言永远测不到真正的分支。
    return build_run_args(
        policy,
        container_name="pp-task-1",
        workspace=workspace,
        command=["python", "-m", "pytest", "-q"] if command is None else command,
    )


# ---------------------------------------------------------------- A. 必需不变量


def test_generated_args_carry_every_required_invariant(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    args = _args(sandbox, workspace)

    assert args[0] == "docker"
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--cpus") + 1] == "2.0"
    assert args[args.index("--memory") + 1] == "4g"
    assert args[args.index("--pids-limit") + 1] == "512"
    assert any("noexec" in a and "nosuid" in a for a in args)
    assert ":latest" not in " ".join(args), "镜像必须锁定 tag"
    # 不变量自检本身必须通过（生成与执行前都会调用）
    assert_required_invariants(args)


# ------------------------------- 不变量：非 root 必须按数值判定且不得被覆盖


@pytest.mark.parametrize("run_as", ["00:0", "0:1000", "root:root", "0", "0:0", "0000:1000"])
def test_root_like_run_as_is_rejected_at_construction(run_as: str) -> None:
    """真机证据：`--user 00:0` 在容器里 `id -u` 就是 0；字符串精确比对拦不住。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( run_as=run_as)


def test_build_run_args_checks_invariants_on_generation(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """唯一生成入口必须在**行为上**保证不变量成立，而不是靠"源码里出现过某字符串"。

    上一版断言的是 `inspect.getsource` 里含 `assert_required_invariants(args)`——
    在它之前插一句 `return args` 仍能让用例通过。现在改为行为断言：
    生成出的 argv 必须能通过配对校验；且人为破坏后必须抛错。
    """
    args = _args(sandbox, workspace)

    # 行为：生成期产物本身就是合规的
    assert_required_invariants(args)

    # 行为：把非 root 用户换掉后必须被拒（证明这条校验真的在跑）
    broken = [
        "0:0" if arg == "1000:1000" else arg
        for arg in args
    ]
    with pytest.raises(SandboxPolicyError):
        assert_required_invariants(broken)


def test_invariants_detect_duplicated_user_flag() -> None:
    """重复的 `--user` 会覆盖安全语义（后者生效），必须被发现而不是只看第一个。

    注意用**两次都是合法的非 root 值**（`1000:1000` / `1000:1000`）：
    上一版用例用 `1000:0`，实际是被 `_validate_run_as` 拦下的，
    重复检测那条分支根本没执行（第十一轮变异实验 M12 因此存活）。
    """
    with pytest.raises(SandboxPolicyError, match="重复传入"):
        assert_required_invariants(
            [
                "docker",
                "run",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "1000:1000",
                "--user",
                "1000:1000",
            ]
        )


def test_assert_safe_requires_run_subcommand() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_safe(["docker", "images"])


# -------------------------------------------------------- workdir / env 校验


@pytest.mark.parametrize(
    "workdir",
    [
        "/workspace,readonly",
        # `"workspace"` / `"relative/path"` 这些相对路径**实际由下面的子树规则拒绝**
        # （`startswith("/workspace/")` 为 False）。绝对路径规则因此是**冗余纵深防御**：
        # 第十四轮变异实测把它改成 `if False:` 后全套仍全绿——上一批注释里
        # "这条用例把绝对路径分支真正钉住"的说法**不成立**，已更正（详见 PLAN 的对应条目）。
        "workspace",
        "relative/path",
        "a",
        "./x",
        "/work space",
        "",
        # 下面这几条**只有逗号规则能拦**（子树前缀本身成立）——
        # 补第十一轮变异实验 M10：此前唯一带逗号的输入被子树规则拦下，
        # 删掉逗号检查套件仍全绿。真机上逗号会让 dockerd 把后面的串当 mount 选项解析
        # （实测 `dst=/workspace,ReadOnly=true` 使容器内写 /workspace 失败）。
        "/workspace/,readonly",
        "/workspace/x,y",
        "/tmp/,ro",
        "/tmp/x,y",
    ],
)
def test_workdir_is_validated(workdir: str) -> None:
    """真机证据：`workdir="/workspace,readonly"` 会被 Docker 解析成 `ReadOnly=true`——
    即用逗号往 `--mount` 选项串里注入。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( workdir=workdir)


@pytest.mark.parametrize("workdir", ["/", "/etc", "/etc/passwd", "/workspace/../etc", "/var"])
def test_workdir_must_stay_inside_allowed_subtrees(workdir: str) -> None:
    """子树规则必须有用例：`workdir="/"` 会把工作副本挂到根、遮蔽整个 rootfs。

    （这条用例是为补上第十轮复审的变异实验 M8 而加——当时把该规则改成 `if False:`
    套件仍然全绿，等于这条为安全而加的限制没有任何断言在守。）
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( workdir=workdir)


@pytest.mark.parametrize("workdir", ["/workspace", "/workspace/sub/dir", "/tmp", "/tmp/x"])
def test_workdir_accepts_allowed_subtrees(workdir: str) -> None:
    assert SandboxPolicy( workdir=workdir).workdir == workdir


def test_bare_extra_args_no_longer_exists() -> None:
    """结构性修复：**裸 argv 片段这一整个可变面已被删除**。

    四轮对抗性审查各自击穿了它的一个版本（拒绝清单 → 允许清单 → 布尔捆绑 → 悬空取值），
    并且"flag 紧跟 flag"的相邻态**原理上不可检**：某个 flag 的取值位置被前一个 token
    吃掉后，argv 层面再也分不清"用户想要 --init"与"用户在给 -e 传值"。
    因此改成类型化字段；`extra_args` 必须不存在——否则这一类缺陷会随新拼写重新出现。
    """
    with pytest.raises(TypeError):
        SandboxPolicy( extra_args=("-e",))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SandboxPolicy(**{"extra-args": ("-e",)})  # type: ignore[arg-type]


def test_typed_fields_drive_argv(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """所有可调项都由**类型化字段**决定，且不破坏镜像/命令位置。"""
    policy = _with(
        sandbox,
        env=(("A", "1"),),
        labels=(("k", "v"),),
        init=True,
        tty=True,
        interactive=True,
        hostname="pp-box",
        stop_timeout_s=30,
    )
    command = ["python", "-c", "print(1)"]

    args = _args(policy, workspace, command=command)

    for expected in ("--init", "--tty", "--interactive", "--hostname", "pp-box"):
        assert expected in args
    stop_at = args.index("--stop-timeout")
    assert args[stop_at + 1] == "30"
    assert "A=1" in args
    assert "k=v" in args
    # 镜像与命令位置不受可选字段影响
    image_index = len(args) - len(command) - 1
    assert args[image_index] == policy.image
    assert args[image_index + 1 :] == command


def test_boolean_fields_emit_no_value(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """布尔项只写 flag 本身：绝不产生 `--init=true` 这类"带取值"的写法。

    这正是旧实现里"取值被前一个 token 吃掉"的根源之一——布尔项一旦能带取值，
    就又有了解析歧义。现在类型系统直接保证它是 `bool`。
    """
    args = _args(_with(sandbox,  init=True), workspace)

    assert "--init" in args
    assert not any(token.startswith("--init=") for token in args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "a b"),
        ("hostname", ""),
        ("labels", (("k", "a b"),)),
        ("labels", (("", "v"),)),
        ("env", (("A", "a b"),)),
        ("stop_timeout_s", -1),
    ],
)
def test_typed_fields_are_validated(field: str, value: object) -> None:
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( **{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("run_as", ["１０００:１０００", "١٢٣٤", "1000:１０００"])
def test_run_as_rejects_non_ascii_digits(run_as: str) -> None:
    """`int()` 接受全角/阿拉伯-印度数字，docker 却不认（报 unable to find user）。

    虽然那是 fail-closed，但 doctype 声称"必须是数值 uid/gid"，所以限定 ASCII 数字。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( run_as=run_as)


@pytest.mark.parametrize("run_as", [":", "1:", ":1", "-1", "+1", "1e3"])
def test_run_as_rejects_ascii_non_digits(run_as: str) -> None:
    """ASCII 但非数字的取值（`:` / 负号 / 加号 / 科学计数）也必须拒绝。

    补第十二轮变异 M-M6-digit：删掉 `not piece.isdigit()` 后此前没有断言会红
    （因为非 ASCII 的非数字都被前一个 `isascii()` 子句拦下了）。
    注意不测 `" 1000"`：`_validate_run_as` 刻意先 `strip()` 再判定，那是**预期行为**
    （用户从配置里带进来的空白应当被容忍），不是缺口。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( run_as=run_as)


def test_image_position_is_verified_in_generated_argv(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """结构不变量：镜像必须正好在"命令之前那一位"，命令原样跟随其后。

    这是**纵深防御**：删掉裸 `extra_args` 后，这类错位已不可能由外部输入造成
    （旧实现里 `("-e",)` 会让镜像名被当成取值、命令丢掉首元素）。
    断言留着是为了防止将来新增可调字段时又打乱 argv 结构。
    """
    policy = _with(sandbox,  image="python:3.12-slim", env=(("FOO", "1"),), init=True)
    command = ["python", "-c", "print(1)"]

    args = _args(policy, workspace, command=command)

    image_index = len(args) - len(command) - 1
    assert args[image_index] == policy.image
    assert args[image_index + 1 :] == command


@pytest.mark.parametrize(
    "image",
    [
        # **真机逃逸**（第二十二轮）：`image="-vC:\…\secret:/mnt"` 会被 docker 的
        # 短选项粘连（`-v<value>`）解析成 `--volume`，真机 rc=0 且容器读到宿主文件。
        "-vC:\\hostsecret:/mnt",
        "-v/host:/mnt",
        "--privileged",
        "--volume=/host:/mnt",
        "-",
    ],
)
def test_image_may_not_look_like_a_flag(image: str) -> None:
    """镜像位是**唯一处于 flag 位置**的字段，绝不能以 `-` 开头。

    这一条是 `image` 侧的真机逃逸守卫（第二十二轮 BLOCKING）。
    变异自证：把 `if self.image.startswith("-"):` 置假后，**若没有本用例**全套全绿
    （我自查时正是这样发现的——参数化里原先是"尾空格/空白"这些与 `-` 无关的取值）。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( image=image)


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim ",  # 尾空格：docker 会以 invalid reference format 拒绝
        " python:3.12-slim",
        "python:3.12 slim",
        "python:3.12-slim\n",
        "python:3.12-slim\x00",
    ],
)
def test_image_rejects_whitespace_and_nul(image: str) -> None:
    """镜像名含空白/NUL 必须在**构造期**拒绝（此前到 docker 才 fail-closed）。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( image=image)


def test_image_position_guard_is_present_in_code() -> None:
    """登记：生成期镜像位置断言是**纵深防御**（§8.5 第 5 条）。

    构造期已拒绝"以 `-` 开头 / 含空白"的镜像，所以这条生成期断言在当前构造路径上
    **不可达**；保留它是为了防止将来新增字段时再次打乱 argv 结构。
    **不得**据此声称"已被用例覆盖"。
    """
    import inspect

    from patchpilot.sandbox import policy as policy_module

    source = inspect.getsource(policy_module.build_run_args)

    assert "argv 结构被打乱" in source
    assert "extra_args" not in policy_module.SandboxPolicy.__dataclass_fields__


def test_image_position_guard_actually_raises(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    sandbox: SandboxPolicy,
) -> None:
    """**行为**负例：人为把镜像位顶掉后，生成期断言必须真的抛错。

    补第十三轮变异 M-imguard-logic：此前只断言"源码里有那句话"，
    把守卫条件改成 `if False:` 套件仍全绿。这里通过让配对校验变成空操作，
    使"结构被破坏"的 argv 走到该守卫（守卫本身在正常构造路径下确实不可达，
    所以只能这样构造可达场景）。
    """
    from patchpilot.sandbox import policy as policy_module

    real_assert_safe = policy_module.assert_safe

    def corrupting_assert_safe(args: list[str]) -> None:
        real_assert_safe(args)
        # 模拟"某个字段多吃了一位"：把镜像名从原位挤走。
        # 用 DEFAULT_IMAGE 而非字面量：否则换镜像时这里会以 ValueError 暴露，
        # 而不是给出"结构被打乱"的清晰断言失败。
        index = args.index(DEFAULT_IMAGE)
        args.insert(index, "--label")
        del args[index + 2]

    monkeypatch.setattr(policy_module, "assert_safe", corrupting_assert_safe)

    with pytest.raises(SandboxPolicyError, match="argv 结构被打乱"):
        policy_module.build_run_args(
            sandbox, container_name="pp-x", workspace=workspace, command=["true"]
        )


@pytest.mark.parametrize(
    "memory", ["0", "0b", "0k", "0m", "0g", "", "abc", "1m", "4", 1, True, None]
)
def test_memory_is_validated(memory: object) -> None:
    """第十三轮 BLOCKING：`memory="0"` 会让内存上限**静默取消**。

    真机证据：`--memory 0 --memory-swap 0` 时 `docker run` rc=0、无任何告警，
    `docker inspect` 得 `HostConfig.Memory=0 MemorySwap=0`（对照默认 `4g` = 4294967296）。
    同模块对 `cpus=0`/`pids_limit=0`/`timeout_s=0` 都明确拒绝，唯独内存可无声抹掉。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( memory=memory)


@pytest.mark.parametrize("memory", ["4g", "512m", "104857600", "2G", "64M"])
def test_memory_accepts_legal_sizes(
    memory: str,
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    policy = _with(sandbox,  memory=memory)

    args = _args(policy, workspace)

    assert args[args.index("--memory") + 1] == memory
    assert args[args.index("--memory-swap") + 1] == memory, "swap 必须跟 memory 同值（禁 swap）"


def test_invariants_reject_zero_memory_in_argv() -> None:
    """即使 argv 里的零值不是由策略字段产生，也必须被不变量校验拦下。

    **必须覆盖零值的等价写法**：第一版只认字面量 `{"0","0b","0k","0m","0g"}`，
    而真机对 `0G`/`00`/`0000`/`0B` 同样报 rc=0 且 `HostConfig.Memory=0`
    （第十四轮 BLOCKING：同一类缺陷在**同一层**复发）。
    现在改为"能解析出正值才算合法"，这个参数化表就是它的守门用例。
    """
    zero_equivalents = [
        "0", "0b", "0B", "0k", "0K", "0m", "0M", "0g", "0G", "00", "0000", "0x10", "",
    ]
    for value in zero_equivalents:
        with pytest.raises(SandboxPolicyError, match=r"不得为 0（等于取消该资源上限）: .*$"):
            assert_required_invariants(
                [
                    "docker",
                    "run",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--user",
                    "1000:1000",
                    "--memory",
                    value,
                    "--memory-swap",
                    value,
                    "--pids-limit",
                    "512",
                ]
            )


# ============================================================================
# 唯一硬编码源（single source of truth）
# ============================================================================
#
# **唯一的一处"含取值"硬编码清单**，其余表一律从它派生。
# （另有第二处硬编码表 `_VALIDATOR_NULL_FLAGS`（校验器不查等值的 flag 集合）
#   与第三处硬编码锚点 `REQUIRED_FLAG_LITERALS_FOR_EMISSION`（不派生的 flag 字面元组）——
#   它们分别是"语义标注"与"独立锚点"，不是同一事实的副本，且三者之间都有双向断言守着。）
#
# 为什么必须收成一份（四轮同类缺陷的最终结论）：
#   16 轮：参数化用 `[flag for flag, _ in REQUIRED_INVARIANTS]` → 用例与被测对象同源，
#          删清单项时用例**消失**而非变红；
#   17 轮：字面表**只存 flag 名**、丢掉期望取值 → 三条取值降级变异全存活；
#   18 轮：`PINNED_INVARIANT_FLAGS` 从 `EXPECTED_INVARIANTS` 派生 → **两处同删**即全绿；
#   19 轮：再加一份硬编码 flag 表仍是**第三份可写副本** → **三处同删**即全绿
#          （审查者实测 `--network`/`--cap-drop`/`--security-opt` rc=0、0 failed）。
# 每一轮的"修法"都只是把通道变窄。**根本原因**：只要同一事实存在两份可写副本，
# 就能把两份一起改坏。因此这里只保留**一份**硬编码清单，其余全部派生——
# "同删"不再可能，因为已经没有第二份可删。
#
# 它同时承担三种角色：
#   1. **发射事实**：默认策略拼出的 argv 必须含这 8 对字面量（见
#      `test_default_policy_actually_emits_every_invariant`）——这是唯一不依赖
#      任何"清单纯否"的探测器；
#   2. **期望取值的来源**：`EXPECTED_INVARIANTS` 与 `PINNED_INVARIANT_FLAGS` 由它派生；
#      **注意：逐项存在性用例的参数不是它**——那用的是下面**谁也不派生**的
#      `REQUIRED_FLAG_LITERALS_FOR_EMISSION`（第二十轮结论：参数表若派生自任何可被改小的表，
#      删条目时用例会"消失"而不是"变红"）。
#
# `None` 的语义：该 flag 在**校验器**里不做等值校验（只要求存在），
# 而这里给出的取值是**构建器实际发射**的取值（两者是不同的事实，见
# `test_emitted_values_satisfy_the_pinned_expectations` 的说明）。
EMITTED_INVARIANTS: tuple[tuple[str, str | None], ...] = (
    ("--network", "none"),
    ("--read-only", None),
    ("--cap-drop", "ALL"),
    ("--security-opt", "no-new-privileges"),
    ("--user", "1000:1000"),
    ("--memory", "4g"),
    ("--memory-swap", "4g"),
    ("--pids-limit", "512"),
)

# 以下全部**派生**，不得手抄（手抄就是又造了一份可写副本）。
PINNED_INVARIANT_FLAGS: tuple[str, ...] = tuple(flag for flag, _ in EMITTED_INVARIANTS)

# 校验器的期望取值：`--user`/`--memory`/`--memory-swap`/`--pids-limit` 在
# `REQUIRED_INVARIANTS` 里是 `None`（只校验存在与非 root/正值），故这里同样置 None。
_VALIDATOR_NULL_FLAGS = frozenset(
    {"--read-only", "--user", "--memory", "--memory-swap", "--pids-limit"}
)
EXPECTED_INVARIANTS: tuple[tuple[str, str | None], ...] = tuple(
    (flag, None if flag in _VALIDATOR_NULL_FLAGS else value)
    for flag, value in EMITTED_INVARIANTS
)


def test_required_invariants_manifest_is_exactly_as_pinned() -> None:
    """实现对清单的**任何增删改（含取值）**都必须同步修改测试里的唯一硬编码源。

    这是清单↔实现的"桥"；`EXPECTED_INVARIANTS` 与 `PINNED_INVARIANT_FLAGS`
    都由 `EMITTED_INVARIANTS` 派生，故不再需要"两份测试侧表互比"那种恒真式断言。
    """
    assert REQUIRED_INVARIANTS == EXPECTED_INVARIANTS
    assert tuple(flag for flag, _ in REQUIRED_INVARIANTS) == PINNED_INVARIANT_FLAGS


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(flag, value) for flag, value in EXPECTED_INVARIANTS if value is not None],
)
def test_pinned_non_null_invariants_really_pin_their_value(flag: str, expected: str) -> None:
    """反向用例：非 None 的项，其取值必须**真的**被校验。

    做法：把该 flag 的取值换成一个合法但不匹配的字符串，必须抛错——
    这样"取值被静默降级成 None"就再也躲不过（第十七轮 BLOCKING）。
    """
    args = _minimal_argv()
    args[args.index(flag) + 1] = "wrong-value"

    pattern = rf"{re.escape(flag)} 必须为 {re.escape(expected)}"
    with pytest.raises(SandboxPolicyError, match=pattern):
        assert_required_invariants(args)


# 派生，不手抄：校验器不要求等值（None）的那些项。
NULL_VALUED_INVARIANT_FLAGS: tuple[str, ...] = tuple(
    flag for flag, _ in EXPECTED_INVARIANTS if flag in _VALIDATOR_NULL_FLAGS
)


def test_validator_null_flag_set_is_closed() -> None:
    """`_VALIDATOR_NULL_FLAGS` 与派生结果必须**集合相等**（防"缩小它"静默少覆盖）。

    第二十一轮非阻塞项：`_VALIDATOR_NULL_FLAGS` 是第二张硬编码表，从它去掉
    `--read-only` 后，`test_pinned_null_invariants_accept_other_legal_values[--read-only]`
    只是**消失**（rc=0、0 failed），没有任何断言变红。
    """
    assert set(NULL_VALUED_INVARIANT_FLAGS) == {
        flag for flag, value in EXPECTED_INVARIANTS if value is None
    }
    assert set(_VALIDATOR_NULL_FLAGS) <= {flag for flag, _ in EMITTED_INVARIANTS}


@pytest.mark.parametrize("flag", NULL_VALUED_INVARIANT_FLAGS)
def test_pinned_null_invariants_accept_other_legal_values(flag: str) -> None:
    """字面表里标 None 的项：换成**另一个合法取值**也必须通过（只受其它规则约束）。

    第十九轮修正：上一版写成 `assert_required_invariants(args), "msg"`——断言的是 `None`，
    **恒真**且 msg 永不求值，只证明了 `_minimal_argv()` 不抛，没证明"接受任意合法取值"。
    现在真的把取值替换掉再断言。
    """
    alternatives = {
        "--read-only": None,  # 无取值项不变
        "--user": "12345:67890",
        "--memory": "8g",
        "--memory-swap": "8g",
        "--pids-limit": "64",
    }
    args = _minimal_argv()
    replacement = alternatives[flag]
    if replacement is not None:
        args[args.index(flag) + 1] = replacement
        assert_required_invariants(args)  # 不抛即通过
        assert args[args.index(flag) + 1] == replacement
    else:
        assert args[args.index(flag) + 1] != args[args.index(flag)]


def _minimal_argv() -> list[str]:
    """一条**完整**的安全 argv——按**谁也不派生**的 `REQUIRED_FLAG_LITERALS_FOR_EMISSION` 构建。

    §8.5 第 3 条：参数表不得与被测对象（或与实现比对用的那张表）同源，
    否则"两边同时删项"会让用例消失而非变红。这里的取值表是字面量，
    flag 列表来自硬编码锚点。
    """
    values = {
        "--network": "none",
        "--read-only": None,
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--user": "1000:1000",
        "--memory": "4g",
        "--memory-swap": "4g",
        "--pids-limit": "512",
    }
    args = ["docker", "run"]
    for flag in REQUIRED_FLAG_LITERALS_FOR_EMISSION:
        args.append(flag)
        value = values[flag]
        if value is not None:
            args.append(value)
    return args


def test_minimal_argv_is_complete() -> None:
    """先证明 `_minimal_argv()` 本身是完整的——否则下面的"摘掉某项"用例会被别的缺失项代偿。"""
    assert_required_invariants(_minimal_argv())


def _as_subsequence_pairs(args: list[str]) -> list[tuple[str, str]]:
    """把 argv 里所有 `(flag, 取值)` 相邻对抽出来，供子序列比对。"""
    return [
        (args[index], args[index + 1])
        for index in range(len(args) - 1)
        if args[index].startswith("--")
    ]


def test_default_policy_actually_emits_every_invariant(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """**发射侧**证据：默认策略拼出的 argv 必须真的含 `EMITTED_INVARIANTS` 里的每一对。

    这是不依赖"清单纯否"的探测器：清单、期望表被同删时仍然有效
    （它只读策略真正发射出来的 argv）。
    """
    args = _args(sandbox, workspace)
    pairs = _as_subsequence_pairs(args)

    for flag, value in EMITTED_INVARIANTS:
        if value is None:
            assert any(pair[0] == flag for pair in pairs), f"argv 里缺少 {flag}"
        else:
            assert (flag, value) in pairs, (
                f"argv 里缺少 {flag} {value}（实际相邻对: "
                f"{[p for p in pairs if p[0] == flag]}）"
            )


# **最终锚点**：默认策略 argv 里必须出现的必需 flag，逐个**硬编码**。
#
# 为什么还需要这一条（第二十轮自查实测）：上面的发射用例**迭代 `EMITTED_INVARIANTS`**，
# 于是"同时删掉实现清单 + 测试唯一源里的同一个条目"时，**检查与被检查的对象一起消失**
# ——实测 rc=0、316 passed（与十九轮审查者实测的 B3 同形，我的修法当时并没有封闭它）。
# 参数化列表本质上是"检查项清单"，它必须是**独立、完整、硬编码**的，
# 不能从任何可能被改小的表派生。因此这里逐字写死 8 个 flag：删清单条目、
# 改 `build_run_args` 的发射、或把取值降级，都会被它抓住。
REQUIRED_FLAG_LITERALS_FOR_EMISSION: tuple[str, ...] = (
    "--network",
    "--read-only",
    "--cap-drop",
    "--security-opt",
    "--user",
    "--memory",
    "--memory-swap",
    "--pids-limit",
)


def test_default_argv_contains_every_hardcoded_required_flag(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """**不派生自任何表**的发射断言：默认 argv 必须含 8 个硬编码 flag。

    这一条与上面的发射用例有本质区别：上面的参数是 `EMITTED_INVARIANTS`（表），
    本条的参数是**写死的字面元组**。因此"把表改小"无法让它少查——
    这正是"参数表与被测对象同源"（§8.5 第 3 条）的最终堵法。
    """
    args = _args(sandbox, workspace)

    missing = [flag for flag in REQUIRED_FLAG_LITERALS_FOR_EMISSION if flag not in args]
    assert missing == [], f"默认 argv 缺少必需 flag: {missing}"


def test_hardcoded_emission_literals_cover_the_manifest() -> None:
    """硬编码锚点必须覆盖实现清单（防"锚点比清单少"而漏查）。"""
    assert set(REQUIRED_FLAG_LITERALS_FOR_EMISSION) == {
        flag for flag, _ in REQUIRED_INVARIANTS
    }
    assert len(REQUIRED_INVARIANTS) == 8, "必需不变量的数量变化必须同步这里与硬编码锚点"


def test_hardcoded_anchor_matches_the_derived_tables() -> None:
    """硬编码锚点与派生表逐项相等。

    注意断言方向：`REQUIRED_FLAG_LITERALS_FOR_EMISSION` 是**谁也不派生**的字面元组，
    这里拿它去比派生表（而不是反过来），所以"把派生表改小"会让本条变红，
    而"把锚点改小"会让上一条的清单覆盖断言变红——两个方向都有出口。
    """
    assert REQUIRED_FLAG_LITERALS_FOR_EMISSION == PINNED_INVARIANT_FLAGS


def test_host_exposure_surface_is_pinned_by_literals(
    workspace: Path, sandbox: SandboxPolicy
) -> None:
    """**宿主暴露面**必须被字面金标钉住（第二十一轮 BLOCKING，严重度最高）。

    为什么单列一条：`--mount type=bind,src=…,dst=…` 是"容器能看到宿主什么"的**唯一决定点**。
    实测（审查者 + 我复核）以下**单行**变异此前**全部全绿**：
      - m1 删掉整行 `--mount`；
      - **m2 把 `src={workspace}` 改成 `src=/`** ← 把宿主根以 uid 1000 可读写挂进容器；
      - w1 删 `--workdir`；n1 删 `--name`；r1 删 `--rm`。
    `assert_safe` 与 `assert_required_invariants` 都**不看**这个串，所以一次单行改动就能
    把"只挂副本"变成"挂宿主根"而 322 条用例全绿——这是本模块最该被钉住、却一直没人守的地方。

    这里用**字面金标**：mount 串必须与期望逐字符相等，且这四个 flag 各恰好出现一次。
    """
    policy = sandbox
    resolved = workspace.resolve()
    args = _args(policy, workspace)

    expected_mount = f"type=bind,src={resolved},dst={policy.workdir}"
    assert ("--mount", expected_mount) in _as_subsequence_pairs(args), (
        "宿主暴露面被改动：--mount 必须恰好是 "
        f"{expected_mount!r}，实际 {[p for p in _as_subsequence_pairs(args) if p[0] == '--mount']}"
    )

    for flag in ("--mount", "--workdir", "--rm", "--name"):
        assert args.count(flag) == 1, f"{flag} 必须恰好出现 1 次，实际 {args.count(flag)} 次"

    # mount 的目标必须与 --workdir 一致，否则副本挂到别处、workdir 指向空目录
    assert args[args.index("--workdir") + 1] == policy.workdir


@pytest.mark.parametrize(
    "hostile",
    ["C:\\", "/", "..", ".", "src", "D:\\Code\\PatchPilot", str(Path.home()), "C:\\Windows"],
)
def test_hostile_mount_sources_are_rejected(hostile: str) -> None:
    """**敌意挂载源**必须被 `src/` 拒绝——这是宿主暴露面的真正守卫。

    第二十二轮 BLOCKING：此前 `src/` 里**没有任何代码**校验 `workspace`，
    真机实测 `workspace="C:\\"` 把整个系统盘可读写挂进 `/workspace`、
    `workspace=<宿主目录>` 让容器读到达宿主机密文件、`workspace=".."` 被 docker
    按自己的 cwd 静默解析。而"防线"只存在于测试文件里——停用两条用例后
    `src={workspace}`→`src=/` 的变异全绿。

    本用例直接喂敌意输入（而不是对夹具自己断言），所以它**不可能**被"夹具改了"骗过。
    """
    with pytest.raises(SandboxPolicyError):
        build_run_args(SandboxPolicy(), container_name="pp-x", workspace=hostile, command=["sh"])


def test_mount_source_rejects_filesystem_root_before_containment() -> None:
    """文件系统根必须被**专门的规则**拒绝，而不是靠"不在沙箱根之下"顺带拦住。

    变异自证（第二十二轮自查）：把 `if resolved.parent == resolved:` 置假后全套仍绿——
    因为 `/`、`C:\\` 本来就落在沙箱根之外，被包含判定兜住了。
    也就是说该规则是**行为等价的纵深防御**（§8.5 第 5 条要求登记）。
    这里断言的是**契约**：即使 `sandbox_root` 被显式设成文件系统根（一种错误配置），
    挂载源也不得是根本身——此时包含判定会放行，只有这条规则拦得住。
    """
    root = Path(Path.cwd().anchor)  # "/" 或 "C:\\"

    with pytest.raises(SandboxPolicyError, match=r"不得是文件系统根"):
        build_run_args(
            SandboxPolicy(sandbox_root=root),
            container_name="pp-x",
            workspace=root,
            command=["sh"],
        )


def test_mount_source_must_be_an_existing_directory(tmp_path: Path) -> None:
    """挂载源必须是**已存在的目录**：不存在 → 拒绝；是普通文件 → 拒绝。

    变异自证（第二十二轮自查）：把 `if not resolved.is_dir():` 置假后全套仍绿——
    因为"文件"本来就落在沙箱根之外时会被包含判定拦住；而当文件**就在**沙箱根之内时，
    只有这条规则拦得住。这里用显式 `sandbox_root=tmp_path` 构造那个场景。
    """
    a_file = tmp_path / "host-secret.txt"
    a_file.write_text("secret", encoding="utf-8")
    policy = SandboxPolicy(sandbox_root=tmp_path)

    with pytest.raises(SandboxPolicyError, match=r"必须是目录"):
        build_run_args(policy, container_name="pp-x", workspace=a_file, command=["sh"])

    with pytest.raises(SandboxPolicyError, match=r"不存在"):
        build_run_args(
            policy, container_name="pp-x", workspace=tmp_path / "nope", command=["sh"]
        )


def test_legit_copy_dir_is_accepted_as_mount_source(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """反向：落在沙箱根（默认系统临时目录）之下的合法副本目录必须通过。"""
    args = _args(sandbox, workspace)
    mount = next(value for flag, value in _as_subsequence_pairs(args) if flag == "--mount")

    src = next(part for part in mount.split(",") if part.startswith("src=")).removeprefix("src=")

    assert Path(src) == workspace.resolve()
    assert Path(src).is_absolute()


def test_emitted_values_satisfy_the_pinned_expectations() -> None:
    """两侧自洽：**期望取值非 None** 的那些项，发射的取值必须与之相符。

    这里必须区分两个不同的事实（第一版写错了，特此记下）：
      - `EXPECTED_INVARIANTS` 是**校验器的约束**：`None` 表示"只要求存在、不做等值校验"；
      - `EMITTED_INVARIANTS` 是**构建器真正发射的取值**（`--user` 是 `1000:1000` 等）。
    对 `None` 项要求"发射值也必须是 None"是错的——`None` 只约束校验器。
    因此只比对非 None 项；`None` 项的发射值由
    `assert_required_invariants`（非 root / 正值校验）与
    `test_default_policy_actually_emits_every_invariant`（必须真的发射）共同守住。
    """
    emitted = {flag: value for flag, value in EMITTED_INVARIANTS}
    for flag, value in EXPECTED_INVARIANTS:
        if value is None:
            continue
        assert emitted[flag] == value, (
            f"{flag}: 发射事实 {emitted[flag]!r} 与期望取值 {value!r} 不一致"
        )


@pytest.mark.parametrize("flag", REQUIRED_FLAG_LITERALS_FOR_EMISSION)
def test_each_required_invariant_is_enforced(flag: str) -> None:
    """**逐项**验证存在性不变量：从完整 argv 里只摘掉这一项，必须精确报出这一项缺失。

    参数化用的是**硬编码**的 `PINNED_INVARIANT_FLAGS`，不是从 `REQUIRED_INVARIANTS`
    反推——后者会让"删掉清单条目"这一变异表现为"用例消失"而非"用例变红"（第十六轮 BLOCKING）。
    文案用 `$` 锚定：未锚定时 `"…: --memory-swap"` 会前缀命中 `--memory`（第十五轮 BLOCKING）。
    """
    args = _minimal_argv()
    index = args.index(flag)
    del args[index : index + 1]  # 只删 flag 本身
    if index < len(args) and not args[index].startswith("--") and flag != "--read-only":
        del args[index]  # 连带删掉它的取值（--read-only 无取值）

    with pytest.raises(SandboxPolicyError, match=rf"缺少必需的安全参数: {flag}$"):
        assert_required_invariants(args)


def test_invariants_accept_legal_memory_sizes() -> None:
    for value in ("4g", "512m", "6m", "104857600"):
        args = _minimal_argv()
        args[args.index("--memory") + 1] = value
        args[args.index("--memory-swap") + 1] = value
        assert_required_invariants(args)


@pytest.mark.parametrize("value", ["0", "0b", "00", "0g", "-1", "-0", " 0", "0 ", "+1", ""])
def test_pids_limit_must_be_a_positive_integer(value: str) -> None:
    """`--pids-limit 0/-1` 会让进程数熔断静默失效——与内存零值同一条防线。

    第十六轮 BLOCKING：该分支此前**零覆盖**（零值等价写法循环把 `--pids-limit`
    恒写成 `512`，零/负值从未进入 argv 级校验），把 `<= 0` 改成 `== 0` 甚至 `if False:`
    套件都全绿。
    """
    args = _minimal_argv()
    args[args.index("--pids-limit") + 1] = value

    with pytest.raises(SandboxPolicyError, match=r"^--pids-limit 必须为正整数: .*$"):
        assert_required_invariants(args)


def test_pids_limit_accepts_positive_integer() -> None:
    args = _minimal_argv()
    args[args.index("--pids-limit") + 1] = "512"

    assert_required_invariants(args)


@pytest.mark.parametrize("value", ["6m", "7m", "104857600", "1g"])
def test_memory_minimum_boundary_is_enforced(value: str) -> None:
    """`MIN_MEMORY_BYTES` 边界要**双向**钉住。

    第十七轮 BLOCKING 的教训：上一版这组"放行"用例只喂 `assert_required_invariants`，
    **而那个函数根本不读 `MIN_MEMORY_BYTES`**（只判 `> 0`），于是把常量从 6 MiB 抬到 7 MiB
    全套仍全绿。现在这组用例改走 `SandboxPolicy(...)` 构造期——真正读该常量的路径。
    """
    policy = SandboxPolicy( memory=value)

    assert policy.memory == value


@pytest.mark.parametrize("value", ["6291456", "6291456b", "6m"])
def test_memory_exactly_at_minimum_is_accepted(value: str) -> None:
    """**恰好在** 6 MiB 下界上必须放行——这一向专门钉住"下限被上抬"（6→7 MiB）。

    下界本身（< 6 MiB 拒绝）由 `test_memory_below_minimum_is_rejected` 钉住；
    两条合起来才是真正的"双向"。
    """
    assert SandboxPolicy( memory=value).memory == value


@pytest.mark.parametrize("value", ["6291455", "6291455b", "5m", "1m", "6m0"])
def test_memory_below_minimum_is_rejected(value: str) -> None:
    """低于 6 MiB 的取值必须被拒（否则删掉下限检查无人能红）。"""
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( memory=value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpus": True},
        {"timeout_s": False},
        {"image": 1},
        {"workdir": None},
        {"run_as": 1},
        {"tmpfs_size": 1},
        {"hostname": 123},
        # 注意 `hostname=None` **不列入**：None 是"不设置 hostname"的合法取值
        {"env": (("A", None),)},
        {"labels": (("k", None),)},
        {"tmpfs_size": "512m\x00"},
        {"memory": "4G "},
        {"memory": "\t4g"},
        # 非二元组容器：此前是**裸 ValueError**（`not enough values to unpack`）
        {"env": {"A": "1"}},
        {"env": ["A=1"]},
        {"env": "A=1"},
        {"labels": {"k": "v"}},
        {"labels": ["k=v"]},
        {"env": (("A", "1", "extra"),)},
        # 非序列容器：此前抛裸 `TypeError: cannot unpack non-sequence`
        {"env": 42},
        {"env": set()},
        {"labels": 42},
        {"labels": set()},
        {"env": (None,)},
        {"labels": (None,)},
    ],
)
def test_bad_types_raise_policy_error_not_bare_exceptions(kwargs: dict) -> None:
    """所有类型错误都必须收敛成 `SandboxPolicyError`（第十三/十四轮的存活变异）。

    变异证据：删掉 `_require_number` 的 bool 守卫、删掉 `_validate_hostname` 的
    `isinstance`、把 `workdir`/`run_as`/`image` 的类型检查去掉，套件此前都不会红——
    而解释器会抛裸 `AttributeError`/`TypeError`/`ValueError`，与调用方只捕获
    `SandboxPolicyError` 的口径冲突。
    传 dict/list/str 给 `env`/`labels` 时，解包会抛裸 `ValueError`，故也列入。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( **kwargs)


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"command": "python -V"}, "字符串命令会按字符展开成 argv"),
        ({"command": [None]}, "命令元素必须是字符串"),
        ({"command": []}, "空命令"),
        ({"container_name": 1}, "容器名必须是字符串"),
        ({"workspace": None}, "workspace 必须是路径"),
        ({"workspace": 123}, "workspace 必须是路径"),
    ],
)
def test_build_run_args_type_errors_are_policy_errors(
    workspace: Path, overrides: dict, why: str
) -> None:
    """生成期的类型错误同样要收敛（第十四轮非阻塞项）。"""
    call = {"workspace": workspace, "container_name": "pp-x", "command": ["true"]}
    call.update(overrides)

    with pytest.raises(SandboxPolicyError):
        build_run_args(sandbox, **call)


def test_env_names_are_validated() -> None:
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( env=(("A=B", "1"),))


# ------------------------------------------------- 最后一道防线：assert_safe


def test_assert_safe_blocks_privileged() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_safe(["docker", "run", "--privileged", "img"])


def test_assert_safe_requires_docker_prefix() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_safe(["bash", "-c", "rm -rf /"])


def test_assert_safe_blocks_pid_host() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_safe(["docker", "run", "--pid=host", "img"])


def test_never_allowed_lists_cover_the_dangerous_surface() -> None:
    """`NEVER_ALLOWED_TOKENS` 被 `assert_safe` 真正读取，因此可以断言其内容；
    `NEVER_ALLOWED_OVERRIDES` 是**装饰性常量**（`assert_safe` 不读它），
    故对它的约束改由 `test_assert_safe_blocks_dangerous_flags_behaviourally` 的行为断言承担。
    """
    assert {"--privileged", "--cap-add", "--device", "--volume"} <= set(NEVER_ALLOWED_TOKENS)
    assert "-v" in NEVER_ALLOWED_TOKENS, "`-v` 必须在拒绝清单里（短选项挂载）"

    # 行为断言：`-v` 被 `assert_safe` 真正拦下（第二十一轮：此前 `-v` 只被清单子集断言覆盖，
    # 删掉它全套仍绿。虽然裸 argv 入口已删除、该路径不可达，但把它按行为钉住成本极低）。
    with pytest.raises(SandboxPolicyError, match=r"^检测到危险参数，拒绝执行: -v$"):
        assert_safe(["docker", "run", "-v", "/host:/mnt", "img"])


def test_invariants_detect_network_override() -> None:
    """`--network=host` 必须被拒，且**报错必须指向 --network**。

    第二十一轮修正：原写法用**不完整 argv** 且 `pytest.raises` **没有 `match=`**，
    于是把 `("--network","none")` 从清单删掉后，本用例仍会因"缺少别的必需参数"而通过
    （假绿）——这正是"配套删除即全绿"通道的一部分。
    现在改为从**完整** argv（硬编码锚点拼出）出发、只改被测那一项，并锚定报错原因。
    """
    args = _minimal_argv()
    index = args.index("--network")
    args[index] = "--network=host"  # 只动被测项
    del args[index + 1]

    with pytest.raises(SandboxPolicyError, match=r"^--network 必须为 none，实际为 host$"):
        assert_required_invariants(args)


def test_invariants_detect_missing_read_only() -> None:
    """删掉 `--read-only` 必须被拒，且报错**精确指向**它。"""
    args = _minimal_argv()
    del args[args.index("--read-only")]

    with pytest.raises(SandboxPolicyError, match=r"^缺少必需的安全参数: --read-only$"):
        assert_required_invariants(args)


def test_invariants_detect_root_run() -> None:
    """`--user 0:0` 必须被**root 检查**拦下（而不是"缺少别的参数"）。

    第二十二轮：`match` 必须锚定；此前 `match=r"root"` 未锚定，PLAN 却声称已锚定。
    """
    args = _minimal_argv()
    args[args.index("--user") + 1] = "0:0"

    with pytest.raises(SandboxPolicyError, match=r"^禁止以 root 运行"):
        assert_required_invariants(args)


# ------------------------------------------------- C. 策略自身的构造期校验


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"image": "python:latest"}, "latest 镜像不可复现"),
        ({"image": "python"}, "无 tag 镜像不可复现"),
        ({"run_as": "0:0"}, "禁止 root"),
        ({"cpus": 0}, "cpus 必须为正"),
        ({"timeout_s": 0}, "超时必须为正"),
    ],
)
def test_policy_rejects_unsafe_configuration(kwargs: dict, why: str) -> None:
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( **kwargs)


def test_build_run_args_rejects_empty_command(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    with pytest.raises(SandboxPolicyError):
        _args(sandbox, workspace, command=[])


def test_build_run_args_rejects_container_name_with_slash(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    with pytest.raises(SandboxPolicyError):
        build_run_args(
            sandbox, container_name="a/b", workspace=workspace, command=["ls"]
        )


@pytest.mark.parametrize("size", ["512m,exec", "512 m", "", "1g,size=2g"])
def test_tmpfs_size_rejects_comma_injection(size: str) -> None:
    """真机判决性证据（第十二轮非阻塞项）：`tmpfs_size="512m,exec"` 会**取消 noexec**。

    `docker inspect` 显示 `/tmp:rw,noexec,nosuid,size=512m,exec`——重复选项后写覆盖，
    内核取 `exec`，容器内脚本可执行（基线 `512m` 下被 `noexec` 拦下）。
    现有"argv 里出现 noexec"的字面断言抓不到这种覆盖，所以这里针对字段值断言。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( tmpfs_size=size)


def test_tmpfs_option_string_has_no_injectable_parts(
    workspace: Path,
    sandbox: SandboxPolicy,
) -> None:
    """正向：默认策略生成的 tmpfs 选项串必须恰好是预期形态（可被机器断言）。"""
    args = _args(sandbox, workspace)

    option = args[args.index("--tmpfs") + 1]

    assert option == "/tmp:rw,noexec,nosuid,size=512m"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cpus": "x"},
        {"pids_limit": 1.5},
        {"timeout_s": "1"},
        {"stop_timeout_s": True},
        {"stop_timeout_s": "--privileged"},
        {"stop_timeout_s": 1.5},
        {"tmpfs_size": 512},
    ],
)
def test_type_errors_are_normalised_to_policy_error(kwargs: dict) -> None:
    """类型非法必须抛 `SandboxPolicyError`，而不是裸 `TypeError`/`ValueError`。

    调用方（P1 驱动层）的捕获口径只有 `SandboxPolicyError`；`bool` 是 `int` 子类，
    所以 `stop_timeout_s=True` 这种写法必须被显式排除（否则会变成 `--stop-timeout 1`）。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( **kwargs)


@pytest.mark.parametrize("hostname", ["-x", "a/b", "a b", "", "a,b"])
def test_hostname_rejects_flag_like_and_invalid_values(hostname: str) -> None:
    """前导 `-`（可能被当 flag）、`/`（无效 hostname）、空白与逗号都必须拒绝。

    注意 `"a\\rb"` 不列入：`\\r` 在本模块的字符集检查里**不在**拒绝集内，
    而 docker 自身会拒绝含控制字符的 hostname（fail-closed），故它不是注入面。
    （第二十二轮更正：原文写"`build_run_args` 用的是 `str.splitlines()`"——
    该函数在 `sandbox/policy.py` 里从未出现，理由是错的，结论仍成立。）
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy( hostname=hostname)


def test_assert_safe_blocks_dangerous_flags_behaviourally() -> None:
    """`assert_safe` 的行为断言（取代"只比对常量自身"的装饰性断言）。

    第十二轮指出：`NEVER_ALLOWED_OVERRIDES` 全仓只被一条"断言该常量包含某些字符串"的
    测试引用，而 `assert_safe` 根本不读它——那种断言对行为毫无约束力。
    """
    for dangerous in ("--privileged", "--pid=host", "--ipc=host", "--userns=host", "--uts=host"):
        with pytest.raises(SandboxPolicyError):
            assert_safe(["docker", "run", dangerous, "img"])


# ---------------------------------------------------------------- D. 路径守卫


def test_resolve_within_accepts_legal_relative_path(workspace: Path) -> None:
    assert resolve_within(workspace, "test_x.py") == (workspace / "test_x.py").resolve()


@pytest.mark.parametrize(
    "candidate",
    ["../outside.txt", "a/../../etc/passwd", "/etc/passwd", "C:/Windows/system32"],
)
def test_resolve_within_rejects_escapes(workspace: Path, candidate: str) -> None:
    with pytest.raises(SandboxPolicyError):
        resolve_within(workspace, candidate)


def test_resolve_within_rejects_missing_file_when_required(workspace: Path) -> None:
    with pytest.raises(SandboxPolicyError):
        resolve_within(workspace, "nope.py", must_exist=True)


def test_resolve_within_rejects_symlink_escape(tmp_path: Path, workspace: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "escape_link"
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - 取决于平台权限
        pytest.skip(f"环境不支持创建 symlink：{exc.__class__.__name__}")

    with pytest.raises(SandboxPolicyError):
        resolve_within(workspace, "escape_link")
    assert find_escaping_links(workspace) == ["escape_link"]


def test_internal_symlink_is_not_an_escape(workspace: Path) -> None:
    inside = workspace / "inside.txt"
    inside.write_text("ok", encoding="utf-8")
    link = workspace / "inside_link"
    try:
        link.symlink_to(inside)
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"环境不支持创建 symlink：{exc.__class__.__name__}")

    assert find_escaping_links(workspace) == []


# ------------------------------------------- E. 链接类对象：junction 绝不能漏


def _try_make_junction(link: Path, target: Path) -> bool:
    """Windows 上创建目录联接。`mklink /J` **不需要管理员权限**。"""
    if os.name != "nt":
        return False
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def test_junction_is_recognised_and_not_expanded(tmp_path: Path) -> None:
    """junction 的 `is_symlink()` 为 False、`os.walk(followlinks=False)` 仍会进入。

    只判 `is_symlink()` 时，一个指向宿主任意位置的 junction 会被整棵复制进沙箱，
    "不复制任何链接"的策略被完全绕过。自我引用的 junction 还曾把 `rglob` 展开成
    上千条路径——所以这里同时断言"恰好只报顶层那一个、且不进入它"。

    Windows 上跑 junction；其他平台用 symlink 覆盖**同一逻辑**（CI 在 Linux 上）。
    """
    repo = tmp_path / "junc_repo"
    repo.mkdir()
    (repo / "keep.txt").write_text("k", encoding="utf-8")
    link = repo / "j"

    if os.name == "nt":
        if not _try_make_junction(link, tmp_path):
            # 刻意 fail 而不是 skip：junction 是本用例的**唯一被测对象**，
            # 造不出来就等于链接策略没被验证（原型注释："跳过的检查同样是缺陷"）。
            pytest.fail("Windows 上 mklink /J 失败——junction 策略未被验证，拒绝静默通过")
        # 前提自证：不成立的话这条用例就在测别的东西
        assert link.is_symlink() is False, "junction 的 is_symlink() 必须为 False（前提）"
        assert is_link_like(link) is True, "is_link_like() 必须认出 junction"
    else:
        try:
            link.symlink_to(tmp_path, target_is_directory=True)
        except OSError as exc:
            # 非 Windows 上造不出 symlink 属于环境异常（CI 是 Linux，必然可造）→ fail；
            # Windows 上普通用户默认无 symlink 权限，但**同一逻辑已由 junction 用例覆盖**
            # （junction 不需要管理员权限，且 `is_link_like` 对两者的判定路径相同）。
            if os.name == "nt":
                pytest.skip("Windows 无 symlink 权限：同一逻辑由 junction 用例覆盖")
            pytest.fail(
                f"环境不支持创建 symlink（{exc.__class__.__name__}）——"
                "链接策略未被验证，拒绝静默通过"
            )
        assert is_link_like(link) is True

    found = find_escaping_links(repo)

    assert found == ["j"], f"应恰好报出 1 条且不递归展开，实际 {len(found)} 条：{found[:3]}"


def test_link_like_detects_plain_files_as_not_links(tmp_path: Path) -> None:
    """对照：普通文件与普通目录不得被 `is_link_like` 误判（否则复制会被整棵跳过）。"""
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    directory = tmp_path / "dir"
    directory.mkdir()

    assert is_link_like(plain) is False
    assert is_link_like(directory) is False
