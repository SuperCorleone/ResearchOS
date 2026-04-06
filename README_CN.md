# ResearchOS

知识驱动的多智能体科研操作系统，基于 [OpenHarness](./OpenHarness) 构建。

全自主科研流水线：从论文抓取 → 知识编译 → 研究发现 → 实验设计与执行 → LaTeX 论文 → 投稿打包，全流程闭环。支持 LLM 自主决策或人工审核两种模式。

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                   OpenHarness 前端（React 终端 UI）                  │
│  /research dashboard │ launch │ fix │ publish │ approve │ priority  │
└───────────────────────────────┬─────────────────────────────────────┘
                                ↓
┌──────────────────────────┐  ┌──────────────────────────────────────┐
│  心跳模块（arXiv 抓取）  │  │  知识编译器（PDF → wiki）             │
│  每日 PDF 下载 +         │→ │  /knowledge/raw/ → /knowledge/wiki/  │
│  Semantic Scholar 过滤   │  └──────────────┬───────────────────────┘
└──────────────────────────┘                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│          知识库（BM25 + n-gram 重排序，通过 kb_retrieve 访问）        │
│  /knowledge/wiki/{concepts, methods, experiments, failures, datasets}│
└──────────┬──────────────────────────────────────┬───────────────────┘
           ↓                                      ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  知识矿工            │    │  研究调度器                               │
│  方向感知的           │    │  无状态 tick 驱动，按优先级调度            │
│  空白发现             │    │  预算控制，里程碑检查点                    │
└────────┬────────────┘    └────────────────┬─────────────────────────┘
         ↓                                  ↓
┌─────────────────────┐    ┌──────────────────────────────────────────┐
│  Professor（LLM）    │    │  Pipeline 状态机                          │
│  评估提案            │←──→│  PROPOSAL → FAST_FAIL → MAIN → PAPER    │
│  审查里程碑          │    │  Fixer agent 自动修正                     │
│  批准/终止/转向/暂停 │    └────────────────┬─────────────────────────┘
└─────────────────────┘                     ↓
                           ┌──────────────────────────────────────────┐
                           │  输出层                                   │
                           │  paper/<id>/ : draft.md → .tex → .pdf    │
                           │  publications/<id>/ : 代码 + 数据 + 报告  │
                           └──────────────────────────────────────────┘
```

## 完整科研生命周期

```
原始论文（PDF / arXiv 心跳抓取）
  → 知识编译器提取全文 → wiki 知识页面
    → 知识矿工扫描 wiki → 发现空白（含创新模式挖掘 + 用户偏好学习）
      → Topic Evaluator（8步）：KB 检索 → 在线检索 → 相似度 → 评分
        → 创新点模板 → 证伪设计 → 门禁规则
          → 通过 → 候选列表（runtime/candidates/）→ **用户选择**（默认）
              └─ [--auto-select] 自动创建 Pipeline（需显式开启）
          → 用户选择/拒绝 + 反馈 → wiki/decisions/topic_preferences_*.md（学习素材）
          → 需审查：Professor 审查边界案例 → 批准 / 驳回
          → 候补：每周重评（最多 3 次，30 天过期）
          → 拒绝：归档 + 记录到 wiki/failures/duplicate_ideas.md
        → Pipeline FAST_FAIL 快速验证（读取 falsification_design.yaml）
          → 假设 → 实验设计 → [代码] → 实验运行 → 分析（G9 数据审计）→ 报告
            → Professor 审查里程碑 → 批准 / 转向 / 终止 / 暂停 / 保留
              ├─ [人工审核关闭] Professor LLM 直接决策
              └─ [人工审核开启] Professor 写报告 → 用户决策
                   └─ [fix 命令] Fixer agent 自动修正 → 恢复
            → MAIN 主循环（含质量检查点 + 质询协议）→ 里程碑审查
              → 假设 → 设计 → **设计审查** → 代码 → **代码预检** → 实验 → 分析
                → **质询**（对抗式提问 1-3 轮）→ 报告
              → 达到最大里程碑数 → PAPER 阶段
                → draft.md → 自动审计（G4+G5）→ review.md → paper.tex → paper.pdf
                  → 知识沉淀 → wiki 更新（含 datasets/）→ 完成
                    → [publish] 打包：代码 + 数据 + 过程报告 + 可复现清单 + 期刊推荐
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

也可以不通过 oh，直接运行：

```bash
python scripts/run_research.py launch                  # 全自主
python scripts/run_research.py launch --human-review   # 带人工审核
python scripts/run_research.py trial                   # 单课题试运行
python scripts/run_research.py trial --mode simulate   # 试运行，不调用 LLM
```

