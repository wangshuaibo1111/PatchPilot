"""P1 沙箱安全策略（纯逻辑，无需 Docker 即可完整测试）。

设计要点：把「安全参数怎么拼」与「容器怎么起」彻底分开。
前者是纯函数，可以被穷举测试；后者只是一层薄驱动。
**这样安全性的核心不依赖 Docker 就能验证**——这是本模块存在的全部理由。

位置：`src/patchpilot/sandbox/policy.py`（原 `.tmp/p1_staging/sandbox_policy.py`）。
对应的断言已迁为 `tests/test_sandbox_policy.py` 的 pytest 用例；
真正调用 docker 的薄驱动见 `patchpilot.sandbox.manager`（P1 实现）。
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# 内存下限：低于此值容器几乎必然起不来（6 MiB 是 docker 侧常见的最小可用值）。
# 存在的意义是让 `memory="0"` 这类"静默取消上限"的取值在构造期就被拒绝。
MIN_MEMORY_BYTES = 6 * 1024**2

# 沙箱执行镜像：**自建**（`docker/sandbox/Dockerfile`），因为官方 `python:3.12-slim`
# 不含 pytest，而容器是 `--network none`、不可能在容器里 `pip install`。
# tag 锁定到具体版本（PLAN §6 可复现纪律）；构建命令见该 Dockerfile 顶部注释。
DEFAULT_IMAGE = "patchpilot-sandbox:0.1.0-py312"

# 绝不允许出现在最终命令行里的裸参数
NEVER_ALLOWED_TOKENS = (
    "--privileged",
    "--cap-add",
    "--device",
    "--volume",
    "-v",
)

# 安全语义必须由 build_run_args 独占决定，任何外部输入都不得覆盖这些参数。
#
# 缺陷记录（四轮对抗性审查的完整教训，值得保留）：
#   1. 初版把 "--network=host" 直接写进上面的清单，而校验时按 "=" 截断成
#      "--network" 去比对——两边形式不一致，以致 --network=host 与 --pid=host 能绕过；
#   2. **拒绝清单（黑名单）**被短选项**粘连写法**击穿：`-v<host>:/mnt`、`-u0:0`；
#   3. 改**允许清单**后被**布尔短选项捆绑**击穿：`-iu0:0`（pflag 语义 = `-i` + `-u0:0`）；
#   4. 再补状态机后被**悬空取值**击穿：`("-e",)` 吃掉镜像名 → 真机跑了 `python:latest`；
#      并且"flag 紧跟 flag"（`("-e","-l")`）这一相邻态**原理上不可检**——
#      某个 flag 的取值位置被前一个 token 吃掉后，argv 层面再也分不清"用户想要 --init"
#      与"用户在给 -e 传值"。
# **最终结论：这里不该做 token 级校验，而该把可变面降为类型化字段。**
# `extra_args` 字段已删除；所有可调项都是下面 dataclass 里的显式字段，
# 由 build_run_args 唯一拼装。新增字段时必须配构造期校验 + 生成期结构不变量。
# 因此 `NEVER_ALLOWED_TOKENS` / `NEVER_ALLOWED_OVERRIDES` 现在只作为
# `assert_safe` 的**纵深防御**保留（传给它的 argv 由本模块自己生成，理论上不会命中）。
NEVER_ALLOWED_OVERRIDES = (
    "--privileged",
    "--network",
    "--pid",
    "--ipc",
    "--userns",
    "--uts",
    "--security-opt",
    "--cap-drop",
    "--user",
    "--read-only",
    "--mount",
    "--runtime",
)

# 必须出现在最终命令行里的安全不变量：(标志, 期望的取值或 None 表示只校验标志)
REQUIRED_INVARIANTS: tuple[tuple[str, str | None], ...] = (
    ("--network", "none"),
    ("--read-only", None),
    ("--cap-drop", "ALL"),
    ("--security-opt", "no-new-privileges"),
    ("--user", None),
    # 资源上限也要有存在性不变量：否则 `--memory 0` 这类取值会让熔断静默失效
    # （真机：rc=0、无告警、`docker inspect` 得 Memory=0，对照默认 4g = 4294967296）。
    ("--memory", None),
    ("--memory-swap", None),
    ("--pids-limit", None),
)


class SandboxPolicyError(ValueError):
    """策略本身不安全、参数被污染，或路径越界。"""


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    image: str = DEFAULT_IMAGE
    workdir: str = "/workspace"
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    tmpfs_size: str = "512m"
    run_as: str = "1000:1000"
    timeout_s: float = 900.0
    runtime: str | None = None  # 设为 "runsc" 启用 gVisor（需 Linux 宿主）
    env: tuple[tuple[str, str], ...] = (
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("PYTHONUTF8", "1"),
        ("HOME", "/tmp"),
    )
    # ---- 曾经是 `extra_args: tuple[str, ...]`（裸 argv 片段），现已删除 ----
    #
    # 为什么必须删（第十一轮复审的结构性判断，我采纳）：裸 argv 片段的校验必须同时做
    # "逐 token 判定"与"跨 token 状态跟踪"（待取值 / 布尔捆绑 / 短选项粘连 / 位置错位），
    # 而这条状态机**四轮都没收敛**，每轮都被同一类缺陷的新拼写击穿（都有真机证据）：
    #   8 轮 短选项粘连 `-v<host>:/mnt`、`-u0:0`；
    #   9 轮 布尔短选项捆绑 `-iu0:0`（= `-i` + `-u0:0`）；
    #   10 轮 悬空取值 `("-e",)` 吃掉镜像名 → 真机跑了 `python:latest`；
    #   11 轮 "flag 紧跟 flag"（`("-e","-l")` 让 `-l` 变成 `-e` 的取值）——该分支的用例
    #         参数写错，实际从未覆盖，变异实验全绿。
    # 结论：**"取值/位置"错位一旦发生就不可检**（结构断言只看得到镜像位），
    # 所以正确的修法是把可变面从"字符串前缀匹配一个 frozenset"降为"类型化的字段"。
    # 现在所有可调项都是**显式字段**，由 build_run_args 唯一拼装；
    # 状态机、允许清单、29 项拼写表连同那类缺陷一起消失。
    # 新增字段时必须同时给出**构造期校验**与**生成期断言**，见下面的 `__post_init__`
    # 与 `build_run_args` 末尾的结构不变量。
    init: bool = False
    interactive: bool = False
    tty: bool = False
    labels: tuple[tuple[str, str], ...] = ()
    hostname: str | None = None
    stop_timeout_s: int | None = None
    # **显式声明的沙箱根**：bind mount 的宿主源必须落在它之下。
    # 默认取系统临时目录（副本按约定就建在那里）；生产上由驱动层显式指定一个专用工作根。
    # 为什么需要一个"根"而不只是黑名单（第二十二轮 BLOCKING）：挂载源的威胁面是
    # "任意宿主目录"——枚举敏感目录（家目录、系统盘、仓库）永远列不全，真机实测
    # `Path.home()` 就能绕过"是 CWD 祖先"这条规则（家目录与仓库不在同一棵树上）。
    # 改为**白名单式包含判定**后，逃逸面被压到"显式声明的根之内"，可审计、可测试。
    sandbox_root: Path | None = None

    def __post_init__(self) -> None:
        # 字符串字段先做类型检查：否则 `image=1` / `workdir=None` / `run_as=1` 会在下面
        # 的 `endswith` / `startswith` / `strip` 处抛**裸 AttributeError**，
        # 而调用方的捕获口径只有 SandboxPolicyError（第十三轮非阻塞项）。
        _require_str("image", self.image)
        _require_str("workdir", self.workdir)
        _require_str("run_as", self.run_as)
        _require_str("tmpfs_size", self.tmpfs_size)
        _require_str("memory", self.memory)
        if not self.image:
            raise SandboxPolicyError("镜像不能为空")
        # **镜像位是唯一处于 flag 位置的字段**：它被写在 argv 里"下一个 flag 之前"，
        # 所以一旦允许它像 flag，docker 的 pflag 就会把它当选项解析。
        # 真机判决性证据（第二十二轮）：`image="-vC:\\…\\hostsecret:/mnt"` 构造成功，
        # `build_run_args` 把它写在镜像位，**docker pflag 的短选项粘连**（`-v<value>`）
        # 把它解析成 `--volume` → 容器经 `/mnt/SECRET.txt` 读到宿主文件，rc=0。
        # 这正是本模块第 8 轮被 `-v<host>:/mnt` 击穿的**同一条通道经 image 字段复发**。
        if self.image.startswith("-"):
            raise SandboxPolicyError(
                f"镜像名不得以 `-` 开头（会被 docker 当选项解析）: {self.image!r}"
            )
        if any(ch in self.image for ch in " \t\n\x00"):
            raise SandboxPolicyError(f"镜像名不得含空白或 NUL: {self.image!r}")
        if self.image.endswith(":latest") or ":" not in self.image:
            raise SandboxPolicyError(f"镜像必须锁定具体 tag，禁止 latest/无 tag: {self.image}")
        # 类型面：显式检查，避免把裸 TypeError/ValueError 抛给只捕获
        # SandboxPolicyError 的调用方（第十二轮非阻塞项）。必须在比较之前做，
        # 否则 `cpus="x"` 会在 `<= 0` 处抛裸 TypeError。
        _require_number("cpus", self.cpus)
        _require_int("pids_limit", self.pids_limit)
        _require_number("timeout_s", self.timeout_s)
        if self.stop_timeout_s is not None:
            _require_int("stop_timeout_s", self.stop_timeout_s)
        if self.cpus <= 0:
            raise SandboxPolicyError("cpus 必须为正数")
        if self.pids_limit <= 0:
            raise SandboxPolicyError("pids_limit 必须为正数")
        if self.timeout_s <= 0:
            raise SandboxPolicyError("timeout_s 必须为正数")
        if self.stop_timeout_s is not None and self.stop_timeout_s < 0:
            raise SandboxPolicyError("stop_timeout_s 不能为负数")
        _validate_run_as(self.run_as)
        _validate_workdir(self.workdir)
        _validate_tmpfs_size(self.tmpfs_size)
        _validate_memory(self.memory)
        _validate_hostname(self.hostname)
        # `env`/`labels` 必须是"二元组序列"：传 dict / list / str 时，解包会抛**裸
        # ValueError**（`not enough values to unpack`），与"调用方只捕获 SandboxPolicyError"
        # 的口径冲突（第十四轮非阻塞项）。先检查容器形态，再检查每对的内容。
        _require_pairs("env", self.env)
        _require_pairs("labels", self.labels)
        for key, value in self.env:
            _require_str(f"env[{key!r}] 的取值", value)
            _require_str("env 的变量名", key)
            if not key or any(ch in key for ch in "= \t\n\x00"):
                raise SandboxPolicyError(f"非法环境变量名: {key!r}")
            if any(ch in value for ch in " \t\n\x00"):
                raise SandboxPolicyError(f"环境变量 {key} 的取值不得含空白: {value!r}")
        for key, value in self.labels:
            _require_str(f"labels[{key!r}] 的取值", value)
            _require_str("label 名", key)
            if not key or any(ch in key for ch in "= \t\n\x00"):
                raise SandboxPolicyError(f"非法 label 名: {key!r}")
            if any(ch in value for ch in " \t\n\x00"):
                raise SandboxPolicyError(f"label {key} 的取值不得含空白: {value!r}")


def _validate_run_as(run_as: str) -> None:
    """按**数值**判定 uid：`00:0`、`0:1000`、`root:root` 都是 root，必须拒绝。

    缺陷记录（真机判决性证据）：旧实现是字符串精确比对
    `run_as in {"", "0", "0:0", "root"}`，于是 `run_as="00:0"` 不仅通过构造期校验，
    连 `assert_required_invariants` 的 `user.split(":")[0] in {"0", "root"}` 也骗过——
    `docker run --user 00:0` 在容器里 `id -u` 就是 **0**。
    Docker 默认无 userns-remap 时，容器 root 对 bind mount 进来的副本就是宿主 root。
    """
    text = run_as.strip()
    if not text:
        raise SandboxPolicyError("run_as 不能为空（禁止以 root 运行）")
    parts = text.split(":")
    if len(parts) > 2:
        raise SandboxPolicyError(f"非法 run_as（应为 uid 或 uid:gid）: {run_as!r}")
    for part in parts:
        piece = part.strip()
        if piece.lower() == "root":
            raise SandboxPolicyError(f"禁止以 root 运行: {run_as!r}")
        # 只接受 **ASCII** 数字：Python 的 `int()` 会接受全角/阿拉伯-印度数字
        # （`run_as="１０００:１０００"`），而 docker 认不出来（报 unable to find user）。
        # 那虽然 fail-closed，却与 docstring 的"必须是数值 uid/gid"口径不符。
        if not piece.isascii() or not piece.isdigit():
            raise SandboxPolicyError(
                f"run_as 必须是 ASCII 数值 uid/gid: {run_as!r}"
            )
        # 到这里 `piece` 已确定是 ASCII 数字 → 必为非负，故无需再判 `< 0`（死分支）。
        value = int(piece, 10)
        if value == 0:
            raise SandboxPolicyError(f"禁止以 root 运行（uid/gid 为 0）: {run_as!r}")


def _validate_workdir(workdir: str) -> None:
    """workdir 会同时进入 `--workdir` 与 `--mount` 的逗号分隔串。

    缺陷记录：
      1. 真机上 `workdir="/workspace,readonly"` 会被 Docker 解析成
         `dst=/workspace` + `ReadOnly=true`，也就是**用逗号往 mount 选项里注入**；
      2. `workdir="/"` 会把工作副本挂到根、遮蔽整个 rootfs。后者在真机上会因
         镜像二进制消失而 fail-closed（实测），但不是可靠的失败方式——
         这里显式限制在 `/workspace` 或 `/tmp` 子树内。
    """
    if not workdir.startswith("/"):
        raise SandboxPolicyError(f"workdir 必须是绝对路径: {workdir!r}")
    if "," in workdir or any(ch.isspace() for ch in workdir):
        raise SandboxPolicyError(
            f"workdir 不得含逗号或空白（逗号会注入 mount 选项）: {workdir!r}"
        )
    # `..` 会让 `startswith("/workspace")` 这类判定被绕过（`/workspace/../etc`）。
    # Docker 不做路径归一化，所以这里直接拒绝任何回溯段。
    if ".." in workdir.replace("\\", "/").split("/"):
        raise SandboxPolicyError(f"workdir 不得含 `..` 回溯段: {workdir!r}")
    in_workspace = workdir == "/workspace" or workdir.startswith("/workspace/")
    in_tmp = workdir == "/tmp" or workdir.startswith("/tmp/")
    if not (in_workspace or in_tmp):
        raise SandboxPolicyError(
            f"workdir 只允许 /workspace 或 /tmp 子树（避免遮蔽 rootfs）: {workdir!r}"
        )


def _validate_tmpfs_size(tmpfs_size: str) -> None:
    """tmpfs 大小会拼进 `--tmpfs /tmp:rw,noexec,nosuid,size=<value>` 的**逗号串**。

    真机判决性证据（第十二轮非阻塞项）：`tmpfs_size="512m,exec"` 构造期放行，
    `docker inspect` 显示 `/tmp:rw,noexec,nosuid,size=512m,exec`——重复选项**后写覆盖**，
    内核取 `exec`，容器内脚本可执行（基线 `512m` 下同一脚本被 `noexec` 拦下）。
    即"只读 rootfs + tmpfs"承诺里的 `noexec` 可被字段值静默取消（仍未获 root、
    无宿主挂载、无网络，故不是逃逸，但必须堵住）。与 `_validate_workdir` 同一类注入面。
    """
    if not isinstance(tmpfs_size, str):
        raise SandboxPolicyError(f"tmpfs_size 类型非法: {type(tmpfs_size).__name__}")
    if not tmpfs_size or any(ch in tmpfs_size for ch in ", \t\n\x00"):
        raise SandboxPolicyError(
            f"tmpfs_size 不得为空或含逗号/空白（逗号会注入 tmpfs 挂载选项）: {tmpfs_size!r}"
        )


def _validate_memory(memory: str) -> None:
    """内存上限是本模块唯一没有任何校验的资源字段——真机证据表明它可以被**静默取消**。

    缺陷记录（第十三轮 BLOCKING）：`memory="0"` / `"0b"` 构造期放行 →
    `--memory 0 --memory-swap 0`，`assert_safe` 与 `assert_required_invariants` 都不覆盖
    `--memory` → 真机 `docker run` **rc=0、无任何告警**，`docker inspect` 得
    `HostConfig.Memory=0 MemorySwap=0`（对照默认 `4g` = 4294967296）。
    同模块对 `cpus=0`/`pids_limit=0`/`timeout_s=0` 都明确拒绝，唯独内存可无声抹掉——
    属"资源熔断失效/静默错误结果"。

    另外 `memory=1` / `True` 会从 `assert_safe` 抛**裸 AttributeError**，
    与"调用方只需捕获 SandboxPolicyError"的口径冲突，所以这里先做类型检查。
    """
    if not isinstance(memory, str) or isinstance(memory, bool):
        raise SandboxPolicyError(
            f"memory 必须是字符串（如 '4g'）: {memory!r}（类型 {type(memory).__name__}）"
        )
    text = memory.strip()
    if not text:
        raise SandboxPolicyError("memory 不能为空")
    # 校验对象必须与**生成对象**一致：此前用 `text`（strip 后）检查、却把原始 `memory`
    # 写进 argv，于是 `"4G "`/`"\t4g"` 能过构造期，到 docker 才以 125 失败
    # （第十四轮非阻塞项）。这里直接拒绝空白，让"校验通过"等价于"argv 里的字面量合法"。
    if any(ch in memory for ch in " \t\n\x00"):
        raise SandboxPolicyError(f"memory 不得含空白或 NUL: {memory!r}")
    # `isascii()` 是**冗余纵深防御**（第十四轮复审指出：非 ASCII 取值如 `'４g'`
    # 本来就会被 `_parse_size_bytes` 的 `isdigit()` 拒掉，删掉它全绿）。
    # 保留是让"非 ASCII"这类输入有独立出口；不声称它被用例钉住。
    if not memory.isascii():
        raise SandboxPolicyError(f"memory 必须是 ASCII: {memory!r}")
    value = _parse_size_bytes(text)
    if value is None:
        raise SandboxPolicyError(
            f"memory 必须是整数+单位的形式（如 '512m'、'4g'、'104857600'）: {memory!r}"
        )
    # 解析器只返回正整数或 None，故此处不再判 `value <= 0`（原为冗余死分支）。
    if value < MIN_MEMORY_BYTES:
        raise SandboxPolicyError(
            f"memory 过小（{memory!r} < {MIN_MEMORY_BYTES} 字节），容器无法运行"
        )


def _parse_size_bytes(text: str) -> int | None:
    """把 docker 的容量写法（`512m` / `4g` / `104857600`）解析成字节数。

    只接受单字母后缀 b/k/m/g（docker 本身接受的写法）；解析失败返回 None。
    """
    suffixes = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    number = text[:-1] if text[-1:].lower() in "bkmg" else text
    suffix = text[len(number) :].lower()
    if suffix not in suffixes or not number.isdigit():
        return None
    return int(number) * suffixes[suffix]


def _validate_hostname(hostname: str | None) -> None:
    """hostname 会直接进 `--hostname <value>`。

    除空白/逗号/NUL 外，还要拒绝**前导 `-`**（可能被 docker 当成 flag 解析）
    与 `/`（hostname 里 `/` 是无效值，真机上会被原样写进 `/etc/hostname`）。
    """
    if hostname is None:
        return
    if (
        not isinstance(hostname, str)
        or not hostname
        or hostname.startswith("-")
        or any(ch in hostname for ch in " \t\n\x00,/")
    ):
        raise SandboxPolicyError(f"非法 hostname: {hostname!r}")


def _require_pairs(name: str, value: object) -> None:
    """`env`/`labels` 必须是"二元组序列"，且每个元素本身是长度 2 的序列。

    否则 `for key, item in value` 会抛裸 `ValueError: not enough values to unpack`
    （传 dict 时迭代出的是 key、传 str 时迭代出的是字符），调用方无法用
    `SandboxPolicyError` 捕获。显式检查后给出可操作的错误信息。
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise SandboxPolicyError(
            f"{name} 必须是 (名, 取值) 二元组的序列，实际是 {type(value).__name__}"
        )
    for pair in value:
        if isinstance(pair, (str, bytes)) or not isinstance(pair, (tuple, list)):
            raise SandboxPolicyError(
                f"{name} 的每一项都必须是 (名, 取值) 二元组，实际是 {type(pair).__name__}"
            )
        if len(pair) != 2:
            raise SandboxPolicyError(
                f"{name} 的每一项必须恰好 2 个元素，实际 {len(pair)} 个: {pair!r}"
            )


