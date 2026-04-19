# ResearchOS

知识驱动的多智能体科研操作系统，基于 [OpenHarness](./OpenHarness) 构建。

全自主科研流水线：从论文抓取 → 知识编译 → 研究发现 → 实验设计与执行 → LaTeX 论文 → 投稿打包，全流程闭环。本版本（v4）吸收了 AI Scientist v1/v2、AI-Researcher（HKU）、DeepScientist、CycleResearcher、Zochi、FARS 等系统已被 peer review 或开源复现的机制，同时精简了先前版本中冗余的审查链路。

## 新版核心升级（v4）

**堵幻觉红线**（Wave 1）
- **A12 原子概念 ↔ 代码双向映射**（吸收 HKU AI-Researcher）— 新增 `concept_extractor` agent，MAIN 阶段 DESIGN_REVIEW 之后、CODE 之前强制产出 `concepts.yaml`，Coder 必须按 `concept.id` 逐项实现或显式 SKIPPED，`preflight.py` 逐项校验缺项直接 BLOCK
- **A16 Advisor 每概念裁决** — Critic 额外产出 `advisor_verdict.yaml`，给每个原子概念 pass/fail + 建议
- **A1 独立 CLI 重执行验证**（吸收 DeepScientist，原创实现）— RUN 后新增 RESULTS_REVERIFY 步，在 scratch 目录用独立子进程固定 seed 重跑，比对 metrics（>10% 漂移 BLOCK）
- **A6 引用约束到 evidence_refs**（吸收 CycleResearcher + AI Scientist v1）— paper_writer 只能引用 `knowledge/raw/` 或 `evidence_refs` 里的论文，paper_auditor 做 bib 存在性扫描

**实验质量**（Wave 2）
- **A3 / A18 Tree search + flat Node + Journal**（吸收 AI Scientist v2）— 新 `branch_manager.py` 实现 BFTS 原语：flat Node dataclass（无子类层次）+ Journal + `select_parallel_nodes` + 4 个 stopping criteria。调度器在每次 CRITIC 完成后自动把结果作为 Node 追加进 `journal.json`（持久化记忆），BFTS 选择策略本身 opt-in（`research_config.yaml::branching.enabled`）
- **A5 VLM 图表双通门**（吸收 v2）— `vlm_reviewer` 含 3 段 prompt（review / selection / minimal）。调度器在 **CRITIC 完成后自动 dispatch**（若 pipeline 目录有 png/jpg），产出 `vlm_review.json`；另外 paper_auditor 写作末期也用（双通门）
- **A19 LaTeX 写作硬化**（吸收 v1）— paper_writer 含固定 section 顺序、"I am done" 哨兵、13 项 error_list、chktex 5 轮修正

**基建 + 精简**（Wave 0 + 3 + R）
- **A13 LLM 调用层硬化**（吸收 v1）— `llm_client.py` 含 `extract_json_between_markers`、控制字符清洗、backoff 重试、前缀路由（claude/gpt/o1/ollama）、`</think>` 剥离、150s 启动错峰；dispatcher 的 `executor.execute` 出口通过 `_retry_llm_async` 统一包裹指数退避 + 抖动，最多 5 次重试
- **A14 重试常量中心化** — `max_runs=5`、`max_iters=4`、`exp_timeout=7200`、`plot_timeout=600`、`max_stderr=1500` 统一到 `runtime/workflow_config.yaml`；`RetryPolicy(project_root=...)` 构造时自动加载（不再硬编码 `max_retries=3`）
- **A9 Reviewer 升级**（吸收 v1）— ensemble=3 + pos/neg 双 prompt + Area Chair 元聚合 + `"I am done"` 早退；R3 不再重算 G4
- **A11 HITL 3 点 + 规范化动作枚举**（吸收 DeepSci OSS）— `CanonicalAction` 枚举（continue/launch/branch/write/finalize/stop/tweak），pre-MAIN + milestone-N + pre-publish 三个 gate 复用 `user_approve/` 机制
- **A17 Baseline metric contract**（吸收 DeepSci OSS）— `BASELINE_CONFIRM` 是 FAST_FAIL 的**第一步**（由调度器 Wave2 edges 自动插入）：有 baseline 块就固化 `metric_contract.json`，无 baseline 就 SKIP 放行；跑完对表
- **A2 L1 `bash_exec` 沙箱**（吸收 DeepSci OSS）— `remote_executor.py` 新增 `sandbox` 后端：工作目录白名单 + 1800s watchdog + CUDA 环境变量 + JSONL 日志
- **A7 Findings Memory**（吸收 DeepSci OSS 结构）— `findings_memory.py` 支持 MEMORY_KINDS(`papers/ideas/decisions/episodes/knowledge/templates/findings`) + markdown+YAML frontmatter + 阶段偏置检索；Critic SUPPORTED 时自动落库
- **A8 5 轴快速门** — `topic_evaluator.py` 第 0 步：novelty/falsifiability/feasibility/evidence_quality/constraint_fit 各 0/1/2，总分 < 7/10 直接拒绝
- **A15 4 槽结构化实验 plan**（吸收 HKU）— `experiment_plan.yaml` 顶层应含 `plan_dataset` / `plan_model` / `plan_training` / `plan_testing`；`design_checks.py::check_four_slot_plan` 做 schema 校验（缺槽 ERROR→BLOCK，缺子键 WARN），对全无 4 槽键的旧格式 plan 向后兼容
- **A10 Reviewer drift 测试** — `tests/test_reviewer_drift.py` 结构化 Proxy MAE 占位测试，季度跑真实评估
- **R1 `fix` / `continue` 统一** — `fix --user-feedback <path>` 与 `continue` 走同一条路径；`continue` 保留为别名
- **R2 研究简报 Python 化** — 调度器在添加候选时优先调用 `briefer.py::render_brief()`（读取 `evaluated_<id>.yaml` 的 5D 评分 + 创新声明 + 证伪设计 + KB 片段），失败或缺 YAML 时回退到旧模板；`research_briefer` agent 不再参与生产路径

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                   OpenHarness 前端（React 终端 UI）                   │
│  /research dashboard │ launch │ fix │ continue │ approve │ priority │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌──────────────────────────┐  ┌──────────────────────────────────────┐
│  心跳模块（arXiv 抓取）  │  │  知识编译器（PDF → wiki）             │
│  每日 PDF 下载 +         │→ │  /knowledge/raw/ → /knowledge/wiki/   │
│  Semantic Scholar 过滤   │  └──────────────┬───────────────────────┘
└──────────────────────────┘                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│         知识库 + Findings Memory（BM25 + n-gram + 阶段偏置）          │
│  wiki/ : concepts, methods, experiments, failures, datasets          │
│  findings/ : 已被 Advisor 验证的 finding（fully_correct=True 时落库） │
└──────────┬──────────────────────────────────────┬───────────────────┘
           ↓                                      ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  知识矿工            │    │  研究调度器                               │
