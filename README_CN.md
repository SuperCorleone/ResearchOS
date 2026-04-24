# ResearchOS

知识驱动的多智能体科研操作系统，基于 [OpenHarness](./OpenHarness) 构建。

全自主科研流水线：论文抓取 → 知识编译 → 假设发现 → 实验设计与执行 → LaTeX 论文 → 投稿打包。所有步骤落盘为文件；调度器幂等、可恢复；每一跳的契约由 Pydantic 强制。

版本变更记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                   OpenHarness 前端（React 终端 UI）                    │
│  /research dashboard │ launch │ trial │ approve │ priority           │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌──────────────────────────┐  ┌──────────────────────────────────────┐
│  心跳模块（arXiv 抓取）    │  │  知识编译器（PDF → wiki）              │
│  每日 PDF 下载 +          │→ │  /knowledge/raw/ → /knowledge/wiki/   │
│  Semantic Scholar 过滤   │  └──────────────┬───────────────────────┘
└──────────────────────────┘                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│         知识库 + Findings Memory（BM25 + n-gram + 阶段偏置）           │
│  wiki/ : concepts, methods, experiments, failures, datasets          │
│  findings/ : Critic supported 自动落库；failures/ 30 天衰减            │
└──────────┬──────────────────────────────────────┬───────────────────┘
           ↓                                      ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  知识矿工            │    │  研究调度器                                │
│  方向感知的          │    │  无状态 tick 驱动，按优先级调度              │
│  空白发现            │    │  预算控制，里程碑检查点                      │
└────────┬────────────┘    └────────────────┬─────────────────────────┘
         ↓                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Topic Evaluator（0+8 步）                                            │
│  [0] 5 轴 0/1/2 快速门（<7/10 拒绝）                                   │
│  [1-8] BM25 检索 → 在线检索 → 相似度 → 评分 → 创新声明 → 证伪 → 门禁      │
└─────────────────────┬────────────────────────────────────────────────┘
                     ↓
┌──────────────────────────────────────────────────────────────────────┐
│  Pipeline 状态机（2026-04 简化）                                       │
│  PROPOSAL → BASELINE_CONFIRM → FAST_FAIL → [AUTO-GATE] → MAIN        │
│            → MILESTONE → PAPER → COMPLETED                           │
│                                                                       │
│  Professor 仍是决策边界的 LLM；但所有 HITL gate（pre-MAIN /           │
│  milestone-N / pre-publish）在 2026-04 refactor 中移除，改由 typed     │
│  JudgeVerdict + contract_guard 自动决策。保留 kill/tweak 作紧急制动。   │
└──────────────────────────────────────────────────────────────────────┘
```

## 完整科研生命周期

```
原始论文（PDF / arXiv 心跳）
  → 知识编译器 → wiki 知识页面
    → 知识矿工扫描 wiki → 发现空白（7 种创新模式 + 用户偏好）
      → Topic Evaluator：5 轴快速门 → KB/在线/评分/证伪/门禁
        → 候选列表（runtime/candidates/ + briefer.py 确定性简报）
          → 用户选择 → Pipeline BASELINE_CONFIRM（metric_contract.json）
        → FAST_FAIL（精简路径，跳过重型质量层）
          → HYPOTHESIS → DESIGN → CODE → RUN → JUDGE → REPORT
          → [AUTO-GATE] 全 FAIL + INCONCLUSIVE → 自动 PAUSE
        → MAIN（完整质量链）
          → HYPOTHESIS → DESIGN（含 Self-Review）→ CODE_RECON
            → CONCEPT_PAPER + CONCEPT_CODE（2-pass concept extraction）
            → CODE（按 concept.id 逐项映射）→ CODE_PREFLIGHT（BLOCK 缺项）
            → RUN → RESULTS_REVERIFY（独立子进程重跑比对）
            → JUDGE（合并 critic + experiment_analyser + interrogator）
            → FIGURE_COMPOSE（有图表数据时）→ REPORT（纯 Python 渲染）
          → 达到最大里程碑 → PAPER
            → draft.md → Paper Auditor（G4+G10+G11+G12 + chktex）
            → Reviewer Simulator（单轮 NeurIPS schema + model_override）
            → paper.tex → paper.pdf
            → 知识沉淀：Critic supported → findings card；UNSUPPORTED → failure card