def _require_str(name: str, value: object) -> None:
    """字符串字段的显式类型检查（把裸 AttributeError 收敛成 SandboxPolicyError）。"""
    if not isinstance(value, str):
        raise SandboxPolicyError(
            f"{name} 必须是字符串: {value!r}（类型 {type(value).__name__}）"
        )


def _require_int(name: str, value: object) -> None:
    """整数字段的显式类型检查（`bool` 是 `int` 子类，必须单独排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SandboxPolicyError(f"{name} 必须是整数（不接受布尔值/浮点）: {value!r}")


def _require_number(name: str, value: object) -> None:
    """数值字段的显式类型检查：让调用方只需捕获 `SandboxPolicyError` 一种异常。

    缺陷记录（第十二轮）：`cpus="x"` / `pids_limit=1.5` / `stop_timeout_s="--privileged"`
    会抛裸 `TypeError`/`ValueError`，而调用方的捕获口径是 `SandboxPolicyError`。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SandboxPolicyError(f"{name} 必须是数值: {value!r}")


def _validate_mount_source(workspace: Path, sandbox_root: Path | None) -> None:
    """校验 bind mount 的宿主源目录——容器可见面的**唯一决定点**。

    真机判决性证据（第二十二轮，Docker 29.5.2）：
      - `workspace="C:\\\\"` → `--mount type=bind,src=C:\\,dst=/workspace` rc=0，
        容器 `ls /workspace` 列出 `Program Files`/`Users`/…（**整个系统盘可读写**）；
      - `workspace=<宿主目录>` → 容器 `cat` 到宿主机密文件；
      - `workspace=".."` → 被 docker 客户端按**它自己的 cwd**静默解析成宿主目录。
    三条都能通过 `assert_safe` 与 `assert_required_invariants`，即此前**没有任何守卫**。

    规则（逐条对应上面的证据）：
      1. 必须是**绝对**路径——堵住 `".."` / `"src"` 被客户端自行解析；
      2. 必须**存在且是目录**；
      3. 不得是**文件系统锚点**（`/`、`C:\\`）——堵住整盘挂载；
      4. 必须落在**显式声明的沙箱根**之下（`sandbox_root`，默认系统临时目录）——
         白名单式包含判定。仅靠"不是 CWD 祖先"这类黑名单不够：真机实测
         `Path.home()` 因与仓库不同盘而绕过该规则。

    `Path(ws).parent == Path(ws)` 是"文件系统根"的跨平台判据；比较前统一 `.resolve()`，
    避免 `a/../..` 之类绕过。
    """
    if not workspace.is_absolute():
        raise SandboxPolicyError(
            f"沙箱挂载源必须是绝对路径（相对路径会被 docker 按客户端 cwd 解析）: {workspace}"
        )
    resolved = workspace.resolve()
    if resolved.parent == resolved:
        raise SandboxPolicyError(f"沙箱挂载源不得是文件系统根（整盘/整机暴露）: {resolved}")
    if not resolved.exists():
        raise SandboxPolicyError(f"沙箱挂载源不存在: {resolved}")
    if not resolved.is_dir():
        raise SandboxPolicyError(f"沙箱挂载源必须是目录: {resolved}")

    root = (sandbox_root or Path(tempfile.gettempdir())).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise SandboxPolicyError(
            f"沙箱挂载源必须落在声明的沙箱根之下（避免把任意宿主目录交给容器）: "
            f"{resolved} 不在 {root} 之下"
        )


