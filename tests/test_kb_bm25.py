"""Tests for BM25 search quality in kb_server."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from researchos.mcp_servers.kb_server import (
    _compute_idf,
    _score_page_bm25,
    _tokenize,
    search_wiki,
)


def test_stopword_removal():
    """Verify stopwords are filtered from tokenisation."""
    tokens = _tokenize("the attention mechanism is a key component")
    assert "the" not in tokens
    assert "is" not in tokens
    assert "a" not in tokens
    assert "attention" in tokens
    assert "mechanism" in tokens
    assert "key" in tokens
    assert "component" in tokens


def test_short_tokens_removed():
    """Single-char tokens should be filtered."""
    tokens = _tokenize("I am a test x y z")
    # 'I' -> 'i' (len 1, filtered), 'a' (stopword), 'x','y','z' (len 1)
    assert "i" not in tokens
    assert "x" not in tokens
    assert "test" in tokens


def test_cjk_support():
    """CJK characters should be tokenised."""
    tokens = _tokenize("attention 注意力机制 transformer")
    assert "attention" in tokens
    assert "transformer" in tokens
    # CJK chars are extracted individually
    assert any("\u4e00" <= t <= "\u9fff" for t in tokens)


def test_idf_computation(tmp_path):
    """Test IDF computation over a small corpus."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "doc1.md").write_text("attention mechanism transformer model")
    (wiki / "doc2.md").write_text("transformer architecture neural network")
    (wiki / "doc3.md").write_text("reinforcement learning policy gradient")

    pages = [(f, f.read_text()) for f in wiki.rglob("*.md")]

    with patch("researchos.mcp_servers.kb_server.WIKI_DIR", wiki):
        with patch("researchos.mcp_servers.kb_server._idf_cache", {}):
            with patch("researchos.mcp_servers.kb_server._idf_cache_mtime", 0.0):
                idf = _compute_idf(pages)

    # 'transformer' appears in 2/3 docs -> lower IDF
    # 'reinforcement' appears in 1/3 docs -> higher IDF
    assert idf["transformer"] < idf["reinforcement"]
    # 'attention' appears in 1/3 docs -> same IDF as reinforcement
    assert abs(idf["attention"] - idf["reinforcement"]) < 0.01


def test_bm25_length_normalisation():
    """BM25 should prefer shorter documents with same term frequency."""
    idf = {"attention": 1.0, "mechanism": 1.0}

    # Short doc with "attention" once
    short_tokens = _tokenize("attention mechanism is great")
    # Long doc with "attention" once (padded with filler)
    long_tokens = _tokenize(
        "attention " + " ".join(["padding"] * 100)
    )

    short_score = _score_page_bm25(["attention"], short_tokens, idf, avg_dl=50)
    long_score = _score_page_bm25(["attention"], long_tokens, idf, avg_dl=50)

    # Short doc should score higher due to length normalisation
    assert short_score > long_score


def test_bm25_vs_tf_ranking(tmp_path):
    """BM25 should rank focused short docs higher than long docs with many mentions."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    # Doc A: mentions "attention" 50 times in a 500-word doc (high raw TF)
    (wiki / "long.md").write_text(
        "attention " * 50 + " ".join(["other"] * 450)
    )
    # Doc B: focused 20-word doc about attention mechanism
    (wiki / "focused.md").write_text(
        "attention mechanism transformer architecture "
        "multi head attention self attention cross attention "
        "scaled dot product attention key query value "
        "positional encoding layer normalisation"
    )

    with patch("researchos.mcp_servers.kb_server.WIKI_DIR", wiki):
        with patch("researchos.mcp_servers.kb_server._idf_cache", {}):
            with patch("researchos.mcp_servers.kb_server._idf_cache_mtime", 0.0):
                result = search_wiki("attention mechanism", max_results=2)

    # The focused doc should rank first
    lines = result.split("\n")
    first_result_line = [l for l in lines if l.startswith("## [")][0]
    assert "focused.md" in first_result_line


def test_search_empty_wiki(tmp_path):
    """Search on empty wiki should return a helpful message."""
    wiki = tmp_path / "empty_wiki"
    wiki.mkdir()

    with patch("researchos.mcp_servers.kb_server.WIKI_DIR", wiki):
        result = search_wiki("anything")

    assert "empty" in result.lower() or "No wiki pages" in result


def test_search_no_matches(tmp_path):
    """Search with no matching results should say so."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "doc.md").write_text("alpha beta gamma delta")

    with patch("researchos.mcp_servers.kb_server.WIKI_DIR", wiki):
        with patch("researchos.mcp_servers.kb_server._idf_cache", {}):
            with patch("researchos.mcp_servers.kb_server._idf_cache_mtime", 0.0):
                result = search_wiki("xylophone zephyr")

    assert "No relevant pages" in result
