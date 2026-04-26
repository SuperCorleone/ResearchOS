"""Code-reference reconnaissance.

Pure Python, no LLM. Clones the reference codebases hinted at by a paper
(via ``knowledge_compiler.extract_code_refs``) or by an explicit
``experiment_plan.yaml::reference_codebases`` list, then produces a
``code_recon.yaml`` that the ``concept_extractor`` agent consumes.

The evidence-ref → wiki bridge uses the same ``_slugify`` rule as
``knowledge_compiler.compile_raw_to_wiki`` (lowercase, non-alnum → ``_``,
truncated to 60 chars). Without this, evidence_refs whose ``path_or_url``
points at a long PDF filename never match the slugified wiki page name and
the wiki's ``code_refs:`` frontmatter is silently dropped — which was the
direct cause of empty ``repos: []`` in the v5.1 stacking pipeline test.

Design constraints (read-only, safe defaults):

- **Never execute code** from the cloned repository. We only shell out to
  ``git`` and read files on disk.
- **Shallow clone** (``--depth=1``) and a 500 MB size cap per repo.
- Skip well-known heavy extensions (model weights, tarballs, image
  archives) on disk after clone so we don't store them in scratch.
- On any git failure, record a stub row with ``status: error`` rather
  than crashing — the pipeline must still advance.

The result is always written so the scheduler can treat the step as
idempotent: re-running overwrites the report.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import yaml

from researchos.runtime.knowledge_compiler import _slugify, extract_code_refs

logger = logging.getLogger(__name__)


def _load_baseline_recon_config() -> dict:
    """Read baseline_recon block from runtime/workflow_config.yaml.
    Falls back to safe defaults if config or block is missing."""
    from researchos.runtime.workflow_loader import WorkflowConfig
    try:
        # WorkflowConfig.load expects project_root; resolve relative to CWD.
        cfg = WorkflowConfig.load(Path.cwd())
        return cfg.get_baseline_recon_config()
    except Exception:
        return {
            "max_size_mb": 500,
            "max_files": 5000,
            "allowed_hosts": (
                "github.com", "gitlab.com", "bitbucket.org",
                "zenodo.org", "osf.io", "figshare.com",
                "huggingface.co", "codeocean.com", "doi.org",
            ),
            "prune_extensions": (
                ".pth", ".pt", ".bin", ".safetensors", ".ckpt", ".h5", ".onnx",
                ".tar", ".tar.gz", ".tgz", ".zip", ".7z", ".rar",
                ".mp4", ".mov", ".avi", ".mkv",
                ".iso", ".dmg",
                ".parquet", ".arrow", ".feather",
            ),
        }


def _max_repo_bytes() -> int:
    return int(_load_baseline_recon_config().get("max_size_mb", 500)) * 1024 * 1024


def _max_file_count() -> int:
    return int(_load_baseline_recon_config().get("max_files", 5000))


def _host_allowed(url: str, allowed_hosts: Iterable[str] | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). reason is empty string if allowed.

    doi.org URLs are accepted without a HEAD-follow at this layer; the
    actual git clone will fail loudly if the resolved target is not git.
    """
    if allowed_hosts is None:
        allowed_hosts = _load_baseline_recon_config().get("allowed_hosts", ())
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"unparseable URL: {e}"
    host = (parsed.hostname or "").lower()
    # Strip leading www. for matching
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False, "empty hostname"
    for allowed in allowed_hosts:
        allowed_norm = allowed.lower()
        if allowed_norm.startswith("www."):
            allowed_norm = allowed_norm[4:]
        if host == allowed_norm or host.endswith("." + allowed_norm):
            return True, ""
    return False, f"host '{host}' not in allowed_hosts"


# Reference codebases can be huge (weights, datasets). Cap per-repo disk
# usage. 500 MB is generous for code-only scans; anything over is likely
# checkpoints or a bundled dataset and gets pruned after clone.
# Kept for backward compatibility; runtime callers should use _max_repo_bytes().
MAX_REPO_BYTES = 500 * 1024 * 1024