## 课题选择：用户驱动 + 学习

**默认行为**：矿工发现的课题不会自动进入 pipeline。经过 Topic Evaluator 评估后，合格提案进入**候选列表**，等待用户选择。

```bash
# 查看候选课题
python scripts/run_research.py candidates list -v

# 选择课题（附反馈）
python scripts/run_research.py candidates select 3 -f "和我们的项目方向一致"

# 拒绝课题（附反馈）
python scripts/run_research.py candidates reject 1 -f "这个问题已被 Paper X 解决"
```

**学习机制**：用户的选择/拒绝决策和反馈写入 `wiki/decisions/topic_preferences_*.md`。知识矿工在后续提案中通过 `kb_retrieve` 检索这些反馈，学习用户偏好：
- 用户多次选择 novelty 高的题目 → 系统倾向于提出更大胆的方向
- 用户拒绝增量式工作 → 系统减少 incremental 类型提案

**全自动模式**需显式开启：`launch --auto-select` 或在 `research_config.yaml` 中设置 `topic_selection.auto_select: true`。

## 创新模式挖掘

知识矿工除了传统的空白扫描，还使用两种高价值创新模式：

| 模式 | 描述 | 示例 |
|------|------|------|
| **旧方法 × 新问题**（cross_domain） | 成熟方法应用到未被该方法处理过的领域 | 对比学习（视觉）→ 代码漏洞检测 |
| **新方法 × 旧问题**（cross_method） | 近期新方法用于改进已有基准中表现不佳的老问题 | State Space Models → 长文档摘要 |
| **失败驱动**（failure_driven） | 分析负结果的原因，提出改进假设 | 上次 GAN 训练不稳定 → 换用 diffusion |

每个提案都标注 `innovation_type`，cross_domain 和 cross_method 类型在评估中获得额外加分。

## 入口模式

所有方式共享同一个知识库：

| 模式 | 命令 | 流程 |
|------|------|------|
| 默认 | `launch` | 心跳 → 矿工 → 候选列表 → **用户选择** → Pipeline → 论文 |
| 全自主 | `launch --auto-select` | 心跳 → 矿工 → 自动创建 Pipeline → 论文 |
| 试运行（单课题） | `trial` | 心跳一次 → 矿工一次 → 跑一个 Pipeline → 停止 |
| 注入论文 | `inject paper1.pdf paper2.pdf` | 论文 → wiki → 矿工发现课题 → 候选/Pipeline |
| 注入 + 提案 | `inject paper.pdf --title "..." --question "..."` | 论文 → wiki + 直接创建 Pipeline |
| 手动提案 | `create --title "..." --question "..."` | 直接创建 Pipeline |

## 前端集成（OpenHarness UI）

运行 `oh` 进入 OpenHarness 交互会话，所有操作通过 slash 命令完成。React 看板组件实时渲染管线状态。

| 命令 | 功能 |
|------|------|
| `/research dashboard` | 管线看板（优先级、预算、方向） |
| `/research launch [--human-review]` | 启动调度器 |
| `/research trial [--human-review] [--mode simulate]` | 试运行：一个课题，一个 Pipeline，跑完即停 |
| `/research create --title "..." --question "..."` | 创建提案 |
| `/research inject file.pdf [--title "..." --question "..."]` | 导入论文 |
| `/research candidates list [-v]` | 查看待选课题（用户驱动选题） |
| `/research candidates select N [-f "理由"]` | 选择第 N 个课题进入 pipeline |
| `/research candidates reject N [-f "理由"]` | 拒绝第 N 个课题（反馈供学习） |
| `/research query --pipeline ID "问题"` | 向 pipeline 提问（对话式交互） |
| `/research tweak --pipeline ID "指令"` | 注入用户指令到下一个实验循环 |
| `/research explore --pipeline ID` | 查看 pipeline 中间产物和指标 |
| `/research fix --pipeline ID` | 自动修正被暂停的管线 |
| `/research publish --pipeline ID` | 打包投稿 |
| `/research approve ID [--paper]` | 批准管线（可指定进入论文阶段） |
| `/research kill ID` | 终止管线 |
| `/research priority ID 0-3` | 设置优先级（0=低, 1=普通, 2=高, 3=紧急） |
| `/research pause-miner` / `resume-miner` | 暂停/恢复课题发现 |
| `/heartbeat status` / `enable` / `disable` / `fetch` | 论文抓取控制 |

## 心跳模块（每日论文抓取）

