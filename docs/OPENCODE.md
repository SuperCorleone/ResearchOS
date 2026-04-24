# 在本地 OpenCode 里启动 ResearchOS

有两种路径：**CLI 直调**（最简单，总能用）和 **MCP server**（OpenCode 自己
把 ResearchOS 的 5 个动作识别为 tools，可直接在对话里触发）。

## 路径 1：CLI 直调（推荐首选）

```bash
# 1. 装包（worktree 根目录）
cd /Users/adukopun/Documents/ClaudeCodeWorkspace/ResearchOS
pip install -e .

# 2. 配环境变量（任选一个 provider）
export RESEARCHOS_API_KEY="sk-..."
export RESEARCHOS_BASE_URL="https://api.openai.com/v1"   # 或 anthropic / openrouter / ollama
export RESEARCHOS_MODEL="gpt-5.4"

# 3. 在 OpenCode 里随便开一个 shell tool 或在项目根目录开 opencode，然后：
researchos trial --mode simulate    # 不调 LLM，验证管线跑通
researchos trial                     # 真跑一个 pipeline，跑完即停
researchos launch                    # 全自主调度循环
researchos state <pipeline_id>       # 看 state.json
researchos kill <pipeline_id>        # 紧急制动
researchos tweak <pipeline_id> --directive "换一种 baseline"
```

`RESEARCHOS_RUNTIME` 环境变量切后端：

| 值 | 行为 |
|---|---|
| `sdk`（默认）| 直接打 OpenAI-compat SDK（最轻量）|
| `openharness` | 走 `oh -p` subprocess（需装 OpenHarness）|
| `skill` | 用 `.skill_handoff/` 文件握手（由 Claude Skill 宿主回填）|
| `mcp` | 把自己暴露为 MCP 工具（见路径 2）|

## 路径 2：把 ResearchOS 挂为 OpenCode 的 MCP server

OpenCode 支持本地 MCP server。装好包后，`researchos-mcp` 这个命令会用
官方 `mcp` SDK 起 stdio server，暴露 5 个 tool：

- `researchos_launch` — 启动自主调度循环
- `researchos_trial` — 跑单个 pipeline
- `researchos_get_state` — 查 state
- `researchos_inject_directive` — 注入用户指令
- `researchos_kill` — 终止 pipeline

### 配置

在项目根（或 `~/.config/opencode/opencode.json`）放一份 `opencode.json`，
仓库里已有 `opencode.example.json` 可直接复制：

```bash
cp opencode.example.json opencode.json
```

内容：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "researchos": {
      "type": "local",
      "command": ["researchos-mcp"],
      "environment": {
        "RESEARCHOS_PROJECT_ROOT": "${workspaceFolder}"
      }
    }
  },
  "instructions": ["CLAUDE.md"]
}
```

注意：

- `researchos-mcp` 在 PATH 里由 `pip install -e .` 注册（见 pyproject
  `[project.scripts]`）；如果装在 venv 里，用绝对路径指向 venv 的
  bin：`"command": ["/path/to/venv/bin/researchos-mcp"]`。
- OpenCode 会把 `${workspaceFolder}` 替换成当前项目根，ResearchOS 所有产物
  （`artifacts/`、`runtime/`、`decisions/`、`paper/`…）就落在那里。
- `RESEARCHOS_API_KEY` / `RESEARCHOS_BASE_URL` / `RESEARCHOS_MODEL` 可放进
  `environment` 块，也可以放 shell 环境里。

### 启动后在 OpenCode 对话里怎么用

连上 MCP server 之后，OpenCode 会自动把 5 个 tool 加到可用集合。示例对话：

> **你**：用 ResearchOS 试跑一次，simulate 模式。
>
> **OpenCode**：调用 `researchos_trial({"mode":"simulate"})`…
> 返回 `{"status":"trial_complete"}`。产物在 `artifacts/pipeline_<id>/`。

> **你**：pipeline_xxx 现在什么状态？
>
> **OpenCode**：调用 `researchos_get_state({"pipeline_id":"xxx"})`，打出 JSON。

> **你**：给 pipeline_xxx 追加一条指令：换 ElasticNet 做 baseline。
>
> **OpenCode**：调用 `researchos_inject_directive(...)`，下一 DESIGN / CODE /
> RUN step 的 prompt 里会带上。

## 故障排查

- **`command not found: researchos` 或 `researchos-mcp`**
  → 没 `pip install -e .`，或 OpenCode 启动时 PATH 不含你的 venv。用
    绝对路径指向 `/path/to/venv/bin/researchos-mcp`。
- **MCP 连接失败 / tool list 为空**
  → 在另一个 shell 先手跑 `researchos-mcp < /dev/null`，看有没有报错
    （能正常阻塞等 stdin 就说明 import 和注册都 OK）。
- **`RESEARCHOS_API_KEY is not set`**
  → env 没传进去；在 `opencode.json::environment` 或系统 shell 里加。
- **Pipeline 立刻 BLOCKED**
  → `state.last_error` 会写 `contract_violation:<guard>:<msg>`；这是预期的
    护栏。查对应的 digest 文件（`hypothesis.digest.json` /
    `plan.digest.json` / `run.digest.json` / `concepts.digest.json`）修正。
- **想先只跑 FAST_FAIL 看看**
  → `researchos trial --mode simulate` 不调 LLM，验证全链路；拿到真实
    API key 后再 `researchos trial`。

## 验证安装

```bash
# 1. import smoke
python -c "from researchos.agent_runtime.base import available_runtimes; \
           import researchos.agent_runtime.sdk, adapters.openharness.runtime, adapters.skill.runtime; \
           print(available_runtimes())"
# → ['openharness', 'sdk', 'skill']

# 2. MCP tool 列表
python -c "from adapters.mcp.server import TOOLS; print([t['name'] for t in TOOLS])"
# → ['researchos_launch', 'researchos_trial', 'researchos_get_state',
#    'researchos_inject_directive', 'researchos_kill']

# 3. 测试全套
python -m pytest tests/ -q
# → 226 passed / 4 skipped
```
