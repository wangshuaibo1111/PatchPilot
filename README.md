# PatchPilot

> 一个能在隔离沙箱里自主**定位 bug、复现、改代码、跑测试自验证**的代码修复 Agent，配一套客观、可回归、可归因的**评测闭环**。

**目标岗位**：国内大厂 Agent 开发 / 算法岗。
**详细计划、阶段状态与变更日志**：见 **[PLAN.md](PLAN.md)**（唯一事实来源）。
**当前状态**：P0 工程骨架**已完成并发布**（确定性门禁 **10 项全绿**、摘要可复算、Postgres/sqlite checkpoint 真机验证）；七轮对抗性复审累计裁定的阻塞（10 → 3 → 1 → 4 → 1 → 2 → 3 项）**已全部修复并补齐回归用例**，第八轮 APPROVE 后发布（commit `0ad9ea8`）。**P1（验收项 2）已完成**：沙箱驱动层 `manager.py`（沙箱内跑 pytest → JUnit 结构化返回）+ **4 个 MCP server**（fs/git/test/search，已在 stdio 协议层验证）+ 自建执行镜像；**真机 Docker 端到端验证 15/15 通过**（`.tmp/verify_sandbox_container.out`）。**尚无论文级效果数据**（见第 6 节）。

---

## 1. 这个 Agent 具体做什么

**一句话**：给它一个代码仓库和一个 issue，它自己找到该改哪里、改完、跑到测试变绿，然后交出一个补丁和一份可核查的过程记录。

### 1.1 输入与输出

| | 内容 |
|---|---|
| **输入** | ① 一个代码仓库（git 仓库 + 可运行的测试套件）<br>② 一个任务：自然语言 issue 描述，或一个失败的测试用例 |
| **输出** | ① 一个可 `git apply` 的补丁<br>② 一份 trace（JSONL）：每一步的输入输出、检索到的候选、每次尝试的测试结果、token 用量与成本、耗时<br>③ 一份结论：修好了 / 没修好 / 无法复现（**三种都算有效产出**） |

### 1.2 一次运行的完整链路（七步闭环）

| 步 | 做什么 | 产出物 | 失败时怎么办 |
|---|---|---|---|
| 1 **定位** | 从 issue 抽关键词，用符号索引（tree-sitter）+ BM25 + 向量 + 调用图召回候选文件/函数 | Top-K 候选文件及**召回理由** | 召回为空 → 报告"无法定位"，不猜 |
| 2 **复现** | 在沙箱里跑目标测试，确认失败可稳定重现 | 失败输出原文 + 复现命令 | 复现不出 → 报告"无法复现"，**不改代码** |
| 3 **规划** | 给出修复假设与预计改动范围 | 计划（含理由） | 假设不成立 → 回到 1 |
| 4 **打补丁** | 在沙箱的工作区副本上生成 diff | unified diff | diff 不合法 → 重新生成 |
| 5 **验证** | 沙箱内跑 `fail_to_pass`（必须由红转绿）+ `pass_to_pass`（不能打破别的） | 两份测试结果 | 任一不满足 → 回到 3 带证据重试 |
| 6 **自省** | 带测试输出复盘，最多 N 轮 | 每轮假设与结果 | 超轮次 → 报告"未修好" |
| 7 **交付** | 汇总补丁 + trace + 结论 | 上述输出三件套 | — |

### 1.3 它明确**不做**什么

- 不做开放式重构、不顺手改无关代码（补丁超出规划范围即被判失败）；
- **不改测试来"通过"**——`fail_to_pass` 的用例集合由评测方给定，Agent 无法修改；
- 不在无沙箱的情况下执行仓库里的代码；
- 不把"猜一个看起来合理的改动"当成修复：复现失败就一定如实报告。

### 1.4 为什么它的效果可以量化

反馈信号**天然客观**——代码修复的成败由测试通过与否判定，不依赖任何主观打分：

