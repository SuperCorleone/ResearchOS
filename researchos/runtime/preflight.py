"""Code preflight checks — pure Python, no LLM.

Runs after CODE step, before RUN step (MAIN stage only).
Validates that experiment code matches experiment_plan.yaml.

Checks:
1. Syntax validity (Python compile)
2. Import availability
3. Models match experiment plan
4. Metrics match experiment plan
5. Data source consistency
6. G3 claim-to-code mapping completeness
7. Timeout/cost risk estimation
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import re
from pathlib import Path

import yaml

from researchos.runtime.design_checks import check_four_slot_plan

logger = logging.getLogger(__name__)


def run_preflight(pipeline_dir: Path, project_root: Path) -> dict:
    """Run all preflight checks on experiment code.

    Returns a report dict with verdict (PASS/WARN/BLOCK) and details.
    """
    issues: list[dict] = []
    checks: dict[str, bool | str] = {}

    # Load experiment plan
    plan_path = pipeline_dir / "experiment_plan.yaml"
    plan = {}
    if plan_path.exists():
        try:
            plan = yaml.safe_load(plan_path.read_text()) or {}
        except Exception:
            issues.append({"severity": "ERROR", "check": "plan_parse",
                           "detail": "Failed to parse experiment_plan.yaml"})

    # 0. Four-slot plan schema: plan_dataset / plan_model / plan_training / plan_testing
    # Run this regardless of whether code is present — a missing slot must BLOCK.
    four_slot_issues = check_four_slot_plan(plan)
    issues.extend(four_slot_issues)
    checks["four_slot_plan"] = not any(
        i["severity"] == "ERROR" for i in four_slot_issues
    )

    # Find experiment code
    code_text = _load_code(pipeline_dir)
    if not code_text:
        issues.append({
            "severity": "WARN",
            "check": "code_found",
            "detail": "No experiment code found (code_patch.diff or run_experiment.py)",
        })
        # Preserve four-slot verdict escalation even when no code is present.
        if any(i["severity"] == "ERROR" for i in issues):
            early_verdict = "BLOCK"
        else:
            early_verdict = "WARN"
        return {
            "verdict": early_verdict,
            "checks": checks,
            "issues": issues,
            "g3_implementation_coverage": {},
        }

    # 1. Syntax check
    checks["syntax_valid"] = _check_syntax(code_text, issues)

    # 2. Import check
    checks["imports_available"] = _check_imports(code_text, issues)

    # 3. Models match plan
    checks["models_match_plan"] = _check_models(code_text, plan, issues)

    # 4. Metrics match plan
    checks["metrics_match_plan"] = _check_metrics(code_text, plan, issues)

    # 5. Data source consistency
    checks["data_source_match"] = _check_data_source(code_text, plan, pipeline_dir, project_root, issues)

    # 5b. Reference data consistency
    checks["reference_data_consistency"] = _check_reference_data_consistency(code_text, plan, issues)

    # 6. Seed parameter
    checks["seed_parameter_accepted"] = _check_seed_param(code_text, issues)

    # 7. Timeout risk
    checks["timeout_risk"] = _estimate_timeout_risk(code_text, plan)

    # 8. G3 claim-to-code mapping
    g3 = _check_g3_mapping(code_text, pipeline_dir, issues)

    # 9. G3 trivial implementation detection (AST-based)
    trivial = _detect_trivial_implementations(code_text, issues)
    g3["trivial_functions"] = trivial

    # 10. A20 — reference-codebase locator verification.
    # For each concept's ``reference_codebases`` entry of the form
    # ``<slug>::<path>::L<a>-L<b>``, verify the file exists in the cloned
    # repo and the line range lies within the file. A bad triple BLOCKs
    # the pipeline; URL-only entries are tolerated (WARN if we have a
    # matching clone and the concept is nontrivial).
    code_ref_result = _check_reference_codebases(pipeline_dir, project_root, issues)
    checks["reference_codebases_grounded"] = code_ref_result.get("verdict", "PASS") != "BLOCK"

    # Determine verdict
    errors = [i for i in issues if i["severity"] == "ERROR"]
    warns = [i for i in issues if i["severity"] == "WARN"]

    if errors:
        verdict = "BLOCK"
    elif warns:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return {
        "verdict": verdict,
        "checks": checks,
        "issues": issues,
        "g3_implementation_coverage": g3,
        "reference_codebases": code_ref_result,
    }


def _load_code(pipeline_dir: Path) -> str:
    """Extract Python code from code_patch.diff or run_experiment.py."""
    # Try run_experiment.py first
    py_file = pipeline_dir / "run_experiment.py"
    if py_file.exists():
        return py_file.read_text(errors="replace")

    # Fall back to code_patch.diff — extract added lines
    diff_file = pipeline_dir / "code_patch.diff"
    if diff_file.exists():
        lines = []
        for line in diff_file.read_text(errors="replace").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])  # Strip the leading +
        return "\n".join(lines)

    return ""


def _check_syntax(code: str, issues: list[dict]) -> bool:
    """Check if Python code compiles without syntax errors."""
    try:
        compile(code, "<experiment>", "exec")
        return True
    except SyntaxError as e:
        issues.append({
            "severity": "ERROR",
            "check": "syntax",
            "detail": f"Syntax error at line {e.lineno}: {e.msg}",
        })
        return False


def _check_imports(code: str, issues: list[dict]) -> bool:
    """Check if imported packages are available."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    all_ok = True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if not _is_importable(top):
                    issues.append({
                        "severity": "WARN",
                        "check": "import",
                        "detail": f"Package '{top}' may not be installed",
                    })
                    all_ok = False
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if not _is_importable(top):
                    issues.append({
                        "severity": "WARN",
                        "check": "import",
                        "detail": f"Package '{top}' may not be installed",
                    })
                    all_ok = False
    return all_ok