│  方向感知的          │    │  无状态 tick 驱动，按优先级调度            │
│  空白发现            │    │  预算控制，里程碑检查点                    │
└────────┬────────────┘    └────────────────┬─────────────────────────┘
         ↓                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Topic Evaluator（0+8 步）                                            │
│  [0] 5 轴 0/1/2 快速门（<7/10 拒绝）                                   │
│  [1-8] BM25 检索 → 在线检索 → 相似度 → 评分 → 创新声明 → 证伪 → 门禁    │
└─────────────────────┬────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Professor（LLM，仅门禁边界案例）  ↔  Pipeline 状态机                   │
│  PROPOSAL → BASELINE_CONFIRM → FAST_FAIL                              │
│          → [HITL gate 1: pre-MAIN] → MAIN                            │
│          → [HITL gate 2: milestone-N] → ...                          │
│          → PAPER → [HITL gate 3: pre-publish] → PDF                  │
└──────────────────────────────────────────────────────────────────────┘
```

## 完整科研生命周期

```
原始论文（PDF / arXiv 心跳抓取）
  → 知识编译器提取全文 → wiki 知识页面
    → 知识矿工扫描 wiki → 发现空白（含创新模式挖掘 + 用户偏好学习）
      → Topic Evaluator：[0] 5 轴快速门 → [1-8] KB/在线/评分/证伪/门禁
        → 通过 → 候选列表（runtime/candidates/ + briefer.py 确定性简报）
          → 用户选择 → Pipeline BASELINE_CONFIRM（写 metric_contract.json）
        → Pipeline FAST_FAIL（精简路径；skip 重型质量层）
          → BASELINE_CONFIRM（写 metric_contract.json 或 SKIP）→ 假设 → 设计 → [代码?] → 运行 → 分析（G9 数据审计）→ 报告
            → [HITL gate 1: pre-MAIN] 阅读 pre_main_review.md + user_approve/
               ├─ /research fix --user-feedback → 修复并进入 MAIN
               ├─ /research review --approve  → 直接进入 MAIN
               └─ /research review --kill     → 终止
          → MAIN（完整质量链）
            → 假设 → 设计 → 设计审查 → CONCEPT_EXTRACT（concepts.yaml）
               → 代码（按 concept.id 逐项映射）→ 代码预检（每 concept 存在性 BLOCK）
               → 运行 → RESULTS_REVERIFY（独立子进程重跑比对）
               → Critic（含 advisor_verdict.yaml）
               → [post-CRITIC hook] VLM 图表 review（若有 png/jpg）+ Journal 节点追加
               → Interrogator → Report
              → [HITL gate 2: milestone-N（N≥2 自动暂停）] 复用 user_approve/
              → 达到最大里程碑 → PAPER
                → draft.md → Paper Auditor（G4 数值 + citation integrity + chktex）
                → Reviewer Simulator（ensemble=3 + pos/neg + Area Chair，不重算 G4）
                → paper.tex → paper.pdf
                → [HITL gate 3: pre-publish] → publications/<id>/
                  → 知识沉淀：Critic 判 supported → findings_memory 落 findings card
```

## 快速开始

```bash
# 安装依赖
pip install -e .
cd OpenHarness && pip install -e . && cd ..

# 启动 OpenHarness 交互会话
oh

# 在 oh 中使用 slash 命令：
#   /research launch              — 一键启动：心跳抓取 + 自主调度
#   /research launch --human-review  — Professor 写报告，你做最终决策
#   /research trial               — 心跳一次 + 一个课题 + 一个 Pipeline，跑完即停
#   /research trial --mode simulate  — 试运行，不调用 LLM（纯测试）
```

## 入口模式

| 模式 | 命令 | 流程 |
|------|------|------|
| 默认 | `launch` | 心跳 → 矿工 → 候选列表 → **用户选择** → Pipeline → 论文 |
| 全自主 | `launch --auto-select` | 心跳 → 矿工 → 自动创建 Pipeline → 论文 |
| 试运行 | `trial` | 心跳一次 → 矿工一次 → 跑一个 Pipeline → 停止 |
| 注入论文（Path A） | `inject paper1.pdf paper2.pdf` | 论文 → wiki → 矿工发现 → 候选+简报 → 用户选择 → Pipeline |
| 注入 + 提案（Path B） | `inject paper.pdf --title "..." --question "..."` | 论文 → wiki + 直接创建 Pipeline |
| 手动提案 | `create --title "..." --question "..." --refs ref1.pdf` | 直接创建 Pipeline |

## HITL 三点用户审核

v4 统一通过 `artifacts/pipeline_<id>/user_approve/` 机制，在三个关键位置等待用户决策：

| Gate | 位置 | 触发 | 产物 |
|------|------|------|------|
| **Gate 1 pre-MAIN** | FAST_FAIL 完成后 | 所有 pipeline | `pre_main_review.md` + inject 模式下完整 `user_approve/` 包 |
| **Gate 2 milestone-N** | MAIN 第 N 次里程碑（默认 N≥2） | 每次里程碑 | `milestone_N.md` + `user_approve/` 附加 |
| **Gate 3 pre-publish** | PAPER 定稿、LaTeX 编译前 | 所有 pipeline | `pre_publish_gate.md` + 草稿/评审/review.md |

### 统一决策命令

```bash
# Gate 1 pre-MAIN
python scripts/run_research.py review --pipeline ID [--approve | --tweak "directive" | --kill]
python scripts/run_research.py fix --pipeline ID --user-feedback artifacts/pipeline_ID/user_approve/feedback.md
python scripts/run_research.py continue --pipeline ID   # 等价于 fix --user-feedback

