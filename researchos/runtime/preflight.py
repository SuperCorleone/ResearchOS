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

    # Find experiment code
    code_text = _load_code(pipeline_dir)
    if not code_text:
        return {
            "verdict": "WARN",
            "checks": {},
            "issues": [{"severity": "WARN", "check": "code_found",
                        "detail": "No experiment code found (code_patch.diff or run_experiment.py)"}],
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

    # 6. Seed parameter
    checks["seed_parameter_accepted"] = _check_seed_param(code_text, issues)

    # 7. Timeout risk
    checks["timeout_risk"] = _estimate_timeout_risk(code_text, plan)

    # 8. G3 claim-to-code mapping
    g3 = _check_g3_mapping(code_text, pipeline_dir)

    # 9. G3 trivial implementation detection (AST-based)
    trivial = _detect_trivial_implementations(code_text, issues)
    g3["trivial_functions"] = trivial

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
        # Fuzzy match: check if model name appears anywhere in code
        if model not in code_lower.replace("_", ""):
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


def _check_g3_mapping(code: str, pipeline_dir: Path) -> dict:
    """Check G3 claim-to-code mapping completeness.

    Reads hypothesis.md for claims, checks if code has mapping table
    and whether all claims are covered.
    """
    result = {
        "claims_total": 0,
        "claims_mapped": 0,
        "trivial_implementations": 0,
        "missing_claims": [],
    }

    # Read hypothesis for claims
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
    if "CLAIM-TO-CODE MAPPING" in code:
        # Count mapping entries (lines with | delimiters inside mapping block)
        in_mapping = False
        mapped = 0
        for line in code.splitlines():
            if "CLAIM-TO-CODE MAPPING" in line:
                in_mapping = True
                continue
            if in_mapping and "END MAPPING" in line:
                break
            if in_mapping and "|" in line:
                # Strip comment prefix (# ) before parsing
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
