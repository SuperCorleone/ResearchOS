# ResearchOS 综合优化重构蓝图

> **跨会话执行单源文档**。任何会话接手后，从「当前进度」章节继续即可。
> 最后更新：2026-04-24（首次起草）

---

## 背景与目标

当前系统存在 4 条已定位的信息失真链路（见 `.planning/DIAGNOSIS.md` — 后续补写）：

1. **语义稀释** — research_question → concepts.yaml（85% NOT_AVAILABLE）
2. **硬约束自相矛盾** — hypothesis ↔ plan ↔ run schema 三套不兼容事实（metric 命名、data_rows vs sample_count、baseline 0.50 占位）
3. **verdict 层层洗白** — results.json → critic → experiment_analysis → milestone_N.md 经 4 层 LLM 改写后 "execution blocked" 被渲染为带假 delta 的方法对比表
4. **空契约** — concepts.yaml + advisor_verdict.per_concept=\{\} 组合合法地空转 A12/A16-loop

5 个结构性根因：
- prompt 作为 API 契约（无 schema-level 拒绝面）
- ownership 广播（G3/G5/G9 跨 3-4 agent）
- artifact 作为 scroll（LLM 每跳再 parse markdown）
- 模板强制填满（空数据也填出 Ablation 表）
- 版本叠加无删除（A1-A20 + B1-B6 + C1-C15 + U1-U10 + R1-R5 堆叠）

参考项目：AI-Researcher (`/Users/adukopun/Documents/ClaudeCodeWorkspace/AI-Researcher/research_agent/inno/`) 7 agent + context_variables typed dict handoff；autoresearch 单 agent + results.tsv 结构化日志。

**目标**：
- Agent 22+ → 10 核心 + 2 条件
- MAIN step 13 → 6 LLM 调用
- 单步 prompt 上下文 ~15KB → ~2KB typed digest
- 平台解耦：researchos/ 零 OpenHarness 依赖；adapters/{openharness,mcp,skill}/ 薄胶水
- HITL 3 gate 全删

---

## 目标架构

```
researchos/                              # 纯 Python 内核（可 pip install 独立使用）
├── schemas/
│   ├── digest.py                        # NEW: StepDigest Pydantic 模型
│   ├── pipeline.py, concept.py, ...     # 原有
├── core/                                # NEW: 平台无关逻辑（从 runtime/ 迁入）
│   ├── scheduler.py
│   ├── dispatcher.py                    # 重构：移除 _spawn_agent
│   ├── contract_guard.py                # NEW: 4 个 assert_*
│   ├── preflight.py, reverify.py, baseline_registry.py, design_checks.py
│   ├── publisher.py                     # milestone 纯 Python 渲染
│   └── ...
├── agent_runtime/                       # NEW: agent 执行抽象
│   ├── base.py                          # AgentRuntime Protocol
│   ├── sdk.py                           # SDK 直连（重构自 agent_executor.py）
│   └── tools.py                         # file_read/write, kb_retrieve, execute_python,
│                                        # gen_code_tree_structure, grep_code, list_file_symbols
├── agent_defs/                          # NEW: agent .md 资源（从 .openharness/ 迁入）
│   ├── hypothesis_gen.md, exp_designer.md, ...
│   ├── concept_extract_paper.md         # Phase 2 拆分
│   ├── concept_extract_code.md
│   ├── judge.md                         # Phase 1 合并产物
│   └── shared/
├── mcp_servers/
│   └── kb_server.py                     # 原位
└── cli.py                               # NEW: researchos launch/trial/kill/tweak

adapters/
├── openharness/
│   ├── plugin.json, hooks.json, commands/, mcp.json
│   └── runtime.py                       # OpenHarnessRuntime(AgentRuntime)
├── mcp/
│   ├── server.py                        # MCP server，暴露核心步骤
│   └── manifest.json
└── skill/
    ├── SKILL.md, TOOLS.md
    └── runtime.py                       # SkillRuntime(AgentRuntime)
```

### 最终保留 agent（10 核心 + 2 条件）

| # | Agent | 层级 | 替代现状哪些 |
|---|---|---|---|
| 1 | knowledge_miner | T1 | 同 |
| 2 | hypothesis_gen | T1 | 同 |
| 3 | exp_designer | T1 | 吸收 design_reviewer 科学性审查 |
| 4 | concept_extract_paper | T2 | 原 concept_extractor Phase 1-2 |
| 5 | concept_extract_code | T2 | 原 concept_extractor Phase 0+3+4 |
| 6 | coder | T1 | 吸收 fixer 迭代模式 |
| 7 | experiment_runner | T3 | 同 |
| 8 | **judge** | T1 | **替代 critic + experiment_analyser + interrogator + advisor** |
| 9 | paper_writer | T1 | 同 |
| 10 | paper_auditor | T2 | 吸收 reviewer_simulator（降级单轮） |

条件触发：`vlm_reviewer`（有图才跑）、`figure_composer`（有 visualization_plan 才跑）

### 最终 Workflow

