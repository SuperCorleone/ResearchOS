# 科研生命周期实现审查

**日期**：2026-04-26
**范围**：对照用户在 inject 重构对话中给出的目标生命周期，审查当前 `researchos.runtime.scheduler` 的实际路由与 step 实现，逐项标注 `已实现 / 部分 / 未实现 / 反向`，附 `file:line` 证据，并给出修复粒度。
**注**：本审查只产报告，**不**改 scheduler / dispatcher / agent 代码。修改建议仅作为下一轮 refactor 的输入。

---

## 0. 用户目标生命周期（原文复述）

```
PROPOSAL
  → FAST_FAIL（快速得到轻量实验结果，并分析更多信息）
      → HYPOTHESIS（复用）
      → DESIGN（含 Self-Review）
      → CODE_RECON
      → BASELINE_ANALYZE（LLM code-first 分析复现包：grep IO 调用辨识真数据）
      → DATA_RECON（薄执行器：照 baseline_analysis.yaml 复制+下载）
      → CONCEPT_PAPER + CONCEPT_CODE（2-pass concept extraction）
      → CODE（按 concept.id 逐项映射）
      → CODE_PREFLIGHT（BLOCK 缺项）
      → RUN（只跑轻量实验，比如模型训练只跑 1%-10% epoch / 数据 1%-10%）
      → JUDGE
      → REPORT
      → [AUTO-GATE] 全 FAIL + INCONCLUSIVE → 自动 PAUSE
  → MAIN（完整实验运行）
      → 复用 fast_fail 已有路径产物（不重新重复跑，直接跳到 RUN 完全体处开始）
      → RUN（完全体）
      → RESULTS_REVERIFY（独立子进程重跑比对）
      → JUDGE（合并 critic + experiment_analyser + interrogator）
      → FIGURE_COMPOSE（有图表数据时）
      → REPORT（纯 Python 渲染）
```

---

## 1. 总体诊断

**routing 系统存在两套真相，且彼此冲突**：
- **真相 A（声明式）**：`runtime/workflow_config.yaml::stages` —— 与用户目标基本一致：
  - `main.sequence`: hypothesis → paper_spec → code_recon → baseline_analyze → data_recon → concept_paper → concept_code → design → code → code_preflight → run → judge → figure_compose → report
  - `fast_fail.sequence`: 同上，仅去掉 `figure_compose`
- **真相 B（命令式，实际生效）**：`scheduler._apply_wave2_edges()` 在加载完 yaml 后**硬编码 patch** 覆盖路由：
  - MAIN 入口强制改成 `PAPER_SPEC`（不是 yaml 写的 `hypothesis`）
  - MAIN 把 yaml 里的 `concept_paper + concept_code` 路由全部改成 `CONCEPT_EXTRACT`（1-pass）
  - MAIN 把 yaml 里的 `RUN → JUDGE` 改成 `RUN → RESULTS_REVERIFY → CRITIC → JUDGE → REPORT`（保留旧四件套）
  - FAST_FAIL 强制 `(FAST_FAIL, None) → BASELINE_CONFIRM`、`(FAST_FAIL, BASELINE_CONFIRM) → HYPOTHESIS`、`(FAST_FAIL, RUN) → CRITIC`（精简短路，跳过 recon/concept/preflight/figure_compose/judge）

**结论**：当前实现是"yaml 写的是用户目标蓝图，但 scheduler.py 硬编码强制走旧 lean 路径"。要做的不是新功能，**而是删 `_apply_wave2_edges` 的覆盖、让 yaml 真正生效**，再补上几个未实现的 step 与 RUN 轻量预算机制。

证据：
- `scheduler.py:280-336` `_apply_wave2_edges`
- `scheduler.py:226-227`：
  ```python
  self.workflow = WorkflowConfig.load(project_root)
  self._next_step_map = self.workflow.build_next_step_map()
  ```
  yaml 先生效，随后 `_apply_wave2_edges` 在 `__init__` 里 patch 覆盖。
- `runtime/workflow_config.yaml:336-373` 真相 A 序列定义。

---

## 2. 逐节审查（FAST_FAIL）