def build_run_args(
    policy: SandboxPolicy,
    *,
    container_name: str,
    workspace: Path,
    command: list[str],
) -> list[str]:
    """生成 `docker run` 参数。这是唯一允许拼装安全参数的入口。"""
    # 生成期也要做类型检查：`container_name=1` / `command=[None]` / `workspace=None`
    # 此前会抛裸 `TypeError`/`AttributeError`（第十四轮非阻塞项）。
    _require_str("container_name", container_name)
    if isinstance(command, (str, bytes)) or not isinstance(command, (tuple, list)):
        raise SandboxPolicyError(
            f"command 必须是字符串序列，实际是 {type(command).__name__}"
            "（传字符串会按字符展开）"
        )
    if not command:
        raise SandboxPolicyError("命令不能为空")
    for index, token in enumerate(command):
        _require_str(f"command[{index}]", token)
    if not isinstance(workspace, (str, os.PathLike)):
        raise SandboxPolicyError(
            f"workspace 必须是路径或字符串，实际是 {type(workspace).__name__}"
        )
    if not container_name or any(ch in container_name for ch in " \t\n/"):
        raise SandboxPolicyError(f"非法容器名: {container_name!r}")

    workspace = Path(workspace)
    # **宿主暴露面的唯一守卫**（第二十二轮 BLOCKING）：`workspace` 直接进
    # `--mount type=bind,src=<workspace>,dst=<workdir>`，是"容器能看到宿主什么"的
    # 唯一决定点。此前它只做了类型检查，**没有任何安全校验**——真机实测
    # `workspace="C:\\"` → 整个 C 盘可读写挂进 `/workspace`（容器列出 Program Files/Users），
    # `workspace=<宿主目录>` → 容器直接读到宿主机密文件，`workspace=".."` → 被 docker 客户端
    # 按自己的 cwd 静默解析成宿主目录（两个守卫全过、rc=0）。
    _validate_mount_source(workspace, policy.sandbox_root)
    args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",
        "--user",
        policy.run_as,
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,size={policy.tmpfs_size}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        str(policy.cpus),
        "--memory",
        policy.memory,
        "--memory-swap",
        policy.memory,
        "--pids-limit",
        str(policy.pids_limit),
        "--workdir",
        policy.workdir,
        "--mount",
        f"type=bind,src={workspace},dst={policy.workdir}",
    ]
    # ---- 可选字段：全部由**类型化字段**决定，不再是裸 argv 片段 ----
    # 布尔项只写 flag 本身（`--init`），绝不接受 `=任意值`——这样就不存在
    # "取值被前一个 token 吃掉"的相邻态问题（见模块顶部的缺陷记录）。
    if policy.init:
        args.append("--init")
    if policy.interactive:
        args.append("--interactive")
    if policy.tty:
        args.append("--tty")
    if policy.hostname is not None:
        args += ["--hostname", policy.hostname]
    if policy.stop_timeout_s is not None:
        args += ["--stop-timeout", str(policy.stop_timeout_s)]
    for key, value in policy.env:
        args += ["--env", f"{key}={value}"]
    for key, value in policy.labels:
        args += ["--label", f"{key}={value}"]
    if policy.runtime:
        args += ["--runtime", policy.runtime]
    args += [policy.image, *command]

    # 生成与执行前**都要**校验不变量。漏掉这一句时，`run_as="0:1000"` 能经
    # `build_run_args` 一路走到底（构造期与 assert_safe 都拦不住），真机 `id -u` = 0。
    assert_safe(args)
    assert_required_invariants(args)
    # **结构不变量**（纵深防御）：镜像必须正好在"命令之前那一位"。
    # 删掉裸 `extra_args` 后这一类错位已不可能发生，但断言留着——
    # 它守的是"将来有人再加一个可调字段时不得打乱 argv 结构"。
    expected_image_index = len(args) - len(command) - 1
    actual = args[expected_image_index] if 0 <= expected_image_index < len(args) else "<越界>"
    if expected_image_index < 0 or actual != policy.image:
        raise SandboxPolicyError(
            "argv 结构被打乱：镜像未出现在预期位置"
            f"（期望 {policy.image!r} 位于索引 {expected_image_index}，实际 {actual!r}）"
        )
    return args


