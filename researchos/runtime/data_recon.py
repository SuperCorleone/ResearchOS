"""Data-reference reconnaissance — thin executor of ``baseline_analysis.yaml``.

v5.3 rewrite: this module no longer makes any judgement about what counts
as data. The ``BASELINE_ANALYZE`` LLM step (`baseline_analyzer.md`) reads
the cloned baseline package code-first, identifies real datasets vs.
badges/logs/plots, and emits ``baseline_analysis.yaml``. DATA_RECON's job
is now exclusively to *execute* that decision:

* For each ``analyzed_repos[*].dataset_paths[*]``: copytree from
  ``scratch/<id>/code_refs/<slug>/<path>`` to
  ``data/raw/<id>/<slug>/<path>``.
* For each ``external_data_urls[*]`` with ``should_download: true``:
  urlretrieve to ``data/raw/<id>/``.
* Generate ``data_manifest.yaml`` (SHA256 of every file under data/raw/<id>/).

No URL host filtering, no extension filtering, no directory-name guesses,
no "auxiliary path" heuristics — the LLM owns those. If
``baseline_analysis.yaml`` is missing or has ``status: failed``, write a
stub manifest and continue (the pipeline must not block on this step).

Why the rewrite: the v5.2 hardcoded tables (``_DATA_HOSTS``,
``_BADGE_FRAGMENTS``, ``_DATA_EXTENSIONS``, ``_CLONE_DATA_DIRS``) couldn't
handle real-world repo layouts. They miscounted Zenodo DOI shields as
data and assumed every project puts data in ``data/``. The LLM analyser
gets it right by reading the entry-point code.
"""

from __future__ import annotations

import logging
import os
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchos.schemas.baseline_analysis import (
    BaselineAnalysis,
    load_baseline_analysis,
)
from researchos.utils.data_manifest import generate_manifest, write_manifest
from researchos.utils.paths import DATA_RAW_DIR

logger = logging.getLogger(__name__)

# Per-pipeline byte cap for URL downloads (replication-package archives are
# typically <200 MB). Local copytree from cloned repos is not capped — those
# bytes are already on disk and the LLM analyser is responsible for not
# selecting absurd directories.
MAX_TOTAL_DOWNLOAD_BYTES = 500 * 1024 * 1024
PER_FILE_TIMEOUT = 90  # seconds


def _filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name or "dataset"
    return name.split("?")[0] or "dataset"


def _download_one(url: str, dest_dir: Path, *, max_bytes: int) -> dict:
    """Download a single URL to *dest_dir*. Returns a manifest log row."""
    filename = _filename_from_url(url)
    target = dest_dir / filename
    row: dict = {"url": url, "filename": filename, "status": "pending"}

    if target.exists() and target.stat().st_size > 0:
        row["status"] = "cached"
        row["size_bytes"] = target.stat().st_size
        return row

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/1.0"})
        with urllib.request.urlopen(req, timeout=PER_FILE_TIMEOUT) as resp:
            data = resp.read(max_bytes + 1)
    except Exception as exc:
        row["status"] = "error"
        row["error"] = str(exc)[:200]
        return row

    if len(data) > max_bytes:
        row["status"] = "skipped_oversize"
        row["error"] = f"download exceeded per-call cap of {max_bytes} bytes"
        return row

    try:
        target.write_bytes(data)
    except OSError as exc:
        row["status"] = "error"
        row["error"] = f"write failed: {exc}"
        return row

    row["status"] = "ok"
    row["size_bytes"] = len(data)

    if target.suffix.lower() == ".zip":
        import zipfile
        try:
            with zipfile.ZipFile(target) as zf:
                zf.extractall(dest_dir)
            row["extracted"] = True
        except zipfile.BadZipFile:
            row["extracted"] = False
            row["error"] = "downloaded file is not a valid zip"
    return row


