"""Platform-agnostic tool implementations used by SDKRuntime.

These are the default implementations of the tool names that agents
reference in their markdown definitions. When ResearchOS runs inside a
host that already provides equivalents (OpenHarness, Claude Skill),
the adapter's runtime supplies the host versions instead — see
``AgentRuntime.supports_tool``.

Organized by category:

io        — file_read, file_write
kb        — kb_retrieve (delegates to researchos.mcp_servers.kb_server)
exec      — execute_python, fetch_dataset
inspect   — gen_code_tree_structure, grep_code, list_file_symbols  (NEW)
net       — (reserved for future web tools)

The 3 "inspect" tools (gen_code_tree_structure, grep_code,
list_file_symbols) are what the redesigned judge agent uses to verify
multi-file experiment implementations without loading entire files into
its context window.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_MAX_TREE_ENTRIES_PER_DIR = 50
_MAX_GREP_MATCHES = 200
_LONG_FILE_THRESHOLD = 200               # lines — above which judge should
                                         # read by offset, never full file


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileSymbol:
    """Top-level def/class in a Python file."""

    name: str
    kind: str                            # "function" | "class" | "async_function"
    lineno: int
    end_lineno: int | None


@dataclass(frozen=True)
class CodeMatch:
    """Match from grep_code."""

    path: Path                           # absolute path
    lineno: int
    snippet: str                         # <= 120 chars


# ---------------------------------------------------------------------------
# 1. gen_code_tree_structure
# ---------------------------------------------------------------------------

def gen_code_tree_structure(
    path: str | Path,
    max_depth: int = 4,
    include_extensions: tuple[str, ...] = (".py", ".yaml", ".json", ".md"),
) -> str:
    """Return a tree-style view of a directory with per-file line counts.

    Used by the judge agent as its FIRST action to discover the shape of
    the experiment code without assuming any specific entry-file name.

    Caps output at ~200 entries total to protect context budget.
    """

    root = Path(path).resolve()
    if not root.exists():
        return f"[error] path does not exist: {root}"
    if not root.is_dir():
        return f"[error] path is not a directory: {root}"

    lines: list[str] = [f"{root.name}/"]
    _total = [0]                         # mutable counter across recursion

    def _walk(d: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            lines.append(prefix + "... (max depth reached)")
            return
        if _total[0] >= 200:
            return
        try:
            entries = sorted(
                d.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except PermissionError:
            lines.append(prefix + "[permission denied]")
            return

        # Filter out noise
        entries = [
            e for e in entries
            if not e.name.startswith((".", "__"))
            and e.name not in {"node_modules", "venv", ".venv", "target"}
        ]
        if len(entries) > _MAX_TREE_ENTRIES_PER_DIR:
            omitted = len(entries) - _MAX_TREE_ENTRIES_PER_DIR
            entries = entries[:_MAX_TREE_ENTRIES_PER_DIR]
            lines.append(
                prefix + f"... [+{omitted} entries omitted]"
            )

        for i, entry in enumerate(entries):
            if _total[0] >= 200:
                lines.append(prefix + "... [truncated]")
                return
            _total[0] += 1
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")

            if entry.is_dir():
                lines.append(prefix + connector + entry.name + "/")
                _walk(entry, next_prefix, depth + 1)
            else:
                if entry.suffix.lower() not in include_extensions:
                    continue
                try:
                    n_lines = sum(1 for _ in entry.open("r", errors="replace"))
                except OSError:
                    n_lines = -1
                lines.append(
                    prefix + connector + f"{entry.name}  ({n_lines} lines)"
                )

    _walk(root, "", 1)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. grep_code
# ---------------------------------------------------------------------------

def grep_code(
    pattern: str,
    path: str | Path,
    ext: str = ".py",
    case_sensitive: bool = True,
) -> list[CodeMatch]:
    """Regex-search ``path`` recursively for ``pattern`` in files with ``ext``.

    Used by the judge agent to verify that metric names / concept IDs /
    function references claimed in the digest actually appear in the code.

    Returns up to 200 matches. Does NOT load whole files into memory —
    streams line by line.
    """

    root = Path(path).resolve()
    if not root.exists():
        return []

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        logger.warning("grep_code: invalid pattern %r (%s)", pattern, e)
        return []

    results: list[CodeMatch] = []
    files: Iterator[Path]
    if root.is_file():
        files = iter([root])
    else:
        files = root.rglob(f"*{ext}")

    for f in files:
        if len(results) >= _MAX_GREP_MATCHES:
            break
        try:
            for lineno, raw in enumerate(
                f.open("r", errors="replace"), start=1
            ):
                if regex.search(raw):
                    snippet = raw.rstrip("\n")
                    if len(snippet) > 120:
                        snippet = snippet[:117] + "..."
                    results.append(
                        CodeMatch(path=f.resolve(), lineno=lineno, snippet=snippet)
                    )
                    if len(results) >= _MAX_GREP_MATCHES:
                        break
        except OSError:
            continue

    return results


# ---------------------------------------------------------------------------
# 3. list_file_symbols
# ---------------------------------------------------------------------------

def list_file_symbols(path: str | Path) -> list[FileSymbol]:
    """Return all top-level def/class in a Python file via AST parsing.

    This is the judge's cheap way to verify that
    ``concept.code_implementation = 'file.py::func_name'`` actually points
    at a real symbol, without loading the file contents into the LLM prompt.

    Returns [] for non-Python files, missing files, or syntax errors
    (AST parse failure is a SIGNAL to judge: if the file doesn't parse,
    the smoke test will fail too, so the failure is surfaced elsewhere).
    """

    p = Path(path).resolve()
    if not p.exists() or not p.is_file() or p.suffix != ".py":
        return []

    try:
        source = p.read_text(errors="replace")
        tree = ast.parse(source, filename=str(p))
    except (OSError, SyntaxError):
        return []

    symbols: list[FileSymbol] = []
    for node in tree.body:
        kind: str | None = None
        if isinstance(node, ast.FunctionDef):
            kind = "function"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async_function"
        elif isinstance(node, ast.ClassDef):
            kind = "class"
        if kind is None:
            continue
        symbols.append(
            FileSymbol(
                name=node.name,
                kind=kind,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", None),
            )
        )
    return symbols


# ---------------------------------------------------------------------------
# Export: ToolSpec descriptors for registration with AgentRuntime
# ---------------------------------------------------------------------------

def inspect_tool_specs() -> list[dict]:
    """Return JSON-schema ToolSpec dicts for the 3 inspect tools.

    SDKRuntime (and any runtime that wants to expose these) imports and
    maps these through its own ToolSpec type. Kept as dict here to avoid
    a circular import with base.py.
    """
    return [
        {
            "name": "gen_code_tree_structure",
            "description": (
                "Tree-style directory listing with per-file line counts. "
                "Use as the FIRST action when inspecting an experiment's "
                "code — do not assume a single entry-file name."
            ),
            "json_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 4},
                },
                "required": ["path"],
            },
            "category": "inspect",
        },
        {
            "name": "grep_code",
            "description": (
                "Regex-search files recursively under a directory. "
                "Returns up to 200 (path, lineno, snippet) matches. "
                "Use to verify metric names / function references appear "
                "in the actual code."
            ),
            "json_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "ext": {"type": "string", "default": ".py"},
                    "case_sensitive": {"type": "boolean", "default": True},
                },
                "required": ["pattern", "path"],
            },
            "category": "inspect",
        },
        {
            "name": "list_file_symbols",
            "description": (
                "Return all top-level def/class in a Python file via AST. "
                "Cheap way to verify 'file.py::func_name' locators without "
                "loading the full file into context."
            ),
            "json_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
            "category": "inspect",
        },
    ]
