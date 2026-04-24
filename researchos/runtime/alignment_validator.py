"""Alignment contract validator (pure Python — no LLM).

Runs after the ALIGNMENT step produces alignment_contract.yaml.
Validates structural integrity, evidence quote provenance, value_ref
dereferencing, and cross-spec consistency. Emits
alignment_validation_report.json with verdict PASS/WARN/BLOCK.

Helper primitives (normalize_quote, match_quote, deref, EVIDENCE_EXEMPT)
live at the top; the full check pipeline is in ``run_validator()`` near
the bottom. Each individual check is a ``check_*`` function returning a
list of CheckResult so the orchestrator stays declarative.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence coverage exemption list
# ---------------------------------------------------------------------------

EVIDENCE_EXEMPT: frozenset[str] = frozenset({
    "paper_id",
    "reference_code.available",
    "reference_code.commit_or_tag",
    "candidate_model_pool.applicable",
    "dataset.access.type",
    "split_protocol.strategy",
    "evidence",  # the evidence array itself
})


# ---------------------------------------------------------------------------
# Quote normalisation: PDF artefacts → plain text
# ---------------------------------------------------------------------------

LIGATURES: dict[str, str] = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}

_LIGATURE_TRANS = str.maketrans(LIGATURES)
_HYPHEN_BREAK_RE = re.compile(r"-\n([a-z])")
_WS_RE = re.compile(r"\s+")


def normalize_quote(s: str) -> str:
    """Normalize a string for substring matching across PDF→md noise.

    Steps applied in order:
      1. Re-join hyphenated line breaks: "infor-\\nmation" → "information".
      2. Expand ligatures: ﬁ→fi, ﬂ→fl, ﬀ→ff, ﬃ→ffi, ﬄ→ffl.
      3. Collapse all whitespace runs (including newlines) to single space.
      4. Strip leading/trailing whitespace.

    Case is preserved; the matcher does a case-fold fallback.
    """
    if not isinstance(s, str):
        return ""
    s = _HYPHEN_BREAK_RE.sub(r"\1", s)
    s = s.translate(_LIGATURE_TRANS)
    s = _WS_RE.sub(" ", s).strip()
    return s


def match_quote(quote: str, paper_md: str) -> bool:
    """Return True if `quote` is a substring of `paper_md` after normalisation.

    Performs case-sensitive match first, then case-folded fallback. Quotes
    shorter than 8 tokens after normalisation should be excluded by the
    caller (this function does not skip them — the caller decides whether
    to count short quotes towards the coverage denominator).
    """
    nq = normalize_quote(quote)
    np = normalize_quote(paper_md)
    if not nq or not np:
        return False
    if nq in np:
        return True
    return nq.lower() in np.lower()


def quote_token_count(quote: str) -> int:
    """Token count after normalisation. Used to skip too-short quotes."""
    return len(normalize_quote(quote).split())


# ---------------------------------------------------------------------------
# Path dereferencing for value_ref / value_ref_subtree
# ---------------------------------------------------------------------------

_ARRAY_INDEX_RE = re.compile(r"^([^\[]+)\[([^\]=]+)=([^\]]+)\]$")


class DerefError(ValueError):
    """Raised when a value_ref / value_ref_subtree cannot be resolved."""


def _step(obj: Any, segment: str) -> Any:
    """Apply one path segment to obj. Supports `key` and `key[id=value]`."""
    m = _ARRAY_INDEX_RE.match(segment)
    if m:
        key, id_field, id_val = m.group(1), m.group(2), m.group(3)
        if not isinstance(obj, dict) or key not in obj:
            raise DerefError(f"missing key {key!r} for array index {segment!r}")
        arr = obj[key]
        if not isinstance(arr, list):
            raise DerefError(f"expected list at {key!r}, got {type(arr).__name__}")
        for item in arr:
            if isinstance(item, dict) and str(item.get(id_field)) == id_val:
                return item
        raise DerefError(f"no array element with {id_field}={id_val!r} under {key!r}")
    # plain key
    if not isinstance(obj, dict):
        raise DerefError(f"expected dict to access key {segment!r}, got {type(obj).__name__}")
    if segment not in obj:
        raise DerefError(f"missing key {segment!r}")
    return obj[segment]


def deref(
    path: str,
    specs: dict[str, dict],
    *,
    expect: Literal["leaf", "subtree"] = "leaf",
) -> Any:
    """Dereference a value_ref path against a dict of named specs.

    Args:
        path:   dot-path like "paper_spec.dataset.name" or
                "paper_spec.baseline_method.method_steps[id=S2]".
        specs:  mapping of root name → loaded spec dict, e.g.
                {"paper_spec": {...}, "proposal_spec": {...}}.
        expect: "leaf" requires a scalar (str/int/float/bool/None).
                "subtree" requires a dict or list.

    Raises:
        DerefError: any path segment missing, type mismatch, or final
                    type does not match `expect`.

    Returns the dereferenced value.
    """
    if not isinstance(path, str) or "." not in path:
        raise DerefError(f"path must be dotted, got {path!r}")
    parts = path.split(".")
    root = parts[0]
    if root not in specs:
        raise DerefError(f"unknown spec root {root!r}; known: {sorted(specs)}")
    obj: Any = specs[root]
    for seg in parts[1:]:
        obj = _step(obj, seg)
    if expect == "leaf":
        if isinstance(obj, (dict, list)):
            raise DerefError(
                f"path {path!r} resolved to {type(obj).__name__}; expected leaf scalar"
            )
    else:  # subtree
        if not isinstance(obj, (dict, list)):
            raise DerefError(
                f"path {path!r} resolved to {type(obj).__name__}; expected dict or list"
            )
    return obj


def is_not_stated(value: Any) -> bool:
    """Detect the NOT_STATED sentinel in extracted spec values."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().upper() in {"NOT_STATED", "NOT STATED"}:
        return True
    return False


