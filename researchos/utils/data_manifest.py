"""Data manifest generator — produces data_manifest.yaml with hashes and stats.

Called by experiment scripts via execute_python to create a versioned
snapshot of data used in an experiment. This is CODE, not LLM — ensuring
deterministic hash computation.

Usage from experiment code:
    from researchos.utils.data_manifest import generate_manifest
    manifest = generate_manifest("/path/to/data/dir", pipeline_id="my_pipeline")

Or as a standalone script:
    python -m researchos.utils.data_manifest /path/to/data/dir --pipeline my_pipeline
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def sha256_file(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def count_rows(path: Path) -> int | None:
    """Count data rows in a file. Returns None if not a data file."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with open(path, "r") as f:
                reader = csv.reader(f)
                return sum(1 for _ in reader) - 1  # subtract header
        elif suffix == ".tsv":
            with open(path, "r") as f:
                return sum(1 for _ in f) - 1
        elif suffix == ".json":
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return len(data)
            return None
        elif suffix == ".jsonl":
            with open(path, "r") as f:
                return sum(1 for _ in f)
    except Exception:
        return None
    return None


def generate_manifest(
    data_dir: str | Path,
    pipeline_id: str = "",
    cleaning_script: str | None = None,
    split_script: str | None = None,
    split_config: str | None = None,
    dataset_name: str = "",
    version: str = "",
    source_url: str = "",
    license_info: str = "",
) -> dict:
    """Generate a data manifest from a directory of data files.

    Returns a dict suitable for writing as YAML.
    """
    data_path = Path(data_dir)
    if not data_path.exists():
        return {"error": f"Directory not found: {data_dir}"}

    files = sorted(f for f in data_path.rglob("*") if f.is_file())

    # Build file inventory
    file_inventory = []
    total_rows = 0
    for f in files:
        info = {
            "path": str(f.relative_to(data_path)),
            "size_bytes": f.stat().st_size,
            "hash": sha256_file(f),
        }
        rows = count_rows(f)
        if rows is not None:
            info["rows"] = rows
            total_rows += rows
        file_inventory.append(info)

    # Compute directory-level hash (hash of all file hashes, sorted)
    all_hashes = sorted(f["hash"] for f in file_inventory)
    dir_hash = hashlib.sha256("|".join(all_hashes).encode()).hexdigest()

    manifest = {
        "data_snapshot_id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        "pipeline_id": pipeline_id,
        "dataset_name": dataset_name or "unknown",
        "version": version or "v1",
        "source_url": source_url,
        "license": license_info,
        "download_date": datetime.now(timezone.utc).isoformat(),
        "directory_hash": f"sha256:{dir_hash}",
        "total_files": len(file_inventory),
        "total_rows": total_rows if total_rows > 0 else None,
        "files": file_inventory,
    }

    # Add script hashes if provided
    for label, script_path in [
        ("cleaning_script", cleaning_script),
        ("split_script", split_script),
        ("split_config", split_config),
    ]:
        if script_path:
            p = Path(script_path)
            if p.exists():
                manifest[label] = str(p.name)
                manifest[f"{label}_hash"] = sha256_file(p)

    return manifest


def write_manifest(manifest: dict, output_path: str | Path) -> Path:
    """Write manifest dict to YAML file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(manifest, default_flow_style=False, allow_unicode=True))
    return path


# ── CLI entry point ──────────────────────────────────────────────


def main() -> None:
    """CLI: python -m researchos.utils.data_manifest <dir> [--pipeline ID] [--output path]"""
    import argparse

    parser = argparse.ArgumentParser(description="Generate data manifest")
    parser.add_argument("data_dir", help="Directory containing data files")
    parser.add_argument("--pipeline", default="", help="Pipeline ID")
    parser.add_argument("--dataset", default="", help="Dataset name")
    parser.add_argument("--version", default="", help="Dataset version")
    parser.add_argument("--source-url", default="", help="Source URL")
    parser.add_argument("--license", default="", help="License")
    parser.add_argument("--cleaning-script", default=None, help="Path to cleaning script")
    parser.add_argument("--split-script", default=None, help="Path to split script")
    parser.add_argument("--output", default=None, help="Output path (default: <dir>/data_manifest.yaml)")
    args = parser.parse_args()

    manifest = generate_manifest(
        args.data_dir,
        pipeline_id=args.pipeline,
        dataset_name=args.dataset,
        version=args.version,
        source_url=args.source_url,
        license_info=args.license,
        cleaning_script=args.cleaning_script,
        split_script=args.split_script,
    )

    output = args.output or str(Path(args.data_dir) / "data_manifest.yaml")
    write_manifest(manifest, output)
    print(json.dumps(manifest, indent=2, default=str))


if __name__ == "__main__":
    main()
