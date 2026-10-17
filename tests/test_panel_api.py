"""验收项 7：Web 面板测试（离线，不需 Docker）。

用 FastAPI 的 TestClient 打真实 HTTP 请求（不是直接调函数），
这样"路由挂错/返回类型不对/JSON 结构变了"都能被发现。
另有一条**真起 uvicorn** 的用例，验证 `pp serve` 的命令行入口真的能服务。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="面板需要 server 依赖：pip install -e '.[server]'")

from fastapi.testclient import TestClient  # noqa: E402

from patchpilot.api.app import create_app  # noqa: E402


def _trace(
    trace_id: str = "abc123",
    *,
    passed: bool = True,
    task_id: str = "fix-001",
    cost: float = 0.000165,
    cost_unknown: bool = False,
    ended_at: str = "2026-10-17T01:00:00+00:00",
) -> dict:
    return {
        "trace_id": trace_id,
        "name": "repair",
        "status": "ok" if passed else "failed",
        "task_id": task_id,
        "passed": passed,
        "rounds": 1,
        "candidates": 2,
        "duration_ms": 3835.3,
        "prompt_tokens": 162,
        "completion_tokens": 235,
        "cost_usd": cost,
        "cost_unknown": cost_unknown,
        "models": ["deepseek-flash"],
        "failure_kind": "" if passed else "test_fail",
        "ended_at": ended_at,
        "spans": [
            {"name": "retrieval.search", "duration_ms": 28.9, "attributes": {"terms": 3}},
            {"name": "llm.generate", "duration_ms": 1557.8, "attributes": {"round": 1}},
            {"name": "tool.apply_patch", "duration_ms": 393.7, "attributes": {"round": 1}},
            {"name": "tool.run_tests", "duration_ms": 1855.0, "attributes": {"exit_code": 0}},
        ],
    }


def _write_traces(directory: Path, *records: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-10-17.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    return directory


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    traces = _write_traces(tmp_path / "traces", _trace())
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "dataset": "fast_corpus.json",
                "meta": {"temperature": 0.0, "model": "deepseek-flash"},
                "metrics": {
                    "total": 6,
                    "valid": 6,
                    "passed": 6,
                    "pass@1": 1.0,
                    "total_cost_usd": 0.0025,
                    "p95_latency_s": 11.3,
                },
                "outcomes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return TestClient(create_app(trace_dir=traces, eval_report=report))


# ---------------------------------------------------------------- 健康检查与首页


def test_health_reports_trace_count(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["traces"] == 1


def test_index_serves_the_panel_html(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # 面板必须真的能用：关键元素与取数逻辑都在
    assert "PatchPilot" in body
    assert "/api/traces" in body
    assert "span 时间线" in body


def test_panel_html_has_no_external_dependencies(client: TestClient) -> None:
    """零前端构建 + 离线可用：页面不得引用任何外部 CDN 资源。"""
    body = client.get("/").text

    assert "http://" not in body.replace("http://127.0.0.1:8000", "")
    assert "https://" not in body
    assert "cdn." not in body


# ---------------------------------------------------------------- 轨迹接口


def test_list_traces_returns_summaries(client: TestClient) -> None:
    payload = client.get("/api/traces").json()

    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["trace_id"] == "abc123"
    assert item["passed"] is True
    assert item["rounds"] == 1
    assert item["span_counts"]["tool.run_tests"] == 1
    assert "spans" not in item, "列表接口只返回摘要，不返回全部 span（省带宽）"


def test_get_trace_returns_full_spans(client: TestClient) -> None:
    payload = client.get("/api/traces/abc123").json()

    names = [span["name"] for span in payload["spans"]]
    assert "retrieval.search" in names
    assert "llm.generate" in names
    assert "tool.apply_patch" in names
    assert "tool.run_tests" in names
    assert payload["summary"]["cost_usd"] == pytest.approx(0.000165)


def test_unknown_trace_is_404(client: TestClient) -> None:
    assert client.get("/api/traces/nope").status_code == 404


def test_traces_are_ordered_newest_first(tmp_path: Path) -> None:
    traces = _write_traces(
        tmp_path / "traces",
        _trace("old", ended_at="2026-10-16T00:00:00+00:00"),
        _trace("new", ended_at="2026-10-17T00:00:00+00:00"),
    )
    client = TestClient(create_app(trace_dir=traces))

    items = client.get("/api/traces").json()["items"]

    assert [i["trace_id"] for i in items] == ["new", "old"]


def test_cost_unknown_is_surfaced(tmp_path: Path) -> None:
    """未定价模型必须让面板显式标出来——不能显示成"成本 0，一切正常"。"""
    traces = _write_traces(tmp_path / "traces", _trace(cost_unknown=True))
    client = TestClient(create_app(trace_dir=traces))

    item = client.get("/api/traces").json()["items"][0]

    assert item["cost_unknown"] is True
    assert "⚠成本不可信" in client.get("/").text


# ---------------------------------------------------------------- 容错


def test_missing_trace_dir_is_not_an_error(tmp_path: Path) -> None:
    """trace 目录不存在时应正常返回空列表（首次部署就是这种状态）。"""
    client = TestClient(create_app(trace_dir=tmp_path / "nope"))

    assert client.get("/api/health").json()["traces"] == 0
    assert client.get("/api/traces").json()["total"] == 0


def test_corrupt_jsonl_line_is_skipped(tmp_path: Path) -> None:
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "2026-10-17.jsonl").write_text(
        '{"trace_id": "good", "ended_at": "2026-10-17T00:00:00+00:00"}\n'
        "{ 这不是 JSON\n"
        "\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(trace_dir=traces))

    payload = client.get("/api/traces").json()

    assert payload["total"] == 1, "坏行被跳过，好行仍可用"
    assert payload["items"][0]["trace_id"] == "good"


def test_missing_eval_report_degrades_gracefully(tmp_path: Path) -> None:
    client = TestClient(create_app(trace_dir=tmp_path, eval_report=tmp_path / "nope.json"))

    payload = client.get("/api/evals").json()

    assert payload["available"] is False
    assert payload["metrics"] == {}


def test_eval_metrics_are_exposed(client: TestClient) -> None:
    payload = client.get("/api/evals").json()

    assert payload["available"] is True
    assert payload["metrics"]["pass@1"] == 1.0
    assert payload["meta"]["temperature"] == 0.0


# ---------------------------------------------------------------- 真起服务


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_pp_serve_actually_serves_http(tmp_path: Path) -> None:
    """真起 `pp serve` 子进程，用真 HTTP 请求验证命令行入口可服务。

    只测 TestClient 会漏掉"uvicorn 起不来 / 参数拼错"这类问题——
    验收项 7 要的是"浏览器能打开"，所以这里必须走真端口。
    """
    traces = _write_traces(tmp_path / "traces", _trace())
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "patchpilot.cli", "serve",
            "--host", "127.0.0.1", "--port", str(port),
            "--trace-dir", str(traces),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        import urllib.request

        deadline = time.time() + 40
        payload: dict | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310 - 本地固定地址
                    f"http://127.0.0.1:{port}/api/health", timeout=1
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception:  # noqa: BLE001 - 服务尚未就绪
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    pytest.fail(f"pp serve 提前退出：{output[-800:]}")
                time.sleep(0.3)
        assert payload is not None, "pp serve 未在 40s 内就绪"
        assert payload["ok"] is True
        assert payload["traces"] == 1

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:  # noqa: S310
            html = response.read().decode("utf-8")
        assert "PatchPilot" in html
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