# Gate 2/3 待扩展 CLI；目前可直接写 decisions/approval_<id>_<ts>.yaml
```

决策 YAML 支持 `CanonicalAction` 枚举（`continue/launch/branch/write/finalize/stop/tweak`）与 `gate` 字段（`pre_main/milestone_N/pre_publish`），下游路由可直接机读。

## 注入模式用户反馈闭环

针对 `inject --title --question` 创建的 Pipeline，FAST_FAIL 完成后调度器把审查素材复制到本地 `user_approve/` 让用户直接在文件系统里写意见：

```
artifacts/pipeline_<id>/user_approve/
├── papers/                     # 原始论文 PDF（从 evidence_refs 拷贝）
├── proposal.md                 # 标题、问题、初始假设快照
├── fast_fail_report.md         # critic analysis + pre_main_review 合并
├── professor_suggestion.md     # 从 pre_main_review 提取的修改建议段
├── feedback.md                 # **用户在此写修改意见**（含模板占位符）
└── README.md                   # 操作说明
```

编辑 feedback.md 后：

```bash
# 新的统一入口
python scripts/run_research.py fix --pipeline ID --user-feedback artifacts/pipeline_ID/user_approve/feedback.md

# 或旧别名（仍可用）
python scripts/run_research.py continue --pipeline ID
```

Fixer 收到的 prompt 里 USER FEEDBACK 段被标注 authoritative，覆盖 Professor 建议。fixer 按要求修改代码 → `execute_python` 重跑 → 写 `correction_report.md` → 清除 `user_approve_pending` → 写 approve 决策 → 下一 tick 进入 MAIN。

## 原子概念 ↔ 代码双向映射（A12）

### 动机
LLM 最顽固的 failure class 是"看似实现了但其实漏关键组件"（HKU 论文记录：GPT-4o 在 diffusion 任务中静默省略关键模块，仍声称完成）。v4 引入 AI-Researcher 的双向原子映射机制，对每一个参考论文里的原子概念都要求显式代码实现或显式 SKIPPED。

### 流程

1. **DESIGN_REVIEW 之后、CODE 之前**：调度器分发 `CONCEPT_EXTRACT` 步给 `concept_extractor` agent
2. Agent 按 HKU 三阶段协议（survey → paper analyst → code analyst）分解 `hypothesis.md` + `experiment_plan.yaml` + `evidence_refs`，产出 `concepts.yaml`：
   ```yaml
   - id: C1
     definition: "adaptive Q-value selection"
     math_formula: "softmax(Q/τ) over action space A"
     code_implementation: "adapt_selector.py::softmax_select"
     reference_papers: ["RefPaper2023.pdf"]
     reference_codebases: []
   - id: C2
     ...
   ```
3. **Coder** 读 `concepts.yaml`，实现时必须按 `id` 顺序产出 CLAIM-TO-CODE 表：
   ```
   # === CLAIM-TO-CODE MAPPING ===
   # | id | concept            | file::func                  | mechanism          |
   # | C1 | adaptive selection | adapt_selector.py::softmax_select | softmax over Q |
   # | C2 | SKIPPED because ... |                            |                    |
   # === END MAPPING ===
   ```
4. **preflight.py `_check_g3_mapping`** 升级：按 `concept.id` 逐项校验
   - 每个 id 必须在表中出现（mapped 或 SKIPPED）
   - mapped 的必须在代码里能找到对应 file/func
   - 缺失概念 → ERROR → 整体 verdict = BLOCK（不是 WARN）
   - 无 concepts.yaml 时回退到旧的行数统计
5. **Critic** 除 `analysis_report.md` 外，额外产出 `advisor_verdict.yaml`：
   ```yaml
   pipeline_id: demo
   fully_correct: false
   per_concept:
     C1: pass
     C2: fail
   suggestions:
     C2: "implement ablation with this concept removed"
   ```

### 为什么重要
- **堵 LLM 静默漏实现**：缺概念直接 BLOCK，无法走到 RUN
- **可追溯**：每个代码块能反查回参考论文的公式
- **Advisor 验证**：Critic 不再只看整体指标，还做每概念裁决

## Pipeline 状态机（v4）

```
PROPOSAL ──批准──→ FAST_FAIL(step=BASELINE_CONFIRM) ──→ FAST_FAIL(hypothesis → design → [code?] → run → critic → report)
                                      │
                                      │       [HITL gate 1: pre-MAIN]
                                      ↓
                           user_approve/（inject 模式完整包）
                                      ↓
                           /research fix --user-feedback 或 review --approve/tweak/kill
                                      ↓
MAIN ──→ hypothesis → design → design_review
     ──→ CONCEPT_EXTRACT（concepts.yaml）
     ──→ code（按 concept.id 逐项实现）
     ──→ code_preflight（每 concept 存在性 BLOCK）
     ──→ run
     ──→ RESULTS_REVERIFY（独立子进程固定 seed 重跑；>10% 漂移 BLOCK）
     ──→ critic（含 advisor_verdict.yaml）
     ──→ interrogator → report
     ──→ milestone
         │
         │  [HITL gate 2: milestone-N（N≥2 自动暂停）]
         ↓
         批准 → 下一 milestone / 达到 max → PAPER
         ↓
PAPER ──→ draft.md
      ──→ paper_auditor（G4 数值 + citation integrity + chktex）
      ──→ reviewer_simulator（ensemble=3 + pos/neg + Area Chair；不重算 G4）
      ──→ paper.tex → paper.pdf
      ──→ [HITL gate 3: pre-publish]
      ──→ publish → publications/<id>/
         │
         │  Critic SUPPORTED 时 findings_memory 落库
         ↓
      COMPLETED