从 arXiv 下载完整论文 PDF，通过 Semantic Scholar 过滤低影响力论文。

```yaml
# runtime/heartbeat_config.yaml
enabled: true
fetch_hour: 6
max_papers_per_direction: 5
professor:
  domain: "cs.AI"
  domain_description: "Artificial Intelligence"
directions:
  - name: "agentic-workflow-optimization"
    keywords: ["multi-agent systems", "agent orchestration"]
    priority: high
```

- 一个 Professor 负责一个领域
- 矿工按方向标记提案
- 发表不足 6 个月的论文跳过引用过滤
- API 失败时优雅降级

## Topic Evaluator（8步提案门禁）

所有矿工提案在到达 Professor 之前，都经过自动评估流水线：

| 步骤 | 操作 | 输出 |
|------|------|------|
| 1. KB 检索 | BM25 + 字符 n-gram 重排序匹配 wiki、pipeline、failures | 重复项 & 相关工作 |
| 2. 在线检索 | Semantic Scholar + arXiv（24h 缓存，时间衰减 + 引用加权） | Top-10 论文 |
| 3. 四轴相似度 | LLM 判断 problem/method/setting/metric 为 similar/different | 重复度 |
| 4. 五维评分 | significance, novelty, differentiation, feasibility, falsifiability（0-3 各项） | 总分 /15 |
| 5. 创新点声明 | "已有方法因 ___ 不足；我们提出 ___；在 ___ 条件下改善 ___" | 强制模板 |
| 6. 证伪设计 | 可验证假设、基线方法、评估指标、失败条件、预期信号 | `falsification_design` |
| 7. 门禁规则 | 根据分数和重复度判定：直接拒绝 / 需审查 / 通过 / 候补 | 门禁结果 |
| 8. 输出 | `evaluated_<id>.yaml` 包含全部分析 | 评估产物 |

**门禁规则**（`runtime/research_config.yaml`）：
- **直接拒绝**：novelty=0、falsifiability=0、significance<=1 或四轴全相似
- **需 Professor 审查**：三轴相似 或 differentiation<=1
- **通过**：总分 >= 9、significance >= 2、falsifiability >= 2
- **候补**：总分 [5, 9)——每周重评（最多 3 次，30 天过期）
- **拒绝**：总分 < 5——归档 + 记录到 `wiki/failures/duplicate_ideas.md`

## 候补 & 归档机制

| 目录 | 用途 |
|------|------|
| `knowledge/backlog/` | 有潜力的提案（5-8 分），等待重评 |
| `knowledge/archive/expired_ideas/` | 过期候补项（>30 天或 3 次重评） |
| `knowledge/archive/rejected_ideas/` | 永久拒绝的提案 |
| `knowledge/wiki/failures/duplicate_ideas.md` | 所有拒绝记录的 wiki 归档，供未来参考 |

调度器在每个 tick 周期中自动清理过期候补项并重新评估候选项。通过的提案自动创建 pipeline；再次被拒的提案永久归档。

## Pipeline 状态机

```
PROPOSAL ──批准──→ FAST_FAIL ──→ 假设 → 设计 →[代码?]→ 实验 → 分析 → 报告
                 │                  ↑                  │
                 │                  │   exp_designer 决定: requires_code true/false
                 │  falsification_design.yaml           ↓
                 │  从评估结果传播到 pipeline 目录       （FAST_FAIL 无检查点，保持快速）
                  ┌──────────────── 里程碑 ←─── 等待审批
                  │
                  ├─ 批准 ──→ MAIN ──→ 假设 → 设计 → 设计审查 →[代码]→ 代码预检 → 实验 → 分析 → 质询 → 报告
                  ├─ 转向 ──→ MAIN（新方向重启）       ↑ LLM+规则     ↑ 纯Python    ↑ 对抗式1-3轮
                  ├─ 终止 ──→ 完成（+ 知识沉淀）
                  ├─ 暂停 ──→ 阻塞 → [fix 命令] → 自动修正 → 恢复
                  └─ 保留 ──→ 阻塞（等待重新评估）
                                                      ↓
达到最大里程碑数:  里程碑 → PAPER → draft.md → review.md → paper.tex → paper.pdf
                                                                  ↓
                                                            知识沉淀 → 完成
                                                                  ↓
                                                  [publish] → publications/<id>/
```

## Professor 决策模式

| 模式 | 命令 | 行为 |
|------|------|------|
| 交互式 | `run interactive` | 用户就是 Professor |
| 全自主 | `run autonomous` | Professor LLM 直接决策；失败时暂停（不自动批准） |
| 人工审核 | `run autonomous --human-review` | Professor LLM 写推荐报告 → Pipeline 暂停 → 用户决策 |

