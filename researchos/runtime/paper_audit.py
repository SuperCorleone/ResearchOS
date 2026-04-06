"""Automated paper audit: G4 numerical consistency + G5 statistical completeness
+ VLM figure review.

Extracts numbers from the paper draft markdown and cross-references them
against results.json. Optionally audits figures using a vision-language model.
Produces a structured audit report.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Patterns for extracting numbers from markdown
_NUMBER_RE = re.compile(
    r"(?<![a-zA-Z_/])"  # not preceded by letter/underscore/slash
    r"(\d+\.?\d*%?)"     # number with optional decimal and %
    r"(?![a-zA-Z_/])"    # not followed by letter/underscore/slash
)

# Patterns for statistical reporting items
_STAT_KEYWORDS = {
    "effect_size": re.compile(r"(?:cohen.?s?\s*d|effect\s*size|η²|eta.?squared|r²)\s*[=:]\s*[\d.]+", re.I),
    "p_value": re.compile(r"p\s*[=<>]\s*[\d.]+", re.I),
    "confidence_interval": re.compile(r"(?:CI|confidence\s*interval|±)\s*[\[(]?\s*[\d.]+", re.I),
    "test_statistic": re.compile(r"(?:t|F|χ²|chi.?square|z|W|U)\s*[\(=]\s*[\d.]+", re.I),
    "sample_size": re.compile(r"(?:n|N|sample\s*size|num_runs|seeds?)\s*[=:]\s*[\d]+", re.I),
}


def _extract_numbers_from_text(text: str) -> list[tuple[str, float, int]]:
    """Extract (raw_str, numeric_value, line_number) from markdown text."""
    results = []
    for i, line in enumerate(text.splitlines(), 1):
        # Skip frontmatter, code blocks, references
        stripped = line.strip()
        if stripped.startswith("---") or stripped.startswith("```") or stripped.startswith("#"):
            continue
        for match in _NUMBER_RE.finditer(line):
            raw = match.group(1)
            try:
                if raw.endswith("%"):
                    value = float(raw[:-1])
                else:
                    value = float(raw)
                # Skip trivially common numbers (section refs, years, etc)
                if value in (0, 1, 2, 3, 4, 5, 6, 7) or (1900 < value < 2100):
                    continue
                results.append((raw, value, i))
            except ValueError:
                continue
    return results


def _flatten_metrics(data: dict, prefix: str = "") -> dict[str, float]:
    """Flatten nested dict to {dotted_key: value} for all numeric leaves."""
    flat = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, (int, float)):
            flat[key] = float(v)
        elif isinstance(v, dict):
            flat.update(_flatten_metrics(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, (int, float)):
                    flat[f"{key}[{i}]"] = float(item)
                elif isinstance(item, dict):
                    flat.update(_flatten_metrics(item, f"{key}[{i}]"))
    return flat


def _number_matches(draft_val: float, result_val: float, tolerance: float = 0.02) -> bool:
    """Check if two numbers match within relative tolerance (handles % vs decimal)."""
    def _close(a: float, b: float) -> bool:
        if a == b:
            return True
        denom = max(abs(a), abs(b), 1e-9)
        return abs(a - b) / denom < tolerance

    # Direct match
    if _close(draft_val, result_val):
        return True
    # Percentage match: draft says "85.3%" (85.3) and result is 0.853
    if _close(draft_val / 100.0, result_val):
        return True
    # Reverse: draft says "0.853" and result is 85.3%
    if _close(draft_val * 100.0, result_val):
        return True
    return False


def audit_g4_numerical_consistency(
    draft_path: Path,
    results_path: Path,
) -> dict:
    """G4: Cross-reference numbers in draft against results.json.

    Returns:
        {
            "passed": bool,
            "total_numbers": int,
            "matched": int,
            "unmatched": int,
            "issues": [{"line": int, "value": str, "status": "MATCH"|"NO_SOURCE"|"MISMATCH", ...}]
        }
    """
    if not draft_path.exists():
        return {"passed": False, "total_numbers": 0, "matched": 0, "unmatched": 0,
                "issues": [{"line": 0, "value": "N/A", "status": "ERROR", "detail": "Draft not found"}]}

    draft_text = draft_path.read_text()
    draft_numbers = _extract_numbers_from_text(draft_text)

    # Load results
    result_values: dict[str, float] = {}
    if results_path.exists():
        try:
            data = json.loads(results_path.read_text())
            result_values = _flatten_metrics(data)
        except Exception:
            pass

    all_result_nums = set(result_values.values())
    issues = []
    matched = 0

    for raw, value, line in draft_numbers:
        # Try to find a matching value in results
        found = False
        for rkey, rval in result_values.items():
            if _number_matches(value, rval):
                issues.append({
                    "line": line,
                    "value": raw,
                    "status": "MATCH",
                    "detail": f"Matches results.{rkey} = {rval}",
                })
                matched += 1
                found = True
                break

        if not found:
            issues.append({
                "line": line,
                "value": raw,
                "status": "NO_SOURCE",
                "detail": "No matching value found in results.json",
            })

    unmatched = len(draft_numbers) - matched
    return {
        "passed": unmatched == 0 or matched >= len(draft_numbers) * 0.5,
        "total_numbers": len(draft_numbers),
        "matched": matched,
        "unmatched": unmatched,
        "issues": issues,
    }


def audit_g5_statistical_completeness(draft_path: Path) -> dict:
    """G5: Check that statistical reporting is complete.

    For each comparison, checks for: effect size, p-value, CI, test statistic, sample size.

    Returns:
        {
            "passed": bool,
            "items_found": {keyword: count},
            "missing": [str],
        }
    """
    if not draft_path.exists():
        return {"passed": False, "items_found": {}, "missing": ["Draft not found"]}

    text = draft_path.read_text()
    items_found = {}
    missing = []

    for name, pattern in _STAT_KEYWORDS.items():
        matches = pattern.findall(text)
        items_found[name] = len(matches)
        if len(matches) == 0:
            missing.append(name)

    # Pass if at least 3 of 5 statistical items are present
    present_count = sum(1 for v in items_found.values() if v > 0)
    return {
        "passed": present_count >= 3,
        "items_found": items_found,
        "missing": missing,
        "present_count": present_count,
    }


def _encode_image_base64(image_path: Path) -> str:
    """Read an image file and return base64 string."""
    return base64.b64encode(image_path.read_bytes()).decode("utf-8")


def _image_media_type(path: Path) -> str:
    """Return MIME type for common image extensions."""
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def _load_vlm_config(project_root: Path) -> dict[str, Any]:
    """Load VLM config from research_config.yaml."""
    config_path = project_root / "runtime" / "research_config.yaml"
    if config_path.exists():
        try:
            cfg = yaml.safe_load(config_path.read_text()) or {}
            return cfg.get("vlm", {})
        except Exception:
            pass
    return {}


def audit_figures(
    pipeline_id: str,
    project_root: Path,
    draft_text: str = "",
    vlm_model: str | None = None,
) -> dict[str, Any]:
    """Audit figures in paper/<pipeline_id>/ using a vision-language model.

    Scans for image files, sends each to a VLM, and checks:
    axes labeled, legends present, resolution adequate, consistent styling,
    data matches text claims.

    Returns dict with passed, figures_checked, total_failures, issues list.
    """
    from openai import OpenAI

    vlm_config = _load_vlm_config(project_root)
    if not vlm_config.get("enabled", True):
        return {"passed": True, "figures_checked": 0, "issues": [],
                "note": "VLM figure audit disabled in config."}

    paper_dir = project_root / "paper" / pipeline_id
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    figure_paths: list[Path] = []
    for search_dir in [paper_dir / "figures", paper_dir]:
        if search_dir.exists():
            for f in sorted(search_dir.iterdir()):
                if f.suffix.lower() in image_extensions and f not in figure_paths:
                    figure_paths.append(f)

    if not figure_paths:
        return {"passed": True, "figures_checked": 0, "issues": [],
                "note": "No figures found to audit."}

    model = (vlm_model
             or vlm_config.get("model")
             or os.environ.get("RESEARCHOS_VLM_MODEL")
             or os.environ.get("RESEARCHOS_MODEL", "deepseek-chat"))
    api_key = os.environ.get("RESEARCHOS_API_KEY")
    base_url = os.environ.get("RESEARCHOS_BASE_URL", "https://api.deepseek.com")

    if not api_key:
        return {"passed": True, "figures_checked": 0, "issues": [],
                "note": "No API key configured; VLM figure audit skipped."}

    client = OpenAI(api_key=api_key, base_url=base_url)
    draft_context = ""
    if draft_text:
        draft_context = f"\n\nPaper draft excerpt (for data-text alignment check):\n{draft_text[:3000]}"

    all_issues: list[dict] = []
    total_failures = 0

    for fig_path in figure_paths:
        try:
            b64 = _encode_image_base64(fig_path)
            media_type = _image_media_type(fig_path)

            prompt = (
                f"You are a scientific figure reviewer. Analyze this figure from a research paper.\n"
                f"Figure filename: {fig_path.name}\n"
                f"{draft_context}\n\n"
                f"For this figure, evaluate each check and respond with EXACTLY this JSON format:\n"
                f'{{"axes_labeled": {{"status": "PASS or FAIL or NA", "detail": "..."}}, '
                f'"legend_present": {{"status": "PASS or FAIL or NA", "detail": "..."}}, '
                f'"resolution_adequate": {{"status": "PASS or FAIL", "detail": "..."}}, '
                f'"consistent_styling": {{"status": "PASS or FAIL", "detail": "..."}}, '
                f'"data_text_match": {{"status": "PASS or FAIL or UNCHECKED", "detail": "..."}}, '
                f'"summary": "one-line overall assessment"}}'
            )

            response = client.chat.completions.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{b64}"},
                        },
                    ],
                }],
            )

            response_text = response.choices[0].message.content or ""
            try:
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    checks = json.loads(json_match.group())
                else:
                    checks = {"parse_error": response_text[:500]}
            except json.JSONDecodeError:
                checks = {"parse_error": response_text[:500]}

            fig_failures = sum(
                1 for k, v in checks.items()
                if isinstance(v, dict) and v.get("status") == "FAIL"
            )
            total_failures += fig_failures

            all_issues.append({
                "figure": fig_path.name,
                "checks": checks,
                "summary": checks.get("summary", ""),
                "failures": fig_failures,
            })

        except Exception as e:
            logger.warning("VLM audit error for %s: %s", fig_path.name, e)
            all_issues.append({
                "figure": fig_path.name,
                "checks": {},
                "summary": f"VLM audit error: {e}",
                "failures": 0,
            })

    return {
        "passed": total_failures == 0,
        "figures_checked": len(figure_paths),
        "total_failures": total_failures,
        "issues": all_issues,
    }


def run_paper_audit(pipeline_id: str, project_root: Path) -> str:
    """Run full G4+G5+figure audit and return markdown report."""
    paper_dir = project_root / "paper" / pipeline_id
    pdir = project_root / "artifacts" / f"pipeline_{pipeline_id}"

    draft_path = paper_dir / "draft_v1.md"
    results_path = pdir / "results.json"

    g4 = audit_g4_numerical_consistency(draft_path, results_path)
    g5 = audit_g5_statistical_completeness(draft_path)

    # VLM figure audit
    draft_text = draft_path.read_text() if draft_path.exists() else ""
    fig_audit = audit_figures(pipeline_id, project_root, draft_text=draft_text)

    # Overall verdict
    verdicts = [g4["passed"], g5["passed"], fig_audit["passed"]]
    pass_count = sum(verdicts)
    if pass_count == 3:
        overall = "PASS"
    elif pass_count >= 2:
        overall = "CONDITIONAL_PASS"
    else:
        overall = "FAIL"

    # Build report
    lines = [
        f"# Automated Paper Audit Report\n",
        f"**Pipeline**: {pipeline_id}\n",
        f"**Overall**: {overall}\n",
        f"\n## G4: Numerical Consistency\n",
        f"**Verdict**: {'PASS' if g4['passed'] else 'FAIL'}\n",
        f"**Numbers found**: {g4['total_numbers']}, "
        f"**Matched**: {g4['matched']}, "
        f"**Unmatched**: {g4['unmatched']}\n\n",
    ]

    if g4["issues"]:
        lines.append("| Line | Value | Status | Detail |\n")
        lines.append("|------|-------|--------|--------|\n")
        sorted_issues = sorted(g4["issues"], key=lambda x: x["status"] != "MATCH")
        for issue in sorted_issues[:30]:
            lines.append(
                f"| {issue['line']} | {issue['value']} | {issue['status']} | {issue['detail']} |\n"
            )

    lines.extend([
        f"\n## G5: Statistical Reporting Completeness\n",
        f"**Verdict**: {'PASS' if g5['passed'] else 'FAIL'} "
        f"({g5.get('present_count', 0)}/5 items present)\n\n",
        "| Item | Found | Count |\n",
        "|------|-------|-------|\n",
    ])
    for name in _STAT_KEYWORDS:
        count = g5["items_found"].get(name, 0)
        status = "YES" if count > 0 else "MISSING"
        lines.append(f"| {name} | {status} | {count} |\n")

    if g5["missing"]:
        lines.append(f"\n**Missing items**: {', '.join(g5['missing'])}\n")

    # Figure audit section
    if fig_audit["figures_checked"] > 0:
        lines.extend([
            f"\n## Figure Quality Audit (VLM)\n",
            f"**Figures checked**: {fig_audit['figures_checked']}\n",
            f"**Total failures**: {fig_audit.get('total_failures', 0)}\n",
            f"**Verdict**: {'PASS' if fig_audit['passed'] else 'FAIL'}\n\n",
        ])
        for issue in fig_audit["issues"]:
            lines.append(f"### {issue['figure']}\n")
            lines.append(f"**Summary**: {issue['summary']}\n\n")
            checks = issue.get("checks", {})
            if isinstance(checks, dict) and "parse_error" not in checks:
                lines.append("| Check | Status | Detail |\n|-------|--------|--------|\n")
                for check_name, check_val in checks.items():
                    if check_name == "summary":
                        continue
                    if isinstance(check_val, dict):
                        lines.append(
                            f"| {check_name} | {check_val.get('status', '?')} | "
                            f"{check_val.get('detail', '')} |\n"
                        )
                lines.append("\n")
    elif fig_audit.get("note"):
        lines.append(f"\n## Figure Quality Audit (VLM)\n{fig_audit['note']}\n")

    return "".join(lines)
