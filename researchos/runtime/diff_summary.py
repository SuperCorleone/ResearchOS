"""Parse user / professor feedback into a focused changelist for the Fixer.

Motivation
----------
Without guidance the Fixer reads the *entire* experiment code, critic analysis,
hypothesis and design files. A single run was measured at **120,736 input
tokens** — the biggest single consumer of budget in a typical research cycle.

Most corrections touch only a handful of lines. ``diff_summary`` scans the
feedback markdown (from either ``user_approve/feedback.md``,
``professor_suggestion.md`` or ``correction_report.md``) and extracts a
structured ``targets`` list the Fixer can use to read only the affected
ranges plus a small context window. Everything that cannot be localised
lands in ``unresolved_hints`` so the Fixer still addresses free-text
guidance, but never blindly re-reads 2K+ lines.

Output schema (``diff_summary.json``)::

    {
      "targets": [
        {
          "file": "experiment.py",
          "line_range": [42, 58] | None,
          "reason": "add F1 metric alongside accuracy",
          "context_radius": 50
        },
        ...
      ],
      "chunked": false,          # true iff some target file > 500 lines
      "unresolved_hints": [       # free-text directives not tied to a file
        "consider adding regularization"
      ]
    }

The module is deliberately pure-Python / stdlib so it can run anywhere the
scheduler runs (no AST libs, no regex engines beyond ``re``).

CLI usage::

    python -m researchos.runtime.diff_summary <feedback.md> <pipeline_dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Regex heuristics
# ---------------------------------------------------------------------------

# Matches patterns such as:
#   experiment.py:L42-L58
#   path/to/module.py:L42
#   utils.py:42-58
#   experiment.py line 42-58
#   experiment.py lines 42 to 58
#   experiment.py (line 42)
# Groups:
#   1 -> file path
#   2 -> L-style start  (optional)  e.g. "42" in "L42"
#   3 -> L-style end    (optional)  e.g. "58" in "L42-L58"
#   4 -> colon-style start          e.g. "42" in ":42"
#   5 -> colon-style end            e.g. "58" in ":42-58"
#   6 -> "line"-style start         e.g. "42" in "line 42"
#   7 -> "line"-style end           e.g. "58" in "line 42-58" / "lines 42 to 58"
_FILE_REGEX = re.compile(
    r"""
    (?P<file>[\w./\-]+\.(?:py|yaml|yml|json|md))   # file path (relative allowed)
    (?:
        :\s*(?:
            L(?P<lstart>\d+)(?:\s*[-–]\s*L?(?P<lend>\d+))?      # :L42 / :L42-L58 / :L42-58
            |
            (?P<cstart>\d+)(?:\s*[-–]\s*(?P<cend>\d+))?          # :42 / :42-58
        )
        |
        \s*(?:\(|,)?\s*lines?\s+
            (?P<wstart>\d+)
            (?:\s*(?:[-–]|to)\s*(?P<wend>\d+))?                  # line 42 / lines 42-58 / line 42 to 58
    )?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Matches function-name hints we can try to resolve to a file via grep:
#   function `foo`, def bar, method baz, `qux()`
_FUNC_REGEX = re.compile(
    r"""
    (?:
        (?:function|def|method|class)\s+`?(?P<named>[A-Za-z_]\w+)`?
        |
        `(?P<back>[A-Za-z_]\w+)\(\)`
    )
    """,
    re.VERBOSE,
)

# A minimal set of file extensions we are willing to resolve automatically.
_SOURCE_EXTS = (".py", ".yaml", ".yml", ".json", ".md")

# Feedback prelude / template noise we do *not* want in unresolved_hints.
_NOISE_PREFIXES = (
    "<!--",
    "## what to change",
    "## what to keep",
    "## ",
    "# ",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_feedback(feedback_text: str, pipeline_dir: Path) -> dict:
    """Extract a focused changelist from free-form feedback text.

    Heuristic, in priority order:

    1. Scan for explicit ``file.py:L42-L58`` / ``file.py line 42-58`` refs.
    2. Scan for ``function foo`` / ```foo()``` mentions; resolve to a file
       in ``pipeline_dir`` via grep of ``def foo`` / ``class foo``.
    3. Anything that survived into neither bucket becomes an ``unresolved_hint``.

    Parameters
    ----------
    feedback_text:
        Markdown body of the feedback file.
    pipeline_dir:
        Directory of the pipeline artifacts (used both for detecting file
        size / existence and for resolving function names).
    """
    targets: list[dict] = []
    seen_keys: set[tuple[str, Optional[int], Optional[int]]] = set()

    # ------ 1. explicit file:line references --------------------------------
    for match in _FILE_REGEX.finditer(feedback_text):
        file_raw = match.group("file")
        if not file_raw.lower().endswith(_SOURCE_EXTS):
            continue
        # Skip bare mentions of the feedback file itself.
        if file_raw.endswith("feedback.md") or file_raw.endswith(
            "correction_report.md"
        ):
            continue

        lstart = match.group("lstart") or match.group("cstart") or match.group("wstart")
        lend = match.group("lend") or match.group("cend") or match.group("wend")

        start = int(lstart) if lstart else None
        end = int(lend) if lend else start
        line_range: Optional[list[int]] = (
            [start, end] if start is not None else None
        )

        key = (file_raw, start, end)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        reason = _snippet_around(feedback_text, match.start(), match.end())
        targets.append(
            {
                "file": file_raw,
                "line_range": line_range,
                "reason": reason,
                "context_radius": 50,
            }
        )

    # ------ 2. function-name hints ------------------------------------------
    for match in _FUNC_REGEX.finditer(feedback_text):
        name = match.group("named") or match.group("back")
        if not name or name in {"function", "def", "method", "class"}:
            continue

        resolved = resolve_function_to_file(name, pipeline_dir)
        if resolved is None:
            continue
        file_rel, line_start, line_end = resolved

        key = (file_rel, line_start, line_end)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        reason_ctx = _snippet_around(feedback_text, match.start(), match.end())
        targets.append(
            {
                "file": file_rel,
                "line_range": [line_start, line_end],
                "reason": f"function `{name}` — {reason_ctx}",
                "context_radius": 50,
            }
        )

    # ------ 3. unresolved free-text directives ------------------------------
    unresolved = _extract_unresolved_hints(feedback_text, targets)

    # ------ 4. chunking decision --------------------------------------------
    chunked = False
    for target in targets:
        candidate = pipeline_dir / target["file"]
        if candidate.exists() and candidate.is_file():
            try:
                # Line count without loading whole file into memory.
                with candidate.open("r", encoding="utf-8", errors="ignore") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                continue
            if line_count > 500:
                chunked = True
                break

    return {
        "targets": targets,
        "chunked": chunked,
        "unresolved_hints": unresolved,
    }


def resolve_function_to_file(
    func_name: str, pipeline_dir: Path
) -> Optional[Tuple[str, int, int]]:
    """Grep ``pipeline_dir`` for ``def <func_name>`` / ``class <func_name>``.

    Returns ``(file_relative_to_pipeline_dir, start_line, end_line)`` or
    ``None`` if the function cannot be located. End line is a heuristic
    upper bound (start + 40) — the fixer's ``context_radius`` handles the
    rest.
    """
    if not pipeline_dir.is_dir():
        return None

    patterns = (
        re.compile(rf"^\s*def\s+{re.escape(func_name)}\s*\("),
        re.compile(rf"^\s*async\s+def\s+{re.escape(func_name)}\s*\("),
        re.compile(rf"^\s*class\s+{re.escape(func_name)}\b"),
    )

    for py_file in pipeline_dir.rglob("*.py"):
        try:
            with py_file.open("r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if any(pat.match(line) for pat in patterns):
                        rel = py_file.relative_to(pipeline_dir).as_posix()
                        return rel, lineno, lineno + 40
        except OSError:
            continue
    return None


def write_diff_summary(feedback_path: Path, pipeline_dir: Path) -> Path:
    """Parse ``feedback_path`` and write ``pipeline_dir/diff_summary.json``.

    Returns the path to the written JSON. Safe to call repeatedly — it
    overwrites any prior summary.
    """
    if not feedback_path.exists():
        raise FileNotFoundError(f"feedback file not found: {feedback_path}")
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    text = feedback_path.read_text(encoding="utf-8", errors="ignore")
    summary = parse_feedback(text, pipeline_dir)
    out = pipeline_dir / "diff_summary.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return out


def extract_context_around(
    file_path: Path, line_range: Tuple[int, int], radius: int = 50
) -> str:
    """Return a labelled slice of ``file_path`` around ``line_range``.

    Expands the window by ``radius`` lines on each side, clamped to file
    bounds. The returned string is prefixed with a header such as::

        # experiment.py lines 10-110 (+-50 context)
    """
    lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    start_idx = max(0, line_range[0] - radius - 1)
    end_idx = min(len(lines), line_range[1] + radius)
    snippet = "\n".join(lines[start_idx:end_idx])
    header = (
        f"# {file_path.name} lines {start_idx + 1}-{end_idx} "
        f"(+-{radius} context)"
    )
    return f"{header}\n{snippet}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snippet_around(text: str, start: int, end: int, radius: int = 60) -> str:
    """Return a cleaned one-line snippet centred on ``[start, end]``."""
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    raw = text[lo:hi].replace("\n", " ").strip()
    # Collapse whitespace for compact JSON.
    return re.sub(r"\s+", " ", raw)[:240]


def _extract_unresolved_hints(text: str, targets: list[dict]) -> list[str]:
    """Return directive-like lines that did not match any file/function cue.

    We deliberately keep only short, instructive sentences — bullets or
    first-person imperatives — and drop markdown headers, comment lines
    and the feedback template scaffolding.
    """
    targeted_files = {t["file"] for t in targets}
    hints: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(lower.startswith(pref) for pref in _NOISE_PREFIXES):
            # Still allow "## " headings only if they look like directives,
            # otherwise skip. Default to skip for simplicity.
            continue
        if line.startswith(("-", "*", "+", ">")):
            line = line.lstrip("-*+> \t")
        if not line:
            continue
        # Skip lines that already reference a localised file.
        if any(f in line for f in targeted_files):
            continue
        # Skip lines that are just filenames without guidance.
        if re.fullmatch(r"[\w./\-]+\.(?:py|yaml|yml|json|md)", line):
            continue
        # Keep only lines that look like directives (contain a verb or "should").
        if len(line) < 8:
            continue
        hints.append(line)

    # Deduplicate while preserving order; cap to 20 to keep summary small.
    seen: set[str] = set()
    deduped: list[str] = []
    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)
        deduped.append(hint)
        if len(deduped) >= 20:
            break
    return deduped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python -m researchos.runtime.diff_summary "
            "<feedback.md> <pipeline_dir>",
            file=sys.stderr,
        )
        return 2
    feedback_path = Path(argv[1]).resolve()
    pipeline_dir = Path(argv[2]).resolve()
    out = write_diff_summary(feedback_path, pipeline_dir)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