### 决策类型

| 决策 | 效果 |
|------|------|
| `approve` | 继续下一个里程碑 / 推进阶段 |
| `kill` | 终止管线 + 知识沉淀 |
| `pivot` | 以新方向重启 MAIN |
| `pause` | 阻塞管线，等待人工介入 |
| `hold` | 阻塞管线，等待重新评估（如等待相关实验结果） |

决策支持 `allocated_budget`（设置总预算）和 `budget_adjustment`（增减调整）两种预算控制方式。

### 自动修正

Pipeline 因评审报告暂停时：

```bash
python scripts/run_research.py fix --pipeline ID           # 修正 + 自动恢复
python scripts/run_research.py fix --pipeline ID --no-resume  # 仅修正
```

Fixer agent 解析报告 → 修改代码 → 重跑实验 → 写 `correction_report.md` → 恢复管线。

### 智能代码步骤

`exp_designer` 在 `experiment_plan.yaml` 中设置 `requires_code: true/false`：
- 文献调研 → `false` → 跳过 CODE
- 实证实验 → `true` → 搭建 demo

## 数据管理

两阶段数据生命周期，集成到管线各环节：

**Phase 1 — 数据感知**（agent prompt 层）：
- `exp_designer` 在 `experiment_plan.yaml` 中输出 `data` 段：来源、描述、预处理步骤、划分策略、风险标注
- `coder` 强制"先划分后预处理"，打印数据溯源信息
- `experiment_runner` 在 results.json config 中记录 `data_source`、`data_rows`、`split_seed`、`data_hash`
- `critic` 执行 G9 数据有效性审计（来源匹配、泄漏检查、溯源验证）

**Phase 2 — 数据生命周期**（代码基础设施）：

| 组件 | 用途 |
|------|------|
| `fetch_dataset` 工具 | 第 5 个 agent 工具：从 URL 下载数据集到 `/data/raw/<pipeline_id>/`，自动生成 `data_manifest.yaml` |
| `data_manifest.py` | 代码生成（非 LLM）：按文件计算 SHA256 哈希 + 目录级哈希 + 行数统计 + 脚本版本追踪 |
| `/data/raw/` | 每个管线的原始下载数据 |
| `/data/processed/` | 每个管线的清洗/划分后数据 |
| `knowledge_compiler` | 将 `data_manifest.yaml` 提取到 `wiki/datasets/data_<id>.md`，供知识复用 |
| `publisher` | 生成 `REPRODUCIBILITY_CHECKLIST.md`，含数据完整性检查项 |

## 质量检查点（MAIN 阶段）

MAIN 阶段在 DESIGN 和 CODE 之后各插入一个检查点，在浪费 tokens 之前拦截问题。FAST_FAIL 阶段跳过检查点以保持快速。

### 设计审查（DESIGN_REVIEW）

实验设计完成后、编码开始前运行。包含两层：

| 层次 | 实现方式 | 成本 | 检查内容 |
|------|---------|------|---------|
| 结构检查 | `design_checks.py`（Python 规则） | 0 tokens | 基线 ≥2、统计检验定义、成功/失败标准、数据段、消融实验、种子 ≥3、资源预算 |
| 科学性审查 | `design_reviewer` LLM agent | ~2000 tokens | 假设对齐、基线充分性、统计方案合理性、成功标准现实性、混杂因素 |

结构检查结果注入 LLM 审查 prompt，LLM 聚焦科学判断而非格式检查。产出 `design_review.md`，判定 PASS / REVISE / BLOCK。

### 代码预检（CODE_PREFLIGHT）

代码完成后、实验运行前执行。**纯 Python，零 LLM 调用**：

| 检查项 | 方法 | 说明 |
|--------|------|------|
| 语法校验 | `compile()` | 语法错误 → BLOCK |
| import 可用性 | `importlib` 探测 | 缺包 → WARN |
| 模型覆盖 | 解析 plan vs 代码 | 计划中有但代码没实现 → WARN |
| 指标覆盖 | 解析 plan vs 代码 | 计划要求的指标未计算 → WARN |
| 数据源一致 | 对比 data.source vs 代码中的加载方式 | 用 sklearn 替代 fetch_dataset → ERROR |
| seed 参数 | 文本搜索 | 不接受 seed → WARN |
| 超时风险 | 启发式（深度学习、大循环、GridSearch） | HIGH/MEDIUM/LOW |
| G3 映射表 | 解析 CLAIM-TO-CODE 注释块 | 声明数 vs 映射数 |
| G3 trivial 检测 | AST 分析 | 常量返回、pass-through、空函数体、全路径相同常量 → WARN |