# Extensions we prune post-clone. They carry weights, datasets, or binary
# archives that we don't need for static analysis.
_PRUNE_EXTENSIONS: tuple[str, ...] = (
    ".pth", ".pt", ".bin", ".safetensors", ".ckpt", ".h5", ".onnx",
    ".tar", ".tar.gz", ".tgz", ".zip", ".7z", ".rar",
    ".mp4", ".mov", ".avi", ".mkv",
    ".iso", ".dmg",
    ".parquet", ".arrow", ".feather",
)

# Files we treat as potential entry points (shown in the report so the
# concept_extractor can cite a real file::func locator).
_ENTRY_POINT_NAMES: tuple[str, ...] = (
    "train.py", "main.py", "run.py", "run_experiment.py", "experiment.py",
    "inference.py", "eval.py", "evaluate.py", "benchmark.py",
)

# README / license filenames we scan for dataset hints and license text.
_README_NAMES: tuple[str, ...] = (
    "README.md", "README.rst", "README.txt", "README",
    "readme.md", "Readme.md",
)
_LICENSE_NAMES: tuple[str, ...] = (
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE",
    "license", "license.md",
)


def _owner_repo_slug(url: str) -> str:
    """Turn https://github.com/foo/bar into ``foo__bar`` for a dir name."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    if len(parts) < 2:
        return url.replace("/", "_").replace(":", "_")
    owner = parts[-2]
    repo = parts[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{owner}__{repo}"


def clone_repository(
    url: str,
    target_dir: Path,
    *,
    allowed_hosts: Iterable[str] | None = None,
    max_size_mb: int | None = None,
    max_files: int | None = None,
    prune_extensions: Iterable[str] | None = None,
    timeout: int = 180,
) -> dict:
    """Shared clone helper used by code_recon and (future) baseline_recon agents.

    Returns a result dict with at least: url, status, path (or None), reason
    (when status != ok), size_mb, file_count, license_path (when found),
    readme_path (when found), language (when detected).

    status ∈ {ok, cached, rejected_by_whitelist, error, oversized, too_many_files}
    """
    # Resolve limits from config if not explicitly supplied.
    cfg = _load_baseline_recon_config()
    if max_size_mb is None:
        max_size_mb = int(cfg.get("max_size_mb", 500))
    if max_files is None:
        max_files = int(cfg.get("max_files", 5000))
    if prune_extensions is None:
        prune_extensions = cfg.get("prune_extensions", _PRUNE_EXTENSIONS)

    max_bytes = max_size_mb * 1024 * 1024

    # Whitelist check.
    allowed, reason = _host_allowed(url, allowed_hosts)
    if not allowed:
        return {
            "url": url,
            "status": "rejected_by_whitelist",
            "reason": reason,
            "path": None,
        }

    dest = target_dir
    dest.parent.mkdir(parents=True, exist_ok=True)

    cached = dest.exists()
    if not cached:
        # Perform the actual git clone.
        try:
            proc = subprocess.run(
                [
                    "git", "clone", "--depth=1", "--single-branch",
                    "--no-tags", url, str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {"url": url, "status": "error", "reason": "git executable not found", "path": None}
        except subprocess.TimeoutExpired:
            return {"url": url, "status": "error", "reason": f"git clone timed out after {timeout}s", "path": None}
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            return {"url": url, "status": "error", "reason": err[0][:200], "path": None}

    # Count files before pruning (use rglob to count).
    file_count_pre = sum(1 for f in dest.rglob("*") if f.is_file() and not f.is_symlink())
    if file_count_pre > max_files:
        shutil.rmtree(dest, ignore_errors=True)
        return {
            "url": url,
            "status": "too_many_files",
            "reason": f"file count {file_count_pre} > max_files {max_files}",
            "path": None,
        }

    # Prune heavy extensions.
    pruned_bytes = 0
    prune_ext_tuple = tuple(prune_extensions)
    for f in dest.rglob("*"):
        try:
            if not f.is_file() or f.is_symlink():
                continue
            name = f.name.lower()
            if name.endswith(prune_ext_tuple):
                pruned_bytes += f.stat().st_size
                f.unlink()
        except OSError:
            continue

    size = _dir_size(dest)
    if size > max_bytes:
        shutil.rmtree(dest, ignore_errors=True)
        return {
            "url": url,
            "status": "oversized",
            "reason": f"size {size} > max_bytes {max_bytes} after prune",
            "path": None,
            "size_mb": round(size / (1024 * 1024), 2),
        }

    file_count = sum(1 for f in dest.rglob("*") if f.is_file() and not f.is_symlink())

    # Detect README / LICENSE.
    readme = _find_first(dest, _README_NAMES)
    license_file = _find_first(dest, _LICENSE_NAMES)

    result: dict = {
        "url": url,
        "status": "cached" if cached else "ok",
        "path": str(dest),
        "size_mb": round(size / (1024 * 1024), 2),
        "file_count": file_count,
        "pruned_bytes": pruned_bytes,
        "commit_sha": _head_commit(dest),
        "entry_points": _detect_entry_points(dest),
        "license": _detect_license(dest),
        "readme_path": str(readme) if readme else None,
        "license_path": str(license_file) if license_file else None,
        "language": None,  # Language detection reserved for future enhancement.
    }
    return result


def _shallow_clone(url: str, dest: Path, timeout: int = 180) -> dict:
    """Run ``git clone --depth=1`` into ``dest``.

    Returns a dict with ``status`` (``ok`` / ``error``) and diagnostic
    fields. Never raises.

    Delegates to ``clone_repository`` for whitelist enforcement and size
    checking; adapts the return dict to the legacy schema expected by
    ``run_code_recon``.
    """
    result = clone_repository(url, dest, timeout=timeout)
    status = result["status"]
    if status in ("ok", "cached"):
        return {"status": status, "path": result["path"]}
    if status == "rejected_by_whitelist":
        return {"status": "rejected_by_whitelist", "reason": result.get("reason", ""), "path": None}
    # oversized / too_many_files / error all map to error for legacy callers.
    return {"status": status, "error": result.get("reason", ""), "path": None}


def _head_commit(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _prune_heavy(repo: Path) -> int:
    """Delete files matching ``_PRUNE_EXTENSIONS``; return byte count removed."""
    removed = 0
    for f in repo.rglob("*"):
        try:
            if not f.is_file() or f.is_symlink():
                continue
            name = f.name.lower()
            # Handle double suffixes like .tar.gz.
            if name.endswith(_PRUNE_EXTENSIONS):
                removed += f.stat().st_size
                f.unlink()
        except OSError:
            continue
    return removed


def _find_first(repo: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        candidate = repo / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


_ENTRY_SKIP_SEGMENTS: tuple[str, ...] = (
    "site-packages/", "/.venv/", "/venv/", "/__pycache__/",
    "/tests/", "/test/", "/.git/", "/build/", "/dist/",
    "/node_modules/", "/docs/", "/doc/",
)


def _is_skipped_path(rel_str: str) -> bool:
    """Tests/build/venv segments do not count as entry points."""
    if rel_str.startswith("tests/") or rel_str.startswith("test/"):
        return True
    return any(seg in rel_str for seg in _ENTRY_SKIP_SEGMENTS)


def _has_main_block(path: Path) -> bool:
    """True if the file contains an ``if __name__ == "__main__":`` block.

    Read in binary + replace to avoid choking on encodings; cap at 256 KB
    to keep us out of huge generated files.
    """
    try:
        with path.open("rb") as fh:
            blob = fh.read(256 * 1024)
    except OSError:
        return False
    text = blob.decode("utf-8", errors="replace")
    # Tolerate whitespace and quote variants.
    return bool(re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", text))


def _detect_entry_points(repo: Path, limit: int = 10) -> list[str]:
    """Return relative paths of plausible entry-point scripts.

    Detection is layered (priority order, dedup on first occurrence):

    1. Files matching the conventional names (``train.py`` / ``main.py`` /
       …) anywhere in the tree — original v5.3 behaviour.
    2. Top-level ``*.py`` files containing an ``if __name__ == "__main__":``
       block. Catches packages whose entry script is named after the
       method (e.g. BMA's ``bma_adaptation.py``). Without this we returned
       ``[]`` for any non-conventional repo and the BASELINE_ANALYZE LLM
       had nothing to grep — see pipeline_mitigating_model_uncertainty_i.
    3. Bare top-level ``*.py`` fallback. Last-resort hint so the LLM has
       *something* to grep when no main block is present (e.g. library
       repos without a CLI entry).
    """
    found: list[str] = []
    seen: set[str] = set()

    def _push(rel_str: str) -> None:
        if rel_str in seen or _is_skipped_path(rel_str):
            return
        seen.add(rel_str)
        found.append(rel_str)

    # Layer 1: conventional names (full-tree).
    for name in _ENTRY_POINT_NAMES:
        for candidate in repo.rglob(name):
            try:
                rel = candidate.relative_to(repo)
            except ValueError:
                continue
            _push(str(rel))
            if len(found) >= limit:
                return found

    # Layer 2: top-level *.py with `if __name__ == "__main__":` block.
    main_block_hits: list[str] = []
    bare_hits: list[str] = []
    try:
        top_level_py = sorted(p for p in repo.glob("*.py") if p.is_file())
    except OSError:
        top_level_py = []
    for candidate in top_level_py:
        rel_str = candidate.name  # top-level → no dir prefix
        if rel_str in seen or _is_skipped_path(rel_str):
            continue
        if _has_main_block(candidate):
            main_block_hits.append(rel_str)
        else:
            bare_hits.append(rel_str)

    for rel_str in main_block_hits:
        _push(rel_str)
        if len(found) >= limit:
            return found

    # Layer 3: bare top-level *.py fallback.
    for rel_str in bare_hits:
        _push(rel_str)
        if len(found) >= limit:
            return found

    return found


def _detect_license(repo: Path) -> str:
    lic = _find_first(repo, _LICENSE_NAMES)
    if lic is None:
        return ""
    try:
        text = lic.read_text(errors="replace")
    except OSError:
        return ""
    # Heuristic identifier from first non-empty line.
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s[:120]
    return ""


def gather_urls_for_pipeline(pipeline_dir: Path, project_root: Path) -> list[str]:
    """Collect reference-codebase URLs that belong to *pipeline_dir*.

    Sources (in priority order):

    1. ``experiment_plan.yaml::reference_codebases`` (explicit).
    2. ``evidence_refs`` entries in ``state.json`` that already carry
       a URL (GitHub etc.).
    3. Code refs found in the wiki pages of the papers this pipeline
       cites — we use the paper's evidence-ref stem to locate the page.

    Returns a deduplicated list in source-priority order.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _push(candidate: str) -> None:
        candidate = candidate.strip()
        if not candidate:
            return
        # Normalise trailing slash and drop fragments/queries.
        candidate = candidate.rstrip("/").split("#")[0].split("?")[0]
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)

    # 1. experiment_plan.yaml
    plan_path = pipeline_dir / "experiment_plan.yaml"
    if plan_path.exists():
        try:
            plan = yaml.safe_load(plan_path.read_text()) or {}
        except Exception:
            plan = {}
        for item in plan.get("reference_codebases", []) or []:
            if isinstance(item, str):
                _push(item)
            elif isinstance(item, dict) and item.get("url"):
                _push(item["url"])

    # 2. state.json / evidence_refs — parse once, reuse for both passes.
    state_path = pipeline_dir / "state.json"
    state: dict = {}
    if state_path.exists():
        try:
            import json
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}

    for ref in state.get("evidence_refs", []) or []:
        if isinstance(ref, str):
            for u in extract_code_refs(ref):
                _push(u)
        elif isinstance(ref, dict):
            # url/repo/code_url are URL-by-name → push directly.
            for field in ("url", "repo", "code_url"):
                val = ref.get(field)
                if isinstance(val, str):
                    _push(val)
            # path_or_url can be a local path OR a URL — only push when it
            # actually starts with http(s):// or git@. Otherwise the local
            # PDF path leaks in and crashes _shallow_clone downstream.
            por = ref.get("path_or_url")
            if isinstance(por, str) and re.match(r"^(https?://|git@)", por):
                _push(por)
            # Scan every string field for embedded URLs as a backstop. This
            # also catches code-host URLs that happen to live in path_or_url.
            for val in ref.values():
                if isinstance(val, str):
                    for u in extract_code_refs(val):
                        _push(u)

    # 3. Wiki pages whose source matches an evidence_ref filename stem.
    wiki = project_root / "knowledge" / "wiki"
    if wiki.exists() and state.get("evidence_refs"):
        # Build both raw and slugified stems. ``compile_raw_to_wiki`` writes
        # pages under ``_slugify(stem)`` (lowercase, non-alnum→_, trunc 60),
        # so we must compare with the same rule — not raw lowercase.
        raw_stems: set[str] = set()
        slug_stems: set[str] = set()

        def _record_stem(text: str) -> None:
            stem = Path(text).stem
            if not stem:
                return
            raw_stems.add(stem.lower())
            slug_stems.add(_slugify(stem))

        for ref in state.get("evidence_refs", []) or []:
            if isinstance(ref, str):
                _record_stem(ref)
            elif isinstance(ref, dict):
                for field in ("path", "file", "source", "path_or_url"):
                    val = ref.get(field)
                    if isinstance(val, str):
                        _record_stem(val)

        if raw_stems or slug_stems:
            for page in wiki.rglob("*.md"):
                try:
                    head = page.read_text(errors="replace")[:4000]
                except OSError:
                    continue
                if "code_refs:" not in head:
                    continue
                page_stem = page.stem.lower()
                # Match if (a) wiki filename's slug starts with one of our
                # slugged stems (slugify truncates at 60 chars, so equality
                # is too strict), or (b) the raw stem still appears in the
                # page head text (covers wikis written from a title).
                slug_match = any(
                    page_stem.startswith(s) or s.startswith(page_stem)
                    for s in slug_stems if s
                )
                head_lower = head.lower()
                text_match = any(s in head_lower for s in raw_stems if s)
                if not (slug_match or text_match):
                    continue
                try:
                    fm_end = head.index("---", 3)
                    fm = yaml.safe_load(head[3:fm_end]) or {}
                except Exception:
                    continue
                for u in fm.get("code_refs", []) or []:
                    if isinstance(u, str):
                        _push(u)

    # 4. Direct content scan of locally-extracted markdown (base_paper.md,
    #    proposal.md, …). Cmd_inject converts the input PDF to markdown and
    #    writes evidence_refs::path_or_url pointing at this file — its stem
    #    ("base_paper") is generic, so the wiki bridge in step 3 can't match
    #    it back to the slugified wiki page. The PDF body, however, contains
    #    the same code-host URLs the wiki frontmatter would carry. Reading
    #    the markdown directly recovers them.
    candidate_local_md: list[Path] = []
    for ref in state.get("evidence_refs", []) or []:
        if not isinstance(ref, dict):
            continue
        por = ref.get("path_or_url") or ref.get("path") or ""
        if not isinstance(por, str) or not por:
            continue
        if por.startswith(("http://", "https://", "git@")):
            continue  # remote — handled by step 2
        local = Path(por)
        if not local.is_absolute():
            local = (project_root / local).resolve()
        if local.suffix.lower() in (".md", ".txt") and local.is_file():
            candidate_local_md.append(local)
    # Always include the standard slots even when not in evidence_refs (older
    # ingest paths may not have populated typed refs).
    for fallback_name in ("base_paper.md", "proposal.md"):
        candidate = pipeline_dir / fallback_name
        if candidate.is_file() and candidate not in candidate_local_md:
            candidate_local_md.append(candidate)

    for md_path in candidate_local_md:
        try:
            text = md_path.read_text(errors="replace")
        except OSError:
            continue
        for u in extract_code_refs(text):
            _push(u)

    return urls