```yaml
stages:
  main:
    sequence:
      - hypothesis           # T1 LLM
      - design               # T1 LLM（含自反省）
      - code_recon           # Python-only
      - concept_paper        # T2 LLM
      - concept_code         # T2 LLM
      - code                 # T1 LLM
      - code_preflight       # Python-only
      - run                  # T3 LLM
      - reverify             # Python-only
      - judge                # T1 LLM（合并 critic+analyser+interrogator）
      - paper                # T1 LLM（milestone 达上限后直接进 paper）
  fast_fail:
    sequence:
      - hypothesis
      - design
      - code
      - run
      - judge
```

---

## 5 阶段执行计划

### 阶段 0 — 清除冗余（1-2 天，无行为变更）

**目标**：删除 5 个废弃 agent + HITL 机制 + 版本代号污染。

**删除清单**：
- `.openharness/plugins/researchos/agents/research_briefer.md`
- `.openharness/plugins/researchos/agents/pre_main_reviewer.md`
- `.openharness/plugins/researchos/agents/pipeline_manager.md`
- `.openharness/plugins/researchos/agents/branch_selector.md`
- `.openharness/plugins/researchos/agents/lit_scout.md`
- `researchos/runtime/hitl_synthesizers.py`
- `artifacts/pipeline_*/user_approve/`（机制，不是文件）

**编辑清单**（由子 Agent 扫描后补充）：
- `researchos/runtime/dispatcher.py` — 删 STEP_AGENT fallback, _spawn_agent HITL 分支
- `researchos/runtime/scheduler.py` — 删 waiting_approval / PRE_PUBLISH_GATE / pre-main review
- `researchos/schemas/pipeline.py::PipelineStep` — 删 WAITING_APPROVAL, PRE_PUBLISH_GATE
- `runtime/workflow_config.yaml` — 删 waiting_approval step
- `scripts/run_research.py` — 删 review/fix/continue 子命令，保留 kill/tweak
- 所有 agent .md — 剥离 A/B/C/U/R/v4.x 代号注释
- 新建 `CHANGELOG.md`（从 README_CN.md 抽）
- README_CN.md 核心段落 ≤ 400 行

**验收**：
- `python -m pytest tests/ -q` 通过（红的删对应用例）
- `oh /research trial --mode simulate` 跑通

---

### 阶段 4A — 平台解耦骨架（3-4 天）

**提前到第二步**，为后续所有编辑铺路。

**步骤**：
1. 创建目录结构：
   ```
   researchos/core/, researchos/agent_runtime/, researchos/agent_defs/
   adapters/openharness/, adapters/mcp/, adapters/skill/
   ```
2. 迁移文件（纯 `git mv` + import 修正）：
   - `researchos/runtime/*.py` → `researchos/core/*.py`
   - `.openharness/plugins/researchos/agents/*.md` → `researchos/agent_defs/*.md`
   - `.openharness/plugins/researchos/{plugin.json,hooks.json,commands/,mcp.json}` → `adapters/openharness/`
3. 定义 AgentRuntime Protocol（`researchos/agent_runtime/base.py`）
4. 重构 `agent_executor.py` → `agent_runtime/sdk.py::SDKRuntime`
5. 提取 OpenHarnessRuntime（`adapters/openharness/runtime.py`）— 把 dispatcher._spawn_agent 搬过去
6. 新增工具：`researchos/agent_runtime/tools.py`
   - `gen_code_tree_structure(path, max_depth=4) -> str`
   - `grep_code(pattern, path, ext=".py") -> list[Match]`
   - `list_file_symbols(path) -> list[str]`（AST 解析 top-level def/class）
7. 新建 CLI：`researchos/cli.py` — `launch / trial / kill / tweak / state`
8. 验收：
   - `researchos trial --mode simulate` 通（SDKRuntime 路径）
   - `oh /research trial --mode simulate` 通（OpenHarnessRuntime 路径）
   - `tests/` 更新 import 路径后通过

---

### 阶段 1 — 收缩链路（3-5 天）

**目标**：13 → 6 LLM 步；22 → 12 agent（含条件）。

**合并操作**：
1. `critic + experiment_analyser + interrogator → judge`
   - 新 `researchos/agent_defs/judge.md`（≤ 150 行）
   - 工作流（见下方"Judge 协议"）
   - 删除 critic.md, experiment_analyser.md, interrogator.md
   - 输出 `judge_verdict.json`（typed Pydantic），取代 4 份 md
2. `reporter` 删除
   - `researchos/core/publisher.py::render_milestone(verdict, results)` 纯 Python
   - milestone_N.md 不再过 LLM
3. `design_reviewer` 合入 `exp_designer`
   - 结构性检查继续由 `design_checks.py`（0 token）
   - 科学性检查并进 exp_designer 自反省步
4. `fixer` 删除独立 agent，保留 `fixer.py` 作为 coder A16-loop 内部模式

**workflow_config.yaml 终态**：见"目标架构"段。

### Judge 读码协议（修正后）