```

FAST_FAIL 跳过 CONCEPT_EXTRACT、RESULTS_REVERIFY、DESIGN_REVIEW、CODE_PREFLIGHT、INTERROGATION 保持轻量。MAIN 全链启用。

## Professor 决策模式

| 模式 | 命令 | 行为 |
|------|------|------|
| 交互式 | `run interactive` | 用户就是 Professor |
| 全自主 | `run autonomous` | Professor LLM 直接决策；失败时暂停（不自动批准） |
| 人工审核 | `run autonomous --human-review` | Professor LLM 写推荐报告 → Pipeline 暂停 → 用户决策 |

### 决策类型 + CanonicalAction（A11）

| DecisionType | CanonicalAction 映射 | 效果 |
|------|------|------|
| `approve` | CONTINUE / WRITE / FINALIZE | 继续 / 推进阶段 |
| `kill` | STOP | 终止管线 + 知识沉淀 |
| `pivot` | BRANCH | 以新方向重启 MAIN |
| `pause` | (无) | 阻塞管线，等待人工介入 |
| `hold` | (无) | 阻塞管线，等待重新评估 |
| `tweak` | TWEAK | 注入指令，清理 DESIGN 之后的产物，从 HYPOTHESIS → DESIGN 重跑 |

YAML 决策文件支持两个新可选字段：`gate: pre_main | milestone_N | pre_publish` 和 `canonical_action: <CanonicalAction>`。后续路由无需 parse 自由文本 `comment`。

## 独立重执行验证（A1）

RUN 步完成后，调度器自动调用 `researchos.runtime.reverify.reverify_results`：

1. 复制 pipeline 目录到 `scratch/<pipeline_id>_reverify_<ts>/`
2. 在独立子进程跑 `run_experiment.py`（或 `experiment.py` / `main.py`），环境变量 `PYTHONHASHSEED=0`，参数 `--seed 42`
3. 捕获 stdout/stderr 最后 1500 字符，解析最后一行 JSON
4. 与原 `results.json` 比对：
   - `stdout_hash_match`: bool
   - `metrics_diff: {metric: {original, rerun, rel_pct}}`
   - `verdict`: PASS（≤2% 相对误差）/ WARN（2–10%）/ BLOCK（>10% 或崩溃）/ SKIP（找不到入口脚本）
5. 写 `reverify_report.json`

纯 Python，零 LLM 调用。MAIN 阶段强制启用；FAST_FAIL 跳过。

## Tree Search（A3/A18）

### 架构

`researchos/runtime/branch_manager.py` 提供 v2 AI-Scientist 的 BFTS 原语。遗留的 `BranchState` / 旧 `BranchManager` 已删除，K 路并行分支现统一由 `BranchRunner` 驱动（内部用 Journal + Node）：

- **`Node`**: flat dataclass（~18 字段，布尔标志区分变体而非子类层次）
- **`Journal`**: `list[Node]` 容器 + O(1) 访问器（`draft_nodes`/`buggy_nodes`/`good_nodes`）+ `get_best_node()`（两级：metric 筛选 → LLM 兜底）
- **`debug_depth(node)`**: 沿父链数连续 `is_buggy=True` 祖先数
- **`select_parallel_nodes(journal, cfg)`**: BFTS 策略
  - 若 `len(draft_nodes) < num_drafts`：生成新 draft
  - 否则以 `debug_prob=0.5` 概率挑选 `debug_depth ≤ max_debug_depth` 的 buggy leaf
  - 否则挑选最佳 good_node 做 improve
- **`stage_complete(journal, stage, cfg)`**: 4 阶段停止判据

默认参数在 `runtime/tree_search_defaults.yaml`：

```yaml
num_workers: 4
num_drafts: 3
debug_prob: 0.5
max_debug_depth: 3
multi_seed_eval:
  num_seeds: 3
exec:
  timeout: 3600
stages:
  stage1_max_iters: 20
  stage2_max_iters: 12
  stage3_max_iters: 12
  stage4_max_iters: 18
```

### 持久化
`artifacts/pipeline_<id>/journal.json`（由 `JournalPersistence.save/load` 管理）。

## Findings Memory（A7）

`researchos/runtime/findings_memory.py` 实现 DeepScientist 风格的跨 pipeline 记忆：

```
knowledge/findings/
├── index.jsonl                # 按创建时间的全局索引
├── papers/<id>.md             # YAML frontmatter + body
├── ideas/<id>.md
├── decisions/<id>.md
├── episodes/<id>.md
├── knowledge/<id>.md
├── templates/<id>.md
└── findings/<id>.md           # 已被 Advisor 验证的 finding
```

调度器在 COMPLETED 转移（kill / knowledge / paper finish）时读 `advisor_verdict.json`，当 `fully_correct=True` 时自动把 pipeline 转成 `FindingCard` 持久化。

### 检索
`MemoryService.search(query, kind=None, top_k=15)` 做子串匹配 + 阶段偏置（scout 阶段偏好 papers/knowledge，experiment 阶段偏好 ideas/episodes，analysis 阶段偏好 findings）。向量层留作后续升级。

## Baseline Metric Contract（A17）

`researchos/runtime/baseline_registry.py`：

```python
class MetricContract(BaseModel):
    task: str
    dataset: str
    eval_script: str
    metrics: dict[str, Literal["higher", "lower"]]
    baseline_values: dict[str, float]
    source_commit: str
    known_deviations: list[str]