def _is_importable(module_name: str) -> bool:
    """Check if a top-level module can be imported."""
    # Standard library modules and common packages — don't flag these
    KNOWN_SAFE = {
        "os", "sys", "json", "csv", "math", "random", "time", "datetime",
        "pathlib", "typing", "collections", "itertools", "functools",
        "argparse", "hashlib", "tempfile", "warnings", "traceback",
        "re", "copy", "io", "abc", "logging", "subprocess", "shutil",
        "numpy", "np", "pandas", "pd", "sklearn", "scipy", "matplotlib",
        "seaborn", "torch", "tensorflow", "tf", "yaml", "pydantic",
    }
    if module_name in KNOWN_SAFE:
        return True
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


def _check_models(code: str, plan: dict, issues: list[dict]) -> bool:
    """Check if all models from experiment plan appear in code."""
    code_lower = code.lower()

    # Extract model names from plan
    planned_models = set()
    for var in plan.get("variables", {}).get("independent", []):
        if var.get("name") in ("model_type", "model", "algorithm", "method"):
            for v in var.get("values", []):
                planned_models.add(v.lower().replace("_", ""))

    # Also check baselines
    for b in plan.get("baselines", []):
        name = b.get("name", "").lower().replace("_", "")
        if name:
            planned_models.add(name)

    if not planned_models:
        return True  # No models specified in plan

    missing = []
    for model in planned_models:
        # Word-boundary match to avoid "bert" matching "albert"
        code_words = code_lower.replace("_", " ")
        if not re.search(r'\b' + re.escape(model) + r'\b', code_words):
            missing.append(model)

    if missing:
        issues.append({
            "severity": "WARN",
            "check": "models",
            "detail": f"Models in plan but not found in code: {missing}",
        })
        return False
    return True