```

## 快速开始

```bash
pip install -e .
cd OpenHarness && pip install -e . && cd ..

oh  # 启动 OpenHarness 交互会话

# 在 oh 中：
#   /research launch              — 全自主：心跳 + 调度
#   /research trial               — 跑一个 pipeline 即停
#   /research trial --mode simulate   — 不调用 LLM（纯测试）
```

## 入口模式

| 模式 | 命令 | 流程 |
|------|------|------|
| 默认 | `launch` | 心跳 → 矿工 → 候选列表 → **用户选择** → Pipeline → 论文 |
| 全自主 | `launch --auto-select` | 心跳 → 矿工 → 自动创建 Pipeline → 论文 |
| 试运行 | `trial` | 心跳一次 → 矿工一次 → 跑一个 Pipeline → 停止 |
| 注入论文 | `inject paper1.pdf paper2.pdf` | PDF → wiki → 矿工 → 候选+简报 → 用户选择 |
| 注入 + 提案 | `inject paper.pdf --title "..." --question "..."` | PDF → wiki + 直接创建 Pipeline |
| 手动提案 | `create --title "..." --question "..." --refs ref1.pdf` | 直接创建 Pipeline |

## 运行时保障：StepDigest + ContractGuard

2026-04 refactor 将 pipeline step 之间的契约从 "下游 LLM 再解析上游 markdown"
改为 "Pydantic 模型强制检查"。每个步骤产出两份：

1. `<step>.<ext>` — 人类可读归档
2. `<step>.digest.json` — Pydantic 验证的 typed payload（~2KB）

Dispatcher 注入 digest JSON 到下游 prompt，而非原 markdown。Digests 见
`researchos/schemas/digest.py`：

| Digest | 失败模式 validator 捕获的例子 |
|---|---|
| `HypothesisDigest` | metric_names 未绑定到 rq_id |
| `PlanDigest` | `baseline_strategy=waived_with_reason` 无 reason |
| `ConceptsDigest` | > 30% 概念是背景名词（无算法动词 + 无公式）|
| `RunDigest` | data_hash / data_rows 与实际文件不一致 |
| `JudgeVerdict` | per_concept 空 + fully_correct=False（空契约无证据）|

跨 digest 不变量见 `researchos/core/contract_guard.py`：
- `assert_metric_alignment` — plan.metric_names ⊇ hypothesis；run.metrics.keys() ⊇ plan
- `assert_data_manifest_match` — run.data_hash 必须等于 data_manifest.yaml::directory_hash
- `assert_baseline_honesty` — run_inline 必须有 baseline_metrics；waived 不得有
- `assert_concept_atomicity` — 委托给 ConceptsDigest model_validator

`state_manager.save_pipeline()` 每次 save 后自动运行 `enforce_all`；违反 →
pipeline stage=BLOCKED + `ContractViolation` 异常向上冒泡。

## 智能体（重构后 10 核心 + 2 条件）

| # | Agent | 层 | 职责 |
|---|---|---|---|
| 1 | knowledge_miner | T1 | wiki 扫描 → 候选提案（含 research_goal）|
| 2 | hypothesis_gen | T1 | 原子可证伪假设 |
| 3 | exp_designer | T1 | 实验 plan + Self-Review（吸收 design_reviewer）|
| 4 | concept_extract_paper | T2 | 概念定义 + math + refs |
| 5 | concept_extract_code | T2 | 概念 ↔ 代码 locator 绑定（`<file>::<sym>` 或 `<slug>::<path>::L<a>-L<b>`）|
| 6 | coder | T1 | 按 concept.id 实现；吸收 fixer 的 A16-loop |
| 7 | experiment_runner | T3 | 执行；产 results.json + data_manifest.yaml |
| 8 | **judge** | T1 | **合并 critic + experiment_analyser + interrogator**；产 JudgeVerdict |
| 9 | paper_writer | T1 | 分节写作；章节根据 JudgeVerdict optional 字段条件化 |
| 10 | paper_auditor | T2 | G4/G10/G11/G12 + chktex |

条件触发：`vlm_reviewer`（有 PNG/JPG 时）、`figure_composer`（有 visualization_plan 时）。

REPORT 步骤不再有 agent — `researchos/core/publisher.py::render_milestone` 从
digests 确定性渲染 milestone_N.md（零 LLM）。

## 原子概念 ↔ 代码双向映射

LLM 最顽固的失败类是"看似实现了但漏关键组件"。流程（MAIN）：

1. **CONCEPT_PAPER**（T2）— 读 hypothesis + plan，产 `concepts_paper.digest.json`：
   每个概念 `{id, definition, math_formula, reference_papers}`。概念定义必须
   含算法动词（combine/select/learn/optimize/...）OR 非空 math_formula —
   ConceptsDigest validator 在 boundary 直接拒绝背景名词。
2. **CONCEPT_CODE**（T2）— 读 code_recon.yaml + concepts_paper.digest.json，
   用 `list_file_symbols` 遍历克隆仓库绑定每个 `code_implementation`。产
   `concepts.digest.json`。覆盖率 < 0.5（在有可用仓库时）→ validator BLOCK。
3. **CODE** — coder 读 concepts.digest.json；每个 concept.id 必须在主脚本
   末尾的 CLAIM-TO-CODE MAPPING 表中 mapped 或显式 SKIPPED。
4. **CODE_PREFLIGHT** — preflight.py 按 id 逐项校验；缺项 BLOCK。
5. **JUDGE** — 用 gen_code_tree_structure → list_file_symbols → 按需窄
   file_read 逐概念验证 mechanism ↔ math_formula；grep 验证 metric 真的被
   计算；import 烟测；输出 `judge.digest.json`（Pydantic）。

## Professor 决策模式

```yaml
# decisions/approval_<pipeline_id>_<ts>.yaml
pipeline_id: demo
action: "continue"   # 或 stop / tweak / launch / branch / write / finalize
stage_after: "MAIN"
comment: "baseline 已固化，进入 MAIN 跑主实验"
```

Professor 在以下位置被触发：
- PROPOSAL 评估后（需要 approve 才能启动）
- 每个 milestone 完成后
- 每次 JUDGE ITERATE/PIVOT 建议时
- 达到 max_iter 或预算上限时

## 质量门 G1–G12

定义在 `researchos/agent_defs/shared/quality_gates.md`，agent 按"owns + how"
声明而非内联完整文本。

| Gate | 含义 | 责任 agent |
|---|---|---|
| G1 | 假设可证伪 + 含操作化指标 | hypothesis_gen（产出）/ judge（审计）|
| G2 | 因果/控制：每组变一个变量 + 资源对等 | exp_designer |
| G3 | 原子概念 ↔ 代码双向映射完整性 | coder（产出）/ judge（结果级）|
| G4 | 论文所有数字可追至 results.json | paper_auditor |
| G5 | MAIN 统计报告完整（effect size + p + N + CI）| exp_designer + judge + paper_auditor |
| G6 | 可复现：deps + seed + 单入口 | coder |
| G7 | 资源报告 measured/estimated/simulated 明确标注 | exp_designer + publisher |
| G8 | Conclusion 不超出实验范围；Limitations 含 ≥3 失败条件 | paper_writer + reviewer_simulator |
| G9 | Data: 显式 source + 预处理前 split + provenance | exp_designer + coder + judge |
| G10 | 章节完整性（Abstract/Intro/Related/Method/Experiments/Conclusion）| paper_auditor |
| G11 | 数值跨节一致性（Abstract vs 表格 vs 正文 vs 图注）| paper_auditor |
| G12 | 引用利用率 ≥85% + 多层验证 | paper_auditor |

## 目录结构

```
researchos/
├── schemas/           # Pydantic 模型（digest, pipeline, decision, ...）
│   └── digest.py      # StepDigest 契约
├── core/              # 平台无关逻辑（contract_guard, publisher, ...）
│   ├── contract_guard.py
│   └── publisher.py   # 纯 Python milestone 渲染
├── agent_runtime/     # Agent 执行抽象
│   ├── base.py        # AgentRuntime Protocol
│   └── tools.py       # gen_code_tree_structure, grep_code, list_file_symbols
├── agent_defs/        # Agent prompt 定义（唯一来源；平台中立）
│   ├── judge.md       # 替代 critic + experiment_analyser + interrogator
│   ├── concept_extract_paper.md / concept_extract_code.md
│   ├── coder.md / exp_designer.md / ...
│   └── shared/quality_gates.md
├── runtime/           # 调度器、dispatcher、state_manager
└── mcp_servers/       # MCP kb_retrieve