产出 `preflight_report.json`，Critic 可读取并纳入报告。

## 质询协议（MAIN 阶段）

MAIN 阶段在 CRITIC 之后、REPORT 之前插入**对抗式质询**环节。Interrogator agent 作为"红队"角色，对实验结果进行压力测试。FAST_FAIL 阶段跳过质询以保持快速。

### 质询流程

```
CRITIC → analysis_report.md
  → Interrogator 读取全部产物（hypothesis, plan, results, report）
    → 生成 3-5 个精准质询问题
      → 基于现有数据分析回答
        → 评估每个问题：SATISFIED / UNSATISFIED / INCONCLUSIVE
          → 迭代（可配置 1-3 轮）
            → 产出 interrogation_report.md
```

### 五类质询

| 类型 | 目的 | 示例 |
|------|------|------|
| 异常追问 | 发现指标不一致 | "准确率高但 F1 低，为什么？" |
| 反事实测试 | 验证因果关系 | "去掉特征 X 后结论是否成立？" |
| 边界测试 | 评估鲁棒性 | "数据量减半后方法是否仍然有效？" |
| 方法论质疑 | 统计方法合理性 | "5 个样本的 t-test 统计功效够吗？" |
| 替代解释 | 排除混杂因素 | "提升是否只来自更大的超参搜索空间？" |

配置：`research_config.yaml` 中 `interrogation.rounds` 控制轮次（默认 2）。

产出的 `interrogation_report.md` 包含优化建议和方法论风险，Professor 在里程碑审查时一并参考。

## 人类深度交互

除了 approve/kill/pause 等决策命令外，支持对话式协作：

```bash
# 查询：向 pipeline 提问
python scripts/run_research.py query --pipeline ID "为什么 group B 表现异常？"

# 干预：注入指令到下一个实验循环
python scripts/run_research.py tweak --pipeline ID "增加 ElasticNet 基线"
python scripts/run_research.py tweak --pipeline ID "将 learning_rate 改为 0.001"

# 探索：查看关键中间产物和指标
python scripts/run_research.py explore --pipeline ID
```

### 指令机制

`tweak` 写入 `user_directives.yaml` 到 pipeline 目录。`exp_designer` 和 `coder` 在下一个实验循环中读取并纳入设计/代码。每条指令有 applied/pending 状态追踪。

## 参考代码复用与数据一致性

### 代码复用

当参考论文提供开源代码时，实验设计中应分析是否复用：

```yaml
reference_code:
  papers:
    - title: "Referenced Paper"
      repo_url: "https://github.com/author/repo"
      reusable_components: ["evaluation pipeline", "data loader"]
      must_modify: ["model architecture"]
      build_on_top: true
  strategy: "fork_and_extend"  # fork_and_extend | borrow_components | rewrite
```

`coder` 在复用代码时标注原始来源和自有贡献，避免重复造轮子。

### 数据一致性原则

当实验目标是改进参考论文的结论时，**必须使用相同的数据**：

```yaml
data:
  consistency_requirement:
    reference_paper: "Paper X"
    must_match: ["dataset_version", "train_test_split", "preprocessing_pipeline"]
    verification: "Download from same source, verify SHA256"
```

Critic 的 G9 审计增加 `Reference data consistency` 检查项，不一致时标记 FAIL。

## 论文输出与投稿打包

每个 Pipeline 生成 `paper/<pipeline_id>/`：
```
draft_v1.md → audit_report.md → review_v1.md → paper.tex → paper.pdf
```

打包投稿：
```bash
python scripts/run_research.py publish --pipeline ID
```

生成 `publications/<pipeline_id>/`：
```
├── paper/                        # LaTeX、PDF、图表
├── code/                         # 干净代码（学术级英文注释）+ README
├── data/                         # 实验结果、数据集、data_manifest.yaml
├── PROCESS_REPORT.md             # 科研全过程解读 + 分级期刊/会议投递推荐（A*/A/B/Journal）
└── REPRODUCIBILITY_CHECKLIST.md  # G6 可复现清单：代码、依赖、种子、数据、README 验证
```

## 科研管线看板

```bash
python scripts/run_research.py dashboard              # 可视化概览
python scripts/dashboard.py --priority my_pipeline 3   # 优先级（0-3）
python scripts/dashboard.py --pause-miner              # 暂停新课题发现
python scripts/dashboard.py --resume-miner             # 恢复发现
```