```

`BASELINE_CONFIRM` step 在 PROPOSAL → FAST_FAIL 之间读 `experiment_plan.yaml` 的 `baseline` 块，写 `artifacts/pipeline_<id>/metric_contract.json`。实验跑完 `check_comparability` 对表，移动门槛自动拒绝。

无 baseline 块时调用 `waive_baseline(reason)` 显式放弃。

## Sandbox 执行后端（A2 L1）

`researchos/runtime/remote_executor.py::BashExecSandbox`：

```python
execute_backend("sandbox", cmd=..., cwd=..., project_root=..., timeout=1800, cuda_device="0")
```

- POSIX 走 PTY，其他走管道
- 工作目录白名单：只允许 `project_root/artifacts`、`data`、`scratch` 及子目录
- 默认 1800s watchdog
- 支持 `CUDA_VISIBLE_DEVICES` 环境变量注入
- JSONL 日志 → `runtime/bash_exec.log`

比 Docker 轻，比裸 subprocess 安全。配置 `execution.backend: sandbox` 启用。

## Topic Evaluator 5 轴快速门（A8）

`topic_evaluator.py` 第 0 步：

```
novelty            0/1/2
falsifiability     0/1/2
feasibility        0/1/2
evidence_quality   0/1/2
constraint_fit     0/1/2
-----------------------
total              /10
```

< 7/10 → 直接拒绝（不进入后续 8 步完整评估）。简单、快速、几乎零 token 成本。保留原有 5D scoring 作为通过门后的精细化评分。

## 4 槽结构化实验 Plan（A15）

`experiment_plan.yaml` 顶层必须包含 4 个槽：

```yaml
plan_dataset:
  dataset_description: ...
  dataset_location: ...
  task_definition: ...
  read_data_step: ...
  data_preprocessing_step: ...
  data_dataloader_step: ...
plan_model:
  model_input: ...
  model_output: ...
key_components: [...]
plan_training:
  training_pipeline: ...
  loss_function: ...
  optimizer: ...
  training_configurations: ...
  monitor_and_logging: ...
plan_testing:
  test_metric: ...
  test_data: ...
  test_code: ...
# 可选顶层兄弟：requires_code, data, reference_code, user_directives_applied
```

preflight 据此 schema 校验，缺槽即报错。

## LLM 调用层（A13）

`researchos/runtime/llm_client.py`：

| 函数 | 作用 |
|------|------|
| `extract_json_between_markers(text)` | 优先 ```json``` 块，退化为第一个 `{...}` 段；剥离控制字符 |
| `strip_reasoning(text)` | 剥离 `</think>` 之前的思维链 |
| `retry_llm(fn, ...)` | `backoff.expo` 指数退避（有 backoff 库走它，否则本地回退） |
| `route_model(model)` | claude-* / gpt-\|o1-\|o3-* / ollama\|local/\* → 对应客户端类型，其余走 openrouter |
| `stagger_sleep(i)` | 150s × i 启动错峰，防止并发 pipeline 触发速率限制 |

全部 LLM 出口走 `Dispatcher.parse_json_output` 统一清洗。

## 重试 / 超时 / 预算常量（A14）

`runtime/workflow_config.yaml`：

```yaml
retry:
  max_runs: 5
  max_iters: 4
  exp_timeout_seconds: 7200
  plot_timeout_seconds: 600
  max_stderr_chars: 1500
