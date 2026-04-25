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
│                                                                       │
│  PROPOSAL ──批准──→ 共享前缀（每个 pipeline 生命周期跑一次，缓存复用）   │
│                 │   hypothesis → paper_spec → code_recon              │
│                 │   → baseline_analyze → data_recon                   │
│                 │   → concept_paper → concept_code                    │
│                 │   → design → code → code_preflight                  │
│                 ↓                                                     │
│            run(mode) → judge → [figure_compose] → report             │
│                 ↓                           → waiting_approval        │
│        M1: mode=fast_fail（小规模 falsification）   ←─── 等待审批    │
│        M2+: mode=main（完整规模）                        ↓            │
│                 ↓                          ┌─ 批准 → 下一轮 milestone │
│        critic G10 baseline 复现度检查      ├─ 转向 → awaiting_human_pivot │
│                 ↓                          ├─ 终止 → 完成            │
│        SOFT_FLAG → baseline_diagnose → fixer └─ 暂停 → 阻塞          │
│        HARD_FLAG → awaiting_human_pivot (人工)                        │
│        KILL_FLAG → 自动 kill + 失败知识沉淀                            │
│                 ↓                                                     │
│        达到 max_milestones → PAPER → completed                        │
│                                                                       │
│  Professor 仍是决策边界的 LLM；但所有 HITL gate（pre-MAIN /           │
│  milestone-N / pre-publish）在 2026-04 refactor 中移除，改由 typed     │
│  JudgeVerdict + contract_guard 自动决策。保留 kill/tweak 作紧急制动。   │
└──────────────────────────────────────────────────────────────────────┘
```

### 流水线步骤总览

| 步骤 | 类型 | 阶段 | 说明 |
|---|---|---|---|
| hypothesis | LLM | 共享 | 假设生成 |
| paper_spec | LLM | 共享 | 从 base_paper 抽 baseline 名称、URL、关键指标 |
| code_recon | Python | 共享 | 浅克隆 reference 仓库（白名单 + 大小限制）|
| baseline_analyze | LLM | 共享 | code-first 分析 baseline 包，输出 baseline_analysis.yaml |
| data_recon | Python | 共享 | 按 baseline_analysis.yaml 拷贝/下载数据 |
| concept_paper | LLM | 共享 | 论文侧概念抽取 |
| concept_code | LLM | 共享 | 代码侧概念抽取 |
| design | LLM | 共享 | 实验设计（含 scale_profile, skip_fast_fail）|
| code | LLM | 共享 | 实验代码生成（Case A/B/C 硬规则）|
| code_preflight | Python | 共享 | 静态检查（含 baseline_reuse 校验）|
| run | LLM | 每 milestone | 执行实验（mode=fast_fail\|main 由 scheduler 注入）|
| judge | LLM | 每 milestone | 合并 critic + 分析 + interrogation |
| figure_compose | LLM | 每 milestone (main) | 论文图表 |
| report | LLM | 每 milestone | milestone 报告 |
| baseline_diagnose | Python | 触发式 | critic SOFT_FLAG 时自动调度 |
| awaiting_human_pivot | 阻塞 | 触发式 | critic HARD_FLAG 时等待用户 pivot_response |

## 对齐契约工作流（2026-04-25 新增）

**问题**：用户提供基线论文 + 自己的 proposal 时，pipeline 必须严格在用户输入范围内工作 —— baseline / dataset / metric / RQ 由结构化抽取直接获得，不应经过 wiki + BM25 稀释（容易把 proposal 当背景 paper 用、把别的 paper 的 dataset 当 baseline 用）。

**方案**：MAIN 阶段头部插 4 个新 step，把"用户输入 → 实验设计"拆成"抽取 → 对齐契约 → 验证 → 下游消费"四层。

### Typed inject 模式

```bash
researchos inject \
  --base-paper paper.pdf \
  --proposal proposal.md \
  --ref-code https://github.com/owner/repo \
  --title "..." --question "..." --budget 20000
