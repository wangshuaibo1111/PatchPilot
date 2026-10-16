# PatchPilot 沙箱执行镜像
#
# 为什么需要自建镜像（而不是直接用 python:3.12-slim）：
#   沙箱要跑 `python -m pytest`，而官方 slim 镜像**不含 pytest**；
#   而沙箱是 `--network none`，**不可能**在容器里 `pip install`。
#   因此按 p1_sandbox_design.md §3 的要求：基础镜像**预装** git + pytest，
#   缩短单题耗时，并让"跑测试"这一步可离线完成。
#
# 可复现纪律（PLAN §6）：tag 必须锁定到具体版本，禁止 latest。
#   构建：docker build -f docker/sandbox/Dockerfile -t patchpilot-sandbox:0.1.0-py312 .
#
# 安全约束：
#   - 镜像本身不携带任何凭据、不写入仓库内容；
#   - 容器运行时仍由 policy.py 施加非 root、只读 rootfs、cap-drop ALL 等（本镜像不负责这些）。

FROM python:3.12-slim

# 版本**锁定**：pytest 与常用插件版本固定，保证同一镜像 tag 下行为可复现。
# `PIP_NO_CACHE_DIR` + `--no-compile` 用于压缩镜像体积。
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 锁定版本的测试依赖（沙箱内跑 pytest 所需的最小集合）
RUN python -m pip install --no-compile \
      "pytest==8.3.4" \
      "pytest-timeout==2.3.1" \
      "pytest-xdist==3.6.1" \
      "pytest-cov==6.0.0"

# 非 root 用户：与 policy.run_as 的默认 1000:1000 对应。
# 注意容器实际以 `--user 1000:1000` 运行，这里建同名用户是为了 HOME 等语义正确。
RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox

WORKDIR /workspace
USER sandbox

# 自检：构建期就确认 pytest 可用，避免"镜像做出来了却跑不起测试"
RUN python -m pytest --version