def _check_metrics(code: str, plan: dict, issues: list[dict]) -> bool:
    """Check if all metrics from experiment plan are computed in code."""
    code_lower = code.lower()

    planned_metrics = set()
    for m in plan.get("evaluation", {}).get("metrics", []):
        name = m.get("name", "").lower()
        if name:
            planned_metrics.add(name)

    # Also check dependent variables
    for dep in plan.get("variables", {}).get("dependent", []):
        name = dep.get("name", "").lower()
        if name:
            planned_metrics.add(name)

    if not planned_metrics:
        return True

    missing = []
    for metric in planned_metrics:
        # Fuzzy: check code for metric name or common aliases
        aliases = _metric_aliases(metric)
        if not any(a in code_lower for a in aliases):
            missing.append(metric)

    if missing:
        issues.append({
            "severity": "WARN",
            "check": "metrics",
            "detail": f"Metrics in plan but not found in code: {missing}",
        })
        return False
    return True


def _metric_aliases(name: str) -> list[str]:
    """Generate common aliases for a metric name."""
    aliases = [name, name.replace("_", "")]
    ALIAS_MAP = {
        "accuracy": ["accuracy_score", "acc"],
        "f1_macro": ["f1_score", "f1macro", "f1_macro"],
        "f1_micro": ["f1_score", "f1micro"],
        "mae": ["mean_absolute_error", "mae"],
        "mse": ["mean_squared_error", "mse"],
        "auc_roc": ["roc_auc_score", "aucroc", "auc"],
        "precision": ["precision_score"],
        "recall": ["recall_score"],
    }
    aliases.extend(ALIAS_MAP.get(name, []))
    return aliases


def _check_data_source(
    code: str, plan: dict, pipeline_dir: Path, project_root: Path, issues: list[dict]
) -> bool:
    """Check if code uses the data source declared in experiment plan."""
    data_section = plan.get("data", {})
    source = data_section.get("source", "synthetic")
    code_lower = code.lower()

    if source in ("file", "api"):
        # Code should load from /data/raw/ or use fetch_dataset
        builtin_loaders = [
            "sklearn.datasets", "load_iris", "load_wine", "load_digits",
            "load_boston", "load_breast_cancer", "fetch_",
            "torchvision.datasets", "tensorflow_datasets",
        ]
        for loader in builtin_loaders:
            if loader in code_lower:
                issues.append({
                    "severity": "ERROR",
                    "check": "data_source",
                    "detail": (
                        f"Plan says data.source='{source}' but code uses "
                        f"built-in loader '{loader}'. Must load from "
                        f"/data/raw/ via fetch_dataset."
                    ),
                })
                return False

        # Check if code references the data directory
        pipeline_id = pipeline_dir.name.replace("pipeline_", "")
        if f"data/raw/{pipeline_id}" not in code and "data/raw" not in code:
            issues.append({
                "severity": "WARN",
                "check": "data_source",
                "detail": f"Plan says data.source='{source}' but code doesn't reference data/raw/",
            })
            return False

    return True


def _check_reference_data_consistency(
    code: str, plan: dict, issues: list[dict]
) -> bool:
    """Check that code does not generate synthetic data when reference data consistency is required."""
    cons_req = plan.get("data", {}).get("consistency_requirement")
    if not cons_req:
        return True  # No consistency requirement, skip

    ref_paper = cons_req.get("reference_paper", "unknown")
    code_lower = code.lower()

    # Patterns that suggest synthetic data generation
    synthetic_patterns = [
        ("make_classification", "sklearn.datasets.make_classification"),
        ("make_regression", "sklearn.datasets.make_regression"),
        ("make_blobs", "sklearn.datasets.make_blobs"),
        ("np.random.randn", "numpy random data generation"),
        ("np.random.rand(", "numpy random data generation"),
        ("np.random.normal", "numpy random data generation"),
        ("torch.randn", "PyTorch random tensor generation"),
        ("random.gauss", "random.gauss data generation"),
        ("fake.", "faker library data generation"),
        ("generate_data", "custom data generation function"),
        ("simulate_data", "custom data simulation function"),
        ("create_synthetic", "synthetic data creation"),
    ]

    found_synthetic = []
    for pattern, description in synthetic_patterns:
        if pattern in code_lower:
            found_synthetic.append(description)

    if found_synthetic:
        issues.append({
            "severity": "ERROR",
            "check": "reference_data_consistency",
            "detail": (
                f"data.consistency_requirement references '{ref_paper}' but code "
                f"appears to generate synthetic data: {', '.join(set(found_synthetic))}. "
                f"When improving upon a reference paper, you MUST use the same dataset. "
                f"Use fetch_dataset to download the reference data."
            ),
        })
        return False

    # Also check that code references data/raw/ or fetch_dataset
    source = plan.get("data", {}).get("source", "")
    if source in ("file", "api"):
        if "data/raw" not in code and "fetch_dataset" not in code_lower:
            issues.append({
                "severity": "WARN",
                "check": "reference_data_consistency",
                "detail": (
                    f"data.consistency_requirement references '{ref_paper}' but code "
                    f"does not reference data/raw/ directory or fetch_dataset. "
                    f"Ensure the reference paper's data is being loaded."
                ),
            })
            return False

    return True


