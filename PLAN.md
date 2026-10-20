# PatchPilot 项目计划与变更日志

> **本文件是 PatchPilot 唯一的计划与变更记录（single source of truth）。**
> 任何代码、配置、文档的改动完成后，必须在第 9 节追加一条日志；
> 任何上传（git commit / push）之前，必须完成第 8 节的检查门禁。

---

## 0. 维护规则

**复审与提交节拍（2026-10-16 调整，按用户要求）**：`code-reviewer` 复审与 git 提交
**不再逐批进行**，改为**每完成 §1 的 9 项验收项之一**时执行一次；期间若发现 bug，
在该节拍内具体修改即可。

理由与代价（如实登记）：第九至二十二轮的"每批必审"产生了极高密度的对抗性审查
（单轮最多 31 个变异、十几项阻塞级修复），安全性确实被压实，但**严重挤占了功能开发**——
P1 的驱动层与 MCP server 在二十余轮复审期间没有任何进展。
因此改为"**按验收项粒度**"：一个验收项真正做完（含真机验证）→ 一次复审 → 一次提交。
风险是验收项内部的中间状态会缺少逐批审查，故要求：
每个验收项**完成时**必须附**自证材料**（变异自证 / 真机证据 / 范围自证），
不满足 §8.5 的部分不得计入完成声明。

| 规则 | 内容 |
|---|---|
| R1 | 每次改动完成后，在「变更日志」追加条目，不得事后补写或批量补写 |
| R2 | 日志必须包含四要素：变更内容、涉及文件、验证命令与结果、提交 hash |
| R3 | 阶段状态变化（未开始 → 进行中 → 已完成）必须同步更新第 5 节表格 |
| R4 | 上传前必须通过 `pp check`（确定性门禁）+ code-reviewer 子代理审查（语义门禁） |
| R5 | 未通过审查的代码一律不得 commit / push；NDA 与密钥问题优先级最高 |

---

## 1. 项目定位与验收标准

**一句话**：一个能在隔离沙箱里自主读代码、复现 bug、改代码、跑测试自验证的代码修复 Agent，配一套可回归、可归因的评测体系。

**目标岗位**：国内大厂 Agent 开发 / 算法岗

| # | 验收项（Definition of Done） | 验证方式 | 状态 |
|---|---|---|---|
| 1 | CLI 一条命令在任意本地仓库上完成一次修复 | `pp run --repo . --issue-file bug.md` | ✅ **已完成（真机验证）**：8 节点修复闭环（`graph/repair.py`）+ `pp run`；**真机端到端（DeepSeek 真模型 + Docker 沙箱）完成一次修复并通过测试**；同一命令在 6 道自建题上全部 1 轮通过（见第 3 项） |
| 2 | 沙箱安全边界成立 | 逃逸 / 超时 / 资源耗尽 / 恶意代码 4 类用例通过 | ✅ **已完成**：驱动层 `manager.py`（沙箱内跑 pytest → JUnit 结构化返回）＋ **4 个 MCP server**（stdio 协议层已验证）＋ 自建镜像；**真机端到端 15/15**（网络不可达 / rootfs 只读 / 非 root / 内存 OOM 熔断 / pids 超限 fork 被拒 / 超时截断且无残留）；逃逸面经六轮真机复审逐步收口（search 剪链接 / `git apply` 写穿 → **以 git 清单为准 + fail-closed**；再修 **stdin 编码侧**：补丁按字节喂 git，杜绝 CRLF 注入），变异自证 **13/13 KILLED**（2 条等价变异已登记）；**535 项单测在 cp936 与 UTF-8 两种模式下均全绿** |
| 3 | 端到端解析率有数字 | `pp eval --subset lite50` 输出 pass@1 + 成本 + P95 | ✅ **已有数字（自建 FastEval 6 题，勿与 SWE-bench 混谈）**：`pass@1 = 6/6 = 100%`、总成本 **\$0.0025**、总 token 5,865、平均 **8.9s**、**P95 11.3s**（`deepseek-flash`、温度 0、镜像 `patchpilot-sandbox:0.1.0-py312`）；**未做**：BenchEval（SWE-bench Lite 50 题）——那是论文级数字，需要外部数据集与更长预算 |
| 4 | 评测可复现 | 公布题目 ID、温度、commit、镜像 tag，第三方可复跑 | ✅ **已完成**：报告 JSON 固定写入 `task_ids` / `temperature=0.0` / `commit` / `image` / `python` / `max_rounds`；评测集以**数据**入库（`eval/fast_corpus.json`，含每题的源码、测试与 issue 全文），基线入库（`eval/baseline.json`），`pp eval` 一条命令即可复跑并对比 |
| 5 | 回归门禁生效 | 故意改坏 prompt → CI 变红并给出指标 diff | ✅ **已完成（真机验证）**：`pp eval --baseline eval/baseline.json` 与提交在库里的基线比，`pass@1` 退化即**退出码 2** 并打印逐题 diff；`.github/workflows/ci.yml` 两道门禁（`pp check`+单测必过、评测回归需密钥否则显式跳过）。**实测**：未改动 → 退出码 0／"无回归"；把 prompt 改成"不要输出任何 diff"→ **pass@1 100% → 50%**、退出码 **2**、点名 `fast-003-wrong-exception` 等退化题 |
| 6 | 全链路可观测 | 每个任务有完整 trace（LLM / 工具 / 检索 / 成本） | ✅ **已完成**：修复闭环接入 `Tracer`，四类 span 齐备（`retrieval.search` / `llm.generate` / `tool.apply_patch` / `tool.run_tests`）+ token 与成本；`pp trace --limit N --verbose` 可回放。真机 trace 实测：检索 28.9ms、LLM 1557.8ms、git 393.7ms、沙箱 1855.0ms，成本 \$0.000165；未定价模型**显式标 `cost_unknown`** 而非记 0 |
| 7 | 一键部署 | `docker compose up` 起 Web 面板，浏览器看轨迹回放 | ✅ **已完成（真机验证）**：`pp serve` 起只读面板（`/` 单页 + `/api/traces`、`/api/traces/{id}`、`/api/evals`、`/api/health`）；`docker-compose.yml` 增加 `panel` 服务（`docker/app/Dockerfile`，**零前端构建**、trace 与评测产物只读挂载、带 HEALTHCHECK）。**实测**：镜像构建成功（237MB）、容器起来后读到宿主的真实 trace（`{"traces":1}`）、首页含轨迹与 span 时间线、`/api/evals` 返回 pass@1 指标、`docker compose config` 校验通过（4 个服务） |
| 8 | PC 可用 | Windows/macOS 上安装后本地可跑 | ✅ **已完成**（conda 环境 + `pp` 命令） |
| 9 | 上传前门禁可自证范围 | 门禁输出的 pass 不得来自"被关掉的测试/未暂存的字节/没扫到的编码" | ✅ **已完成**（第八轮：三项阻塞修复 + 8 条判决性回归用例；`pp check` 10 项全绿） |

---

## 2. 架构与关键工程决策

```
接口层   CLI (Typer)  │  Web 面板 (Next.js)  │  GitHub App
编排层   LangGraph 状态机 + Postgres Checkpointer（断点续跑 / 人工接管 / 三重预算 / 失败记忆）
工具层   MCP Servers:  fs │ git │ test │ search
检索层   tree-sitter AST 索引 → 符号BM25 + 向量 + 调用图扩散 → rerank
执行层   Docker 沙箱（每任务一容器 / 无网络 / 非root / 资源限额）
评测层   FastEval(20题/分钟级) + BenchEval(50题/小时级) + 失败分类器 + 消融矩阵
观测层   OTel GenAI 语义约定 → Langfuse + Prometheus
存储     Postgres(任务/轨迹/checkpoint) · Redis(队列/锁) · 卷(仓库快照)
```

状态机：`ingest → localize → reproduce → plan → edit → verify →（失败）reflect → 回环` → `finalize`

| 决策 | 选择 | 理由 |
|---|---|---|
| 上下文怎么给 | 符号级检索，不整文件塞 | token 成本 × 定位精度 |
| 修改怎么表达 | unified diff + 工具校验 | 省 token、可校验、可回滚 |
| 失败怎么处理 | 回环 + 失败记忆（记录已试过的文件/思路） | 防止原地打转烧钱 |
| 预算怎么控 | token / 金额 / 时长 / 轮数 四重硬上限 | 生产必须有熔断 |
| 沙箱怎么隔离 | 无网络 + 非 root + 只读 rootfs + tmpfs 工作区 | 默认拒绝，gVisor 作为进阶开关 |
| 模型怎么用 | 分级：小模型做定位/摘要，大模型做编辑 | 成本优化是硬指标 |
| 检查怎么信 | 确定性门禁（脚本） + 语义门禁（子代理）双轨 | 脚本可复现，代理能理解意图 |

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言/工程 | Python 3.12（conda 环境 `patchpilot`）、pip + PEP 621、ruff、pytest、pydantic-settings |
| 编排 | LangGraph（StateGraph + Checkpointer）、Pydantic v2 全量 IO Schema |
| 模型 | DeepSeek / Qwen（OpenAI 兼容）、Structured Output；**不部署本地模型** |
| 工具协议 | MCP 官方 Python SDK（stdio + streamable HTTP） |
| 代码理解 | tree-sitter + tree-sitter-language-pack（先做 Python） |
| 检索 | rank_bm25 / Tantivy、Qdrant、bge-small-zh / bge-m3、bge-reranker-base |
| 沙箱 | Docker SDK for Python，自建多语言镜像；进阶 gVisor |
| 存储 | Postgres、Redis、本地卷 |
| 观测 | OpenTelemetry + Langfuse、structlog、Prometheus |
| 评测 | 自研 Harness（对齐 SWE-bench 数据格式）+ 官方 harness 交叉验证 |
| 服务 | FastAPI + SSE、ARQ/Celery |
| 前端 | Next.js 15 + TS + Tailwind + shadcn/ui |
| 部署 | Docker Compose、GitHub Actions、可选 K8s |

---

## 4. 目录结构

```
D:\Code\PatchPilot\
├─ PLAN.md                  本文件：计划 + 变更日志 + 协作协议
├─ README.md                项目门面：简介 / 快速开始 / 指标 / 指向 PLAN.md
├─ agents\                  子代理角色定义（可复用、可版本化）
│  ├─ code-reviewer.md      上传前语义审查，只读，有否决权
│  └─ git-publisher.md      提交与推送，必须持审查通过令牌
├─ src\patchpilot\
│  ├─ core\                 config / logging / models / budget / pricing / llm / preflight
│  ├─ graph\                checkpointer + minimal 状态机（P3 扩展为 8 节点全图）
│  ├─ observability\        tracing（span + 用量 + JSONL）
│  ├─ sandbox\  index\  eval\     P1 / P2 / P4 实现位
│  └─ cli.py                pp doctor / dev-up / dev-down / smoke / check
├─ tests\                   单元测试
├─ .github\workflows\ci.yml
└─ docker-compose.yml       postgres + redis + qdrant
```

---

## 5. 阶段计划与状态

| 阶段 | 内容 | 完成标志 | 状态 |
|---|---|---|---|
| **P0** | 工程骨架：工程配置、compose、状态机、checkpointer、CLI、CI、测试 | `pp smoke` 通过 + 全部单测通过 + `code-reviewer` 出具 APPROVE | ✅ **已完成并发布**：`0ad9ea8` 已推送 `origin/main`（第八轮 APPROVE，摘要 `ab1ce173…`）；4 条非阻塞项见最新日志 |
| **P1** | 沙箱与工具：Docker 隔离、4 个 MCP Server、安全测试 | 沙箱跑 pytest 结构化返回；4 类安全用例全绿 | ✅ **已完成（验收项 2）**：驱动层 `manager.py`（沙箱内跑 pytest → JUnit 结构化返回）+ **4 个 MCP server**（fs/git/test/search，stdio 协议层已验证）+ 自建镜像 `patchpilot-sandbox:0.1.0-py312`；真机 Docker 端到端 **15/15**（网络/只读/非 root/内存 OOM/pids 熔断/超时清理） |
| **P2** | 代码索引与检索：符号表、三路召回、rerank | 定位集 Top-5 命中率有基线与消融表 | ✅ **已完成**：`index/` 符号索引（`ast` + 非 Python 近似规则）+ BM25 + 调用图 + RRF 融合 + 可解释重排；`pp locate` 一条命令出**消融表**。**历史题集（10 题）Top-1：基线 30% → P2 完整 80%**，Top-5 90%，MRR 0.867；语料题集（6 题）已达天花板 100%。已接入修复闭环 `locate` |
| **P3** | 修复闭环：8 节点全图、真实模型、预算熔断 | 10 个真实 issue 端到端跑通 ≥3 个 | 🔧 **进行中**：8 节点图（`graph/repair.py`）+ `pp run` 已落地，**真机端到端（DeepSeek 真模型 + Docker 沙箱）跑通 1 例**；`openai` 已装、Key 已验活；待做：10 个真实 issue 的批量跑通与统计（属 P4 评测） |
| **P4** | 评测闭环：两级评测集、指标、失败分类、报告 | `pp eval` 一条命令出报告；≥3 组策略对比 | 未开始 |
| **P5** | 观测与 CI 门禁：OTel/Langfuse、指标回归门禁 | 改坏 prompt → CI 变红并指出指标 | 未开始 |
| **P6** | Web 面板与部署：轨迹回放、评测看板、一键 compose | 公网可访问 demo；PC 上 3 条命令跑通 | 未开始 |
| **P7** | 打磨产出：成本优化、ADR、评测报告、英文 README、视频 | 仓库可开源，README 首屏即指标 | 未开始 |

**最小可交付内核** = P1 + P3 + P4。剪裁线：P6 可砍，P4 绝不砍。

---

## 6. 评测体系要点

| | FastEval | BenchEval |
|---|---|---|
| 规模 | 自建 20 题小仓库 | SWE-bench Lite 采样 50 题 |
| 单题耗时 | < 60 秒 | 5–15 分钟 |
| 触发时机 | **每次 commit / 改 prompt** | 每周 / 里程碑 |
| 用途 | 开发迭代 + CI 门禁 | 对外可信数字 |

指标：pass@1、Top-5 文件命中率、符号命中率、首次修复率、平均轮数/token/成本、P95 耗时、失败原因分布。
统计纪律：温度 0、固定种子、固定 base commit、公布题目 ID、镜像 tag 锁定。
**子集绝对数字不可比，报告必须同时给"相对基线的提升"与消融结果。**

---

## 7. 风险与对策

| 风险 | 对策 | 现状 |
|---|---|---|
| Docker 守护进程不可用 | 启动 Docker Desktop；smoke/单测/门禁均不依赖 Docker | ✅ 已解除：本会话实测 `docker info` 返回 `ServerVersion=29.5.2`，受限模式限制也已取消，P1 可开工 |
| 沙箱无网络，装包/拉镜像需授权 | 写盘与联网操作按批次合并，一次授权覆盖多个动作 | ⚠️ 持续影响 |
| 沙箱内 git 走 schannel 后端握手必然失败 | 推送显式使用 openssl 后端；已写入仓库本地配置 | ✅ 已定位并规避 |
| SWE-bench 环境构建慢且坑多 | 用官方预构建环境，选题优先 env 易装的 | 待 P4 |
| 成本失控 | 预算熔断 + 结果缓存 + 模型分级 + 并发上限 | 熔断已接入编排层并有终止测试；其余待 P3 |
| 评测噪声 | 温度 0、固定种子、关键结论跑 3 次取均值 | 待 P4 |

### 7.1 环境事实（已实测）

| 事实 | 值 | 来源 |
|---|---|---|
| 项目根 | `D:\Code\PatchPilot` | 用户指定 |
| Python | Anaconda 独立环境 `patchpilot`，3.12.15，`D:\develop\anaconda3\envs\patchpilot\python.exe` | 主 Agent |
| Docker | CLI 29.5.2 / compose 5.1.4；**守护进程本会话实测可用**（`docker info` → `ServerVersion=29.5.2`）；真实沙箱验证见 `.tmp/p1_staging/verify_docker_sandbox.py` | `pp doctor` / 主 Agent |
| 已装依赖 | `langgraph 1.2.15`、`langgraph-checkpoint{-sqlite 3.1.2, -postgres 3.2.0}`、`psycopg 3.3.6`、`pytest 9.1.1`、`ruff 0.17.0` | 主 Agent（`pip list`） |
| **未装依赖** | `openai`（P3 真实模型）、`qdrant-client` / `rank_bm25` / `tree-sitter`（P2）、`fastapi` / `uvicorn`（P6） | 主 Agent（`find_spec`） |
| git 身份 | `Asqion <2285694559@qq.com>`，来自 `C:/Users/22856/.gitconfig` | git-publisher |
| git 代理 | `http.proxy=http://127.0.0.1:7897`（本地代理运行时 GitHub / Gitee 均可达） | git-publisher |
| **TLS 陷阱** | 默认 `schannel` 后端报 `SEC_E_NO_CREDENTIALS (0x8009030e)`；改用 `openssl` 后 `ls-remote` 对 github（5393 refs）与 gitee（2033 refs）均 exit=0 | git-publisher |
| 凭据助手 | `credential.helper=manager`（GCM）；非交互沙箱内首次推送可能仍需用户在本机终端完成认证 | git-publisher |
| 会话沙箱 | **已解除**：本会话为 `danger-full-access`，Docker/命名管道/工作区外写盘均不再受限 | 主 Agent |

---

## 8. 协作流程：变更 → 检查 → 上传

### 8.1 角色

| 角色 | 职责 | 权限边界 |
|---|---|---|
| 主 Agent（开发） | 写代码、跑验证、更新本文件日志 | 不得跳过门禁直接上传 |
| `code-reviewer` 子代理 | 独立审查代码，出具 APPROVE / REJECT | **只读**，禁止修改任何文件 |
| `git-publisher` 子代理 | 提交、打标签、推送 | **必须持 APPROVE 令牌**，否则拒绝执行 |

### 8.2 门禁协议（硬性）

```
改动完成
   │
   ├─▶ 步骤1  主 Agent 运行 pp check（确定性门禁）
   │          语法 / ruff / pytest / 密钥扫描 / .gitignore 覆盖
   │          输出 TREE_DIGEST：对「将被提交的全部字节」的内容摘要
   │          任一 fail ──▶ 必须修复后重跑，禁止进入步骤2
   │
   ├─▶ 步骤2  调用 code-reviewer 子代理（语义门禁）
   │          传入：TREE_DIGEST + pp check JSON + 变更摘要
   │          输出：VERDICT: APPROVE | REJECT，且必须原样引用 TREE_DIGEST
   │          REJECT ──▶ 修复后从步骤1重来（摘要随之变化）
   │
   └─▶ 步骤3  调用 git-publisher 子代理
              必须同时提供：APPROVE 结论 + TREE_DIGEST
              提交前复算 pp check --json 的 tree_digest 并与 APPROVE 中的值逐字符比对
              缺失或不一致 ──▶ 拒绝上传并回报 BLOCKED
```

> **为什么需要 TREE_DIGEST**：git-publisher 首轮核查指出"同一范围出现两份文字描述，
> 审查者无法界定 scope"。摘要把"批准的是哪一份字节"从**文字描述**变成**可校验的值**——
> 任何一处字节变动都会使批准自动失效。

### 8.3 上传流程细则

1. **顺序固定**：先改代码 → 再写完本文件第 9 节变更日志（含验证证据）→ 然后 `git add -A`
   → 最后才运行 `pp check --json` 取 `tree_digest` → 交由 code-reviewer 审查 → 发布。
   **本文件本身在摘要范围内，所以"先取摘要再补日志"必然自我作废**；
   而 `worktree_matches_index` 要求摘要集合里每个文件都在索引中，
   因此**取摘要必须发生在 `git add` 之后**（否则该项直接 fail）。
2. **取摘要之后到推送之前**，审查范围内不得再有任何字节改动——`git add` 会更新索引但不改工作区，
   所以"审查通过后重新暂存"是安全的；"审查通过后改文件"则必然使批准失效。
3. `git-publisher` 执行 `git status --porcelain` + `git diff --stat` 确认范围。
4. 提交信息采用 Conventional Commits（`feat:` / `fix:` / `chore:` / `docs:` / `test:`）。
5. 推送前确认远端可达；若网络受限，输出可复制的推送命令交由用户在本机执行。
6. 回报格式：`STATUS / COMMIT / BRANCH / REMOTE / REVIEW / MANUAL_COMMAND`。
7. 禁止事项：`push --force`、`reset --hard`、`clean -fd`（除用户逐条确认外）；禁止提交 `.env`、`.patchpilot/`、`.conda/`、`__pycache__/`。

> **自指约束**：本文件在摘要范围内。因此 commit hash **不得回填**到已批准的日志条目
> ——只能记入**下一条**日志。任何字节变动（包括"给旧条目补一个 hash"）都会使
> `TREE_DIGEST` 失效，批准随之作废。

### 8.4 角色定义位置

- `agents/code-reviewer.md`
- `agents/git-publisher.md`

### 8.5 安全代码的"变异自证"规程（第十六轮后新增，强制）

**背景**：第十三至十六轮连续四轮 REJECT，全部是**同一类缺陷的不同形态**——
"声称已修复/已覆盖，但验证手段本身有洞"：

| 轮次 | 形态 | 具体表现 |
|---|---|---|
| 13 | 字段层**字面量表** | `memory` 完全没校验，`"0"` 静默取消内存上限 |
| 14 | 不变量层**字面量表** | 零值黑名单漏掉 `0G`/`00`/`0000`/`0B` |
| 15 | 用例**文案未锚定** | `match="…--memory"` 被 `--memory-swap` 前缀代偿 → 假绿 |
| 16 | **参数表与被测对象同源** | 参数化用 `[flag for flag, _ in REQUIRED_INVARIANTS]`，删清单条目时用例**消失**而非变红 |

**因此立规（对 `src/` 下的每一条安全校验/守卫/不变量都适用）**：

1. **每个守卫都必须配一条"变异自证"**：把该守卫改成恒假（或删除该分支），
   对应用例**必须变红**。没做这个实验就不得声称"已覆盖"。
2. **禁止"用枚举对抗等价类"**：判断一个取值集合是否安全时，必须写成**解析/判定函数**
   （如 `_parse_size_bytes`），不得写 `in {"0","0b",...}` 这类字面量表。
3. **禁止"参数表与被测对象同源"**：参数化用例的表必须是测试文件里**独立钉住**的字面表；
   从被测常量/清单反推参数会让"删掉清单条目"表现为"用例消失"。
   对清单类不变量，另配一条"清单恰好等于字面表"的双向断言。
   **（第二十轮加严）** 单有"字面表"还不够：若同一事实存在**多份可写副本**，
   "一起改坏"就能全绿。因此清单类断言必须同时具备三个条件：
   (a) 一条**谁也不派生**的硬编码锚点（连"由唯一源派生"都不行——唯一源被改小同样沉默）；
   (b) 锚点与实现清单的**双向**断言；
   (c) 锚点本身还被**独立事实**钉住（如 `len(REQUIRED_INVARIANTS) == 8`）。
   判据是"配套删除后套件必须变红"，而不是"我加了一张表"。
   **（第二十一轮补充：构造性上限的如实登记）** 即使 (a)(b)(c) 三条齐备，
   仍存在"把锚点、实现清单、测试源、条数事实**一起**改"的协同变异可全绿。
   这不是可以靠再加一层表解决的问题（会无限递归），只能**如实登记**：
   当前可达空间是"必须同时改 4 处、跨 2 个文件、3 类断言"，且每轮把该空间
   压小后必须在日志里写明**剩余上限**，不得声称"已构造性封闭"。
   **（第二十二轮实测收窄）** 4 处协同实际只对 **2/8** 个不变量仍然有效
   （`--cap-drop`、`--security-opt`）；其余 6 项被"完整 argv 负例 + `args.index(flag)`"
   杀掉——如实登记上限本身也是本规程的要求。