```

`RetryPolicy(project_root=...)` 自动加载。单一数据源，无硬编码。

## Reviewer 升级（A9 + R3）

`reviewer_simulator.md` 结构：

1. **双系统 prompt**: `reviewer_system_prompt_pos`（constructive）+ `reviewer_system_prompt_neg`（adversarial）
2. **Ensemble**: `num_reviews_ensemble = 3`，temp=0.75，每次 Reflexion 自反省（`"I am done"` 早退，最多 `num_reflections = 2` 轮）
3. **Area Chair**: `get_meta_review()` 聚合 — 数值取均值，决定多数投票，std>1.0 标 dissent
4. **NeurIPS JSON schema**: Summary / Strengths / Weaknesses / Originality / Quality / Clarity / Significance / Soundness / Presentation / Contribution / Overall / Confidence / Decision / Questions / Limitations / Ethical Concerns
5. **R3**: 不重算 G4 数值（paper_auditor 已做）— reviewer_simulator 必须引用 `audit_report.md` 而非自己跑计算

## Paper Writer 硬化（A6 + A19）

### A6 引用约束
硬规则："只能引用 `knowledge/raw/` 或 `evidence_refs` 里的论文。每个 `\cite{key}` 必须能在 `references.bib` 查到且最佳匹配 evidence ref 的文件名 stem。不得发明新引用。"

### A19 写作硬化
- **Section 顺序**：Abstract → Introduction → Background → Method → Experimental Setup → Results → Conclusion（严格顺序）
- **自反省循环**：每节写完短 self-review，输出 `"I am done"` 字面量时退出
- **error_list**：13 项 LaTeX/写作检查（下划线未转义、数学符号未包裹、orphan `\cite`、重复节标题、`\begin/\end` 不匹配、inline 数学无分隔符、`??` 未解析引用、无新引用等）
- **chktex 修正**：`num_error_corrections = 5` 轮，`chktex {path} -q -n2 -n24 -n13 -n1` 输出回灌修复 prompt

### paper_audit.py 新增
- `check_bib_existence(draft, bib, evidence_refs)` — 解析 `\cite{key}`，校验 bib 成员 + best-effort title 匹配；返回 `{total, resolved, hallucinated, unevidenced}`
- `run_chktex(tex_path)` — subprocess 调用 chktex
- `audit_report.md` 自动含 Citation integrity + LaTeX lint 两节

## 数据管理（v3 保留）

两阶段数据生命周期未变：

**Phase 1 — 数据感知**（agent prompt 层）：
- `exp_designer` 在 `plan_dataset` 中输出来源、描述、预处理、划分策略、风险标注
- `coder` 强制"先划分后预处理"，打印数据溯源
- `experiment_runner` 在 results.json config 中记录 `data_source`/`data_rows`/`split_seed`/`data_hash`
- `critic` 执行 G9 数据有效性审计

**Phase 2 — 数据生命周期**（代码基础设施）：
- `fetch_dataset` 工具（第 5 个 agent 工具）从 URL 下载到 `/data/raw/<pipeline_id>/`
- `data_manifest.py` 生成 SHA256 哈希 + 目录级哈希 + 行数统计
- `knowledge_compiler` 提取到 `wiki/datasets/data_<id>.md`
- `publisher` 生成 `REPRODUCIBILITY_CHECKLIST.md`

## 质量检查点（MAIN）

MAIN 阶段在 DESIGN、CONCEPT_EXTRACT、CODE、RUN 之后插入检查点：

| 位置 | 层次 | 实现 | 成本 |
|------|------|------|------|
| DESIGN_REVIEW | 结构 | `design_checks.py`（10 规则） | 0 token |
| DESIGN_REVIEW | 科学 | `design_reviewer` agent | ~2000 token |
| **CONCEPT_EXTRACT**（**新**） | 原子概念分解 | `concept_extractor` agent | ~2000 token |
| CODE_PREFLIGHT | 结构+概念映射 | `preflight.py` | 0 token |
| **RESULTS_REVERIFY**（**新**） | 重跑对表 | `reverify.py` 独立子进程 | 0 token |

FAST_FAIL 跳过 CONCEPT_EXTRACT、RESULTS_REVERIFY、DESIGN_REVIEW、CODE_PREFLIGHT、INTERROGATION 保持速度。

## 质询协议（MAIN，v3 保留）

MAIN 阶段在 Critic 之后、Report 之前插入 Interrogator：

- **异常追问**：指标不一致
- **反事实**：验证因果
- **边界测试**：鲁棒性
- **方法论质疑**：统计合理性
- **替代解释**：排除混杂因素

产出 `interrogation_report.md`，1–3 轮（`research_config.yaml` 中 `interrogation.rounds` 控制）。

## 命令索引

| 命令 | 功能 |
|------|------|
| `/research dashboard` | 管线看板（优先级、预算、方向、HITL 状态） |
| `/research launch [--human-review]` | 启动调度器 |
| `/research trial` | 心跳一次 + 一个 Pipeline 跑完即停 |
| `/research create --title "..." --question "..."` | 创建提案 |
| `/research inject file.pdf [--title "..." --question "..."]` | 导入论文 |
| `/research candidates list/select/reject/brief` | 候选课题管理（briefer.py 确定性渲染，非 LLM） |
| `/research review --pipeline ID [--approve | --tweak "..." | --kill]` | Gate 1 pre-MAIN 决策 |
| `/research fix --pipeline ID [--user-feedback PATH]` | 统一修复命令（带 --user-feedback 即旧 continue） |
| `/research continue --pipeline ID` | `fix --user-feedback artifacts/pipeline_ID/user_approve/feedback.md` 别名 |
| `/research query --pipeline ID "问题"` | 对话式交互 |
| `/research tweak --pipeline ID "指令"` | 注入指令到 user_directives.yaml |
| `/research explore --pipeline ID` | 查看中间产物 |
| `/research publish --pipeline ID` | 打包投稿 |
| `/research approve ID [--paper]` | 批准（可指定进入论文阶段） |
| `/research kill ID` | 终止管线 |
| `/research priority ID 0-3` | 设置优先级 |
| `/research pause-miner / resume-miner` | 暂停/恢复矿工 |
| `/heartbeat status / enable / disable / fetch` | 心跳控制 |

## 目录结构

| 目录 | 用途 | 写入者 |
|------|------|--------|
| `researchos/` | Python 核心包 | 开发者 |
| `.openharness/plugins/researchos/` | 插件：agents（含 concept_extractor）、commands、hooks | 开发者 |
| `knowledge/raw/` | 原始 PDF 论文 | 心跳 / 手动 |
| `knowledge/wiki/` | 编译后的知识 | 知识编译器 / 用户 |
| `knowledge/proposals/` | 矿工研究提案 + 评估报告 | 矿工 / Topic Evaluator |
| `knowledge/backlog/` / `archive/` | 候补 / 过期 / 永久拒绝 | BacklogManager |
| **`knowledge/findings/`** | **已验证 finding 的跨 pipeline 记忆** | findings_memory（A7） |
| `artifacts/pipeline_<id>/` | 状态、产出、**concepts.yaml**、**reverify_report.json**、**metric_contract.json**、**advisor_verdict.yaml**、**user_approve/** | Pipeline Agent / 调度器 |
| `artifacts/pipeline_<id>/workplace/` | （可选）Docker / sandbox 后端工作目录 | remote_executor |
| `data/raw/` / `data/processed/` | 原始/清洗数据 | fetch_dataset / coder |
| `runtime/candidates/` | 候选课题 + 简报（briefer.py 渲染） | Topic Evaluator / briefer |
| `runtime/bash_exec.log` | sandbox 后端 JSONL 日志（A2） | BashExecSandbox |
| **`runtime/tree_search_defaults.yaml`** | **BFTS 默认超参（A3）** | 配置 |
| **`runtime/workflow_config.yaml`** | **步骤路由 + 重试常量（A14）** | 配置 |
| `decisions/` | Professor 决策 + HITL gate 决策 YAML | Professor / 用户 |
| `paper/<pipeline_id>/` | 论文输出（md/tex/pdf） | paper_writer |
| `publications/<pipeline_id>/` | 投稿打包 | publisher |
| `scratch/<pipeline_id>_reverify_<ts>/` | A1 独立重执行临时沙盒 | reverify |

## 核心组件（runtime）

| 模块 | 职责 | Wave |
|------|------|------|
| `scheduler.py` | Tick 驱动调度器 + Wave 2 edges 叠加 + HITL 3 gate + findings hook | — |
| `dispatcher.py` | Prompt 构建 + 多后端路由 + `Dispatcher.parse_json_output` | A13 |
| **`llm_client.py`** | **`extract_json_between_markers` / `strip_reasoning` / `retry_llm` / `route_model`** | **A13** |
| `state_manager.py` | 原子化文件 CRUD | — |
| `topic_evaluator.py` | 0+8 步评估（5 轴门 + 完整 8 步） | A8 |
| `agent_executor.py` | OpenAI 兼容工具调用循环 | — |
| **`briefer.py`** | **研究简报纯 Python 渲染器** | **R2** |
| `knowledge_compiler.py` | PDF/MD → wiki；pipeline 产物 → wiki | — |
| `paper_audit.py` | G4 + G5 + **bib existence + chktex** | A6 A19 |
| `latex_compiler.py` | Markdown → LaTeX → PDF | — |
| `heartbeat.py` | arXiv PDF 抓取 + 引用过滤 | — |
| `publisher.py` | 投稿打包 + 期刊推荐 | — |
| `frontend_state.py` | React 看板 JSON + `user_approve_pending` 标志 | — |
| `recovery.py` | 检查点 + 指数退避 + **retry 常量化** | A14 |
| `design_checks.py` | 实验设计结构验证 | — |
| `preflight.py` | 代码预检 + **concepts.yaml 逐项校验** | A12 |
| **`reverify.py`** | **独立子进程重执行 + 指标对比** | **A1** |
| `hooks.py` | 产物写入、KB 访问、预算、schema 校验 | — |
| `workflow_loader.py` | YAML 工作流配置 | — |
| `remote_executor.py` | local/docker/ssh + **sandbox** 后端 | A2 |
| `experiment_tracker.py` | W&B / MLflow 集成 | — |
| `branch_manager.py` | **Node/Journal/BFTS + BranchRunner**（K 路并行统一走 BFTS） | A3/A18 |
| **`findings_memory.py`** | **MEMORY_KINDS + MemoryService + 阶段偏置检索** | **A7** |
| **`baseline_registry.py`** | **MetricContract + confirm/waive/check_comparability** | **A17** |

## 智能体（21 个）

| Agent | 职责 | Wave |
|------|------|------|
| `professor` | 评估提案、审查里程碑、approve/kill/pivot/pause/hold/tweak | — |
| `knowledge_miner` | 方向感知的知识空白发现 | — |
| ~~`research_briefer`~~ | ~~≤500 词候选简报~~（**R2 降级为 `briefer.py::render_brief`**） | R2 |
| `hypothesis_gen` | 可证伪假设 + G1 关键词可操作化 | — |
| `exp_designer` | **4 槽结构化实验 plan** + G2/G5/G7/G9 | A15 |
| `design_reviewer` | MAIN 科学性审查 + 结构检查 | — |
| **`concept_extractor`**（**新**） | **原子概念 ↔ 代码双向分解** | **A12** |
| `coder` | **按 concept.id 逐项实现** + G3/G6/G9 | A12 |
| `experiment_runner` | `execute_python` + `fetch_dataset` | — |
| `critic` | 结果分析 + G1/G3/G5/G9 + **advisor_verdict.yaml** | A16 |
| `interrogator` | 对抗式质询（MAIN，1-3 轮） | — |
| `reporter` | 里程碑报告 + G7 开销 | — |
| `pre_main_reviewer` | FAST_FAIL 后 ≤800 词评审 | — |
| `paper_writer` | 学术论文 + **G6/G8 + A6 引用约束 + A19 硬化** | A6 A19 |
| `paper_auditor` | G4 + G5 + **bib + chktex** | A6 A19 |
| `reviewer_simulator` | **ensemble + pos/neg + Area Chair；不重算 G4** | A9 R3 |
| `fixer` | 按评审/用户反馈修正代码并重跑 | R1 |
| `lit_scout` | kb_retrieve 文献调研 | — |
| `vlm_reviewer` | **3 段 prompt（review/selection/minimal）+ duplicate detection** | A5 |
| `branch_selector` | 并行实验分支比较选择 | — |
| `pipeline_manager` | 通用兜底（无专职 agent 时接管步骤） | — |

## 质量门 G1–G9（v4 增强）

| 门 | 审查内容 | 位置 |
|----|---------|------|
| G1 | 主张-实验一致性 | hypothesis_gen, critic |
| G2 | 因果与对照有效性 | exp_designer |
| **G3** | **实现真实性** | **coder（concept 映射）, code_preflight（每 concept BLOCK）, critic（结果级 + advisor_verdict）** |
| G4 | 数据与结果一致性 | paper_auditor（自动化 + LLM） — **reviewer_simulator 不再重算（R3）** |
| G5 | 统计报告完整性 | exp_designer, critic, paper_auditor |
| G6 | 可复现性 | coder, publisher |
| G7 | 开销与资源报告 | exp_designer, reporter |
| G8 | 结论边界 | paper_writer, reviewer_simulator |
| G9 | 数据有效性与溯源（含参考数据一致性） | exp_designer, coder, experiment_runner, critic |

## 吸收/精简对照（A1–A19 + R1–R5）

| ID | 机制 | 源 | 位置 |
|----|------|------|------|
| A1 | 独立 CLI 重执行验证 | DeepScientist（paper）| `reverify.py` / `RESULTS_REVERIFY` |
| A2 | Sandbox 执行后端 | DeepScientist OSS | `remote_executor.py::BashExecSandbox` |
| A3 | Tree search + Journal | AI Scientist v2 | `branch_manager.py` (new) |
| A5 | VLM 图表门 | AI Scientist v2 | `vlm_reviewer.md` |
| A6 | 引用约束 | CycleResearcher + v1 | `paper_writer.md` + `paper_audit.py` |
| A7 | Findings Memory | DeepScientist OSS | `findings_memory.py` + scheduler hook |
| A8 | 5 轴快速门 | DeepScientist OSS | `topic_evaluator.py::five_axis_quick_gate` |
| A9 | Reviewer ensemble + pos/neg + Area Chair | AI Scientist v1 | `reviewer_simulator.md` |
| A10 | Reviewer drift 测试 | v1 fewshots | `tests/test_reviewer_drift.py` |
| A11 | HITL 3 点 + CanonicalAction | DeepScientist OSS | `decision.py` + scheduler |
| A12 | 原子概念 ↔ 代码双向映射 | **HKU AI-Researcher** | `concept_extractor.md` + `concept.py` + `preflight.py` |
| A13 | LLM 调用层硬化 | AI Scientist v1 | `llm_client.py` |
| A14 | 重试常量中心化 | AI Scientist v1 | `workflow_config.yaml` + `recovery.py` |
| A15 | 4 槽结构化 plan | HKU | `exp_designer.md` |
| A16 | Advisor per-concept 裁决 | HKU | `critic.md` + `concept.py::AdvisorVerdict` |
| A17 | Baseline metric contract | DeepScientist OSS | `baseline_registry.py` + `BASELINE_CONFIRM` |
| A18 | Flat Node + Journal 两级选择 | AI Scientist v2 | 合并入 A3 |
| A19 | LaTeX 写作硬化 | AI Scientist v1 | `paper_writer.md` + `paper_audit.py` |
| R1 | `fix` / `continue` 统一 | — | `scripts/run_research.py` |
| R2 | Briefer Python 化 | — | `briefer.py` 取代 `research_briefer` agent |
| R3 | Reviewer 不重算 G4 | — | `reviewer_simulator.md` |

## 配置

### 环境变量

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `RESEARCHOS_API_KEY` | LLM API 密钥 | （必填） |
| `RESEARCHOS_BASE_URL` | API 基础 URL | 无默认（必须显式设置） |
| `RESEARCHOS_MODEL` | 模型名称 | 无默认 |
| `RESEARCHOS_VLM_MODEL` | 视觉语言模型（图表审查） | 回退到 `RESEARCHOS_MODEL` |
| `RESEARCHOS_REVERIFY` | A1 独立重执行（`1`/`true` 启用） | `0`（默认关闭） |

### 多后端支持

`llm_client.route_model()` 按模型名前缀路由到正确的 SDK：

| 模型前缀 | 路由到 | 典型配置 |
|----------|--------|----------|
| `gpt-*`, `o1-*`, `o3-*`, `o4-*` | OpenAI 直连 | `BASE_URL=https://api.openai.com/v1` |
| `claude-*` | Anthropic 直连 | `BASE_URL=https://api.anthropic.com` |
| `ollama/*`, `local/*` | 本地 Ollama | `BASE_URL=http://localhost:11434/v1` |
| 其他（含 `qwen/*`, `deepseek/*`, `meta-llama/*`） | OpenRouter | `BASE_URL=https://openrouter.ai/api/v1` |