def _check_seed_param(code: str, issues: list[dict]) -> bool:
    """Check if code accepts a seed parameter."""
    if "--seed" in code or "seed" in code.lower():
        return True
    issues.append({
        "severity": "WARN",
        "check": "seed_param",
        "detail": "Code doesn't appear to accept a seed parameter for reproducibility",
    })
    return False


def _estimate_timeout_risk(code: str, plan: dict) -> str:
    """Estimate risk of code exceeding the 120s execute_python timeout.

    Returns: LOW / MEDIUM / HIGH
    """
    risk_score = 0

    # Deep learning indicators
    if any(kw in code.lower() for kw in ["torch.nn", "tensorflow", "keras", "epochs"]):
        risk_score += 3

    # Large loop counts
    for match in re.finditer(r"range\((\d+)\)", code):
        n = int(match.group(1))
        if n > 10000:
            risk_score += 2
        elif n > 1000:
            risk_score += 1

    # Multiple seeds with heavy computation
    seeds = plan.get("evaluation", {}).get("seeds", [])
    if len(seeds) > 5:
        risk_score += 1

    # Grid search
    if "gridsearchcv" in code.lower() or "grid_search" in code.lower():
        risk_score += 1

    if risk_score >= 4:
        return "HIGH"
    elif risk_score >= 2:
        return "MEDIUM"
    return "LOW"


def _extract_mapping_rows(code: str) -> list[str]:
    """Return the raw text of rows inside the CLAIM-TO-CODE MAPPING block."""
    rows: list[str] = []
    in_mapping = False
    for line in code.splitlines():
        if "CLAIM-TO-CODE MAPPING" in line:
            in_mapping = True
            continue
        if in_mapping and "END MAPPING" in line:
            break
        if in_mapping:
            rows.append(line)
    return rows