```

每个文件按 role 分类：`base_paper` / `proposal` / `ref_code` / `related_work`。
- `base_paper` 进 wiki + 转 markdown 写 `artifacts/pipeline_<id>/base_paper.md`，并在 `runtime/raw_registry.yaml` 登记 role
- `proposal` **不**入 wiki（避免被 BM25 当背景污染），写 `artifacts/pipeline_<id>/proposal.md`，**不**进 raw 也**不**进 registry
- `ref_code` 仅记 git URL，由 CODE_RECON 实际 clone + import 测试

**raw 编译白名单（2026-04-26 起）**：`scheduler.tick()` 每次跑 `compile_all_raw()` 仅
编译 `runtime/raw_registry.yaml` 中标了 `base_paper` / `related_work` 的文件。即使
proposal 因为遗留路径或手动复制误入 `knowledge/raw/`，registry 缺它就不进 wiki。
inject / heartbeat fetch 入口都会调 `register_raw_paper(file, role)` 自动登记，
普通用户不感知。
- 同时 wiki frontmatter 不再写启发式抽取的 `evaluation_protocol` / `follow_up_questions`（之前会把"to address RQ1 we executed the?"这种回指误抓为 RQ）；结构化 RQ/metric 一律由 `paper_spec_extractor` 写到 `paper_spec.yaml`

### 4 个新 pipeline step

| Step | 实现 | 产物 |
|---|---|---|
| `PAPER_SPEC` | LLM agent (`paper_spec_extractor.md`) | `paper_spec.yaml` |
| `PROPOSAL_SPEC` | LLM agent (`proposal_spec_extractor.md`) | `proposal_spec.yaml` |
| `ALIGNMENT` | LLM agent (`alignment_synthesizer.md`) + Python orchestrator | `alignment_contract.yaml` |
| `ALIGNMENT_VALIDATOR` | 纯 Python (`alignment_validator.py`) | `alignment_validation_report.json` |

### 三份强 schema

- **`paper_spec.yaml`**（`schemas/paper_spec.py`）— 从单一 paper 逐字抽取：`research_questions[]`、`baseline_method.method_steps[]`（结构化方法步骤，proposal 通过 step ID 引用）、`dataset.name`（原样字符串）、`evaluation_metrics[]`、`reference_code.url`。每个非空字段必须有 `evidence[]` 中的 verbatim quote，validator 对原文做 substring match（容忍 PDF hyphenation / ligature）。
- **`proposal_spec.yaml`**（`schemas/proposal_spec.py`）— 抽取 `change_type`（5 枚举强制：replacement / extension / cross_method / cross_domain / new_task）、`new_components.replaces|adds_to`（指向 paper `method_steps[].id`，互斥）、`borrowed_from_paper[]`、`divergences[].arbitration_hint`（强制 `binding=false`）。
- **`alignment_contract.yaml`**（`schemas/alignment_contract.py`）— 9 维契约 + `executive_summary` 自动浓缩 + `preconditions[]` 硬门禁。

### Lock 词汇表（10 值）

```
LOCKED_TO_PAPER                LOCKED_TO_PAPER_VIA_REF_CODE
LOCKED_TO_PAPER_VIA_REIMPL     LOCKED_TO_PAPER_PLUS_PROPOSAL
AUGMENTED_BY_PROPOSAL          ARM_DEPENDENT
NEGOTIATED                     DOMAIN_SUBSTITUTED
FREE                           NOT_APPLICABLE
```

`LOCKED_*` 字段强制 `value_ref` 指向 `paper_spec.*` 的 leaf 路径，**禁止 inline 复制**——下游 agent 通过 dereference 读，单一真实源。`ARM_DEPENDENT` 在 baseline 与 proposal arm 设计冲突时启用，强制额外的 ablation arm 隔离机制 vs 数据扩张的影响归因。

### change_type → 默认锁表（synthesizer prior）

| 维度 \ change_type | replacement | extension | cross_method | cross_domain | new_task |
|---|---|---|---|---|---|
| dataset | LOCKED | LOCKED | NEGOTIATED | DOMAIN_SUB | FREE |
| split / preprocessing | LOCKED | LOCKED / AUGMENTED | NEGOTIATED | NEGOTIATED | FREE |
| baseline_algorithm | LOCKED_VIA_REF | LOCKED_VIA_REF | LOCKED (each) | LOCKED_VIA_REF | NOT_APPLIC |
| candidate_model_pool | LOCKED | LOCKED | NEGOTIATED | NEGOTIATED | FREE |
| metrics | LOCKED | LOCKED+PROP | NEGOTIATED | NEGOTIATED | FREE |
| rq_coverage | LOCKED | LOCKED+ADD | NEGOTIATED | LOCKED+ADD | FREE |

LLM 可以 override，每次 override 必须填 `reason` 引用 `paper_spec` / `proposal_spec` 字段路径。

### 18 项 validator 检查

`alignment_validator.run_validator()` 跑过的硬检查（任一 ERROR → BLOCK 整条 pipeline）：

- 三份 yaml 各自 round-trip 通过 pydantic
- `paper_spec.evidence` 覆盖率 ≥ 0.90（除 7 项豁免字段外）
- 每条 quote 在 `base_paper.md` 中可 substring 匹配（≥95% PASS / 80–95% WARN / <80% ERROR）
- 每个 `RQ.question_verbatim` 在原文找得到
- `proposal_spec.new_components.replaces|adds_to` 解析到合法 `method_step.id`
- 所有 `value_ref` / `value_ref_subtree` 路径 dereferenceable
- `LOCKED_TO_PAPER` 引用不得解析到 `NOT_STATED`
- `LOCKED_TO_PAPER_VIA_REF_CODE.code_handle.symbol` 在 `code_recon.yaml` 存在
- `reference_code.available=False` 且 `change_type ∈ {replacement, extension}` → BLOCK
- 每个 `in_scope_rq` 在 metrics 维度有覆盖
- 每个 `ARM_DEPENDENT` 的 ablation arm 在 `executive_summary.arms_to_run` 存在

### Contract Gate（HYPOTHESIS / DESIGN 强制）

`hypothesis_gen.md` 和 `exp_designer.md` 头部增加 Contract Gate：
- `contract_status == BLOCKED` 或 `DRAFT` → 立即 EXIT BLOCKED，要求人工干预
- `contract_status == FINAL` → LOCKED 字段从 contract `value_ref` dereference，禁止 inline 重抄
- DESIGN 写 `experiment_plan.yaml::baseline / metric_alignment / reference_code` 时强制 1:1 复制 contract 内容

### Coder 硬规则

`coder.md` 新增 FORBIDDEN PATTERNS（preflight 扫描即 BLOCK）：
- 字面 `NotImplementedError` / `NOT_IMPLEMENTED`
- 函数体仅 `pass` / 行仅 `...`
- `results.json::metrics` 为空或非数值

REQUIRED PATTERNS（contract 存在时）：
- `baseline_wiring.py` 必须 `from <import_path> import <symbol>`，**禁止重实现** baseline
- `executive_summary.arms_to_run` 每个 arm 在 `run_experiment.py` 必须有同名函数
- `data_loader.py` 必须 `Path(...).exists()` 校验 + 真读 `dimensions[dim=dataset].code_handle.access_path`

### 兼容旧 pipeline

不带 typed `base_paper` 角色的 pipeline（legacy / miner-driven）在 4 个新 step 自动写 SKIP 标记直通 HYPOTHESIS。`scheduler._has_alignment_inputs()` 同时检查 `metadata['source']=='user_inject'` 与 `evidence_refs` 的 typed role；行为完全向后兼容。

### Focused mode：inject 独占项目作用域

inject 的设计意图是"在用户给定的 paper+proposal 范围内开一条 pipeline，走 PROPOSAL → FAST_FAIL → MAIN，不要旁支"。一旦 `artifacts/pipeline_*` 里出现任何 `metadata['source']` 以 `user_inject` 开头（typed 或 legacy）的 pipeline——**无论其 stage 是 active、BLOCKED、COMPLETED 还是 FAILED**——项目即进入 focused mode，下面两个矿工相关入口同时静默：

- `scheduler._maybe_run_knowledge_miner()`：不会触发新一轮挖矿，`knowledge/proposals/` 不会冒出 off-topic `new_idea_*.yaml` / `evaluated_*.yaml`。
- `scheduler._intake_knowledge_proposals()`：即使存在上一次运行残留的 `new_idea_*.yaml`，intake 直接 return，不会派生平行 pipeline 跳出 inject 作用域。

判定逻辑集中在 `scheduler._is_focused_mode()`（O(N) 扫 artifacts/）。退出 focused mode 必须显式移除 inject pipeline 目录（或 archive），不存在自动放行。全局静音仍可 `touch runtime/.miner_paused`。

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
            → BASELINE_ANALYZE（LLM code-first 分析复现包：grep IO 调用辨识真数据）
            → DATA_RECON（薄执行器：照 baseline_analysis.yaml 复制+下载）
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
| 注入论文 | `inject paper1.pdf paper2.pdf` | PDF → wiki → 矿工 → 候选+简报 → 用户选择（**deprecated**，会有 stderr 警告）|
| **Typed inject**（推荐） | `inject --base-paper paper.pdf --proposal proposal.md --ref-code <git-url> --title "..." --question "..."` | 启用对齐契约工作流（PAPER_SPEC → ALIGNMENT → ...）；proposal 不入 wiki |
| 注入 + 提案（旧） | `inject paper.pdf --title "..." --question "..."` | 旧 PDF → wiki + 直接创建 Pipeline 路径，将在下一 milestone 移除 |
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
- `assert_strategy_actions_consistent` — `BaselineAnalysis.analysis_strategy` 与产出必须一致：
  - `code_primary` ⇒ `analyzed_repos[*].dataset_paths` 非空 + `status ≠ no_data_found`
  - `text_fallback` ⇒ `fallback_used=true` + `notes` 非空
  - 任何非 `none` 策略 + `data_manifest.yaml` 存在 ⇒ `executed_actions` 非空
  - 触发场景：BMA pipeline 出现的 "strategy=text_fallback + executed_actions=[]" 矛盾会被立即 BLOCK

`state_manager.save_pipeline()` 每次 save 后自动运行 `enforce_all`；违反 →
pipeline stage=BLOCKED + `ContractViolation` 异常向上冒泡。

### LLM 不再选策略，只描述事实（2026-04-26 拓展）

下面这些"看似 LLM 决策、其实是事实判断"的环节都已退化成纯 Python：

| 函数/位置 | 之前 | 现在 |
|---|---|---|
| `scheduler._run_baseline_analyze` | LLM 自选 `code_primary` / `text_fallback` | 看 cloned repo 实际有没有数据形态文件 (`data_shape.decide_strategy`)；LLM 错配 → BLOCK |
| `topic_evaluator.competitive_select` | LLM 两两 head-to-head 选 N→top_k | 直接按 `EvaluationScores` 5 维加权 + 字典序 tie-break；O(N log N) 纯函数；权重在 `research_config.yaml::tournament.weights` |
| BASELINE_ANALYZE URL 分类 | LLM 给每个 URL 标 `should_download` / `kind` | scheduler 先跑 `url_classify` 给 high 置信度判定；LLM 抄写 high 条目；contradicting → BLOCK |

原则：能 `os.listdir` / 加权和 / 正则 host 解决的事，LLM 不再插手；
LLM 只在确实需要语义解读（写假设、设计实验、写代码、写论文）时被调用。

## 智能体（重构后 10 核心 + 3 对齐 + 2 条件）

| # | Agent | 层 | 职责 |
|---|---|---|---|
| 1 | knowledge_miner | T1 | wiki 扫描 → 候选提案（含 research_goal）|
| **A** | **paper_spec_extractor** | T2 | **从 base_paper.md 单文档抽取强 schema paper_spec.yaml；禁 kb_retrieve；NOT_STATED 合法**|
| **B** | **proposal_spec_extractor** | T2 | **从 proposal.md 抽取 proposal_spec.yaml；强制 5 枚举 change_type；禁读 paper**|
| **C** | **alignment_synthesizer** | T2 | **合成 alignment_contract.yaml（9 维 lock + executive_summary）；纯结构化推理无外部工具**|
| 2 | hypothesis_gen | T1 | 原子可证伪假设；contract FINAL 时禁 kb_retrieve 重做 RQ/metric |
| 3 | exp_designer | T1 | 实验 plan + Self-Review；contract LOCKED 字段强制 dereference 不重抄 |
| 4 | concept_extract_paper | T2 | 概念定义 + math + refs |
| 5 | concept_extract_code | T2 | 概念 ↔ 代码 locator 绑定（`<file>::<sym>` 或 `<slug>::<path>::L<a>-L<b>`）|
| 6 | coder | T1 | 按 concept.id 实现；FORBIDDEN PATTERNS（禁 placeholder）+ REQUIRED PATTERNS（强制 direct import baseline）|
| 7 | experiment_runner | T3 | 执行；产 results.json + data_manifest.yaml |
| 8 | **judge** | T1 | **合并 critic + experiment_analyser + interrogator**；产 JudgeVerdict |
| 9 | paper_writer | T1 | 分节写作；章节根据 JudgeVerdict optional 字段条件化 |
| 10 | paper_auditor | T2 | G4/G10/G11/G12 + chktex |

条件触发：`vlm_reviewer`（有 PNG/JPG 时）、`figure_composer`（有 visualization_plan 时）。

**纯 Python step**（无 LLM）：`alignment_validator`（18 项检查）、`code_recon`、`data_recon`、`baseline_confirm`、`results_reverify`、`report`（publisher 渲染）。

## 自动数据与代码发现（CODE_RECON → BASELINE_ANALYZE → DATA_RECON）

ResearchOS 的核心承诺是 **"用户只交论文 + 提案，剩下的都是系统自己找"**。
v5.3 把这条流水线分成 **三段职责清晰的 step**：Python 做克隆和 IO，LLM 做语义判断。
**用户从不需要往 `data/raw/<id>/` 里手动放任何文件**。

> **设计原则（v5.3 重设计）**：凡是"这东西是不是数据 / 是不是 baseline / 是不是装饰"
> 这类语义判断，**全部交给 LLM**；硬编码 host whitelist / 后缀黑名单 / 目录命名启发
> 式都已删除。Python 只负责确定性 IO + manifest 生成。
>
> 关键洞见：**有代码库时必须用代码分析为主**，远比从论文文本扫描 URL 优越——
> 代码里的 `pd.read_csv()` / `torch.load()` / `wget` 调用就是 baseline 实际用了什么数据
> 的真相。论文文本可能列了 5 个数据集但实际只用 1 个；badge SVG 永远不会出现在
> `pd.read_csv` 的参数里。文本扫描只在 *无代码可分析* 时作为 fallback。

### Step 1: CODE_RECON （pure Python）

`researchos/runtime/code_recon.py` —— 把论文 / proposal / wiki frontmatter 里的
所有代码托管 URL 摸出来，浅克隆到 `scratch/<id>/code_refs/<owner>__<repo>/`。

URL 来源（优先级降序）：

1. `experiment_plan.yaml::reference_codebases`（如已显式列出）
2. `state.json::evidence_refs[*]` 字段：`url` / `repo` / `code_url` / `path_or_url`
3. wiki 页面 frontmatter 里的 `code_refs:` —— 通过 evidence_ref 的 stem 经
   `_slugify()` 标准化后匹配到 `knowledge/wiki/concepts/<slug>.md`

匹配 GitHub / GitLab / Bitbucket / HuggingFace Hub / Codeberg。Shallow clone
（`--depth=1 --single-branch`），500 MB 上限，下载后立刻 prune 掉
`.pth/.bin/.zip/.parquet/...` 等大型权重以节省 scratch 空间。

CODE_RECON v5.3 **不再扫 README 推 dataset_hints**（v5.2 的启发式被删）——这件事
现在由 BASELINE_ANALYZE 用 LLM 做，更准。

### Step 2: BASELINE_ANALYZE （LLM, code-first） — v5.3 新增

`researchos/agent_defs/baseline_analyzer.md` + 输出 schema
`researchos/schemas/baseline_analysis.py::BaselineAnalysis`。读取 CODE_RECON
克隆好的仓库，**先 grep 入口代码里的真实 IO 调用**（`pd.read_csv` /
`np.load` / `torch.load` / `urllib.request.urlretrieve` / `wget` /
`hf_hub_download` / 等），从中反推 baseline 实际用到的文件和外部 URL，
再做语义分类。

输出 `baseline_analysis.yaml`：

```yaml
analysis_strategy: code_primary       # 或 text_fallback / upstream_trusted / mixed
analyzed_repos:
  - slug: matteocamilli__bma-package
    role: "Complete replication package for the BMA paper"
    entry_points: [bma_adaptation.py, bma_generator.py]
    code_data_calls:                  # grep 出的真凭实据
      - {file: bma_adaptation.py, lineno: 87,
         call_text: "pd.read_csv('data/training_rescueRobot_25600_64.csv')",
         resolved_path: data/training_rescueRobot_25600_64.csv}
    dataset_paths:                    # 要 copy 的 (有上面的 call 作为证据)
      - {path: data/training_rescueRobot_25600_64.csv, kind: training,
         purpose: "Loaded by bma_adaptation.py at L87 to fit BMA models."}
    auxiliary_paths:                  # 显式 skip
      - {path: plots/, reason: "post-hoc visualisations"}
      - {path: logs/,  reason: "stale run logs"}
