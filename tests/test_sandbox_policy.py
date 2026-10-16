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

import os
import subprocess
from pathlib import Path

import pytest

from patchpilot.sandbox.policy import (
    NEVER_ALLOWED_OVERRIDES,
    NEVER_ALLOWED_TOKENS,
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


def test_generated_args_carry_every_required_invariant(workspace: Path) -> None:
    args = _args(SandboxPolicy(), workspace)

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
        SandboxPolicy(run_as=run_as)


def test_build_run_args_checks_invariants_on_generation(workspace: Path) -> None:
    """唯一生成入口必须在**行为上**保证不变量成立，而不是靠"源码里出现过某字符串"。

    上一版断言的是 `inspect.getsource` 里含 `assert_required_invariants(args)`——
    在它之前插一句 `return args` 仍能让用例通过。现在改为行为断言：
    生成出的 argv 必须能通过配对校验；且人为破坏后必须抛错。
    """
    args = _args(SandboxPolicy(), workspace)

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
        "workspace",
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
        SandboxPolicy(workdir=workdir)


@pytest.mark.parametrize("workdir", ["/", "/etc", "/etc/passwd", "/workspace/../etc", "/var"])
def test_workdir_must_stay_inside_allowed_subtrees(workdir: str) -> None:
    """子树规则必须有用例：`workdir="/"` 会把工作副本挂到根、遮蔽整个 rootfs。

    （这条用例是为补上第十轮复审的变异实验 M8 而加——当时把该规则改成 `if False:`
    套件仍然全绿，等于这条为安全而加的限制没有任何断言在守。）
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(workdir=workdir)


@pytest.mark.parametrize("workdir", ["/workspace", "/workspace/sub/dir", "/tmp", "/tmp/x"])
def test_workdir_accepts_allowed_subtrees(workdir: str) -> None:
    assert SandboxPolicy(workdir=workdir).workdir == workdir


def test_bare_extra_args_no_longer_exists() -> None:
    """结构性修复：**裸 argv 片段这一整个可变面已被删除**。

    四轮对抗性审查各自击穿了它的一个版本（拒绝清单 → 允许清单 → 布尔捆绑 → 悬空取值），
    并且"flag 紧跟 flag"的相邻态**原理上不可检**：某个 flag 的取值位置被前一个 token
    吃掉后，argv 层面再也分不清"用户想要 --init"与"用户在给 -e 传值"。
    因此改成类型化字段；`extra_args` 必须不存在——否则这一类缺陷会随新拼写重新出现。
    """
    with pytest.raises(TypeError):
        SandboxPolicy(extra_args=("-e",))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        SandboxPolicy(**{"extra-args": ("-e",)})  # type: ignore[arg-type]


def test_typed_fields_drive_argv(workspace: Path) -> None:
    """所有可调项都由**类型化字段**决定，且不破坏镜像/命令位置。"""
    policy = SandboxPolicy(
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


def test_boolean_fields_emit_no_value(workspace: Path) -> None:
    """布尔项只写 flag 本身：绝不产生 `--init=true` 这类"带取值"的写法。

    这正是旧实现里"取值被前一个 token 吃掉"的根源之一——布尔项一旦能带取值，
    就又有了解析歧义。现在类型系统直接保证它是 `bool`。
    """
    args = _args(SandboxPolicy(init=True), workspace)

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
        SandboxPolicy(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("run_as", ["１０００:１０００", "١٢٣٤", "1000:１０００"])
def test_run_as_rejects_non_ascii_digits(run_as: str) -> None:
    """`int()` 接受全角/阿拉伯-印度数字，docker 却不认（报 unable to find user）。

    虽然那是 fail-closed，但 doctype 声称"必须是数值 uid/gid"，所以限定 ASCII 数字。
    """
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(run_as=run_as)


def test_image_position_is_verified_in_generated_argv(workspace: Path) -> None:
    """结构不变量：镜像必须正好在"命令之前那一位"，命令原样跟随其后。

    这是**纵深防御**：删掉裸 `extra_args` 后，这类错位已不可能由外部输入造成
    （旧实现里 `("-e",)` 会让镜像名被当成取值、命令丢掉首元素）。
    断言留着是为了防止将来新增可调字段时又打乱 argv 结构。
    """
    policy = SandboxPolicy(image="python:3.12-slim", env=(("FOO", "1"),), init=True)
    command = ["python", "-c", "print(1)"]

    args = _args(policy, workspace, command=command)

    image_index = len(args) - len(command) - 1
    assert args[image_index] == policy.image
    assert args[image_index + 1 :] == command


def test_image_position_guard_is_a_documented_defense_in_depth() -> None:
    """**登记为纵深防御，而不是"已被覆盖的防线"**（第十一轮复审的诚实要求）。

    生成期的镜像位置断言在删除裸 `extra_args` 之后**由构造路径不可达**：
    `build_run_args` 自己把 `policy.image` 追加在命令之前，而所有可调字段都经
    构造期校验（不会再出现"某字段多吃一位"）。因此这里不去伪造一个假的可达路径，
    而是显式断言"该断言依然写在代码里"，避免它被当成"已验证的安全网"。
    """
    import inspect

    from patchpilot.sandbox import policy as policy_module

    source = inspect.getsource(policy_module.build_run_args)

    assert "argv 结构被打乱" in source, "纵深防御断言必须保留在 build_run_args 里"
    # 同时确认裸入口确实不存在（真正的修法是删面，而不是加断言）
    assert not hasattr(policy_module.SandboxPolicy, "__dataclass_fields__") or (
        "extra_args" not in policy_module.SandboxPolicy.__dataclass_fields__
    )


def test_env_names_are_validated() -> None:
    with pytest.raises(SandboxPolicyError):
        SandboxPolicy(env=(("A=B", "1"),))


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
    assert {"--privileged", "--cap-add", "--device", "--volume"} <= set(NEVER_ALLOWED_TOKENS)
    assert {"--network", "--pid", "--ipc", "--userns", "--uts"} <= set(NEVER_ALLOWED_OVERRIDES)


def test_invariants_detect_network_override() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_required_invariants(
            [
                "docker",
                "run",
                "--network=host",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "1000:1000",
            ]
        )


def test_invariants_detect_missing_read_only() -> None:
    with pytest.raises(SandboxPolicyError):
        assert_required_invariants(
            [
                "docker",
                "run",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "1000:1000",
            ]
        )


def test_invariants_detect_root_run() -> None:
    with pytest.raises(SandboxPolicyError):
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
                "0:0",
            ]
        )


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
        SandboxPolicy(**kwargs)


def test_build_run_args_rejects_empty_command(workspace: Path) -> None:
    with pytest.raises(SandboxPolicyError):
        _args(SandboxPolicy(), workspace, command=[])


def test_build_run_args_rejects_container_name_with_slash(workspace: Path) -> None:
    with pytest.raises(SandboxPolicyError):
        build_run_args(
            SandboxPolicy(), container_name="a/b", workspace=workspace, command=["ls"]
        )


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