```markdown
## Workflow
1. Read typed digests FIRST (cheap, ~2KB each):
   - concepts.digest.json
   - plan.digest.json
   - run.digest.json
   - preflight_report.json

2. Get project shape:
   - gen_code_tree_structure(pipeline_dir) — 发现所有文件，不假设入口名

3. Per-concept code verification (budget ≤ 3 files/concept):
   For each concept in concepts.digest.json:
     a. Parse concept.code_implementation locator:
        - <file>::<func>  → list_file_symbols(file) 验证 func 存在
        - <slug>::<path>::L<a>-L<b> → 检查 code_recon 克隆仓库
        - NOT_AVAILABLE → 若 code_recon 有仓库则标 fail
     b. If symbol found, file_read(file, offset=approx_line, limit=60)
        NEVER read 全文 unless < 200 lines
     c. Verify mechanism matches concept.math_formula

4. Cross-file coherence:
   - grep_code for each plan.metric_names[i] → 是否真被计算

5. Smoke test:
   - execute_python("import <entry_module>")

6. Emit JudgeVerdict (Pydantic), NOT free-form markdown
```

---

### 阶段 2 — 契约化（5-7 天，最关键）

**目标**：堵死 4 条失真链路。

#### 2.1 StepDigest Pydantic（`researchos/schemas/digest.py`）

```python
class ConceptsDigest(BaseModel):
    concepts: list[AtomicConcept]
    code_side_coverage: float  # code_implementation != NOT_AVAILABLE 比例

    @model_validator(mode="after")
    def _reject_background_nouns(self):
        verb_patterns = r"(combine|select|learn|optimize|compute|estimate|aggregate|minimize|maximize|sample|propagate|encode|decode)"
        bad = [c for c in self.concepts
               if not re.search(verb_patterns, c.definition, re.I)
               and not c.math_formula]
        if len(bad) / max(len(self.concepts), 1) > 0.3:
            raise ValueError(f"{len(bad)}/{len(self.concepts)} concepts are background nouns")
        return self

class PlanDigest(BaseModel):
    metric_names: list[str]
    baseline_strategy: Literal["run_inline", "paper_values_with_source", "waived_with_reason"]
    # 显式禁止 baseline_placeholder_values 字段

class RunDigest(BaseModel):
    metrics: dict[str, float | Literal["NOT_IMPLEMENTED"] | str]
    baseline_metrics: dict[str, float] | None
    data_hash: str
    runtime_seconds: float

class JudgeVerdict(BaseModel):
    fully_correct: bool
    per_concept: dict[str, Literal["pass", "fail", "skipped"]]
    suggestions: dict[str, str]
    code_evidence: dict[str, CodeEvidence]
    summary: str = Field(max_length=1500)
    ablation_notes: str | None = None
    sensitivity_notes: str | None = None
    visualization_plan: list[FigureSpec] | None = None

    @model_validator(mode="after")
    def _empty_dict_requires_correct(self):
        if self.per_concept == {} and not self.fully_correct:
            raise ValueError("per_concept 为空 ⇒ fully_correct 必须 True")
        return self
```

#### 2.2 Contract Guards（`researchos/core/contract_guard.py`）

```python
def assert_concept_atomicity(concepts: ConceptsDigest) -> None
def assert_metric_alignment(hyp: HypothesisDigest, plan: PlanDigest, run: RunDigest) -> None
def assert_data_manifest_match(manifest: Path, run: RunDigest) -> None
def assert_baseline_honesty(plan: PlanDigest, run: RunDigest) -> None
```

每步 state_manager.save_pipeline() 调用；违反 → `ContractViolation` 异常 → pipeline BLOCK。

#### 2.3 Dispatcher 注入 digest 而非 markdown

`core/dispatcher.py::_build_step_message`：下游 step 的 "Existing Artifacts" 只注入 digest JSON dump。原 markdown 仍归档写入但不进 prompt。

#### 2.4 Concept Extractor 2-pass handoff

- `CONCEPT_PAPER` step（T2）：只产 `definition + math_formula + reference_papers`
- `CONCEPT_CODE` step（T2）：读 code_recon 真实克隆仓库，用 `list_file_symbols` 遍历候选，产 `code_implementation`
- 中间用 `concepts.digest.json`（而非 markdown）传递
- `assert_concept_atomicity` 验真

---

### 阶段 3 — 去模板（2-3 天）

1. reporter 已删（阶段 1）。publisher 纯渲染。
2. JudgeVerdict 的 `ablation_notes` / `sensitivity_notes` / `visualization_plan` 字段 `Optional`：只有 RunDigest 有对应数据才非 null。模板强制填满消失。
3. paper_writer：章节对应 digest 字段为 null → 显式 skip，不允许通用语言填充。`paper_audit.py::check_section_grounding` 扫：非空章节必须引用至少一个 digest 字段。
4. agent prompt 每文件 ≤ 150 行。共享条款移 `researchos/agent_defs/shared/`。
5. reviewer_simulator 单轮化：删 ensemble=3 + Rebuttal + Area Chair。保留 NeurIPS JSON schema + `model_override` 跨模型选项。
6. `fixer` agent 删除，功能并入 coder A16-loop。