external_data_urls:
  - {url: "...zenodo.org/badge/...svg", should_download: false,
     kind: decorative_badge, reason: "DOI shield SVG, not data"}
```

**策略选择（2026-04-26 起改为确定性裁决，不交给 LLM）**：

策略由 `scheduler._run_baseline_analyze` 在 dispatch LLM **之前** 用纯 Python
判定（`researchos/utils/data_shape.py::decide_strategy`），写进 prompt 的
`MANDATORY analysis_strategy: X` 区块。LLM 只做事实描述。

| 输入 | strategy | 决策依据 |
|---|---|---|
| 至少一个 ok/cached clone repo + `scan_repo_for_data` 有 high/medium hits | `code_primary` | 仓库目录里真有数据形态文件 |
| 有 ok/cached clone repo 但 0 hits | `text_fallback` | 仓库存在但里面没数据 |
| 没有 ok/cached clone repo | `text_fallback` | 全部 clone 失败 / 被白名单拒 |

`scan_repo_for_data` 两层置信度：
- **high**：路径含 `data/` `datasets/` `fixtures/` `simulation*/` `examples/` `benchmarks/` 等约定段
- **medium**：扩展名 ∈ `{.csv,.tsv,.parquet,.h5,.npy,.npz,.pkl,.zip,.gz,.xlsx,...}`
- 排除：`.git/` `tests/` `docs/` `figures/` `__pycache__/` `node_modules/` `venv/` 等

post-LLM 校验：若 LLM 写出与强制策略不一致的 `analysis_strategy`，scheduler
直接 BLOCK pipeline（`last_error: baseline_analyze:strategy mismatch:...`），
避免 LLM 拍脑袋选 `text_fallback` 而仓库里其实有数据这类错位。

**URL 预分类（2026-04-26 起）**：scheduler 在 dispatch LLM 之前扫每个 cloned
repo 的 README/`*.md` 抓出所有 URL，过 `researchos/utils/url_classify.py` 的
host+扩展规则得到 `(kind, should_download, confidence)`：
- **high**：badge/DOI/已知 dataset host (`zenodo.org` `huggingface.co/datasets`
  `archive.ics.uci.edu` `figshare.com` 等)、明确扩展（`.svg/.png` → badge；
  `.csv/.parquet/.h5` → data；`.pt/.safetensors` → model_weights）
- **medium**：未知 host 上的数据扩展（提示 LLM 可能是 data，需确认）
- **low**：完全无信号 → LLM 自行判断

prompt 注入"URL pre-classification"块；high 置信度条目要求 LLM 逐字
抄进 `external_data_urls[*]`。post-LLM 校验：LLM 与 high 置信度结果在
`kind` 或 `should_download` 上不一致 → BLOCK（典型反例："想下载一个
shields.io 的 SVG"）。

### Step 3: DATA_RECON （pure Python, thin executor）

`researchos/runtime/data_recon.py` v5.3 重写为 **薄执行器**——零启发式、零白名单、
零目录猜测。完全照 `baseline_analysis.yaml` 执行：

- 对每个 `analyzed_repos[*].dataset_paths[*]`：从
  `scratch/<id>/code_refs/<slug>/<path>` **hardlink → symlink → copy 三级降级**
  到 `data/raw/<id>/<slug>/<path>`（同 fs 走 hardlink，零额外字节，inode 共享让 hash 直接对应原文件；不同 fs 退 symlink；都失败再 copy）。manifest 行记录 `materialise_mode`
- 对每个 `external_data_urls[*]` 中 `should_download: true` 的：
  `urlretrieve` 到 `data/raw/<id>/`
- 生成 `data_manifest.yaml`（每个文件 SHA256 + `directory_hash`）

URL 下载预算：单 pipeline 500 MB 上限，单文件 90 秒超时。

**幂等**：`data_manifest.yaml` 已存在且 `data_recon_status ∈ {success, partial, cached}`
则跳过整步。删 manifest 即可强制重跑。

**baseline_analysis 缺失或 `status: failed` 时**：写
`data_recon_status: no_baseline_analysis` 或 `baseline_analyzer_failed` 的 stub
manifest，pipeline 不阻塞继续推进。下游 DESIGN 会看到这个状态并据此决定是否
退到 `synthetic` 数据（必须在 `risk_notes` 中说明）。

### Upstream Trusted Sources 接口（v5.3 预留，待启用）

未来版本会加上游学术研究 agent，可以预先从 paper / 官方补充材料 / 仓库列表中
找到高置信度的数据 / 代码源，标注好 confidence 后写到
`artifacts/pipeline_<id>/upstream_sources.yaml`：

```yaml
pipeline_id: my_pipe
sources:
  - kind: dataset_url
    url: "https://zenodo.org/record/12345/files/main.zip"
    confidence: trusted          # trusted | high | medium
    annotated_by: literature_researcher_v2
    annotation: "Official dataset cited in §4.2 of the supplementary material"