def _flag_values(args: list[str], flag: str) -> list[str]:
    """`flag` 的**全部**出现（`--flag value` 与 `--flag=value` 两种写法都认）。

    缺陷记录：旧实现只返回第一个匹配，于是"重复 flag"对不变量校验完全不可见——
    `--user 1000:1000 … --user 0:0` 会被当成合法的非 root 参数。
    """
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg == flag:
            if index + 1 < len(args):
                values.append(args[index + 1])
            continue
        if arg.startswith(f"{flag}="):
            values.append(arg.split("=", 1)[1])
    return values


def _flag_value(args: list[str], flag: str) -> str | None:
    """兼容旧调用点：返回第一个取值（内部改用 `_flag_values`）。"""
    values = _flag_values(args, flag)
    return values[0] if values else None


def assert_safe(args: list[str]) -> None:
    """最后一道防线：无论上游怎么拼，危险参数一律拒绝执行。

    与 `assert_required_invariants` 配对使用——前者排除危险，后者保证必要防护没被漏掉。
    """
    if not args or args[0] != "docker":
        raise SandboxPolicyError("命令必须由 docker 开头")
    if len(args) < 2 or args[1] != "run":
        raise SandboxPolicyError(f"只允许 `docker run`，实际为: {args[:2]}")

    for arg in args:
        head = arg.split("=", 1)[0]
        if head in NEVER_ALLOWED_TOKENS:
            raise SandboxPolicyError(f"检测到危险参数，拒绝执行: {arg}")
        if head in ("--pid", "--ipc", "--userns", "--uts"):
            raise SandboxPolicyError(f"不得设置命名空间参数: {arg}")