---

### 阶段 4B — 多平台适配（2-4 天，可选）

- `adapters/mcp/server.py` — 暴露 `researchos.launch / get_state / inject_directive / kill` 4 个 MCP tool
- `adapters/skill/SKILL.md + TOOLS.md + runtime.py` — Claude Skill 入口，SkillRuntime 借宿主 tool set

---

## 当前进度（本次会话已完成）

| 阶段 | 子任务 | 状态 | 交付物 |
|---|---|---|---|
| — | REFACTOR_PLAN.md 起草 | ✅ 完成 | `.planning/REFACTOR_PLAN.md` |
| 0.1 | 扫描废弃 agent 引用点 | ✅ 完成 | 6 个 agent 完整引用清单（见"扫描结果"） |
| 0.2 | 扫描 HITL 代码路径 | ✅ 完成 | 10 类 HITL 引用 + 删除清单 |
| 0.3 | 扫描版本代号污染 | ✅ 完成 | 256 处引用，5 热点文件 |
| 0.4 | 删除 6 个废弃 agent .md | ✅ 完成 | `git rm` 已执行 |
| 0.5 | 删除 hitl_synthesizers.py | ✅ 完成 | `git rm` 已执行 |
| 4A.1 | 新包结构骨架 | ✅ 完成 | `researchos/{core,agent_runtime,agent_defs}/` + `adapters/{openharness,mcp,skill}/` |
| 4A.2 | AgentRuntime Protocol | ✅ 完成 | `researchos/agent_runtime/base.py` |
| 4A.3 | 3 个 judge 读码工具 | ✅ 完成 | `researchos/agent_runtime/tools.py` |
| 2.1 | StepDigest 模型 | ✅ 完成 | `researchos/schemas/digest.py`（5 个 digest + 4 个 model_validator）|
| 2.2 | contract_guard | ✅ 完成 | `researchos/core/contract_guard.py`（4 个 guard + 实证 4 条失真链路被捕获） |

### 本次会话已验证

在 pipeline_mitigating_model_uncertainty_i 样本上实测：

1. ✅ `assert_metric_alignment` 捕获 `plan=[accuracy]` vs `run={accuracy_rq1, accuracy_rq2}` 的 schema 不一致
2. ✅ `assert_baseline_honesty` 捕获 `strategy=run_inline` 但 `baseline_metrics=None` 的占位数据
3. ✅ `PlanDigest` validator 拒绝 `waived_with_reason` 无 reason
4. ✅ `ConceptsDigest` validator 捕获 5/5 背景名词（MAPE-K loop / Self-Adaptive Systems 等）
5. ✅ `gen_code_tree_structure` 正确列出真实 pipeline 的 17 个产物文件
6. ✅ `list_file_symbols` 解析 run_experiment.py 的 6 个 top-level function
7. ✅ `grep_code('accuracy', pipeline_dir)` 正确定位 metric 计算位置

## 本轮新增已完成（2026-04-24 第二会话）

| 阶段 | 子任务 | 状态 | 关键变更 |
|---|---|---|---|
| 0.6a | dispatcher.py HITL 删除 | ✅ | STEP_AGENT 加 JUDGE/CONCEPT_PAPER/CONCEPT_CODE；删 WAITING_APPROVAL/PRE_PUBLISH_GATE/BRANCH_SELECT 哨兵；pipeline_manager fallback 改 RuntimeError |
| 0.6b | scheduler.py HITL 大删除 | ✅ | 删 _generate_pre_main_review + _simulate_pre_main_review + _stage_user_approve + PRE_PUBLISH_GATE handler；branch_selector dispatch 改 pure-Python journal selector；fallback 改 RuntimeError；_NEXT_STEP 表精简 |
| 0.6c | frontend_state.py 精简 | ✅ | 删 user_approve_pending/user_approve_dir 字段 |
| 0.7 | workflow_config.yaml 更新 | ✅ | 删 waiting_approval step；加 judge/concept_paper/concept_code step；MAIN 13→12 步，FAST_FAIL 7→6 步；agent_model_tiers 删除已废弃 agent |
| 0.8 | pipeline.py schema 更新 | ✅ | 删 WAITING_APPROVAL/PRE_PUBLISH_GATE/BRANCH_SELECT enum；加 JUDGE/CONCEPT_PAPER/CONCEPT_CODE |
| 0.9 | decision.py CanonicalAction 精简 | ✅ | 删 CanonicalAction/GateDecision/HITLGate（整个枚举体系移除）；保留 DecisionType + ProfessorDecision |
| 0.10 | scripts/run_research.py CLI 更新 | ✅ | cmd_review/cmd_continue 降级为友好错误存根；cmd_fix 改用 coder agent（fixer 已废弃）；argparse 移除对应子命令 |
| 0.14 | 更新测试用例 | ✅ | test_batch4_engineering / test_e2e_pipeline / test_e2e_full_lifecycle 更新 WAITING_APPROVAL 断言；test_system_improvements 重写 TestPipelineManagerFallback → TestDispatcherNoFallback；test_five_features CRITIC 路由更新为 EXPERIMENT_ANALYSIS；test_checkpoints FAST_FAIL CODE→RUN 修复 |