adapters/
├── openharness/       # OH plugin + OpenHarnessRuntime
├── mcp/               # MCP server（暴露 launch/state/kill 为 tools）
└── skill/             # Claude Skill 适配

artifacts/pipeline_<id>/    # Pipeline 产物（state.json, digests, markdown）
knowledge/
├── raw/              # 原始 PDF
├── wiki/             # 结构化知识（concepts/methods/experiments/failures/datasets）
├── findings/         # Critic SUPPORTED 落库
└── failures/         # UNSUPPORTED 落库，30 天衰减
runtime/               # 调度器状态、预算、workflow_config.yaml
decisions/             # Professor 决策文件
paper/                 # 论文草稿 + PDF
tests/                 # 228 passing / 4 skipped
```

## 配置

### 环境变量

```bash
# OpenAI 直连（推荐）
export RESEARCHOS_API_KEY="sk-..."
export RESEARCHOS_BASE_URL="https://api.openai.com/v1"
export RESEARCHOS_MODEL="gpt-5.4"

# Anthropic
export RESEARCHOS_API_KEY="sk-ant-..."
export RESEARCHOS_BASE_URL="https://api.anthropic.com"
export RESEARCHOS_MODEL="claude-opus-4-7"

# OpenRouter（混合后端）
export RESEARCHOS_API_KEY="sk-or-..."
export RESEARCHOS_BASE_URL="https://openrouter.ai/api/v1"
export RESEARCHOS_MODEL="qwen/qwen3.6-plus:free"