- 好不好不用辩论：**跑一遍测试就知道**；
- 改一次 prompt / 检索策略 / 轮次上限，通过率曲线立刻给出方向；
- 评测本身可以进 CI 做回归门禁。

---

## 2. 验收标准

> 分三层。**功能与工程验收是硬门槛**（不达标不算完成）；量化指标随评测闭环（P4）产出后回填，
> 交付时同时给出**绝对值与相对基线的提升**，并附消融矩阵。

### 2.1 功能验收（做到这些才算"能用"）

| # | 验收项 | 判定方式 | 状态 |
|---|---|---|---|
| F1 | 给定仓库与失败测试，能**复现**失败 | 沙箱内复现命令的输出与给定失败一致 | P1 |
| F2 | 能**定位**到需要改动的文件，并给出理由 | 与人工标注的 ground truth 文件对比，输出 Top-5 命中率 | P2 |
| F3 | 能产出**可应用**的补丁 | `git apply` 无冲突 | P3 |
| F4 | 修复后 `fail_to_pass` 全部由红转绿 | 沙箱内测试结果 | P3 |
| F5 | 修复后 `pass_to_pass` 无一回归 | 沙箱内测试结果 | P3 |
| F6 | 修不好时**如实报告**，而不是交一个假装修好的补丁 | 人工抽查 + trace 一致性 | P3 |
| F7 | 每次运行可**完整归因**：哪一步用了什么、花了多少 | trace 覆盖全部 LLM 调用与工具调用 | P3 |
| F8 | 沙箱**真的隔离**：网络不可达、根文件系统只读、非 root、资源超限被杀、超时被杀 | 4 类安全用例（见 2.2） | **已验证**：真机 Docker 端到端 **15/15**（uid=1000、宿主目录不可见、rootfs 只读、网络不可达、**内存 OOM rc=137**、**pids 超限 fork 被拒**、超时截断且容器无残留、30 类非法字段构造期被拒）；日志 `.tmp/verify_sandbox_container.out` |

### 2.2 安全验收（沙箱必须挡住的 4 类用例）

每一条都在真实 Docker 上验证过，不是设计声明：

| 用例 | 判定 | 实测证据 |
|---|---|---|
| 网络不可达 | 容器内建连失败 | `OSError: [Errno 101] Network is unreachable` |
| 根文件系统只读 | 容器内写 `/usr/local` 失败，`/tmp`（tmpfs）可写 | `Errno 30 Read-only file system` |
| 非 root 运行 | `id -u` = 1000 | `uid=1000` |
| 资源超限被杀 | 超内存 → OOM kill；超进程数 → 建线程失败 | `exit=137`；`RuntimeError: can't start new thread` |
| 超时被杀且**无残留容器** | 宿主侧超时后 `docker ps -a` 无该容器 | `elapsed=8.3s`，无残留 |
| 危险参数被拒 | `--privileged` 等被 `assert_safe` 拦截 | 抛 `SandboxPolicyError` |

### 2.3 量化验收（P4 评测闭环产出后回填）

| 指标 | 目标 | 现状 |
|---|---|---|
| SWE-bench Lite 子集（50 题）pass@1 | **≥ 25%** | ⛔ **本环境不可达**（已解锁到 **11 题**：**真实 instance** 上用宿主 conda py3.9 解释器跑通），修正判分口径后实测 **0/10**（1 题 invalid 不计分母），失败集中在**补丁无法应用 9/11**——这是统一 diff 与 sympy 大文件上下文对不上的真问题；50 题仍不可达（镜像仓库 403 / Docker Hub 超时，无法批量准备环境） |
| Top-5 文件定位命中率 | 给出基线与消融表 | 待测（P2） |
| 单题成本 / P95 耗时 | 随迭代**持续下降**，并给出曲线 | 待测（P4） |
| 复现率（能稳定复现给定失败的比例） | 报告实测值 | 待测（P1） |