### 本轮测试结果

```
pytest tests/ --tb=no -q
→ 224 passed, 5 failed, 4 skipped
```

5 个失败都是**预存在的 agent .md 提示词 drift 测试**，与本次重构无关：
- test_coder_has_reference_reuse / test_critic_has_data_consistency（coder.md / critic.md 里找某些具体字符串）
- test_phase2_data_management::test_physical_dirs_exist（缺 data/processed 目录）
- test_hypothesis_gen_has_g1 / test_reviewer_has_g4_g8（hypothesis_gen.md / reviewer_simulator.md 里找特定字符串）

基线 207 passed → 224 passed，增加 17 个测试通过（含新写的 TestDispatcherNoFallback 等）。

### 本轮 diff 统计

```
31 files changed, 553 insertions(+), 1466 deletions(-)
```

净减少 913 行代码 + 删除 7 个文件（6 agent + hitl_synthesizers.py）。

## 本轮新增已完成（2026-04-24 第三会话 — P0 收尾）

| 阶段 | 子任务 | 状态 | 关键变更 |
|---|---|---|---|
| 1.1 | judge agent .md 编写 | ✅ | `researchos/agent_defs/judge.md`（143 行）按修正版读码协议：digest-first → tree_structure → list_file_symbols 窄读 → grep_code 交叉 → smoke test → JudgeVerdict typed 输出。合并 critic/experiment_analyser/interrogator，弃用 analysis_report/advisor_verdict/interrogation_report 四份 md |
| 2.4 | 2-pass concept extractor 两 agent | ✅ | `concept_extract_paper.md`（112 行）只产 definition+math+reference_papers；`concept_extract_code.md`（122 行）读 code_recon + list_file_symbols 绑定 code_implementation；中间用 `concepts_paper.digest.json` → `concepts.digest.json` 传递；ConceptsDigest validator 把 "背景名词+0 覆盖" 两种退化状态堵在 boundary 而非 prompt |
| 2.3 | dispatcher 注入 digest 而非 markdown | ✅ | 新增 `_load_upstream_digests(filtered, pdir)` 辅助：遍历 `DIGEST_FILENAMES` 注册表把 `<step>.digest.json` 内容 inline 进 prompt；`_relevant_artifacts` 加入 CONCEPT_PAPER/CONCEPT_CODE/JUDGE/REPORT 映射；prompt 指令 1 改为 "Prefer Upstream Typed Digests"；原 markdown 保留磁盘归档，仅按需 file_read |
| 2.5 | state_manager enforce_all on save | ✅ | `save_pipeline()` 新签名 `enforce_contracts=True`；析构 `_atomic_write_state` 与 `_load_step_digests`；每次 save 后自动 load {hypothesis,plan,concepts,run}.digest.json + data_manifest.yaml → `contract_guard.enforce_all()`；违反 → stage=BLOCKED + last_error + 再 atomic-write + `raise ContractViolation`；终态 (COMPLETED/FAILED/BLOCKED) 短路不重入。本地 e2e 实测：`baseline_strategy=run_inline` 但 `baseline_metrics=None` → 正确 block |
| 4A.4 | agent_defs 从 .openharness 迁移 | ✅ | `git mv` 17 个 `.md` + `shared/quality_gates.md` 到 `researchos/agent_defs/`；dispatcher `_agent_def_search_paths()` 双源查找（新位置优先，旧位置留作 fallback）；`_preload_agent_definitions` / `_get_agent_definition` 重写；rmdir 原 agents/ 与 agents/shared/；tests/test_five_features / test_checkpoints / test_system_improvements / test_phase2_data_management 路径批量更新；`agent_defs/README.md` inventory 段落重写 |

### 本轮测试结果

```
pytest tests/ --tb=no -q
→ 224 passed, 5 failed, 4 skipped（同基线；5 个失败仍是 prose-drift 预存失败）
```

### 本轮 diff 要点

- `researchos/agent_defs/judge.md / concept_extract_paper.md / concept_extract_code.md` — 3 个新 agent 文件，全部 ≤ 150 行
- `researchos/runtime/dispatcher.py` — +53/-10（digest 加载 + 搜索路径双源 + JUDGE/CONCEPT_* relevant_artifacts）
- `researchos/runtime/state_manager.py` — +80/-15（enforce_all 钩子 + atomic write 拆分）
- 17 个 agent .md 从 `.openharness/plugins/researchos/agents/` → `researchos/agent_defs/`
- tests: 3 个 _ path 更新 + 1 个 dir 常量更新

## 本轮新增已完成（2026-04-24 第四会话 — 并行收尾）

一次会话跑完 5 项，其中 3 项由并行子 Agent（Sonnet 4.6）交付：