def run_code_recon(
    pipeline_dir: Path,
    project_root: Path,
    *,
    scratch_root: Path | None = None,
    clone_timeout: int = 180,
) -> dict:
    """Execute CODE_RECON for a pipeline.

    Writes ``code_recon.yaml`` into ``pipeline_dir`` and returns the same
    report as a dict. Always produces a report (even when no URLs were
    found — the step must never block the pipeline on its own).
    """
    scratch_root = scratch_root or (project_root / "scratch")
    pipeline_id = pipeline_dir.name.removeprefix("pipeline_")
    recon_root = scratch_root / pipeline_id / "code_refs"

    max_bytes = _max_repo_bytes()
    urls = gather_urls_for_pipeline(pipeline_dir, project_root)
    report: dict = {
        "pipeline_id": pipeline_id,
        "max_repo_bytes": max_bytes,
        "repos": [],
    }

    for url in urls:
        slug = _owner_repo_slug(url)
        dest = recon_root / slug
        clone = _shallow_clone(url, dest, timeout=clone_timeout)
        status = clone["status"]
        row: dict = {
            "url": url,
            "slug": slug,
            "path": str(dest.relative_to(project_root)) if dest.exists() else "",
            "status": status,
        }
        if status == "rejected_by_whitelist":
            row["reason"] = clone.get("reason", "")
            row["path"] = ""
            report["repos"].append(row)
            continue
        if status in ("error", "oversized", "too_many_files"):
            row["error"] = clone.get("error", clone.get("reason", ""))
            row["path"] = ""
            report["repos"].append(row)
            continue

        # ok / cached — clone_repository already pruned heavy files and checked
        # the size cap; record the results from the clone helper.
        pruned = _prune_heavy(dest)
        row["pruned_bytes"] = pruned
        size = _dir_size(dest)
        row["size_bytes"] = size
        if size > max_bytes:
            # Belt-and-suspenders: wipe if still oversized after secondary prune.
            shutil.rmtree(dest, ignore_errors=True)
            row["status"] = "oversize"
            row["error"] = (
                f"size {size} > max_repo_bytes {max_bytes} after prune"
            )
            row["path"] = ""
            report["repos"].append(row)
            continue

        row["commit_sha"] = _head_commit(dest)
        row["entry_points"] = _detect_entry_points(dest)
        # Dataset hints (v5.2 README scraping) removed in v5.3 — the
        # baseline_analyzer LLM step does this with code-first grounding.
        # v5.4: persist data_shape scan into the artifact so downstream
        # consumers (alignment_validator, lineage, debug review) don't
        # have to re-scan the clone — and so an auditor can see what the
        # baseline_analyzer LLM was given without reading scheduler logs.
        try:
            from researchos.utils.data_shape import scan_repo_for_data
            hits = scan_repo_for_data(dest)
            row["data_shape_hits"] = [
                {"path": h.path, "tier": h.tier, "reason": h.reason}
                for h in hits[:20]
            ]
            row["data_shape_hit_count"] = len(hits)
        except Exception:
            logger.debug("data_shape scan failed for %s", dest, exc_info=True)
            row["data_shape_hits"] = []
            row["data_shape_hit_count"] = 0
        row["license"] = _detect_license(dest)
        report["repos"].append(row)

    out_path = pipeline_dir / "code_recon.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(report, sort_keys=False))
    logger.info(
        "code_recon: pipeline=%s urls=%d ok=%d",
        pipeline_id,
        len(urls),
        sum(1 for r in report["repos"] if r["status"] in ("ok", "cached")),
    )
    return report


__all__ = [
    "MAX_REPO_BYTES",
    "clone_repository",
    "extract_code_refs",
    "gather_urls_for_pipeline",
    "run_code_recon",
    "_host_allowed",
    "_load_baseline_recon_config",
    "_max_repo_bytes",
    "_max_file_count",
]