# ---------------------------------------------------------------------------
# Check infrastructure
# ---------------------------------------------------------------------------


Severity = Literal["PASS", "WARN", "ERROR"]
Verdict = Literal["PASS", "WARN", "BLOCK"]

#: Coverage thresholds for evidence-quote substring matching.
#: ≥ MATCH_PASS_RATIO  → PASS for the aggregate check
#: ≥ MATCH_WARN_RATIO  → WARN
#: < MATCH_WARN_RATIO  → ERROR
MATCH_PASS_RATIO = 0.95
MATCH_WARN_RATIO = 0.80

#: Minimum token count for a quote to count toward the denominator.
MIN_QUOTE_TOKENS = 8

#: Minimum evidence-coverage ratio for paper_spec to pass.
EVIDENCE_COVERAGE_THRESHOLD = 0.90

#: Locks that *must* dereference to a concrete paper-side value (no
#: NOT_STATED, no None). Mirrors AlignmentContract.LOCKS_REQUIRING_PAPER_VALUE
#: but kept as a string set so this module can run even when the
#: alignment_contract pydantic module is not yet loaded.
LOCKS_REQUIRING_PAPER_VALUE: frozenset[str] = frozenset({
    "LOCKED_TO_PAPER",
    "LOCKED_TO_PAPER_VIA_REF_CODE",
    "LOCKED_TO_PAPER_VIA_REIMPL",
    "LOCKED_TO_PAPER_PLUS_PROPOSAL",
})


@dataclass
class CheckResult:
    check: str
    severity: Severity
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ValidationReport:
    verdict: Verdict
    checks: list[CheckResult] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


def _aggregate_verdict(results: list[CheckResult]) -> Verdict:
    """ERROR ⇒ BLOCK; WARN ⇒ WARN; otherwise PASS."""
    if any(r.severity == "ERROR" for r in results):
        return "BLOCK"
    if any(r.severity == "WARN" for r in results):
        return "WARN"
    return "PASS"


def _summary(results: list[CheckResult]) -> dict[str, int]:
    return {
        "PASS": sum(1 for r in results if r.severity == "PASS"),
        "WARN": sum(1 for r in results if r.severity == "WARN"),
        "ERROR": sum(1 for r in results if r.severity == "ERROR"),
    }


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> tuple[dict | None, str | None]:
    """Load YAML; return (data, error_message). Either is None on success."""
    if not path.exists():
        return None, f"file not found: {path}"
    try:
        import yaml  # local import keeps this module importable without yaml
    except ImportError:
        return None, "PyYAML not installed"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # yaml.YAMLError + UnicodeDecodeError + ...
        return None, f"YAML parse error: {e}"
    if data is None:
        return None, "file is empty"
    if not isinstance(data, dict):
        return None, f"top-level must be a mapping, got {type(data).__name__}"
    return data, None


# ---------------------------------------------------------------------------
# Field walking — used by evidence coverage and NOT_STATED detection
# ---------------------------------------------------------------------------


def _walk_fields(
    obj: Any,
    prefix: str = "",
) -> list[tuple[str, Any]]:
    """Yield (dotted_path, value) for every leaf and structural node.

    Lists with id'd elements use [id=X] indexing (matches deref's vocabulary).
    Empty containers are emitted as their own leaf so they can be evaluated
    against EVIDENCE_EXEMPT and NOT_STATED rules.
    """
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        if not obj:
            out.append((prefix, obj))
            return out
        for k, v in obj.items():
            sub = f"{prefix}.{k}" if prefix else k
            out.extend(_walk_fields(v, sub))
    elif isinstance(obj, list):
        if not obj:
            out.append((prefix, obj))
            return out
        # Use [id=X] when items are dicts with an "id" field; else [i].
        for i, v in enumerate(obj):
            if isinstance(v, dict) and "id" in v:
                sub = f"{prefix}[id={v['id']}]"
            else:
                sub = f"{prefix}[{i}]"
            out.extend(_walk_fields(v, sub))
    else:
        out.append((prefix, obj))
    return out