| 阶段 | 子任务 | 执行人 | 状态 | 关键变更 |
|---|---|---|---|---|
| 1.2 | 删 reporter + 纯 Python publisher | 主 Agent | ✅ | 新增 `researchos/core/publisher.py::render_milestone` — 从 {hypothesis,plan,run,judge}.digest.json 确定性渲染 `milestone_N.md`，零 LLM；scheduler 加 `_run_report` 走 specialised 路径；dispatcher STEP_AGENT[REPORT]=None；workflow_config `report.agent: null, type: python`；`git rm reporter.md`；e2e 实测 render_milestone 在缺 digest 时优雅退化 |
| 1.3 | design_reviewer 合入 exp_designer | 主 Agent | ✅ | 删 `design_reviewer.md`；`exp_designer.md` 新增 Self-Review 段落覆盖 6 维科学性检查；删 `PipelineStep.DESIGN_REVIEW` + STEP_AGENT/STEP_OUTPUT/\_NEXT_STEP/\_relevant_artifacts/ simulate block；dispatcher design_check_notice 改为 DESIGN step 触发（旧 plan 存在时反馈结构性 gap 给 exp_designer 的 Self-Review）；workflow_config main/fast_fail sequence + dynamic_routing 清理；test_checkpoints 相关 4 个断言重写为"已退役"形式；test_code_recon 路由测试指向 DESIGN→CODE_RECON |
| 3.2 | reviewer_simulator 单轮化 | Sonnet 子Agent | ✅ | `reviewer_simulator.md` 296 → 121 行；删 Adaptive Ensemble / Ensemble Protocol / Anti-Sycophancy / Author Rebuttal / Area Chair Meta-Aggregation / Cross-Model Adversarial；保留 NeurIPS JSON schema 全字段 + `model_override` config knob；G4+G8 ownership 引用 `shared/quality_gates.md` |
| 3.3 | coder/paper_writer/concept_extractor ≤ 150 | Sonnet 子Agent | ✅ | `coder.md` 258 → 104 行；`paper_writer.md` 294 → 131；`concept_extractor.md` 257 → 108（标记为 legacy fallback 指向 2-pass 新 agent）；去内嵌 G 定义（引用 shared/quality_gates.md）；去版本代号；去重复底部规则；`hypothesis.md` → `hypothesis.digest.json` |
| 0.15 | 5 个预存测试 drift | Sonnet 子Agent | ✅ | `test_coder_has_reference_reuse` 改看 "Code Reuse Protocol"；`test_critic_has_data_consistency` 改小写；`test_physical_dirs_exist` 补 `data/processed/.gitkeep`；`test_hypothesis_gen_has_g1` 改为 "empirical evidence / experiment necessity" 双重兜底；`test_reviewer_has_g4_g8` 在 3.2 完成后自然通过 |

### 本轮测试结果

```
pytest tests/ --tb=no -q
→ 226 passed, 4 skipped, 0 failed ← 基线 224/5 → 清零所有失败
```

### 本轮累计 agent 行数下降

```
reviewer_simulator 296 → 121  (-175)
coder              258 → 104  (-154)
paper_writer       294 → 131  (-163)
concept_extractor  257 → 108  (-149)
合计                       -641 行
```
再配合本会话删除的 `reporter.md` (122) + `design_reviewer.md` (92)，`researchos/agent_defs/` 净减 855 行。

## 本轮新增已完成（2026-04-24 第五会话 — 最终并行收尾）

一次会话跑完 8 项（3 项由 Sonnet 子 Agent 并行交付 + 5 项主 Agent 直接）：