```

`baseline_analyzer` 见到此文件时：`trusted` 条目 **直接 verbatim 透传** 进
`baseline_analysis.yaml`（不重新评分），`high` 条目作为强提示加入证据池，
`medium` 条目按常规证据规则处理。Schema 见
`researchos/schemas/baseline_analysis.py::UpstreamSources`。

### DESIGN 如何消费

dispatcher 在 DESIGN 步的 prompt 里 **同时** 注入 `data_manifest.yaml` 和
`baseline_analysis.yaml` 摘要——后者带 code-evidence per dataset path，比单纯
"文件存在" 强得多。`exp_designer` 强制：

- `data.source: "file"`
- `plan_dataset.dataset_location: "data/raw/<pipeline_id>/"`
- `plan_dataset.read_data_step` 必须引用 manifest `files[]` 里真实的文件名
- `data.consistency_requirement.reference_paper` 取自 `paper_spec.yaml` 标题

manifest 不存在或 `data_recon_status` 表示无数据时才允许 `synthetic`。

### CONCEPT 守门

LLM 偶尔会把仓库 URL 直接写进 `concepts.yaml::code_implementation`（schema 只允许
`<file>::<func>` / `<slug>::<path>::L<a>-L<b>` / `NOT_AVAILABLE`），CODE_PREFLIGHT
就会 BLOCK。`scheduler._run_concept_extract` 在 agent 写盘后调用
`schemas/concept.py::sanitize_concepts_yaml` 自动把 URL 之类的非法值降级为
`NOT_AVAILABLE` 并 WARN 日志。

### v5.2 → v5.3 重设计的来龙去脉

v5.2 在 DATA_RECON 内部用四张硬编码表 (`_BADGE_FRAGMENTS`、`_DATA_HOSTS`、
`_DATA_EXTENSIONS`、`_CLONE_DATA_DIRS`) 做"是不是数据"的判断。BMA case 暴露两个
反模式：

- Zenodo badge SVG `zenodo.org/badge/452358696.svg` 命中 host whitelist 被当数据
  下载（实际是 1 KB 装饰图片）
- 假设 baseline 的数据一定在 `data/` 子目录——对 BMA 巧合成立，对 ROS / 自定义
  layout 的 repo 全错

**v5.3 一刀切**：黑白名单全删；判断改由 `baseline_analyzer` LLM 看代码做，DATA_RECON
退化成 yaml 解释器。LLM 看 `pd.read_csv('data/x.csv')` 一行就知道 `data/x.csv`
是真数据；它永远不会"看到 Zenodo badge URL 然后误判为数据"，因为 badge 不会出现在
任何 IO 调用的参数里。

REPORT 步骤不再有 agent — `researchos/core/publisher.py::render_milestone` 从
digests 确定性渲染 milestone_N.md（零 LLM）。

## 研究产物图谱（lineage.json）

每条 pipeline 产生的所有 artifact —— ref papers / wiki / code repos / data files /
concepts / decisions / milestones —— 不再孤立。一份**派生视图**
`artifacts/pipeline_<id>/lineage.json` 把它们用一张有向图串起来，scheduler
**每完成一个 step 就重建一次**，永远反映当前真实状态。

> **设计原则**：lineage 是**派生视图，永远不是真理之源**。每条 edge 的真实信息
> 都已在某个 yaml/json 里；这个模块只做 join。`lineage.json` 任何时候都可以
> 删掉重生成，丢失它不丢任何研究信息。

### 节点类型（NodeKind）

| kind | 来源 | 例子 |
|---|---|---|
| `pipeline` | state.json | 这个 pipeline 本身（根节点） |
| `paper` | evidence_refs + knowledge/wiki | Taming BMA paper（PDF + 编译后 wiki 页） |
| `proposal` | evidence_refs (role=proposal) | 用户提案文本 |
| `code_repo` | code_recon.yaml | matteocamilli/bma-package |
| `dataset_file` | baseline_analysis.yaml + data_manifest.yaml | training_rescueRobot_25600_64.csv（含 sha256） |
| `dataset_url` | baseline_analysis.yaml::external_data_urls | Zenodo badge URL（标 should_download=false） |
| `concept` | concepts.yaml | C2 = "Bayesian Model Averaging" |
| `step` | state.json::artifacts + workflow_config | step:hypothesis / step:design / ... |
| `decision` | decisions/<pid>_*.yaml | Professor approve/pause/kill |
| `milestone_report` | milestone_N.md | milestone 1, milestone 2 |
| `paper_draft` | paper/<pid>_*.tex | 最终论文草稿 |

### 边关系（RelKind）

| rel | 语义 | 例子 |
|---|---|---|
| `derived_from` | 派生关系 | pipeline `derived_from` paper |
| `references` | 引用关系 | paper `references` code_repo |
| `produces` | 产出关系 | step:data_recon `produces` dataset_file |
| `consumes` | 消费关系 | step:design `consumes` data_manifest.yaml |
| `ships` | 内含关系 | code_repo `ships` dataset_file（仓库内带的数据） |
| `selects` | 选择关系（带证据） | step:baseline_analyze `selects` dataset_file（证据：grep 出的 IO 调用文件 + 行号） |
| `defined_in` | 概念→出处 | concept:C2 `defined_in` paper |
| `implemented_in` | 概念→代码 | concept:C2 `implemented_in` `<file>::<func>` |
| `next_step` | 流程顺序 | step:hypothesis `next_step` step:design |
| `applies_to` | 决策对象 | decision:approval_X `applies_to` pipeline |
| `evaluates` | 评判关系 | step:judge `evaluates` step:run |
| `tested_with` | 实验—数据 | step:run `tested_with` dataset_file |
| `cites` | 报告→概念 | milestone `cites` concept |

### 节点 ID 约定（为未来跨 pipeline 合并预留）

跨 pipeline 通用的节点用**内容哈希**：
- `paper:<sha256(filename)[:12]>` → 同一论文在多 pipeline 中 ID 一致
- `code_repo:<owner>__<repo>` → 同 GitHub 仓库 ID 一致
- `dataset_file:<sha256(content)[:12]>` 或 fallback `dataset_file:p<sha(path)>`

Pipeline 局部的节点带 pipeline_id 前缀：
- `concept:<pid>:<concept_id>`
- `step:<pid>:<step_name>`
- `decision:<pid>:<decision_filename>`

未来 Layer 3（项目级 `runtime/research_graph.json`）就能把多条 pipeline 的 lineage
按节点 ID 自动 merge —— "BMA paper 在哪几条 pipeline 中被用过" 就是查
`paper:2c67b7b1bfa7` 的入边。

### 重建时机

Scheduler 在每个 step 成功后调一次 `lineage.write_pipeline_lineage(pdir, root)`：
- specialised step（CODE_RECON / DATA_RECON / BASELINE_ANALYZE / ALIGNMENT_VALIDATOR / ...）
  完成后
- 标准 LLM step 完成后

失败被吞掉（`logger.exception` 留痕但不阻塞 pipeline）。simulate 模式下跳过。

实测开销：每次重建 ~10-50 ms（无 LLM、纯 yaml 读），相比一次 step 几秒到几分钟可忽略。

### CLI 用法

```bash
# 摘要：节点数 / 边数 / 各类节点统计
researchos lineage <pipeline_id>

