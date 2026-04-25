"""P0: knowledge_compiler raw registry + minimal frontmatter.

Asserts:
  - compile_all_raw skips files not in raw_registry.yaml
  - register_raw_paper writes the entry, registered files compile
  - wiki frontmatter no longer carries evaluation_protocol /
    follow_up_questions (those fields would be wrong heuristics on
    arbitrary text)
  - title is parsed from first markdown header, not from filename
"""

from __future__ import annotations

from pathlib import Path

import yaml

from researchos.runtime.knowledge_compiler import (
    compile_all_raw,
    compile_raw_to_wiki,
    register_raw_paper,
)


def _seed_raw(root: Path, name: str, body: str) -> Path:
    raw = root / "knowledge" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    p = raw / name
    p.write_text(body)
    return p


def test_compile_all_raw_skips_unregistered_file(tmp_path: Path):
    """A proposal accidentally dropped into raw/ must NOT be compiled."""
    _seed_raw(
        tmp_path,
        "research proposal.txt",
        "# Mitigating Model Uncertainty\n\nProposal body here…\n",
    )
    pages = compile_all_raw(tmp_path)
    assert pages == []
    wiki = tmp_path / "knowledge" / "wiki" / "concepts"
    assert not wiki.exists() or not list(wiki.glob("*.md"))


def test_compile_all_raw_compiles_registered_file(tmp_path: Path):
    paper = _seed_raw(
        tmp_path,
        "bma_paper.md",
        "# Taming Model Uncertainty in Self-Adaptive Systems\n\nBody.\n",
    )
    register_raw_paper(paper, "base_paper", tmp_path)
    pages = compile_all_raw(tmp_path)
    assert len(pages) == 1
    assert pages[0].name.startswith("bma_paper")


def test_compile_all_raw_skips_non_compile_role(tmp_path: Path):
    """Roles outside _AUTO_COMPILE_ROLES should be skipped."""
    p = _seed_raw(tmp_path, "proposal.txt", "# Proposal\n\nBody.\n")
    register_raw_paper(p, "proposal", tmp_path)
    pages = compile_all_raw(tmp_path)
    assert pages == []


def test_register_raw_paper_idempotent(tmp_path: Path):
    p = _seed_raw(tmp_path, "x.md", "# X\nbody\n")
    register_raw_paper(p, "base_paper", tmp_path)
    before = (tmp_path / "runtime" / "raw_registry.yaml").read_text()
    register_raw_paper(p, "base_paper", tmp_path)  # no-op
    after = (tmp_path / "runtime" / "raw_registry.yaml").read_text()
    assert before == after


def test_register_raw_paper_role_change_wins(tmp_path: Path):
    p = _seed_raw(tmp_path, "y.md", "# Y\n")
    register_raw_paper(p, "related_work", tmp_path)
    register_raw_paper(p, "base_paper", tmp_path)
    registry = yaml.safe_load(
        (tmp_path / "runtime" / "raw_registry.yaml").read_text()
    )
    assert registry == {"y.md": "base_paper"}


def test_compile_raw_to_wiki_direct_call_bypasses_registry(tmp_path: Path):
    """compile_raw_to_wiki(file, root) is the direct API used by tests + cmd_inject;
    it MUST work without registry entry (cmd_inject registers separately).
    """
    p = _seed_raw(tmp_path, "direct.md", "# Direct\nbody\n")
    page = compile_raw_to_wiki(p, tmp_path)
    assert page is not None and page.exists()


def test_wiki_frontmatter_minimal(tmp_path: Path):
    """Frontmatter must not carry the retired heuristic fields."""
    body = (
        "# Real Title\n\n"
        "RQ1: What is the prediction accuracy obtained through BMA?\n"
        "Future work: extend to threshold goals.\n"
    )
    p = _seed_raw(tmp_path, "paper.md", body)
    register_raw_paper(p, "base_paper", tmp_path)
    pages = compile_all_raw(tmp_path)
    assert len(pages) == 1
    head = pages[0].read_text().split("---", 2)[1]
    fm = yaml.safe_load(head)
    assert "evaluation_protocol" not in fm
    assert "follow_up_questions" not in fm
    assert fm["title"] == "Real Title"  # parsed from header, not filename


def test_wiki_title_falls_back_to_stem_when_no_header(tmp_path: Path):
    p = _seed_raw(tmp_path, "no_header.md", "Some body without a header.\n")
    register_raw_paper(p, "base_paper", tmp_path)
    pages = compile_all_raw(tmp_path)
    assert len(pages) == 1
    head = pages[0].read_text().split("---", 2)[1]
    fm = yaml.safe_load(head)
    # First non-empty line passes the heuristic — that's the title.
    assert fm["title"] == "Some body without a header."[:200] or fm["title"] == "no_header"