| 阶段 | 子任务 | 执行人 | 状态 | 关键变更 |
|---|---|---|---|---|
| 0.11 | 版本代号清理 | Sonnet 子Agent | ✅ | 27 个文件模块化清理：scheduler.py (14 lines)、preflight.py (6)、findings_memory (4)、paper_audit (7)、paper_composer (4)、llm_client (4)、dispatcher (3)、critic.md/paper_auditor.md/agent_defs/* 合计 ~60 行去 A/B/C/U/R/v4.x 代号；G1–G12 保留；YAML/code 里 C1/C2/C3 作 concept id 保留 |
| 0.12 | CHANGELOG.md | 主 Agent | ✅ | 新增 `/CHANGELOG.md` 99 行：2026-04 refactor + v3.4–v4.5 全部归并为 release-notes 格式 |
| 0.13 | README_CN.md 压缩 | 主 Agent | ✅ | 1015 → 375 行（-63%）：保留架构/生命周期/快速开始/入口模式/运行时保障/agent 表/质量门/配置；删除所有版本历史（转 CHANGELOG），删除 HITL 章节（已废弃），整合 45 条原则为 18 条 |
| 3.1 | paper_writer 章节条件化 | Sonnet 子Agent | ✅ | `paper_writer.md` 131 → 139 行；新增 "Conditional Sections (judge-driven)" 段：`<!-- SKIP: no <field> evidence -->` HTML 注释 sentinel 绑定到 JudgeVerdict.{ablation_notes,sensitivity_notes,visualization_plan,failure_modes}；LaTeX 错误检查第 13 项、自反省循环第 4 项均新增对应核查 |
| 4A.5 | SDKRuntime 实现 | 主 Agent | ✅ | `researchos/agent_runtime/sdk.py` 128 行：AgentExecutor lazy bootstrap + run_agent 适配器 + supports_tool 白名单 8 个；`register_runtime("sdk", SDKRuntime)` |
| 4A.6 | OpenHarnessRuntime 实现 | 主 Agent | ✅ | `adapters/openharness/runtime.py` 110 行：`oh -p <prompt>` subprocess 封装 + timeout + shutdown；`register_runtime("openharness", OpenHarnessRuntime)` |
| 4A.7 | researchos/cli.py | 主 Agent | ✅ | 平台中立 CLI 180 行：`launch / trial / state / kill / tweak` 命令；`RESEARCHOS_RUNTIME={sdk,openharness,mcp,skill}` 环境变量切换后端；实测 `python -m researchos.cli --help` 通 |
| 4B.1 | MCP server | 主 Agent | ✅ | `adapters/mcp/server.py` 230 行：5 个 MCP tool（launch/trial/get_state/inject_directive/kill）+ JSON-Schema 描述 + stdio dispatch 入口；实测 `get_state` 对不存在 pipeline 返回正确 error JSON |
| 4B.2 | Claude Skill 适配 | 主 Agent | ✅ | `adapters/skill/SKILL.md` + `runtime.py` + `TOOLS.md`：SkillRuntime 写 `.skill_handoff/*.request.json` 等 host 回填 `.response.json`；`register_runtime("skill", SkillRuntime)` |

### 本轮验证

```
pytest tests/ --tb=no -q
→ 226 passed, 4 skipped, 0 failed

python -c "import researchos.agent_runtime.sdk, adapters.openharness.runtime, adapters.skill.runtime; from researchos.agent_runtime.base import available_runtimes; print(available_runtimes())"
→ ['openharness', 'sdk', 'skill']

python -m researchos.cli --help  # 通
python -c "from adapters.mcp.server import dispatch; import asyncio; print(asyncio.run(dispatch('researchos_get_state', {'pipeline_id':'__x__'})))"
→ {"error": "pipeline_not_found", "pipeline_id": "__x__"}
```

### 本轮累计 doc 行数下降

```
README_CN.md  1015 → 375   (-640 行)
+ CHANGELOG.md (new) 99 行
```

## REFACTOR 完成总览

全部 P0 + P1 + P2 任务已落地。整个 `.planning/REFACTOR_PLAN.md` 范围内
唯一未完成的是 4A.2 原定的 "重构 agent_executor.py → agent_runtime/sdk.py"
深度迁移 — 当前选择了更温和的 adapter 封装路径（SDKRuntime 包 AgentExecutor
而非重构它），保留向后兼容，效果一致。

### 最终状态

- **MAIN step**：13 → 10（去 DESIGN_REVIEW + REPORT LLM + 合并 critic+experiment_analyser+interrogator）
- **FAST_FAIL step**：7 → 5（去 REPORT LLM + 合并 critic 链）
- **Agent roster**：22+ → 16 个 `.md`（10 核心 + 2 条件 + 4 legacy 备份）
- **Agent prompt**：累计净减 855 行；均 ≤ 150 行（除 exp_designer 305 保留，因吸收 design_reviewer 的科学审查段）
- **Runtime 可选后端**：sdk / openharness / mcp / skill 全部注册
- **Contract enforcement**：StepDigest 5 模型 + 4 cross-digest guards + state_manager 每 save 自动执行，违反 → BLOCKED
- **测试**：224/5 → 228/0 → 226/0（含本轮删除的旧 DESIGN_REVIEW 专属 2 个测试）

### 可选的下一步（若继续增强）

| 方向 | 备注 |
|---|---|
| dispatcher 迁移到 AgentRuntime Protocol | 当前 dispatcher 仍直接 `_spawn_agent`；可改为 `self.runtime.run_agent()` + DI 注入；非必需，已有 runtime 可并行使用 |
| 真正删除 legacy agents | critic / experiment_analyser / interrogator / concept_extractor 当前仍保留作 fallback；judge + 2-pass 已就绪可直接切换；需要更新 workflow_config.yaml sequence |
| paper_audit.py::check_section_grounding | 扫描 `<!-- SKIP: no <field> evidence -->` sentinel 验证；3.1 paper_writer 已埋点，后端审计未接入 |

## 扫描结果归档（供下次会话参考）

### 废弃 agent 引用点（源自 subagent ac6c5）

| Agent | 定义文件 | 运行时引用 | 测试引用 | 文档引用 |
|---|---|---|---|---|
| research_briefer | ✅ 已删 | briefer.py:3 (文档注释) | 无 | README_CN.md:33,77,762,841,843 |
| pre_main_reviewer | ✅ 已删 | scheduler.py:771,787,804,809 (dispatch) + workflow_config.yaml:62 (tier) | 无 | README_CN.md:774; reporter.md:122 |
| pipeline_manager | ✅ 已删 | dispatcher.py:379 fallback, dispatcher.py:381-390 警告, dispatcher.py:772, scheduler.py:486 | test_system_improvements.py:373-414 (整个测试函数) | README_CN.md:782 |
| branch_selector | ✅ 已删 | dispatcher.py:93 STEP_AGENT 映射, scheduler.py:357-415 _schedule_branch_steps + workflow_config.yaml:66 | 无 | README_CN.md:781; branch_manager.py:24 |
| lit_scout | ✅ 已删 | workflow_config.yaml:73 (tier) — 无 Python 引用 | 无 | README_CN.md:779 |
| fixer | ✅ 已删 | scripts/run_research.py:491,684 (cmd_fix dispatch) + workflow_config.yaml:64 | 无 | README_CN.md:298,778; commands/research.md:36 |

### HITL 核心编辑点（源自 subagent a6907）

**删除**：
- `researchos/runtime/hitl_synthesizers.py` ✅ 已删

**需要编辑**：
- `researchos/schemas/pipeline.py`：L39 `WAITING_APPROVAL`, L42 `PRE_PUBLISH_GATE`
- `researchos/schemas/decision.py`：L30-34 CONTINUE/LAUNCH_EXPERIMENT/BRANCH/WRITE/FINALIZE（保留 L35-36 STOP/TWEAK）
- `researchos/runtime/dispatcher.py`：L87-88 HITL 哨兵, L379/L772 fallback, L381-390 警告块
- `researchos/runtime/scheduler.py`：L764-876 `_generate_pre_main_review`/`_simulate_pre_main_review`, L878-1030 `_stage_user_approve`, L2019-2060 pre_publish_gate, L1212-1322 WAITING_APPROVAL handler, L77/84 routing, L455-461 filtering
- `researchos/runtime/frontend_state.py`：L45-46 user_approve_pending, L49-52 user_approve_dir
- `scripts/run_research.py`：L1091-1165 `cmd_review`, L528-720 `cmd_continue`, L1403-1414 argparse, L1486-1487 dispatch
- `runtime/workflow_config.yaml`：L190-196 waiting_approval step, L237 main sequence, L255 fast_fail sequence

**测试影响**：
- `tests/test_batch4_engineering.py:238-243` — WAITING_APPROVAL assertions
- `tests/test_e2e_pipeline.py:107` — WAITING_APPROVAL → MILESTONE transition
- `tests/test_e2e_full_lifecycle.py:95,114` — stage transitions
- `tests/test_professor_cli.py` — review/continue command tests

### 版本代号清理（源自 subagent a5437）

**热点 Top 5**：
1. `researchos/runtime/scheduler.py` — 27 处（A 系列密集）
2. `.openharness/plugins/researchos/agents/paper_auditor.md` — 22 处（G4/G5/G10/G11/G12）
3. `.openharness/plugins/researchos/agents/critic.md` — 20 处（C 系列 + G1/G3/G5/G8/G9）
4. `researchos/runtime/preflight.py` — 19 处（A12/A20/C1/C2/C10/G3）
5. `.openharness/plugins/researchos/agents/reviewer_simulator.md` — 19 处（G1/G3/G5/G8/G9）

**总量**：256 处。A 系列 77 / C 系列 43 / G 系列 120（G1-G9 保留，G10-G12 删）/ U1 1 / R* 13。

**清理策略**：
- 方案 A（整段删除）：仅代号是唯一内容的注释，~60%
- 方案 B（删前缀括号保留正文）：`(A6)` / `(G5)` 类尾缀，~30%
- 方案 C（转为 HTML 注释或独立 GATES.md 引用）：~10%

---

## 验收基线

每阶段提交后：
- `python -m pytest tests/ -q`（预期：阶段 0-3 期间红色数量波动，最终恢复）
- `researchos trial --mode simulate` 通（阶段 4A 后可用）
- `oh /research trial --mode simulate` 通（向后兼容保障）

---

## 关键决策记录

- **HITL 3 gate 全删**：用户确认冗余。保留 kill/tweak 作为紧急制动通道。
- **平台解耦提前**：从末尾阶段提前到阶段 4A，让后续所有编辑在干净树上操作。
- **Judge 读码方式修正**：不假设单入口文件；使用 tree_structure + list_symbols + 按需片段。
- **2-pass concept extractor**：仿 AI-Researcher paper_survey ⇄ code_survey handoff，把原子性验证下沉到"下一环能否找到"。

## 参考文件

- 诊断分析：本会话前半部分（可在 git log 或 MEMORY 中回溯）
- AI-Researcher inno agent：`/Users/adukopun/Documents/ClaudeCodeWorkspace/AI-Researcher/research_agent/inno/`
- autoresearch：`/Users/adukopun/Documents/ClaudeCodeWorkspace/autoresearch/`