### 典型配置示例（`~/.zshrc` 或 `.env`）

```bash
# OpenAI 直连（当前推荐）
export RESEARCHOS_API_KEY="sk-..."
export RESEARCHOS_BASE_URL="https://api.openai.com/v1"
export RESEARCHOS_MODEL="gpt-5.4"                # 或你使用的 OpenAI 模型

# Anthropic 直连
export RESEARCHOS_API_KEY="sk-ant-..."
export RESEARCHOS_BASE_URL="https://api.anthropic.com"
export RESEARCHOS_MODEL="claude-opus-4-7"

# OpenRouter（混合后端，cost-sensitive）
export RESEARCHOS_API_KEY="sk-or-..."
export RESEARCHOS_BASE_URL="https://openrouter.ai/api/v1"
export RESEARCHOS_MODEL="qwen/qwen3.6-plus:free"

# 本地 Ollama（离线测试）
export RESEARCHOS_BASE_URL="http://localhost:11434/v1"
export RESEARCHOS_MODEL="ollama/llama3.1:70b"

# 启用独立重执行验证（默认关闭）
export RESEARCHOS_REVERIFY=1
```

VLM 用作独立模型（paper_auditor 的图表审查）时，设置 `RESEARCHOS_VLM_MODEL=gpt-4o` 或其他 vision-capable 模型。