展示：管线阶段图标、优先级标签、预算进度、研究方向追踪、矿工状态。调度器按优先级顺序执行管线。

## 目录结构

| 目录 | 用途 | 写入者 |
|------|------|--------|
| `researchos/` | Python 核心包 | 开发者 |
| `.openharness/plugins/researchos/` | 插件：agents、commands、hooks | 开发者 |
| `knowledge/raw/` | 原始 PDF 论文 | 心跳模块 / 手动 |
| `knowledge/wiki/` | 编译后的知识（含 decisions/topic_preferences_*.md 用户反馈）| 知识编译器 / 用户 |
| `knowledge/proposals/` | 矿工研究提案 + 评估报告 | 知识矿工 / Topic Evaluator |
| `knowledge/backlog/` | 等待重评的候补提案 | Topic Evaluator |
| `knowledge/archive/` | 过期和永久拒绝的提案 | BacklogManager |
| `artifacts/pipeline_<id>/` | Pipeline 状态、产出、data_manifest.yaml、design_review.md、preflight_report.json、cost_report.json、tracking_url.txt | Pipeline Agent |
| `artifacts/pipeline_<id>/branch_<n>/` | 并行分支产出（启用 branching 时） | Branch Agent |
| `data/raw/` | 每个管线的原始下载数据 | fetch_dataset 工具 |
| `data/processed/` | 清洗/划分后的数据 | Coder 生成的脚本 |
| `runtime/candidates/` | 待用户选择的候选课题 | Topic Evaluator |
| `runtime/` | 调度器状态、预算、心跳配置、workflow_config.yaml、research_config.yaml | 调度器 |
| `decisions/` | Professor 决策和评审报告 | Professor Agent |
| `paper/<pipeline_id>/` | 论文输出（md, tex, pdf） | Paper Writer |
| `publications/<pipeline_id>/` | 投稿打包 | Publisher |

## 核心组件

### 运行时（`researchos/runtime/`）

| 模块 | 职责 |
|------|------|
| `scheduler.py` | Tick 驱动调度器，按优先级调度 + 候补处理 |
| `topic_evaluator.py` | 8步提案评估流水线 + BacklogManager |
| `agent_executor.py` | OpenAI 兼容的工具调用循环 |
| `dispatcher.py` | Prompt 构建 + 多后端路由 |
| `state_manager.py` | 原子化文件 CRUD |
| `knowledge_compiler.py` | PDF/MD → wiki；Pipeline 产物 + 数据清单 → wiki |
| `paper_audit.py` | 自动化 G4 数值一致性 + G5 统计完整性 + VLM 图表质量审查 |
| `latex_compiler.py` | Markdown → LaTeX → PDF |
| `heartbeat.py` | arXiv PDF 抓取器 + 引用过滤 |
| `publisher.py` | 投稿打包 + 期刊推荐 |
| `frontend_state.py` | React 看板 JSON 状态提供者 |
| `recovery.py` | 检查点 + 指数退避重试 |
| `design_checks.py` | 实验设计结构验证（10 项规则检查，纯 Python，0 tokens） |
| `preflight.py` | 代码预检：语法、import、模型/指标一致性、数据源合规、G3 trivial 检测、超时风险 |
| `hooks.py` | 产物写入、知识库访问、预算验证、results.json schema 强制校验、数据源合规预检 |
| `workflow_loader.py` | YAML 工作流配置加载器，替代硬编码步骤路由 |
| `remote_executor.py` | 实验执行后端抽象（本地/Docker/SSH） |
| `experiment_tracker.py` | W&B / MLflow 实验追踪集成 |
| `branch_manager.py` | 并行实验分支管理（创建/调度/选择/合并） |

### 智能体（17 个）

智能体在管线各阶段执行 9 道质量关卡（G1-G9）：

| 关卡 | 审查内容 | 执行位置 |
|------|---------|---------|
| G1 | 主张-实验一致性 | hypothesis_gen, critic |
| G2 | 因果与对照有效性 | exp_designer |
| G3 | 实现真实性 | coder, **code_preflight**（结构+trivial检测）, critic（结果级验证） |
| G4 | 数据与结果一致性 | paper_auditor（自动化 + LLM）, reviewer_simulator |
| G5 | 统计报告完整性 | exp_designer, critic, paper_auditor（自动化） |
| G6 | 可复现性 | coder, publisher（REPRODUCIBILITY_CHECKLIST.md） |
| G7 | 开销与资源报告 | exp_designer, reporter |
| G8 | 结论边界 | paper_writer, reviewer_simulator |
| G9 | 数据有效性与溯源 | exp_designer, coder, experiment_runner, critic |