4. **文案断言必须锚定**：涉及"缺失/拒绝原因"的 `match=` 一律加 `$`，防止被同类错误信息前缀代偿。
5. **不可达的守卫要如实登记**：若某分支在构造上不可达（行为冗余），
   必须在注释里写明"这是冗余纵深防御、不被任何用例钉住"，**不得**计入覆盖率声明。
6. **变异实验必须跑完整套件、不得用 `-x`**，且在**副本**里显式设
   `PYTHONPATH=<副本>/src`——conda 的 editable `.pth` 会把真源码钉进 `sys.path`，
   否则实验会静默测到真源码（审查者第二次踩到）。
7. 提交前自查：本轮**新增/修改**的每条守卫，逐条做一次变异自证并记录结果；
   日志里写"补杀了 Mx"时必须附**实验命令与判据**，而不是断言。

---

## 9. 变更日志

格式：`### [日期] <阶段> <标题>` + 变更 / 文件 / 验证 / 审查 / 提交 五项。

### [2026-10-17] **P2 完成**：代码索引 + 三路召回 + 定位消融表

- **新增模块**（全部纯标准库，门禁无需网络/Docker/模型）：
  1. `index/symbols.py` —— 符号索引：Python 用 `ast`（精确行号、方法带 parent、
     收集调用名）；非 Python 用**近似正则**并**如实标注**"不是完整语法树"；
     索引跳过重目录与链接类对象（与 search 同口径），且**确定性**（排序后遍历）。
  2. `index/lexical.py` —— BM25（`k1=1.5, b=0.75`，**未调参**，已注明）+
     **标识符拆分**（`load_config` → `load`/`config`，`ConfigError` → `config`/`error`，
     同时保留整词），中文按整段保留（无分词器时比乱切稳）。
  3. `index/retrieval.py` —— 三路召回 + **RRF 融合**（`k=60`）+ **可解释重排**
     （符号名完全一致加权 / 查询词命中路径加权 / issue 未提测试时测试文件降权）。
     选 RRF 而非分数加权：三路量纲完全不同（BM25 任意正数、符号是计数、图是跳数），
     直接相加等于让量纲最大的那一路说话。
  4. `eval/locate_bench.py` —— 定位基准：两组题目 + 指标 + **6 组消融配置**。
- **接入闭环**：`RepairOptions.use_index_localizer`（默认 True）；`locate` 节点先走索引，
  返回空时回退到 P1 的符号+子串路径（保证行为可对比、可消融）。
- **真实数字**（`pp locate` / `eval/locate.json`）：

  | 配置 | 历史组 Top-1 | 历史组 Top-5 | MRR |
  |---|---|---|---|
  | **基线（P1 现有 locate：符号+子串）** | **30%** | **30%** | 0.300 |
  | 仅符号通道 | 70% | 70% | 0.700 |
  | 仅词法 BM25 | **80%** | **100%** | 0.858 |
  | 符号+词法（无图无重排） | 80% | 90% | 0.842 |
  | 符号+词法+图（无重排） | 70% | 90% | 0.792 |
  | **三路+重排（P2 完整）** | **80%** | 90% | 0.867 |

  语料组（6 题）所有配置都是 100% —— **天花板效应**，这组题里 issue 直接点名了函数。
- **如实登记的限制与负面发现**：
  1. **样本很小**：历史组 10 题、语料组 6 题。历史组里**1 题 = 10 个百分点**，
     所以除"基线 30% → 80%"这个大幅提升外，其余配置间差异**都在噪声内**，不应据此调参。
  2. **history 组信息偏乐观**：提交信息常直接点名函数（如"补丁按字节喂 git"），
     比真实用户报告好定位；故两组指标**分开报告，不混成一个数**。
  3. **图通道在这 10 题上对 Top-5 有轻微稀释**：BM25 单独 100%，完整配置 90%。
     原因是 RRF 的已知行为——新增通道会给大量候选加小分，把原本排第 5 的正确答案挤到第 6。
     样本太小不足以判定这是普遍现象，**未据此调参**（在 N=10 上调参等于过拟合），
     登记为待更大基准验证。
  4. **纯中文 issue 定位不到**（分词器不做翻译，代码里也没有中文标识符）：
     已写用例 `test_localizing_a_pure_chinese_issue_is_a_known_limitation` 显式钉住，
     避免被误当成已支持。真实用户报告通常带英文报错/标识符，影响有限。
- **第一版踩的坑（记录以免复发）**：基准初始实现里，索引路径是"相对索引根"、
  而 `git show --name-only` 给的是"相对 git 根"，两者直接比较 → **所有指标全 0**。
  现在显式归一坐标系，并有回归用例 `test_locate_bench_expected_paths_share_the_index_coordinate_system`。
- **文件**：`src/patchpilot/index/{symbols,lexical,retrieval}.py`（新增）、
  `src/patchpilot/eval/locate_bench.py`（新增）、`src/patchpilot/graph/repair.py`、
  `src/patchpilot/cli.py`、`tests/test_index.py`（新增 23 例）、`eval/locate.json`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -o addopts=""` → **558 passed + 4 skipped**，0 failed / 0 error
  - `pp locate` → 消融表落盘 `eval/locate.json`
- **审查**：按节拍，P2 属一个新阶段，留待与该阶段一并复审
- **提交**：见本批提交

### [2026-10-17] **验收项 2 收口**：`apply_patch` 的 fail-closed 缺口

- **背景**：前一轮把验收项 2 以"存档待收口"提交，登记的残余风险是
  **"git 清单为空"分支未经独立复审**。本轮把它补掉。
- **根因**：`_files_git_would_write` 在 `git apply --numstat` 失败时返回**空列表**，
  于是两种截然不同的状态被混为一谈：
  - "git 解析成功，且宣称不写任何文件"；
  - "git 根本解析不了这条补丁"。
  后者意味着**我们无法验证它会写到哪里**，此时静态校验（`_patch_touches_outside`）
  成了唯一防线——而静态道已被"引号名"与"非 `a/b` 前缀"绕过过两轮。
- **修法（三条 fail-closed）**：
  1. `_files_git_would_write` 改为返回 `list[str] | None`，`None` = git 解析失败；
  2. `apply_patch` 遇 `None` → **拒绝**（"无法枚举将要写的文件"）；
  3. git 解析成功但清单为空、而补丁里**又声明了路径** → **拒绝**（自相矛盾）；
  4. 逐文件解析级校验保留（第二道网）。
- **变异自证（§8.5）**：4 条守卫逐条删除，**4/4 KILLED**。
  其中一条一开始 **SURVIVED**（删掉"逐文件解析级校验"后全套仍绿）——
  因为静态道也拦得住同样形态，两层重叠。按 §8.5 第 5 条**不能就此算"已钉住"**：
  重叠≠第二层有用。于是补了一条**分层独立验证**用例
  `test_git_list_layer_holds_even_when_static_layer_is_blind`：
  把静态道人为致盲（`return None`），只留 git 清单道，验证它仍能拒绝 junction 写穿。
  补完后 M-D 变 **KILLED**。
- **未采用的方案（记录取舍）**：曾考虑"git 解析失败时直接放行、让真正的 apply 去报错"
  （因为两者用同一个解析器）。否决理由：两者**选项集不同**
  （`--numstat --summary` vs `--whitespace=nowarn --verbose`），且我无法穷举
  两者行为差异；安全边界上"无法验证"就应拒绝，而不是赌"另一个入口也会失败"。
- **第二十九轮 BLOCKING 修复（解码）**：`subprocess.run(..., text=True)` **未指定 encoding**，
  于是按本地代码页解码 git 的 UTF-8 输出。在 cp936 机器上：
  - 复审者环境：reader 线程抛 `UnicodeDecodeError` → **`stdout is None` 而 `rc=0`**
    → 旧代码 `(proc.stdout or "")` 把它当"空清单" → 守卫 3 误拒**合法**补丁，
    报错文案还是误导性的"不写任何文件"；
  - 也就是说"无法验证"又一次被静默当成"已验证"，只是换了个成因。
  **修法**：三处 `subprocess.run` 全部显式 `encoding="utf-8", errors="replace"`；
  `stdout is None` 一律判为"无法枚举"→ `None`（fail-closed）；
  并加 `-c core.quotePath=false` 让 git 直接输出**真实路径**（工具调用方需要真实名）。
- **第二十九轮同时修的清单道盲区**：清单道此前只 `strip('"')`、**不做 C 反转义**，
  而静态道做了。于是凡是 git 会引号化的名字（非 ASCII / 控制字符），清单道拿到的是
  **不存在的路径**——"第二道网独立成立"只对 ASCII 名成立（复审实测 junction `J链`）。
  修法：抽出 `_names_from_numstat` 统一做 C 反转义，还原失败 → `None`（fail-closed）；
  并修掉 `--summary` 里 `create`/`delete` 行（**没有** `(NN%)` 后缀）被正则漏掉的形态；
  同一文件同时出现在 numstat 行与 summary 行时**去重保序**。
- **§8.5 登记（如实）**：
  - `GitListError` 是死代码（全仓无 raise），**已删除**；
  - 守卫 3（"git 说不写文件但补丁声明了路径"）**在生产路径上几乎不可达**：
    复审构造了 10 种"声明了路径但 git 不列文件"的补丁，`--numstat` 全部 `rc=128`；
    它唯一被观测到的触发者正是上面那条解码失效。修完解码后它近似为**纵深防御**，
    保留是因为"两侧不一致就拒绝"是对的原则，且已有用例钉住它的行为；
  - `_names_from_numstat` 的 C 反转义在 `core.quotePath=false` 下对非 ASCII 名是**冗余**
    （git 已输出原始名），但一旦该配置被改动/遇到控制字符名就会成为唯一防线；
    为让它可被钉住，已抽成独立函数并加**解析规则直测**（喂 git 的引号化输出）。
- **已知边界（如实登记·经复审更正）**：早先写"`--numstat` 对纯 mode 变更 / 二进制 /
  空文件新建本机 git 2.54 自己就拒绝"——**这条不实**。复审实测：同一补丁以 **bytes** 喂
  git 2.54，mode-only 是 `rc=0` + `0\t0\tkeep.txt` + `mode change …`，空文件新建是
  `rc=0` + `create mode 100644 …`。它们**经工具失败的真因是 CRLF stdin**（见下一条），
  不是 git 拒绝。真实二进制补丁无法经 str API 表达，属另一类不可达。
- **平台相关性的教训（写进来以免复发）**：解码类缺陷**依赖环境**——
  本机 Python 的 subprocess 恰好按 UTF-8 解码，而复审者机器直接抛异常。
  因此解码契约用**结构化断言**（检查调用点是否传了 `encoding`）钉住，
  而不是写"复现旧缺陷"的行为断言（那种断言在部分机器上会假绿）。
  全量套件在 **cp936 与 UTF-8 两种模式下各跑一遍**（各 532 passed / 4 skipped）。
- **第三十轮 BLOCKING 修复（编码侧）**：`input=patch` 与 `text=True` 同时使用时，
  Windows 上 subprocess 会按 `os.linesep` 把补丁的 `\n` **翻译成 `\r\n`** 再喂给 git。
  两个方向都能观测到，且都是"静默错误结果"：
  - 对 **LF 文件**（本仓库就是 `i/lf w/lf`）做一行修改 → `rc=1 patch does not apply`
    （git 在找带 CR 的上下文行），**合法补丁被拒**；删除补丁同样失败；
  - **新建文件**：补丁是 `+alpha\n+beta`，落盘却是 `b'alpha\r\nbeta\r\n'`，函数仍返回 `ok`
    → **静默改了行尾**。
  **为什么 535 项测试没抓到**：夹具全用 `Path.write_text`，在 Windows 上落盘就是 CRLF，
  "夹具 CRLF + 补丁被翻成 CRLF"两边一起错、恰好一致（`write_text("# demo\n")` → `b'# demo\r\n'`）。
  **修法**：
  1. 新增 `_run_git_with_stdin`：`input=patch.encode("utf-8")`（**字节**）、不走 text 模式、
     输出显式 `.decode("utf-8", "replace")`；`apply_patch` 真应用与 `_files_git_would_write` 都改；
  2. 抽出 `_git_env(root)`，三处调用共用"剥凭据 / 不交互"环境；
  3. `_run_git` 补 `stdout is None` 的 fail-closed 守卫；
  4. 新增 `core/textio.write_text_lf`：所有会参与补丁/内容比对的写入（语料物化、补丁落盘、
     `fs` 写文件、评测报告、基线）统一按字节写 UTF-8，杜绝 `write_text` 的平台换行翻译；
  5. 测试夹具新增 `write_lf()`（`write_bytes`），测试仓库加 `core.autocrlf=false`。
  **判据**：改 LF 文件成功 / 删 LF 文件成功 / 新建文件盘上字节严格 `\n` /
  清单道与 apply 看到同一份补丁；变异 **M-I（退回 text 模式）与 M-K（绕过 stdin）双双 KILLED**。
- **§8.5 追加登记（等价变异）**：`_run_git_with_stdin` 里 `(bytes).decode()` 与
  `.decode("utf-8")` **行为等价**（后者默认即 UTF-8），唯一差异是不带参数时 `errors=strict`。
  要区分二者需让 git 输出**非法 UTF-8 字节**（Windows 上无法造），故登记为等价变异，
  不计为覆盖缺口。
- **文件**：`src/patchpilot/tools/git_server.py`、`tests/test_tools.py`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -o addopts=""` → **535 passed + 4 skipped**（cp936 与 UTF-8 两种模式），0 failed / 0 error
  - 真机 e2e（沙箱 4 类安全）→ **15/15**
  - junction 形态矩阵 → **12/12**（越界全拒、根内全通过）
  - junction 写穿复核 → **8/8**
  - 合法补丁防误伤：改/新建/删除/重命名/多文件/引号名/非 `a/b` 前缀 **7 类全部 OK**
- **审查**：本轮按新节拍属"完成一个验收项"，**随本批发起一次复审**
- **提交**：见本批提交

### [2026-10-17] **验收项 7 完成**：一键部署 + Web 轨迹回放面板

- **新增**：
  1. `src/patchpilot/api/app.py` —— FastAPI 只读面板。端点：
     `/`（单页）、`/api/traces`（列表摘要）、`/api/traces/{id}`（含全部 span）、
     `/api/evals`（评测指标）、`/api/health`（供部署脚本判断就绪）。
  2. `pp serve` 命令（host/port/trace-dir/eval-report）。
  3. `docker/app/Dockerfile` + `docker-compose.yml` 的 `panel` 服务：
     trace 与评测产物**只读**挂载、HEALTHCHECK 探活、`restart: unless-stopped`。
- **三个刻意的设计选择**：
  1. **零前端构建**：面板是单个内联 HTML+JS 页面，没有 npm/打包步骤。
     "一键部署"若需要先装 node 跑 build，就不叫一键了（并且镜像只有 237MB）。
  2. **只读**：不提供任何执行入口——避免把"远程执行不可信代码"暴露到 Web 上。
  3. **无状态**：两个数据源都是文件（`traces/*.jsonl` 与 `eval/report.json`），
     面板重启不丢数据，也不需要数据库。
- **真机验证**：
  - `docker build -f docker/app/Dockerfile -t patchpilot-panel:0.1.0 .` → 成功（64s，237MB）；
  - `docker run` 后用真实 HTTP 请求验证：`/api/health` → `{"ok":true,"traces":1}`（读到了宿主的真实 trace）、
    `/api/traces` 返回 `eca95a80fa48432f`（就是 `pp run` 那次任务的 trace）、
    `/api/evals` 返回 `pass@1` 等指标、`/` 返回 6607 字节页面且含"span 时间线"；
  - `docker compose config` 校验通过，4 个服务（panel/postgres/qdrant/redis）。
- **容错**（测试覆盖）：trace 目录不存在 → 空列表而非报错（首次部署就是这种状态）；
  JSONL 坏行跳过；缺评测报告 → `available: false`；
  未定价模型在面板上显式标"⚠成本不可信"，不显示成"一切正常"。
- **文件**：`src/patchpilot/api/{__init__,app}.py`（新增）、`src/patchpilot/cli.py`、
  `docker/app/Dockerfile`（新增）、`docker-compose.yml`、
  `tests/test_panel_api.py`（新增 13 例）、`pyproject.toml`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → 见本批冻结自证
  - 13 例面板测试含**真起 uvicorn 子进程**用真 HTTP 请求验证（只测 TestClient 会漏掉
    "uvicorn 起不来/参数拼错"）
- **审查**：按用户指示的节拍，留待以后与该验收项一并复审
- **提交**：见本批提交

### [2026-10-17] **验收项 5 完成**：回归门禁（改坏 prompt → CI 变红 + 指标 diff）

- **新增**：
  1. `src/patchpilot/eval/baseline.py` —— 基线对比。基线是**提交进版本库的数据**
     （`eval/baseline.json`），由一次确认合格的运行产出；之后每次对比 `pass@1`、
     成本、P95，并给出**逐题**得失（哪几题从通过变失败、哪几题新增通过）。
  2. `pp eval --baseline PATH`（退化 → **退出码 2**）、`--write-baseline PATH`、`--strict-cost`。
  3. `.github/workflows/ci.yml` —— 两道门禁：`gate`（`pp check` + 单测，无需密钥/Docker/网络，
     必过）与 `eval-regression`（真模型 + 真沙箱跑基线对比，缺密钥时**显式跳过并告警**，
     绝不假绿）。
- **真机验证（本项的关键证据）**：
  - 未改动 → 退出码 **0**、"无回归"；
  - 把 prompt 从"只输出 unified diff"改成"不要输出任何 diff 或代码块"→
    **pass@1 从 100% 掉到 50%**、退出码 **2**、diff 点名 `fast-003-wrong-exception` 等退化题。