def _is_field_empty(value: Any) -> bool:
    """Field counts as 'non-empty' iff it has a real value the extractor filled."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _strip_array_indices(path: str) -> str:
    """Remove [id=X] / [i] suffixes so paths can match EVIDENCE_EXEMPT."""
    return re.sub(r"\[[^\]]+\]", "", path)


def _is_exempt_path(path: str) -> bool:
    stripped = _strip_array_indices(path)
    if stripped in EVIDENCE_EXEMPT:
        return True
    # Exempt parents — e.g. anything under "evidence" is exempt.
    for exempt in EVIDENCE_EXEMPT:
        if stripped.startswith(f"{exempt}."):
            return True
    return False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_round_trip_paper_spec(paper_spec_dict: dict) -> CheckResult:
    try:
        from researchos.schemas.paper_spec import PaperSpec
        PaperSpec.model_validate(paper_spec_dict)
        return CheckResult("paper_spec.round_trip", "PASS")
    except Exception as e:
        return CheckResult(
            "paper_spec.round_trip", "ERROR",
            f"PaperSpec validation failed: {str(e)[:400]}",
        )


def check_round_trip_proposal_spec(proposal_spec_dict: dict) -> CheckResult:
    try:
        from researchos.schemas.proposal_spec import ProposalSpec
        ProposalSpec.model_validate(proposal_spec_dict)
        return CheckResult("proposal_spec.round_trip", "PASS")
    except Exception as e:
        return CheckResult(
            "proposal_spec.round_trip", "ERROR",
            f"ProposalSpec validation failed: {str(e)[:400]}",
        )


def check_round_trip_contract(contract_dict: dict) -> CheckResult:
    try:
        from researchos.schemas.alignment_contract import AlignmentContract
        AlignmentContract.model_validate(contract_dict)
        return CheckResult("alignment_contract.round_trip", "PASS")
    except Exception as e:
        return CheckResult(
            "alignment_contract.round_trip", "ERROR",
            f"AlignmentContract validation failed: {str(e)[:400]}",
        )


def check_evidence_coverage(paper_spec: dict) -> CheckResult:
    """Quoted-field count / (non-empty - NOT_STATED - exempt) >= 0.90."""
    fields = _walk_fields(paper_spec)
    non_empty_paths = {
        path for path, val in fields
        if not _is_field_empty(val) and not is_not_stated(val)
    }
    exempt_paths = {p for p in non_empty_paths if _is_exempt_path(p)}
    denom_paths = non_empty_paths - exempt_paths

    quoted_field_paths = {
        e.get("field", "")
        for e in paper_spec.get("evidence", []) or []
        if isinstance(e, dict) and e.get("field")
    }
    quoted_count = sum(1 for p in denom_paths if any(
        # An evidence entry covers a path if its field equals or is a prefix
        _path_covered_by_evidence(p, ev) for ev in quoted_field_paths
    ))

    denom = len(denom_paths)
    if denom == 0:
        return CheckResult("paper_spec.evidence_coverage", "PASS",
                           "no eligible fields (denom=0)")
    ratio = quoted_count / denom
    detail = f"coverage={ratio:.2f} ({quoted_count}/{denom})"
    if ratio >= EVIDENCE_COVERAGE_THRESHOLD:
        return CheckResult("paper_spec.evidence_coverage", "PASS", detail)
    return CheckResult(
        "paper_spec.evidence_coverage", "ERROR",
        f"{detail}, below threshold {EVIDENCE_COVERAGE_THRESHOLD:.2f}",
    )


def _path_covered_by_evidence(field_path: str, evidence_field: str) -> bool:
    """A field is covered if evidence.field == field_path or is a strict prefix."""
    fp = _strip_array_indices(field_path)
    ef = _strip_array_indices(evidence_field)
    return fp == ef or fp.startswith(f"{ef}.") or fp.startswith(f"{ef}[")


def check_quote_substring_match(
    paper_spec: dict,
    base_paper_md: str,
) -> list[CheckResult]:
    """Substring-match every paper_spec.evidence quote against base_paper.md."""
    out: list[CheckResult] = []
    quotes = paper_spec.get("evidence", []) or []
    if not quotes:
        out.append(CheckResult(
            "paper_spec.evidence.quote_match", "WARN",
            "evidence list is empty",
        ))
        return out

    eligible = [
        q for q in quotes
        if isinstance(q, dict)
        and q.get("quote")
        and quote_token_count(q["quote"]) >= MIN_QUOTE_TOKENS
    ]
    if not eligible:
        out.append(CheckResult(
            "paper_spec.evidence.quote_match", "WARN",
            f"no eligible quotes (min tokens = {MIN_QUOTE_TOKENS})",
        ))
        return out

    matched = sum(1 for q in eligible if match_quote(q["quote"], base_paper_md))
    ratio = matched / len(eligible)
    detail = f"matched={matched}/{len(eligible)} ({ratio:.2f})"

    if ratio >= MATCH_PASS_RATIO:
        out.append(CheckResult("paper_spec.evidence.quote_match", "PASS", detail))
    elif ratio >= MATCH_WARN_RATIO:
        out.append(CheckResult("paper_spec.evidence.quote_match", "WARN", detail))
    else:
        out.append(CheckResult("paper_spec.evidence.quote_match", "ERROR", detail))

    # Also surface the unmatched quotes individually as a single WARN line each
    # so the report is debuggable.
    for q in eligible:
        if not match_quote(q["quote"], base_paper_md):
            preview = normalize_quote(q["quote"])[:80]
            out.append(CheckResult(
                f"paper_spec.evidence[{q.get('field', '?')}].unmatched",
                "WARN" if ratio >= MATCH_WARN_RATIO else "ERROR",
                f"quote not found in base_paper.md: {preview!r}",
            ))
    return out


def check_rq_verbatim_match(
    paper_spec: dict,
    base_paper_md: str,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    rqs = paper_spec.get("research_questions", []) or []
    for rq in rqs:
        if not isinstance(rq, dict):
            continue
        verbatim = rq.get("question_verbatim", "") or ""
        rq_id = rq.get("id", "?")
        if not verbatim:
            out.append(CheckResult(
                f"paper_spec.research_questions[{rq_id}].verbatim_match",
                "ERROR",
                "question_verbatim is empty",
            ))
            continue
        if not match_quote(verbatim, base_paper_md):
            preview = normalize_quote(verbatim)[:120]
            out.append(CheckResult(
                f"paper_spec.research_questions[{rq_id}].verbatim_match",
                "ERROR",
                f"question_verbatim not found in base_paper.md: {preview!r}",
            ))
        else:
            out.append(CheckResult(
                f"paper_spec.research_questions[{rq_id}].verbatim_match",
                "PASS",
            ))
    return out


def check_replaces_adds_to_resolve(
    proposal_spec: dict,
    paper_spec: dict,
) -> list[CheckResult]:
    """new_components.replaces / adds_to must point to existing method_step IDs."""
    out: list[CheckResult] = []
    step_ids = {
        s.get("id")
        for s in (paper_spec.get("baseline_method", {}) or {}).get("method_steps", []) or []
        if isinstance(s, dict)
    }
    new_comps = (proposal_spec.get("proposed_method", {}) or {}).get("new_components", []) or []
    for c in new_comps:
        if not isinstance(c, dict):
            continue
        cid = c.get("id", "?")
        for field_name in ("replaces", "adds_to"):
            ref = c.get(field_name)
            if ref is None:
                continue
            check_name = f"proposal_spec.new_components[{cid}].{field_name}"
            if ref not in step_ids:
                out.append(CheckResult(
                    check_name, "ERROR",
                    f"{ref!r} not in paper_spec.baseline_method.method_steps "
                    f"(known: {sorted(step_ids)})",
                ))
            else:
                out.append(CheckResult(check_name, "PASS"))
    return out


def check_borrowed_paths(
    proposal_spec: dict,
    paper_spec: dict,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    specs = {"paper_spec": paper_spec}
    borrowed = (proposal_spec.get("proposed_method", {}) or {}).get("borrowed_from_paper", []) or []
    for i, b in enumerate(borrowed):
        if not isinstance(b, dict):
            continue
        path = b.get("paper_field", "")
        check_name = f"proposal_spec.borrowed_from_paper[{i}].paper_field"
        if not path:
            out.append(CheckResult(check_name, "ERROR", "empty paper_field"))
            continue
        # The proposal extractor records paths without the "paper_spec." root.
        full = path if path.startswith("paper_spec.") else f"paper_spec.{path}"
        try:
            # Try leaf, then subtree
            try:
                deref(full, specs, expect="leaf")
            except DerefError:
                deref(full, specs, expect="subtree")
            out.append(CheckResult(check_name, "PASS"))
        except DerefError as e:
            out.append(CheckResult(check_name, "ERROR", str(e)))
    return out


def check_divergence_paths(
    proposal_spec: dict,
    paper_spec: dict,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    specs = {"paper_spec": paper_spec}
    divs = (proposal_spec.get("proposed_method", {}) or {}).get("divergences", []) or []
    for i, d in enumerate(divs):
        if not isinstance(d, dict):
            continue
        path = d.get("paper_field", "")
        check_name = f"proposal_spec.divergences[{i}].paper_field"
        if not path:
            out.append(CheckResult(check_name, "ERROR", "empty paper_field"))
            continue
        full = path if path.startswith("paper_spec.") else f"paper_spec.{path}"
        try:
            try:
                deref(full, specs, expect="leaf")
            except DerefError:
                deref(full, specs, expect="subtree")
            out.append(CheckResult(check_name, "PASS"))
        except DerefError as e:
            out.append(CheckResult(check_name, "ERROR", str(e)))
    return out


def check_value_refs_resolve(
    contract: dict,
    paper_spec: dict,
    proposal_spec: dict,
    code_recon: dict | None,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    specs: dict[str, Any] = {"paper_spec": paper_spec, "proposal_spec": proposal_spec}
    if code_recon is not None:
        specs["code_recon"] = code_recon

    def _check_one(path: str | None, expect: str, label: str) -> None:
        if not path:
            return
        try:
            deref(path, specs, expect=expect)  # type: ignore[arg-type]
            out.append(CheckResult(label, "PASS"))
        except DerefError as e:
            out.append(CheckResult(label, "ERROR", str(e)))

    for dim in contract.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        name = dim.get("dim", "?")
        _check_one(dim.get("value_ref"), "leaf",
                   f"contract.dim[{name}].value_ref")
        _check_one(dim.get("value_ref_subtree"), "subtree",
                   f"contract.dim[{name}].value_ref_subtree")
        _check_one(dim.get("base_value_ref_subtree"), "subtree",
                   f"contract.dim[{name}].base_value_ref_subtree")
        _check_one(dim.get("paper_metrics_ref_subtree"), "subtree",
                   f"contract.dim[{name}].paper_metrics_ref_subtree")
        # Arms inside ARM_DEPENDENT
        for arm_name, arm in (dim.get("arms") or {}).items():
            if not isinstance(arm, dict):
                continue
            _check_one(arm.get("value_ref"), "leaf",
                       f"contract.dim[{name}].arms.{arm_name}.value_ref")
            _check_one(arm.get("value_ref_subtree"), "subtree",
                       f"contract.dim[{name}].arms.{arm_name}.value_ref_subtree")
    return out


#: Dimensions whose lock semantics are expressed via custom shape (in_scope,
#: arms, baseline/proposal/glue blocks) rather than value_ref. Skipped from
#: the generic "LOCKED → must deref to a real value" check.
_DIMS_WITHOUT_VALUE_REF: frozenset[str] = frozenset({
    "rq_coverage",
    "code_implementation_strategy",
})


def check_locked_not_not_stated(
    contract: dict,
    paper_spec: dict,
    proposal_spec: dict,
) -> list[CheckResult]:
    """LOCKED_TO_PAPER* dimensions must not deref to NOT_STATED / None.

    Dimensions in ``_DIMS_WITHOUT_VALUE_REF`` are skipped because their
    lock semantics live in custom fields (e.g., ``rq_coverage.in_scope``)
    rather than ``value_ref``. Cross-checks for those dims are handled by
    dedicated functions (``check_rq_metric_coverage`` etc.).
    """
    out: list[CheckResult] = []
    specs = {"paper_spec": paper_spec, "proposal_spec": proposal_spec}
    for dim in contract.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        lock = dim.get("lock", "")
        if lock not in LOCKS_REQUIRING_PAPER_VALUE:
            continue
        name = dim.get("dim", "?")
        if name in _DIMS_WITHOUT_VALUE_REF:
            continue
        ref = dim.get("value_ref") or dim.get("value_ref_subtree")
        if not ref:
            # Some LOCKED_TO_PAPER_PLUS_PROPOSAL use paper_metrics_ref_subtree
            ref = dim.get("paper_metrics_ref_subtree")
        if not ref:
            out.append(CheckResult(
                f"contract.dim[{name}].locked_value_present", "ERROR",
                f"lock={lock} but no value_ref / value_ref_subtree set",
            ))
            continue
        try:
            value = deref(
                ref, specs,
                expect="leaf" if dim.get("value_ref") else "subtree",
            )
        except DerefError as e:
            out.append(CheckResult(
                f"contract.dim[{name}].locked_value_present", "ERROR",
                f"deref failed: {e}",
            ))
            continue
        if is_not_stated(value):
            out.append(CheckResult(
                f"contract.dim[{name}].locked_value_present", "ERROR",
                f"{ref!r} resolved to NOT_STATED — cannot lock to undefined value",
            ))
        elif isinstance(value, dict) and any(
            is_not_stated(v) for v in value.values()
        ):
            # Subtree containing NOT_STATED entries — WARN, not ERROR (some
            # children may legitimately be NOT_STATED while the lock as a
            # whole is meaningful).
            ns_keys = [k for k, v in value.items() if is_not_stated(v)]
            out.append(CheckResult(
                f"contract.dim[{name}].locked_value_present", "WARN",
                f"{ref!r} subtree has NOT_STATED children: {ns_keys}",
            ))
        else:
            out.append(CheckResult(
                f"contract.dim[{name}].locked_value_present", "PASS",
            ))
    return out


def check_ref_code_symbol(
    contract: dict,
    code_recon: dict | None,
) -> list[CheckResult]:
    """LOCKED_TO_PAPER_VIA_REF_CODE.code_handle.symbol must appear in code_recon."""
    out: list[CheckResult] = []
    if code_recon is None:
        # Without code_recon we cannot verify; emit one informational WARN.
        out.append(CheckResult(
            "contract.ref_code_symbols", "WARN",
            "code_recon.yaml not provided — symbol verification skipped",
        ))
        return out

    # Index every (file, symbol) pair available.
    available: set[tuple[str, str]] = set()
    available_paths: set[str] = set()
    for repo in code_recon.get("repos", []) or []:
        if not isinstance(repo, dict):
            continue
        for ep in repo.get("entry_points", []) or []:
            if isinstance(ep, dict) and ep.get("file") and ep.get("symbol"):
                available.add((ep["file"], ep["symbol"]))
                available_paths.add(ep["file"])
        # Also index paths/symbols from `reusable_functions` if present.
        for trip in repo.get("reusable_functions", []) or []:
            if isinstance(trip, str):
                # "<slug>::<path>::L<a>-L<b>" format
                parts = trip.split("::")
                if len(parts) >= 2:
                    available_paths.add(parts[1])

    for dim in contract.get("dimensions", []) or []:
        if not isinstance(dim, dict):
            continue
        if dim.get("lock") != "LOCKED_TO_PAPER_VIA_REF_CODE":
            continue
        name = dim.get("dim", "?")
        handle = dim.get("code_handle") or {}
        symbol = handle.get("symbol")
        import_path = handle.get("import_path")
        check_name = f"contract.dim[{name}].ref_code_symbol"
        if not symbol:
            out.append(CheckResult(check_name, "ERROR", "code_handle.symbol missing"))
            continue
        # Try matching by symbol presence — code_recon entry_points may
        # not store the import_path directly, so we accept symbol-only match
        # but record the import_path for debuggability.
        symbol_known = any(s == symbol for _, s in available)
        if symbol_known:
            out.append(CheckResult(check_name, "PASS",
                                   f"symbol={symbol!r} found in code_recon"))
        else:
            out.append(CheckResult(
                check_name, "ERROR",
                f"symbol={symbol!r} (import_path={import_path!r}) not found "
                f"in any code_recon entry_points",
            ))
    return out


def check_ref_code_availability_vs_change_type(
    paper_spec: dict,
    contract: dict,
) -> CheckResult:
    available = (paper_spec.get("reference_code") or {}).get("available", False)
    change_type = (contract.get("classification") or {}).get("change_type", "")
    if available:
        return CheckResult(
            "contract.ref_code_availability",
            "PASS",
            "ref_code available; downstream import checks apply",
        )
    if change_type in ("replacement_in_method", "extension_of_method"):
        return CheckResult(
            "contract.ref_code_availability",
            "ERROR",
            f"reference_code.available=False with change_type={change_type!r}; "
            f"baseline faithfulness cannot be guaranteed. Provide ref_code "
            f"or reclassify change_type as cross_method/cross_domain/new_task.",
        )
    # cross_*/new_task → reimpl path, WARN
    return CheckResult(
        "contract.ref_code_availability",
        "WARN",
        f"ref_code unavailable; running with REIMPL strategy "
        f"(change_type={change_type!r}). Confidence should be 'medium' and "
        f"judge_must_address should include 'reimpl_drift_risk'.",
    )


def check_rq_metric_coverage(
    contract: dict,
    paper_spec: dict,
) -> list[CheckResult]:
    """Each in_scope RQ has at least one of its paper_metrics covered in metrics dim."""
    out: list[CheckResult] = []
    # Collect metric names from contract.metrics dim.
    metrics_names: set[str] = set()
    for dim in contract.get("dimensions", []) or []:
        if not isinstance(dim, dict) or dim.get("dim") != "metrics":
            continue
        # paper-side: dereference paper_metrics_ref_subtree (a list of {name, ...})
        ref = dim.get("paper_metrics_ref_subtree")
        if ref:
            try:
                value = deref(ref, {"paper_spec": paper_spec}, expect="subtree")
                if isinstance(value, list):
                    for m in value:
                        if isinstance(m, dict) and m.get("name"):
                            metrics_names.add(m["name"])
            except DerefError:
                pass
        # Or fall back to paper_spec.evaluation_metrics directly.
        if not metrics_names:
            for m in paper_spec.get("evaluation_metrics", []) or []:
                if isinstance(m, dict) and m.get("name"):
                    metrics_names.add(m["name"])
        # proposal_added: inline list of {name, ...}
        for m in dim.get("proposal_added", []) or []:
            if isinstance(m, dict) and m.get("name"):
                metrics_names.add(m["name"])

    rq_cov = next(
        (d for d in contract.get("dimensions", []) or []
         if isinstance(d, dict) and d.get("dim") == "rq_coverage"),
        None,
    )
    in_scope = (rq_cov or {}).get("in_scope", []) or []
    if not in_scope:
        out.append(CheckResult(
            "contract.rq_metric_coverage", "WARN",
            "no in_scope RQs to verify",
        ))
        return out

    rq_index = {
        rq.get("id"): rq for rq in (paper_spec.get("research_questions") or [])
        if isinstance(rq, dict)
    }
    for rq_id in in_scope:
        rq = rq_index.get(rq_id)
        if rq is None:
            out.append(CheckResult(
                f"contract.rq_metric_coverage[{rq_id}]", "ERROR",
                f"in_scope RQ {rq_id!r} not present in paper_spec.research_questions",
            ))
            continue
        rq_metrics = set(rq.get("paper_metrics", []) or [])
        if not rq_metrics:
            # RQ with no metrics declared in paper_spec — WARN, not ERROR
            out.append(CheckResult(
                f"contract.rq_metric_coverage[{rq_id}]", "WARN",
                f"RQ {rq_id} has no paper_metrics; cannot verify coverage",
            ))
            continue
        if rq_metrics & metrics_names:
            out.append(CheckResult(
                f"contract.rq_metric_coverage[{rq_id}]", "PASS",
                f"covered by {sorted(rq_metrics & metrics_names)}",
            ))
        else:
            out.append(CheckResult(
                f"contract.rq_metric_coverage[{rq_id}]", "ERROR",
                f"none of RQ's paper_metrics {sorted(rq_metrics)} "
                f"appear in contract.metrics ({sorted(metrics_names)})",
            ))
    return out


def check_arm_dependent_ablation(contract: dict) -> list[CheckResult]:
    """Every ARM_DEPENDENT dim with ablation_required must have an arm in arms_to_run."""
    out: list[CheckResult] = []
    arms_to_run = {
        a.get("name"): a
        for a in ((contract.get("executive_summary") or {}).get("arms_to_run") or [])
        if isinstance(a, dict)
    }
    for dim in contract.get("dimensions", []) or []:
        if not isinstance(dim, dict) or dim.get("lock") != "ARM_DEPENDENT":
            continue
        dim_name = dim.get("dim", "?")
        arms = dim.get("arms", {}) or {}
        # Need at least one arm in the EXECUTIVE_SUMMARY whose required_by
        # cites this dim. The schema already enforces that one of the arms
        # in dim.arms has ablation_required=True, but we need the
        # executive_summary to reflect that as a runnable arm.
        marker = f"ARM_DEPENDENT.{dim_name}"
        match = [
            a for a in arms_to_run.values()
            if a.get("required_by") == marker
        ]
        check_name = f"contract.arm_dependent[{dim_name}].ablation_arm_present"
        if match:
            out.append(CheckResult(check_name, "PASS",
                                   f"{len(match)} arm(s) reference {marker}"))
        else:
            # Or the synthesizer encoded the ablation arm under a
            # different naming convention — accept any arm that contains
            # the dim_name in its name as a softer signal.
            soft = [a for a in arms_to_run.values()
                    if dim_name in (a.get("name") or "")]
            if soft:
                out.append(CheckResult(
                    check_name, "WARN",
                    f"no arm.required_by={marker!r}; matched by name: "
                    f"{[a['name'] for a in soft]}",
                ))
            else:
                out.append(CheckResult(
                    check_name, "ERROR",
                    f"no arm in executive_summary.arms_to_run cites {marker!r} "
                    f"(arms present: {sorted(arms_to_run)})",
                ))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_validator(pipeline_dir: Path | str) -> ValidationReport:
    """Run every alignment check and write alignment_validation_report.json.

    Args:
        pipeline_dir: artifacts/pipeline_<id>/. Must contain
            paper_spec.yaml, proposal_spec.yaml, alignment_contract.yaml.
            base_paper.md is required for evidence quote matching.
            code_recon.yaml is optional but skipping it surfaces a WARN.

    Returns:
        ValidationReport with verdict in {PASS, WARN, BLOCK} and the list
        of individual CheckResults. Always writes the report JSON.
    """
    pdir = Path(pipeline_dir)
    results: list[CheckResult] = []

    # --- 1. Load all inputs ----------------------------------------------
    paper_spec, err = _load_yaml(pdir / "paper_spec.yaml")
    if err:
        results.append(CheckResult("paper_spec.load", "ERROR", err))
    proposal_spec, err = _load_yaml(pdir / "proposal_spec.yaml")
    if err:
        results.append(CheckResult("proposal_spec.load", "ERROR", err))
    contract, err = _load_yaml(pdir / "alignment_contract.yaml")
    if err:
        results.append(CheckResult("alignment_contract.load", "ERROR", err))

    # If any of the three failed to load, write an early-exit report.
    if paper_spec is None or proposal_spec is None or contract is None:
        return _finalize_report(pdir, results)

    base_paper_path = pdir / "base_paper.md"
    if not base_paper_path.exists():
        results.append(CheckResult(
            "base_paper.present", "ERROR",
            f"missing {base_paper_path}; cannot verify quotes",
        ))
        return _finalize_report(pdir, results)
    base_paper_md = base_paper_path.read_text(encoding="utf-8", errors="replace")

    code_recon, _ = _load_yaml(pdir / "code_recon.yaml")  # optional

    # --- 2. Round-trip three YAMLs through pydantic ---------------------
    results.append(check_round_trip_paper_spec(paper_spec))
    results.append(check_round_trip_proposal_spec(proposal_spec))
    results.append(check_round_trip_contract(contract))

    # If round-trip failed, downstream cross-spec checks will mostly noise.
    # Skip them in that case to keep the report focused.
    if any(r.severity == "ERROR" and r.check.endswith(".round_trip")
           for r in results):
        return _finalize_report(pdir, results)

    # --- 3. paper_spec internal consistency -----------------------------
    results.append(check_evidence_coverage(paper_spec))
    results.extend(check_quote_substring_match(paper_spec, base_paper_md))
    results.extend(check_rq_verbatim_match(paper_spec, base_paper_md))

    # --- 4. proposal_spec ↔ paper_spec ---------------------------------
    results.extend(check_replaces_adds_to_resolve(proposal_spec, paper_spec))
    results.extend(check_borrowed_paths(proposal_spec, paper_spec))
    results.extend(check_divergence_paths(proposal_spec, paper_spec))

    # --- 5. contract ↔ specs ↔ code_recon -------------------------------
    results.extend(check_value_refs_resolve(contract, paper_spec, proposal_spec, code_recon))
    results.extend(check_locked_not_not_stated(contract, paper_spec, proposal_spec))
    results.extend(check_ref_code_symbol(contract, code_recon))
    results.append(check_ref_code_availability_vs_change_type(paper_spec, contract))
    results.extend(check_rq_metric_coverage(contract, paper_spec))
    results.extend(check_arm_dependent_ablation(contract))

    return _finalize_report(pdir, results)


def _finalize_report(pdir: Path, results: list[CheckResult]) -> ValidationReport:
    report = ValidationReport(
        verdict=_aggregate_verdict(results),
        checks=results,
        summary=_summary(results),
    )
    out_path = pdir / "alignment_validation_report.json"
    try:
        out_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("alignment_validator: failed to write report: %s", e)
    return report


__all__ = [
    "CheckResult",
    "EVIDENCE_COVERAGE_THRESHOLD",
    "EVIDENCE_EXEMPT",
    "LIGATURES",
    "LOCKS_REQUIRING_PAPER_VALUE",
    "MATCH_PASS_RATIO",
    "MATCH_WARN_RATIO",
    "MIN_QUOTE_TOKENS",
    "DerefError",
    "ValidationReport",
    "check_arm_dependent_ablation",
    "check_borrowed_paths",
    "check_divergence_paths",
    "check_evidence_coverage",
    "check_locked_not_not_stated",
    "check_quote_substring_match",
    "check_ref_code_availability_vs_change_type",
    "check_ref_code_symbol",
    "check_replaces_adds_to_resolve",
    "check_round_trip_contract",
    "check_round_trip_paper_spec",
    "check_round_trip_proposal_spec",
    "check_rq_metric_coverage",
    "check_rq_verbatim_match",
    "check_value_refs_resolve",
    "deref",
    "is_not_stated",
    "match_quote",
    "normalize_quote",
    "quote_token_count",
    "run_validator",
]
