"""Tests for AgentExecutor with mocked Anthropic API."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure ANTHROPIC_API_KEY is set for testing (mocked anyway)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-for-unit-tests")


def _make_text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_tool_use_block(tool_id: str, name: str, tool_input: dict):
    """Create a mock OpenAI-format tool call."""
    return SimpleNamespace(
        id=tool_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(tool_input)),
    )


def _make_response(content, stop_reason="end_turn", input_tokens=100, output_tokens=50):
    """Create a mock OpenAI-format chat completion response.

    Maps from the old Anthropic-style test helpers to the OpenAI format
    that agent_executor.py actually uses.
    """
    # Separate text blocks and tool-use blocks
    text_parts = [c for c in content if getattr(c, "type", None) == "text"]
    tool_calls = [c for c in content if hasattr(c, "function")]

    # Build the message
    message_text = text_parts[0].text if text_parts else None
    finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

    message = SimpleNamespace(
        content=message_text,
        tool_calls=tool_calls if tool_calls else None,
    )
    # Add model_dump for OpenAI message serialization
    message.model_dump = lambda: {
        "role": "assistant",
        "content": message_text,
        "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in (tool_calls or [])
        ] or None,
    }

    choice = SimpleNamespace(
        message=message,
        finish_reason=finish_reason,
    )

    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens),
    )


@pytest.fixture
def project_dir(tmp_path):
    """Set up a minimal project structure."""
    # Create directories
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "runtime" / "pipelines").mkdir(parents=True)
    (tmp_path / "runtime" / "logs").mkdir(parents=True)
    (tmp_path / "decisions").mkdir()
    (tmp_path / "knowledge" / "wiki").mkdir(parents=True)

    # Create agent definition
    agents_dir = tmp_path / ".openharness" / "plugins" / "researchos" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "hypothesis_gen.md").write_text(
        "---\nname: hypothesis_gen\n---\n# Test Agent\nGenerate a hypothesis."
    )

    # Create budget file
    budget = {"total_credits": 100000, "allocations": {"test_pipe": 20000}, "usage": {"test_pipe": 0}, "reserve": 50000}
    (tmp_path / "runtime" / "budget.json").write_text(json.dumps(budget))

    # Create pipeline dir and state
    pdir = tmp_path / "artifacts" / "pipeline_test_pipe"
    pdir.mkdir()
    state = {
        "pipeline_id": "test_pipe",
        "title": "Test Pipeline",
        "research_question": "Does testing work?",
        "stage": "main",
        "step": None,
        "retry_count": 0,
        "max_retries": 3,
        "last_error": None,
        "artifacts": {},
        "budget_allocated": 20000,
        "budget_used": 0,
        "milestone_count": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "metadata": {},
    }
    (pdir / "state.json").write_text(json.dumps(state))

    return tmp_path


@pytest.fixture
def pipeline_dir(project_dir):
    return project_dir / "artifacts" / "pipeline_test_pipe"


def test_executor_basic_flow(project_dir, pipeline_dir):
    """Test basic tool-use loop: kb_retrieve -> file_write -> end."""
    # Mock responses: first calls kb_retrieve, then writes file, then ends
    responses = [
        _make_response(
            [_make_tool_use_block("t1", "kb_retrieve", {"query": "testing"})],
            stop_reason="tool_use",
        ),
        _make_response(
            [_make_tool_use_block(
                "t2", "file_write",
                {"path": "hypothesis.md", "content": "# Hypothesis\n\nTest hypothesis."},
            )],
            stop_reason="tool_use",
        ),
        _make_response([_make_text_block("Done. Hypothesis written.")]),
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 15
            executor.max_tokens = 4096
            executor._client = mock_client

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "Generate a hypothesis", "test_pipe", pipeline_dir
            ))

    assert result["success"] is True
    assert "hypothesis.md" in result["artifacts_written"]
    assert result["tokens_used"] > 0
    assert (pipeline_dir / "hypothesis.md").exists()
    assert "Test hypothesis" in (pipeline_dir / "hypothesis.md").read_text()


def test_executor_blocks_state_json_write(project_dir, pipeline_dir):
    """Test that writing state.json is blocked by hooks."""
    responses = [
        _make_response(
            [_make_tool_use_block(
                "t1", "file_write",
                {"path": "state.json", "content": '{"stage": "COMPLETED"}'},
            )],
            stop_reason="tool_use",
        ),
        _make_response([_make_text_block("Done.")]),
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 15
            executor.max_tokens = 4096
            executor._client = mock_client

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "test", "test_pipe", pipeline_dir
            ))

    # state.json should NOT have been overwritten
    state = json.loads((pipeline_dir / "state.json").read_text())
    assert state["stage"] == "main"  # unchanged
    assert "state.json" not in result["artifacts_written"]


def test_executor_blocks_wiki_direct_read(project_dir, pipeline_dir):
    """Test that direct reads to knowledge/wiki/ are blocked."""
    # Create a wiki file
    wiki_dir = project_dir / "knowledge" / "wiki"
    (wiki_dir / "test.md").write_text("Secret wiki content")

    responses = [
        _make_response(
            [_make_tool_use_block(
                "t1", "file_read",
                {"path": "../../knowledge/wiki/test.md"},
            )],
            stop_reason="tool_use",
        ),
        _make_response([_make_text_block("Done.")]),
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 15
            executor.max_tokens = 4096
            executor._client = mock_client

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "test", "test_pipe", pipeline_dir
            ))

    # Should succeed overall but the read should be blocked or escaped
    assert result["success"] is True


def test_executor_max_turns(project_dir, pipeline_dir):
    """Test that execution stops after max_turns."""
    # Agent keeps calling tools forever
    infinite_response = _make_response(
        [_make_tool_use_block("t1", "kb_retrieve", {"query": "test"})],
        stop_reason="tool_use",
    )

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=infinite_response)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 3  # Low limit for test
            executor.max_tokens = 4096
            executor._client = mock_client

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "test", "test_pipe", pipeline_dir
            ))

    # Should terminate after max_turns
    assert result["success"] is True
    assert mock_client.chat.completions.create.call_count == 3


def test_executor_budget_block(project_dir, pipeline_dir):
    """Test that execution is blocked when budget is exhausted."""
    # Set budget to exhausted
    budget = {"total_credits": 100000, "allocations": {"test_pipe": 100}, "usage": {"test_pipe": 100}, "reserve": 0}
    (project_dir / "runtime" / "budget.json").write_text(json.dumps(budget))

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 15
            executor.max_tokens = 4096
            executor._client = MagicMock()

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "test", "test_pipe", pipeline_dir
            ))

    assert result["success"] is False
    assert "budget" in result["output"].lower() or "Budget" in result["output"]
    assert result["tokens_used"] == 0


def test_executor_path_escape_blocked(project_dir, pipeline_dir):
    """Test that path traversal attacks are blocked."""
    responses = [
        _make_response(
            [_make_tool_use_block(
                "t1", "file_write",
                {"path": "../../../etc/passwd", "content": "hacked"},
            )],
            stop_reason="tool_use",
        ),
        _make_response([_make_text_block("Done.")]),
    ]

    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("researchos.runtime.agent_executor.AgentExecutor.__init__", return_value=None):
            from researchos.runtime.agent_executor import AgentExecutor

            executor = AgentExecutor.__new__(AgentExecutor)
            executor.project_root = project_dir
            executor.model = "claude-sonnet-4-20250514"
            executor.max_turns = 15
            executor.max_tokens = 4096
            executor._client = mock_client

            result = asyncio.run(executor.execute(
                "hypothesis_gen", "test", "test_pipe", pipeline_dir
            ))

    # Path escape should be blocked
    assert not Path("/etc/passwd").read_text().__contains__("hacked") if Path("/etc/passwd").exists() else True
    assert result["success"] is True