# 列出某类节点的所有实例（含 attrs）
researchos lineage <pid> --kind dataset_file
researchos lineage <pid> --kind concept

# 完整 JSON
researchos lineage <pid> --format json > lineage.json

# Mermaid 图（可直接粘到 markdown）
researchos lineage <pid> --format mermaid

# 调试：哪些 extractor 跑了，找到了什么
researchos lineage <pid> --diagnostics
```

### 实测样例（BMA pipeline）

```
$ researchos lineage stacking_based_uncertainty_han
Pipeline: stacking_based_uncertainty_han
Nodes: 74  Edges: 44

Node counts by kind:
  dataset_file         39   (含 4 个 baseline 实际用的 + 35 个 manifest 列出的)
  step                 14
  decision              7
  concept               6
  paper                 2
  dataset_url           2
  milestone_report      2
  pipeline              1
  code_repo             1

Edge counts by relation:
  next_step            13   (workflow ordering)
  produces              9   (step → artifact)
  applies_to            7   (decision → pipeline)
  defined_in            6   (concept → paper)
  ships                 4   (repo → dataset)
  derived_from          2   (pipeline → paper/proposal)
  references            2   (paper → code_repo)
  evaluates             1   (judge → run)
```

可以直接回答："C2 这个 BMA 概念定义来自哪？"——查 `concept:<pid>:C2` 的
`defined_in` 出边，回到 `paper:<bma>`；"BMA paper 提供了哪些数据？"——
`paper:<bma> -references-> code_repo:matteocamilli__bma-package -ships-> dataset_file:*`。

### 与 Layer 1 / Layer 3 的关系

- **Layer 1（artifact 自带 provenance 头）**：未来增量做。每个新写的 yaml/json
  在头部加 `provenance: {pipeline_id, step, producer, inputs[], created_at,
  content_hash}` 块，方便 lineage 直接读而不必反向解析。**当前不需要**——
  Layer 2 反向解析已能覆盖 90% 边。
- **Layer 3（项目级 `runtime/research_graph.json`）**：把所有 pipeline 的
  lineage 按节点 ID merge 成一张全局图，回答跨 pipeline 问题（"哪些概念在多
  条 pipeline 复用"、"BMA paper 总共支持过多少 hypothesis"）。设计已就位
  （ID 系统、edge 语义跨 pipeline 一致），**实现按需**。

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

## fast_fail vs main 的 run mode 设计

design + code 阶段只跑一次，run 阶段在不同 milestone 用不同参数执行：

```yaml
# experiment_plan.yaml 示例
scale_profile:
  fast_fail:
    data_slice: "first_50_rows"
    seeds: [42]
    baselines: ["primary"]
    ablations: false
  main:
    data_slice: "all"
    seeds: [42, 7, 13, 21, 99]
    baselines: "all"
    ablations: true