| 智能体 | 职责 |
|--------|------|
| `professor` | 评估提案、审查里程碑、批准/终止/转向/暂停/保留 |
| `knowledge_miner` | 方向感知的知识空白发现 |
| `hypothesis_gen` | 基于知识库生成可证伪假设 + G1 关键词可操作化检查 |
| `exp_designer` | 实验方案设计 + 数据计划 + G2 因果对照 + G5 统计要求 + G7 开销 + G9 数据设计 |
| `design_reviewer` | 审查实验设计的科学性、基线充分性、成功标准合理性（MAIN 阶段，含自动结构检查） |
| `coder` | 实验代码 + G3 声明-代码映射 + G6 可复现性 + G9 数据完整性（获取 + 清洗） |
| `experiment_runner` | 通过 `execute_python` + `fetch_dataset` 执行实验；记录数据溯源（G9） |
| `critic` | 结果分析 + G1 声明-实验审计 + G3 结果级验证 + G5 统计检查 + G9 数据有效性（含参考数据一致性） |
| `interrogator` | 对抗式质询：异常追问、反事实测试、方法论质疑、替代解释（MAIN 阶段，1-3 轮） |
| `reporter` | 生成里程碑报告 + G7 计算开销明细 |
| `paper_writer` | 撰写学术论文 + G8 结论边界 + 结构化局限性 |
| `paper_auditor` | 自动化 G4 数值扫描（代码）+ G5 统计完整性（代码）+ LLM 语义审计 |
| `reviewer_simulator` | 模拟同行评审 + G4 数据审计 + G8 范围/过度声明检查 |
| `fixer` | 根据 Professor 评审报告自动修正代码并重跑实验 |
| `lit_scout` | 通过 kb_retrieve 进行文献调研 |
| `vlm_reviewer` | 视觉语言模型审查论文图表质量（坐标轴、图例、分辨率、样式、数据一致性） |
| `branch_selector` | 比较并行实验分支结果，选择最佳方案继续推进 |

## 核心设计原则

1. **基于文件的状态机** — 所有状态在 JSON/YAML 中；调度器无状态、幂等
2. **知识驱动** — 通过 `kb_retrieve`（BM25 + n-gram + 可选 FAISS 向量混合检索）访问知识库；数据集追踪到 `wiki/datasets/`
3. **Topic 评估 + Professor 门控** — 提案先经 8 步自动评估（相似度、评分、证伪设计），再由 Professor 审查；失败时绝不自动批准
4. **自适应管线** — 智能体动态决定是否需要某些步骤
5. **人工介入可选** — `--human-review` 开启时 Professor 写推荐报告
6. **自动修正闭环** — Fixer agent 读取评审报告 → 修正代码 → 重跑 → 恢复
7. **预算强制执行** — 按管线分配 credit；超额阻塞
8. **优先级调度** — 高优先级管线优先执行
9. **里程碑检查点** — 自动快照，失败可回滚
10. **投稿就绪** — 干净代码打包 + 分级期刊推荐 + 可复现性清单
11. **数据版本化** — SHA256 哈希数据清单、`fetch_dataset` 工具、"先划分后预处理"强制执行
12. **前置质量门禁** — MAIN 阶段设计审查（LLM+规则）+ 代码预检（纯 Python AST），在消耗 RUN tokens 前拦截缺陷
13. **用户驱动选题** — 默认展示候选课题由用户决策，反馈沉淀到 wiki 供系统学习偏好；全自动需显式开启
14. **对抗式质询** — MAIN 阶段 Interrogator agent 从异常、反事实、边界、方法论、替代解释五个维度质询实验结果
15. **代码复用优先** — 参考论文有开源代码时优先复用，避免重复造轮子；改进参考论文时严格保证数据一致性
16. **对话式协作** — 研究者可随时查询中间结果、注入实验指令、探索 pipeline 状态，而非仅限批处理式决策
17. **混合检索** — BM25 精确匹配 + FAISS 向量语义搜索，可配置权重（需安装 `researchos[vector]`）
18. **VLM 图表审查** — 视觉语言模型自动检查论文图表的坐标轴标注、图例、分辨率、数据-文本一致性
19. **精细成本追踪** — 每步记录 input/output tokens、模型、耗时、credits，Dashboard 全局展示
20. **并行实验分支** — MAIN 阶段可同时探索多条实验路径，自动选择最佳方案合并（灵感：AI Scientist v2 树搜索）
21. **远程执行** — 实验代码支持本地、Docker、SSH 三种后端，解决 GPU 算力限制
22. **YAML 驱动工作流** — Pipeline 步骤路由、agent 映射、超时配置均从 YAML 加载，无需改代码即可定制
23. **实验追踪集成** — 可选 W&B / MLflow 自动记录实验指标、配置、产物，tracking URL 显示在 Dashboard
24. **容器化部署** — Docker Compose 一键部署，含 pdflatex 论文编译环境

