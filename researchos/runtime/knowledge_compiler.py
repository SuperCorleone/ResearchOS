"""Knowledge Compiler: converts pipeline artifacts and raw inputs into wiki pages.

Triggered by the scheduler after milestone completion or manually.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from researchos.utils.paths import (
    ARTIFACTS_DIR,
    KNOWLEDGE_RAW_DIR,
    KNOWLEDGE_WIKI_DIR,
    KNOWLEDGE_INDEX_DIR,
)

logger = logging.getLogger(__name__)


def compile_pipeline_to_wiki(pipeline_id: str, project_root: Path | None = None) -> list[Path]:
    """Compile a pipeline's key artifacts into wiki pages.

    Returns list of created wiki page paths.
    """
    root = project_root or Path(__file__).resolve().parent.parent.parent
    pdir = root / "artifacts" / f"pipeline_{pipeline_id}"
    wiki = root / "knowledge" / "wiki"
    created: list[Path] = []
    ts = datetime.now(timezone.utc).isoformat()

    if not pdir.exists():
        logger.warning("Pipeline directory not found: %s", pdir)
        return created

    # Read state for metadata
    state_file = pdir / "state.json"
    title = pipeline_id
    question = ""
    if state_file.exists():
        import json
        state = json.loads(state_file.read_text())
        title = state.get("title", pipeline_id)
        question = state.get("research_question", "")

    # 1. Compile hypothesis into concepts/
    hyp_file = pdir / "hypothesis.md"
    if hyp_file.exists():
        dest = wiki / "concepts" / f"hypothesis_{pipeline_id}.md"
        if not dest.exists():
            content = _make_wiki_page(
                title=f"Hypothesis: {title}",
                tags=["hypothesis", pipeline_id],
                source=str(hyp_file.relative_to(root)),
                created=ts,
                body=hyp_file.read_text(),
            )
            dest.write_text(content)
            created.append(dest)

    # 2. Compile experiment design into methods/
    plan_file = pdir / "experiment_plan.yaml"
    if plan_file.exists():
        dest = wiki / "methods" / f"experiment_{pipeline_id}.md"
        if not dest.exists():
            plan_text = plan_file.read_text()
            content = _make_wiki_page(
                title=f"Experiment Design: {title}",
                tags=["experiment-design", pipeline_id],
                source=str(plan_file.relative_to(root)),
                created=ts,
                body=f"## Experiment Plan\n\n```yaml\n{plan_text}\n```",
            )
            dest.write_text(content)
            created.append(dest)

    # 3. Compile results into experiments/
    results_file = pdir / "results.json"
    analysis_file = pdir / "analysis_report.md"
    if results_file.exists():
        dest = wiki / "experiments" / f"results_{pipeline_id}.md"
        if not dest.exists():
            import json
            results = json.loads(results_file.read_text())
            metrics_text = "\n".join(
                f"- **{k}**: {v}" for k, v in results.get("metrics", {}).items()
            )
            analysis_text = ""
            if analysis_file.exists():
                analysis_text = f"\n## Analysis\n\n{analysis_file.read_text()}"

            content = _make_wiki_page(
                title=f"Experiment Results: {title}",
                tags=["experiment", "results", pipeline_id],
                source=str(results_file.relative_to(root)),
                created=ts,
                body=(
                    f"## Research Question\n{question}\n\n"
                    f"## Key Metrics\n{metrics_text}\n\n"
                    f"## Configuration\n```json\n{json.dumps(results.get('config', {}), indent=2)}\n```"
                    f"{analysis_text}"
                ),
            )
            dest.write_text(content)
            created.append(dest)

    # 4. Check for failures
    if analysis_file.exists():
        text = analysis_file.read_text()
        if "REFUTED" in text or "FAIL" in text.upper():
            dest = wiki / "failures" / f"failure_{pipeline_id}.md"
            if not dest.exists():
                content = _make_wiki_page(
                    title=f"Failed/Refuted: {title}",
                    tags=["failure", pipeline_id],
                    source=str(analysis_file.relative_to(root)),
                    created=ts,
                    body=text,
                )
                dest.write_text(content)
                created.append(dest)

    # 5. Compile data manifest into wiki/datasets/
    manifest_file = pdir / "data_manifest.yaml"
    if manifest_file.exists():
        dest = wiki / "datasets" / f"data_{pipeline_id}.md"
        if not dest.exists():
            manifest_text = manifest_file.read_text()
            import yaml as _yaml
            try:
                manifest = _yaml.safe_load(manifest_text) or {}
            except Exception:
                manifest = {}
            dataset_name = manifest.get("dataset_name", "unknown")
            dir_hash = manifest.get("directory_hash", "unknown")
            total_files = manifest.get("total_files", 0)
            total_rows = manifest.get("total_rows")

            body_parts = [
                f"## Dataset: {dataset_name}\n",
                f"- **Pipeline**: {pipeline_id}\n",
                f"- **Source URL**: {manifest.get('source_url', 'N/A')}\n",
                f"- **License**: {manifest.get('license', 'N/A')}\n",
                f"- **Download Date**: {manifest.get('download_date', 'N/A')}\n",
                f"- **Directory Hash**: `{dir_hash}`\n",
                f"- **Files**: {total_files}\n",
            ]
            if total_rows:
                body_parts.append(f"- **Total Rows**: {total_rows}\n")
            body_parts.append(f"\n## Manifest\n\n```yaml\n{manifest_text}\n```\n")

            (wiki / "datasets").mkdir(parents=True, exist_ok=True)
            content = _make_wiki_page(
                title=f"Dataset: {dataset_name} ({pipeline_id})",
                tags=["dataset", pipeline_id, dataset_name],
                source=str(manifest_file.relative_to(root)),
                created=ts,
                body="".join(body_parts),
            )
            dest.write_text(content)
            created.append(dest)

    # Update topic map
    if created:
        _update_topic_map(root, created)

    logger.info(
        "Knowledge compiled for pipeline %s: %d pages created",
        pipeline_id, len(created),
    )
    return created


def _extract_pdf_text(pdf_path: Path, max_pages: int = 20) -> str:
    """Extract text from a PDF file using PyMuPDF (fitz)."""
    try:
        import fitz
    except ImportError:
        logger.warning("pymupdf not installed, skipping PDF: %s", pdf_path)
        return ""
    doc = fitz.open(str(pdf_path))
    pages = []
    for i in range(min(max_pages, len(doc))):
        pages.append(doc[i].get_text())
    doc.close()
    return "\n\n".join(pages)


# A20: code-reference URL extraction from paper text.
_CODE_HOST_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:github\.com|gitlab\.com|bitbucket\.org|huggingface\.co|codeberg\.org)"
    r"/[A-Za-z0-9][\w.\-]*/[A-Za-z0-9][\w.\-]+"
    r"(?:/tree/[\w.\-/]+)?",
    re.IGNORECASE,
)


def extract_code_refs(text: str) -> list[str]:
    """Return a deduplicated list of code-host URLs found in *text*.

    Supports GitHub, GitLab, Bitbucket, HuggingFace Hub, and Codeberg. Strips
    trailing punctuation and parentheses that often appear around URLs in
    PDF-extracted text. Paths are truncated at the second path segment so we
    always return the repo root (callers can pin a commit later).
    """
    if not text:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for match in _CODE_HOST_RE.finditer(text):
        raw = match.group(0)
        # Strip trailing punctuation that PDF extraction often glues on.
        while raw and raw[-1] in ".,);]}>":
            raw = raw[:-1]
        # Truncate to owner/repo (drop /tree/..., /blob/..., etc.) so dedup works.
        parts = raw.split("/")
        if len(parts) >= 5:
            raw = "/".join(parts[:5])
        if raw in seen:
            continue
        seen.add(raw)
        urls.append(raw)
    return urls


def _slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60]


def compile_raw_to_wiki(raw_file: Path, project_root: Path | None = None) -> Path | None:
    """Compile a single raw file (md, txt, or pdf) into a wiki page."""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    wiki = root / "knowledge" / "wiki"
    ts = datetime.now(timezone.utc).isoformat()

    if not raw_file.exists():
        return None

    stem = raw_file.stem
    suffix = raw_file.suffix.lower()
    slug = _slugify(stem)

    if suffix == ".pdf":
        text = _extract_pdf_text(raw_file)
        if not text.strip():
            logger.warning("No text extracted from PDF: %s", raw_file)
            return None
    elif suffix in (".md", ".txt"):
        text = raw_file.read_text(errors="replace")
    else:
        logger.info("Skipping unsupported file: %s", raw_file)
        return None

    # Put in concepts/ by default
    dest = wiki / "concepts" / f"{slug}.md"
    if dest.exists():
        return None

    code_refs = extract_code_refs(text)
    content = _make_wiki_page(
        title=stem,
        tags=["raw-import", suffix.lstrip(".")],
        source=str(raw_file.relative_to(root)) if raw_file.is_relative_to(root) else str(raw_file),
        created=ts,
        body=text,
        code_refs=code_refs,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    _update_topic_map(root, [dest])
    logger.info("Compiled raw file to wiki: %s -> %s", raw_file.name, dest)
    return dest


def compile_all_raw(project_root: Path | None = None) -> list[Path]:
    """Compile all files in /knowledge/raw/ into wiki pages."""
    root = project_root or Path(__file__).resolve().parent.parent.parent
    raw_dir = root / "knowledge" / "raw"
    created = []
    if not raw_dir.exists():
        return created
    for f in sorted(raw_dir.iterdir()):
        if f.name.startswith("."):
            continue
        page = compile_raw_to_wiki(f, root)
        if page:
            created.append(page)
    return created


def _make_wiki_page(
    title: str,
    tags: list[str],
    source: str,
    created: str,
    body: str,
    code_refs: list[str] | None = None,
) -> str:
    """Format a wiki page with YAML frontmatter.

    When ``code_refs`` is non-empty, a ``code_refs:`` list is added to the
    frontmatter. These URLs are consumed by the CODE_RECON step (A20).
    """
    meta: dict = {"title": title, "tags": tags, "source": source, "created": created}
    if code_refs:
        meta["code_refs"] = list(code_refs)
    frontmatter = yaml.dump(meta, default_flow_style=False).strip()
    return f"---\n{frontmatter}\n---\n\n# {title}\n\n{body}\n"


def _update_topic_map(root: Path, new_pages: list[Path]) -> None:
    """Add new entries to the topic map index (deduplicated)."""
    topic_map = root / "knowledge" / "index" / "topic_map.md"
    topic_map.parent.mkdir(parents=True, exist_ok=True)

    existing_lines: set[str] = set()
    header = "# Topic Map\n\nAuto-generated index of wiki pages.\n\n"
    if topic_map.exists():
        text = topic_map.read_text()
        for line in text.splitlines():
            if line.startswith("- ["):
                existing_lines.add(line.strip())
    else:
        topic_map.write_text(header)

    wiki_dir = root / "knowledge" / "wiki"
    new_entries = []
    for page in new_pages:
        try:
            rel = page.relative_to(wiki_dir)
        except ValueError:
            rel = page.name
        entry = f"- [{page.stem}](wiki/{rel})"
        if entry not in existing_lines:
            new_entries.append(entry)
            existing_lines.add(entry)

    if new_entries:
        with open(topic_map, "a") as f:
            for entry in new_entries:
                f.write(entry + "\n")