skip_fast_fail: false  # 当数据规模不足以体现 scale gap 时设 true
```

scheduler 写 sidecar `run_mode.txt`，experiment_runner 读取并按对应 profile 执行。

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

## Baseline 复现保真度（G10 Baseline）

每个 milestone 的 critic 自动比对实验跑出的 baseline 数值与 paper 报告的数值：

| 相对误差 | Verdict | 后续动作 |
|---|---|---|
| ≤ 5% | REPRODUCED | 无 |
| (5%, 15%] | SOFT_FLAG | 自动 baseline_diagnose；可修则 fixer 介入；不可修则升级 |
| (15%, 50%] | HARD_FLAG | professor 写 `pivot_request_*.md`，pipeline 进入 awaiting_human_pivot |
| > 50% 或 NaN | KILL_FLAG | 自动 kill + 写 `wiki/failures/baseline_reproduction_*.md` |

阈值在 `runtime/research_config.yaml::critic.baseline_divergence_thresholds` 配置；
跨语言移植（cross_language_porting）使用更宽松的阈值组（10/25/60%）。

### 人工 pivot 协议

HARD_FLAG 时 professor 不自动决策，而是写：

```markdown
decisions/pivot_request_<id>_<ts>.md
  - baseline 置信度
  - G10 偏差列表
  - baseline_diagnose 结果
  - 三个建议方向（修参数/换 benchmark/终止）
  - professor 推荐