def _link_or_copy_file(src: Path, dest: Path) -> str:
    """Hard-link, fall back to symlink, fall back to copy. Returns the mode used.

    Hard link is preferred — same inode, free, lineage-clean. Symlink works
    cross-fs but breaks if the source is later cleaned up. Copy is the
    last resort and is the only mode that costs disk.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(src, dest)
        return "hardlink"
    except OSError:
        pass
    try:
        os.symlink(src.resolve(), dest)
        return "symlink"
    except OSError:
        pass
    shutil.copy2(src, dest)
    return "copy"


def _link_dataset_path(
    repo_root: Path, rel_path: str, dest_root: Path,
) -> dict:
    """Materialise a single dataset path (file or dir) from a cloned repo into
    ``data/raw/<pid>/<slug>/``. Files are hard-linked when possible (same fs
    → no extra disk, real inode for hashing); directories are walked and each
    file is hard-linked individually so the manifest sees real per-file hashes.
    """
    src = repo_root / rel_path
    dest = dest_root / rel_path
    row: dict = {
        "source": rel_path,
        "kind": "cloned_repo_path",
        "status": "pending",
    }
    if not src.exists():
        row["status"] = "error"
        row["error"] = f"source not found: {src}"
        return row
    try:
        modes_used: set[str] = set()
        if src.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
            size = 0
            for sub in src.rglob("*"):
                if sub.is_symlink() or not sub.is_file():
                    continue
                rel_sub = sub.relative_to(src)
                modes_used.add(_link_or_copy_file(sub, dest / rel_sub))
                size += sub.stat().st_size
        else:
            modes_used.add(_link_or_copy_file(src, dest))
            size = src.stat().st_size
        row["size_bytes"] = size
        row["dest"] = str(dest.relative_to(dest_root))
        row["materialise_mode"] = "+".join(sorted(modes_used)) or "copy"
        row["status"] = "ok"
    except Exception as exc:
        row["status"] = "error"
        row["error"] = f"link/copy failed: {exc}"
    return row


# Back-compat alias — older callers may import _copy_dataset_path.
_copy_dataset_path = _link_dataset_path


def _existing_success(manifest_path: Path) -> bool:
    if not manifest_path.exists():
        return False
    try:
        existing = yaml.safe_load(manifest_path.read_text()) or {}
    except Exception:
        return False
    return existing.get("data_recon_status") in ("success", "partial", "cached")


def _write_stub_manifest(
    pipeline_dir: Path, target_dir: Path, pipeline_id: str,
    *, status: str, reason: str,
) -> dict:
    manifest = {
        "pipeline_id": pipeline_id,
        "data_recon_status": status,
        "reason": reason,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executed_actions": [],
        "total_files": 0,
        "files": [],
    }
    write_manifest(manifest, pipeline_dir / "data_manifest.yaml")
    target_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest, target_dir / "data_manifest.yaml")
    return manifest


def run_data_recon(
    pipeline_dir: Path,
    project_root: Path,
    *,
    scratch_root: Path | None = None,
) -> dict:
    """Execute the dataset_paths + external_data_urls actions in
    ``baseline_analysis.yaml``. Writes ``data_manifest.yaml``.
    """
    scratch_root = scratch_root or (project_root / "scratch")
    pipeline_id = pipeline_dir.name.removeprefix("pipeline_")
    target_dir = DATA_RAW_DIR / pipeline_id
    pipeline_manifest_path = pipeline_dir / "data_manifest.yaml"

    if _existing_success(pipeline_manifest_path):
        existing = yaml.safe_load(pipeline_manifest_path.read_text()) or {}
        logger.info(
            "data_recon: skipping pipeline %s — manifest already %s",
            pipeline_id, existing.get("data_recon_status"),
        )
        return existing

    analysis_path = pipeline_dir / "baseline_analysis.yaml"
    analysis = load_baseline_analysis(analysis_path)
    if analysis is None:
        logger.warning(
            "data_recon: %s missing or invalid; writing no_baseline_analysis stub",
            analysis_path,
        )
        return _write_stub_manifest(
            pipeline_dir, target_dir, pipeline_id,
            status="no_baseline_analysis",
            reason=f"baseline_analysis.yaml not found or schema-invalid at {analysis_path}",
        )
    if analysis.status == "failed":
        logger.warning(
            "data_recon: baseline_analysis.status=failed; writing stub manifest",
        )
        return _write_stub_manifest(
            pipeline_dir, target_dir, pipeline_id,
            status="baseline_analyzer_failed",
            reason=analysis.notes or "baseline_analyzer reported status=failed",
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    bytes_downloaded = 0

    # Phase 1 — execute every dataset_paths copy across all repos.
    repo_root_base = scratch_root / pipeline_id / "code_refs"
    for repo in analysis.analyzed_repos:
        repo_root = repo_root_base / repo.slug
        repo_dest = target_dir / repo.slug
        if not repo_root.exists():
            log.append({
                "source": f"<scratch>/code_refs/{repo.slug}",
                "status": "error",
                "error": "cloned repo directory not found — was CODE_RECON re-run after analysis?",
            })
            continue
        for ds in repo.dataset_paths:
            row = _link_dataset_path(repo_root, ds.path, repo_dest)
            row["repo"] = repo.slug
            row["kind_label"] = ds.kind
            row["purpose"] = ds.purpose
            log.append(row)

    # Phase 2 — download every external URL flagged should_download=true.
    for ext in analysis.external_data_urls:
        if not ext.should_download:
            log.append({
                "url": ext.url,
                "status": "skipped_by_analyser",
                "kind": ext.kind,
                "reason": ext.reason,
            })
            continue
        remaining = MAX_TOTAL_DOWNLOAD_BYTES - bytes_downloaded
        if remaining <= 0:
            log.append({
                "url": ext.url,
                "status": "skipped_budget",
                "error": "per-pipeline byte budget exhausted",
            })
            continue
        row = _download_one(ext.url, target_dir, max_bytes=remaining)
        row["analyser_kind"] = ext.kind
        row["analyser_reason"] = ext.reason
        log.append(row)
        if row["status"] in ("ok", "cached"):
            bytes_downloaded += row.get("size_bytes", 0)

    # Decide final status from log.
    have_success = any(r.get("status") in ("ok", "cached") for r in log)
    have_failure = any(r.get("status") in ("error", "skipped_oversize", "skipped_budget") for r in log)
    if have_success and have_failure:
        status = "partial"
    elif have_success:
        status = "success"
    else:
        # No copies AND no downloads. Either analyser said no data needed
        # (no_data_found) or it failed silently.
        status = analysis.status if analysis.status in ("no_data_found", "partial") else "no_data_found"

    primary_url = next(
        (r.get("url", "") for r in log if r.get("status") in ("ok", "cached") and r.get("url")),
        "",
    )

    manifest = generate_manifest(
        str(target_dir),
        pipeline_id=pipeline_id,
        dataset_name=analysis.baseline_paper or "",
        source_url=primary_url,
    )
    manifest["data_recon_status"] = status
    manifest["analysis_strategy"] = analysis.analysis_strategy
    manifest["fallback_used"] = analysis.fallback_used
    manifest["executed_actions"] = log

    write_manifest(manifest, pipeline_manifest_path)
    write_manifest(manifest, target_dir / "data_manifest.yaml")

    logger.info(
        "data_recon: pipeline=%s status=%s actions=%d copies_ok=%d "
        "downloads_ok=%d bytes_downloaded=%d",
        pipeline_id, status, len(log),
        sum(1 for r in log if r.get("kind") == "cloned_repo_path" and r.get("status") == "ok"),
        sum(1 for r in log if r.get("status") == "ok" and r.get("url")),
        bytes_downloaded,
    )
    return manifest


__all__ = [
    "MAX_TOTAL_DOWNLOAD_BYTES",
    "run_data_recon",
]
