"""Code-reference reconnaissance.

Pure Python, no LLM. Clones the reference codebases hinted at by a paper
(via ``knowledge_compiler.extract_code_refs``) or by an explicit
``experiment_plan.yaml::reference_codebases`` list, then produces a
``code_recon.yaml`` that the ``concept_extractor`` agent consumes.

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
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

import yaml

from researchos.runtime.knowledge_compiler import extract_code_refs

logger = logging.getLogger(__name__)

# Reference codebases can be huge (weights, datasets). Cap per-repo disk
# usage. 500 MB is generous for code-only scans; anything over is likely
# checkpoints or a bundled dataset and gets pruned after clone.
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


def _shallow_clone(url: str, dest: Path, timeout: int = 180) -> dict:
    """Run ``git clone --depth=1`` into ``dest``.

    Returns a dict with ``status`` (``ok`` / ``error``) and diagnostic
    fields. Never raises.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Idempotent: treat an existing directory as a cache hit and read
        # its commit SHA. We only skip re-cloning; pruning still runs.
        return {"status": "cached", "path": str(dest)}
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
        return {"status": "error", "error": "git executable not found"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": f"git clone timed out after {timeout}s"}
    if proc.returncode != 0:
        # Truncate to keep yaml readable.
        err = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {"status": "error", "error": err[0][:200]}
    return {"status": "ok", "path": str(dest)}


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


def _detect_entry_points(repo: Path, limit: int = 5) -> list[str]:
    """Return relative paths of plausible entry-point scripts."""
    found: list[str] = []
    for name in _ENTRY_POINT_NAMES:
        for candidate in repo.rglob(name):
            try:
                rel = candidate.relative_to(repo)
            except ValueError:
                continue
            # Skip venv / site-packages / test fixtures.
            rel_str = str(rel)
            if any(seg in rel_str for seg in ("site-packages/", "/.venv/", "/venv/", "/__pycache__/")):
                continue
            found.append(rel_str)
            if len(found) >= limit:
                return found
    return found


def _detect_dataset_hints(repo: Path) -> list[str]:
    """Scan README + common config files for dataset download hints.

    We return a small list of raw lines (max 10) that look like they
    reference a dataset URL, an S3 bucket, or a ``torchvision.datasets``
    / ``hf_hub_download`` call. This is a hint for the concept_extractor,
    not a decisive signal.
    """
    hints: list[str] = []
    needles = (
        "dataset", "data_url", "download", "s3://", "gs://",
        "torchvision.datasets", "hf_hub_download", "load_dataset",
        "https://zenodo", "https://huggingface.co/datasets",
        "http://yann.lecun.com", "cifar", "imagenet", "coco",
    )
    readme = _find_first(repo, _README_NAMES)
    if readme is not None:
        try:
            for line in readme.read_text(errors="replace").splitlines():
                low = line.lower()
                if any(n in low for n in needles) and len(line.strip()) < 300:
                    hints.append(line.strip())
                if len(hints) >= 10:
                    break
        except OSError:
            pass
    # Also scan a few common config files.
    for name in ("requirements.txt", "pyproject.toml", "Makefile", "setup.py"):
        cfg = repo / name
        if cfg.exists() and cfg.is_file():
            try:
                for line in cfg.read_text(errors="replace").splitlines():
                    low = line.lower()
                    if any(n in low for n in needles[:10]) and len(line.strip()) < 300:
                        hints.append(f"[{name}] {line.strip()}")
                    if len(hints) >= 10:
                        break
            except OSError:
                continue
        if len(hints) >= 10:
            break
    return hints


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

    # 2. state.json / evidence_refs
    state_path = pipeline_dir / "state.json"
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
                for field in ("url", "repo", "code_url"):
                    val = ref.get(field)
                    if isinstance(val, str):
                        _push(val)
                # Also scan any string field for URLs.
                for val in ref.values():
                    if isinstance(val, str):
                        for u in extract_code_refs(val):
                            _push(u)

    # 3. Wiki pages whose source matches an evidence_ref filename stem.
    wiki = project_root / "knowledge" / "wiki"
    if state_path.exists() and wiki.exists():
        try:
            import json
            state = json.loads(state_path.read_text())
        except Exception:
            state = {}
        stems: set[str] = set()
        for ref in state.get("evidence_refs", []) or []:
            if isinstance(ref, str):
                stems.add(Path(ref).stem.lower())
            elif isinstance(ref, dict):
                for field in ("path", "file", "source"):
                    val = ref.get(field)
                    if isinstance(val, str):
                        stems.add(Path(val).stem.lower())
        if stems:
            for page in wiki.rglob("*.md"):
                try:
                    head = page.read_text(errors="replace")[:4000]
                except OSError:
                    continue
                if "code_refs:" not in head:
                    continue
                if not any(stem in head.lower() for stem in stems):
                    continue
                try:
                    fm_end = head.index("---", 3)
                    fm = yaml.safe_load(head[3:fm_end]) or {}
                except Exception:
                    continue
                for u in fm.get("code_refs", []) or []:
                    if isinstance(u, str):
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

    urls = gather_urls_for_pipeline(pipeline_dir, project_root)
    report: dict = {
        "pipeline_id": pipeline_id,
        "max_repo_bytes": MAX_REPO_BYTES,
        "repos": [],
    }

    for url in urls:
        slug = _owner_repo_slug(url)
        dest = recon_root / slug
        clone = _shallow_clone(url, dest, timeout=clone_timeout)
        row: dict = {
            "url": url,
            "slug": slug,
            "path": str(dest.relative_to(project_root)) if dest.exists() else "",
            "status": clone["status"],
        }
        if clone["status"] == "error":
            row["error"] = clone.get("error", "")
            report["repos"].append(row)
            continue

        pruned = _prune_heavy(dest)
        row["pruned_bytes"] = pruned
        size = _dir_size(dest)
        row["size_bytes"] = size
        if size > MAX_REPO_BYTES:
            # We exceeded the cap even after pruning weights. Wipe the
            # clone to stay within the scratch budget.
            shutil.rmtree(dest, ignore_errors=True)
            row["status"] = "oversize"
            row["error"] = (
                f"size {size} > MAX_REPO_BYTES {MAX_REPO_BYTES} after prune"
            )
            row["path"] = ""
            report["repos"].append(row)
            continue

        row["commit_sha"] = _head_commit(dest)
        row["entry_points"] = _detect_entry_points(dest)
        row["dataset_hints"] = _detect_dataset_hints(dest)
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
    "extract_code_refs",
    "gather_urls_for_pipeline",
    "run_code_recon",
]
