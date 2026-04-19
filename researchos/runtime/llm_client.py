"""LLM client hardening utilities (A13).

Provides:
- `extract_json_between_markers`: robust JSON extraction from LLM output
  (mirrors SakanaAI/AI-Scientist/ai_scientist/llm.py::extract_json_between_markers)
- `strip_reasoning`: strip </think> reasoning preamble
- `retry_llm`: exponential backoff wrapper (uses `backoff` lib if available,
  else a local helper)
- `route_model`: model-prefix based client router
- `get_model_for_agent`: resolve agent role -> model id via
  workflow_config.yaml tier mapping (D' model tiering)
- `compute_artifact_fingerprint`: SHA256 digest prefix used as prompt-cache
  variant key so caches invalidate when artifacts change (A' caching)
- `build_cached_messages`: assemble a messages list whose stable prefix is
  eligible for provider-side prompt caching (OpenAI auto / Anthropic
  explicit `cache_control`)
- `LAUNCH_STAGGER_SECONDS` + `stagger_sleep`: startup-rate limiter
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

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


# ── Model tiering (D') ──────────────────────────────────────────

# Regex for `${VAR}` and `${VAR:-default}` substitution inside YAML values.
_ENV_SUBST_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in a string value.

    Mirrors the POSIX shell default-expansion semantics so that the
    ``workflow_config.yaml`` stays portable and human-editable:

        "${RESEARCHOS_MODEL_T1:-gpt-5.4}"  -> env override or default

    Unknown variables with no default resolve to the empty string, which
    allows callers to distinguish "unset" from "set-but-empty".
    """
    if not isinstance(value, str):
        return value

    def _repl(match: re.Match[str]) -> str:
        var_name, default = match.group(1), match.group(2)
        env_val = os.environ.get(var_name)
        if env_val is not None and env_val != "":
            return env_val
        return default if default is not None else ""

    return _ENV_SUBST_PATTERN.sub(_repl, value)


def _load_workflow_config_raw() -> dict[str, Any]:
    """Load and return the raw YAML of ``runtime/workflow_config.yaml``.

    Returns an empty dict if the file is missing or unreadable. Kept
    private so we don't create a hard dependency from the client side on
    :class:`researchos.runtime.workflow_loader.WorkflowConfig`, which only
    exposes step/stage fields today.
    """
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - optional dep
        logger.debug("PyYAML not installed; cannot load workflow_config.yaml")
        return {}

    # Walk up from this file to find the project root (contains `runtime/`).
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "runtime" / "workflow_config.yaml"
        if candidate.exists():
            try:
                return yaml.safe_load(candidate.read_text()) or {}
            except Exception:
                logger.warning(
                    "Failed to parse %s for agent_model_tiers", candidate,
                    exc_info=True,
                )
                return {}
    return {}


def get_model_for_agent(agent_name: str, config: dict | None = None) -> str:
    """Return the concrete model id that should serve ``agent_name``.

    Lookup order (first non-empty wins):

    1. ``config['agent_model_tiers'][agent_name]`` -> tier label ``Tn`` ->
       ``config['model_tiers'][Tn]`` (with ``${VAR:-default}`` expansion).
    2. ``os.environ['RESEARCHOS_MODEL']``.
    3. Empty string (caller is responsible for its own fallback).

    If ``config`` is ``None`` the workflow YAML is loaded lazily. Missing
    tier maps or unknown agents fall back transparently, so deployments
    that never adopt tiering continue to behave exactly as before.
    """
    if not agent_name:
        return os.environ.get("RESEARCHOS_MODEL", "")

    if config is None:
        config = _load_workflow_config_raw()

    tier_label: str | None = None
    agent_tiers = (config or {}).get("agent_model_tiers") or {}
    if isinstance(agent_tiers, dict):
        tier_label = agent_tiers.get(agent_name)

    model_tiers = (config or {}).get("model_tiers") or {}
    if (
        tier_label
        and isinstance(model_tiers, dict)
        and tier_label in model_tiers
    ):
        resolved = _substitute_env(str(model_tiers[tier_label]))
        if resolved:
            return resolved
        logger.debug(
            "Tier %s for agent %s resolved to empty; falling back to RESEARCHOS_MODEL",
            tier_label, agent_name,
        )

    # Backward-compatible fallback.
    return os.environ.get("RESEARCHOS_MODEL", "")


# ── Artifact fingerprint (A' cache-variant key) ─────────────────


def compute_artifact_fingerprint(path: str | Path) -> str:
    """Return a short SHA256 hex prefix of a file's contents.

    Used as a prompt-cache *variant key* so that when an artifact is
    rewritten (e.g., by ``fixer``), any cached prefix referencing the old
    bytes is invalidated automatically — callers embed this value into
    the cached prompt segment. Returns the literal string ``"missing"``
    when the file does not exist, preserving determinism for callers.
    """
    try:
        p = Path(path)
    except TypeError:
        return "missing"
    if not p.exists() or not p.is_file():
        return "missing"
    try:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return "missing"


# ── Prompt caching (A') ─────────────────────────────────────────


def _fingerprint_footer(artifact_paths: Iterable[str | Path] | None) -> str:
    """Build the trailing ``# ARTIFACT_FINGERPRINT: …`` line(s).

    The footer lives at the *end* of the cached prefix so that identical
    artifact contents produce identical bytes (cache hit), while any
    mutation flips the hash and forces a fresh cache entry.
    """
    if not artifact_paths:
        return ""
    parts: list[str] = []
    for p in artifact_paths:
        fp = compute_artifact_fingerprint(p)
        # Use basename when available so the footer is compact/human-readable.
        name = Path(p).name if p else "artifact"
        parts.append(f"# ARTIFACT_FINGERPRINT[{name}]: {fp}")
    return "\n".join(parts)


def build_cached_messages(
    system_prompt: str,
    *,
    evidence_refs: str | None = None,
    user_prompt: str = "",
    artifact_paths: Iterable[str | Path] | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Assemble a provider-specific messages list with a cache-friendly prefix.

    Layout of the cached prefix (top of the message list, in order):

        <system_prompt>
        <evidence_refs>          # optional, stable across calls
        # ARTIFACT_FINGERPRINT[…]: <sha16>   # invalidates on mutation

    For Anthropic models we tag the system block with
    ``cache_control={"type": "ephemeral"}`` so the prefix is explicitly
    cached. For OpenAI models the prefix is simply the first system
    message — OpenAI auto-caches identical prefixes ≥ 1024 tokens (pricing
    docs "Prompt Caching"), no extra flag required.
    """
    # Build the stable cached prefix.
    prefix_parts: list[str] = [system_prompt.rstrip()]
    if evidence_refs:
        prefix_parts.append(evidence_refs.rstrip())
    footer = _fingerprint_footer(artifact_paths)
    if footer:
        prefix_parts.append(footer)
    cached_prefix = "\n\n".join(p for p in prefix_parts if p)

    provider = route_model(model) if model else "openrouter"

    messages: list[dict[str, Any]] = []
    if provider == "anthropic":
        # Anthropic SDK accepts a list of content blocks in the system role;
        # `cache_control: ephemeral` on a block marks the cached breakpoint.
        messages.append(
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": cached_prefix,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        )
    else:
        # OpenAI / OpenRouter / Ollama: plain system message. OpenAI auto
        # caches identical prefixes server-side.
        messages.append({"role": "system", "content": cached_prefix})

    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})

    return messages
