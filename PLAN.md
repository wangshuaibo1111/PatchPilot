# PatchPilot 项目计划与变更日志

> **本文件是 PatchPilot 唯一的计划与变更记录（single source of truth）。**
> 任何代码、配置、文档的改动完成后，必须在第 9 节追加一条日志；
> 任何上传（git commit / push）之前，必须完成第 8 节的检查门禁。

---

## 0. 维护规则

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
| 1 | CLI 一条命令在任意本地仓库上完成一次修复 | `pp run --repo . --issue-file bug.md` | 未开始（P3） |
| 2 | 沙箱安全边界成立 | 逃逸 / 超时 / 资源耗尽 / 恶意代码 4 类用例通过 | **原型已在真机验证**（`.tmp/p1_staging`，20/20）；工程化与回归用例待 P1 迁入 `src/` |
| 3 | 端到端解析率有数字 | `pp eval --subset lite50` 输出 pass@1 + 成本 + P95 | 未开始（P4） |
| 4 | 评测可复现 | 公布题目 ID、温度、commit、镜像 tag，第三方可复跑 | 未开始（P4） |
| 5 | 回归门禁生效 | 故意改坏 prompt → CI 变红并给出指标 diff | 未开始（P5） |
| 6 | 全链路可观测 | 每个任务有完整 trace（LLM / 工具 / 检索 / 成本） | **部分完成**（JSONL trace 已通，Langfuse 待 P5） |
| 7 | 一键部署 | `docker compose up` 起 Web 面板，浏览器看轨迹回放 | 未开始（P6） |
| 8 | PC 可用 | Windows/macOS 上安装后本地可跑 | **已完成**（conda 环境 + `pp` 命令） |
| 9 | 上传前门禁可自证范围 | 门禁输出的 pass 不得来自"被关掉的测试/未暂存的字节/没扫到的编码" | **已完成**（第八轮：三项阻塞修复 + 8 条判决性回归用例；`pp check` 10 项全绿） |

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
| **P0** | 工程骨架：工程配置、compose、状态机、checkpointer、CLI、CI、测试 | `pp smoke` 通过 + 全部单测通过 + `code-reviewer` 出具 APPROVE | 🔧 功能完成；第七轮 3 项阻塞已修并补回归用例，**待第八轮复审**（通过后才提交/推送） |
| **P1** | 沙箱与工具：Docker 隔离、4 个 MCP Server、安全测试 | 沙箱跑 pytest 结构化返回；4 类安全用例全绿 | ⏳ 阻塞已解除（实测 Docker 守护进程可用，ServerVersion 29.5.2）；纯逻辑原型在 `.tmp/p1_staging`，待迁入 `src/` |
| **P2** | 代码索引与检索：tree-sitter 符号表、三路召回、rerank | 定位集 Top-5 命中率有基线与消融表 | 未开始（可立即开工，不依赖 Docker） |
| **P3** | 修复闭环：8 节点全图、真实模型、预算熔断 | 10 个真实 issue 端到端跑通 ≥3 个 | ⛔ 阻塞：`.env` 未配置 API Key，且 `openai` 依赖未安装 |
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

---

## 9. 变更日志

格式：`### [日期] <阶段> <标题>` + 变更 / 文件 / 验证 / 审查 / 提交 五项。

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