```

用户回复：

```yaml
# decisions/pivot_response_<id>_<ts>.yaml
decision: pivot|kill|continue_with_known_divergence
direction: a|b|c
comment: "..."
```

scheduler 见 response 解除阻塞，按 decision 推进。

## 目录结构

```
researchos/
├── schemas/           # Pydantic 模型（digest, pipeline, decision, ...）
│   ├── digest.py            # StepDigest 契约
│   ├── paper_spec.py        # 2026-04 PaperSpec 强 schema
│   ├── proposal_spec.py     # 2026-04 ProposalSpec 强 schema
│   └── alignment_contract.py # 2026-04 AlignmentContract 强 schema
├── core/              # 平台无关逻辑（contract_guard, publisher, ...）
│   ├── contract_guard.py
│   └── publisher.py   # 纯 Python milestone 渲染
├── agent_runtime/     # Agent 执行抽象
│   ├── base.py        # AgentRuntime Protocol
│   └── tools.py       # gen_code_tree_structure, grep_code, list_file_symbols
├── agent_defs/        # Agent prompt 定义（唯一来源；平台中立）
│   ├── paper_spec_extractor.md      # 2026-04 单文档强抽取
│   ├── proposal_spec_extractor.md   # 2026-04 单文档强抽取
│   ├── alignment_synthesizer.md     # 2026-04 9 维契约合成
│   ├── judge.md       # 替代 critic + experiment_analyser + interrogator
│   ├── concept_extract_paper.md / concept_extract_code.md
│   ├── coder.md / exp_designer.md / hypothesis_gen.md / ...
│   └── shared/quality_gates.md
├── runtime/           # 调度器、dispatcher、state_manager
│   ├── alignment_validator.py    # 2026-04 18 项纯 Python 检查
│   ├── alignment_synthesizer.py  # 2026-04 LLM 编排 + 哈希守卫
│   └── ...
└── mcp_servers/       # MCP kb_retrieve