> 目标定在 25% 而不是更高：本项目不微调模型、不集成多智能体投票，
> 靠的是"检索 + 沙箱验证 + 有限轮次自省"这条干净链路。**如实标注上限比虚报数字重要**。

### 2.4 工程验收（可部署、可复现、可回归）

| # | 验收项 | 判定方式 | 状态 |
|---|---|---|---|
| E1 | 一条命令启动依赖服务 | `pp dev-up` → postgres/redis/qdrant 全 Healthy | **已达标** |
| E2 | 一条命令跑通全链路 | `pp smoke` → 输出状态机轮次、是否通过、熔断情况、trace 落盘 | **已达标** |
| E3 | 上传前确定性门禁全绿 | `pp check` → 10 项检查全部真实执行 | **已达标**（`pass=10 fail=0 skip=0`） |
| E4 | **批准绑定到确切字节**：任何人克隆后能复算出同一摘要 | `pp check` 输出的 `TREE_DIGEST` 与批准值逐字符相同；跨平台（LF 统一） | **已达标**（两处路径同摘要） |
| E5 | 成本可控：token / 金额 / 时长 / 轮次四重熔断 | 超限即终止并归因为 BUDGET，仍落 trace | **已达标** |
| E6 | 状态可持久化，进程重启后能续跑 | sqlite 与 Postgres 两种 checkpointer（含断连重连） | **已达标**（4 passed / 真机 3/3） |
| E7 | CI 与本地同一套检查 | `.github/workflows/ci.yml` 调同一个 `pp check` | **已达标** |
| E8 | 沙箱交付物本身不引入漏洞 | 门禁对"会被提交的字节"做密钥扫描与范围一致性校验 | **已达标**（第七轮 3 项阻塞已修：索引字节绑定、pytest 执行范围、非 UTF-8 密钥不得静默 pass，共 12 条回归用例） |

### 2.5 怎么验（复制即可）

```powershell
conda activate patchpilot
cd D:\Code\PatchPilot
pp doctor                       # 环境自检
pp dev-up                       # 起依赖服务，应全部 healthy
pp smoke                        # 全链路冒烟，应输出 attempts / approved / budget_exceeded
pp check                        # 10 项确定性门禁；末尾打印 TREE_DIGEST
pp check --json                 # 同上，机器可读
```

`pp check` 通过后，摘要值就是"这一份字节被批准了"的凭据；第三方克隆仓库后复算应得到同一个值。

---

## 3. 快速开始

前置：Anaconda、Docker Desktop（P1 起需要）、git。

```powershell
conda activate patchpilot
cd D:\Code\PatchPilot
pip install -e ".[dev]"
pp doctor
```

首次配置模型（P3 起需要，**不要提交 `.env`**）：

```powershell
Copy-Item .env.example .env
# 编辑 .env：设置 PP_LLM_PROVIDER 与 PP_LLM_API_KEY
```

## 4. 目录结构

```
PatchPilot\
├─ PLAN.md              计划 + 阶段状态 + 变更日志（唯一事实来源）
├─ agents\              子代理角色定义
│  ├─ code-reviewer.md  上传前语义审查（只读，有否决权）
│  └─ git-publisher.md  提交与推送（必须持审查通过令牌）
├─ src\patchpilot\
│  ├─ core\             config / logging / models / budget / pricing / llm / preflight
│  ├─ graph\            checkpointer（memory|sqlite|postgres）+ 状态机
│  ├─ observability\    tracing（span + 用量 + 成本 + JSONL）
│  ├─ sandbox\ index\ eval\    P1 / P2 / P4 实现位
│  └─ cli.py            pp doctor / dev-up / dev-down / smoke / check
├─ tests\               单元测试（配置、预算熔断、定价、状态机、门禁）
├─ docker-compose.yml   postgres + redis + qdrant
└─ .github\workflows\ci.yml
```

## 5. 质量门禁

