# 供 WSL / Linux / CI 使用；Windows 原生终端请直接用 pp 命令
.PHONY: help venv install doctor dev-up dev-down smoke test lint fmt clean

help:
	@echo "make install   - 创建 .venv 并安装依赖（含 dev）"
	@echo "make doctor    - 环境自检（Python / Docker / 依赖 / 网络）"
	@echo "make dev-up    - 启动 postgres + redis + qdrant"
	@echo "make dev-down  - 停止依赖服务"
	@echo "make smoke     - 跑通最小 LangGraph 图并输出 trace"
	@echo "make test      - 运行单元测试"
	@echo "make lint      - ruff 检查"
	@echo "make clean     - 清理运行时产物"

venv:
	python -m venv .venv

install: venv
	.venv/bin/python -m pip install -e ".[dev]"

doctor:
	.venv/bin/pp doctor

dev-up:
	.venv/bin/pp dev-up

dev-down:
	.venv/bin/pp dev-down

smoke:
	.venv/bin/pp smoke

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/python -m ruff check src tests

fmt:
	.venv/bin/python -m ruff format src tests

clean:
	rm -rf .patchpilot .tmp .pytest_cache .ruff_cache