## 依赖

- Python >= 3.10
- pydantic >= 2.0、pyyaml >= 6.0、openai >= 1.0、pymupdf >= 1.23
- `backoff`（可选，`llm_client.retry_llm` 优先使用，未装时本地回退）
- pdflatex + chktex（可选，`paper_writer` A19 需 chktex）
- 可选：`pip install researchos[vector|wandb|mlflow]`

## Docker 部署

```bash
docker compose up -d
export RESEARCHOS_API_KEY=your_key
docker compose up
```

`docker-compose.yaml` 自动挂载 knowledge/、artifacts/、runtime/、decisions/、paper/、data/、**`knowledge/findings/`** 为持久卷。

## 运行

```bash
python scripts/run_research.py launch                  # 全自主
python scripts/run_research.py launch --human-review   # 带人工审核
python scripts/run_research.py trial                   # 单课题试运行
python scripts/run_research.py trial --mode simulate   # 不调用 LLM
```

## 测试

```bash
python -m pytest tests/ -q
# 207 passed, 4 skipped（v4 基线）
```

`tests/test_reviewer_drift.py`（A10）包含 reviewer 质量的 Proxy MAE 结构化占位，季度跑真实评估时填入。

## 核心设计原则（v4）

1. **基于文件的状态机** — 所有状态在 JSON/YAML 中；调度器无状态、幂等
2. **知识驱动** — 通过 `kb_retrieve`（BM25 + n-gram + 可选 FAISS）访问知识库
3. **原子概念即代码契约**（A12） — 每个参考论文原子概念必须显式映射到代码或显式 SKIPPED
4. **独立验证优先于信任**（A1） — 主流程跑完后独立子进程重跑比对
5. **规则先行、LLM 后置** — 5 轴门 → topic_evaluator、design_checks → design_reviewer、preflight → critic、paper_auditor → reviewer_simulator
6. **Tree search 取代线性重试**（A3） — 失败分支不阻塞主线，debug_depth 可控
7. **HITL 3 点可中断、不必须**（A11） — pre-MAIN、milestone-N、pre-publish 均可 approve/tweak/kill
8. **每次 LLM 调用都过统一出口**（A13） — extract_json + strip_reasoning + backoff + 路由
9. **重试/超时/预算常量单一数据源**（A14） — `workflow_config.yaml::retry`
10. **findings 跨 pipeline 累积**（A7） — 已验证 finding 自动沉淀，可检索
11. **Baseline 实验前固化，不得移动门槛**（A17）
12. **引用必须来自 evidence_refs**（A6） — 堵引用幻觉
13. **精细成本追踪** — 每步 input/output tokens、模型、耗时、credits → `cost_report.json`
14. **步骤级产物过滤** — `_relevant_artifacts` 让每步只看必需 artifact
15. **硬字数/token 上限** — briefer ≤500w、pre-MAIN ≤800w、query 3000 字符截断
16. **reviewer 不重复做 auditor 已做的工作**（R3）
17. **单一修复入口**（R1） — `fix --user-feedback` 统一，continue 作为别名

## 许可证

基础框架许可证参见 OpenHarness。