任何代码上传前必须**同时**通过两道门禁，否则拒绝上传：

| 门禁 | 执行者 | 内容 |
|---|---|---|
| 确定性门禁 | `pp check` | 语法解析、ruff、pytest、**密钥扫描**、`.gitignore` 覆盖、git↔遍历范围一致性、工作区↔索引字节一致性、`.env` 未被跟踪 |
| 语义门禁 | `code-reviewer` 子代理 | 正确性、资源释放、安全、与 PLAN 决策一致性、可测试性 |

两道门禁的**共同前提**是同一个 `TREE_DIGEST`：它覆盖的文件集合由摘要、语法检查、ruff、密钥扫描四者共用，且同一份逻辑内容在不同路径（git 仓库 / 普通目录）下必须算出同一个值——这样"批准"才真正绑定到确切字节。完整协议见 [PLAN.md 第 8 节](PLAN.md)。

## 6. 路线图与现状

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 工程骨架：CLI、配置、预算熔断、LangGraph 最小图、checkpointer、docker compose、CI、双门禁协议 | ✅ **已完成并发布**（`0ad9ea8` 已推送 `origin/main`） |
| **P1** | 沙箱与工具：Docker 隔离 + 4 个 MCP Server（fs/git/test/search）+ 4 类安全用例 | ✅ **已完成**（验收项 2）：沙箱驱动层 `manager.py`（沙箱内跑 pytest → JUnit 结构化返回）+ **4 个 MCP server 已在 stdio 协议层验证**；自建执行镜像 `docker/sandbox/Dockerfile`；**真机 Docker 端到端 15/15**（uid=1000、宿主不可见、rootfs 只读、网络不可达、内存 OOM rc=137、pids 超限 fork 被拒、超时截断且无残留）；单测 452 |
| **P2** | 检索与定位：符号索引、BM25 + 向量 + 调用图召回、重排 | ✅ **已完成**：纯标准库符号索引（`ast`）+ BM25（标识符拆分）+ **词法哈希向量** + 调用图扩散 + RRF 融合（含每通道 top-K 截断）+ 可解释重排；`pp locate` 出 **8 组消融表**。**题集已冻结**（按每题 commit 取当时的代码树），42 题 = 语料 6 + 合成 20 + 历史 16。**Top-1：基线 57.1% → 四路完整 76.2%（McNemar p=0.0215，显著）**，Top-5 61.9% → 88.1%、MRR 0.824；但**组内差异不显著**（vs 仅词法 p=1.0、vs 仅向量 p=0.625），故不宣称「四路最好」。**未做**：语义 embedding（需模型依赖，与离线门禁冲突） |
| **P3** | 修复闭环：补丁生成、`fail_to_pass` / `pass_to_pass` 判定、有限轮次自省 | 未开始 |
| **P4** | 评测闭环：SWE-bench 子集 runner、消融矩阵、成本/耗时曲线 | 🔧 **FastEval 已实现**：`pp eval` 一条命令出报告 —— 自建 6 题 `pass@1 = 6/6`、总成本 \$0.0040、P95 15.1s（温度 0、镜像锁定 tag，报告含题目 id/commit/镜像，第三方可照着重跑）；**待做**：BenchEval（SWE-bench Lite 50 题）与策略消融矩阵 |
| **P5–P7** | 可观测性、服务化、Web 控制台 | ✅ **已完成（有意简化）**：可观测性 = 自建 JSONL trace（四类 span）+ pp trace 回放；服务化 = pp serve（FastAPI 只读面板）；一键部署 = docker compose up 起 panel 服务。**未采用** OTel/Langfuse 与 Next.js：面板零前端构建（单页内联 HTML/JS，镜像 237MB），只读不提供执行入口 |

**本 README 里的"已达标"仅指已验证的部分**；未开始的阶段一律标注"未开始"，不预支结论。

## 7. 许可

Apache-2.0