# 本地 Ollama
export RESEARCHOS_BASE_URL="http://localhost:11434/v1"
export RESEARCHOS_MODEL="ollama/llama3.1:70b"

# 独立重执行验证（默认关闭）
export RESEARCHOS_REVERIFY=1

# VLM 图表审查（独立模型）
export RESEARCHOS_VLM_MODEL=gpt-4o

# 三层模型覆盖
export RESEARCHOS_MODEL_T1=gpt-5.4   # 主生成
export RESEARCHOS_MODEL_T2=gpt-4o    # 结构化
export RESEARCHOS_MODEL_T3=o4-mini   # 低推理
```

### workflow_config.yaml 关键配置

```yaml
retry:
  max_runs: 5
  max_iters: 4
  exp_timeout: 7200
  plot_timeout: 600

gates:
  fast_fail_auto_block: true   # 全 FAIL + INCONCLUSIVE 自动 PAUSE

review:
  model_override: claude-opus-4-7   # 审稿者用不同模型族

model_tiers:
  T1: ${RESEARCHOS_MODEL_T1:-gpt-5.4}
  T2: ${RESEARCHOS_MODEL_T2:-gpt-4o}
  T3: ${RESEARCHOS_MODEL_T3:-o4-mini}
```

### 多后端支持

`llm_client.py` 根据 model 前缀自动路由：`claude-*` → Anthropic SDK；
`gpt-*` / `o1` / `o3` / `o4` / `o5` → OpenAI SDK；`qwen/` / `deepseek/` 
等 → OpenRouter；`ollama/*` → 本地 Ollama。

## 依赖

- Python >= 3.10
- pydantic >= 2.0、pyyaml、openai、pymupdf
- `backoff`（可选；`llm_client.retry_llm` 优先使用，未装时本地回退）
- pdflatex + chktex（可选；paper_writer 写作硬化需 chktex）
- `pip install researchos[vector|wandb|mlflow]`（可选扩展）

## Docker 部署

```bash
docker compose up -d
export RESEARCHOS_API_KEY=your_key
docker compose up
```

`docker-compose.yaml` 挂载 knowledge/、artifacts/、runtime/、decisions/、
paper/、data/、knowledge/findings/ 为持久卷。

## 运行

```bash
python scripts/run_research.py launch              # 全自主
python scripts/run_research.py trial               # 单课题试运行
python scripts/run_research.py trial --mode simulate   # 不调用 LLM
```

## 测试

```bash
python -m pytest tests/ -q
# 228 passed, 4 skipped（2026-04 refactor 基线）
```

## 核心设计原则

1. **基于文件的状态机** — 所有状态在 JSON/YAML；调度器无状态、幂等、可恢复
2. **知识驱动** — `kb_retrieve`（BM25 + n-gram + 可选 FAISS）访问知识库
3. **Typed digest 即契约**（StepDigest + contract_guard） — 跨步不变量
   Pydantic 强制；违反 → stage=BLOCKED，而非让下游 LLM 发现
4. **原子概念即代码契约** — 每个参考论文原子概念必须显式映射到 `<file>::<sym>`
   或显式 SKIPPED；boundary validation 拒绝背景名词
5. **独立验证优先于信任** — RESULTS_REVERIFY 子进程重跑（>10% 漂移 BLOCK）
6. **规则先行、LLM 后置** — 5 轴门 → topic_evaluator；design_checks → exp_designer
   Self-Review；preflight → judge；paper_auditor → reviewer_simulator
7. **每次 LLM 调用过统一出口** — extract_json + strip_reasoning + 指数退避 +
   多后端路由
8. **预防优于恢复** — 主动 token 预算裁剪（预留 15% 给输出）；tool output
   12K 头尾比截断；overflow 自愈作兜底
9. **重试/超时/预算常量单一数据源** — `workflow_config.yaml::retry`
10. **findings 跨 pipeline 累积** — SUPPORTED 落 findings；UNSUPPORTED 落
    failures（30 天衰减）；dispatcher 注入活跃 failure card 作为下一 pipeline
    的规避指导
11. **Baseline 实验前固化** — `BASELINE_CONFIRM` 是 FAST_FAIL 第一步；跑完对表
12. **引用来自 evidence_refs** — paper_writer 只能引用 `knowledge/raw/` 或
    evidence_refs 中的论文
13. **步骤级幂等重放** — `.step_cache/{stage}__{step}.json`；崩溃后只重跑未
    完成步骤
14. **分节写作防前后矛盾** — paper_composer 按依赖顺序逐节 4 步流水线；后节
    引用前节输出
15. **章节条件化不填模板** — `JudgeVerdict.ablation_notes` 等 Optional 字段
    为 null 时 paper_writer 写 SKIP sentinel 而非生成通用填充
16. **审稿模型多样性消除自审盲区** — `review.model_override` 让 adversarial
    审稿者用不同模型族
17. **Judge → Coder 只修 fail 的概念** — advisor_verdict.yaml 的 per-concept
    裁决驱动 coder 精准修复；pass 保留；iteration_count.txt 防止无限循环
18. **HITL 不是默认** — 2026-04 refactor 移除 3 个 HITL gate；保留
    `kill` / `tweak` 决策作紧急制动

## 许可证

基础框架许可证参见 OpenHarness。
