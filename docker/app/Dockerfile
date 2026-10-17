# PatchPilot 面板镜像（验收项 7：一键部署）
#
# 只装"跑面板"所需的最小依赖：`.[server]`。
# 面板本身只读 trace 与评测报告，不执行任何不可信代码，因此镜像里**不需要** Docker、
# 不需要沙箱；需要跑修复任务时用宿主机的 `pp run`（或另起一个带 docker.sock 的任务容器）。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷贝打包所需的元数据，利用层缓存：依赖不变时改代码不会重装依赖
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && pip install ".[server]"

# trace 与评测报告以只读卷挂进来（面板无状态，重启不丢数据）
ENV PP_TRACE_DIR=/data/traces

EXPOSE 8000

# 健康检查供 compose 判定就绪（面板提供 /api/health）
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status==200 else 1)"

CMD ["python", "-m", "patchpilot.cli", "serve", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--trace-dir", "/data/traces", "--eval-report", "/data/eval/report.json"]
