"""Universal LLM API client via LiteLLM.

Supports Anthropic, OpenAI, DeepSeek, and any LiteLLM-supported provider
through a single unified interface. Drop-in replacement for the previous
Anthropic-only client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from openharness.api.errors import (
    AuthenticationFailure,
    OpenHarnessApiError,
    RateLimitFailure,
    RequestFailure,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0


@dataclass(frozen=True)
class ApiMessageRequest:
    """Input parameters for a model invocation."""

    model: str
    messages: list[ConversationMessage]
    system_prompt: str | None = None
    max_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """Incremental text produced by the model."""

    text: str


@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """Terminal event containing the full assistant message."""

    message: ConversationMessage
    usage: UsageSnapshot
    stop_reason: str | None = None


ApiStreamEvent = ApiTextDeltaEvent | ApiMessageCompleteEvent


class SupportsStreamingMessages(Protocol):
    """Protocol used by the query engine."""

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Yield streamed events for the request."""


def _convert_tools_for_litellm(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool definitions to OpenAI function-calling format."""
    result = []
    for t in tools:
        if "input_schema" in t:
            result.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            })
        elif "function" in t:
            result.append(t)
        else:
            result.append(t)
    return result


def _convert_messages_for_litellm(
    messages: list[ConversationMessage],
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Convert ConversationMessage list to LiteLLM/OpenAI message format."""
    result: list[dict[str, Any]] = []

    if system_prompt:
        result.append({"role": "system", "content": system_prompt})

    for msg in messages:
        api_param = msg.to_api_param()
        role = api_param.get("role", "user")

        if role == "assistant":
            content_blocks = api_param.get("content", [])
            text_parts = []
            tool_calls = []

            if isinstance(content_blocks, str):
                result.append({"role": "assistant", "content": content_blocks})
                continue

            for block in content_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })

            msg_dict: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                msg_dict["content"] = "\n".join(text_parts)
            else:
                msg_dict["content"] = None
            if tool_calls:
                msg_dict["tool_calls"] = tool_calls
            result.append(msg_dict)

        elif role == "user":
            content = api_param.get("content", "")
            if isinstance(content, list):
                # Check for tool_result blocks
                tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": tr.get("content", ""),
                        })
                else:
                    text = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in content
                    )
                    result.append({"role": "user", "content": text})
            else:
                result.append({"role": "user", "content": str(content)})
        else:
            result.append(api_param)

    return result


class AnthropicApiClient:
    """Universal LLM client powered by LiteLLM.

    Despite the class name (kept for backward compatibility), this works
    with any LiteLLM-supported provider: Anthropic, OpenAI, DeepSeek, etc.
    """

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url

        # Suppress LiteLLM debug noise
        import litellm
        litellm.suppress_debug_info = True

        # Set env vars for LiteLLM provider detection
        if base_url and "deepseek" in base_url:
            os.environ.setdefault("DEEPSEEK_API_KEY", api_key)
        elif base_url and "openai" in base_url:
            os.environ.setdefault("OPENAI_API_KEY", api_key)
        else:
            os.environ.setdefault("ANTHROPIC_API_KEY", api_key)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Yield text deltas and final message with retry."""
        import litellm

        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async for event in self._stream_once(request):
                    yield event
                return
            except (AuthenticationFailure, RateLimitFailure):
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= MAX_RETRIES:
                    raise RequestFailure(str(exc)) from exc
                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY) + random.uniform(0, 1)
                log.warning("API error (attempt %d/%d), retrying in %.1fs: %s",
                            attempt + 1, MAX_RETRIES + 1, delay, exc)
                await asyncio.sleep(delay)

        if last_error:
            raise RequestFailure(str(last_error)) from last_error

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """Single streaming attempt via LiteLLM."""
        import litellm

        # Determine model prefix for LiteLLM
        model = request.model
        if self._base_url and "deepseek" in self._base_url and not model.startswith("deepseek/"):
            model = f"deepseek/{model}"

        messages = _convert_messages_for_litellm(request.messages, request.system_prompt)

        # Clamp max_tokens for providers with limits (e.g. DeepSeek max 8192)
        max_tokens = request.max_tokens
        if self._base_url and "deepseek" in self._base_url:
            max_tokens = min(max_tokens, 8192)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "api_key": self._api_key,
        }
        if self._base_url:
            kwargs["api_base"] = self._base_url
        if request.tools:
            kwargs["tools"] = _convert_tools_for_litellm(request.tools)

        try:
            response = await litellm.acompletion(**kwargs)
        except Exception as e:
            error_msg = str(e)
            if "401" in error_msg or "auth" in error_msg.lower():
                raise AuthenticationFailure(error_msg)
            if "429" in error_msg or "rate" in error_msg.lower():
                raise RateLimitFailure(error_msg)
            raise RequestFailure(error_msg) from e

        full_text = ""
        tool_calls_acc: list[dict[str, Any]] = []
        total_input = 0
        total_output = 0
        finish_reason = None

        async for chunk in response:
            if not chunk.choices:
                if chunk.usage:
                    total_input = chunk.usage.prompt_tokens or 0
                    total_output = chunk.usage.completion_tokens or 0
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            finish_reason = choice.finish_reason or finish_reason

            if delta and getattr(delta, "content", None):
                full_text += delta.content
                yield ApiTextDeltaEvent(text=delta.content)

            if delta and getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index if hasattr(tc, "index") else 0
                    while len(tool_calls_acc) <= idx:
                        tool_calls_acc.append({"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        tool_calls_acc[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_acc[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments

        # Build final ConversationMessage
        content_blocks = []
        if full_text:
            content_blocks.append(TextBlock(text=full_text))

        for tc in tool_calls_acc:
            try:
                args = json.loads(tc["arguments"])
            except json.JSONDecodeError:
                args = {}
            content_blocks.append(ToolUseBlock(
                id=tc["id"],
                name=tc["name"],
                input=args,
            ))

        stop = finish_reason or "end_turn"
        if tool_calls_acc:
            stop = "tool_use"

        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=content_blocks),
            usage=UsageSnapshot(input_tokens=total_input, output_tokens=total_output),
            stop_reason=stop,
        )