- **两次自我纠错（都很有价值）**：
  1. **第一次改坏的方式没有效果**：改成"用散文解释"后 pass@1 仍是 100%——
     因为 `extract_patch` 会从散文里把 ```diff 围栏抠出来。**教训**：验证门禁的
     "改坏"必须真的掐断被判定的信号，否则测的是自己的想象。
  2. **门禁误报**：还原源码后复跑仍判红（成本 0.004268 → 0.005673，+33% 超过当时 30% 容差）。
     根因是**采样噪声**（这次模型多转了几轮），不是回归。**修法**：`pass@1` 才是唯一
     默认阻塞项；成本/延时默认**只提示不阻塞**（容差放宽到 100%，`--strict-cost` 可收紧）。
     一个在未改动代码上也会变红的门禁，维护者会很快学会忽略它。
  3. 顺带修掉：`--limit` 子集运行会把"没跑"的题算成"退化"（假的丢失）——
     改为**只在本次真正跑过的题目范围内**做逐题对比，并提示"对比不完整"。
- **文件**：`src/patchpilot/eval/baseline.py`（新增）、`src/patchpilot/cli.py`、
  `eval/baseline.json`（新增）、`.github/workflows/ci.yml`（新增）、
  `tests/test_baseline.py`（新增 11 例）、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **506 passed + 4 skipped**，0 failed / 0 error
  - 真机演示 `.tmp/demo_regression_gate2.py` → **PASS**（未改动绿 / 改坏红）
  - CI 工作流 YAML 已解析验证（2 个 job、步骤齐全）
- **审查**：按用户指示的节拍，留待以后与该验收项一并复审
- **提交**：见本批提交

### [2026-10-17] **验收项 6 完成**：全链路可观测（四类 span + `pp trace` 回放）

- **背景**：此前 `observability/tracing.py` 已有 `Tracer`，但**修复闭环一行都没用**——
  P0 的 `minimal.py` 用，P3 的 `repair.py` 没用，验收项 6 因此停在"部分完成"。
- **改动**：
  1. `RepairOptions` 增加 `trace_dir` 与 `price_overrides`；为 None 时**完全不产生 IO**
     （离线测试与门禁不受影响）。
  2. 四类 span 全部接入：**检索** `retrieval.search`、**LLM** `llm.generate`
     （带 model 与 prompt 字符数）、**工具** `tool.apply_patch`（git）与
     `tool.run_tests`（沙箱，带 exit_code/timed_out）、**成本** `record_usage`。
  3. `finalize` 落盘 trace 并把路径写回状态；`pp run` 在结果表里打印 trace 路径。
  4. 新增 `pp trace --limit N --verbose`：读 JSONL、按任务展开 span 耗时与属性，
     用来回答"这个任务花了多少钱、哪一步慢、检索命中几个文件"。
- **真机实测（`pp run` + `pp trace`）**：单个任务
  `retrieval.search 28.9ms / llm.generate 1557.8ms / tool.apply_patch 393.7ms /
  tool.run_tests 1855.0ms`，总耗时 3835ms，token 162+235，成本 **\$0.000165**，模型 `deepseek-flash`。
- **修掉的 1 个真 bug（口径不一致）**：`_charge` 只要 `LLMResponse.cost_usd is None`
  就把模型记成"未定价"，**不看价格表**；而 `Tracer.record_usage` 会按价格表算出正成本
  → trace 里出现 `cost_usd > 0` 与 `cost_unknown = true` **自相矛盾**的组合（真机实测到）。
  修法：`_charge` 先用 `estimate_cost_usd(..., options.price_overrides)` 再决定是否记未定价，
  与 Tracer 同口径。
- **文件**：`src/patchpilot/graph/repair.py`、`src/patchpilot/cli.py`、
  `tests/test_tracing_pipeline.py`（新增 6 例）、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **495 passed + 4 skipped**，0 failed / 0 error
  - 真机 `pp run`（真 DeepSeek + 真 Docker）→ 1 轮通过，trace 落盘
  - 真机 `pp trace --verbose` → 四类 span 与耗时/成本均正确显示
  - 新增 6 例专门钉这四类（断言落在**trace 的实际内容**上，而不是"Tracer 被调用过"）
- **审查**：按用户指示的节拍，留待以后与该验收项一并复审
- **提交**：见本批提交

### [2026-10-17] **验收项 3/4 实现完成**：`pp eval` 出真实数字 + 可复现报告

- **新增**：
  1. `src/patchpilot/eval/dataset.py` —— 评测集格式（JSONL）+ **加载期校验**。
     拒绝四类会让"度量衡"失效的题：`fail_to_pass` 为空、镜像用 `latest`、
     仓库/issue 路径不存在、题目 id 重复。
  2. `src/patchpilot/eval/runner.py` —— 评测运行器。判定口径与 SWE-bench 对齐：
     **先验证 `fail_to_pass` 在修复前确实失败**（题目前提），再让修复闭环跑，
     最后用闭环产出的补丁在**全新副本**上独立复核 `fail_to_pass + pass_to_pass`。
     修复前就绿的题标成 `invalid_task` 并**从分母排除**（否则指标没有意义）。
  3. `pp eval` 命令 —— 表格输出逐题状态 + 汇总指标（pass@1 / 成本 / token / 平均与
     P95 耗时 / 失败分布 / 可复现信息），并把完整报告落盘为 JSON（验收项 4）。
  4. `eval/corpus/`（6 个自建小仓库）+ `eval/fast.jsonl` —— 自建 **FastEval** 评测集。
     每题都带一个"修复前必须失败"的用例与一个"前后都必须通过"的用例。
- **真实数字（本轮产出）**：`pass@1 = 6/6 = 100%`、总成本 **\$0.0040**、总 token 9,200、
  平均 **10.8s**、**P95 15.1s**（`deepseek-flash`，温度 0，镜像 `patchpilot-sandbox:0.1.0-py312`）。
  这是**自建 6 题**的结果，不能与 SWE-bench 数字混谈——Lite 50 题属后续 BenchEval。
- **过程中修掉的 3 个真 bug**（都由"数字不对"倒查出来）：
  1. **`workspace` 不是声明的状态通道**：LangGraph **静默丢弃**未声明的键 →
     `run_tests` 拿到的副本路径恒为空 → 悄悄回落到**未打补丁的源仓库** →
     表现为"6 道题全 failed，但每轮产出的补丁逐字符相同且内容正确"。
     这与 `attempts` 是同一类坑；已补 `workspace` 通道 + 一条端到端用例钉住
     （断言 `runner` 收到的目录里**必须已含补丁**，且不等于源仓库）。
  2. **补丁产物写进被评测的仓库**：`finalize` 原先按 `issue_file.parent` 落盘，
     评测时那正是语料目录 → 6 个语料目录里被写入 `*.diff`（自己也成了脏数据）。
     新增 `RepairOptions.output_dir`，评测器指向工作目录。
  3. **5 道题的前提不成立**（自查脚本在**真沙箱**里逐题验证后才发现）：
     `fast-001` 的 `pass_to_pass` 断言本身写错；`fast-002` 的 bug 不会让那条断言失败；
     `fast-004` 只有一个匹配键时 `del` 后循环即结束、**不抛 RuntimeError**；
     `fast-005` 的 Python `sorted(reverse=True)` 是**稳定**的，同分本来就保序（该 bug 不存在）；
     `fast-006` 的 `"a=".split("=")` 长度为 2、**不抛 ValueError**。
     全部重写后 6/6 前提成立。**这条最能说明"前提校验"的价值**：
     若不校验，这 5 道题会让 `pass@1` 变成无意义的数字。
- **文件**：`src/patchpilot/eval/{dataset,runner}.py`（新增）、`src/patchpilot/cli.py`、
  `src/patchpilot/graph/repair.py`、`eval/corpus/**`、`eval/fast.jsonl`、
  `eval/report.json`、`tests/test_eval.py`（新增 21 例）、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **489 passed + 4 skipped**，0 failed / 0 error
  - **真机** `pp eval`（真 DeepSeek + 真 Docker）→ **6/6 passed**，报告落盘
  - 语料前提校验（真沙箱逐题跑）→ **6/6 成立**
- **审查**：按用户指示的节拍，留待以后与该验收项一并复审
- **提交**：见本批提交

### [2026-10-17] **验收项 1 实现完成**：`pp run` 一条命令在本地仓库上完成一次修复

- **背景**：按用户指示，验收项 2 以"存档提交（`0f5793f`）+ 待收口"收尾，直接转入下一项。
  本批实现 **验收项 1**（`pp run --repo . --issue-file bug.md`），也就是 P3 的修复闭环。
- **新增**：
  1. `src/patchpilot/graph/repair.py` —— **8 节点修复闭环**：
     `load_issue`（读 issue、抽检索词）→ `locate`（`tools.search` 找候选）→ `generate`
     （模型产出 unified diff）→ `apply`（**只打在工作副本上**）→ `run_tests`
     （Docker 沙箱跑 pytest，取 JUnit 结构化结果）→ `evaluate` → `reflect`
     （失败用例压成记忆，供下一轮 prompt）→ `finalize`（落盘补丁 + 报告）。
     路由含三条早停：定位失败、补丁打不上/解析不出、轮数或预算耗尽。
  2. `pp run`（`cli.py`）—— 验收项 1 的命令行入口，输出轮数/候选/测试结果/归因/补丁路径。
  3. **可测试性设计**：模型（`LLMClient`）与测试执行（`TestRunner`）/补丁应用
     （`PatchApplier`）都是可注入协议，因此**整条图能离线端到端验证**；
     真机版本另有一个用例（缺 openai/Key/Docker 自动 skip，不给门禁引网络依赖）。
- **真机验证（本轮最有价值的一条）**：`tests/test_repair_e2e.py` 用**真 DeepSeek 模型 +
  真 Docker 沙箱**在临时仓库上跑完整条链路——**修好了那个 bug 并且测试转绿**（耗时 75s），
  同时断言"用户仓库一个字节都没被改"。这是本批首次让"模型 → 补丁 → 沙箱验证"闭环在真机上跑通。
- **过程中修掉的 4 个真 bug**（都不是测试问题）：
  1. **预算熔断永不触发**：`attempts` 没进 `RepairState`，而 `BudgetTracker.to_state()` 写这个键
     ——LangGraph 不持久化它，`from_state` 每次从 0 开始，于是 `max_attempts` 形同虚设
     （实测 `max_rounds=2` 跑了十几轮）。这是 P0 就埋下的同类问题的复发形态。
  2. **`started_at=0.0` 导致第一个节点就熔断**：时长上限算出 1.3e6 秒的"已用时长"。
  3. **`Event loop is closed`**：`AsyncOpenAI` 的连接池绑定创建它的事件循环，而图节点是同步的
     ——原先每次 `asyncio.run` 建新循环、用完即关，**第二轮调用必崩**。
     修法：`core/llm.py` 新增 `complete_sync` / `run_sync`（**常驻事件循环 + 后台守护线程**）。
  4. **`_default_applier` 形参名与协议不一致**（`root` vs `workspace`）：真机第一次调用就抛
     `TypeError`；而错误处理把 `workspace` 重置成 `"."`，**把这条信息掩盖成"补丁应用失败"**。
     两处都修了（签名对齐 + 不再掩盖真实错误）。
- **文件**：`src/patchpilot/graph/repair.py`（新增）、`src/patchpilot/core/llm.py`、
  `src/patchpilot/cli.py`、`tests/test_repair_graph.py`（新增 15 例）、
  `tests/test_repair_e2e.py`（新增，真机）、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **468 passed + 4 skipped**，0 failed / 0 error（收集 472）
  - **真机端到端** `tests/test_repair_e2e.py` → **1 passed**（DeepSeek + Docker 沙箱，
    修好 bug 且测试转绿，用户仓库未被改动）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：按用户指示的节拍，本批**留待以后与该验收项一并复审**（不再逐批复审）
- **提交**：见本批提交

### [2026-10-16] **验收项 2 完成**：沙箱驱动层 + 4 个 MCP server（P1 收口）

- **范围**：这是按新节拍（§0：每完成一个验收项才复审+提交）产出的**第一个**验收项。
  补上 P1 缺失的两块：**驱动层**（真正跑容器）与 **4 个 MCP server**。
- **新增**：
  1. `src/patchpilot/sandbox/manager.py` —— 沙箱薄驱动。**不拼任何参数**
     （安全语义仍由 `policy.build_run_args` 独占决定），只负责：复制副本 → 起容器 →
     双通道超时 → 读回结果。三个真机踩过的坑直接落在实现里：
     ① 宿主侧超时后容器仍在跑 → 显式 `docker rm -f`；
     ② 结果不走 stdout 解析（用户测试会往 stdout 打任何东西）→ 走 JUnit XML 文件通道；
     ③ JUnit 写到**挂载的副本内**（rootfs 只读、`/tmp` 是 tmpfs 会随容器消失）。
     命令里只有 pytest 才追加 `--junitxml`（对任意命令强加会把参数变成位置参数）。
  2. `docker/sandbox/Dockerfile` —— 沙箱执行镜像 `patchpilot-sandbox:0.1.0-py312`。
     官方 `python:3.12-slim` **不含 pytest**，而容器 `--network none` 无法 `pip install`，
     故按设计文档 §3 自建镜像（预装 git + 锁定版本的 pytest 栈，非 root uid 1000）。
     `policy.DEFAULT_IMAGE` 随之切换。
  3. `src/patchpilot/tools/` —— 4 个 server 的**工具逻辑**（纯标准库）：
     - `fs`：`read_file`/`write_file`/`list_dir`，全部经 `resolve_within`（根之内，
       拒绝对路径/`..`/symlink 逃逸）；额外拒绝 `~` 前缀（`Path.resolve()` 不做 shell 展开，
       `"~/x"` 会被静默当成仓库内目录名，属语义陷阱）；
     - `git`：**两层白名单**（子命令 + 每个子命令的参数）。只做子命令白名单会漏掉
       `git diff --no-index /etc/passwd x` 这类读根之外文件的用法。
       子进程注入 `GIT_TERMINAL_PROMPT=0`、`GIT_CONFIG_NOSYSTEM=1`、`HOME=<root>`
       （避免读到宿主 `~/.gitconfig` / `~/.git-credentials`）；
     - `test`：**命令白名单**（只允许 `python -m pytest`）+ pytest 参数白名单。
       刻意拒绝 `--rootdir`/`--confcutdir`/`-c`/`--junitxml`/`--html`/`--basetemp`/任意 `-p`；
     - `search`：`search_text`（默认字面子串，可选正则）/`search_symbol`（定义模式）。
       纯标准库实现，**接口刻意与 P2 的符号索引保持一致**，P2 只换实现不改接口。
  4. `src/patchpilot/tools/servers.py` + `pp mcp <fs|git|test|search>` —— MCP stdio 入口。
     **兼容两代 SDK**：`mcp 2.x` 把 `FastMCP` 改名成 `MCPServer`（实测踩到），
     故惰性加载时先试 `mcp.server.mcpserver.MCPServer` 再回退 `mcp.server.fastmcp.FastMCP`；
     缺失依赖时给可操作报错，且工具函数本身仍可 import（CI 不必装 MCP）。
- **验证（真机 + 协议层）**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **452 passed + 4 skipped**，0 failed / 0 error
    （跳过项：2 个 Windows 无 symlink 权限、1 个 symlink 同因、1 个 postgres 变体；
     沙箱三模块 244 → 全套 **456 收集**）
  - **真机 Docker 端到端 15/15**：`manager.py` 在沙箱里跑 pytest 拿到结构化报告
    （1 过 1 失败逐用例正确、全绿仓库 ok=True、超时 5.3s 截断且容器无残留、副本无残留）；
    4 类安全用例齐全——网络不可达、rootfs 只读、非 root（uid=1000）、
    **内存 OOM 熔断（rc=137）**、**pids 超限 fork 被拒（BlockingIOError，pids.max=512）**、
    超时被杀且清理、宿主目录不可见。
    日志落盘 `.tmp/verify_sandbox_container.out`。
    （记录一次自查失误：第一版内存用例只申请 3GB，低于 4g 上限，容器当然能分配——
    是**用例写错**而非熔断失效；改为 12GB 后 rc=137。）
  - **MCP 协议层**（真起进程走 JSON-RPC）：4 个 server 全部构造成功、工具集合与硬编码表
    逐项相符（fs 3 / git 4 / search 2），fs 越界读取在协议层以 `is_error=True` 返回，
    search/read_file 往返内容正确。`tests/test_mcp_servers.py` 12 例。
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **变异自证（§8.5 第 7 条；第二十四轮要求补记）**：复审者在副本内跑了 9 个变异，
  **KILLED 6 / SURVIVED 3**，据此确认 fs/git/test 三套白名单确实在**代码路径**上生效：
  - KILLED：删副本清理、挂载源改回源仓库、不追加 `--junitxml`、超时不 `docker rm -f`、
    删 pytest 参数被禁模式、删 fs 的 `~` 守卫；
  - SURVIVED（已处理或登记）：git `_FORBIDDEN_VALUE` 无用例（纵深防御，已登记）、
    `SandboxResult.ok` 的 `failed == 0`（**已改为委托 `JUnitReport.ok`**）、
    `pp mcp test` 沙箱根放宽（**已补 CLI 用例**）；
  - 另一条（search 退回绝对路径比对）**裸跑 SURVIVED，但把 basetemp 放到
    `.patchpilot/` 下即 KILLED**——印证这条只有门禁能钉住，故必须留在门禁里。
- **第二十四轮 BLOCKING 修复（复审判"逃逸类不成立"）**：
  `search_server._iter_files` 原先用 `Path.rglob("*")` + 只过滤 `is_symlink()`，
  而 **Windows junction 的 `is_symlink()` 是 False**，于是会递归进入 junction。
  真机实测：仓库内 `mklink /J linked <外部目录>` 后
  `search_text(root,"PRIVATE KEY")` 返回 `linked/id_rsa`（宿主私钥进了 Agent 上下文），
  而 `fs_server.read_file` 对同一路径**正确拒绝**——只有 search 漏。
  修法：改手工 `os.walk(followlinks=False)` + `policy.is_link_like` **按目录剪枝**
  （一个判据同时覆盖 symlink 与 junction，与 `workspace._scan` 同口径），
  并对每个候选文件复核 `resolve().is_relative_to(root)`；
  补 `test_search_never_follows_directory_links`（Windows 真建 junction，本机**实跑未跳过**）。
  同批非阻塞项：`test_server.py` → `pytest_runner.py`（原先门禁把**源码**当测试模块收集并告警）、
  `SandboxResult.ok` 委托报告、补 `subprocess.run` 抛 `OSError` 的清理用例、
  `pp mcp test` 副本根改落系统临时目录（原先落在**仓库父目录**，强杀会残留）、
  e2e 脚本改用 `DEFAULT_IMAGE`、README 三处口径同步（12/12 → 15/15）。
- **第二十五轮 BLOCKING 修复（git 写穿，与 search 同根因同形态）**：
  `git_server.apply_patch` 只做**文本级**路径校验（绝对路径/`..`），完全不解析链接，
  而 `git apply` 会**穿过 junction** 写文件（junction 的 `is_symlink()` 是 False）。
  真机复现 3 形态：① 改写根外既有文件；② 在根外新建；③ junction 套 junction 两跳；
  且**协议层可达**——`pp mcp git --root <repo>` 调 `apply_patch` 返回 ok，
  宿主文件被写成 `WRITTEN-THROUGH-JUNCTION-BY-MCP`；同一路径经 `fs_server.write_file`
  是正确的拒绝。**修法**：`_patch_touches_outside(patch, root)` 改为**文本 + 解析**两道——
  对每个路径 token 逐级检查祖先段的 `is_link_like`，并要求 `(root/candidate).resolve()`
  仍在根内；补 junction 三形态回归用例（宿主字节未变 + 根外无新文件 + 根内补丁仍生效）。
  **同批修掉同一根因的最后一处旧口径**：`preflight._walk_files` 的 `is_symlink()`
  改 `is_link_like`（此前它会跟随 junction，把**根外**字节纳入摘要/密钥扫描；
  自引用 junction 还会造成指数放大：实测 8.5s、40 条 `self/self/...`）。
  另外按 §8.5 补钉三个存活变异：指向**根内**的链接不得重复命中（钉住目录剪枝）、
  `errors=1,failed=0` 必须判非 ok（钉住 `ok` 委托）、以及把 CLI 沙箱根断言改为
  **断言专用根的名字**（原先只断言 `is_relative_to(tempdir)`，"仓库父目录"同样满足 → 假绿）。
  并真的在生产路径调用 `find_escaping_links`（`workspace.py` 声称"调用方必须调用"，
  此前**无任何生产调用点**）。
- **第二十六轮 BLOCKING 修复（rename/copy 未收口）**：`_patch_paths` 只解析
  `diff --git`/`---`/`+++`，漏掉 `rename to`/`copy to`/`rename from`/`copy from`/`edit`，
  而 `git apply` 会真的执行它们 → 4 行补丁即可在根外**新建**文件并把根内源文件移走，
  协议层可达。
  **修法**：补齐五类指令行；**第一次改法有 bug**（用 `split(" ",1)[1]` 取剩余，
  抽出 `'to linked/X'`，指令名没剥干净），自查探针发现后改为按指令名长度切片。
- **第二十七轮 BLOCKING 修复（仍在"猜"git 的路径解析）**：审查者用 21 例矩阵证明
  候选路径与 git 实际落盘路径**不一致**就会放行，两处：
  ① **引号名**——`git apply` 会对 `"..."` 做 C 反转义后执行，而我们只 `strip()`，
  于是 `Path('"linked/X"')` 既不是链接、`resolve()` 也在根内，两道校验全盲；
  而且 `core.quotePath=true`（默认）时 git **自己**就会这么输出非 ASCII 名。
  ② **非 `a/`/`b/` 前缀**——`-p1` 的语义是"剥掉第一段分量，不管它叫什么"，
  而解析道只按字面 `a/`/`b/` 剥，于是 `--- x/linked/victim.txt` 被判安全。
  **修法（结构性）**：不再猜前缀，改为
  (a) `_unquote_c_style` 按 git 规则解 C 转义（`\\`/`\t`/`\n`/八进制 `\NNN`；未知转义 fail-closed）；
  (b) 对每个名字生成**所有"去掉前 k 段"的形式**，逐个跑解析道——`-p1` 究竟剥哪一段由 git 决定，
     我们把所有解释都当候选，任一种越界即拒绝；
  (c) 对"提到 rename/copy/edit 但不符合已知形态"的行（制表符分隔、行首空格）
      **fail-closed 拒绝**，不再静默跳过；
  (d) 元测试改成**精确等值**（原先是子串断言，对"前缀没剥干净"完全钝感——审查者实测
     改回有 bug 的版本后元测试 8 例仍全绿），并把引号/非 `a/b` 前缀形态加进硬编码锚点表。
  **判据**：12 例形态矩阵（含引号、八进制转义、`x/`/`y/` 前缀、可疑行、未知转义）
  **12/12 拒绝且根外字节不变**，同时"根内改写/新建/rename"3 例仍成功（防误伤）。
- **第二十八轮加强：不再只靠"我们理解 git 怎么解析路径"**。前两轮修法有同一个前提弱点——
  **我们复刻 git 的路径语法**，于是每次都被一种新形态绕过。本批增加一道
  **以 git 的解析为准**的网：先跑 `git apply --numstat --summary -`（只解析、不落盘），
  取 git **自己报告**的"将要写的文件"清单，对这份**真实清单**逐个跑解析级校验
  （`_path_escapes`：绝对路径 / 回溯 / 祖先段 `is_link_like` / `resolve()` 仍在根内），
  任一项越界即拒绝。这样"我们的解析"与"git 的解析"不一致时**以 git 的清单为准**，
  漏一种形态不再等于失守。
- **文件**：`src/patchpilot/sandbox/manager.py`、`src/patchpilot/tools/*`、
  `docker/sandbox/Dockerfile`、`tests/test_tools.py`、`tests/test_sandbox_manager.py`、
  `tests/test_mcp_servers.py`、`src/patchpilot/cli.py`、`pyproject.toml`、`README.md`、`PLAN.md`
- **审查**：第二十四至二十七轮连续判 BLOCKING（search/git 的链接逃逸），均已修复；
  第二十八轮复审（`049b18c6`）**按用户要求中止**——理由见下。
- **按用户指示的节拍调整（2026-10-16）**：用户指出"在一个验收项上审查过多轮，影响功能开发"，
  指示**先推进下一个验收项，这一项留待以后检查**。因此：
  - 本项以"**门禁通过 + 真机 15/15 + 452 单测全绿**"的状态**提交存档**（见下条提交 hash），
    但**不标记为已验收**；`PLAN §1` 第 2 项状态改回 **🔧 待收口**；
  - **已知待收口点**（如实登记，不隐瞒）：`apply_patch` 的路径安全经四轮修法（纯文本 →
    补指令行 → 解引号/展开 `-p1` → **以 `git apply --numstat` 的清单为准**）后，
    静态道 + git 清单道双重把关；但"清单为空时静态道是唯一防线"这一情形
    **未经独立复审确认**，属已知残余风险；
  - 其余子项（驱动层、4 个 MCP server、`fs`/`search`/`test` 的解析级根约束、真机 4 类安全用例）
    已经多轮独立复核成立。


### [2026-10-16] 第二十二轮 BLOCKING 修复：宿主暴露面在 **src/ 里**没有任何守卫

- **背景**：第二十二轮 REJECT。审查者独立复现了第二十一轮的 6 个代码编辑变异（6/6 KILLED，
  与我自查吻合），但指出**真正的缺口在参数通道**：第二十一轮的修法**纯粹是测试侧的**——
  把两条新用例改名停用后，`src={workspace}`→`src=/` 的变异**全绿**。
  即"挂宿主根"既不需要改代码（传参即可），又能靠删测试彻底掩盖。
- **变更（全部落在 `src/`，不再靠测试挡）**：
  1. **BLOCKING——`workspace` 完全未校验**。真机（Docker 29.5.2）实测：
     `build_run_args(..., workspace="C:\\")` **不抛异常**，整个系统盘可读写挂进 `/workspace`
     （容器列出 `Program Files`/`Users`）；`workspace=<宿主目录>` → 容器 `cat` 到宿主机密文件；
     `workspace=".."` → 被 docker 客户端按**它自己的 cwd** 静默解析成宿主目录；
     `assert_safe` 与 `assert_required_invariants` **全部通过**。
     **修法**：新增 `_validate_mount_source()`，在 `build_run_args` 内校验挂载源——
     必须绝对路径、存在且为目录、非文件系统根、且**落在显式声明的沙箱根之下**。
     新增 `SandboxPolicy.sandbox_root` 字段（默认系统临时目录；生产由驱动层指定专用工作根）。
     **为什么用白名单包含判定而不是黑名单**：枚举敏感目录永远列不全——我第一版只拒
     "CWD 及其祖先"，自查时 `Path.home()` 因与仓库**不同盘**而绕过（真机可复现）。
  2. **BLOCKING——`image` 字段可冒充 flag**。`SandboxPolicy(image="-vC:\\…\\secret:/mnt")`
     构造成功，`build_run_args` 把它写在**镜像位**（唯一处于 flag 位置的字段），
     docker 的 pflag **短选项粘连**（`-v<value>`）把它解析成 `--volume` → 真机 rc=0、
     容器经 `/mnt/SECRET.txt` 读到宿主文件。这正是本模块第 8 轮被 `-v<host>:/mnt`
     击穿的**同一条通道经 `image` 字段复发**。修法：`__post_init__` 拒绝以 `-` 开头
     或含空白/NUL 的镜像名。
  3. **BLOCKING——`pp smoke` 新增逻辑零覆盖**。实测删掉"默认强制 stub provider"整块、
     或删掉 `--real-llm` 选项，全套仍 325 passed + 3 skipped。而它恰是"改 `.env` 就能让
     零依赖门禁变红"的唯一实现点。修法：新增 `tests/test_cli_smoke.py`（3 例）——
     monkeypatch `get_settings`/`run_minimal_demo`，断言 `.env` 配 `deepseek` 时
     `smoke()` 仍把 `stub` 传给 demo、显式 `real_llm=True` 时保留 `deepseek`、`issue` 透传。
  4. 非阻塞一并处理：`match=` 全部锚定（`^禁止以 root 运行`、`^--pids-limit 必须为正整数: .*$`）；
     更正两处失效注释（`_minimal_argv` 用锚点而非派生表；"`str.splitlines()`" 的理由是错的——
     该函数在 `sandbox/policy.py` 从未出现，结论仍成立）；删掉错位的裸 docstring 与重复断言；
     `test_writable_mount_sources_are_never_the_host_root` 改为**直接喂敌意输入**
     （`C:\\`/`/`/`..`/`~`/`C:\\Windows`/仓库路径…），不再对夹具自己断言。
  5. **§8.5 上限登记收窄（审查者的净收益，如实更正）**：4 处协同**不再"8 项都记得住"**——
     实测只有 **2/8**（`--cap-drop`、`--security-opt`）仍可全绿；
     `--network`/`--read-only`/`--user`/`--memory`/`--memory-swap`/`--pids-limit`
     六项已被第二十一轮的"完整 argv 负例"杀掉（`args.index(flag)` 抛 `ValueError` → 用例变红）。
- **文件**：`src/patchpilot/sandbox/policy.py`、`src/patchpilot/cli.py`、
  `tests/test_sandbox_policy.py`、`tests/test_cli_smoke.py`（新增）、`PLAN.md`
- **设计说明（沙箱根的显式化）**：新增的 `sandbox_root` 字段把"容器能挂哪些宿主目录"
  从"一个全局默认值碰巧覆盖"变成**可审计的显式配置**：默认取系统临时目录，
  驱动层可为每个任务指定专用工作根。测试侧因此必须**声明自己的根**
  （pytest 的 basetemp 可能在 `.patchpilot/` 下，不在系统临时目录里），
  所以 `tests/test_sandbox_policy.py` 增加 `sandbox` 夹具（`SandboxPolicy(sandbox_root=tmp_path)`）
  与 `_with(policy, **overrides)` 辅助函数：**生成 argv 的用例**从夹具派生（保留根），
  **构造期校验的用例**仍直接构造（它们要验的正是构造器本身）。
  这一改动是被门禁自己逼出来的——我的第一个版本让挂载源只允许系统临时目录，
  门禁（basetemp 在 `.patchpilot/` 下）立刻报 16 项失败，说明"默认值假设"在真实门禁下不成立。
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **347 passed + 3 skipped**，0 failed / 0 error
  - 沙箱三模块单测 **244**（policy 212 / workspace_copy 13 / junit 19）；全套收集 **350**
  - 敌意挂载源逐项复核：`C:\\`/`/`/`..`/`src`/仓库根/家目录/`C:\\Windows`/仓库内文件
    全部构造期拒绝；合法的临时副本目录与"显式 `sandbox_root` 之下"的目录通过
  - 变异自证（副本 + 显式 `PYTHONPATH` + 先断言导入来源在内）：删掉挂载源校验调用 → 8 项红；
     绝对路径检查置假 → 3 项红；沙箱根包含判定置假 → 3 项红；删掉 smoke 强制 stub 块 → 1 项红；
     **image 前导 `-` 检查置假 → 我自查时发现原本无人能红**（原参数化里没有以 `-` 开头的取值，
     即那条真机逃逸的修复当时是**未被钉住**的），已补 `test_image_may_not_look_like_a_flag`
     并在重跑中确认变红；文件系统根与"必须是目录"两条为**行为等价纵深防御**，
     已各补一条"在其专属场景下可触达"的用例（`sandbox_root` 显式设为根 / 指向根内文件）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第二十三轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第二十一轮 BLOCKING 修复：**宿主暴露面零覆盖** + 负例不完整导致的假绿

- **背景**：第二十一轮 REJECT。审查者独立复现了我自查的 6 条变异（数字吻合），
  但找到一条**严重度更高**的问题，以及我自己修法里的一处"配套删除仍可全绿"。
- **变更**：
  1. **BLOCKING（最高严重度）——宿主暴露面一个断言都没有**。
     `--mount f"type=bind,src={workspace},dst={policy.workdir}"` 是"容器能看到宿主什么"的
     **唯一决定点**，而 `assert_safe` 与 `assert_required_invariants` **都读不到这个串**。
     审查者实测 6 个**单行**变异在 325 条用例下全绿：
     m1 删 `--mount`、**m2 `src={workspace}` → `src=/`（把宿主根以 uid 1000 可读写挂进容器）**、
     m3 `dst=/`、w1 删 `--workdir`、n1 删 `--name`、r1 删 `--rm`。
     即"一次单行改动就能把'只挂副本'变成'挂宿主根'"，且这是**最外层**的安全边界。
     **修法**：新增字面金标用例
     `test_host_exposure_surface_is_pinned_by_literals`（`--mount` 串必须逐字符等于
     `type=bind,src=<副本绝对路径>,dst=<workdir>`，且 `--mount/--workdir/--rm/--name`
     各恰好出现一次）与反向断言 `test_writable_mount_sources_are_never_the_host_root`
     （mount 源必须正好是副本目录，绝不允许 `/`、`C:\` 等）。
     **判据**：6/6 KILLED（此前 6/6 全绿）。
  2. **BLOCKING——负例用"不完整 argv"且无 `match=`，导致配套删除时假绿**。
     `test_invariants_detect_network_override` / `_missing_read_only` / `_root_run`
     三条负例此前各喂一条**残缺** argv 且不校验报错原因，于是清单条目被删后它们改由
     "缺少别的必需参数"过关（`root_run` 当天抛的其实正是
     `缺少必需的安全参数: --memory`，根本没走到 root 检查）。
     **修法**：三条负例改为从**完整** argv（按硬编码锚点拼出）出发、只动被测那一项，
     并加锚定断言（`^--network 必须为 none，实际为 host$`、
     `^缺少必需的安全参数: --read-only$`、`root`）。
  3. 非阻塞一并处理：
     - `_VALIDATOR_NULL_FLAGS` 补**双向集合相等**断言（此前从它去掉 `--read-only`
       只是让对应用例"消失"，rc=0/0 failed）；
     - `NEVER_ALLOWED_TOKENS` 里的 `-v` 补**行为**断言（`assert_safe(["docker","run","-v",…])`
       必须拒绝）——此前它只被清单子集断言覆盖，删掉仍绿；
     - `config.py` 的 `llm_model`/`llm_small_model` 默认值仍为 `deepseek-chat`，
       与 `.env.example` 的 `deepseek-flash` 矛盾（`test_config.py` 还钉着发散值）→ 统一为
       `deepseek-flash`，并新增 `test_code_defaults_match_env_example_template`
       让"代码默认 == 模板"被断言约束；
     - **`pp smoke` 在本机退化为崩溃**：`.env` 把 provider 切成 `deepseek` 后，
       未装 `openai` 时该命令抛 `RuntimeError`、退出码 1——而 README/PLAN 把 `pp smoke`
       当作 P0 的**零依赖门禁**，即"改 `.env` 就能让门禁变红"。
       修法：`smoke` **默认强制 stub provider**（骨架自测语义），要连真实模型须显式
       `--real-llm`；实测退出码恢复 0；
     - 注释/文档口径对齐（`_minimal_argv` 用的是锚点而非派生表；`EMITTED_INVARIANTS`
       头部"参数表由它参数化"已失效，照旧注释对齐会**精确回退**本轮修复）。
  4. **如实登记构造性上限**（§8.5 第 7 条）：仍有 **4 处协同**组合可全绿
     （删实现表条目 + 删测试源条目 + 删锚点字面量 + 把 `len(REQUIRED_INVARIANTS) == 8`
     改成 `== 7`）。这是"锚点本身也要被独立事实钉住"这条链的末端：
     本轮已把可自动发现的空间压到"必须同时改 4 处、且跨越 2 个文件与 3 类断言"，
     但**不声称**已构造性封闭——该上限已写入 §8.5。
- **文件**：`src/patchpilot/sandbox/policy.py`（注释）、`src/patchpilot/core/config.py`、
  `src/patchpilot/cli.py`、`tests/test_sandbox_policy.py`、`tests/test_config.py`、
  `.env.example`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **325 passed + 3 skipped**，0 failed / 0 error
  - 宿主暴露面变异自查 → **6/6 KILLED**（m1/m2/m3/w1/n1/r1，此前 6/6 全绿）
  - 清单类逃逸路线自查 → 6/6 KILLED（双删/三删/只删发射/降级取值/追加条目/删发射）
  - `pp smoke` → 退出码 0（零依赖语义恢复）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第二十二轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第二十轮 BLOCKING 修复：**可写副本**的终结——硬编码锚点 + 唯一源

- **背景**：第十九/二十轮连续 REJECT，指向同一根因：我为"清单被删"加的三张表
  （`REQUIRED_INVARIANTS` 的实现表、测试的 `EXPECTED_INVARIANTS`、硬编码 flag 表）
  全都是**同一事实的可写副本**，只要一起改坏就全绿。审查者逐项实测：
  **三删/四删** `--network`/`--cap-drop`/`--security-opt` → rc=0、0 failed；
  且我上一批声称的"用例仍在、会去 `args.index(flag)` 而失败"**不成立**
  （该结果没进任何断言）。
- **本轮自己动手前先承认一件事**：我先写的"统一到唯一源 `EMITTED_INVARIANTS`"版
  仍**没封闭**——因为发射用例**迭代的就是那张表**，配套删除时"检查项与被检查对象一起消失"。
  我用 6 条逃逸路线的自查脚本抓到了自己（第 1 条 rc=0），才做出真正的修法。
- **变更**：
  1. **唯一源**：测试里只保留**一处**硬编码清单 `EMITTED_INVARIANTS`（含取值），
     `PINNED_INVARIANT_FLAGS`、`EXPECTED_INVARIANTS`、`NULL_VALUED_INVARIANT_FLAGS`
     全部由它**派生**，不再手抄（"没有第二份可删"）。
  2. **真正的锚点**：新增**谁也不派生**的硬编码字面元组
     `REQUIRED_FLAG_LITERALS_FOR_EMISSION`（8 个 flag 写死），
     并用它给 `test_each_required_invariant_is_enforced` **参数化**——
     这样删清单条目的变异表现为"用例变红"而不是"用例消失"。
     另加 `test_default_argv_contains_every_hardcoded_required_flag`
     （默认 argv 必须含这 8 个 flag）、`test_hardcoded_emission_literals_cover_the_manifest`
     （锚点 == 清单，且 `len(REQUIRED_INVARIANTS) == 8` 这条**独立事实**）、
     `test_hardcoded_anchor_matches_the_derived_tables`（断言方向：用锚点比派生表）。
  3. **逃逸路线自查**（副本 + 显式 `PYTHONPATH` + 先断言导入来源在副本内 + 不用 `-x`）：
     **6/6 KILLED**——① 双删（实现表+测试源）rc=1/3 failed ② 三删（+删发射，即
     审查者的 G3/H2 路线）rc=1/5 failed ③ 只删发射 rc=1/15 failed
     ④ 降级 `--cap-drop ALL→None`（表+发射同改）rc=1/15 failed
     ⑤ 追加未知条目 rc=1/36 failed ⑥ 删 `--user` 发射 rc=1/15 failed
     （基线 rc=0。上一版在第①条上 rc=0，即"通道未封闭"，本轮才真正封住。）
  4. 非阻塞一并处理：`.env.example` 与 `config.py` 的 `llm_base_url` 口径统一为裸域名
     （两份模板此前互相矛盾且无断言约束）；`config.py` 的注释更正
     （`get_secret_value()` 有**两处**调用点，`safe_summary()` 那处不泄漏明文）；
     memory 零值 `match=` 补 `$`；`test_pinned_null_invariants_accept_other_legal_values`
     改为真的替换取值再断言（原写法 `assert f(x), "msg"` 断言的是 `None`、恒真）。
  5. **规程补丁（§8.5 第 3 条加严）**：参数表不得派生自任何"可被删小的表"；
     凡"清单类"断言，必须同时具备(a)一条**谁也不派生**的硬编码锚点，
     (b)锚点与实现的**双向**断言，(c)锚点本身也被独立事实（如条数）钉住。
- **文件**：`tests/test_sandbox_policy.py`、`src/patchpilot/core/config.py`、
  `.env.example`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **322 passed + 3 skipped**，0 failed / 0 error
  - 沙箱三模块单测 **225**（policy 193 / workspace_copy 13 / junit 19）
  - 逃逸路线自查 → **6/6 KILLED**（见上；含审查者实测存活的三删路线）
  - `Settings()` 与 `.env.example` 双向一致：`base_url`/`model` 均相等
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第二十一轮复审**
- **提交**：待本轮推送

### [2026-10-16] 模型接入：切到官方 DeepSeek API（`deepseek-flash` / V4.1-Flash）

- **变更**：按用户要求把模型接入从 `stub` 切到官方 DeepSeek API。
  - **凭据存放**：API Key 写入 `.env`（**已在 `.gitignore`，且门禁 `env_not_tracked`
    检查"`.env` 不得被跟踪"**）——这是本仓库约定的唯一凭据入口，`.env.example` 保持空值。
    Key 使 `SecretStr` 承载，`repr`/`str`/`model_dump` 均为掩码，取值只能显式调
    `get_secret_value()`（唯一调用点 `llm.build_llm`）；已复核 `safe_summary()` 不含明文。
  - **模型名**：官方文档的模型名为 **`deepseek-flash`**（= DeepSeek-V4.1-Flash；
    旧名 `deepseek-v4-flash` 已退役但兼容）。1M 上下文 / 384K 输出，支持 thinking 模式、
    tool calls、vision、JSON 输出。
  - **base_url**：`https://api.deepseek.com`（OpenAI 兼容；`.env.example` 原模板带的
    `/v1` 后缀在官方网关下并非必需，改回裸域名）。
  - **价格覆盖**：内置价格表未收录该模型，故在 `.env` 用
    `PP_PRICE_OVERRIDES={"deepseek-flash": [0.15, 0.6]}` 补上（官方 off-peak 的
    cache-miss 输入价与输出价，单位「美元 / 每百万 token」；peak 为其 2 倍）。
    不补这一项，成本就不会参与金额熔断（§6 的四重预算会漏一重）。
- **验证**：
  - `Settings()` 载入实测：`provider=deepseek`、`base_url=https://api.deepseek.com`、
    `model=deepseek-flash`、Key 已配置（`SecretStr('**********')`）、
    `cost(1M in + 1M out) = $0.75`（= 0.15 + 0.6，与官方价目一致）
  - **真机验活**：`GET https://api.deepseek.com/models`（**只读端点，不消耗 token**）
    → HTTP 200，返回 `['deepseek-flash', 'deepseek-v4-pro']`，目标模型名在列表中
  - `git check-ignore -v .env` → `.gitignore:22`；`git ls-files .env` → 空（未被跟踪）
- **说明与风险登记**：
  - 该 Key 是在与本会话的人类对话中明文提供的，**已进入会话记录**；若本会话记录会外传，
    应去 [DeepSeek 平台](https://platform.deepseek.com/) 轮换。仓库侧不会泄露：
    `.env` 被忽略、且门禁会拦"`.env` 被跟踪"。
  - `openai` 依赖仍未安装（属 P3 前置），因此真实**对话**调用尚未跑过；
    本次只验证了凭据有效性与模型名可用性。P3 开工时执行
    `pip install -e ".[llm]"` 即可用 `PP_LLM_PROVIDER=deepseek` 直接调用。
- **文件**：`.env`（**不在摘要内**，未跟踪）、`PLAN.md`
- **审查**：配置变更，随本批一并提交
- **提交**：随本批

### [2026-10-16] 第十八轮 BLOCKING 修复：**双删洗白通道** + prune 冗余守卫未登记

- **背景**：第十八轮 REJECT。审查者确认上一轮 5 个存活变异 **5/5 补杀**（完整套件 rc=1），
  但指出本轮新构件上有两处洞——其中第一处是**目前最严重的一类**：
  "把安全不变量和守住它的网**一起改坏**，套件报全绿"。
- **变更**：
  1. **BLOCKING——双删洗白通道**。`_minimal_argv()` 原本迭代
     `EXPECTED_INVARIANT_FLAGS = tuple(flag for flag, _ in EXPECTED_INVARIANTS)`，
     而 `EXPECTED_INVARIANTS` 是**与实现比对**的那张表。于是"**同时**从实现
     `REQUIRED_INVARIANTS` 与测试 `EXPECTED_INVARIANTS` 删掉 `--network`"
     会让清单相等断言成立、参数化用例**一起消失**，全套 rc=0 零 failed
     （审查者实验 C1）。**修法**：新增**硬编码、不经任何变量派生**的
     `PINNED_INVARIANT_FLAGS: tuple[str, ...]`，参数化与 `_minimal_argv()` 都迭代它；
     另加 `test_pinned_flag_tuple_matches_the_expected_table` 保证两份测试侧表自洽。
     **判决性实验**：重跑 C1 → **rc=1、4 failed**（此前 rc=0），通道封闭。
  2. **BLOCKING——`_prune_stale_basetemps` 的 `not entry.is_dir()` 未登记**。
     我做对了 `_is_writable_dir` 那条（登记为冗余），却在**同一批**代码里漏了结构完全相同的
     这一条：变异删掉它全套仍绿（陈旧普通文件由 `rmtree` 的 `NotADirectoryError`
     经 `except OSError` 吞掉，文件仍在）。**修法**：就地登记为"冗余纵深防御、不被任何用例
     钉住（§8.5 第 5 条）"，**并**补 `test_prune_stale_basetemps_leaves_stale_named_regular_files_alone`
     钉住契约（命名像运行目录的陈旧普通文件必须保留、内容不变）。
  3. 非阻塞一并处理：`NULL_VALUED_INVARIANT_FLAGS` 与字面表用集合相等断言**闭合**
     （否则字面表新增 None 项时该参数不会增长、静默少覆盖）；
     memory 零值的 `match=` 补 `$` 锚定；`PLAN.md:115` 的 P1 行同步为 **12/12** 与
     "第九至十七轮"（此前与 §1 的 216/12/12 自相矛盾）。
- **文件**：`src/patchpilot/core/preflight.py`、`tests/test_sandbox_policy.py`、
  `tests/test_preflight.py`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **318 passed + 3 skipped**，0 failed / 0 error
  - **C1 判决性实验**（副本 + 显式 `PYTHONPATH`，先断言导入来源在副本内）：
     基线 rc=0；双删 `--network` → **rc=1、4 failed**（KILLED）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十九轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十七轮 BLOCKING 修复：新加的两张网各自有一个"非对称面"

- **背景**：第十七轮 REJECT。审查者确认第十六轮两项 BLOCKING 的**主修**成立
  （清单删除/新增双向都有网、`--pids-limit` 守卫真覆盖且真机证实 `0`/`-1` 会让
  `pids.max=max`、`HostConfig.PidsLimit=<nil>` 且 rc=0 无告警——是真实的静默熔断取消），
  但指出**本轮新加的两张网自身各有一个零覆盖的非对称面**。
- **变更**：
  1. **BLOCKING——字面表只钉 flag 名、丢掉期望取值**。变异实测：把
     `("--network","none")`→`("--network",None)`、`("--cap-drop","ALL")`→`None`、
     `("--security-opt","no-new-privileges")`→`None` **三条全部全套全绿**——即"必须为 none/ALL"
     这类校验可被静默删除（另 5 项侥幸被 `test_generated_args_carry_every_required_invariant`
     的字面断言救下，不是这张网守住的）。
     **修法**：字面表升级为含取值的 `EXPECTED_INVARIANTS: tuple[tuple[str, str|None], ...]`
     并与 `REQUIRED_INVARIANTS` **整体**比对；另加反向用例
     `test_pinned_non_null_invariants_really_pin_their_value`（把取值换成合法但不匹配的串，
     必须抛错）与 `test_pinned_null_invariants_accept_any_value`（None 项只受其它规则约束）。
  2. **BLOCKING——`MIN_MEMORY_BYTES` 只钉下界、未钉上界**。变异 6→7 MiB **全套全绿**：
     那组"放行"用例只喂 `assert_required_invariants`，而该函数**不读**这个常量（只判 `> 0`）；
     唯一读它的用例只测低于 6 MiB 的值。**修法**：放行用例改走 `SandboxPolicy(memory=...)`
     构造期（真正读常量的路径），并补"恰好在 6 MiB 上"的 `6291456`/`6291456b`/`6m` 用例。
     上下两向合起来才是真正的"双向"。
  3. **§8.5 残留清理**：`_minimal_argv()` 改按**测试侧字面表**构建（不再从
     `REQUIRED_INVARIANTS` 反推）；`match=` 文案补锚定（`--pids-limit` 加 `^`、
     memory 分支补全文案）；`preflight.py` 的 `path.is_dir()` 就地登记为
     **冗余纵深防御、不被任何用例钉住**（§8.5 第 5 条）。
  4. **日志更正**：本文件上一批条目里"`_is_writable_dir` 补负例**真正钉住** `path.is_dir()`"
     与实测相反（删掉它全套全绿），已按 §8.5 第 5 条改为"钉住的是契约、该守卫是冗余"。
  5. **真机证据落盘**：`.tmp/verify_sandbox_container.py` 新增 C2 段——`--pids-limit 0/-1/0b/空`
     必须被不变量层拒绝；端到端由 **11/11 升为 12/12**。
  6. 文档同步：`PLAN.md` §5 的 P1 行不再把九至十七轮修复挂在 `0e93345` 上
     （该提交只有当时的 5 项不变量）；沙箱单测数 **216**（policy 184 / 13 / 19）；
     README 两处改为 12/12。
- **文件**：`src/patchpilot/core/preflight.py`、`tests/test_sandbox_policy.py`、
  `README.md`、`PLAN.md`（另有 `.tmp/` 脚本，不在摘要内）
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **314 passed + 3 skipped**，0 failed / 0 error
  - **按 §8.5 做了变异自证**（副本 + 显式 `PYTHONPATH`，先断言导入来源在副本内）：
    7 个变异 → **7/7 KILLED**，含本轮修复针对的三条取值降级、清单删项、
    下限上抬 7 MiB / 下调 5 MiB、`--pids-limit` 守卫整关
  - 真机端到端 → **12/12**（新增 C2 段），日志落盘 `.tmp/verify_sandbox_container.out`
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十八轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十六轮 BLOCKING 修复：用例参数表**就是**待守对象（清单删除方向零约束）

- **背景**：第十六轮 REJECT（摘要一致）。审查者逐项删除 `REQUIRED_INVARIANTS` 的条目后实测：
  删 `--network`/`--read-only`/`--cap-drop`/`--security-opt`/`--pids-limit` 时
  **rc=0、无任何 failed**，只是对应的参数化用例**消失**了（`gone_ids` 恰为该用例）。
  即我上一批"8/8 KILLED"的自述**不成立**：实际只补杀了 3/8，且那 3 项是靠别处的
  硬编码 `args.index("--memory")` 偶然兜住的，不是靠我新写的那条用例。
- **根因（我的第三层同类错误）**：`test_each_required_invariant_is_enforced` 的参数表写成
  `[flag for flag, _ in REQUIRED_INVARIANTS]`——**参数表就是待守对象本身**。
  删掉清单条目后，pytest 在导入期重新读清单，被删项的用例**直接消失而不是变红**，
  于是该用例对"清单被删项"零约束。这是"用枚举对抗等价类"的第三次复发形态：
  前两次是字段层/不变量层的**字面量表**，这次是**参数表与被测对象同源**。
- **变更**：
  1. 新增**独立钉住**的字面表 `EXPECTED_INVARIANT_FLAGS`（8 项，写在测试里、与实现解耦），
     并加 `test_required_invariants_manifest_is_exactly_as_pinned` 做**双向**约束
     （实现增删改任一项都必须同步改测试）。
  2. `test_each_required_invariant_is_enforced` 改为按**字面表**参数化。这样即使清单少了某项，
     用例依然存在、会去 `_minimal_argv()` 里 `args.index(flag)` 而**失败**——两个独立网兜住。
     等价模拟逐项验证：**8/8 KILLED**（"清单钉死用例红=True" 且 "逐项用例红=True"）。
  3. **`--pids-limit` 正值守卫补覆盖**：`0/0b/00/0g/-1/-0/" 0"/"0 "/+1/""` 逐个断言拒绝、
     `512` 断言放行（第十六轮第二项 BLOCKING：该分支此前零覆盖，
     把 `<= 0` 改成 `== 0`/`if False:` 套件都全绿）。
  4. `MIN_MEMORY_BYTES` 边界**双向**钉住：`6m/6m 整数值/7m/104857600/1g` 构造期放行、
     `6291455/6291455b/5m/1m/6m0` 构造期拒绝。
     **更正**：第一版这组"放行"用例只喂 `assert_required_invariants`，而该函数**不读**
     `MIN_MEMORY_BYTES`（只判 `> 0`），所以把常量 6→7 MiB 全套仍绿——"双向"声明当时不成立
     （第十七轮 BLOCKING）。现已改走 `SandboxPolicy(...)` 构造期这条真正读常量的路径。
  5. `_is_writable_dir` 补"路径是普通文件"负例，钉住的是**契约**（文件不被当作可写目录）。
     **更正**：本条原写"真正钉住 `path.is_dir()` 守卫"是**错的**——变异自证证明删掉该守卫后
     全套仍绿（探针以 `NotADirectoryError` 收场，行为等价），属§8.5 第 5 条要求登记的
     **冗余纵深防御、不被任何用例钉住**。代码注释与测试 docstring 均已按此措辞。
  6. 文档按实测更新：沙箱单测数 **206**（policy 174 / workspace_copy 13 / junit 19）；
     README 首屏更正为"第九至十六轮修复随**本次 P1 提交**发布"——原文写"随 `0e93345` 推送"
     与实测矛盾（`git show 0e93345:...policy.py` 只有 5 项不变量、无 `_require_pairs`/
     `_validate_memory`，九至十六轮修复其实一直在暂存区）。
- **文件**：`src/patchpilot/sandbox/policy.py`、`tests/test_sandbox_policy.py`、
  `tests/test_preflight.py`、`README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **304 passed + 3 skipped**，0 failed / 0 error
  - 序列等价模拟（删清单项 → 按字面表参数化执行）→ **8/8 KILLED**
  - **按新增的 §8.5 规程做了变异自证**（副本 + 显式 `PYTHONPATH`，先断言导入来源在副本内）：
    7 个变异 → **5 KILLED / 2 等价变异**。存活的两个已逐个归因并**如实登记为等价变异**
    （不是覆盖缺口）：`M-pids-branch-sign`（`isdigit()` 已排除负号，`<= 0` 与 `== 0`
    行为等价——已顺手改成 `== 0` 以免误导读者）、`M-writable-isdir`
    （删掉 `is_dir()` 后探针以 `NotADirectoryError` 收场，返回值仍是 `False`——用例钉的是
    "文件不被当作可写目录"这个契约，不得据此声称该守卫"已覆盖"）。
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十七轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十五轮 BLOCKING 修复：存在性不变量的"假守门用例"（M7/M7b 实际存活）

- **背景**：第十五轮 REJECT，摘要与审查者复算一致，唯一阻塞项是
  **我上一批声称"补杀了 M7"不成立**。它用变异实验证明：把 `("--memory", None)` 从
  `REQUIRED_INVARIANTS` 删掉后**全套仍然全绿**。
- **根因（两层，都是我的错）**：
  1. 用例里的 `match="缺少必需的安全参数: --memory"` 是**未锚定**的 `re.search`：
     删掉 memory 项后实际缺的是 `--memory-swap`，文案
     `"缺少必需的安全参数: --memory-swap"` **前缀命中了 `--memory`** → 用例假绿；
  2. 那条用例只摘 `--memory` 一项，其它缺失项可以**代偿**，所以它从来没有真正验证过
     "逐项存在性"。
  这正是我自己在本轮开头总结的"**用枚举对抗等价类**"的另一种形态：
  以为写了守门用例，其实用例的空洞让防线无人看守。
- **变更**：
  1. 新增 `_minimal_argv()`：按 `REQUIRED_INVARIANTS` **反推**一条完整的安全 argv，
     并用 `test_minimal_argv_is_complete()` 先证明它本身完整（否则"摘一项"的用例
     会被别的缺失项代偿）。
  2. `test_each_required_invariant_is_enforced` **对每一项参数化**（8 项），
     每次只摘这一项，断言 `match=rf"缺少必需的安全参数: {flag}$"`（**加 `$` 锚定**）。
     已用等价模拟逐项验证：8/8 全部 KILLED（删任一项都精确报出该项缺失）。
  3. 顺带补齐 `--pids-limit` 的存在性用例（`M7b` 此前同样存活）。
  4. 非阻塞一并处理：`README.md:176` 的"22 类"确实还没改（上一批只改了 :67），现已改 **30 类**；
     `env`/`labels` 传 `42`/`set()`/`(None,)` 此前抛裸 `TypeError: cannot unpack non-sequence`，
     补用例并确认已被 `_require_pairs` 收敛；`memory` 的 `isascii()` 与 `value <= 0`
     登记为**冗余纵深防御**（后者直接删除，前者保留但注明不被任何用例钉住）；
     `_prune_stale_basetemps` 的 `entry.is_dir()` 同类冗余，保留并注明
     （同名普通文件会被 `rmtree` 的 `OSError` 吞掉）。
- **文件**：`src/patchpilot/sandbox/policy.py`、`tests/test_sandbox_policy.py`、
  `README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **282 passed + 3 skipped**，0 failed / 0 error
  - M7/M7b 等价模拟：删 `--network`/`--read-only`/`--cap-drop`/`--security-opt`/`--user`/
    `--memory`/`--memory-swap`/`--pids-limit` **8/8 全部 KILLED**（锚定匹配 True）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十六轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十四轮 BLOCKING 修复：不变量层的零值黑名单（同一类缺陷在同一层复发）

- **背景**：第十四轮 REJECT，摘要与审查者复算值一致（不是摘要问题），唯一阻塞项是
  **`assert_required_invariants` 的零值检查仍是字面量表**。真机实测
  `--memory 0G`/`00`/`0000`/`0B` 全部 rc=0、无告警、`docker inspect` 得
  `HostConfig.Memory=0`——即上限同样被静默取消。而我又把它登记为"资源上限不得被 0
  静默取消"的防线，**登记与行为不符**；且删掉表里任意一项（`M9`/`M10` 变异）全套都不会红。
- **变更**：
  1. **BLOCKING**：删掉字面量表，改为调用**与 `_validate_memory` 同一套解析器**
     `_parse_size_bytes`——"能解析出正值才算合法"；`--pids-limit` 走 `isdigit() and > 0`。
     新增守门用例：`0/0b/0B/0k/0K/0m/0M/0g/0G/00/0000/0x10/空串` 共 13 个零值等价写法，
     以及**存在性**用例（`M7`：从 `REQUIRED_INVARIANTS` 删掉 `("--memory", None)` 此前无人能红，
     因为所有用例都自带 `--memory`）。
  2. **校验对象 = 生成对象**：`memory` 此前用 `strip()` 后的值检查、却把**原始**值写进 argv，
     于是 `"4G "`/`"\t4g"` 能过构造期、到 docker 才 125 失败。现在直接拒绝含空白的取值。
  3. **类型面收尾（容器类型）**：新增 `_require_pairs`——`env`/`labels` 传 dict/list/str
     此前抛**裸 `ValueError: not enough values to unpack`**；`command`/`container_name`/
     `workspace` 在生成期做类型检查（`command="python -V"` 会按字符展开成 argv、
     `command=[None]` 抛裸 `AttributeError`、`container_name=1` 抛裸 `TypeError`）。
  4. **诚实更正我上一批的错误声明**：`M-workdir-abs` **仍然存活**——审查者的归因实验证明
     "必须绝对路径"规则被 workdir 子树规则**完全覆盖**，没有任何输入只被前者拦下。
     注释与用例措辞已改正为"冗余纵深防御"，PLAN 对应条目的"真正钉住"也一并更正。
  5. **`_prune_stale_basetemps` 补杀 `M19`/`M20`**：原来 `entry == current` 与
     `startswith("run-")` 两个守卫**不可达**（测试里所有目录都是新鲜 mtime，先被 mtime 判据跳过）。
     现在把 `current`/非 run 目录的 mtime 也推旧，并新增边界用例（阈值内外各 1 秒、
     父目录不存在/是文件）；命名收窄为精确模式 `run-\d+-[0-9a-f]{6}`，
     不再误删同一 `--basetemp` 下恰好也叫 `run-*` 的他人目录。
  6. 文档数字校正：`PLAN.md` §1 的沙箱单测数改为实测 **171**（policy 139 / workspace_copy 13 /
     junit 19），并就地更正此前两处错误构成（143 = 113/12/18 → 121/13/19 → 现 171）；
     README 的"22 类非法字段"改为实测 **30 类**。
- **文件**：`src/patchpilot/sandbox/policy.py`、`src/patchpilot/core/preflight.py`、
  `tests/test_sandbox_policy.py`、`tests/test_preflight.py`、`README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **268 passed + 3 skipped**，0 failed / 0 error
  - 不变量层逐值复核：13 个零值等价写法全部拒绝、4 个合法值放行、缺失 `--memory` 被拒
  - 真机脚本 `rejected_fields` 实测 **30 项**，端到端 **11/11**
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十五轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十三轮 BLOCKING 修复：`memory` 可静默取消内存上限 + 4 个存活变异

- **背景**：第十三轮 `code-reviewer` 给出 REJECT，唯一阻塞项是
  **`memory` 是本模块唯一完全没有校验的资源上限字段**（其余 6 条非阻塞项经它复核成立）。
- **变更**：
  1. **BLOCKING——`memory` 可被合法字面量静默取消**。真机判决性证据：
     `memory="0"` / `"0b"` 构造期放行 → `--memory 0 --memory-swap 0`，
     而 `assert_safe`/`assert_required_invariants` 都不覆盖 `--memory` →
     `docker run` **rc=0、无任何告警**，`docker inspect` 得
     `HostConfig.Memory=0 MemorySwap=0`（对照默认 `4g` = 4294967296）。
     同模块对 `cpus=0`/`pids_limit=0`/`timeout_s=0` 都明确拒绝，唯独内存可无声抹掉——
     属"资源熔断失效 / 静默错误结果"。
     **修法**：新增 `_validate_memory`（拒非字符串/布尔/空串/非 ASCII/无法解析/为 0/小于
     `MIN_MEMORY_BYTES`=6 MiB）；`REQUIRED_INVARIANTS` 纳入
     `--memory`/`--memory-swap`/`--pids-limit`，并在校验里显式拒绝 `0` 与 `0b/0k/0m/0g`。
     补 3 类用例：非法 `memory` 参数化（含 `1`/`True`/`None`）、合法尺寸放行
     （含"swap 必须与 memory 同值"）、argv 里出现 `--memory 0` 时必须被不变量拦下。
  2. **类型面收尾**：新增 `_require_str`，在 `endswith`/`startswith`/`strip` 之前检查
     `image`/`workdir`/`run_as`/`tmpfs_size`/`memory`，把裸 `AttributeError` 收敛成
     `SandboxPolicyError`（此前 `image=1`、`workdir=None`、`run_as=1`、`hostname=123`、
     `env` 取值 `None` 都会抛裸异常）；`tmpfs_size` 的字符集补 `\x00`
     （此前 `"512m\x00"` 会在执行前抛裸 `ValueError`）。
  3. **四个存活变异补杀**：
     - `M-workdir-abs`：**绝对路径规则被子树规则完全覆盖**（任何相对路径都被
       `workdir` 子树检查拒绝），故删掉它没有断言会红——我上一批在注释里声称
       "补杀了 M10" 对这条**不成立**，已改正措辞，并新增 `"relative/path"` 用例
       把该分支真正钉住；
     - `M-reqnum-bool`：补 `cpus=True` / `timeout_s=False` 用例；
     - `M-hostname-type`：补 `hostname=123` 用例（并注意 `hostname=None` 是**合法**取值）；
     - `M-imguard-logic`：镜像位置守卫此前只断言"源码里有那句话"，现补**行为负例**
       （monkeypatch `assert_safe` 人为顶掉镜像位，断言守卫真的抛错）。
  4. 其它非阻塞：`sandbox/__init__.py` 里指向不存在的 `docs/adr/` 改为
     "详见 `.tmp/p1_sandbox_design.md`（待 P1 收尾后并入 `docs/adr/`）"；
     `PLAN.md` §1 第 2 项的"143+"改为实测 **153**；
     新增 `_prune_stale_basetemps`：回收 basetemp 下**陈旧**的 `run-*` 目录
     （每运行一个、长期累积 100+），只删非本次且 mtime 超过 1 小时的，
     绝不碰并发中的目录——并配用例断言"本次保留/陈旧删除/并发保留/非 run-* 保留"。
- **文件**：`src/patchpilot/sandbox/policy.py`、`src/patchpilot/core/preflight.py`、
  `src/patchpilot/sandbox/__init__.py`、`tests/test_sandbox_policy.py`、
  `tests/test_preflight.py`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **249 passed + 3 skipped**，0 failed / 0 error
  - 字段级复核：15 类非法类型/取值全部抛 `SandboxPolicyError`（含 `memory=0`、`cpus=True`、
    `image=1`、`tmpfs_size="512m\x00"`）；合法 `4g/512m/104857600/2G` 正常生成
  - 真机脚本 `rejected_fields` 扩到 **29 项**（新增 `memory=0`/`0b`/`1`/过小、tmpfs 逗号等）
    —— 第十四轮复审复算为 **30 项**（该表随后又加了一项），以 30 为准。
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十四轮复审**
- **提交**：待本轮推送

### [2026-10-16] 第十二轮复审的 8 条非阻塞项收尾（含一条真机实证的 tmpfs 注入）

- **背景**：第十二轮 APPROVE 时列出 8 条非阻塞项。其中一条是**真机实证的安全缺口**，
  其余是类型面与文档一致性。按"发布已批准的字节、非阻塞项记入下一条日志"的协议，
  本批在 `0e93345` 之后单独提交。
- **变更**：
  1. **`tmpfs_size` 未做逗号校验（真机实证）**：`tmpfs_size="512m,exec"` 构造期放行，
     `docker inspect` 显示 `/tmp:rw,noexec,nosuid,size=512m,exec`——重复选项**后写覆盖**，
     内核取 `exec`，容器内脚本可执行（基线 `512m` 下被 `noexec` 拦下）。
     即"只读 rootfs + tmpfs"承诺里的 `noexec` 可被字段值静默取消（未获 root、无宿主挂载、
     无网络，故非逃逸）。现按与 `workdir` 相同的口径拒绝逗号/空白，并新增
     "tmpfs 选项串恰好等于预期形态"的机器可断言用例（旧的"argv 里出现 noexec"字面断言
     抓不到覆盖）。
  2. **类型面收敛**：新增 `_require_number` / `_require_int`，在**比较之前**做类型检查，
     使 `cpus="x"` / `pids_limit=1.5` / `timeout_s="1"` / `stop_timeout_s="--privileged"`
     一律抛 `SandboxPolicyError`（此前抛裸 `TypeError`/`ValueError`，而调用方只捕获前者）；
     `bool` 是 `int` 子类，整数位字段显式排除（`stop_timeout_s=True` 此前会变成
     `--stop-timeout 1`）。
  3. **`hostname` 补校验**：拒绝前导 `-`（可能被当 flag）与 `/`（无效 hostname）。
  4. **`_validate_run_as` 死分支清理**：`isdigit()` 已排除负号，`value < 0` 永不触发；
     同时补 `":"`/`"-1"`/`"+1"`/`"1e3"` 这类 **ASCII 非数字** 用例
     （此前只有非 ASCII 用例，而它们被 `isascii()` 子句拦下，`isdigit()` 子句无断言）。
  5. `NEVER_ALLOWED_OVERRIDES` 是**装饰性常量**（`assert_safe` 不读它），
     原测试只断言"该常量含某些字符串"，对行为无约束；改为对
     `assert_safe(["docker","run","--pid=host","img"])` 等**行为**断言。
  6. 生成期镜像位置断言的措辞更正：不再声称"构造路径不可达"——构造值确实能操纵镜像位/
     命令尾（例如 `image="python:3.12-slim "` 带尾空格），但 docker 会以 125 fail-closed；
     现按"纵深防御"登记并补一条可达性用例。
  7. 文档口径统一：README 首屏与 F8 行、PLAN §1 第 2 项与 §5 的 P1 行，原写"原型 20/20、
     正在迁入 `src/`"，与现状（已迁入、219 passed + 3 skipped、真机 11/11）矛盾，已同步。
- **文件**：`src/patchpilot/sandbox/policy.py`、`tests/test_sandbox_policy.py`、
  `README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **219 passed + 3 skipped**，0 failed / 0 error
  - 字段级复核：`tmpfs_size="512m,exec"`、`hostname="-x"`、`stop_timeout_s=True`、
    `cpus="x"`、`run_as=":"` 等全部构造期抛 `SandboxPolicyError`（非裸 TypeError）
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十三轮复审**（本批 8 条非阻塞项修复）
- **提交**：待本轮推送

### [2026-10-16] P1 批次发布完成：commit `0e93345` 已推送 `origin/main`

- **变更**：第十二轮 `code-reviewer` 给出 **APPROVE**（`SCOPE: WORKTREE`，
  `TREE_DIGEST = fa6a097a81586d871c55d02a2fb4312c`）后，按门禁协议提交并推送。
  提交内容 = 第八轮 4 条非阻塞项收尾 + **P1 沙箱纯逻辑层迁入 `src/`** + 第九至十二轮
  对抗性审查的全部安全修复（参数面改类型化字段、`.git` 凭据剥离补全、
  `junit` 不可信输入闸门、门禁并发与反方向校验等）。
  - 提交前复算 `pp check --json` 的 `tree_digest` 与 APPROVE 引用值**逐字符一致**
    （`fa6a097a…`，`ok=True files=42 checks=10 fail=[]`），`git diff --stat` 为空。
  - 按 §8.3 自指约束，commit hash 记入**本条新日志**，不回填任何已批准条目。
- **文件**：12 个变更文件，`2529 insertions(+) / 39 deletions(-)`，共 42 文件。
- **验证**：
  - `git log --oneline` → `0e93345`（父提交 `ee4d80d`）
  - `git rev-parse HEAD` → `0e9334540f515a304a41f5852e5ca7e0bdcb9a0f`
  - `git ls-tree -r --name-only HEAD` → 42 个文件
  - `git push origin main` → exit=0，`ee4d80d..0e93345  main -> main`
  - `git ls-remote origin` → `0e9334540f515a304a41f5852e5ca7e0bdcb9a0f  HEAD` /
    同值 `refs/heads/main`（远端与本地一致）
  - 推送前基线：`ruff` 全绿；`pytest -q -rA` → **193 passed + 3 skipped**，0 failed/error；
    真机 Docker 端到端 `.tmp/verify_sandbox_container.py` → **11/11**；
    摘要连续两次 `pp check` 逐字符相同；42/42 文件 `i/lf w/lf`（工作区 CRLF 文件数 0）
- **审查**：第十二轮 **APPROVE**（审查者独立复算摘要一致；其真机字段注入矩阵 62 组
  未发现任何让容器拿到 root / 宿主 bind mount / 非锁定镜像 / 丢首元素命令的路径；
  第十一轮点名的存活变异 M4/M6/M8/M10/M12/M28/M32 已全部补杀）
- **提交**：`0e9334540f515a304a41f5852e5ca7e0bdcb9a0f`（`main`，已推送 `origin/main`）

### [2026-10-16] 第十一轮复审：采纳结构性改法——删除裸 `extra_args`，参数面改类型化字段

- **背景与决策依据**：第十轮修完的 3 项阻塞（悬空取值 / 未覆盖的新规则 / `*.git` 命名）
  经审查者逐条真机复核确已修复，但同一轮又指出：**"允许清单 + 跨 token 状态机"这条路不能收敛**，
  并给出结构性判断——四轮里每轮都被同一类缺陷的新拼写击穿（粘连 → 布尔捆绑 → 悬空取值 →
  "flag 紧跟 flag"），其中"取值/位置错位"**原理上不可检**：某个 flag 的取值位置被前一个
  token 吃掉后，argv 层面再也分不清"用户想要 `--init`"与"用户在给 `-e` 传值"。
  审查者推荐 **(a) 完全去掉裸 `extra_args`，改显式字段**。我采纳。
- **变更**：
  1. **`SandboxPolicy.extra_args` 字段整体删除**，改为类型化字段：
     `init: bool` / `interactive: bool` / `tty: bool` / `labels: tuple[tuple[str, str], ...]` /
     `hostname: str | None` / `stop_timeout_s: int | None`。
     随之**消失**的代码与概念：`SAFE_EXTRA_FLAGS`、`_EXTRA_FLAGS_WITH_VALUE`（含重复定义那份）、
     `_BOOLEAN_FLAGS/_BOOLEAN_VALUES`、`_space_terminated_prefix`、`_validate_extra_arg(s)`、
     `_validate_flag_value`，以及"拒绝拼写表"这个概念本身——**状态机不复存在，也就没有下一代拼写**。
  2. 新增字段全部配**构造期校验**：`_validate_hostname`（非空、无空白/逗号）、
     `labels`/`env` 的名与值不得含空白、`stop_timeout_s` 非负。
  3. `build_run_args` 改为按字段拼装；布尔项只写 flag 本身（不产生 `--init=true`）。
     生成期的镜像位置断言**保留为纵深防御**，并在测试里**如实登记为"构造路径不可达"**
     ——不再声称它是"已被覆盖的防线"（第十一轮的诚实要求）。
  4. `run_as` 限定 **ASCII 数字**：Python `int()` 会接受全角/阿拉伯-印度数字，
     docker 却认不出（`unable to find user`），与 docstring 的"数值 uid/gid"口径不符。
  5. **`.git-credentials` 与嵌套 git 目录补全**（第十一轮实测的泄漏路径）：
     `vendor/.git-credentials`、`nested/.git/.git-credentials`、`deps/lib.git/config`、
     `vendor/.git/modules/sub/hooks` 此前都能进副本。现在判定统一为
     "任意祖先段是 git 仓库目录（`.git` 或以 `.git` 结尾）"，配置类按 basename
     （新增 `git-credentials`）、`modules`/`worktrees` 目录按"git 目录之下"判定
     （不再只匹配顶层 `.git/modules`）。
  6. **`.tmp/verify_sandbox_container.py` 重写**：第十一轮用 AST 抓到该脚本
     `run`/`main` **各定义两次**、后者覆盖前者（直播的是陈旧那份：只有 4 项注入、
     会 `docker pull`、超时后不清理）——即"日志里的证据不可复现"。
     现在：单一定义（AST 自检无重复）、不 `docker pull`（`image inspect` 预检 + `--pull=never`）、
     注入面改为**字段级非法值**（22 项）、Docker 缺失 skip 而隔离失效 fail。
  7. 补上变异实验暴露的覆盖缺口：重复 `--user`（用两次**合法**值以便真正触发 `len>1` 分支）、
     `workdir` 逗号检查（`/workspace/,readonly` 这类只有逗号规则能拦）、
     复制中途失败的回滚分支、`parse_junit_text` 的内存入口体积闸。
- **文件**：`src/patchpilot/sandbox/policy.py`、`src/patchpilot/sandbox/workspace.py`、
  `tests/test_sandbox_policy.py`、`tests/test_workspace_copy.py`、`tests/test_junit.py`、
  `README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **193 passed + 3 skipped**，0 failed / 0 error
  - 真机 Docker 29.5.2 端到端 → **11/11**（uid=1000、宿主目录 NOT-SEEN、rootfs Errno 30、
    /tmp 可写、网络 Errno 101、22 类字段级非法值全部构造期拒绝、超时 8.2s 截断且无残留），
    日志落盘 `.tmp/verify_sandbox_container.out`；AST 自检确认脚本无重复定义
  - `.git` 凭据剥离复算：`vendor/.git-credentials`、`nested/.git/.git-credentials`、
    `deps/lib.git/config`、`vendor/.git/modules` 全部剥离并记录，**副本内不再有任何 `ghp_`**，
    业务文件（`app/config`、`vendor/lib.py`）保留
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十二轮复审**（结构变更 + 7 项同批修复）
- **提交**：待本轮推送

### [2026-10-16] 第十轮复审 REJECT（3 项 BLOCKING）修复：悬空取值 / 未覆盖的新规则 / `*.git` 命名

- **背景**：第九轮第二轮的 4 项阻塞（布尔短选项捆绑、嵌套 `.git`、两处零覆盖）已修完并由审查者
  逐条真机复核通过（`-iu0:0`/`-iv…` 在原始 docker 上确实产出 `RAWUID 0` / 宿主挂载，
  证明修法必要），但同一轮审查又发现 3 项新问题。本批按更严格者全部处理。
- **变更**：
  1. **BLOCKING——`extra_args` 里的"悬空取值"会把镜像名吃掉**。真机判决性观察：
     `SandboxPolicy(image="python:3.12-slim", extra_args=("-e",))` 配
     `command=["python:latest", ...]` → `--pull=never` 下 **exit=0 且跑的是 python:latest（3.15.0）**，
     命令还静默丢掉首元素，而 `assert_safe` 与 `assert_required_invariants` **全部通过**。
     即"镜像锁定"与"安全语义由 build_run_args 独占"两条承诺可被**单个 extra_args 元素**击穿。
     **修法**：`_validate_extra_args` 循环结束后校验 `pending_value_for`（悬空即拒绝，
     含中间被另一个 flag 打断的情形）；`build_run_args` 末尾加**结构不变量**——
     断言 `args[-(len(command)+1)] == policy.image`。`--workdir/-w` 一并移出允许清单
     （workdir 是策略的一等字段，见 `_validate_workdir`；否则 `--workdir=/` 会绕过子树限制）。
  2. **BLOCKING——本轮新增的 `workdir` 子树规则与长选项严格性零测试覆盖**。
     审查者的变异实验：把子树规则改成 `if False:`（M8）、把 `_space_terminated_prefix`
     末行改成 `return True`（M9），套件**仍然全绿**。**修法**：补
     `test_workdir_must_stay_inside_allowed_subtrees`（`/`、`/etc`、`/etc/passwd`、
     `/workspace/../etc`、`/var`）与 `test_workdir_accepts_allowed_subtrees`；
     补 `test_long_flag_prefix_matching_is_strict`（`--env-file=/etc/passwd`、`--label-file=x`、
     `--workdirx`、`--initx`、`--init=maybe`、`--tty=yes`）——
     其中 `--env-file` 若被放宽会让 docker 去读**宿主文件**当容器环境变量。
  3. **BLOCKING——`*.git` 命名（bare/mirror）的仓库仍被复制**。真机证据：
     `deps/lib.git/config`（明文 `ghp_` 令牌）与 `deps/lib.git/hooks/pre-commit` 进了副本——
     与上一轮**同一类缺陷**，只是命名变体（`foo.git` 而非 `foo/.git`）。
     **修法**：`_is_git_dir_name()` 同时认"== `.git`"与"以 `.git` 结尾"；
     `_is_git_hooks_dir()` 覆盖 `<gitdir>/hooks`、`foo.git/hooks`、
     以及**子模块**的 `vendor/.git/modules/sub/hooks`。**刻意不整棵丢弃 `*.git`**
     （那会误删业务数据），只按 basename 剥配置类文件与 hooks 目录。
     取舍已写明：段名以 `.git` 结尾即视为仓库目录，**假阳性只代价一个文件 + 一条记录**，
     而假阴性是把宿主凭据交给不可信沙箱。
  4. 非阻塞一并处理：`_is_writable_dir` 先判存在性再写探针（原 `mkdir(parents=True)`
     会把"目录不存在"自己修好再报成功）；**拒绝拼写表收敛为单一来源**
     （`REJECTION_SPELLINGS` 定义在 `tests/test_sandbox_policy.py`，真机脚本
     `.tmp/verify_sandbox_container.py` 通过 `sys.path` 导入同一张表——两处清单此前已分叉），
     现共 **29 种拼写**；`--init=maybe`/`--tty=yes` 这类非法布尔值改在构造期拒绝
     （原为运行期 `strconv.ParseBool`）；`workdir` 拒绝 `..` 回溯段；
     README 的 P1 行与 PLAN 编号同步（单测数改为实测的 143）。
- **文件**：`src/patchpilot/sandbox/policy.py`、`src/patchpilot/sandbox/workspace.py`、
  `src/patchpilot/core/preflight.py`、`tests/test_sandbox_policy.py`、
  `tests/test_workspace_copy.py`、`tests/test_preflight.py`、`README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **238 passed + 3 skipped**，0 failed / 0 error
    （三个沙箱模块 143 项：policy 113 / workspace_copy 12 / junit 18）
    —— **该构成数字有误**：第十四轮复审按 `--collect-only` 复算当时为
    policy 121 / workspace_copy 13 / junit 19 = 153；本批后又增至
    policy 139 / workspace_copy 13 / junit 19 = **171**。以最新实测为准。
  - 真机 Docker 29.5.2 端到端 `.tmp/verify_sandbox_container.py` → **10/10**，
    **29 种注入拼写全部在构造期被拒**；日志落盘 `.tmp/verify_sandbox_container.out`
  - `pp check --json` ×2 → 见本批冻结自证（摘要由审查者复算）
- **审查**：**待第十一轮复审**（本批 = 3 项阻塞 + 7 项非阻塞，集中在 `sandbox/` 与 `preflight`）
- **提交**：待本轮推送

### [2026-10-16] 收尾第八轮复审的 4 条非阻塞项（P0 发布后第一批）

- **背景**：第八轮审查者明确要求"这 4 条记入**下一条**日志，不得回填已批准条目"
  （回填会使已批准摘要作废）。本条即该条目；`0ad9ea8` 的批准不受本批影响。
- **变更**：
  1. **非阻塞 1——收集期报错进不了报告**。`collect_pytest_scope` 已返回四元组，
     但 `_check_pytest` 把它截断成三元组，`collect_errors` 只活在 detail 文案里，
     机器消费不到。现在：新增 `PreflightReport.pytest_collect_errors` 字段并接线；
     引入类型别名 `PytestScope = tuple[list[str], list[str], list[str], list[str]]`，
     `_check_pytest` / `collect_pytest_scope` / `run_preflight` 的注解与解包统一到它——
     这个"形状前后不一致"正是我自查时踩过的同一个坑，别名是结构性防复发。
  2. **非阻塞 2——`environment_issue` 仍在整段输出里做字符串匹配**。
     现在只认**以异常类型开头的行**（`PermissionError:` / `WinError 5`）与
     `INTERNALERROR`：断言失败打印的是 `AssertionError`，即使内容里提到这些词也不会命中。
  3. **非阻塞 3——`os.environ` 进程级副作用**。`_check_pytest` 里的
     `os.environ.update(_git_climb_env(root))` **已删除**：界标改为只经
     `extra_env=_pytest_env(root)` 传给 pytest 子进程（收集与执行两处都传），
     本进程环境不再被改写，行为不再依赖执行顺序。
  4. **非阻塞 4——`--basetemp` 文案**：核对结论是**无需修改**。
     `git show 0ad9ea8:src/patchpilot/cli.py` 显示已发布的字节里就是
     "pytest 临时目录的**父目录**（每次门禁运行会在其下建独占子目录 run-<pid>-<hex>）"；
     审查者读到旧措辞是时序问题（它读的是我改动前的快照）。
  5. 新增两条回归用例：
     `test_report_carries_pytest_collect_errors`（真正的收集期错误——
     **导入不存在的模块**——必须出现在 `pytest_collect_errors` 字段里；
     fixture 里特别注明"模块级 `pytest.skip(allow_module_level=True)` 不会产生 ERROR 行，
     用那种写法测不到这条路径"，这是我第一版夹具失败后才确认的事实）、
     `test_assertion_text_mentioning_access_denied_is_not_reclassified`
     扩展为同时覆盖 `access is denied` 与 `PermissionError: [WinError 5]` 两种断言文案。
- **同批附带：P1 沙箱纯逻辑层迁入 `src/`（step 1+2）**，按 `.tmp/p1_staging/MIGRATION.md`：
  1. `sandbox_policy.py` → `src/patchpilot/sandbox/policy.py`、
     `junit.py` → `sandbox/junit.py`、`workspace_copy.py` → `sandbox/workspace.py`
     （模块内容逐字节复制后仅改 import 与 docstring：`from sandbox_policy import is_link_like`
     → `from patchpilot.sandbox.policy import is_link_like`）；
  2. `sandbox/__init__.py` 从占位 docstring 改为真正的导出面（**16 个公开名** + `__all__`）；
  3. 三个离线验证器改写为 pytest：
     `tests/test_sandbox_policy.py`（含 **Windows junction** 分支：先断言
     `is_symlink() is False` 与 `is_link_like() is True` 作为前提自证，
     再断言 `find_escaping_links` 恰好报 1 条且不递归展开；非 Windows 用 symlink 覆盖同一逻辑）、
     `tests/test_junit.py`、`tests/test_workspace_copy.py`。
     改写中发现并修正了我自己的一处测试 bug：`_args()` 里 `command or [...]` 会把
     **空命令**（"拒绝空命令"用例的输入）替换成默认值，使该断言永远测不到真实分支。
- **第九轮复审 REJECT（3 项 BLOCKING，均为真机 Docker 判决性证据）→ 已修**：
  1. **短选项粘连写法整体击穿"拒绝清单"**：`extra_args=("-v<host>:/mnt/host",)` 与
     `("-u0:0",)` 构造通过、生成通过、`assert_safe`/`assert_required_invariants` 双双放行；
     审查者在 Docker 29.5.2 真机上用 `build_run_args` 的原样 argv 执行，得到
     `id -u` = **0** 与 `ls /mnt/host` 列出**宿主目录**。
     根因是 `arg.split("=", 1)[0]` 的精确比对拿到的是整个粘连串。
     **修法**：改**允许清单**（`SAFE_EXTRA_FLAGS`，默认拒绝），并新增逐项消费取值的
     `_validate_extra_args`（否则 `("--env", "FOO=1")` 这种合法分列写法会被误拒——
     实测第一版修法就误拒了它）；`--entrypoint` 刻意不在清单内（可替换容器入口命令）。
  2. **"非 root"不变量实际不成立**：`run_as="00:0"` 连 `assert_required_invariants` 都骗过
     （字符串精确比对），`"0:1000"`/`"root:root"` 则因为**唯一生成入口从不调用配对校验**
     而漏出，真机三者 `id -u` 都是 0。**修法**：`_validate_run_as` 按**数值**解析
     （任一段为 0、非数字、负数、超过两段一律拒绝）；`build_run_args` 末尾补
     `assert_required_invariants(args)`；`_flag_values` 改为返回**全部**出现，
     重复 flag 直接拒绝（`--user 1000:1000 … --user 1000:0` 这种覆盖必须被发现）。
  3. **`.git` 嵌套配置被复制进沙箱**：`GIT_STRIP_FILES` 只匹配顶层精确路径，于是
     `.git/modules/<sub>/config`（常含 `https://user:ghp_…@github.com/...`）
     与 `.git/worktrees/<wt>/config.worktree` 会被复制，审查者实测副本内**明文令牌仍在**。
     **修法**：新增 `GIT_STRIP_TREES`（`.git/modules`、`.git/worktrees` 整目录丢弃）
     与 `_is_git_config_like()`（`.git` 下任意层级的 `config`/`config.worktree`/`credentials`），
     两条路径都进 `stripped_paths`。
  4. 一并处理该轮非阻塞项：`workdir` 校验（真机 `"/workspace,readonly"` 会被 Docker 解析成
     `ReadOnly=true`，即**逗号注入 mount 选项**）、`env` 变量名校验、`assert_safe` 校验
     `args[1] == "run"`、`_parse_pytest_collection` 里重复的 `"="`
     **死代码**、人类可读报告也打印收集期报错、`junit.parse_junit_xml` 对**不可信 XML**
     加 8 MiB 体积上限并拒绝 `DOCTYPE/ENTITY`（ElementTree 官方明示不防恶意输入）。
  5. **`environment_issue` 改为"写探针"**（审查者建议，比我原来的文本启发式更可靠）：
     原实现两个方向都错过——真·`PermissionError` 的 traceback 行是 `E       PermissionError: …`
     匹配不到，而 `print("PermissionError: …")` + `assert False` 又会被误判成环境问题。
     现在直接对 basetemp 建/删一个探针文件来判定，与输出文本无关。
  6. **"造不出链接就 fail"**（审查者指出原型注释"跳过的检查同样是缺陷"被我改写成了 skip）：
     junction 用例（Windows 上不需要管理员权限，必然可造）与 workspace 复制用例改为
     **fail**；symlink 用例在 Windows 上仍允许 skip，并注明"同一逻辑由 junction 用例覆盖"。
  7. **重要教训（写进结论）**：原型 `verify_policy.py` 的 43/43 是**断言集不完整**，
     不是"代码正确"——它只试过 `--privileged/--cap-add=SYS_ADMIN/--network=host/--pid=host`，
     从未覆盖短选项粘连写法。**"全绿"只能证明"已声明的断言全部成立"**，
     不能证明"没有被绕过的路径"；安全层因此必须有真机端到端用例。
  8. **补上真机端到端验证** `.tmp/verify_sandbox_container.py`（在 `.tmp/` 内，不进摘要）：
     直接拿 `build_run_args` 的 argv 在 Docker 29.5.2 上跑，结果 **10/10 通过**并**落盘日志**
     `.tmp/verify_sandbox_container.out`（闭合第八轮登记的"真机验证缺落盘日志"）：
     容器可用并能读挂载的工作区、`id -u`=1000、宿主目录未挂载（看不到宿主机密文件）、
     根文件系统只读（`Errno 30`）、`/tmp` tmpfs 可写、网络不可达（`Errno 101`）、
     **18 种参数注入拼写全部在构造期被拒**（含两轮审查各自的粘连/捆绑写法）、
     超时被宿主侧截断（8.2s）且容器无残留。
     按复审建议：**不 `docker pull`**（`docker image inspect` 预检 + `--pull=never`，
     避免给门禁引入网络依赖）、注入拼写表与单测共用、Docker 缺失时 skip 而注入生效时 fail。
  9. **第九轮复审第二轮 REJECT（3 项 BLOCKING）→ 已修**：
     1. **布尔短选项捆绑写法击穿允许清单**：`extra_args=("-iu0:0",)` 与 `("-iv<host>:/mnt/host",)`
        通过全部四道校验（pflag 语义：`-i` 后的字符继续当选项解析 → `-i` + `-u0:0`），
        真机分别得到 `id -u` = **0** 与宿主目录列表。**修法**：把**布尔**短选项 `-i`/`-t`
        整体移出允许清单（只保留长写法 `--init`/`--tty`/`--interactive`）；
        **取值**型短选项（`-e/-l/-w/-h`）保留，因为它们的后续字符会被当成取值
        （`-eu0:0` 只是把 `u0:0` 当取值，实测无危害）。补 `-iu0:0`/`-iv…`/`-tu0:0`/`-tv…`/
        `-tiu0:0` 与"允许短选项 × 危险短选项"穷举用例。
     2. **嵌套仓库的 `.git` 仍被复制**：上一轮把锚点写成"**首段**必须是 `.git`"，
        于是 `vendor/.git/config`（`ghp_…` 令牌）与 `vendor/.git/hooks/post-checkout`
        照样进副本——与上一轮同一类缺陷，只是 `.git` 不在路径首段。
        实测修正：`_is_git_config_like` 改为"任意祖先段是 `.git`"，
        新增 `_is_git_hooks_dir`（`<任意>.git/hooks` 整目录丢弃）。
     3. **两处新逻辑零测试覆盖**（规程第 4 条）：`GIT_STRIP_TREES`/`_is_git_config_like`
        与写探针 `_is_writable_dir` 当时都没有用例。现在新增
        `test_all_git_credential_paths_are_stripped`（夹具铺开三类共 9 条路径，
        断言副本内不存在、`stripped_paths` 如实记录、且**副本内任何文件都不再含 `ghp_`**）
        与写探针正/反例 + "basetemp 不可写 → 报环境问题"的端到端用例。
     4. 非阻塞一并处理：symlink 用例在**非 Windows 上改为 fail**（Windows 上仍 skip 并注明
        "同一逻辑由 junction 用例覆盖"）；`test_build_run_args_checks_invariants_on_generation`
        从"断言源码文本"改为**行为断言**（生成物必须通过配对校验 + 人为破坏后必须抛错）；
        `MAX_JUNIT_BYTES` 上限同时作用于内存入口 `parse_junit_text`；
        `workdir` 限制在 `/workspace` 或 `/tmp` 子树（`workdir="/"` 会遮蔽整个 rootfs）；
        README 的 P1 行同步为本轮实测结果。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/sandbox/{__init__,policy,junit,workspace}.py`、
  `tests/test_preflight.py`、`tests/{test_sandbox_policy,test_junit,test_workspace_copy}.py`、
  `README.md`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → 本批**基线**（`0ad9ea8`）为 **92 passed + 1 skipped**；
    本批后 **183 passed + 3 skipped = 186 项执行**，0 failed / 0 error
    （逐文件收集：budget 11 / checkpointer 4 / config 7 / **junit 18** / minimal_graph 14 /
    **preflight 49** / pricing 6 / **sandbox_policy 59** / tracing 6 / **workspace_copy 12**；
    3 个 skip：2 个是 Windows 无 symlink 权限的平台分支——**同一逻辑由 junction 用例真跑覆盖**，
    1 个是既有的"已装 postgres 后端"分支）
  - `pp check --json` ×2 → `ok=True files=42 checks=10 fail=[]`，摘要两次逐字符相同
    （取值见审查者复算；本条目不写具体值，理由同上一条日志）
  - 报告字段核对：`pytest_collect_errors` 已出现在 `--json` 输出与人类可读报告中
- **审查**：**待第九轮复审**（本批 = 4 条非阻塞 + P1 沙箱纯逻辑层迁入，集中在
  `preflight.py` 与 `sandbox/` 新增包）
- **提交**：待本轮推送

### [2026-10-16] P0 发布完成：commit `0ad9ea8` 已推送 `origin/main`

- **变更**：取得 `code-reviewer` 第八轮 APPROVE 后，由 `git-publisher` 流程提交并推送。
  本条目是**新的**日志条目（按 §8.3 自指约束，commit hash 记入新条目，不回填已批准条目）。
  - `VERDICT: APPROVE`（`SCOPE: WORKTREE`，`TREE_DIGEST = ab1ce173380d5bb2ae8fc574fad4c4d1`），
    审查者独立复算一致，并确认审查全程只读、审查前后摘要相同；其对抗集在新代码上 **39/39 通过**
    （上一轮 38/39，唯一失败项就是它提出的 BLOCKING，现已修复）。
  - 提交前复算 `pp check --json` 的 `tree_digest` 与 APPROVE 引用值**逐字符一致**，
    且 `git diff --stat` 为空、`git status --porcelain` 全部为 `A `（无 `AM`/`MM`）。
- **文件**：36 个文件（`5975 insertions(+)`），首次提交为 root-commit。
- **验证**：
  - `git log --oneline -1` → `0ad9ea8 feat(p0): 工程骨架与确定性门禁（含第七轮 3 项阻塞修复）`
  - `git rev-parse HEAD` → `0ad9ea818d01bdb11b27512fc15e213657bd0f6a`
  - `git ls-tree -r --name-only HEAD` → 36 个文件
  - `git push -u origin main` → exit=0，`* [new branch] main -> main`，
    分支已 `set up to track 'origin/main'`
  - `git ls-remote origin` → `0ad9ea818d01bdb11b27512fc15e213657bd0f6a  HEAD` /
    同值 `refs/heads/main`（远端唯一 ref，与本地一致）
  - 推送前基线：`pp check` → `preflight=OK (pass=10 fail=0 skip=0 diff=0 info=0)`、`files=36`、
    `secrets=0`、pytest 范围证据 `7/7`；`pytest -q` → 92 passed + 1 skipped；
    `ruff check src tests` → All checks passed
- **审查**：第八轮 `code-reviewer` **APPROVE**（摘要 `ab1ce173…`）。
  同轮曾出现两个结论（旧会话 APPROVE `c41ec41d…`、新会话 REJECT 同摘要）；
  按"取更严格者"先修 REJECT 的 BLOCKING（子目录 root 静默丢失索引绑定）与 4 项非阻塞，
  重新取摘要后由 `2af545b4` 复审并 APPROVE。
  **其 4 条新非阻塞项登记入下一条日志**（本条目不复述，避免继续改动已发布条目的字节——
  注意：本条目写在提交**之后**，因此它不在任何已批准的摘要内）。
- **提交**：`0ad9ea818d01bdb11b27512fc15e213657bd0f6a`（`main`，已推送 `origin/main`）

### [2026-10-15] 第八轮复审收口：子目录 root 的索引绑定 + 6 项非阻塞

- **背景**：第八轮复审出现了**两个结论**——原会话 `99415545` 给 `VERDICT: APPROVE`
  （对 `TREE_DIGEST = c41ec41d82dbd8f9ee396ecd9fd24565`，BLOCKING 无，另指出一处
  `88 passed` 与实际不符）；新起的会话 `2af545b4` 对**同一摘要**给 `VERDICT: REJECT`
  （1 项 BLOCKING）。按既定原则**取更严格者**，本批按 REJECT 修。
- **变更**：
  1. **BLOCKING（`2af545b4`，我已独立复现）——子目录 root 静默丢失索引绑定**。
     `_check_worktree_matches_index` 的判定顺序是"先查 `root/.git` 是否存在"，
     而 `.git` 只存在于**仓库根**，所以"root 是仓库子目录"与"root 根本不在仓库里"
     在 `(root/'.git').exists()` 上**表现完全相同**（都是 False）→
     `pp check --root <仓库子目录>` 直接得到 `skip`（detail 还写"非 git 仓库"），
     **永远走不到** `git_scope_status` 的 fail 分支。
     实测：同一仓库 `pp check --root <子树>` 得 `files=3`、三项索引/范围检查全 `skip`，
     而 `git add -A` + commit 会提交**整个仓库**（含 root 之外的字节）——
     即"批准只覆盖子树"这一前提无人校验，且 `TREE_DIGEST` 两边各自复算都对得上
     （错的是前提）。**修法**：拆出 `find_toplevel()`，用"在不在仓库内"区分三类处境——
     不在任何仓库 → `skip`（诚实）；在仓库内但不是仓库根 → **`fail`**；
     是仓库根 → 正常校验。`git_scope` 同步改为"只有子目录那一类 fail"，
     普通非仓库目录仍 `pass`（合法用法，避免把纯目录误伤成阻塞）。
     **回归用例**：`test_subdirectory_root_must_fail_not_skip`（子树 root → `fail` 且 `ok=False`）、
     `test_non_git_directory_still_skips_index_binding`（对照：真不在仓库里仍 `skip`）。
  2. **两位审查者一致指出的计数错误**：`PLAN.md` 第八轮条目写 `88 passed`，实测为
     **89 passed + 1 skipped = 90 执行**（`-rA` 逐条统计）。已在原条目内标注更正。
  3. 非阻塞 3（`2af545b4`）：`environment_issue` 不再匹配人类可读的 `access is denied`
     （一个断言消息里引用该短语会被误判成"环境问题"），改为只匹配异常类型串
     `PermissionError` / `WinError 5` / `INTERNALERROR`。用例：
     `test_assertion_text_mentioning_access_denied_is_not_reclassified`。
  4. 非阻塞 2（`2af545b4`）：`_parse_pytest_collection` 改为返回
     `(已收集文件, 收集期报错行)`；`collect_pytest_scope` 相应返回四元组，
     fail 文案据此区分"被 `collect_ignore` 抑制"与"收集期报错（导入失败/模块级 skip）"，
     避免把导入错误说成范围被抑制。
  5. 非阻塞 4：`pp check --basetemp` 帮助文案改为"pytest 临时目录的**父目录**
     （每次运行会在其下建独占子目录 `run-<pid>-<hex>`）"，与实际语义对齐。
  6. 非阻塞 1：`_pytest_targets` 对自定义 `python_files` 的假 fail 风险**登记不改**
     （方向保守；改了要引入配置解析，收益不足），已在函数 docstring 说明。
  7. 非阻塞 5（加强不变量）：现有 `test_secret_scan_reports_binary_files_instead_of_silence`
     已同时断言"二进制不阻塞**但** detail 必须写明有文件未按文本扫描"，
     即"未发现"与"没扫"不可混同；本次不额外加用例（避免重复覆盖）。
  8. 顺带把两处过期注释（`run-<pid>-<hex>` 子目录）与 `preflight.py` 的
     `_check_worktree_matches_index` 文档更新到当前语义。
  9. **收口过程中我自己又踩了两个坑，都记录下来**：
     ① `collect_pytest_scope` 改为四元组后漏改了最终 `return`（早期返回分支已改），
     门禁的 `pytest` 项直接抛 `ValueError: not enough values to unpack`——
     被 `_guarded` 兜成 fail。教训：改返回值形状时必须**所有返回点一起改**，
     且要有"门禁自己跑一遍自己"的检查（`pp check` 会做，但早期返回分支不经覆盖）。
     ② 为消除"门禁调用 pytest 时 `tmp_path` 看起来像外层仓库子目录"造成的假失败，
     我试过把 `GIT_CEILING_DIRECTORIES` 无条件透传给所有子进程——**反而把以 `cwd`
     为基准的仓库查找套上了别处的界标**，导致 `git_scope` / `worktree_matches_index`
     误报。最终做法：界标取**被审查的仓库根**，只在 pytest 子进程环境中设置，
     不进入 `_run` 的通用路径。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/cli.py`、
  `tests/test_preflight.py`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q -rA` → **92 passed + 1 skipped = 93 项执行**，
    收集 **93 项**（budget 11 / checkpointer 4 / config 7 / minimal_graph 14 /
    **preflight 45** / pricing 6 / tracing 6）；较上轮 +3 条用例
  - 判决性复核：独立脚本复现子目录 root 缺陷（子树 `worktree_matches_index=skip`、
    摘要 `d139d4ee…` 仅覆盖 3 文件 vs 全仓库 5 文件），修复后同场景
    `git_scope=fail` + `worktree_matches_index=fail` + `ok=False`（脚本判定"主张已不成立"）
  - `pp check`（部署目录，含 ruff 与 pytest）→ **`preflight=OK (pass=10 fail=0 skip=0
    diff=0 info=0)`，`files=36`**；`pytest` 范围证据 `7/7`、`missing=[]`
  - **冻结自证**：连续两次 `pp check --json` 的 `TREE_DIGEST` 逐字符相同，
    且 `git diff --stat` 为空、`git status --porcelain` 全部为 `A `（无 `AM`/`MM`）
    > 本条目**刻意不写具体摘要值**：写作本文件会改变摘要，记录"当前值"必然立刻作废
    > （前面的 `49ac4750…` / `013757c4…` 就是这样作废的，两位审查者都按作废值复算过）。
    > 摘要只在**审查者复算时**由 `pp check` 输出给出。
- **审查**：第八轮 APPROVE（`99415545`）与 REJECT（`2af545b4`）**结论冲突**，
  按更严格者执行修复；修复后需重新取摘要并**重新审查**（两位都明确要求不得沿用旧摘要）
- **提交**：未提交

### [2026-10-15] README 口径同步：门禁 10 项、E3/E8 状态、路线图

- **变更**：第八轮新增 `git_scope` 检查项后，README 里的门禁口径与实测不符（写"9 项 /
  `pass=9 fail=0 skip=0`"），且 §2.4 E8 仍标"进行中（第七轮复审提出 3 项加固）"、
  §6 路线图仍写"待 3 项门禁加固后推送"、§2.5 注释仍写"9 项确定性门禁"。
  按**实测值**统一为：门槛 **10 项**、`pass=10 fail=0 skip=0`；E8 改为已达标并注明
  "第七轮 3 项阻塞已修 + 12 条回归用例"；当前状态行改为"七轮累计阻塞已全部修复，
  待第八轮复审通过后提交推送"；路线图 P0/P1 状态同步（P1 注明 Docker 守护进程已可用）。
  本改动**不涉及代码**，只为消除 README 与代码/门禁的口径分歧（审查规程"文档与代码一致性"）。
- **文件**：`README.md`、`PLAN.md`
- **验证**：README 全文重新核对——`pass=10`、10 项、10 项确定性门禁三处一致；
  与 `pp check --json` 输出的 10 个检查项（syntax / ruff / pytest / secret_scan / gitignore /
  skip_rules / scope_agreement / git_scope / worktree_matches_index / env_not_tracked）逐项对齐；
  与 §5 阶段表、本日志第八轮条目的口径一致。门禁复算全绿（见下条日志的最终摘要）。
  **口径同步后的冻结摘要**：两次连续 `pp check` 同为
  `013757c46fca9f71dd3ae494b3970dc2` / 36 文件（本条日志写入前的取值）。
- **审查**：与第八轮修复同批送第八轮复审
- **提交**：未提交

### [2026-10-15] 第八轮：修复第七轮 3 项阻塞并补回归用例（门禁第一次真正自证范围）

- **变更**（用户选定"选项 A：全部修完再发布"后执行）：
  1. **`worktree_matches_index` 改为绑定索引字节且覆盖枚举**。旧实现只遍历
     `git ls-files -s` 的索引条目，而摘要覆盖 `iter_scan_files`（**含未跟踪未忽略文件**），
     所以索引干净时报 pass，而摘要绑定的字节与 git 会提交的字节已经不同；
     索引为空时又是 `skip`，等于"批准了不会被提交的东西"。
     现在以**枚举集合**为准：每个文件都要在索引里（未跟踪/未暂存/被排除即 fail）、
     空索引即 fail、blob 与 `hash-object --no-filters` 不一致即 fail。
  2. **pytest 执行范围受摘要约束**。此前门禁只跑 `pytest`，而 `conftest.py` 的
     `collect_ignore` 或 `pytest.ini` 的 `addopts --ignore` 能把 fail 翻成 pass，
     且报告里看不出区别。现在做两件事：**显式传入**摘要集合中的 `test_*.py`
     （收集范围由门禁而非仓库配置决定），并用 `--collect-only -q` 取实际收集清单
     要求**全覆盖**，缺失项逐条写进报告（`pytest_expected_files` /
     `pytest_collected_files` / `pytest_missing_files`）。收集清单解析同时兼容
     pytest 两种输出形态（`file.py: 6` 与 `file.py::test_x`），后者是旧版格式。
     顺带明确：摘要范围内一个测试文件都没有时 **fail 而不是静默 pass**。
  3. **非 UTF-8 编码不再静默 pass**。判决性复现暴露了真正的根因：不是正则，
     而是**解码前没有嗅探 NUL 字节**——UTF-16LE 的 ASCII 文本在 UTF-8 眼里是合法
     字节序列，`errors="replace"` 连一个替换字符都不会产生，于是解成
     `a\x00p\x00i\x00…`，密钥自然匹配不到。
     **第一次修法被第八轮审查击穿（BLOCKING）**：我改成"按 BOM + 奇偶位 NUL 结构 +
     可打印比例"三分类后，攻击者只需在文件开头塞 **100 个 CJK 字符**，非 NUL 的那一半
     就变成满量高位字节（`啊` = U+554A → `4A 55`），比例塌到 ~0% → 被判成"二进制" →
     当时二进制**不阻塞** → `secret_scan: pass`，而文件里就躺着一条 UTF-16 密钥；
     继续收紧成"某一半必须**全部**为 NUL"同样无效（CJK 两个字节都非 0，
     5000 个 CJK 能让整份文件 NUL 数为 0），审查者的 100-CJK 用例二次穿透。
     **最终修法：不再做"是文本还是二进制"的分类，改为把可能的明文都还原出来再扫**——
     含 NUL 的文件一律额外扫描"去 NUL 后按 UTF-8"（同时覆盖 UTF-16LE/BE 与 UTF-32 的
     ASCII 可读形式）与 UTF-16 解码结果（覆盖 CJK 可读形式），任何一种还原里出现密钥
     都被 `SECRET_PATTERNS` 命中，发现项标注命中的还原方式。
     **于是不存在"含密钥却报 pass"的分类旁路**：实测 UTF-8 / UTF-16LE / UTF-16BE /
     UTF-16 带 BOM / 无 BOM / 100 与 5000 个 CJK 前缀 / UTF-32LE / UTF-32BE
     共 11 种形态全部检出，且不带密钥的同类文件不误报。
     **fail-closed 的边界**（刻意保留区分）：无 NUL 却非 UTF-8 的文本
     （latin-1/GBK 等）进 `undecodable_files` **并 fail**；**二进制资产**
     （含 NUL，且还原形式已扫过）只进 `skipped_binary_files`、出现在报告与
     `secret_scan` 的 detail 里但**不阻塞**——否则仓库里放一张 PNG 就会让门禁变红。
     核心是让报告能区分"未发现密钥"与"没扫"。
  4. **门禁自身可并发**。默认 basetemp `<root>/.patchpilot/pytest-tmp` 被所有运行共享，
     而 pytest 会在开始时清空它——两个并发门禁（审查者跑一次、开发者又跑一次）
     会互相删掉对方的临时目录，表现为大量 `tmp_path` 夹具 `FileNotFoundError` 的
     **假失败**（与用例逻辑无关）。现在每次运行用独占子目录
     `<basetemp>/run-<pid>-<hex6>`，实测两个并发 `pp check` 均全绿且摘要相同。
     另外：测试夹具在临时仓库里 `git init` 时会向上撞见**外层仓库**（门禁的 basetemp
     落在 PatchPilot 仓库内），`git rev-parse --show-toplevel` 会返回外层仓库根，
     使 `worktree_matches_index` 得到 `skip`——夹具改为用 `GIT_CEILING_DIRECTORIES`
     把查找截断在临时目录，并断言 `.git` 确实创建成功（夹具自证，不让断言指向错误方向）。
  5. **`worktree_matches_index` 补上反方向**（自查发现，判决性实测）：索引里有、
     工作区已删除的文件**照样会进提交**。实测 `git init` + 写文件 + `git add -A` +
     删除工作区文件而**不暂存删除** → `git status` 显示 `AD`、`git ls-tree HEAD` 里
     该文件仍存在——等于提交里带着一份从未被摘要与密钥扫描覆盖的字节；
     补一次 `git add -A` 后它才真正从提交中消失。现在这种状态直接 fail 并提示
     "请 `git add -A` 暂存删除"。
  6. 其他非阻塞项：`_check_worktree_matches_index` 改用
     `git hash-object --stdin-paths` **一次**取全部哈希（原为每文件一次子进程；
     实测非 ASCII 与含空格文件名都能与 `ls-files -s -z` 的路径逐一对齐）；
     `_bulk_blob_hashes` 在行数不符时保守判不一致；`_git_files` 的静默退化拆出
     `git_scope_status`，新增 `git_scope` 检查项显式报告"走 git 还是遍历、为什么"；
     `scope_agreement` 差异时改用新的 `diff` 状态（不再冒充 `pass`）；
     测试夹具 `_make_repo` 显式 `newline="\n"`（CRLF 夹具与真实仓库不同源）。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/cli.py`、
  `tests/test_preflight.py`、`PLAN.md`
- **验证**：
  - `ruff check src tests` → All checks passed
  - `pytest -p no:cacheprovider -q` → 当时为 **88 passed + 1 skipped**；本轮用例合计
    **90 项**（收集数）。逐文件收集数：budget 11 / checkpointer 4 / config 7 /
    minimal_graph 14 / preflight 42 / pricing 6 / tracing 6 = 90。
    > **数字更正（第九轮）**：两位审查者各自用三种方法复算均为 **89 passed + 1 skipped = 90 项执行**
    > （`-rA` 逐条统计 PASSED=89 / SKIPPED=1 / FAILED=0），此处原写的 **88 是错的**。
    > 差额来自 `test_checkpointer.py`：该文件收集 4 项、执行时 3 passed + 1 skipped
    > （`test_postgres_without_dependency_is_actionable` 因本机**已装** postgres 后端被 `skipif` 标记跳过）。
    > 更正后的完整口径见下一条日志。
  - `pp check`（部署目录，含 ruff 与 pytest）→ **`preflight=OK (pass=10 fail=0 skip=0
    diff=0 info=0)`，`files=36`**；10 项全部真实执行：syntax / ruff / pytest /
    secret_scan / gitignore / skip_rules / scope_agreement / git_scope /
    worktree_matches_index / env_not_tracked；`pytest` 项含**范围证据**（收集 7/7 个测试文件
    全覆盖，`pytest_missing_files` 为空）
  - **摘要在冻结后复算**：两个连续 `pp check` 的 `TREE_DIGEST` 逐字符相同（见下方"冻结"）
  - **冻结基线**：`files=36`，`ok=True`，`fail=0`；两次连续门禁摘要同为
    `00f85dbbf97a33c411bf915d3dbd345d`（本条日志写入前的取值）
  - **并发安全性**：两个 `pp check` 同时对同一棵树运行时，两次均 `ok=True`、退出码 0、
    摘要逐字符相同（此前会因共享 basetemp 而互相破坏，见变更第 4 条）。
  - **夹具自证**：`_git_init` 里加了 `assert (tmp_path / ".git").exists()`，
    使"外层仓库干扰导致 `git init` 静默失效"这类问题当场暴露，而不是让后面的断言
    指向错误方向（本轮就是这么发现基座问题的）。
  - 判决性回归（每项阻塞都有对应用例，且都是**先复现旧行为、再验证新行为**）：
    ① `test_worktree_matches_index_requires_every_scanned_file_staged`（未跟踪文件 → fail，
    `git add` 后 → pass）、`test_worktree_matches_index_fails_on_empty_index`（空索引 → fail）、
    `test_worktree_matches_index_detects_clean_filter_divergence`（`.gitattributes` eol 过滤下
    工作区 CRLF ≠ 索引 LF → fail，改成 LF → pass）、
    `test_worktree_matches_index_fails_when_index_has_deleted_file`（`AD` 状态 → fail，
    暂存删除后 → pass）；
    ② `test_pytest_scope_check_catches_collect_ignore_suppression`（`collect_ignore` 关掉必失败用例
    → pytest 仍 fail）、`test_declared_pytest_scope_covers_every_test_file_of_this_project`、
    `test_parse_pytest_collection_accepts_both_output_shapes`；
    ③ `test_secret_scan_fails_closed_on_utf16_file`（UTF-16LE 密钥必须**被检出**）、
    `test_secret_scan_still_flags_long_bomless_utf16`（无 BOM 的 UTF-16 密钥必须被检出）、
    `test_bomless_utf16_with_cjk_prefix_cannot_be_downgraded_to_binary`
    （**八轮 BLOCKING 的判决性用例**：100 个 CJK 前缀不得把它降级成静默 pass）、
    `test_utf32_with_cjk_prefix_is_also_detected`（UTF-32 同样要扫）、
    `test_secret_scan_reports_binary_files_instead_of_silence`（二进制可见但不阻塞）、
    `test_secret_scan_flags_nul_that_only_appears_late`（NUL 落在嗅探窗口之外也必须被认出来）、
    `test_secret_scan_still_finds_key_in_plain_utf8`（对照：UTF-8 检出能力未退化）
  - 另一处自查拦下：新用例里形如真密钥的字面量被门禁正确报出，按设计补
    `# preflight:allow-secret` 注释标记并进入 `waived_secrets` 审计栏
- **审查**：第七轮 REJECT（3 阻塞）→ 本轮修复完成，**待第八轮复审**
- **提交**：未提交

### [2026-10-15] README 补全：具体功能链路与验收标准

- **变更**：用户指出 README 对"这个 agent 具体做什么"描述不到位、缺少验收预期。README 由 83 行扩至 186 行，新增三节：
  1. **§1 这个 Agent 具体做什么**：输入/输出表（仓库+issue → 补丁+trace+结论，且"修好了/没修好/无法复现"三种都算有效产出）；**七步闭环表**（定位 → 复现 → 规划 → 打补丁 → 验证 → 自省 → 交付），每步给出产出物与失败分支；**明确不做什么**（不做开放式重构、不改测试来通过、不在无沙箱时执行代码、不把"看起来合理的改动"当修复）；以及"为什么效果可量化"。
  2. **§2 验收标准**（用户要求的核心）：分四层——**功能验收 F1–F8**、**安全验收**（4 类用例，附真机实测证据）、**量化验收**（SWE-bench Lite 子集 pass@1 ≥25%、Top-5 定位命中率、成本/耗时曲线、复现率）、**工程验收 E1–E8**。每项都写明**判定方式**与**当前状态**，并给出可复制的验收命令。
  3. **§6 路线图**：P0–P7 各阶段状态一览，未开始的阶段一律标注"未开始"，不预支结论。
- **文件**：`README.md`、`PLAN.md`
- **验证**：`README.md` 186 行；部署目录完整门禁 `preflight=OK (pass=9 fail=0 skip=0)`；**两处摘要一致** `70f86d6c89d1bc2acabbc3a17e304e4c` / 36 文件；`worktree_matches_index` 通过。
- **说明**：量化验收的目标值刻意定在 25%（不微调模型、不集成多智能体投票），并注明"如实标注上限比虚报数字重要"。§2.1/§2.4 中已达标项仅指**已验证**的部分。
- **审查**：本次改动尚未复审（与第七轮 3 项阻塞一并送下一轮）
- **提交**：未提交

### [2026-10-15] 第七轮复审 REJECT（3 阻塞）；用户决定 A：全部修完再发布

- **本轮未做代码改动**，只记录状态与待办。上下文接近耗尽，用户指令：**采用 A（全部修完再发布）**，
  本轮不执行，由**新开的对话**在工作区中继续。
- **冻结基线**：`TREE_DIGEST = ab2a948a4f4b7230be0d6da3b87c3d95`，36 文件；
  部署目录 `D:\Code\PatchPilot`（git 仓库，`main`，**尚无提交，36 文件已暂存**）
  与工作副本镜像算出**同一摘要**；门禁 `preflight=OK (pass=9 fail=0 skip=0)`。
  第七轮复审 `34afcf91` 已结束，裁定即下方三项阻塞。
- **第七轮阻塞 1——`worktree_matches_index` 只覆盖索引、不覆盖枚举**
  （`preflight.py:703-755`，配合 `:299`）。该检查只遍历 `git ls-files -s` 的**索引条目**，
  而摘要覆盖 `iter_scan_files`（**含未跟踪未忽略文件**）。复审判决性实测：新建仓库、索引干净，
  仅新增**未跟踪**的 CRLF 文件 → 检查报 `pass`（"4 个已暂存文件…一致"），而工作区哈希
  `c318b6e69e2a` ≠ git 实际会提交的 `c1c515d20a50`（LF）；`git add` 之后才 fail，**而摘要字节不变**
  —— 即"批准时通过、提交时字节变了"。第二半：**索引为空时该项 `skip` 而非 fail**，摘要完全无绑定。
  **修法**：把绑定移到**索引字节**——对 `iter_scan_files` 的**每个**文件比对
  `git hash-object --path <rel> <rel>` 与 `git ls-files -s -- <rel>` 的 blob，**不在索引里就 fail**，
  索引为空也 fail；并考虑把摘要本身改为对索引 blob 内容计算。**必须补回归用例**：
  真实 `git init` + 工作区写 CRLF + 未暂存 → 断言非 pass；`git add` 后 → 断言 fail。
- **第七轮阻塞 2——pytest 执行范围不受 `TREE_DIGEST` 约束**（`preflight.py:526-555`）。
  实测：必然失败的 `tests/test_always_fails.py` → `pytest: fail`；仅新增 `tests/conftest.py` 写
  `collect_ignore = ["test_always_fails.py"]` → `pytest: pass`，报告中唯一变化就是这一项；
  `pytest.ini` 的 `addopts = --ignore=…` 同理。摘要确实变了，但**它覆盖的正是"抑制收集的那段代码"**，
  所以门禁给出的 pass 完全由被批准的字节产生 —— **批准无法区分"测试真的通过"与"测试被关掉"**，
  拒绝不具持久性。**修法**：加范围证据——`pytest --collect-only -q` 取 nodeid 清单，
  要求它覆盖摘要集合中的**每一个** `test_*.py`，缺失即 fail（并把清单计入报告）；
  或固定以显式 nodeid 列表调用，使收集范围由门禁而非仓库配置决定。
  `conftest.py` / `pytest.ini` / `pyproject.toml` 的 `[tool.pytest]` 段需显式审查。
- **第七轮阻塞 3——UTF-16 编码的密钥 → `secret_scan` 报 pass（假阴性）**
  （`preflight.py:429-430`）。实测：`notes.txt` / `conf.json` 以 **UTF-16LE** 写入
  `api_key = "sk-live…"` → `secrets=[]`、`secret_scan: pass`（文件在扫描集合内且远小于 512 KB）；
  同内容 UTF-8 版本被正常抓到。根因：一律 `read_text(encoding="utf-8", errors="replace")`，
  UTF-16 字节被解成 U+FFFD 后正则失效。`.py` 情形会被 syntax 的 fail 整体兜住，
  **非 `.py` 文件（配置/文本）则整条路径无人把关**。**修法**：按 BOM/编码探测
  （`utf-8-sig` / `utf-16` / `utf-16-le` / `utf-16-be`），**或**对无法以 UTF-8 解码的文件显式
  `fail`（"存在非 UTF-8 编码的待提交文件，密钥无法扫描"），**绝不静默 pass**。
- **非阻塞待办**：① `scope_agreement` 在两集合确有差异时仍 `status="pass"`（与 detail 文案矛盾）；
  ② `_git_files` 在 git 命令非 0 时静默退化为遍历（"git 不可用"与"仓库损坏"未区分，建议退化时记 skip 说明原因）；
  ③ 符号链接目录分支在 Windows 无法构造（`WinError 1314`）→ 未验证，建议 Linux CI 补用例；
  ④ `_check_worktree_matches_index` 对每个文件各起一次 `git hash-object`（本仓库 36 次），
  建议改 `--stdin-paths` / `git cat-file --batch` 批量取；
  ⑤ `tests/test_preflight.py` 的 `_make_repo` 用 `write_text(newline=None)`，在 Windows 上写出 **CRLF**，
  与 `.gitattributes` 的 `eol=lf` 及真实部署仓库（LF）不同源，建议显式 `newline="\n"`；
  ⑥ **R6 两项修复零回归测试**——这是上面两个阻塞能存活的直接原因。
- **第七轮确认未回退**：R6 阻塞 1（`git ls-files -z`；`core.quotepath` 两取值摘要相同、
  `café_secret.py` / 阿拉伯文 / emoji 文件名全部被抓）；`recursion_limit_for` 计费向量；
  `_check_ruff` / `_check_pytest` 已删 `code == 127` 死代码；部署目录 9 项门禁；
  pytest 73 passed / 1 skipped。
- **复审记录更正**：第七轮称"未找到离线探针与 P1 验证器"。经核实它们**都在工作副本**：
  `.tmp/verify_fixes.py`（15862 B）、`.tmp/p1_staging/verify_policy.py`（8688 B）、
  `verify_junit.py`（6463 B）、`verify_workspace_copy.py`（7430 B）。
  （部署目录的 `.tmp` 被 robocopy 排除，复审大概是在那里找的。）上述"52/52、43/43、29/29、29/29"可复核。
- **交接说明（给新对话）**：镜像副本 `C:\Users\22856\Documents\deepseek-harness\default-workspace\PatchPilot`
  当初只是因为 `D:\Code\PatchPilot` 被沙箱写保护才存在；**该限制已解除**，故
  **`D:\Code\PatchPilot` 应作为唯一工作与发布源**，镜像不应再双向维护。
  完整交接快照见 `.tmp/P0_HANDOFF.md`（`.tmp` 在 SKIP_DIRS 中，不进摘要、不参与密钥扫描）。
  **下一会话清单**：先跑 `pp check --json` 复算摘要 → 按上三条阻塞逐项修并补回归用例 →
  重跑 `pp check`（含 ruff 与 pytest）→ 取新摘要 → 送 `code-reviewer` 复审 →
  取得 APPROVE 后再 `git add -A` + `commit` + `push -u origin main`。
- **审查**：七轮 REJECT（10 → 3 → 1 → 4 → 1 → 2 → 3 项）
- **提交**：未提交（36 文件已暂存，等全部修完后再提交）

### [2026-10-15] 修复第五、六轮审查：枚举取交集、非 ASCII 文件名、工作区≠索引

- **变更**（第五轮 1 阻塞 + 第六轮 2 阻塞，连同非阻塞一并处理）：
  1. **R5 阻塞——逐层 `.gitignore` 未纳入**。git 的忽略来源不止根 `.gitignore`，还有**逐层** `.gitignore`、`.git/info/exclude`、全局 `core.excludesFile`。只解析根文件时：嵌套规则排除的文件在 git 侧消失、在遍历侧仍在 → 摘要分叉**且一条 `sk-` 密钥逃过四道检查**（复审实测 `ok=True/files=3/secrets=[]` vs `secrets=['deploy/conf_local.py:1: …']/files=4`）。
     **修法**：`iter_scan_files` 取「git 集合」与「遍历集合」的**交集**（helper：`_filter_candidates` / `_rel_set` / `scan_scope_diff`）。任一忽略来源排除了某文件，它就不进摘要——而 git 也不会提交它，故"不扫描"是安全的，且摘要与位置/环境无关。新增检查项 **`scope_agreement`** 把两集合差异显式展示（提示性，不阻塞），避免"静默取交集"。
  2. **R6 阻塞 1——`_git_files` 未用 `-z`**。`git ls-files` 默认对非 ASCII/含空格文件名做引号转义（`"src/caf\303\251_secret.py"`），解析出的路径不存在 → `is_file()` 判否 → 文件掉出交集、**密钥与语法错误一并逃检**，摘要还随 `core.quotepath` 分叉（git 内置默认正是 `true`；本机只因 `~/.gitconfig` 显式写了 `false` 才对得上）。**修法**：改用 `git ls-files -z` 并按 `\0` 切分。
  3. **R6 阻塞 2——工作区字节 ≠ 将被提交的字节**。第 3 条里我加的 `.gitattributes`（`* text=auto eol=lf`）会引入 clean 过滤器，而 `TREE_DIGEST` 按工作区字节计算；git 自己把二者视为相同（`git diff` 干净、`hash-object --path` 相等），所以**门禁与发布者复算都发现不了**——批准绑定在结构上不成立。**修法**：新增阻塞检查项 **`worktree_matches_index`**，用 `git hash-object --no-filters`（跳过 clean 过滤器）取原始字节哈希，与 `git ls-files -s` 的索引 blob 逐文件比对，不一致即 fail。
  4. 非阻塞：`recursion_limit_for` 也改用真实计费向量（此前只有构造期与扣费统一）；`_check_ruff`/`_check_pytest` 改用 `importlib.util.find_spec` 判定"未安装"，并删除残留的 `code == 127` 死代码（127 也会来自子进程被中断，那会把真实失败记成 skip 而 ok=True）；补嵌套 `.gitignore` 回归用例（在 tmp 内真实 `git init`，故在非 git 工作副本里不会被跳过）。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/graph/minimal.py`、`tests/test_preflight.py`、`.gitattributes`（新增）、`PLAN.md`
- **验证**：部署目录完整门禁 **`preflight=OK (pass=9 fail=0 skip=0)`**（9 项全真实执行：syntax / ruff / pytest / secret_scan / gitignore / skip_rules / scope_agreement / worktree_matches_index / env_not_tracked）；`worktree_matches_index` 报"36 个已暂存文件的工作区字节与索引一致"；**两处摘要一致**（部署目录 git 路径 == 工作副本遍历路径）；`core.quotepath` 取默认与 `true` 两种值时摘要相同（`-z` 消除了转义歧义）；`git ls-files --eol` 显示 36 文件全部 `i/lf w/lf attr/text=auto eol=lf`（首次提交后任何平台克隆都复现同一摘要）；离线探针 52/52；P1 三个验证器 43/43、29/29、29/29；产品级 ruff 全绿。
- **审查**：五轮 REJECT（10 → 3 → 1 → 4 → 1 项）→ 六轮 REJECT（2 项）→ 本轮修复完成，**待第七轮复审**
- **提交**：未提交

### [2026-10-15] 修复第四轮审查的 4 项阻塞；门禁首次在真实 git 仓库跑通全绿

- **变更**：
  1. **B1 摘要仍与位置相关（大小写）**。Windows 上 git 的忽略匹配大小写无关（`core.ignorecase`），SKIP 规则却大小写敏感 → `Venv/`、`BUILD/`、`DIST/`、`.ENV` 被 git 忽略却被遍历收录，同一份内容算出两个摘要。新增 `_key()`（Windows 下 casefold），`_is_skipped` / `_walk_files` / `iter_scan_files` / `_covered_by_skip_rules` / `_git_files` 统一经由它比较。
  2. **B2 `skip_rules` 只做单方向**（`.gitignore ⊆ SKIP`），SKIP 独有项 `node_modules` / `.eggs` / `.tox` / `.nox` / `htmlcov` / `*.dist-info` / `*.egg-link` 无人检查——**git 并不忽略它们**，于是这些字节既不在摘要、也不在密钥扫描范围内。复审实测：`node_modules/pkg/index.py` 中一条 `sk-` 密钥可被提交，而门禁四道检查全绿。现在自检**双向**（两个方向任一不满足即 fail）；`.gitignore` 补齐上述 7 条；`.egg-link` 从目录后缀移到文件后缀（它其实是文件，混放会让自检说"已覆盖"而摘要仍分叉）。
  3. **B3 成本判据用代理 token 向量**（`prompt=completion=per_node`），实际扣费用的是 `(prompt_tokens_per_attempt, completion_tokens_per_attempt)`。改为**与 `_charge_simulated_call` 完全同一算式**，堵住"单侧零价 + 该侧 token 数为 0"这一支。
  4. **B4 新增分支零测试覆盖**：补 `finish(unpriced_models=[...])` → `cost_unknown=True` 的用例，并顺带过滤 falsy 项（避免留下 `unpriced_models: ['']` 的矛盾组合）。
  - 非阻塞同步修掉：`--extend-exclude` 取代 `--exclude`（后者会替换 ruff 内置排除表）并把目录后缀纳入；`_expand_simple_glob` 注明"单层展开、保守失败"；测试夹具的 `.gitignore` 改为**由 SKIP 规则生成**（手写清单会随规则演进而失配）。
  - **在真实部署路径上跑门禁时又抓到一个绕过**：`_git_files` 只执行 `git ls-files` 而不校验 `root` 就是仓库根。当 root 是仓库**子目录**（`pp check` 把 basetemp 放在 `<root>/.patchpilot/pytest-tmp`，而该子树被 `.gitignore` 忽略）时，git 向上找到仓库并返回**空列表** → 门禁静默地什么都没扫却报通过，且所有依赖 `tmp_path` 的用例失败。现在用 `git rev-parse --show-toplevel` 显式比对，子目录一律退化为目录遍历。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/graph/minimal.py`、`src/patchpilot/observability/tracing.py`、`tests/test_preflight.py`、`tests/test_tracing.py`、`.gitignore`、`PLAN.md`
- **验证**：`ruff check` → All checks passed；**部署目录完整门禁 `preflight=OK (pass=7 fail=0 skip=0)`**（7 项全部真实执行，含 ruff 与 pytest）；**两处摘要一致**——部署目录（git 路径）与工作副本（遍历路径）均为 `ef05c24932ca756adb03f7686642af06` / 35 文件；**Postgres checkpointer 真实端到端 3/3 通过**（建表并跑通图 / 读出历史 7 条 / 断开重连后仍在 7 vs 7），补齐四轮复审一致点名的"从未经真实连接验证"；sqlite checkpoint 用例 4 passed（此前一直跳过）；离线探针 **52/52**；P1 三个验证器 43/43、29/29、29/29；`pp smoke` 通过。
- **审查**：四轮 REJECT（10 → 3 → 1 → 4 项）→ 本轮修复完成，**待第五轮复审**
- **提交**：未提交

### [2026-10-15] 修复第三轮审查阻塞：.gitignore 与 SKIP 规则分叉

- **变更**：
  1. **BLOCKING——`venv/` 在 `.gitignore` 里却不在 `SKIP_DIRS` 里**，于是 `git ls-files -co --exclude-standard`（排除 venv）与目录遍历（包含 venv）**分叉**：同一份逻辑内容在部署目录与工作副本算出**不同摘要**（实测 35→37 文件、`defa38f0…`→`bfa339e2…`），批准绑定失效。本质是**又造了两套忽略规则**——上一轮 B1 是"摘要 vs 检查"，这一条是"git 语义 vs 遍历语义"，同一类缺陷换了个位置。
     - 补齐 `venv` 到 `SKIP_DIRS`、`.pyd` 到 `SKIP_FILE_SUFFIXES`（`*.py[cod]` 原本漏了 `d`）；
     - `_check_ruff` 的 `--exclude` 改为**由 `SKIP_DIRS` 派生**，不再另写一份排除清单；
     - **新增门禁自检项 `skip_rules`**：解析 `.gitignore` 的每个条目（含 `*.py[cod]` 这类方括号类展开），**任何未被 SKIP 规则覆盖的条目直接让门禁 fail**。这类分叉从此由门禁自己抓住，而不是靠人记得同步两份清单——这是本轮最重要的结构性修复。
  2. 对称补漏：预算可执行性判据从「模型是否在价格表里」改为「**每轮成本是否严格为正**」，堵住"已定价但单价为 0 + 金额为唯一上限"这一支（否则仍表现为"配置合法却永不触发"）。
  3. 递归上限封顶：`recursion_limit_for` 加 `min(…, 10_000)`，避免大额预算（如 1e8 token）算出 6e5 反而让"框架先炸"重现。
  4. trace 一致性：`unpriced_models` 非空时同步置 `cost_unknown=true`，消除"成本不可信却标记为可信"的自相矛盾组合。
  5. 补测试：枚举异常分支（**刻意不依赖 `tmp_path`**，任何环境可跑）、`.gitignore` 全覆盖自检、零成本模型拒绝、递归上限封顶。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/graph/minimal.py`、`src/patchpilot/observability/tracing.py`、`tests/test_preflight.py`、`tests/test_minimal_graph.py`、`PLAN.md`
- **验证**：`ruff check` → All checks passed；离线探针 → **52/52 通过**（含第三轮判决性证据：`venv` 产物不改变摘要、不计入文件数；`skip_rules` 自检对本仓库 19 个 `.gitignore` 条目全覆盖；注入一条未覆盖条目 → 门禁 `fail`）；P1 三个验证器 → 43/43、29/29、29/29；`pp check --skip-tests` → `preflight=OK pass=4 files=35`。
- **审查**：首轮 REJECT（10 项）→ 二轮 REJECT（3 项）→ 三轮 REJECT（1 项）→ 本轮修复完成，**待第四轮复审**
- **提交**：未提交

### [2026-10-15] 修复第二轮审查的 3 项阻塞（含 TREE_DIGEST 机制自身的缺陷）

- **变更**：code-reviewer 第二轮复审 **REJECT**（3 项阻塞 + 12 项非阻塞，均附判决性实测）。逐项修复：
  1. **B1 摘要范围缺陷——上一轮刚加的机制被击穿**。上一版摘要用"显式范围白名单"（`src`/`tests`/`agents`/`.github` + 7 个根文件），于是根级自举文件（`conftest.py`、`pytest.ini`、`ruff.toml`…）既不在摘要内、也不在语法/ruff 检查范围内。**实测：坏代码只要新增一个根 `ruff.toml` 就能让 ruff 由 fail 转 pass；新增一个根 `conftest.py` 就能让坏测试文件由"被收集"变为"不被收集"——而摘要逐字符不变。** 现在摘要、语法检查、ruff 检查、密钥扫描四者**共用 `iter_scan_files` 同一集合**；`iter_review_files` 与 `REVIEW_INCLUDE_*` 已删除。
  2. **B2 未定价模型的金额熔断仍然失效**。此前 `.10` 只修到"显式化"，**没修到熔断点**：`cost or 0.0` 把未知成本折成 0，唯一为正的金额上限永不触发，无界组以未捕获的 `GraphRecursionError`（框架默认 10007）收场且不落 trace。现在：`GraphOptions.__post_init__` 校验「**有效**上限非空」（价格未知且金额是唯一上限 → 构造期拒绝）；`_charge_simulated_call` 显式分支并 `mark_unpriced`（未知成本留痕，不再看起来像"免费"）；`recursion_limit_for` 把递归上限绑定到预算；`GraphRecursionError` 归一化为 BUDGET 且**照常落 trace**；`plan` 触顶后不再进入 `critique`；`notes` 通道加上界；`analyze` 仅在状态无用量时初始化。
  3. **B3 枚举步骤缺兜底**。`iter_scan_files` / `compute_tree_digest` 此前位于所有 `_guarded` 之外，异常会逃逸成 traceback、破坏 `--json` 契约。现在包进 try/except，异常转为 `enumerate` 检查项的 fail 结果。
  - 另修 6 项非阻塞：新增「URL 内嵌凭据」与「无引号长值赋值」两种漏报模式；`sk-xxxx…` 误报改为 `^x+$` 放行；豁免标记改用 **tokenize 真实注释**判定（写进字符串字面量不再生效）；默认 DSN 去掉硬编码凭据；修正 `git-publisher.md`/`README.md`/`docker-compose.yml`/`Makefile` 的事实矛盾与陈旧残留；`sqlite` 并入 `dev` extra。
- **文件**：`src/patchpilot/core/preflight.py`、`budget.py`、`config.py`、`src/patchpilot/graph/minimal.py`、`src/patchpilot/cli.py`、`tests/test_preflight.py`、`tests/test_budget.py`、`tests/test_minimal_graph.py`、`pyproject.toml`、`.env.example`、`README.md`、`agents/git-publisher.md`、`docker-compose.yml`、`Makefile`、`PLAN.md`
- **验证**：`ruff check` → All checks passed；离线探针 → **48/48 通过**（含 B1 判决性证据：新增根级 `conftest.py`/`ruff.toml`/`pytest.ini` 均改变摘要，坏掉的根级 `conftest.py` 被语法检查抓到；B3 注入枚举故障不抛异常且 `--json` 可序列化；B2 未定价+仅金额上限被拒、未定价仍按 token 上限终止并显式记录）；P1 三个验证器 → 43/43、29/29、29/29；`pp check --skip-tests` → `preflight=OK files=35`；专项实测：字符串内标记不再豁免（findings 非空）而真注释仍豁免并记入 `waived_secrets`。
  **仍未验证**：`sqlite` checkpoint 的 happy path 在本地仍必然跳过——需要执行一次 `pip install -e ".[dev]"`（`dev` 现已包含 sqlite）才能真实跑起来，本会话无法联网安装。
- **审查**：首轮 REJECT（10 项）→ 二轮 REJECT（3 项）→ 本轮修复完成，**待第三轮复审**
- **提交**：未提交

### [2026-10-15] 新增 TREE_DIGEST：把 APPROVE 绑定到唯一确定的字节

- **变更**：`pp check` 新增 `tree_digest` 输出（表格 + `--json`），对审查对象计算 SHA256 内容摘要。起因是 git-publisher 首轮核查的阻塞项——"同一范围出现两份文字描述，审查者无法界定 scope"。现在 `code-reviewer` 必须在 VERDICT 中**原样引用**该值，`git-publisher` 提交前复算比对，不一致即 `BLOCKED`。
  - **自查中发现并修掉一个自身缺陷**：初版摘要用"git 可见文件集"，导致工作副本（含残留 `src/*.egg-info`）算出 41 个文件、部署目录只有 35 个——**同一份逻辑内容在两处得到不同摘要，批准绑定当场失效**。改为由 `iter_review_files` 显式定义审查对象范围（`src` / `tests` / `agents` / `.github` + 7 个根文件），并新增 `SKIP_DIR_SUFFIXES` 按后缀排除构建产物，使摘要**与位置无关**。
  - **职责分离**：摘要覆盖**审查对象**（可复现导向）；密钥扫描仍覆盖 **git 可见的一切**（安全导向）。报告中分别给出 `review_file_count` 与 `scan_file_count`，不再声称两者相同。
- **文件**：`src/patchpilot/core/preflight.py`、`src/patchpilot/cli.py`、`tests/test_preflight.py`、`agents/code-reviewer.md`、`agents/git-publisher.md`、`PLAN.md`
- **验证**：`ruff check src tests` → All checks passed；离线探针 → **31/31 通过**（新增 4 项：摘要稳定、内容敏感、新增文件敏感、**忽略产物不改变摘要**）；`pp check --skip-tests` → `preflight=OK reviewed=35 scanned=35 digest=707f63fd…`；跨位置一致性实测：对 `D:\Code\PatchPilot` 用同一规则得 33 个审查对象且"混入的构建产物：无"。
- **审查**：待第二轮复审
- **提交**：未提交

### [2026-10-15] 门禁在修复后自查中又拦下 2 处，已补豁免标记

- **变更**：修复批次完成后用 `pp check` 自查，报出 `tests/test_preflight.py:63/64` 为「通用密钥赋值」——这是门禁的**正确行为**（该两行字面量形如 `password = "<32 字符>"`，扫描器无法区分它与真实密钥）。按设计为其补 `# preflight:allow-secret` 注释标记；豁免项同时出现在报告的 `waived_secrets` 中供人工审计，而非静默放行。
- **文件**：`tests/test_preflight.py`
- **验证**：`pp check --skip-tests` → **preflight=OK (pass=3 fail=0 skip=3)**，豁免审计栏列出 4 项；`ruff check src tests` → All checks passed；离线验证脚本 `.tmp/verify_fixes.py` → **27/27 通过（退出码 0）**；**在修复后的代码上重跑 P0 完成标志 `pp smoke`** → `attempts=2`、`approved=True`、`budget_exceeded=无`、已用 token `480 + 160 = 640`（证明预算计量真的在按节点累加）、trace 落盘含 `cost_unknown=false` 与 `failure_kind=none`。
- **审查**：待第二轮复审
- **提交**：未提交

### [2026-10-15] 修复首轮审查的全部 10 项阻塞（离线验证通过，待同步与复审）

- **变更**：code-reviewer 首轮独立审查给出 **REJECT**（10 项阻塞，每项都有实测探针）。逐项修复：
  1. **预算熔断真正接入编排层**——每轮 `start_attempt`、每次调用 `charge`、用量随 checkpoint 持久化，触顶终止并归类 `FailureKind.BUDGET`（此前 tracker 只存在于单测中，PLAN 属虚假验收）；
  2. 删除状态中与 Budget 重复的 `max_attempts` 通道；「何时判定通过」抽成显式 `PassPolicy`，消除"硬编码阈值架空配置"；
  3. `preflight` 的 SKIP_DIRS 改为按**相对 root** 路径判定——修复"仓库位于 build/ 之下就整个被静默跳过并报通过"；
  4. 密钥扫描占位符改为整值/前缀**严格**匹配（`SampleCorp-…` 这类真实密钥不再被放行）；豁免必须写成注释且记入 `waived_secrets`；超限文件记入 `skipped_large_files`；
  5. `.gitignore` 检查优先用 `git check-ignore` 权威判定（`.env.example` 不再冒充 `.env`）；
  6. 语法检查改用 `tokenize.open` 尊重编码声明；`run_preflight` 整体兜底，任何异常转成 CheckResult，`--json` 契约不再被 traceback 破坏；
  7. `sqlite` / `postgres` checkpoint 依赖写入对应 extras，新增 `tests/test_checkpointer.py`；
  8. 未收录价格的模型不再静默按 0 计费：`price_for` 返回 `None`、trace 标记 `cost_unknown`、`pp doctor` 告警、支持 `PP_PRICE_OVERRIDES`；
  9. `llm_api_key` 升级为 **`SecretStr`**，`repr` / `str` / `model_dump` 全部掩码；OpenAI 客户端可显式关闭且异常脱敏后上抛；`asyncio.run` 加事件循环守卫；
  10. `start_attempt` 先判定后自增，消除 off-by-one；`pp check` 支持 `--basetemp` 覆盖。
- **文件**：`PLAN.md`、`pyproject.toml`、`.env.example`、`.gitignore`、`src/patchpilot/core/{budget,pricing,config,llm,preflight}.py`、`src/patchpilot/observability/tracing.py`、`src/patchpilot/graph/minimal.py`、`src/patchpilot/cli.py`、`tests/*`（含新增 `test_checkpointer.py`、`test_tracing.py`）
- **验证**：`ruff check src tests` → **All checks passed**；离线验证脚本 `.tmp/verify_fixes.py` → **27/27 通过**（逐条覆盖上述 10 项阻塞，含三种预算终止路径、密钥漏报回归、gitignore 假通过回归、零文件解析 fail）；`pytest tests/test_budget.py tests/test_pricing.py tests/test_config.py` → **22 passed**。
  **诚实声明未验证部分**：依赖 `tmp_path` 夹具的用例（preflight 全量、tracing、graph trace、sqlite checkpointer）在本会话沙箱内无法运行——pytest 夹具目录被写入受限 ACL（`WinError 5`）；其逻辑改由上述探针逐条覆盖。`sqlite` / `postgres` checkpoint 后端与 Docker 相关路径**待环境解封后验证**，不得视为已通过。
- **审查**：首轮 code-reviewer **REJECT**（10 阻塞）→ 本轮修复完成，**待第二轮复审**
- **提交**：未提交（依门禁协议，须取得 APPROVE 后才由 git-publisher 提交）

### [2026-10-15] 修正环境结论并去重日志
- **变更**：删除 §9 中重复且自相矛盾的「P0 建立门禁与协作机制」条目（其验证仍写"待执行"），与相邻条目合并，使同一阶段在同一范围内只有唯一描述；修正 §7 中「github.com 在沙箱不可达」的**错误结论**——真实原因是沙箱内 git 默认使用 schannel TLS 后端导致 `SEC_E_NO_CREDENTIALS`，与网络无关；新增 §7.1 环境事实表（含代理、凭据助手、Docker、pytest tmp 拦截等实测结论）。
- **文件**：`PLAN.md`；仓库本地 `.git/config` 增加 `http.sslBackend=openssl`
- **验证**：`git -c http.sslBackend=openssl ls-remote https://github.com/git/git.git` → 5393 refs / exit=0，`https://gitee.com/mirrors/git.git` → 2033 refs / exit=0（git-publisher 实测）；修复后 §9 同一阶段条目唯一
- **审查**：未审（待 code-reviewer 首轮）
- **提交**：未提交

### [2026-10-15] P0 门禁与协作机制落地

- **变更**：新增确定性门禁 `pp check`（语法解析 / ruff / pytest / 密钥扫描 / .gitignore 覆盖 / .env 未被跟踪）；`PLAN.md` 成为计划与日志的唯一来源（原 `docs/PLAN.md` 合并并删除）；新增 `code-reviewer` 与 `git-publisher` 两个子代理角色定义；CI 改为复用同一套门禁，避免"本地过了 CI 挂了"；修复 ruff 报出的 9 处问题（E501 六处、I001 一处、B008 经 per-file-ignores 处理）；为密钥扫描器加入 `preflight:allow-secret` 行内豁免标记，消除测试夹具的自指误报。
- **文件**：`PLAN.md`、`README.md`、`agents/code-reviewer.md`、`agents/git-publisher.md`、`src/patchpilot/core/preflight.py`、`tests/test_preflight.py`、`src/patchpilot/cli.py`、`src/patchpilot/core/budget.py`、`src/patchpilot/observability/__init__.py`、`pyproject.toml`、`.github/workflows/ci.yml`
- **验证**：`ruff check src tests` → All checks passed；`pytest` → ..........................                                               [100%]；`pp check` → preflight=OK (pass=6 fail=0 skip=0)
- **审查**：未审（待 code-reviewer 首轮独立审查）
- **提交**：未提交（仓库已 `git init`，等待审查通过后由 git-publisher 执行）

### [2026-10-15] P0 修复 Budget 序列化崩溃
- **变更**：`Settings.safe_summary()` 使用 `self.budget().__dict__`，而 `Budget` 为 `slots=True` 的冻结数据类，无 `__dict__`，导致 `pp doctor` 与两个用例崩溃；改为显式构造字典。
- **文件**：`src/patchpilot/core/config.py`
- **验证**：`pytest` → **18 passed**；`pp doctor` → 正常输出，Docker 检查正确报出守护进程不可达
- **审查**：未审（修复由主 Agent 直接完成，P0 阶段尚无门禁流程）
- **提交**：未提交

### [2026-10-15] P0 环境落地与验证
- **变更**：项目同步至 `D:\Code\PatchPilot`；用 Anaconda 建立独立环境 `patchpilot`（Python 3.12.15）并安装依赖，base 环境零污染；跑通 `pp smoke`。
- **文件**：30 个项目文件
- **验证**：`pp smoke` → 状态机 2 轮回环、checkpoint、trace 落盘全部通过（trace_id `cfa82985b9144ce6`，spans `graph.invoke` + `checkpoint.history`）；`conda list -n base` 无 `patchpilot/langgraph/structlog` → base 未被污染
- **审查**：未审
- **提交**：未提交

### [2026-10-15] P0 骨架创建
- **变更**：搭建工程骨架——`pyproject.toml`（PEP 621，可选依赖分组 llm/server/storage/dev）、`docker-compose.yml`（postgres+redis+qdrant）、`Makefile`、`.env.example`、`.gitignore`、CI workflow；实现 core（config / logging / models / budget / pricing / llm）、observability（tracing）、graph（checkpointer 三后端 + minimal 状态机）、CLI（doctor / dev-up / dev-down / smoke）；sandbox / index / eval 留下带计划的占位模块。
- **文件**：27 个文件（后经同步增至 30）
- **验证**：`pp smoke` 通过；`pytest` 16 passed / 2 failed（后者为 Budget bug，已单独记录并修复）
- **审查**：未审
- **提交**：未提交