### 2.1 入口：`PROPOSAL → FAST_FAIL` (HYPOTHESIS first)

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| FAST_FAIL 第一步 | **反向**：当前 `(FAST_FAIL, None) → BASELINE_CONFIRM`（非用户期望的 HYPOTHESIS） | `scheduler.py:323-325` `edges_fast_fail` | BASELINE_CONFIRM 是 v5.0 的过渡产物，写 `metric_contract.json`。用户的"HYPOTHESIS（复用）"意指从 PROPOSAL 阶段已写好的 hypothesis 直接进入 DESIGN，不需要 BASELINE_CONFIRM 这一层 |
| HYPOTHESIS 复用 | yaml 也只列 hypothesis 不写 BASELINE_CONFIRM；与目标一致 | `workflow_config.yaml:359-360` | yaml 路径正确，但被 wave2 patch 覆盖 |

**修复粒度**：低。删除 `edges_fast_fail` 两条 patch，将 BASELINE_CONFIRM 转为可选的 prelude（只有写了 baseline arm 的 pipeline 才跑），或彻底移到 DESIGN 内部作为 self-check。

### 2.2 DESIGN 含 Self-Review

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| DESIGN_REVIEW 已退役 | ✅ 已实现 | `scheduler.py:56-57` `# DESIGN_REVIEW retired 2026-04 — scientific review folded into exp_designer self-review` `pipeline.py:37-39` 枚举仍存但不路由 | 与目标一致 |
| 结构化 design checks | ✅ 保留在 `design_checks.py` | `scheduler.py:107` 注释 | 与目标一致 |

**结论**：DESIGN self-review **已实现，与用户设计一致**。

### 2.3 CODE_RECON / BASELINE_ANALYZE / DATA_RECON

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| FAST_FAIL 路径里有 CODE_RECON | **未实现**：FAST_FAIL 当前从 HYPOTHESIS 直接跳 DESIGN → CODE → RUN，没有 recon 链 | `scheduler.py:88-94` `_NEXT_STEP[FAST_FAIL]` 把 DESIGN → CODE，跳过整个 recon 段 | 与目标差距大；FAST_FAIL 没有 baseline 复现包分析 |
| BASELINE_ANALYZE 实现 | ✅ MAIN 路径里有；实现完整 | `scheduler.py:307-308`, `runtime/baseline_diagnose.py`, `workflow_config.yaml::steps.baseline_analyze` | step 本身实现良好，FAST_FAIL 没接进去 |
| DATA_RECON 实现 | ✅ MAIN 路径里有；薄执行器实现完整 | `runtime/data_recon.py`, `workflow_config.yaml::steps.data_recon` | step 实现良好，FAST_FAIL 没接进去 |

**修复粒度**：中。这是 FAST_FAIL 路由的根本问题——**当前 FAST_FAIL 是"短路 lean path"语义，用户要求是"轻量但完整 research path"语义**。需要把 yaml 里 fast_fail 的完整 sequence 真正生效（删 wave2 fast_fail 覆盖）。

### 2.4 CONCEPT_PAPER + CONCEPT_CODE（2-pass）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| 枚举存在 | ✅ | `pipeline.py:45-46` `CONCEPT_PAPER`、`CONCEPT_CODE` | — |
| 产物文件 | ✅ 已映射 | `scheduler.py:112-113` `STEP_ARTIFACT[CONCEPT_PAPER] = "concepts_paper.digest.json"`, `STEP_ARTIFACT[CONCEPT_CODE] = "concepts.digest.json"` | — |
| Agent 定义 | ✅ 都已配置 model tier | `workflow_config.yaml:60-61` `concept_extract_paper: T2`, `concept_extract_code: T2` | — |
| step config 注册 | ✅ | `workflow_config.yaml:268-282` `concept_paper:` / `concept_code:` step 块 | — |
| **实际路由生效** | ❌ **未生效**：yaml main sequence 写了 `concept_paper, concept_code`，但被 wave2 patch 覆盖成单步 `CONCEPT_EXTRACT` | `scheduler.py:309-310` `(MAIN, DATA_RECON) → CONCEPT_EXTRACT`、`(MAIN, CONCEPT_EXTRACT) → CODE` | 2-pass 完全没跑过 |
| FAST_FAIL 是否走 2-pass | ❌ 同上，FAST_FAIL 路由甚至没有 concept extraction | `scheduler.py:91` `(FAST_FAIL, DESIGN) → CODE` | — |