## Docker 部署

```bash
# 一键部署
docker compose up -d

# 设置 API 密钥
export RESEARCHOS_API_KEY=your_key
docker compose up
```

`docker-compose.yaml` 自动挂载 knowledge/、artifacts/、runtime/、decisions/、paper/、data/ 为持久卷。容器包含 pdflatex 用于论文编译。

## 混合检索（向量 + BM25）

知识库检索支持可选的 FAISS 向量搜索，与现有 BM25 + n-gram 组合使用：

```bash
# 安装向量搜索依赖
pip install researchos[vector]
```

在 `runtime/research_config.yaml` 中配置权重：

```yaml
retrieval:
  vector_weight: 0.3  # 0.0 = 纯 BM25，1.0 = 纯向量，0.3 = 推荐混合
```

未安装 sentence-transformers 时自动回退到纯 BM25。

## VLM 图表审查

论文审计自动使用视觉语言模型检查图表质量：

- 坐标轴标注、图例、分辨率、样式一致性、数据-文本一致性
- 集成到 `run_paper_audit()` → G4 + G5 + 图表审查三合一
- 需要支持 vision 的模型（通过 `RESEARCHOS_VLM_MODEL` 环境变量设置）

```yaml
vlm:
  enabled: true
  model: ""  # 空 = 使用 RESEARCHOS_VLM_MODEL 或 RESEARCHOS_MODEL
```

## 详细成本追踪

每次 agent 调用自动记录 `cost_report.json` 到 pipeline 目录：

- 每步：input/output tokens、模型名、耗时（秒）、credits
- 累计：总 tokens、总 credits、总耗时、调用次数
- Dashboard 展示全局成本概览 + 每个 pipeline 的成本详情

## 并行实验分支

MAIN 阶段可同时探索多条实验路径（灵感来源：AI Scientist v2 的树搜索）：

```yaml
branching:
  enabled: false  # 默认关闭
  max_branches: 3
```

启用后：HYPOTHESIS → 分支 → 各分支独立运行 DESIGN→CODE→RUN→CRITIC → branch_selector 选出最佳 → 合并回主 pipeline 继续。

## 远程执行

实验代码支持本地、Docker、SSH 三种执行后端：

```yaml
execution:
  backend: "local"  # local | docker | ssh
  docker:
    image: "python:3.11-slim"
    gpu: false
  ssh:
    host: "gpu-server.example.com"
    user: "researchos"
    key_path: "~/.ssh/id_rsa"
```

## YAML 工作流配置

Pipeline 步骤路由从代码硬编码迁移到 YAML 配置（`runtime/workflow_config.yaml`）：

- 自定义步骤顺序、agent 映射、超时、重试次数
- 添加/删除步骤无需改代码
- 不存在配置文件时自动使用默认行为

## 实验追踪（W&B / MLflow）

```bash
pip install researchos[wandb]   # 或 researchos[mlflow]
```

```yaml
tracking:
  backend: "wandb"  # none | wandb | mlflow
  wandb_project: "researchos"
```

实验结果自动记录到 W&B 或 MLflow，tracking URL 显示在 dashboard 中。

## 配置

| 变量 | 用途 | 默认值 |
|------|------|--------|
| `RESEARCHOS_API_KEY` | LLM API 密钥 | （必填） |
| `RESEARCHOS_BASE_URL` | API 基础 URL | `https://openrouter.ai/api/v1` |
| `RESEARCHOS_MODEL` | 模型名称 | `qwen/qwen3.6-plus:free` |
| `RESEARCHOS_VLM_MODEL` | 视觉语言模型（图表审查） | 回退到 RESEARCHOS_MODEL |

## 依赖

- Python >= 3.10
- pydantic >= 2.0、pyyaml >= 6.0、openai >= 1.0、pymupdf >= 1.23
- pdflatex（可选，编译 PDF 需要）
- 可选：`pip install researchos[vector]`（向量检索）、`researchos[wandb]`（W&B 追踪）、`researchos[mlflow]`（MLflow 追踪）

## 许可证

基础框架许可证参见 OpenHarness。