def assert_required_invariants(args: list[str]) -> None:
    """校验安全不变量确实存在，且**没有被重复传入的 flag 覆盖**。

    调用点：`build_run_args` 末尾（生成时）与执行前。必须同时检查每个 flag 的**全部**
    出现，否则 `--user 1000:1000 … --user 0:0` 这类覆盖会被静默放行。
    """
    for flag, expected in REQUIRED_INVARIANTS:
        values = _flag_values(args, flag)
        if not values:
            raise SandboxPolicyError(f"缺少必需的安全参数: {flag}")
        if len(values) > 1:
            raise SandboxPolicyError(f"{flag} 被重复传入（可能覆盖安全语义）: {values}")
        if expected is not None and values[0] != expected:
            raise SandboxPolicyError(f"{flag} 必须为 {expected}，实际为 {values[0]}")
    # 非 root：按**数值**判定，覆盖 00:0 / 0:1000 / root:root 等变体
    for user in _flag_values(args, "--user"):
        _validate_run_as(user)
    # 资源上限不得被"0"静默取消（真机：`--memory 0` 会让 docker 不设上限且不告警）。
    #
    # **不要用字面量表**：第一版写成 `value in {"0","0b","0k","0m","0g"}`，而真机对
    # `0G`/`00`/`0000`/`0B` 同样报 rc=0 且 `HostConfig.Memory=0`——大小写与多位零都是洞，
    # 且删表项不会有任何用例变红（第十四轮 BLOCKING，同一类缺陷在同一层的复发）。
    # 改为**与 `_validate_memory` 共用的解析器**：只有"能解析出正值"才算合法。
    for flag in ("--memory", "--memory-swap", "--pids-limit"):
        for value in _flag_values(args, flag):
            payload = str(value).strip()
            if flag == "--pids-limit":
                # `isdigit()` 已排除负号与空串，所以只需判零——`<= 0` 与 `== 0`
                # 在这里**行为等价**（变异自证：`M-pids-branch-sign` 存活，属等价变异，
                # 不是覆盖缺口）。写成 `== 0` 以免读者误以为还挡着负值。
                if not payload.isdigit() or int(payload) == 0:
                    raise SandboxPolicyError(f"{flag} 必须为正整数: {value!r}")
                continue
            size = _parse_size_bytes(payload)
            if size is None or size <= 0:
                raise SandboxPolicyError(
                    f"{flag} 不得为 0（等于取消该资源上限）: {value!r}"
                )