**修复粒度**：低（routing 修复）+ 中（dispatcher 需要为 CONCEPT_PAPER / CONCEPT_CODE 加 step handler，把现在 CONCEPT_EXTRACT 的 prompt 拆成两份）。

### 2.5 CODE / CODE_PREFLIGHT

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| CODE 按 concept.id 映射 | ✅ 在 coder agent prompt 里有 REQUIRED PATTERNS（baseline_wiring / arms_to_run / data_loader） | `README_CN.md:182-190`, agent_defs/coder.md | concept-id mapping 在 coder prompt 里实现，依赖 concepts.digest.json 存在 |
| CODE_PREFLIGHT 实现 | ✅ 完整实现 | `runtime/preflight.py`, `STEP_ARTIFACT[CODE_PREFLIGHT]="preflight_report.json"` | — |
| FAST_FAIL 走 CODE_PREFLIGHT | ❌ 当前 `(FAST_FAIL, CODE) → RUN`，跳过 preflight | `scheduler.py:92` 与注释 line 90 `# FAST_FAIL skips CODE_PREFLIGHT / INTERROGATION for speed.` | 与用户设计冲突 |

**修复粒度**：低。删除 wave2 fast_fail 短路即可，preflight handler 已就绪。

### 2.6 RUN 轻量实验（1%-10% epoch / 数据）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| run_mode 字段已注入 | ✅ scheduler 在进 RUN 前写 `pipeline_<id>/run_mode.txt` | `scheduler.py:1346-1372` `_inject_run_mode_for_run` | 机制框架已就绪 |
| run_mode 实际语义 | ⚠️ 当前 `get_run_mode_for_milestone(milestone_count)` 返回字符串（"main" / "fast_fail" / "milestone"），并非 epoch / data 比例 | `scheduler.py:1353-1357` | 没有"FAST_FAIL 强制 epoch ≤ 10%"的硬约束；exp_designer prompt 看到 run_mode 后**自行决定**怎么缩小 |
| experiment_runner 强制 reduce | ❌ 没有任何代码层 BLOCK 强制 1%-10%。完全靠 LLM 在 design / runner prompt 里"自觉" | grep `epoch` 全仓只命中 `preflight.py:587`（关键词列表），无强制裁剪逻辑 | 用户要求是**强制**而非建议 |

**修复粒度**：中。建议方案：
- 在 `exp_designer` 的 contract 里加 `run_budget = {"max_epoch_fraction": 0.1, "max_data_fraction": 0.1}` 硬字段
- `code_preflight` 扫描 `run_experiment.py` 时校验是否读了 `run_mode.txt` / `run_budget.json` 并据此缩放
- 或更彻底：让 experiment_runner 直接接收 `epoch_limit`、`data_limit` 参数并 monkey-patch / wrap 执行入口

### 2.7 JUDGE（FAST_FAIL 终结）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| JUDGE step 存在 | ✅ 枚举、agent、产物都已定义 | `pipeline.py:54`, `workflow_config.yaml:258-264` `judge:` step, `agent_defs/judge.md` | — |
| MAIN 路由 JUDGE | ✅ 但 JUDGE 之前还串了 RESULTS_REVERIFY 和 CRITIC | `scheduler.py:312-315` | JUDGE **没合并** critic 的工作，而是 critic 跑完后 JUDGE 又跑一次 |
| FAST_FAIL 路由 JUDGE | ❌ 当前 `(FAST_FAIL, RUN) → CRITIC → REPORT`，无 JUDGE | `scheduler.py:93-94` 与 line 335 `setdefault((FAST_FAIL, RUN), CRITIC)` | 与用户设计直接冲突 |
| JUDGE = merged(critic + experiment_analyser + interrogator) | ❌ **声称合并但实际未合并**：JUDGE agent prompt 文档自称合并，但路由上 critic / experiment_analysis / interrogation 都还在 MAIN 里独立跑（line 314 是 CRITIC → JUDGE，不是 JUDGE 内部合并）| `scheduler.py:84-87` (legacy MAIN 路径), `workflow_config.yaml:42-58` agent_model_tiers 注释 `judge: T1 # merges critic + experiment_analyser + interrogator` | yaml 的 main.sequence 已经移除 critic/exp_analysis/interrogation 只留 judge（行 339-353），但 wave2 patch 把它们续上了 |

