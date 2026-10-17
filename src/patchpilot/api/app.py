"""Web 面板 API（验收项 7：`docker compose up` 起面板，浏览器看轨迹回放）。

## 设计约束（刻意保持极简）

  - **零前端构建**：面板是单个内联 HTML+JS 页面，没有 npm / 打包步骤。
    "一键部署"若需要先装 node 跑 build，就不叫一键了。
  - **只读**：面板只读 trace 与评测报告，不提供任何执行入口——
    避免把"远程执行不可信代码"的能力暴露到 Web 上。
  - **两个数据源都来自文件**：`traces/*.jsonl`（`pp run` 产出）与
    `eval/report.json`（`pp eval` 产出）。因此面板本身无状态，重启不丢数据。

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 单页面板（轨迹列表 + 详情 + 评测指标） |
| GET | `/api/traces` | 轨迹列表（按时间倒序，含摘要） |
| GET | `/api/traces/{trace_id}` | 单条轨迹详情（含全部 span） |
| GET | `/api/evals` | 最近一次评测报告（若存在） |
| GET | `/api/health` | 健康检查（部署脚本用它判断就绪） |
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

MAX_TRACES = 500


def _read_traces(trace_dir: Path) -> list[dict[str, Any]]:
    """读全部 trace JSONL；坏行跳过（面板不应因一条脏数据整体崩掉）。"""
    records: list[dict[str, Any]] = []
    if not trace_dir.is_dir():
        return records
    for path in sorted(trace_dir.glob("*.jsonl"), reverse=True):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # 最近的在最前（trace 里有 ended_at 就用它排，否则靠文件顺序倒推）
    records.sort(key=lambda r: str(r.get("ended_at") or ""), reverse=True)
    return records[:MAX_TRACES]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    spans = record.get("spans") or []
    by_name: dict[str, int] = {}
    for span in spans:
        name = str(span.get("name", ""))
        by_name[name] = by_name.get(name, 0) + 1
    return {
        "trace_id": record.get("trace_id", ""),
        "name": record.get("name", ""),
        "status": record.get("status", ""),
        "task_id": record.get("task_id", ""),
        "passed": bool(record.get("passed")),
        "rounds": record.get("rounds", 0),
        "candidates": record.get("candidates", 0),
        "duration_ms": record.get("duration_ms", 0.0),
        "cost_usd": record.get("cost_usd", 0.0),
        "cost_unknown": bool(record.get("cost_unknown")),
        "prompt_tokens": record.get("prompt_tokens", 0),
        "completion_tokens": record.get("completion_tokens", 0),
        "models": record.get("models") or [],
        "span_counts": by_name,
        "ended_at": record.get("ended_at", ""),
    }


def create_app(*, trace_dir: Path, eval_report: Path | None = None) -> FastAPI:
    """构造应用。`trace_dir` / `eval_report` 由调用方注入（便于测试与部署）。"""
    app = FastAPI(
        title="PatchPilot 面板",
        description="只读面板：任务轨迹回放 + 评测指标",
        version="0.1.0",
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "trace_dir": str(trace_dir),
            "trace_dir_exists": trace_dir.is_dir(),
            "traces": len(_read_traces(trace_dir)),
        }

    @app.get("/api/traces")
    def list_traces() -> dict[str, Any]:
        records = _read_traces(trace_dir)
        return {"total": len(records), "items": [_summary(r) for r in records]}

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str) -> JSONResponse:
        for record in _read_traces(trace_dir):
            if record.get("trace_id") == trace_id:
                return JSONResponse({**record, "summary": _summary(record)})
        raise HTTPException(status_code=404, detail=f"没有这条 trace: {trace_id}")

    @app.get("/api/evals")
    def get_eval() -> dict[str, Any]:
        target = eval_report
        if target is None or not target.is_file():
            return {"available": False, "metrics": {}, "outcomes": []}
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"评测报告损坏: {exc}") from exc
        return {
            "available": True,
            "dataset": payload.get("dataset", ""),
            "meta": payload.get("meta", {}),
            "metrics": payload.get("metrics", {}),
            "outcomes": payload.get("outcomes", []),
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PANEL_HTML

    return app


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>PatchPilot 轨迹回放</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --line:#252a34; --fg:#e6e8ee; --dim:#8b93a7;
          --ok:#3fb950; --bad:#f85149; --warn:#d29922; --accent:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:18px 22px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:14px; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0; font-weight:600; }
  .muted { color:var(--dim); font-size:12px; }
  main { padding:18px 22px; display:grid; grid-template-columns: 340px 1fr; gap:18px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .list { max-height: 70vh; overflow:auto; }
  .item { padding:9px 10px; border-radius:8px; cursor:pointer; border:1px solid transparent; }
  .item:hover { background:#1d222b; }
  .item.active { border-color:var(--accent); background:#1b2430; }
  .row { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .tag { font-size:11px; padding:1px 7px; border-radius:999px; border:1px solid var(--line); color:var(--dim); }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  table { width:100%; border-collapse:collapse; }
  td, th { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); font-size:13px; }
  th { color:var(--dim); font-weight:500; }
  .spans { margin-top:12px; }
  .bar { height:6px; background:#2d3440; border-radius:4px; overflow:hidden; margin-top:3px; }
  .bar > i { display:block; height:100%; background:var(--accent); }
  code { font-size:12px; color:#c9d1d9; }
  .empty { color:var(--dim); padding:20px 4px; }
</style>
</head>
<body>
<header>
  <h1>PatchPilot 轨迹回放</h1>
  <span class="muted" id="hint">只读面板 · 数据来自 trace JSONL 与评测报告</span>
</header>
<main>
  <section class="card">
    <div class="row"><strong>任务轨迹</strong><span class="muted" id="count">…</span></div>
    <div class="list" id="list"><div class="empty">加载中…</div></div>
    <hr style="border:none;border-top:1px solid var(--line);margin:14px 0">
    <div class="row"><strong>评测指标</strong><span class="muted">最近一次 pp eval</span></div>
    <div id="eval"><div class="empty">无评测报告</div></div>
  </section>
  <section class="card" id="detail"><div class="empty">从左侧选择一条轨迹</div></section>
</main>
<script>
const fmtMs = v => (v >= 1000 ? (v/1000).toFixed(2)+"s" : v.toFixed(1)+"ms");
const fmtUsd = v => "$" + Number(v).toFixed(6);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function loadTraces() {
  const res = await fetch("/api/traces");
  const data = await res.json();
  document.getElementById("count").textContent = data.total + " 条";
  const list = document.getElementById("list");
  if (!data.items.length) { list.innerHTML = '<div class="empty">还没有 trace。先跑一次 <code>pp run</code>。</div>'; return; }
  list.innerHTML = data.items.map(t => `
    <div class="item" data-id="${esc(t.trace_id)}">
      <div class="row">
        <span class="${t.passed ? 'ok' : 'bad'}">${t.passed ? '✓ 通过' : '✗ 未通过'}</span>
        <span class="tag">${esc(t.task_id || t.trace_id)}</span>
      </div>
      <div class="muted">${t.rounds} 轮 · ${fmtMs(t.duration_ms)} · ${fmtUsd(t.cost_usd)}${t.cost_unknown ? ' ⚠成本不可信' : ''}</div>
      <div class="muted">${Object.entries(t.span_counts).map(([k,v]) => k+"×"+v).join(" · ") || "无 span"}</div>
    </div>`).join("");
  list.querySelectorAll(".item").forEach(el => el.onclick = () => {
    list.querySelectorAll(".item").forEach(x => x.classList.remove("active"));
    el.classList.add("active");
    loadTrace(el.dataset.id);
  });
}

async function loadTrace(id) {
  const res = await fetch("/api/traces/" + encodeURIComponent(id));
  const t = await res.json();
  const spans = t.spans || [];
  const total = spans.reduce((a, s) => a + (s.duration_ms || 0), 0) || 1;
  const rows = spans.map(s => {
    const pct = Math.max(2, ((s.duration_ms || 0) / total) * 100);
    return `<tr>
      <td><code>${esc(s.name)}</code></td>
      <td style="width:34%"><div class="bar"><i style="width:${pct}%"></i></div></td>
      <td style="text-align:right;white-space:nowrap">${fmtMs(s.duration_ms || 0)}</td>
      <td><code>${esc(JSON.stringify(s.attributes || {}))}</code></td>
    </tr>`;
  }).join("");
  document.getElementById("detail").innerHTML = `
    <div class="row">
      <strong>${esc(t.task_id || t.trace_id)}</strong>
      <span class="${t.passed ? 'ok' : 'bad'}">${t.passed ? '通过' : '未通过'}</span>
    </div>
    <table style="margin-top:10px">
      <tr><th>trace</th><td><code>${esc(t.trace_id)}</code></td></tr>
      <tr><th>轮数</th><td>${t.rounds ?? 0}</td></tr>
      <tr><th>候选文件</th><td>${t.candidates ?? 0}</td></tr>
      <tr><th>总耗时</th><td>${fmtMs(t.duration_ms || 0)}</td></tr>
      <tr><th>token</th><td>${t.prompt_tokens ?? 0} + ${t.completion_tokens ?? 0}</td></tr>
      <tr><th>成本</th><td>${fmtUsd(t.cost_usd || 0)}${t.cost_unknown ? ' <span class="warn">⚠ 含未定价模型</span>' : ''}</td></tr>
      <tr><th>模型</th><td>${esc((t.models || []).join(", ") || "—")}</td></tr>
      <tr><th>失败归因</th><td>${esc(t.failure_kind || "—")}</td></tr>
    </table>
    <div class="spans">
      <div class="row"><strong>span 时间线</strong><span class="muted">${spans.length} 个</span></div>
      <table><thead><tr><th>span</th><th>占比</th><th>耗时</th><th>属性</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="muted">无 span</td></tr>'}</tbody></table>
    </div>`;
}

async function loadEval() {
  const res = await fetch("/api/evals");
  const d = await res.json();
  const box = document.getElementById("eval");
  if (!d.available) { box.innerHTML = '<div class="empty">无评测报告（跑 <code>pp eval</code>）</div>'; return; }
  const m = d.metrics || {};
  box.innerHTML = `
    <table style="margin-top:8px">
      <tr><th>pass@1</th><td class="ok">${(100*(m["pass@1"]||0)).toFixed(1)}%</td></tr>
      <tr><th>通过/有效</th><td>${m.passed ?? 0} / ${m.valid ?? 0}</td></tr>
      <tr><th>总成本</th><td>${fmtUsd(m.total_cost_usd || 0)}</td></tr>
      <tr><th>P95 耗时</th><td>${m.p95_latency_s ?? 0}s</td></tr>
      <tr><th>温度/模型</th><td>${esc(d.meta?.temperature)} / ${esc(d.meta?.model || "—")}</td></tr>
    </table>`;
}

loadTraces(); loadEval();
</script>
</body>
</html>
"""


__all__ = ["PANEL_HTML", "create_app"]
