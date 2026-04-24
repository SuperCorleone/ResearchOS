"""MCP stdio server providing the ``kb_retrieve`` tool for knowledge base access.

Uses BM25 scoring with optional vector search (FAISS) for hybrid retrieval
over ``/knowledge/wiki/`` markdown files.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

# Resolve project root relative to this file
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WIKI_DIR = PROJECT_ROOT / "knowledge" / "wiki"

# MCP SDK is optional -- the search logic works standalone
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False

if _HAS_MCP:
    server = Server("researchos-kb")


# ── BM25 constants & caches ─────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to",
    "for", "of", "and", "or", "but", "not", "with", "this", "that", "it",
    "be", "as", "by", "from", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "its",
})

_idf_cache: dict[str, float] = {}
_idf_cache_mtime: float = 0.0

# ── Optional vector search (FAISS + sentence-transformers) ─────

try:
    import numpy as np
    from sentence_transformers import SentenceTransformer
    import faiss

    _HAS_VECTOR = True
except ImportError:
    _HAS_VECTOR = False

_vector_index: object | None = None  # faiss.IndexFlatIP when available
_vector_embeddings_mtime: float = 0.0
_vector_model: object | None = None  # SentenceTransformer when available
_vector_page_paths: list[Path] = []


# ── Helpers ──────────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """Word tokenisation with stopword removal (lowercase, alphanumeric, CJK)."""
    tokens = re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _load_wiki_pages() -> list[tuple[Path, str]]:
    """Return ``(path, content)`` pairs for all markdown files under wiki/."""
    pages: list[tuple[Path, str]] = []
    if not WIKI_DIR.exists():
        return pages
    for md in WIKI_DIR.rglob("*.md"):
        try:
            pages.append((md, md.read_text(encoding="utf-8")))
        except Exception:
            pass
    return pages


def _compute_idf(pages: list[tuple[Path, str]]) -> dict[str, float]:
    """Compute inverse document frequency for all terms across the corpus.

    Uses a module-level cache invalidated when WIKI_DIR mtime changes.
    """
    global _idf_cache, _idf_cache_mtime

    try:
        current_mtime = WIKI_DIR.stat().st_mtime if WIKI_DIR.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    if _idf_cache and current_mtime == _idf_cache_mtime:
        return _idf_cache

    n = len(pages)
    if n == 0:
        _idf_cache = {}
        _idf_cache_mtime = current_mtime
        return _idf_cache

    doc_freq: Counter[str] = Counter()
    for _, content in pages:
        unique_tokens = set(_tokenize(content))
        for t in unique_tokens:
            doc_freq[t] += 1

    idf: dict[str, float] = {}
    for term, df in doc_freq.items():
        # Standard BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        idf[term] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    _idf_cache = idf
    _idf_cache_mtime = current_mtime
    return idf


def _score_page_bm25(
    query_tokens: list[str],
    page_tokens: list[str],
    idf: dict[str, float],
    avg_dl: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """BM25 relevance score for a single page."""
    if not page_tokens:
        return 0.0

    dl = len(page_tokens)
    freq = Counter(page_tokens)
    score = 0.0

    for qt in query_tokens:
        tf = freq.get(qt, 0)
        if tf == 0:
            continue
        term_idf = idf.get(qt, 0.0)
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * dl / max(avg_dl, 1.0))
        score += term_idf * numerator / denominator

    return score


def _parse_frontmatter(content: str) -> dict | None:
    """Parse YAML frontmatter from a wiki page string.

    Returns the parsed dict, or None if no valid frontmatter block is found.
    """
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end < 0:
        return None
    try:
        return yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Rough truncation assuming 1 token ~ 4 chars."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def _char_ngrams(text: str, n: int = 3) -> set[str]:
    """Extract character n-grams from text for fuzzy matching."""
    text = text.lower()
    return {text[i : i + n] for i in range(len(text) - n + 1)} if len(text) >= n else {text}


def _ngram_similarity(query: str, doc: str, n: int = 3) -> float:
    """Jaccard similarity of character n-grams between query and doc.

    Catches partial word matches that BM25 misses, e.g.:
    - "optimization" matches "optimizing", "optimizer"
    - "neural" matches "neuronal", "neuro"
    """
    q_ngrams = _char_ngrams(query, n)
    d_ngrams = _char_ngrams(doc[:2000], n)  # limit doc length for performance
    if not q_ngrams or not d_ngrams:
        return 0.0
    intersection = len(q_ngrams & d_ngrams)
    union = len(q_ngrams | d_ngrams)
    return intersection / union if union > 0 else 0.0  # symmetric Jaccard


def _load_vector_config() -> float:
    """Load vector_weight from research_config.yaml. Returns weight (0.0-1.0)."""
    config_path = PROJECT_ROOT / "runtime" / "research_config.yaml"
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text()) or {}
            return float(config.get("retrieval", {}).get("vector_weight", 0.3))
        except Exception:
            pass
    return 0.3


def _get_vector_model() -> object:
    """Lazily load the sentence-transformers model."""
    global _vector_model
    if _vector_model is None:
        _vector_model = SentenceTransformer("all-MiniLM-L6-v2")  # type: ignore[name-defined]
    return _vector_model


def _build_vector_index(
    pages: list[tuple[Path, str]],
) -> tuple[object, list[Path]]:
    """Build or rebuild FAISS index from wiki pages.

    Uses inner product on L2-normalized vectors (= cosine similarity).
    """
    global _vector_index, _vector_embeddings_mtime, _vector_page_paths

    try:
        current_mtime = WIKI_DIR.stat().st_mtime if WIKI_DIR.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    if _vector_index is not None and current_mtime == _vector_embeddings_mtime:
        return _vector_index, _vector_page_paths

    model = _get_vector_model()
    texts = [content[:2000] for _, content in pages]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)  # type: ignore[union-attr]

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)  # type: ignore[name-defined]
    index.add(embeddings.astype(np.float32))  # type: ignore[name-defined]

    _vector_index = index
    _vector_embeddings_mtime = current_mtime
    _vector_page_paths = [path for path, _ in pages]

    return index, _vector_page_paths


def _vector_search(
    query: str,
    pages: list[tuple[Path, str]],
    max_results: int = 15,
) -> list[tuple[float, Path, str]]:
    """Return (score, path, content) tuples from FAISS vector search."""
    index, page_paths = _build_vector_index(pages)
    model = _get_vector_model()

    query_embedding = model.encode([query], normalize_embeddings=True)  # type: ignore[union-attr]
    scores, indices = index.search(query_embedding.astype(np.float32), min(max_results, len(pages)))  # type: ignore[name-defined,union-attr]

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        path = page_paths[idx]
        content = next(c for p, c in pages if p == path)
        results.append((float(score), path, content))
    return results


def search_wiki(query: str, max_results: int = 5, max_tokens: int = 4000) -> str:
    """Search wiki pages by BM25 + character n-gram + optional vector reranking.

    Three-stage retrieval:
    1. BM25 lexical match (recall stage) — retrieves top 3x candidates
    2. Optional FAISS vector search (semantic stage) — catches conceptual matches
    3. Combined scoring with configurable weights

    Falls back to BM25-only if sentence-transformers/faiss are not installed.
    """
    pages = _load_wiki_pages()
    if not pages:
        return "Knowledge base is empty. No wiki pages found."

    query_tokens = _tokenize(query)
    if not query_tokens:
        return "Empty query. Please provide search terms."

    idf = _compute_idf(pages)

    # Compute average document length for BM25 length normalisation
    all_page_tokens = []
    for _, content in pages:
        all_page_tokens.append(_tokenize(content))
    avg_dl = sum(len(pt) for pt in all_page_tokens) / max(len(pages), 1)

    # Stage 1: BM25 recall — get 3x candidates
    recall_k = max(max_results * 3, 15)
    scored: list[tuple[float, float, Path, str]] = []
    for i, (path, content) in enumerate(pages):
        bm25_score = _score_page_bm25(query_tokens, all_page_tokens[i], idf, avg_dl)
        if bm25_score > 0:
            scored.append((bm25_score, 0.0, path, content))

    # Also include pages with high n-gram similarity even if BM25=0
    # (catches semantic near-misses where exact words don't overlap)
    query_text = query.lower()
    for i, (path, content) in enumerate(pages):
        ngram_sim = _ngram_similarity(query_text, content)
        if ngram_sim > 0.15:  # reasonable overlap threshold (symmetric Jaccard)
            # Check if already in scored
            already = any(p == path for _, _, p, _ in scored)
            if not already:
                scored.append((0.0, ngram_sim, path, content))

    # Stage 2: Optional vector search
    vector_scores: dict[Path, float] = {}
    vw = _load_vector_config()
    if _HAS_VECTOR and vw > 0:
        try:
            vec_results = _vector_search(query, pages, recall_k)
            max_vec = max((s for s, _, _ in vec_results), default=1.0) or 1.0
            vector_scores = {path: score / max_vec for score, path, _ in vec_results}
            for score, path, content in vec_results:
                already = any(p == path for _, _, p, _ in scored)
                if not already:
                    scored.append((0.0, 0.0, path, content))
        except Exception:
            pass  # Fall back to BM25-only

    # Stage 3: Combined scoring — BM25 + n-gram + optional vector
    if scored:
        max_bm25 = max(s[0] for s in scored) or 1.0
        use_vector = _HAS_VECTOR and bool(vector_scores)
        reranked = []
        for bm25_score, ngram_score_cached, path, content in scored:
            ngram_sim = ngram_score_cached if ngram_score_cached > 0 else _ngram_similarity(query_text, content)
            if use_vector:
                vec_score = vector_scores.get(path, 0.0)
                combined = (1.0 - vw) * 0.7 * (bm25_score / max_bm25) + (1.0 - vw) * 0.3 * ngram_sim + vw * vec_score
            else:
                combined = 0.7 * (bm25_score / max_bm25) + 0.3 * ngram_sim
            reranked.append((combined, path, content))
        reranked.sort(key=lambda x: x[0], reverse=True)
        top = reranked[:max_results]
    else:
        top = []

    if not top:
        return f"No relevant pages found for query: {query}"

    _EVAL_QUERY_KWS = {"rq", "research question", "evaluation", "metric",
                       "precision", "recall", "success rate"}
    is_eval_query = any(kw in query.lower() for kw in _EVAL_QUERY_KWS)

    parts: list[str] = []
    budget = max_tokens
    for score, path, content in top:
        rel = path.relative_to(WIKI_DIR)
        header = f"## [{rel}] (score: {score:.4f})\n"

        # Inject structured evaluation protocol block for relevant queries
        eval_prefix = ""
        if is_eval_query:
            fm = _parse_frontmatter(content)
            ep = fm.get("evaluation_protocol") if fm else None
            if ep and ep.get("has_explicit_rqs") and ep.get("rq_list"):
                lines = ["=== STRUCTURED EVALUATION PROTOCOL ==="]
                for rq in ep["rq_list"]:
                    metrics_str = ", ".join(rq.get("metrics", [])) or "see full text"
                    lines.append(f"{rq['id']}: {rq['question'][:150]}")
                    lines.append(f"  Metrics: {metrics_str}")
                lines.append("=" * 40)
                eval_prefix = "\n".join(lines) + "\n\n"

        snippet = _truncate_to_tokens(content, budget // max(len(top), 1))
        entry = header + eval_prefix + snippet + "\n"
        parts.append(entry)
        budget -= len(entry) // 4

    return "\n---\n".join(parts)


# ── MCP Tool Definition (only when mcp SDK is available) ─────────

if _HAS_MCP:

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="kb_retrieve",
                description=(
                    "Retrieve relevant knowledge from the research wiki. "
                    "All agents MUST use this tool instead of directly reading "
                    "knowledge/wiki/ files. Returns matching pages with source refs."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query for the knowledge base",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return",
                            "default": 5,
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": "Maximum tokens in response",
                            "default": 4000,
                        },
                    },
                    "required": ["query"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name != "kb_retrieve":
            raise ValueError(f"Unknown tool: {name}")

        query = arguments["query"]
        max_results = arguments.get("max_results", 5)
        max_tokens = arguments.get("max_tokens", 4000)

        results = search_wiki(query, max_results, max_tokens)
        return [TextContent(type="text", text=results)]


# ── Entry point ──────────────────────────────────────────────────


async def _run() -> None:
    if not _HAS_MCP:
        print("ERROR: mcp SDK not installed. Install with: pip install mcp", file=sys.stderr)
        sys.exit(1)
    from mcp.server import InitializationOptions
    from mcp.types import ServerCapabilities
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="kb",
                server_version="0.1.0",
                capabilities=ServerCapabilities(tools={}),
            ),
        )


def main() -> None:
    """Run as MCP server, or use --query for standalone testing."""
    if len(sys.argv) > 1 and sys.argv[1] == "--query":
        query = " ".join(sys.argv[2:])
        print(search_wiki(query))
        return
    asyncio.run(_run())


if __name__ == "__main__":
    main()