**修复粒度**：中。需要：
- 删除 wave2 patch 的 `(MAIN, CRITIC) → JUDGE` `(MAIN, RESULTS_REVERIFY) → CRITIC`，让 yaml 的 `RUN → judge` 直接生效
- 修改 JUDGE step handler，使其在 prompt 里同时读取 `results.json` + `analysis_report.md`（如存在）+ `interrogation_report.md`（如存在）+ 自行做 critic+analysis+interrogation 三者合并
- FAST_FAIL 走相同 yaml path，不需要 wave2 fast_fail 短路

### 2.8 REPORT（FAST_FAIL）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| REPORT 实现 | ✅ 渲染由 `researchos.core.publisher` 处理（不是 LLM agent） | `workflow_config.yaml:64` `# reporter removed — REPORT step is now rendered by researchos.core.publisher` | 与用户"纯 Python 渲染"目标一致 |

### 2.9 AUTO-GATE：全 FAIL + INCONCLUSIVE → PAUSE

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| AUTO-GATE 已实现 | ✅ | `scheduler.py:765-836` `_check_fast_fail_gate()`, `workflow_config.yaml:380-386` `gates.fast_fail_auto_block: true`, `scheduler.py:1204` 调用点 | 完全符合设计 |
| 触发条件 | ✅ "INCONCLUSIVE 且 ITERATE recommendation 且全部 Key Metrics 都 FAIL" | `scheduler.py:817-822` | — |
| 触发动作 | ✅ 写 decision 文件并 PAUSE | `scheduler.py:828-834` | — |

**结论**：FAST_FAIL AUTO-GATE 是当前 lifecycle 实现里**最贴合用户设计**的一段。

---

## 3. 逐节审查（MAIN）

### 3.1 复用 FAST_FAIL 产物，跳到 RUN 完全体

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| FAST_FAIL → MAIN 入口点 | **反向**：当前 `(MAIN, None) → PAPER_SPEC`，从最早一步重新开始 | `scheduler.py:295` | 没有跳到 RUN 的快进路径 |
| step_cache 是否能避免重复执行 | ⚠️ 部分缓解：如果产物文件已存在且 schema 一致，dispatcher 会 SKIP；但 alignment 链 / hypothesis / design 仍会被重新调度评估 | `runtime/step_cache.py`, `scheduler.py` 中的 `if (pdir / artifact).exists(): SKIP` 模式 | 缓存只在 step 粒度生效，不会一次跳过 N 步直达 RUN |
| 显式 "skip-to-RUN" 机制 | ❌ 不存在 | grep `skip_to_run`、`_advance_to_main` 全无命中 | 完全未实现 |

**修复粒度**：高。建议方案：
- 进入 MAIN 时检查 `pipeline_dir` 中是否已有 `paper_spec.yaml / hypothesis.md / design.yaml / code_recon.yaml / baseline_analysis.yaml / data_manifest.yaml / concepts*.json / code_patch.diff / preflight_report.json` 全套
- 若全有 → `pipeline.step = PipelineStep.RUN`（直接进 RUN），跳过 PAPER_SPEC … CODE_PREFLIGHT 全段
- 若部分缺失 → 列出缺失项，回填那几步即可，不全量重跑

### 3.2 RUN（完全体）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| run_mode 区分 fast_fail / main | ✅ 通过 `pipeline_<id>/run_mode.txt` 注入 | `scheduler.py:1346-1372` | 框架已就绪 |
| MAIN 强制全量 epoch / 数据 | ❌ 同 §2.6，没有任何代码层 BLOCK | — | 同上 |

### 3.3 RESULTS_REVERIFY（独立子进程重跑比对）

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| 实现存在 | ✅ | `runtime/reverify.py`, `scheduler.py:131-135` import 守护 | — |
| MAIN 路由 | ✅ `RUN → RESULTS_REVERIFY` | `scheduler.py:312` | — |
| 独立子进程 | ⚠️ 需查 reverify.py 实现细节才能确认是否真用了独立子进程 | 未本次详查 | — |

### 3.4 JUDGE（合并 critic + experiment_analyser + interrogator）

见 §2.7。MAIN 当前：`RUN → RESULTS_REVERIFY → CRITIC → JUDGE → REPORT`，JUDGE 未真合并而是 critic 之后再跑一次。

### 3.5 FIGURE_COMPOSE