def _check_g3_mapping(code: str, pipeline_dir: Path, issues: list[dict] | None = None) -> dict:
    """Check G3 claim-to-code mapping completeness.

    Prefers loading ``concepts.yaml`` (A12 atomic-concept ontology). If present,
    every ``AtomicConcept.id`` must appear as a row in the CLAIM-TO-CODE MAPPING
    comment block — either mapped to a real file/function that exists in
    ``code_text`` (substring check), or explicitly marked ``SKIPPED``.

    Falls back to the original hypothesis.md regex + row-count behaviour when
    ``concepts.yaml`` is missing (e.g., FAST_FAIL stage).

    Missing concept (not SKIPPED, not mapped) -> ERROR severity -> BLOCK.
    """
    if issues is None:
        issues = []

    result: dict = {
        "claims_total": 0,
        "claims_mapped": 0,
        "trivial_implementations": 0,
        "missing_claims": [],
        "per_concept": {},
    }

    has_table = "CLAIM-TO-CODE MAPPING" in code
    rows = _extract_mapping_rows(code) if has_table else []

    # ── New path: concepts.yaml exists ──────────────────────────────
    concepts_path = pipeline_dir / "concepts.yaml"
    if concepts_path.exists():
        try:
            concepts = yaml.safe_load(concepts_path.read_text()) or []
        except Exception:
            concepts = []
            issues.append({
                "severity": "WARN",
                "check": "g3_concepts_parse",
                "detail": "Failed to parse concepts.yaml — falling back to legacy G3 check",
            })

        if isinstance(concepts, list) and concepts:
            result["claims_total"] = len(concepts)

            if not has_table:
                # No mapping block at all — every concept is missing
                for c in concepts:
                    cid = str(c.get("id", "")) if isinstance(c, dict) else ""
                    if not cid:
                        continue
                    result["per_concept"][cid] = {
                        "status": "missing",
                        "locator": "",
                    }
                    result["missing_claims"].append(cid)
                issues.append({
                    "severity": "ERROR",
                    "check": "g3_mapping",
                    "detail": (
                        "G3: No CLAIM-TO-CODE MAPPING block in code, but "
                        f"{len(concepts)} atomic concepts are declared in concepts.yaml"
                    ),
                })
                return result

            for c in concepts:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id", ""))
                if not cid:
                    continue

                # Find the row that references this concept id.
                # A row "matches" if the concept id appears as a word.
                matching_row = None
                for raw in rows:
                    stripped = raw.lstrip("# \t")
                    if re.search(r"\b" + re.escape(cid) + r"\b", stripped):
                        matching_row = stripped
                        break

                if matching_row is None:
                    result["per_concept"][cid] = {
                        "status": "missing",
                        "locator": "",
                    }
                    result["missing_claims"].append(cid)
                    issues.append({
                        "severity": "ERROR",
                        "check": "g3_mapping",
                        "detail": (
                            f"G3: Concept '{cid}' from concepts.yaml has no row in "
                            f"CLAIM-TO-CODE MAPPING. Either map it to a file/function "
                            f"or mark it '{cid}: SKIPPED because <reason>'."
                        ),
                    })
                    continue

                if "SKIPPED" in matching_row.upper():
                    result["per_concept"][cid] = {
                        "status": "skipped",
                        "locator": matching_row.strip(),
                    }
                    result["claims_mapped"] += 1
                    continue

                # Extract a locator: prefer explicit code_implementation from the
                # concept, fall back to cells of the row.
                locator = str(c.get("code_implementation", "")).strip()
                row_cells = [
                    cell.strip() for cell in matching_row.split("|") if cell.strip()
                ]
                locator_candidates = [locator] if locator else []
                locator_candidates.extend(row_cells)

                # Substring check against code_text — strip common separators.
                found = False
                chosen = ""
                for cand in locator_candidates:
                    if not cand or cand == cid:
                        continue
                    # Try the raw candidate and its file::func / file:func / func parts.
                    parts = re.split(r"::|[:\s]+", cand)
                    tokens = [cand] + [p for p in parts if p and len(p) > 2]
                    for tok in tokens:
                        # Drop trivial punctuation
                        tok_clean = tok.strip("()`\"' ")
                        if len(tok_clean) < 3:
                            continue
                        if tok_clean in code:
                            found = True
                            chosen = tok_clean
                            break
                    if found:
                        break

                if found:
                    result["per_concept"][cid] = {
                        "status": "mapped",
                        "locator": chosen,
                    }
                    result["claims_mapped"] += 1
                else:
                    # Row exists but locator doesn't appear in code — treat as missing.
                    result["per_concept"][cid] = {
                        "status": "missing",
                        "locator": matching_row.strip(),
                    }
                    result["missing_claims"].append(cid)
                    issues.append({
                        "severity": "ERROR",
                        "check": "g3_mapping",
                        "detail": (
                            f"G3: Concept '{cid}' is listed in CLAIM-TO-CODE MAPPING "
                            f"but its locator does not appear in the code body."
                        ),
                    })

            return result

    # ── Fallback path: legacy hypothesis.md regex ──────────────────
    hyp_path = pipeline_dir / "hypothesis.md"
    if not hyp_path.exists():
        return result

    hyp_text = hyp_path.read_text(errors="replace")

    # Extract key claims (lines with testable predictions or hypothesis statements)
    claims = []
    in_predictions = False
    for line in hyp_text.splitlines():
        if "prediction" in line.lower() or "testable" in line.lower():
            in_predictions = True
            continue
        if in_predictions and line.strip().startswith(("-", "*", "1", "2", "3", "4", "5")):
            claim = re.sub(r"^[\-\*\d\.\)]+\s*", "", line.strip())
            if len(claim) > 10:
                claims.append(claim)
        elif in_predictions and line.strip() == "":
            in_predictions = False

    # If no structured claims found, try the hypothesis statement
    if not claims:
        for line in hyp_text.splitlines():
            if "hypothes" in line.lower() and len(line.strip()) > 20:
                claims.append(line.strip())
                break

    result["claims_total"] = len(claims)

    # Check if code has CLAIM-TO-CODE MAPPING table
    if has_table:
        # Count mapping entries (lines with | delimiters inside mapping block)
        mapped = 0
        for line in rows:
            if "|" not in line:
                continue
            stripped = line.lstrip("# \t")
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cells) >= 2 and not cells[0].startswith("-"):
                mapped += 1
        result["claims_mapped"] = mapped
    else:
        result["missing_claims"] = ["No CLAIM-TO-CODE MAPPING table found in code"]

    return result


