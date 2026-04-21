"""Lightweight LLM agent executor.

Uses the ``openai`` SDK (compatible with OpenAI-protocol providers like
DeepSeek, OpenAI, etc.) to run research agents in a tool-use loop.

Environment variables:
    RESEARCHOS_API_KEY   - API key (required)
    RESEARCHOS_BASE_URL  - API base URL (default: https://openrouter.ai/api/v1)
    RESEARCHOS_MODEL     - Model name (default: qwen/qwen3.6-plus:free)
"""

from __future__ import annotations

import json
import logging
import re
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from researchos.mcp_servers.kb_server import search_wiki
from researchos.runtime.hooks import (
    record_budget_usage,
    validate_artifact_write,
    validate_budget,
    validate_knowledge_access,
)
from researchos.runtime.llm_client import build_cached_messages, route_model

logger = logging.getLogger(__name__)

ESTIMATED_COST_PER_CALL = 500

_AGENTS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / ".openharness"
    / "plugins"
    / "researchos"
    / "agents"
)

# Tool schemas in OpenAI function-calling format
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": (
                "Read a file from the pipeline directory. "
                "Path is relative to the pipeline artifact directory. "
                "Do NOT read from knowledge/wiki/ directly — use kb_retrieve instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the pipeline directory",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": (
                "Write a file to the pipeline directory. "
                "Path is relative to the pipeline artifact directory. "
                "Do NOT write to state.json — it is managed by the scheduler."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the pipeline directory",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kb_retrieve",
            "description": (
                "Search the knowledge base for relevant wiki pages. "
                "All agents MUST use this tool instead of directly reading "
                "knowledge/wiki/ files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for the knowledge base",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Maximum tokens in response (default 4000)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Execute a Python script and return stdout/stderr. "
                "The script runs in a temporary file with a 120-second timeout. "
                "Use this to run experiments and collect metrics. "
                "The script should print a JSON object on the LAST line of stdout "
                "with keys: metrics (dict of floats), config (dict), seed (int)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python source code to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 120)",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_dataset",
            "description": (
                "Download a dataset to /data/raw/<pipeline_id>/. "
                "Supports direct URLs (CSV, JSON, ZIP, etc.). "
                "After download, generates data_manifest.yaml with SHA256 hashes. "
                "Returns the manifest summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to download (direct link to data file)",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Target filename (auto-detected from URL if omitted)",
                    },
                    "dataset_name": {
                        "type": "string",
                        "description": "Dataset name for manifest metadata (e.g., 'MovieLens-1M')",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


# ── Non-function-calling model support ──────────────────────────

# Models known to NOT support native function/tool calling.
_NO_FC_PATTERNS = (
    "deepseek-chat", "deepseek-coder", "deepseek-r1",
    "qwen/qwen2.5", "qwen/qwen2",
)


def _supports_function_calling(model: str) -> bool:
    """Check if a model supports native OpenAI-style function calling."""
    m = model.strip().lower()
    for pattern in _NO_FC_PATTERNS:
        if pattern in m:
            return False
    return True


def _tools_as_text() -> str:
    """Convert tool schemas to text description for non-FC models."""
    lines = ["Available tools (call using XML format shown below):\n"]
    for tool in _TOOLS:
        fn = tool["function"]
        params = fn["parameters"].get("properties", {})
        required = fn["parameters"].get("required", [])
        lines.append(f"### {fn['name']}")
        lines.append(f"{fn['description']}")
        lines.append("Parameters:")
        for pname, pinfo in params.items():
            req = " (required)" if pname in required else " (optional)"
            lines.append(f"  - {pname}: {pinfo.get('description', '')}{req}")
        lines.append("")
    lines.append("To call a tool, use this EXACT format:")
    lines.append("<function=tool_name>")
    lines.append("<parameter=param_name>value</parameter>")
    lines.append("</function>")
    lines.append("")
    lines.append("You may call multiple tools. After all tool calls, continue with your analysis.")
    return "\n".join(lines)


def _parse_text_tool_calls(text: str) -> list[dict]:
    """Parse XML-tag style tool calls from text output."""
    import re as _re
    calls = []
    pattern = r'<function=(\w+)>(.*?)</function>'
    for match in _re.finditer(pattern, text, _re.DOTALL):
        fn_name = match.group(1)
        body = match.group(2)
        params = {}
        param_pattern = r'<parameter=(\w+)>(.*?)</parameter>'
        for pm in _re.finditer(param_pattern, body, _re.DOTALL):
            params[pm.group(1)] = pm.group(2).strip()
        calls.append({"name": fn_name, "arguments": params})
    return calls


class AgentExecutor:
    """Execute a research agent via LLM API with tool-use loop (OpenAI-compatible)."""

    def __init__(
        self,
        project_root: Path,
        *,
        model: str | None = None,
        max_turns: int = 20,
        max_tokens: int = 8192,
    ) -> None:
        self.project_root = Path(project_root)
        self.model = model or os.environ.get("RESEARCHOS_MODEL", "qwen/qwen3.6-plus:free") # "deepseek-chat"
        self.max_turns = max_turns
        self.max_tokens = max_tokens

        api_key = os.environ.get("RESEARCHOS_API_KEY")
        if not api_key:
            raise RuntimeError(
                "RESEARCHOS_API_KEY environment variable is not set. "
                "Set it to use the LLM agent executor."
            )

        base_url = os.environ.get("RESEARCHOS_BASE_URL", "https://openrouter.ai/api/v1") # "https://api.deepseek.com"

        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._execution_backend: Any = None  # lazy remote_executor.ExecutionBackend
        self._kb_cache: dict[str, str] = {}

    # ── Agent definition loading ─────────────────────────────────

    def _load_agent_definition(self, agent_name: str) -> str:
        md_path = _AGENTS_DIR / f"{agent_name}.md"
        if md_path.exists():
            return md_path.read_text(encoding="utf-8")
        logger.warning("Agent definition not found: %s, using generic prompt", agent_name)
        return (
            f"You are the **{agent_name}** research agent. "
            f"Follow the instructions in the user prompt carefully. "
            f"Write your output artifacts to the pipeline directory using the file_write tool."
        )

    # ── Tool execution ───────────────────────────────────────────

    # Directories that agents are allowed to write to (beyond their pipeline dir)
    _ALLOWED_WRITE_DIRS = ("decisions", "paper", "knowledge/proposals")

    def _resolve_path(self, raw_path: str, pipeline_dir: Path) -> Path | None:
        """Resolve a path from an LLM tool call.

        Handles:
        1. Absolute paths within the project root
        2. Project-root-relative paths (e.g. "knowledge/proposals/...")
        3. Pipeline-dir-relative paths (e.g. "hypothesis.md")
        """
        project_root = self.project_root.resolve()

        # 1. Absolute path
        if raw_path.startswith("/"):
            abs_path = Path(raw_path).resolve()
            if abs_path.is_relative_to(project_root):
                if abs_path.is_relative_to(pipeline_dir.resolve()):
                    return abs_path
                for allowed in self._ALLOWED_WRITE_DIRS:
                    if abs_path.is_relative_to(project_root / allowed):
                        return abs_path
            return None

        # 2. Check if it's a project-root-relative path to an allowed dir
        for allowed in self._ALLOWED_WRITE_DIRS:
            if raw_path.startswith(allowed + "/") or raw_path.startswith(allowed + "\\"):
                resolved = (project_root / raw_path).resolve()
                if resolved.is_relative_to(project_root / allowed):
                    return resolved

        # 3. Pipeline-dir-relative path
        resolved = (pipeline_dir / raw_path).resolve()
        if resolved.is_relative_to(pipeline_dir.resolve()):
            return resolved
        return None

    def _execute_python(self, code: str, timeout: int = 120) -> str:
        """Run Python code via the configured execution backend.

        Delegates to ``remote_executor.ExecutionBackend`` which may be local
        subprocess (default), Docker, or SSH depending on research_config.yaml.
        """
        if self._execution_backend is None:
            self._execution_backend = self._load_execution_backend()
        try:
            working_dir = self.project_root / "scratch"
            working_dir.mkdir(parents=True, exist_ok=True)
            data_dir = self.project_root / "data"
            return self._execution_backend.execute(
                code, timeout, working_dir,
                data_dir=data_dir if data_dir.exists() else None,
            )
        except Exception as e:
            return f"ERROR: {e}"

    def _load_execution_backend(self) -> Any:
        """Load execution backend from research_config.yaml."""
        import yaml as _yaml
        from researchos.runtime.remote_executor import create_backend
        config_path = self.project_root / "runtime" / "research_config.yaml"
        config: dict = {}
        if config_path.exists():
            try:
                config = _yaml.safe_load(config_path.read_text()) or {}
            except Exception:
                pass
        return create_backend(config)

    def _fetch_dataset(
        self,
        url: str,
        filename: str | None,
        dataset_name: str,
        pipeline_id: str,
    ) -> str:
        """Download a dataset and generate its manifest."""
        import urllib.request
        import urllib.parse
        from researchos.utils.data_manifest import generate_manifest, write_manifest
        from researchos.utils.paths import DATA_RAW_DIR

        if not url:
            return "ERROR: url is required"

        # Determine target directory: /data/raw/<pipeline_id>/
        target_dir = DATA_RAW_DIR / pipeline_id
        target_dir.mkdir(parents=True, exist_ok=True)

        # Determine filename
        if not filename:
            parsed = urllib.parse.urlparse(url)
            filename = Path(parsed.path).name or "dataset"

        target_file = target_dir / filename

        # Download
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            target_file.write_bytes(data)
            size_kb = len(data) // 1024
        except Exception as e:
            return f"ERROR: Download failed: {e}"

        # Handle ZIP files
        if target_file.suffix.lower() == ".zip":
            import zipfile
            try:
                with zipfile.ZipFile(target_file) as zf:
                    zf.extractall(target_dir)
                extracted = len(zf.namelist())
                result_msg = f"Downloaded and extracted {extracted} files ({size_kb}KB)"
            except zipfile.BadZipFile:
                result_msg = f"Downloaded {filename} ({size_kb}KB) — not a valid ZIP"
        else:
            result_msg = f"Downloaded {filename} ({size_kb}KB)"

        # Generate manifest
        manifest = generate_manifest(
            str(target_dir),
            pipeline_id=pipeline_id,
            dataset_name=dataset_name or filename,
            source_url=url,
        )
        manifest_path = target_dir / "data_manifest.yaml"
        write_manifest(manifest, manifest_path)

        # Also copy manifest to pipeline artifact dir for traceability
        pdir = self.project_root / "artifacts" / f"pipeline_{pipeline_id}"
        if pdir.exists():
            write_manifest(manifest, pdir / "data_manifest.yaml")

        return (
            f"{result_msg}\n"
            f"Saved to: data/raw/{pipeline_id}/{filename}\n"
            f"Manifest: {manifest['total_files']} files, "
            f"hash={manifest['directory_hash'][:30]}...\n"
            f"data_manifest.yaml written to pipeline dir."
        )

    MAX_TOOL_OUTPUT_CHARS = 12000

    @staticmethod
    def _truncate_tool_output(text: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
        """Truncate long tool outputs keeping head + tail to prevent context bloat."""
        if not isinstance(text, str) or len(text) <= max_chars:
            return text
        head = max_chars * 3 // 5  # 60% head
        tail = max_chars * 2 // 5  # 40% tail
        dropped = len(text) - head - tail
        return f"{text[:head]}\n\n...[output truncated: {dropped} chars omitted]...\n\n{text[-tail:]}"

    def _handle_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        pipeline_dir: Path,
        pipeline_id: str,
    ) -> tuple[str, str | None]:
        """Execute a single tool call with hook enforcement."""
        if tool_name == "file_read":
            raw_path = arguments.get("path", "")
            check = validate_knowledge_access("file_read", {"file_path": raw_path})
            if not check["allowed"]:
                return (f"BLOCKED: {check['reason']}", None)
            # Try resolving within pipeline dir first, then as absolute within project
            target = self._resolve_path(raw_path, pipeline_dir)
            if target is None:
                # Allow reading absolute paths within the project root
                abs_path = Path(raw_path).resolve()
                if abs_path.is_relative_to(self.project_root.resolve()):
                    target = abs_path
                else:
                    return ("ERROR: Path escapes project directory.", None)
            if not target.exists():
                return (f"ERROR: File not found: {raw_path}", None)
            try:
                content = target.read_text(encoding="utf-8")
                return (self._truncate_tool_output(content), None)
            except Exception as e:
                return (f"ERROR reading file: {e}", None)

        elif tool_name == "file_write":
            raw_path = arguments.get("path", "")
            content = arguments.get("content", "")
            target = self._resolve_path(raw_path, pipeline_dir)
            if target is None:
                return ("ERROR: Path escapes pipeline directory.", None)
            check = validate_artifact_write(str(target), content)
            if not check["allowed"]:
                return (f"BLOCKED: {check['reason']}", None)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                artifact_name = target.name
                msg = f"Successfully wrote {artifact_name} ({len(content)} chars)"
                if check.get("reason"):
                    msg += f"\nNote: {check['reason']}"
                return (msg, artifact_name)
            except Exception as e:
                return (f"ERROR writing file: {e}", None)

        elif tool_name == "kb_retrieve":
            query = arguments.get("query", "")
            max_results = arguments.get("max_results", 5)
            max_tokens = arguments.get("max_tokens", 4000)
            cache_key = " ".join(query.lower().split())
            if cache_key in self._kb_cache:
                result = self._kb_cache[cache_key]
            else:
                result = search_wiki(query, max_results, max_tokens)
                if isinstance(result, str):
                    self._kb_cache[cache_key] = result
            return (self._truncate_tool_output(result), None)

        elif tool_name == "execute_python":
            code = arguments.get("code", "")
            timeout = arguments.get("timeout", 120)
            result = self._execute_python(code, timeout)
            return (self._truncate_tool_output(result), None)

        elif tool_name == "fetch_dataset":
            result = self._fetch_dataset(
                url=arguments.get("url", ""),
                filename=arguments.get("filename"),
                dataset_name=arguments.get("dataset_name", ""),
                pipeline_id=pipeline_id,
            )
            return (result, "data_manifest.yaml")

        else:
            return (f"ERROR: Unknown tool '{tool_name}'", None)

    # ── Main execution loop ──────────────────────────────────────

    async def execute(
        self,
        agent_name: str,
        prompt: str,
        pipeline_id: str,
        pipeline_dir: Path,
    ) -> dict[str, Any]:
        """Run the agent in a tool-use loop until completion (OpenAI format)."""
        if not hasattr(self, "_kb_cache"):
            self._kb_cache = {}
        self._kb_cache.clear()
        budget_check = validate_budget(
            pipeline_id, ESTIMATED_COST_PER_CALL, project_root=self.project_root
        )
        if not budget_check["allowed"]:
            return {
                "success": False,
                "output": f"Budget blocked: {budget_check['reason']}",
                "artifacts_written": [],
                "tokens_used": 0,
            }

        system_prompt = self._load_agent_definition(agent_name)
        messages = build_cached_messages(
            system_prompt,
            user_prompt=prompt,
            model=self.model,
        )
        artifacts_written: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0
        final_text = ""
        start_time = time.monotonic()

        # gpt-5.x and o-series models use max_completion_tokens instead of max_tokens
        _uses_completion_tokens = bool(
            re.match(r"(o1|o3|o4|gpt-5)", self.model.split("/")[-1].lower())
        )
        _token_kwarg = "max_completion_tokens" if _uses_completion_tokens else "max_tokens"

        # Detect if model supports native function calling
        use_fc = _supports_function_calling(self.model)
        if not use_fc:
            messages[0]["content"] += "\n\n" + _tools_as_text()

        for turn in range(self.max_turns):
            # Retry loop for rate-limit (429) errors
            response = None
            for _retry in range(10):
                try:
                    api_kwargs: dict[str, Any] = {
                        "model": self.model,
                        _token_kwarg: self.max_tokens,
                        "messages": messages,
                    }
                    if use_fc:
                        api_kwargs["tools"] = _TOOLS
                    response = self._client.chat.completions.create(**api_kwargs)
                    break
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "rate" in err_str.lower():
                        wait = min(30 * (2 ** _retry), 300)
                        logger.warning("Rate limited on turn %d, waiting %ds (retry %d/10)...", turn, wait, _retry + 1)
                        time.sleep(wait)
                        continue
                    logger.error("API error on turn %d: %s", turn, e)
                    return {
                        "success": False,
                        "output": f"API error: {e}",
                        "artifacts_written": artifacts_written,
                        "tokens_used": total_input_tokens + total_output_tokens,
                    }
            if response is None:
                logger.error("Exhausted rate-limit retries on turn %d", turn)
                return {
                    "success": False,
                    "output": "Rate limit retries exhausted",
                    "artifacts_written": artifacts_written,
                    "tokens_used": total_input_tokens + total_output_tokens,
                }

            if response.usage:
                total_input_tokens += response.usage.prompt_tokens
                total_output_tokens += response.usage.completion_tokens

            choice = response.choices[0]
            message = choice.message

            if message.content:
                final_text = message.content

            tool_calls = message.tool_calls or []

            # Non-FC model: parse tool calls from text output
            if not use_fc and message.content:
                text_calls = _parse_text_tool_calls(message.content)
                if text_calls:
                    messages.append({"role": "assistant", "content": message.content})
                    for tc in text_calls:
                        result_text, artifact = self._handle_tool_call(
                            tc["name"], tc["arguments"], pipeline_dir, pipeline_id
                        )
                        if artifact:
                            artifacts_written.append(artifact)
                        messages.append({"role": "user", "content": f"Tool result for {tc['name']}:\n{result_text}"})
                    continue  # Next turn

            if choice.finish_reason != "tool_calls" and not tool_calls:
                break

            # Append the assistant message (with tool_calls) to history
            messages.append(message.model_dump())

            for tc in tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}
                result_text, artifact = self._handle_tool_call(
                    tc.function.name,
                    arguments,
                    pipeline_dir,
                    pipeline_id,
                )
                if artifact:
                    artifacts_written.append(artifact)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

        total_tokens = total_input_tokens + total_output_tokens
        credits_used = max(1, (total_tokens + 999) // 1000)
        duration = time.monotonic() - start_time
        record_budget_usage(pipeline_id, credits_used, project_root=self.project_root)

        # Write detailed cost record
        self._write_cost_record(
            pipeline_id=pipeline_id,
            pipeline_dir=pipeline_dir,
            agent_name=agent_name,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            credits_used=credits_used,
            duration_seconds=round(duration, 2),
        )

        # Track experiment if results were produced (e.g., after experiment_runner)
        self._maybe_track_experiment(pipeline_id, pipeline_dir)

        logger.info(
            "Agent %s completed for pipeline %s: %d artifacts, %d tokens, %.1fs",
            agent_name, pipeline_id, len(artifacts_written), total_tokens, duration,
        )

        return {
            "success": True,
            "output": final_text,
            "artifacts_written": artifacts_written,
            "tokens_used": total_tokens,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "duration_seconds": round(duration, 2),
            "model": self.model,
        }

    def _maybe_track_experiment(
        self, pipeline_id: str, pipeline_dir: Path
    ) -> str | None:
        """If tracking enabled and results.json exists, log to tracker."""
        results_path = pipeline_dir / "results.json"
        if not results_path.exists():
            return None

        try:
            import yaml as _yaml
            config_path = self.project_root / "runtime" / "research_config.yaml"
            if not config_path.exists():
                return None
            cfg = _yaml.safe_load(config_path.read_text()) or {}
            if cfg.get("tracking", {}).get("backend", "none") == "none":
                return None

            from researchos.runtime.experiment_tracker import create_tracker
            tracker = create_tracker(cfg)

            results = json.loads(results_path.read_text())
            metrics = results.get("metrics", {})
            config = results.get("config", {})
            seed = results.get("seed")
            if seed is not None:
                config["seed"] = seed

            data_hash = None
            manifest_path = pipeline_dir / "data_manifest.yaml"
            if manifest_path.exists():
                manifest = _yaml.safe_load(manifest_path.read_text())
                data_hash = manifest.get("directory_hash") if manifest else None

            artifact_files = [
                f for f in pipeline_dir.iterdir()
                if f.is_file() and f.suffix in ('.json', '.yaml', '.md')
            ]

            url = tracker.log_run(
                pipeline_id=pipeline_id,
                metrics=metrics,
                config=config,
                data_hash=data_hash,
                artifacts=artifact_files,
            )
            if url:
                (pipeline_dir / "tracking_url.txt").write_text(url)
                logger.info("Experiment tracked: %s", url)
            return url
        except Exception as e:
            logger.warning("Experiment tracking failed: %s", e)
            return None

    def _write_cost_record(
        self,
        pipeline_id: str,
        pipeline_dir: Path,
        agent_name: str,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        credits_used: int,
        duration_seconds: float,
    ) -> None:
        """Append a cost record to the pipeline's cost_report.json."""
        from researchos.schemas.cost import CostRecord, CostReport

        report_path = pipeline_dir / "cost_report.json"

        if report_path.exists():
            try:
                report = CostReport.model_validate_json(report_path.read_text())
            except Exception:
                report = CostReport(pipeline_id=pipeline_id)
        else:
            report = CostReport(pipeline_id=pipeline_id)

        record = CostRecord(
            pipeline_id=pipeline_id,
            step_name=agent_name,
            agent_name=agent_name,
            model_name=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            credits_used=credits_used,
            duration_seconds=duration_seconds,
        )
        report.add_record(record)

        try:
            report_path.write_text(report.model_dump_json(indent=2))
        except Exception as e:
            logger.warning("Failed to write cost report: %s", e)

    def execute_sync(
        self,
        agent_name: str,
        prompt: str,
        pipeline_id: str,
        pipeline_dir: Path,
    ) -> dict[str, Any]:
        import asyncio
        return asyncio.run(self.execute(agent_name, prompt, pipeline_id, pipeline_dir))