| 检查 | 当前状态 | 证据 | Gap |
|---|---|---|---|
| 枚举存在 | ✅ | `pipeline.py:55` `FIGURE_COMPOSE = "figure_compose"` | — |
| step config | ✅ | `workflow_config.yaml:248-256` `figure_compose:` step + `agent: figure_composer` | — |
| Agent 定义 | ✅ `agent_defs/figure_composer.md`（应存在） | `workflow_config.yaml:66` `figure_composer: T2` | — |
| **实际路由** | ❌ **完全未接入**：scheduler `_apply_wave2_edges` 没写 figure_compose 边，yaml 的 `RUN → judge → figure_compose → report` 序列被覆盖成 `RUN → RESULTS_REVERIFY → CRITIC → JUDGE → REPORT`，FIGURE_COMPOSE 永远不会被调度 | grep `FIGURE_COMPOSE` 在 `scheduler.py` 0 命中（除枚举/STEP_ARTIFACT 外） | 死代码 |
| 条件触发（"有图表数据时"） | ❌ 没有触发条件实现 | — | 即使路由通了，也需要补一个"experiment_plan 含 figure_specs[]" 才走 figure_compose 的 dynamic_routing |

**修复粒度**：中。删 wave2 patch + 在 `dynamic_routing` 里加 "if figure_specs empty → skip to report" 逻辑。

### 3.6 REPORT（纯 Python 渲染）

见 §2.8，已实现。

---

## 4. 其他非 routing 隐患

### 4.1 yaml 与 scheduler 双真相

`_apply_wave2_edges` 是 routing 逻辑里**最大的一坨技术债**。它的注释（`scheduler.py:281-287`）声称"YAML doesn't yet know about CONCEPT_EXTRACT, RESULTS_REVERIFY, BASELINE_CONFIRM, PRE_PUBLISH_GATE — patch them in"，但 yaml 已经知道了（甚至更新更准），patch 反而是**保留旧路径**的"防止 yaml 生效"机制。

**建议**：在下一次 refactor 里：
1. 把 `_apply_wave2_edges` 整体删除
2. 让 yaml 的 sequence + dynamic_routing 真正成为唯一 routing 真相
3. 把 BASELINE_CONFIRM 这种"过渡 step"明确标为 deprecated 或合并到 DESIGN
4. 删除 `pipeline.py` 中不再使用的枚举（DESIGN_REVIEW、CONCEPT_EXTRACT 留作向后兼容；FIGURE_COMPOSE 重新激活）

### 4.2 inject 重构后的隔离断言

本次 inject 改造（Phase 1）只是**写入侧**完全隔离。读取侧的 `kb_retrieve` 已通过 `agent_executor._is_inject_pipeline()` 路由到 `search_pipeline_dir`。但仍有两个 lower-priority 残留点：
- `paper_audit.py:546` 把 `knowledge/raw/` 的 stems 当 evidence。inject pipeline 不写 raw，所以正常情况下没影响；但如果 typed inject 与系统其他流程并行运行（通常不会，因 focused mode 静音 miner），可能误判 evidence。建议在 paper_audit 里也加 inject 检测短路。
- `paper_composer.py:45` 提示词说 "Use ONLY citations from evidence_refs and knowledge/raw/"。inject pipeline 的 evidence_refs 都指向 pipeline_dir，knowledge/raw/ 引用对它无意义。建议在 inject 模式下用不同 prompt 模板（去掉 knowledge/raw/ 表述）。

这两个改动都是 prompt / read-side 防御，比较容易做；本次 Phase 1 没做是因为不在 minimum-viable 范围内。

### 4.3 RUN 的轻量预算

§2.6 提的"无强制裁剪"问题是用户设计里**唯一不靠 routing 修复就能解决的语义缺口**。建议优先级：
1. 在 `experiment_plan.yaml` schema 里加 `run_budget: {epoch_fraction: 0.1, data_fraction: 0.1, max_steps: …}`（FAST_FAIL 必填）
2. 在 `code_preflight` 里扫描 `run_experiment.py` 是否读取 `run_budget` 并按比例缩放
3. 在 `experiment_runner` 里加 wrapper：拦截 `for epoch in range(N): …` 模式，根据 run_budget 改成 `range(int(N * epoch_fraction))`（这是激进方案；保守方案是只在 contract / preflight 层检查）

