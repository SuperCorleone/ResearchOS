"""Heartbeat: daily paper fetcher from arXiv.

Downloads actual paper PDFs matching configured research directions,
filters by citation impact via Semantic Scholar, and saves to
/knowledge/raw/ for compilation into the wiki.

Usage:
    python -m researchos.runtime.heartbeat --once       # Fetch now
    python -m researchos.runtime.heartbeat               # Daemon mode
    python -m researchos.runtime.heartbeat --enable/--disable
    python -m researchos.runtime.heartbeat --status
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "runtime" / "heartbeat_config.yaml"
RAW_DIR = PROJECT_ROOT / "knowledge" / "raw"

ARXIV_API_URL = "http://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# Minimum citations to consider a paper "impactful" (within its age)
# Papers < 6 months: 0 (too new to have citations)
# Papers 6-12 months: >= 2
MIN_CITATIONS_THRESHOLD = 2
CITATION_GRACE_MONTHS = 6


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"enabled": False}
    return yaml.safe_load(CONFIG_PATH.read_text()) or {"enabled": False}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def set_enabled(enabled: bool) -> None:
    config = load_config()
    config["enabled"] = enabled
    save_config(config)
    logger.info("Heartbeat %s", "enabled" if enabled else "disabled")


# ── arXiv search ────────────────────────────────────────────────


def _build_query(direction: dict, domain: str) -> str:
    keywords = direction.get("keywords", [])
    if not keywords:
        return ""
    kw_parts = " OR ".join(f'all:"{kw}"' for kw in keywords)
    return f"({kw_parts}) AND cat:{domain}"


def _search_arxiv(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    """Search arXiv API. Fetches extra candidates for filtering."""
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/0.3"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception as e:
        logger.error("arXiv API error: %s", e)
        return []

    papers = []
    root = ET.fromstring(data)
    for entry in root.findall(f"{ATOM_NS}entry"):
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip().replace("\n", " ")
        abstract = (entry.findtext(f"{ATOM_NS}summary") or "").strip()
        raw_id = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        published = (entry.findtext(f"{ATOM_NS}published") or "").strip()

        # Extract clean arXiv ID (e.g. "2401.12345v1")
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id
        arxiv_id = arxiv_id.rstrip("/")

        authors = []
        for author in entry.findall(f"{ATOM_NS}author"):
            name = author.findtext(f"{ATOM_NS}name")
            if name:
                authors.append(name.strip())

        categories = [
            cat.get("term", "")
            for cat in entry.findall(f"{ATOM_NS}category")
            if cat.get("term")
        ]

        pdf_link = ""
        for link in entry.findall(f"{ATOM_NS}link"):
            if link.get("title") == "pdf":
                pdf_link = link.get("href", "")

        if not pdf_link and arxiv_id:
            pdf_link = f"https://arxiv.org/pdf/{arxiv_id}"

        papers.append({
            "title": title,
            "abstract": abstract,
            "authors": ", ".join(authors),
            "arxiv_id": arxiv_id,
            "published": published,
            "categories": ", ".join(categories),
            "pdf_link": pdf_link,
        })

    return papers


# ── Citation impact filtering ───────────────────────────────────


def _get_citation_count(arxiv_id: str) -> int | None:
    """Query Semantic Scholar for citation count. Returns None on failure."""
    clean_id = arxiv_id.split("v")[0]  # strip version
    url = f"{SEMANTIC_SCHOLAR_API}/ARXIV:{clean_id}?fields=citationCount"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/0.3"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("citationCount", 0)
    except Exception:
        return None  # API failure — don't block


def _passes_impact_filter(paper: dict, min_citations: int) -> bool:
    """Check if a paper passes the citation impact filter.

    Papers younger than CITATION_GRACE_MONTHS are always accepted
    (too new to have accumulated citations).
    """
    try:
        pub_date = datetime.fromisoformat(paper["published"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return True  # Can't parse date, accept

    age_months = (datetime.now(timezone.utc) - pub_date).days / 30.0

    if age_months < CITATION_GRACE_MONTHS:
        logger.debug("Paper %s is %.1f months old — grace period, accepted", paper["arxiv_id"], age_months)
        return True

    citations = _get_citation_count(paper["arxiv_id"])
    if citations is None:
        logger.warning("Citation lookup failed for %s — skipping (conservative filter)", paper["arxiv_id"])
        return False

    passed = citations >= min_citations
    logger.debug(
        "Paper %s: %d citations, age %.1f months → %s",
        paper["arxiv_id"], citations, age_months, "PASS" if passed else "SKIP",
    )
    return passed


# ── PDF downloading ─────────────────────────────────────────────


def _download_pdf(paper: dict, direction_name: str) -> Path | None:
    """Download the actual PDF to /knowledge/raw/. Returns path or None."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "_", paper["title"].lower()).strip("_")[:55]
    pdf_filename = f"arxiv_{slug}.pdf"
    pdf_path = RAW_DIR / pdf_filename

    if pdf_path.exists():
        return None  # Already downloaded

    pdf_url = paper.get("pdf_link", "")
    if not pdf_url:
        return None

    try:
        req = urllib.request.Request(pdf_url, headers={"User-Agent": "ResearchOS/0.3"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            pdf_data = resp.read()

        if len(pdf_data) < 5000:
            logger.warning("PDF too small (%d bytes), likely an error page: %s", len(pdf_data), pdf_url)
            return None

        pdf_path.write_bytes(pdf_data)

        # Also save a metadata sidecar for quick indexing
        meta_path = pdf_path.with_suffix(".meta.yaml")
        meta = {
            "title": paper["title"],
            "authors": paper["authors"],
            "arxiv_id": paper["arxiv_id"],
            "published": paper["published"],
            "categories": paper["categories"],
            "direction": direction_name,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "pdf_file": pdf_filename,
        }
        # Enrich with Crossref venue (best-effort)
        venue_info = _resolve_venue_crossref(paper["title"])
        if venue_info:
            meta["venue"] = venue_info["venue"]
            meta["doi"] = venue_info["doi"]

        # Search GitHub for reference implementations (best-effort)
        title_keywords = [w for w in re.findall(r"\b[a-zA-Z]{4,}\b", paper["title"].lower()) if len(w) > 4][:4]
        if title_keywords:
            github_repos = _search_github_repos(title_keywords, max_results=3)
            if github_repos:
                meta["github_repos"] = [{"name": r["full_name"], "url": r["url"], "stars": r["stars"]} for r in github_repos]

        meta_path.write_text(yaml.dump(meta, default_flow_style=False))

        logger.info(
            "Downloaded PDF: %s (%d KB) [%s]",
            paper["title"][:50], len(pdf_data) // 1024, direction_name,
        )
        return pdf_path

    except Exception as e:
        logger.warning("PDF download failed for %s: %s", paper["arxiv_id"], e)
        return None


# ── GitHub repository search ──────────────────────────────────


def _search_github_repos(keywords: list[str], max_results: int = 5) -> list[dict[str, Any]]:
    """Search GitHub for repositories matching research keywords.

    Returns list of {name, full_name, description, url, stars, language, updated_at}.
    Used to discover reference implementations alongside papers.
    """
    query = "+".join(urllib.parse.quote(k) for k in keywords[:5])
    url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page={max_results}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ResearchOS/1.0",
            "Accept": "application/vnd.github.v3+json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        repos = []
        for item in data.get("items", [])[:max_results]:
            repos.append({
                "name": item.get("name", ""),
                "full_name": item.get("full_name", ""),
                "description": (item.get("description") or "")[:200],
                "url": item.get("html_url", ""),
                "stars": item.get("stargazers_count", 0),
                "language": item.get("language", ""),
                "updated_at": item.get("updated_at", ""),
            })
        return repos
    except Exception as e:
        logger.warning("GitHub search failed: %s", e)
        return []


# ── Crossref venue resolution ────────────────────────────────


def _resolve_venue_crossref(title: str) -> dict[str, Any] | None:
    """Query Crossref to resolve publication venue for a paper title.

    Returns {venue, doi, type, published_date} or None on failure.
    """
    params = urllib.parse.urlencode({"query.title": title, "rows": 1})
    url = f"https://api.crossref.org/works?{params}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "ResearchOS/1.0 (mailto:researchos@example.com)",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("message", {}).get("items", [])
        if not items:
            return None
        item = items[0]
        venue = ""
        for cn in item.get("container-title", []):
            if cn:
                venue = cn
                break
        if not venue:
            venue = item.get("publisher", "")
        return {
            "venue": venue,
            "doi": item.get("DOI", ""),
            "type": item.get("type", ""),
            "published_date": "-".join(str(x) for x in (item.get("published-print", {}).get("date-parts", [[]])[0] or [])),
        }
    except Exception as e:
        logger.debug("Crossref lookup failed for '%s': %s", title[:50], e)
        return None


# ── Semantic Scholar citation graph ──────────────────────────


def _get_paper_references(arxiv_id: str, max_refs: int = 10) -> list[dict[str, Any]]:
    """Get papers referenced by a given paper (1-hop outgoing citations).

    Useful for discovering related work that the paper builds upon.
    """
    clean_id = arxiv_id.split("v")[0]
    url = f"{SEMANTIC_SCHOLAR_API}/ARXIV:{clean_id}/references?fields=title,externalIds,citationCount,year&limit={max_refs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        refs = []
        for item in data.get("data", []):
            cited = item.get("citedPaper", {})
            if not cited or not cited.get("title"):
                continue
            ext_ids = cited.get("externalIds", {}) or {}
            refs.append({
                "title": cited.get("title", ""),
                "arxiv_id": ext_ids.get("ArXiv", ""),
                "citation_count": cited.get("citationCount", 0) or 0,
                "year": cited.get("year"),
            })
        return refs
    except Exception as e:
        logger.debug("S2 references lookup failed for %s: %s", arxiv_id, e)
        return []


# ── Main fetch logic ────────────────────────────────────────────


def fetch_papers(config: dict | None = None) -> list[Path]:
    """Fetch papers for all configured directions. Returns list of downloaded PDF paths."""
    if config is None:
        config = load_config()

    if not config.get("enabled", False):
        logger.info("Heartbeat is disabled. Use --enable to activate.")
        return []

    domain = config.get("professor", {}).get("domain", "cs.AI")
    directions = config.get("directions", [])
    max_per_direction = config.get("max_papers_per_direction", 5)
    max_total = config.get("max_total_per_day", 15)
    min_citations = config.get("filters", {}).get("min_citations", MIN_CITATIONS_THRESHOLD)

    saved: list[Path] = []
    seen_ids: set[str] = set()
    for i, direction in enumerate(directions):
        if len(saved) >= max_total:
            break

        name = direction.get("name", "unknown")
        query = _build_query(direction, domain)
        if not query:
            continue

        # Fetch extra candidates (3x) to have room after filtering
        candidates = _search_arxiv(query, max_results=max_per_direction * 3)
        logger.info("Direction '%s': %d candidates from arXiv", name, len(candidates))

        downloaded_this_direction = 0
        for paper in candidates:
            if len(saved) >= max_total:
                break
            if downloaded_this_direction >= max_per_direction:
                break

            # Deduplicate across directions
            arxiv_id = paper.get("arxiv_id", "")
            if arxiv_id in seen_ids:
                continue
            if arxiv_id:
                seen_ids.add(arxiv_id)

            # Filter by impact
            if not _passes_impact_filter(paper, min_citations):
                continue

            path = _download_pdf(paper, name)
            if path:
                saved.append(path)
                downloaded_this_direction += 1

            # Small delay between downloads
            time.sleep(1)

        # Rate limit between directions
        if i < len(directions) - 1:
            time.sleep(3)

    logger.info("Heartbeat: downloaded %d PDFs", len(saved))
    return saved


# ── Daemon ──────────────────────────────────────────────────────


def run_daemon(config: dict | None = None) -> None:
    if config is None:
        config = load_config()

    fetch_hour = config.get("fetch_hour", 6)
    fetch_minute = config.get("fetch_minute", 0)

    logger.info("Heartbeat daemon started. Fetch time: %02d:%02d daily", fetch_hour, fetch_minute)

    while True:
        config = load_config()
        if not config.get("enabled", False):
            logger.info("Heartbeat disabled. Sleeping 60s before re-check...")
            time.sleep(60)
            continue

        now = datetime.now()
        target = now.replace(hour=fetch_hour, minute=fetch_minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info("Next fetch in %.0f seconds (%s)", wait_seconds, target.isoformat())
        time.sleep(wait_seconds)

        config = load_config()
        if config.get("enabled", False):
            try:
                saved = fetch_papers(config)
                log_path = PROJECT_ROOT / "runtime" / "logs" / "heartbeat.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as f:
                    f.write(
                        f"[{datetime.now(timezone.utc).isoformat()}] "
                        f"Downloaded {len(saved)} PDFs\n"
                    )
            except Exception:
                logger.exception("Heartbeat fetch failed")


# ── CLI ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchOS Heartbeat - Daily Paper Fetcher")
    parser.add_argument("--once", action="store_true", help="Fetch once and exit")
    parser.add_argument("--enable", action="store_true", help="Enable heartbeat")
    parser.add_argument("--disable", action="store_true", help="Disable heartbeat")
    parser.add_argument("--status", action="store_true", help="Show heartbeat status")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.enable:
        set_enabled(True)
        print("Heartbeat enabled.")
        return
    if args.disable:
        set_enabled(False)
        print("Heartbeat disabled.")
        return
    if args.status:
        config = load_config()
        enabled = config.get("enabled", False)
        domain = config.get("professor", {}).get("domain", "?")
        desc = config.get("professor", {}).get("domain_description", "")
        directions = config.get("directions", [])
        print(f"  Heartbeat: {'ENABLED' if enabled else 'DISABLED'}")
        print(f"  Domain: {domain} ({desc})")
        print(f"  Directions: {len(directions)}")
        for d in directions:
            print(f"    - {d.get('name', '?')} ({d.get('priority', '?')}): {d.get('keywords', [])}")
        # Count existing PDFs
        pdf_count = len(list(RAW_DIR.glob("arxiv_*.pdf"))) if RAW_DIR.exists() else 0
        print(f"  Downloaded PDFs: {pdf_count}")
        return

    if args.once:
        config = load_config()
        config["enabled"] = True
        saved = fetch_papers(config)
        print(f"Downloaded {len(saved)} PDFs to knowledge/raw/")
        for p in saved:
            print(f"  - {p.name} ({p.stat().st_size // 1024} KB)")
    else:
        run_daemon()


if __name__ == "__main__":
    main()