# ── G3 Trivial implementation detection (AST-based) ─────────────


def _detect_trivial_implementations(code: str, issues: list[dict]) -> list[dict]:
    """Detect functions with trivial implementations via AST analysis.

    Flags:
    1. Functions whose body is only `return <constant>`
    2. Functions that pass through input unchanged (`return x`)
    3. Functions with empty bodies (`pass`)
    4. Functions that always return the same value regardless of input

    Returns list of detected trivial functions with details.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    trivial_funcs: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        func_name = node.name
        body = node.body

        # Skip dunder methods, test helpers, and tiny utils
        if func_name.startswith("_") and func_name != "__init__":
            continue
        if func_name in ("main", "parse_args", "print_versions", "print_version_info",
                         "set_random_seeds", "set_seeds", "setup", "teardown"):
            continue

        # Get the effective body (skip docstrings)
        effective_body = [
            stmt for stmt in body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str))
        ]

        if not effective_body:
            continue

        # Check 1: body is just `pass`
        if len(effective_body) == 1 and isinstance(effective_body[0], ast.Pass):
            trivial_funcs.append({
                "function": func_name,
                "line": node.lineno,
                "reason": "empty_body",
                "detail": f"Function '{func_name}' has empty body (pass only)",
            })
            issues.append({
                "severity": "WARN",
                "check": "g3_trivial",
                "detail": f"G3: Function '{func_name}' (line {node.lineno}) has empty body — trivial implementation",
            })
            continue

        # Check 2: body is just `return <constant>`
        if len(effective_body) == 1 and isinstance(effective_body[0], ast.Return):
            ret_val = effective_body[0].value
            if _is_constant_expr(ret_val):
                const_repr = _const_repr(ret_val)
                trivial_funcs.append({
                    "function": func_name,
                    "line": node.lineno,
                    "reason": "constant_return",
                    "detail": f"Function '{func_name}' always returns constant {const_repr}",
                })
                issues.append({
                    "severity": "WARN",
                    "check": "g3_trivial",
                    "detail": (
                        f"G3: Function '{func_name}' (line {node.lineno}) always returns "
                        f"constant {const_repr} — may be a stub implementation"
                    ),
                })
                continue

        # Check 3: body is just `return <parameter>` (pass-through)
        if len(effective_body) == 1 and isinstance(effective_body[0], ast.Return):
            ret_val = effective_body[0].value
            if isinstance(ret_val, ast.Name):
                param_names = {arg.arg for arg in node.args.args}
                if ret_val.id in param_names:
                    trivial_funcs.append({
                        "function": func_name,
                        "line": node.lineno,
                        "reason": "passthrough",
                        "detail": f"Function '{func_name}' returns parameter '{ret_val.id}' unchanged",
                    })
                    issues.append({
                        "severity": "WARN",
                        "check": "g3_trivial",
                        "detail": (
                            f"G3: Function '{func_name}' (line {node.lineno}) is a pass-through — "
                            f"returns input '{ret_val.id}' unchanged"
                        ),
                    })
                    continue

        # Check 4: all return paths yield the same constant
        returns = _collect_returns(effective_body)
        if len(returns) >= 2 and all(_is_constant_expr(r.value) for r in returns):
            values = {_const_repr(r.value) for r in returns}
            if len(values) == 1:
                trivial_funcs.append({
                    "function": func_name,
                    "line": node.lineno,
                    "reason": "all_paths_same_constant",
                    "detail": f"Function '{func_name}' returns {values.pop()} on all paths",
                })
                issues.append({
                    "severity": "WARN",
                    "check": "g3_trivial",
                    "detail": (
                        f"G3: Function '{func_name}' (line {node.lineno}) returns the same "
                        f"constant on all code paths — ignores input"
                    ),
                })

    return trivial_funcs


def _is_constant_expr(node: ast.AST | None) -> bool:
    """Check if an AST node represents a constant value."""
    if node is None:
        return True  # bare `return` → returns None
    if isinstance(node, ast.Constant):
        return True
    # Negative numbers: ast.UnaryOp(op=USub, operand=Constant)
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)):
        return True
    # Empty collections
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        if not node.elts if hasattr(node, 'elts') else not node.keys:
            return True
    return False


def _const_repr(node: ast.AST | None) -> str:
    """Get a short string representation of a constant AST node."""
    if node is None:
        return "None"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)):
        return repr(-node.operand.value)
    return "?"


def _collect_returns(body: list[ast.stmt]) -> list[ast.Return]:
    """Collect all Return nodes from a function body (shallow — no nested functions)."""
    returns = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Return):
            returns.append(node)
    return returns


# ── A20 reference-codebase locator check ────────────────────────────

# Matches "<slug>::<path>::L<start>-L<end>" locators emitted by
# concept_extractor when code_recon.yaml reports a cloned repo.
_CODE_REF_TRIPLE_RE = re.compile(
    r"^(?P<slug>[A-Za-z0-9_.\-]+)::(?P<path>[^:]+)::L(?P<start>\d+)-L(?P<end>\d+)$"
)


def _load_code_recon_index(pipeline_dir: Path, project_root: Path) -> dict[str, Path]:
    """Return ``slug -> repo_path`` for repos in code_recon.yaml with status ok/cached."""
    recon_path = pipeline_dir / "code_recon.yaml"
    if not recon_path.exists():
        return {}
    try:
        recon = yaml.safe_load(recon_path.read_text()) or {}
    except Exception:
        return {}
    index: dict[str, Path] = {}
    for repo in recon.get("repos", []) or []:
        if not isinstance(repo, dict):
            continue
        if repo.get("status") not in ("ok", "cached"):
            continue
        slug = str(repo.get("slug") or "")
        path_str = str(repo.get("path") or "")
        if not slug or not path_str:
            continue
        # path is repo-root relative to project_root (see code_recon.run_code_recon).
        repo_path = project_root / path_str
        if repo_path.exists():
            index[slug] = repo_path
    return index


def _verify_triple(
    slug: str,
    rel_path: str,
    line_start: int,
    line_end: int,
    repo_root: Path,
) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a single grounded locator triple."""
    if line_start < 1 or line_end < line_start:
        return False, f"invalid line range L{line_start}-L{line_end}"
    # Resolve + containment check: prevent .. / absolute escapes.
    try:
        target = (repo_root / rel_path).resolve()
        repo_root_resolved = repo_root.resolve()
    except OSError as exc:
        return False, f"resolve failed: {exc}"
    try:
        target.relative_to(repo_root_resolved)
    except ValueError:
        return False, f"path escapes repo root: {rel_path}"
    if not target.exists() or not target.is_file():
        return False, f"file not found in repo '{slug}': {rel_path}"
    try:
        total_lines = sum(1 for _ in target.open("r", errors="replace"))
    except OSError as exc:
        return False, f"read failed: {exc}"
    if line_end > total_lines:
        return False, (
            f"line range L{line_start}-L{line_end} exceeds file length {total_lines}"
        )
    return True, ""