### 4.4 FAST_FAIL → MAIN 决策的 HITL 缺口

用户原文设计是 "AUTO-GATE → 自动 PAUSE"。当前实现确实在 critic 给 INCONCLUSIVE/ITERATE/all-FAIL 时 PAUSE（§2.9）。但**正常 PASS 通过 fast_fail 后**，scheduler 自动写 approve 决策直进 MAIN（`scheduler.py:1228-1231`），没有人类检视点。这与用户"FAST_FAIL 是科研主路径"的设计相符（既然轻量结果都好了就直接放大），但需确认这不是 bug。

---

## 5. 改动优先级与建议路径

按修复粒度 + 用户设计对齐度排序：

### P0 — routing 真相统一（最高优先级，blast radius 中等）
- 删除 `_apply_wave2_edges` 整段
- 让 `runtime/workflow_config.yaml` 真正生效
- 处理被覆盖的几个 step：
  - BASELINE_CONFIRM：移到 DESIGN 内部 self-check
  - CONCEPT_EXTRACT：deprecate，让位给 CONCEPT_PAPER + CONCEPT_CODE
  - RESULTS_REVERIFY / CRITIC：要么纳入 JUDGE，要么作为 JUDGE 的 prerequisite step
- 验证：每个 step 都能在 yaml 顺序下被 dispatcher 实际处理

### P1 — JUDGE 真合并
- 修改 `agent_defs/judge.md`，明确合并 critic + experiment_analysis + interrogation 三方观点
- 删除 MAIN 里独立的 CRITIC / EXPERIMENT_ANALYSIS / INTERROGATION step（或保留作为可选 advisor，但不进默认 sequence）
- step handler 在 JUDGE 时聚合所有相关产物到 prompt

### P2 — FIGURE_COMPOSE 激活
- yaml 已就绪
- 实现 dynamic_routing：experiment_plan 有 figure_specs → figure_compose；否则 → report
- 验证 `figure_composer` agent 的 prompt 与产物 schema

### P3 — RUN 轻量预算硬约束
- experiment_plan schema 加 `run_budget`
- code_preflight 校验 wrapper
- experiment_runner 实际 enforce

### P4 — MAIN 复用 FAST_FAIL 产物
- 进 MAIN 时扫描 pipeline_dir，确定哪些 step 已有产物
- 直接 `pipeline.step = RUN`（或最早缺失步骤），不走完整 sequence
- step_cache 在此场景下做最后兜底

### P5 — inject 读取侧防御
- `paper_audit` / `paper_composer` 添加 inject 短路（§4.2）

---

## 6. 验证 checklist（refactor 完成后）

- [ ] `_apply_wave2_edges` 已删除
- [ ] `python -c "from researchos.runtime.workflow_loader import WorkflowConfig; cfg = WorkflowConfig.load(...); print(cfg.build_next_step_map())"` 输出与用户设计 1:1 一致
- [ ] FAST_FAIL 完整跑一遍：HYPOTHESIS → DESIGN → CODE_RECON → BASELINE_ANALYZE → DATA_RECON → CONCEPT_PAPER → CONCEPT_CODE → CODE → CODE_PREFLIGHT → RUN(轻量) → JUDGE → REPORT → AUTO-GATE
- [ ] MAIN 跳过已有产物，从 RUN 完全体起跑
- [ ] JUDGE 产物 `judge_verdict.json` 同时引用 critic / analysis / interrogation 三方观点（即使没单独 step，prompt 也要让 judge 完成这三层工作）
- [ ] FIGURE_COMPOSE 能根据 figure_specs 触发，无 figure 时跳过
- [ ] FAST_FAIL `run_mode.txt` + `run_budget` 实际限制 epoch / data 到 1%-10%
- [ ] 既存 inject pipeline (stacking_sas_uncertainty) 重跑不出现 routing 倒退

---

**审查结束**。本报告共标注：3 项已实现（DESIGN self-review、AUTO-GATE、REPORT 纯 Python）、4 项部分（CONCEPT 2-pass、JUDGE、RESULTS_REVERIFY、step_cache）、5 项未实现或反向（FAST_FAIL 路由、MAIN 跳过逻辑、FIGURE_COMPOSE、RUN 轻量硬约束、yaml-vs-scheduler 双真相）。
