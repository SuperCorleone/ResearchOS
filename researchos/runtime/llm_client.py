"""LLM client hardening utilities (A13).

Provides:
- `extract_json_between_markers`: robust JSON extraction from LLM output
  (mirrors SakanaAI/AI-Scientist/ai_scientist/llm.py::extract_json_between_markers)
- `strip_reasoning`: strip </think> reasoning preamble
- `retry_llm`: exponential backoff wrapper (uses `backoff` lib if available,
  else a local helper)
- `route_model`: model-prefix based client router
- `LAUNCH_STAGGER_SECONDS` + `stagger_sleep`: startup-rate limiter
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────

LAUNCH_STAGGER_SECONDS = 150


# ── JSON extraction (mirrors AI-Scientist verbatim, with explicit ──
# control-char stripping as requested) ─────────────────────────────


def extract_json_between_markers(text: str) -> dict | None:
    """Extract a JSON object from LLM output.

    Looks for a fenced ```json...``` block first; falls back to the first
    `{...}` span in the text. Control chars (\\x00-\\x1F, \\x7F) are stripped
    before parsing. Returns ``None`` on failure (never raises).
    """
    if text is None:
        return None

    # Regular expression pattern to find JSON content between ```json and ```
    json_pattern = r"```json(.*?)```"
    matches = re.findall(json_pattern, text, re.DOTALL)

    if not matches:
        # Fallback: Try to find any JSON-like content in the output
        json_pattern = r"\{.*?\}"
        matches = re.findall(json_pattern, text, re.DOTALL)

    for json_string in matches:
        json_string = json_string.strip()
        # Remove invalid control characters up front
        json_clean = re.sub(r"[\x00-\x1F\x7F]", "", json_string)
        try:
            return json.loads(json_clean)
        except json.JSONDecodeError:
            try:
                # Secondary attempt on the raw string
                return json.loads(json_string)
            except json.JSONDecodeError:
                continue  # Try next match

    return None


# ── Reasoning strip ─────────────────────────────────────────────


def strip_reasoning(text: str) -> str:
    """Strip a ``<think>...</think>`` reasoning preamble from LLM output.

    If ``</think>`` is present, returns everything after its *last*
    occurrence with leading whitespace stripped. Otherwise returns ``text``
    unchanged.
    """
    if text is None:
        return ""
    if "</think>" in text:
        return text.rsplit("</think>", 1)[-1].lstrip()
    return text


# ── Retry wrapper ───────────────────────────────────────────────

# Collect the exception types we consider retriable. Some of these are
# optional imports — we want the wrapper to "just work" even if the
# corresponding SDK isn't installed.
_RETRY_EXC: list[type[BaseException]] = []

try:  # openai >=1.0
    from openai import APITimeoutError, RateLimitError  # type: ignore

    _RETRY_EXC.extend([RateLimitError, APITimeoutError])
except Exception:  # pragma: no cover - optional dep
    class RateLimitError(Exception):  # type: ignore[no-redef]
        pass

    class APITimeoutError(Exception):  # type: ignore[no-redef]
        pass

try:  # anthropic SDK
    from anthropic import (  # type: ignore
        APITimeoutError as _AnthropicAPITimeoutError,
        RateLimitError as _AnthropicRateLimitError,
    )

    _RETRY_EXC.extend([_AnthropicRateLimitError, _AnthropicAPITimeoutError])
except Exception:  # pragma: no cover - optional dep
    pass

try:
    import requests  # type: ignore

    _RETRY_EXC.append(requests.HTTPError)
except Exception:  # pragma: no cover - optional dep
    class _StubHTTPError(Exception):
        pass

    class _StubRequestsModule:
        HTTPError = _StubHTTPError

    requests = _StubRequestsModule()  # type: ignore

RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = tuple(_RETRY_EXC) or (Exception,)


# Prefer the real `backoff` library when available so users get its
# jittered decorator behaviour; otherwise fall back to a local helper.
try:
    import backoff  # type: ignore

    _HAS_BACKOFF = True
except Exception:  # pragma: no cover - optional dep
    _HAS_BACKOFF = False


def _local_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_tries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    **kwargs: Any,
) -> Any:
    """Simple exponential-backoff retry with jitter.

    Retries on ``RETRY_EXCEPTIONS``; re-raises other exceptions immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_tries):
        try:
            return fn(*args, **kwargs)
        except RETRY_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == max_tries - 1:
                break
            delay = min(max_delay, base_delay * (2 ** attempt))
            delay += random.uniform(0, delay * 0.1)  # small jitter
            logger.warning(
                "LLM call failed (%s); retrying in %.1fs (attempt %d/%d)",
                type(exc).__name__, delay, attempt + 1, max_tries,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def retry_llm(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke ``fn(*args, **kwargs)`` with exponential-backoff retry.

    Uses the ``backoff`` library if installed (decorator form mirrors:

        @backoff.on_exception(backoff.expo, RETRY_EXCEPTIONS, max_tries=5)

    ), else falls back to :func:`_local_retry`.
    """
    if _HAS_BACKOFF:
        decorated = backoff.on_exception(  # type: ignore[attr-defined]
            backoff.expo,
            RETRY_EXCEPTIONS,
            max_tries=5,
        )(fn)
        return decorated(*args, **kwargs)
    return _local_retry(fn, *args, **kwargs)


# ── Model router ────────────────────────────────────────────────


def route_model(model: str) -> str:
    """Classify a model id by prefix and return the client type.

    Returns one of: ``"anthropic"``, ``"openai"``, ``"openrouter"``,
    ``"ollama"``.
    """
    if not model:
        return "openrouter"
    m = model.strip().lower()

    # Local / ollama runners
    if m.startswith("ollama/") or m.startswith("local/") or m.startswith("ollama:"):
        return "ollama"

    # Anthropic
    if m.startswith("claude-") or m.startswith("anthropic/"):
        return "anthropic"

    # OpenAI
    if (
        m.startswith("gpt-")
        or m.startswith("o1-")
        or m.startswith("o3-")
        or m.startswith("openai/")
        or m in {"o1", "o3"}
    ):
        return "openai"

    # Fall through
    return "openrouter"


# ── Launch stagger ──────────────────────────────────────────────


def stagger_sleep(i: int) -> None:
    """Sleep ``i * LAUNCH_STAGGER_SECONDS`` seconds to stagger concurrent
    LLM-backed pipeline launches.

    Useful when kicking off many pipelines at once so that providers don't
    rate-limit the whole batch on the first second.
    """
    if i <= 0:
        return
    delay = i * LAUNCH_STAGGER_SECONDS
    logger.info("Stagger sleep: waiting %ds before launch #%d", delay, i)
    time.sleep(delay)