adapters/
├── openharness/       # OH plugin + OpenHarnessRuntime
├── mcp/               # MCP server（暴露 launch/state/kill 为 tools）
└── skill/             # Claude Skill 适配

artifacts/pipeline_<id>/    # Pipeline 产物（state.json, digests, markdown）
│  ├── base_paper.md           # typed inject 模式：base paper 转 markdown
│  ├── proposal.md             # typed inject 模式：用户 proposal（不入 wiki）
│  ├── paper_spec.yaml         # 2026-04 PAPER_SPEC 产物
│  ├── proposal_spec.yaml      # 2026-04 PROPOSAL_SPEC 产物
│  ├── alignment_contract.yaml # 2026-04 ALIGNMENT 产物
│  └── alignment_validation_report.json  # 2026-04 ALIGNMENT_VALIDATOR 报告
knowledge/
├── raw/              # 原始 PDF（注意 typed inject 时 proposal 不会复制到这里）
├── wiki/             # 结构化知识（concepts/methods/experiments/failures/datasets）
├── findings/         # Critic SUPPORTED 落库
└── failures/         # UNSUPPORTED 落库，30 天衰减
runtime/               # 调度器状态、预算、workflow_config.yaml
decisions/             # Professor 决策文件
paper/                 # 论文草稿 + PDF
tests/                 # 279 passing / 4 skipped（2026-04-25 基线）
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

### 代理环境变量（必须 unset）

如果你的 shell 设置了 SOCKS 代理：

```bash
unset ALL_PROXY all_proxy GRPC_PROXY grpc_proxy FTP_PROXY ftp_proxy RSYNC_PROXY
```

httpx（OpenAI SDK 内部）不识别 `socks5h://` scheme。代码层已通过
`httpx.Client(trust_env=False)` 隔离，但保留在 README 留痕以备旧版本使用。
保留 HTTP_PROXY/HTTPS_PROXY 不影响。

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
# 279 passed, 4 skipped（2026-04-25 对齐契约工作流基线）
# 较 2026-04 增加 53 项：alignment_validator helpers (25)
# + alignment_synthesizer orchestrator (15) + typed evidence_refs (13)
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
19. **用户输入即对齐源**（2026-04-25）— typed inject 模式下 base_paper / proposal 各自单文档抽取强 schema，禁止 kb_retrieve 跨文档稀释；alignment_contract 是 pipeline 后续所有 paper-aligned 决策的单一真实源
20. **value_ref 而非 inline 复制**（2026-04-25）— LOCKED 维度只存 dotted path 引用 paper_spec leaf，下游 dereference；inline 复制 contract 字段值的代码会被 validator BLOCK
21. **Baseline 直接 import 而非重实现**（2026-04-25）— 当 ref_code 可用时，coder 必须 `from <pkg> import <symbol>`；preflight 扫描重实现并 BLOCK；目的是数值保真而非代码风格一致

## 许可证

基础框架许可证参见 OpenHarness。