def _check_reference_codebases(
    pipeline_dir: Path, project_root: Path, issues: list[dict]
) -> dict:
    """A20 — verify grounded ``reference_codebases`` locators against cloned repos.

    Every entry of the form ``<slug>::<path>::L<a>-L<b>`` must point at a
    file that exists in the corresponding clone, with a line range that
    fits inside the file. Mismatches add ``ERROR`` issues so the verdict
    escalates to BLOCK. Plain URLs or free-text entries are reported in
    ``per_concept`` but do not block (the concept_extractor is allowed
    to fall back to URL-only when no clone matched).
    """
    result: dict = {
        "verdict": "PASS",
        "repos_indexed": 0,
        "per_concept": {},
        "triples_total": 0,
        "triples_verified": 0,
    }
    concepts_path = pipeline_dir / "concepts.yaml"
    if not concepts_path.exists():
        # Nothing to check — e.g. FAST_FAIL stage, or no concept_extractor yet.
        return result
    try:
        concepts = yaml.safe_load(concepts_path.read_text()) or []
    except Exception:
        return result
    # concepts.yaml may be either a bare list or {"concepts": [...]} — handle both.
    if isinstance(concepts, dict):
        concepts = concepts.get("concepts", []) or []
    if not isinstance(concepts, list) or not concepts:
        return result

    repo_index = _load_code_recon_index(pipeline_dir, project_root)
    result["repos_indexed"] = len(repo_index)

    had_error = False
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        cid = str(concept.get("id") or "")
        entries = concept.get("reference_codebases", []) or []
        if not isinstance(entries, list):
            continue
        per: list[dict] = []
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip():
                continue
            m = _CODE_REF_TRIPLE_RE.match(entry.strip())
            if not m:
                per.append({"entry": entry, "kind": "url_or_text", "ok": True})
                continue
            result["triples_total"] += 1
            slug = m.group("slug")
            rel_path = m.group("path")
            start = int(m.group("start"))
            end = int(m.group("end"))
            repo_root = repo_index.get(slug)
            if repo_root is None:
                had_error = True
                per.append({
                    "entry": entry, "kind": "triple", "ok": False,
                    "error": f"slug '{slug}' not present in code_recon.yaml",
                })
                issues.append({
                    "severity": "ERROR",
                    "check": "reference_codebases",
                    "detail": (
                        f"A20: concept {cid or '?'} cites repo '{slug}' but "
                        f"code_recon.yaml has no ok/cached entry for it"
                    ),
                })
                continue
            ok, detail = _verify_triple(slug, rel_path, start, end, repo_root)
            if ok:
                result["triples_verified"] += 1
                per.append({"entry": entry, "kind": "triple", "ok": True})
            else:
                had_error = True
                per.append({
                    "entry": entry, "kind": "triple", "ok": False,
                    "error": detail,
                })
                issues.append({
                    "severity": "ERROR",
                    "check": "reference_codebases",
                    "detail": (
                        f"A20: concept {cid or '?'} reference_codebases entry "
                        f"'{entry}' failed verification ({detail})"
                    ),
                })
        if per:
            result["per_concept"][cid or f"<anon-{id(concept)}>"] = per

    if had_error:
        result["verdict"] = "BLOCK"
    return result