def resolve_within(root: Path, candidate: str, *, must_exist: bool = False) -> Path:
    """把 candidate 解析到 root 之内。

    拒绝：绝对路径、`..` 回溯、symlink 指向 root 之外。
    解析用 `resolve()` 会跟随符号链接，因此 symlink 逃逸也会被 `relative_to` 拦下。
    """
    root = Path(root).resolve()
    raw = PurePosixPath(candidate.replace("\\", "/"))
    if raw.is_absolute():
        raise SandboxPolicyError(f"拒绝绝对路径: {candidate}")
    if ".." in raw.parts:
        raise SandboxPolicyError(f"拒绝路径回溯: {candidate}")
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SandboxPolicyError(f"路径逃逸出工作区: {candidate}") from exc
    if must_exist and not target.exists():
        raise SandboxPolicyError(f"路径不存在: {candidate}")
    return target


def is_link_like(path: Path) -> bool:
    """是否为「链接类」文件系统对象：符号链接 **或** Windows 目录联接（junction）。

    为什么必须单独判 junction（实测结论，不是推测）：
      - `mklink /J` 创建 junction **不需要管理员权限**；
      - `Path.is_symlink()` 对 junction 返回 **False**；
      - `Path.is_dir()` 返回 True；
      - `os.walk(followlinks=False)` **仍然会进入 junction**。

    三者叠加的后果：只判 `is_symlink()` 时，仓库里一个指向 `C:\\Users\\...`
    的 junction 会被整棵递归复制进沙箱，把「不复制任何链接」的策略完全绕过。
    """
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse) and bool(getattr(info, "st_file_attributes", 0) & reparse)


def find_escaping_links(root: Path) -> list[str]:
    """列出指向 root 之外的链接类对象（相对路径形式，去重排序）。

    工作区副本复制完成后必须调用：只要非空就拒绝挂载进容器。
    这一条必须在**复制阶段**做——容器内已经没有校验能力。

    **不能用 `Path.rglob`**（实测教训）：rglob 会跟着 junction 递归，
    而一个指向自己祖先目录的 junction 会让遍历无限展开——
    安全网检查本身被恶意仓库打爆，返回上千条 'j/junc_repo/j/junc_repo/…'。
    必须像 workspace_copy 那样用 os.walk + 剪枝：遇到链接类目录只判定、不进入。
    """
    root = Path(root).resolve()
    escaping: list[str] = []

    def record(path: Path) -> None:
        try:
            path.resolve().relative_to(root)
        except (ValueError, OSError):
            escaping.append(path.relative_to(root).as_posix())

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            path = current / name
            if is_link_like(path):
                record(path)  # 判定后**不进入**，避免自我引用导致无限递归
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = current / name
            if is_link_like(path):
                record(path)

    return sorted(set(escaping))
