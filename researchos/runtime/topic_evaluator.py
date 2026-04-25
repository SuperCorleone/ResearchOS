"""Topic Evaluator -- 8-step evaluation pipeline for research proposals.

Sits between Knowledge Miner output and Professor decision.
Produces ``evaluated_<id>.yaml`` with scores, similarity analysis,
innovation statement, falsification design, and gate result.

Steps:
  1. Local KB retrieval (duplicate/overlap check)
  2. Multi-source online retrieval (Semantic Scholar + arXiv, cached)
  3. Four-axis similarity analysis (LLM)
  4. Five-dimension scoring (LLM)
  5. Innovation statement template (LLM)
  6. Falsification design (LLM)
  7. Gating rule application
  8. Output evaluated_<id>.yaml
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from researchos.schemas.evaluation import (
    EvaluationScores,
    FalsificationDesign,
    GateResult,
    InnovationStatement,
    RepeatDegree,
    SimilarPaper,
    SimilarityAxis,
    TopicEvaluation,
)

logger = logging.getLogger(__name__)

SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1"
ARXIV_API_URL = "http://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"

# ── Retrieval cache ──────────────────────────────────────────────

_retrieval_cache: dict[str, tuple[float, list[dict]]] = {}


def _cache_key(direction: str, keywords: list[str], max_results: int) -> str:
    raw = f"{direction}|{'|'.join(sorted(keywords))}|{max_results}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str, ttl_hours: int = 24) -> list[dict] | None:
    if key in _retrieval_cache:
        ts, data = _retrieval_cache[key]
        if time.time() - ts < ttl_hours * 3600:
            return data
        del _retrieval_cache[key]
    return None


def _cache_set(key: str, data: list[dict]) -> None:
    _retrieval_cache[key] = (time.time(), data)


# ── Config loading ───────────────────────────────────────────────


def load_eval_config(project_root: Path) -> dict[str, Any]:
    """Load research_config.yaml with defaults."""
    config_path = project_root / "runtime" / "research_config.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


def _gating_config(config: dict) -> dict:
    return config.get("topic_gating", {})


def _retrieval_config(config: dict) -> dict:
    return config.get("retrieval", {})


def _backlog_config(config: dict) -> dict:
    return config.get("backlog", {})


def _innovation_config(config: dict) -> dict:
    return config.get("innovation", {})


# ── Step 1: Local KB retrieval ───────────────────────────────────


def step1_kb_search(
    project_root: Path, title: str, description: str
) -> tuple[list[str], list[str]]:
    """Search local KB for duplicates and related work.

    Returns (duplicates, related) as lists of wiki page names.
    Uses simple BM25-style keyword matching against wiki files.
    """
    wiki_dir = project_root / "knowledge" / "wiki"
    if not wiki_dir.exists():
        return [], []

    query_terms = set(
        re.findall(r"\w{4,}", (title + " " + description).lower())
    )
    if not query_terms:
        return [], []

    duplicates: list[str] = []
    related: list[str] = []

    # Also check existing pipelines and failures
    for search_dir in [wiki_dir, project_root / "knowledge" / "wiki" / "failures"]:
        if not search_dir.exists():
            continue
        for md_file in search_dir.rglob("*.md"):
            try:
                content = md_file.read_text().lower()
            except Exception:
                continue
            hits = sum(1 for t in query_terms if t in content)
            ratio = hits / len(query_terms) if query_terms else 0
            rel_name = str(md_file.relative_to(wiki_dir)) if wiki_dir in md_file.parents or md_file.parent == wiki_dir else md_file.name
            if ratio > 0.7:
                duplicates.append(rel_name)
            elif ratio > 0.3:
                related.append(rel_name)

    # Check existing pipeline titles
    artifacts_dir = project_root / "artifacts"
    if artifacts_dir.exists():
        for pdir in artifacts_dir.iterdir():
            if not pdir.is_dir():
                continue
            state_file = pdir / "state.json"
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    ptitle = state.get("title", "").lower()
                    pq = state.get("research_question", "").lower()
                    combined = ptitle + " " + pq
                    hits = sum(1 for t in query_terms if t in combined)
                    ratio = hits / len(query_terms)
                    if ratio > 0.5:
                        duplicates.append(f"pipeline:{state.get('pipeline_id', pdir.name)}")
                except Exception:
                    continue

    return duplicates, related


# ── Step 2: Multi-source online retrieval ────────────────────────


def _search_semantic_scholar(
    keywords: list[str], max_results: int = 10
) -> list[dict]:
    """Search Semantic Scholar for papers matching keywords."""
    query = " ".join(keywords)
    params = urllib.parse.urlencode({
        "query": query,
        "limit": max_results,
        "fields": "title,authors,year,venue,citationCount,externalIds",
    })
    url = f"{SEMANTIC_SCHOLAR_API}/paper/search?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        papers = []
        for p in data.get("data", []):
            ext_ids = p.get("externalIds", {}) or {}
            papers.append({
                "title": p.get("title", ""),
                "authors": [a.get("name", "") for a in (p.get("authors") or [])],
                "year": p.get("year"),
                "venue": p.get("venue", ""),
                "citation_count": p.get("citationCount", 0) or 0,
                "doi": ext_ids.get("DOI", ""),
                "arxiv_id": ext_ids.get("ArXiv", ""),
                "source": "semantic_scholar",
            })
        return papers
    except Exception as e:
        logger.warning("Semantic Scholar search failed: %s", e)
        return []


def _search_arxiv(keywords: list[str], max_results: int = 10) -> list[dict]:
    """Search arXiv API for papers matching keywords."""
    query = "+AND+".join(urllib.parse.quote(k) for k in keywords)
    url = (
        f"{ARXIV_API_URL}?search_query=all:{query}"
        f"&start=0&max_results={max_results}&sortBy=relevance"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ResearchOS/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_data = resp.read()
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_data)
        papers = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            title = (entry.findtext(f"{ATOM_NS}title") or "").strip().replace("\n", " ")
            authors = [
                a.findtext(f"{ATOM_NS}name") or ""
                for a in entry.findall(f"{ATOM_NS}author")
            ]
            published = entry.findtext(f"{ATOM_NS}published") or ""
            year = int(published[:4]) if len(published) >= 4 else None
            arxiv_id = ""
            for link in entry.findall(f"{ATOM_NS}link"):
                href = link.get("href", "")
                if "abs/" in href:
                    arxiv_id = href.split("abs/")[-1]
                    break
            papers.append({
                "title": title,
                "authors": authors,
                "year": year,
                "venue": "arXiv",
                "citation_count": 0,
                "doi": "",
                "arxiv_id": arxiv_id,
                "source": "arxiv",
            })
        return papers
    except Exception as e:
        logger.warning("arXiv search failed: %s", e)
        return []


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def step2_online_retrieval(
    direction: str,
    keywords: list[str],
    config: dict,
) -> list[SimilarPaper]:
    """Retrieve papers from Semantic Scholar and arXiv, merge and deduplicate.

    Applies time decay and citation weighting.
    """
    rc = _retrieval_config(config)
    max_results = rc.get("max_results", 10)
    cache_ttl = rc.get("cache_ttl_hours", 24)
    time_decay = rc.get("time_decay_factor", 0.8)
    citation_threshold = rc.get("citation_threshold", 3)
    top_venues = set(rc.get("top_venues", []))

    key = _cache_key(direction, keywords, max_results)
    cached = _cache_get(key, cache_ttl)
    if cached is not None:
        return [SimilarPaper(**p) for p in cached]

    # Fetch from both sources
    ss_papers = _search_semantic_scholar(keywords, max_results)
    ax_papers = _search_arxiv(keywords, max_results)

    # Merge and deduplicate by DOI, arXiv ID, or normalized title
    seen: set[str] = set()
    merged: list[dict] = []
    for p in ss_papers + ax_papers:
        dedup_keys = []
        if p.get("doi"):
            dedup_keys.append(f"doi:{p['doi']}")
        if p.get("arxiv_id"):
            dedup_keys.append(f"arxiv:{p['arxiv_id']}")
        dedup_keys.append(f"title:{_normalize_title(p.get('title', ''))}")

        if any(k in seen for k in dedup_keys):
            continue
        seen.update(dedup_keys)

        # Apply time decay and citation weighting
        current_year = datetime.now(timezone.utc).year
        weight = 1.0
        year = p.get("year")
        if year and year < current_year - 2:
            weight *= time_decay
        citations = p.get("citation_count", 0) or 0
        if citations < citation_threshold:
            weight *= 0.7
        venue = p.get("venue", "")
        if venue and venue not in top_venues:
            weight *= 0.9

        p["weighted_similarity"] = round(weight, 3)
        merged.append(p)

    # Sort by weighted similarity descending, take top 10
    merged.sort(key=lambda x: -x.get("weighted_similarity", 0))
    merged = merged[:max_results]

    # Cache results
    _cache_set(key, merged)

    return [SimilarPaper(**p) for p in merged]


# ── Steps 3-6: LLM-based analysis ───────────────────────────────


def _build_analysis_prompt(
    proposal: dict, top_papers: list[SimilarPaper]
) -> str:
    """Build a single LLM prompt for steps 3, 4, 5, and 6 combined.

    We combine into one call to reduce LLM invocations.
    """
    papers_desc = ""
    for i, p in enumerate(top_papers[:5], 1):
        papers_desc += (
            f"\n### Paper {i}: {p.title}\n"
            f"- Authors: {', '.join(p.authors[:3])}\n"
            f"- Year: {p.year}, Venue: {p.venue}, Citations: {p.citation_count}\n"
        )

    title = proposal.get("title", "")
    question = proposal.get("research_question", "")
    hypothesis = proposal.get("hypothesis", "")
    motivation = proposal.get("motivation", "")

    no_papers = not top_papers
    papers_section = (
        f"## Most Similar Papers\n{papers_desc}\n\n"
        if not no_papers else
        "## Most Similar Papers\nNo similar papers found in literature search.\n\n"
    )

    axes_instruction = (
        "### 1. Four-Axis Similarity (for top 5 papers)\n"
        "For each paper, classify each axis as 'similar' or 'different':\n"
        "- problem: Is the problem being solved the same?\n"
        "- method: Is the proposed method similar?\n"
        "- setting: Is the experimental setting the same?\n"
        "- metric: Are the evaluation metrics the same?\n\n"
        if not no_papers else
        "### 1. Four-Axis Similarity\n"
        "No papers to compare. Set axes to empty list.\n\n"
    )

    falsification_instruction = (
        "### 4. Falsification Design\n"
        "Based on the most similar paper's differences, design:\n"
        if not no_papers else
        "### 4. Falsification Design\n"
        "No similar papers available. Design a falsification test based on the "
        "proposal's own hypothesis — pick a reasonable baseline method, define a "
        "concrete metric, and specify what result would disprove the hypothesis:\n"
    )

    return (
        "You are evaluating a research proposal against existing literature.\n\n"
        f"## Proposal\n"
        f"- Title: {title}\n"
        f"- Research Question: {question}\n"
        f"- Hypothesis: {hypothesis}\n"
        f"- Motivation: {motivation}\n\n"
        f"{papers_section}"
        "## Your Tasks\n\n"
        "Analyze the proposal and output a SINGLE JSON object (no markdown fences) with these fields:\n\n"
        f"{axes_instruction}"
        "### 2. Five-Dimension Scoring (0-3 each)\n"
        "- significance: How important is the problem?\n"
        "- novelty: How different from existing work?\n"
        "- differentiation: How clearly articulated is the difference?\n"
        "- feasibility: How technically feasible?\n"
        "- falsifiability: Can the hypothesis be clearly tested/disproved?\n\n"
        "### 3. Innovation Statement\n"
        "Fill in:\n"
        "- problem_gap: 'Existing methods fail because ______'\n"
        "- our_delta: 'We propose ______'\n"
        "- why_it_matters: 'Which improves ______ under ______'\n\n"
        f"{falsification_instruction}"
        "- hypothesis: specific testable claim\n"
        "- baseline: comparison method\n"
        "- metric: evaluation metric\n"
        "- failure_condition: e.g. 'improvement < 2%'\n"
        "- expected_signal: minimum expected improvement\n"
        "- min_budget_ratio: fraction of budget for this test (0.0-1.0)\n\n"
        "## Output Format (JSON)\n"
        "```\n"
        "{\n"
        '  "axes": [\n'
        '    {"paper_index": 1, "problem": "similar", "method": "different", "setting": "similar", "metric": "different"},\n'
        "    ...\n"
        "  ],\n"
        '  "scores": {"significance": 2, "novelty": 2, "differentiation": 2, "feasibility": 3, "falsifiability": 2},\n'
        '  "innovation": {"problem_gap": "...", "our_delta": "...", "why_it_matters": "..."},\n'
        '  "falsification": {"hypothesis": "...", "baseline": "...", "metric": "...", "failure_condition": "...", "expected_signal": "...", "min_budget_ratio": 0.1}\n'
        "}\n"
        "```\n\n"
        "Output ONLY the JSON object, no other text.\n"
    )


def _parse_llm_analysis(raw_output: str) -> dict:
    """Extract JSON from LLM output, handling markdown fences."""
    # Strip markdown code fences if present
    text = raw_output.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # skip opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    logger.warning("Failed to parse LLM analysis output")
    return {}


# ── 5-axis quick gate (DeepScientist-style pre-filter) ──────────


_FIVE_AXIS_KEYS = (
    "novelty",
    "falsifiability",
    "feasibility",
    "evidence_quality",
    "constraint_fit",
)


def _five_axis_prompt(candidate: dict) -> str:
    title = candidate.get("title", "")
    question = candidate.get("research_question", "")
    hypothesis = candidate.get("hypothesis", "")
    motivation = candidate.get("motivation", "")
    constraints = candidate.get("constraints", "")
    return (
        "You are a fast research-proposal triage reviewer using the "
        "DeepScientist 5-axis rubric. Score each axis as 0, 1, or 2 "
        "(0=fails, 1=weak, 2=strong). Axes:\n"
        "- novelty: different from known work\n"
        "- falsifiability: a concrete failure condition exists\n"
        "- feasibility: can be executed with available resources\n"
        "- evidence_quality: supported by evidence / prior signal\n"
        "- constraint_fit: fits stated constraints (budget, stage)\n\n"
        f"## Proposal\nTitle: {title}\nQuestion: {question}\n"
        f"Hypothesis: {hypothesis}\nMotivation: {motivation}\n"
        f"Constraints: {constraints}\n\n"
        "Output ONLY a compact JSON object like:\n"
        "{\"novelty\":2,\"falsifiability\":2,\"feasibility\":1,"
        "\"evidence_quality\":1,\"constraint_fit\":2}\n"
    )


def _parse_five_axis_scores(raw: str) -> dict[str, int] | None:
    """Extract {axis:int} from LLM text output."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*?\}", text)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    out: dict[str, int] = {}
    for k in _FIVE_AXIS_KEYS:
        try:
            v = int(data.get(k, 0))
        except (TypeError, ValueError):
            v = 0
        out[k] = max(0, min(2, v))
    return out


async def five_axis_quick_gate(
    candidate: dict, dispatcher: Any | None = None
) -> tuple[int, str]:
    """DeepScientist-style 5-axis quick triage gate.

    Returns ``(total_score_out_of_10, verdict)`` where verdict is one of
    ``{"reject", "review", "pass"}``.

    Threshold: strictly less than 7/10 → ``"reject"`` (do NOT promote).
    Scores in ``[7, 8]`` → ``"review"``. Scores ``>= 9`` → ``"pass"``.

    When ``dispatcher`` is provided, the rubric is scored via LLM. Otherwise
    a deterministic placeholder returns ``(5, "review")`` so callers can run
    in test/offline mode without an LLM.

    Source: DeepScientist OSS selection-gate.md.
    """
    if dispatcher is None:
        return 5, "review"

    scores: dict[str, int] | None = None
    try:
        result = await dispatcher.dispatch_via_executor(
            "professor", _five_axis_prompt(candidate), "_system_quick_gate"
        )
        if result.get("success"):
            scores = _parse_five_axis_scores(result.get("output", ""))
    except Exception:
        logger.exception("5-axis quick gate LLM dispatch failed")

    if not scores:
        return 5, "review"
    total = sum(scores.values())
    if total < 7:
        verdict = "reject"
    elif total >= 9:
        verdict = "pass"
    else:
        verdict = "review"
    return total, verdict


# ── Step 7: Gating rules ────────────────────────────────────────


def step7_apply_gating(
    scores: EvaluationScores,
    repeat_degree: RepeatDegree,
    config: dict,
) -> tuple[GateResult, list[str]]:
    """Apply gating rules from config. Returns (gate_result, reasons)."""
    gc = _gating_config(config)
    min_total = gc.get("min_total_score", 9)
    min_sig = gc.get("min_significance", 2)
    min_fals = gc.get("min_falsifiability", 2)
    backlog_min = gc.get("backlog_min_score", 5)
    direct_reject = gc.get("direct_reject_if", {})
    prof_review = gc.get("professor_review_if", {})

    reasons: list[str] = []

    # Direct reject checks
    if direct_reject.get("novelty_zero") and scores.novelty == 0:
        reasons.append("novelty == 0")
    if direct_reject.get("falsifiability_zero") and scores.falsifiability == 0:
        reasons.append("falsifiability == 0")
    if direct_reject.get("significance_lte_1") and scores.significance <= 1:
        reasons.append(f"significance ({scores.significance}) <= 1")
    if direct_reject.get("repeat_degree_high") and repeat_degree == RepeatDegree.HIGH:
        reasons.append("repeat_degree == high (4/4 axes similar)")

    if reasons:
        return GateResult.REJECT, reasons

    # Professor review checks
    review_reasons: list[str] = []
    if prof_review.get("repeat_degree_medium") and repeat_degree == RepeatDegree.MEDIUM:
        review_reasons.append("repeat_degree == medium (3/4 axes similar)")
    if prof_review.get("differentiation_lte_1") and scores.differentiation <= 1:
        review_reasons.append(f"differentiation ({scores.differentiation}) <= 1")

    if review_reasons:
        return GateResult.PROFESSOR_REVIEW, review_reasons

    # Score threshold check
    total = scores.total
    if total >= min_total and scores.significance >= min_sig and scores.falsifiability >= min_fals:
        return GateResult.PASS, [f"total={total} >= {min_total}"]

    if total >= backlog_min:
        return GateResult.BACKLOG, [f"total={total} in [{backlog_min}, {min_total})"]

    return GateResult.REJECT, [f"total={total} < {backlog_min}"]


def _determine_repeat_degree(
    axes_list: list[SimilarityAxis],
    innovation_type: str = "",
) -> RepeatDegree:
    """Determine repeat degree from the most similar paper's axes.

    For ``methodological_extension`` and ``hybrid_combination`` innovation
    types the thresholds are raised: 4/4 axes required for HIGH (auto-reject)
    and 4/4 for MEDIUM (professor review) instead of the default 3/4.  This
    prevents the system from auto-rejecting proposals that deliberately share
    problem/setting/metric with a reference but add a novel mechanism.
    """
    if not axes_list:
        return RepeatDegree.LOW

    # Use the paper with the highest similarity count
    max_similar = max(a.similar_count for a in axes_list)

    # Extension-type proposals get higher tolerance — they intentionally
    # share axes with existing work while adding novel mechanisms.
    _extension_types = {"methodological_extension", "hybrid_combination"}
    if innovation_type in _extension_types:
        if max_similar >= 4:
            return RepeatDegree.HIGH
        else:
            # 3/4 or fewer is acceptable for extension proposals
            return RepeatDegree.LOW if max_similar <= 1 else RepeatDegree.MEDIUM_LOW
    else:
        if max_similar >= 4:
            return RepeatDegree.HIGH
        elif max_similar == 3:
            return RepeatDegree.MEDIUM
        elif max_similar == 2:
            return RepeatDegree.MEDIUM_LOW
        else:
            return RepeatDegree.LOW


def _apply_innovation_bonus(
    scores: EvaluationScores,
    innovation_type: str,
    config: dict,
) -> int:
    """Return bonus points for the proposal's innovation type.

    Reads ``innovation.cross_domain_bonus``, ``innovation.cross_method_bonus``,
    and ``innovation.extension_bonus`` from *config* and returns the applicable
    bonus (at most one type applies).  The caller adds this to the total score
    used in gating decisions.
    """
    ic = _innovation_config(config)
    bonus_map: dict[str, str] = {
        "cross_domain": "cross_domain_bonus",
        "cross_method": "cross_method_bonus",
        "methodological_extension": "extension_bonus",
        "hybrid_combination": "extension_bonus",
    }
    key = bonus_map.get(innovation_type, "")
    if key:
        return int(ic.get(key, 0))
    return 0


# ── Main evaluation pipeline ────────────────────────────────────


class TopicEvaluator:
    """Runs the 8-step evaluation pipeline for a research proposal."""

    def __init__(self, project_root: Path, dispatcher: Any) -> None:
        self.project_root = project_root
        self.dispatcher = dispatcher
        self.config = load_eval_config(project_root)

    async def evaluate(self, proposal_data: dict) -> TopicEvaluation:
        """Run all 8 steps and return a TopicEvaluation."""
        title = proposal_data.get("title", "Untitled")
        question = proposal_data.get("research_question", "")
        direction = proposal_data.get("direction", "general")
        hypothesis = proposal_data.get("hypothesis", "")
        motivation = proposal_data.get("motivation", "")
        innovation_type = proposal_data.get("innovation_type", "")
        proposal_id = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:30]

        logger.info("Evaluating proposal: %s", title)

        # Early 5-axis quick gate (pre-filter)
        quick_total, quick_verdict = await five_axis_quick_gate(
            proposal_data, dispatcher=self.dispatcher
        )
        logger.info(
            "5-axis quick gate: total=%d/10 verdict=%s", quick_total, quick_verdict
        )
        if quick_verdict == "reject":
            evaluation = TopicEvaluation(
                proposal_id=proposal_id,
                title=title,
                research_question=question,
                direction=direction,
                kb_duplicates=[],
                kb_related=[],
                similar_papers=[],
                retrieval_cache_key="",
                repeat_degree=RepeatDegree.LOW,
                scores=EvaluationScores(
                    significance=0, novelty=0, differentiation=0,
                    feasibility=0, falsifiability=0,
                ),
                innovation=InnovationStatement(),
                falsification=FalsificationDesign(),
                gate_result=GateResult.REJECT,
                gate_reasons=[
                    f"5-axis quick gate total={quick_total}/10 < 7 "
                    "(reject short-circuit)"
                ],
                evaluated_at=datetime.now(timezone.utc).isoformat(),
            )
            self._write_evaluation(evaluation)
            logger.info(
                "Quick-gate reject: %s (total=%d)", title, quick_total
            )
            return evaluation

        # Step 1: Local KB search
        kb_duplicates, kb_related = step1_kb_search(
            self.project_root, title, question + " " + hypothesis
        )
        if kb_duplicates:
            logger.info("KB duplicates found: %s", kb_duplicates)

        # Step 2: Online retrieval
        keywords = _extract_keywords(title, question, hypothesis)
        similar_papers = step2_online_retrieval(direction, keywords, self.config)
        logger.info("Retrieved %d similar papers", len(similar_papers))

        # Steps 3-6: LLM analysis (combined into one call)
        axes_list: list[SimilarityAxis] = []
        scores = EvaluationScores()
        innovation = InnovationStatement()
        falsification = FalsificationDesign()

        if similar_papers:
            llm_result = await self._run_llm_analysis(proposal_data, similar_papers)
            if llm_result:
                # Parse axes
                for ax_data in llm_result.get("axes", []):
                    axes_list.append(SimilarityAxis(
                        problem=ax_data.get("problem", "different"),
                        method=ax_data.get("method", "different"),
                        setting=ax_data.get("setting", "different"),
                        metric=ax_data.get("metric", "different"),
                    ))

                # Parse scores
                s = llm_result.get("scores", {})
                scores = EvaluationScores(
                    significance=_clamp(s.get("significance", 1), 0, 3),
                    novelty=_clamp(s.get("novelty", 1), 0, 3),
                    differentiation=_clamp(s.get("differentiation", 1), 0, 3),
                    feasibility=_clamp(s.get("feasibility", 1), 0, 3),
                    falsifiability=_clamp(s.get("falsifiability", 1), 0, 3),
                )

                # Parse innovation
                inn = llm_result.get("innovation", {})
                innovation = InnovationStatement(
                    problem_gap=inn.get("problem_gap", ""),
                    our_delta=inn.get("our_delta", ""),
                    why_it_matters=inn.get("why_it_matters", ""),
                )

                # Parse falsification
                fals = llm_result.get("falsification", {})
                falsification = FalsificationDesign(
                    hypothesis=fals.get("hypothesis", ""),
                    baseline=fals.get("baseline", ""),
                    metric=fals.get("metric", ""),
                    failure_condition=fals.get("failure_condition", ""),
                    expected_signal=fals.get("expected_signal", ""),
                    min_budget_ratio=float(fals.get("min_budget_ratio", 0.1)),
                )
        else:
            # No similar papers found — still run LLM for scoring,
            # innovation, and falsification based on proposal alone
            llm_result = await self._run_llm_analysis(proposal_data, [])
            if llm_result:
                s = llm_result.get("scores", {})
                scores = EvaluationScores(
                    significance=_clamp(s.get("significance", 2), 0, 3),
                    novelty=_clamp(s.get("novelty", 3), 0, 3),
                    differentiation=_clamp(s.get("differentiation", 2), 0, 3),
                    feasibility=_clamp(s.get("feasibility", 2), 0, 3),
                    falsifiability=_clamp(s.get("falsifiability", 2), 0, 3),
                )
                inn = llm_result.get("innovation", {})
                innovation = InnovationStatement(
                    problem_gap=inn.get("problem_gap", ""),
                    our_delta=inn.get("our_delta", ""),
                    why_it_matters=inn.get("why_it_matters", ""),
                )
                fals = llm_result.get("falsification", {})
                falsification = FalsificationDesign(
                    hypothesis=fals.get("hypothesis", ""),
                    baseline=fals.get("baseline", ""),
                    metric=fals.get("metric", ""),
                    failure_condition=fals.get("failure_condition", ""),
                    expected_signal=fals.get("expected_signal", ""),
                    min_budget_ratio=float(fals.get("min_budget_ratio", 0.1)),
                )
            else:
                # LLM failed too — use conservative defaults
                scores = EvaluationScores(
                    significance=2, novelty=3, differentiation=2,
                    feasibility=2, falsifiability=2,
                )

        # Apply axes to similar_papers
        for i, paper in enumerate(similar_papers[:len(axes_list)]):
            paper.axes = axes_list[i]

        # Step 7: Gating rules
        repeat_degree = _determine_repeat_degree(axes_list, innovation_type)

        # Apply innovation-type bonus to scoring before gating
        innovation_bonus = _apply_innovation_bonus(
            scores, innovation_type, self.config
        )
        if innovation_bonus:
            # Add bonus to novelty (capped at 3) so it flows through EvaluationScores.total
            boosted = min(3, scores.novelty + innovation_bonus)
            if boosted != scores.novelty:
                logger.info(
                    "Innovation bonus +%d for %s: novelty %d -> %d",
                    innovation_bonus, innovation_type, scores.novelty, boosted,
                )
                scores = EvaluationScores(
                    significance=scores.significance,
                    novelty=boosted,
                    differentiation=scores.differentiation,
                    feasibility=scores.feasibility,
                    falsifiability=scores.falsifiability,
                )

        gate_result, gate_reasons = step7_apply_gating(
            scores, repeat_degree, self.config
        )

        # Step 8: Build output
        evaluation = TopicEvaluation(
            proposal_id=proposal_id,
            title=title,
            research_question=question,
            direction=direction,
            kb_duplicates=kb_duplicates,
            kb_related=kb_related,
            similar_papers=similar_papers[:10],
            retrieval_cache_key=_cache_key(direction, keywords, 10),
            repeat_degree=repeat_degree,
            scores=scores,
            innovation=innovation,
            falsification=falsification,
            gate_result=gate_result,
            gate_reasons=gate_reasons,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Write evaluated file
        self._write_evaluation(evaluation)
        logger.info(
            "Evaluation complete: %s -> %s (score=%d)",
            title, gate_result, scores.total,
        )
        return evaluation

    async def _run_llm_analysis(
        self, proposal: dict, papers: list[SimilarPaper]
    ) -> dict:
        """Run combined steps 3-6 via a single LLM call."""
        prompt = _build_analysis_prompt(proposal, papers)
        try:
            result = await self.dispatcher.dispatch_via_executor(
                "professor", prompt, "_system_evaluator"
            )
            if result["success"]:
                return _parse_llm_analysis(result.get("output", ""))
            else:
                logger.warning("LLM analysis failed: %s", result.get("output", ""))
        except Exception:
            logger.exception("LLM analysis error")
        return {}

    def _write_evaluation(self, evaluation: TopicEvaluation) -> None:
        """Write evaluated_<id>.yaml to proposals directory."""
        proposals_dir = self.project_root / "knowledge" / "proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        out_path = proposals_dir / f"evaluated_{evaluation.proposal_id}.yaml"
        # Convert to dict, then to YAML
        data = json.loads(evaluation.model_dump_json())
        out_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))


# ── Backlog management ───────────────────────────────────────────


class BacklogManager:
    """Manages the backlog of proposals that didn't pass gating but have potential."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.backlog_dir = project_root / "knowledge" / "backlog"
        self.archive_dir = project_root / "knowledge" / "archive"

    def add_to_backlog(self, proposal_data: dict, evaluation: TopicEvaluation) -> Path:
        """Add a proposal to the backlog with its evaluation."""
        self.backlog_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "proposal": proposal_data,
            "evaluation": json.loads(evaluation.model_dump_json()),
            "added_at": datetime.now(timezone.utc).isoformat(),
            "re_eval_count": 0,
        }
        path = self.backlog_dir / f"backlog_{evaluation.proposal_id}.yaml"
        path.write_text(yaml.dump(entry, default_flow_style=False, allow_unicode=True))
        logger.info("Added to backlog: %s", evaluation.title)
        return path

    def list_backlog(self) -> list[dict]:
        """List all backlog entries."""
        if not self.backlog_dir.exists():
            return []
        entries = []
        for f in sorted(self.backlog_dir.glob("backlog_*.yaml")):
            try:
                entries.append(yaml.safe_load(f.read_text()))
            except Exception:
                continue
        return entries

    def sweep_expired(self, config: dict) -> list[str]:
        """Archive expired backlog items. Returns list of archived proposal IDs."""
        bc = _backlog_config(config)
        max_age_days = bc.get("max_age_days", 3)
        max_re_evals = bc.get("max_re_evals", 3)
        now = datetime.now(timezone.utc)

        expired_dir = self.archive_dir / "expired_ideas"
        expired_dir.mkdir(parents=True, exist_ok=True)

        archived = []
        if not self.backlog_dir.exists():
            return archived

        for f in list(self.backlog_dir.glob("backlog_*.yaml")):
            try:
                entry = yaml.safe_load(f.read_text())
                added = datetime.fromisoformat(entry["added_at"])
                age_days = (now - added).total_seconds() / 86400
                re_evals = entry.get("re_eval_count", 0)

                if age_days > max_age_days or re_evals >= max_re_evals:
                    dest = expired_dir / f.name
                    f.rename(dest)
                    proposal_id = entry.get("evaluation", {}).get("proposal_id", f.stem)
                    archived.append(proposal_id)
                    logger.info("Archived expired backlog: %s (age=%.1fd, re_evals=%d)",
                                proposal_id, age_days, re_evals)
            except Exception:
                logger.warning("Failed to process backlog file: %s", f)

        return archived

    def archive_rejected(self, evaluation: TopicEvaluation) -> None:
        """Archive a permanently rejected proposal and record to wiki failures."""
        rejected_dir = self.archive_dir / "rejected_ideas"
        rejected_dir.mkdir(parents=True, exist_ok=True)
        data = json.loads(evaluation.model_dump_json())
        path = rejected_dir / f"rejected_{evaluation.proposal_id}.yaml"
        path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))

        # Also append to wiki/failures/duplicate_ideas.md for knowledge tracking
        self._record_to_wiki_failures(evaluation)

    def _record_to_wiki_failures(self, evaluation: TopicEvaluation) -> None:
        """Append rejected/duplicate idea summary to knowledge wiki."""
        failures_dir = self.project_root / "knowledge" / "wiki" / "failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        dup_file = failures_dir / "duplicate_ideas.md"

        repeat_deg = evaluation.repeat_degree.value if hasattr(evaluation.repeat_degree, 'value') else str(evaluation.repeat_degree)
        gate_res = evaluation.gate_result.value if hasattr(evaluation.gate_result, 'value') else str(evaluation.gate_result)

        entry = (
            f"\n## {evaluation.title}\n"
            f"- **ID**: {evaluation.proposal_id}\n"
            f"- **Direction**: {evaluation.direction}\n"
            f"- **Repeat Degree**: {repeat_deg}\n"
            f"- **Score**: {evaluation.scores.total}/15\n"
            f"- **Gate Result**: {gate_res}\n"
            f"- **Reasons**: {', '.join(evaluation.gate_reasons)}\n"
            f"- **Rejected At**: {evaluation.evaluated_at}\n"
        )

        if dup_file.exists():
            content = dup_file.read_text()
        else:
            content = (
                "---\ntitle: Duplicate and Rejected Ideas\n"
                "category: failures\n---\n\n"
                "# Duplicate and Rejected Research Ideas\n\n"
                "This page tracks proposals that were rejected by the Topic Evaluator "
                "due to high overlap with existing work or insufficient novelty.\n"
            )

        content += entry
        dup_file.write_text(content)
        logger.info("Recorded rejected idea to wiki: %s", evaluation.proposal_id)

    def get_re_eval_candidates(self, config: dict) -> list[tuple[Path, dict]]:
        """Get backlog entries ready for re-evaluation."""
        bc = _backlog_config(config)
        interval_hours = bc.get("re_eval_interval_hours", 24)
        max_re_evals = bc.get("max_re_evals", 3)
        now = datetime.now(timezone.utc)

        candidates = []
        if not self.backlog_dir.exists():
            return candidates

        for f in sorted(self.backlog_dir.glob("backlog_*.yaml")):
            try:
                entry = yaml.safe_load(f.read_text())
                re_evals = entry.get("re_eval_count", 0)
                if re_evals >= max_re_evals:
                    continue

                # Check if enough time passed since last eval
                eval_time = entry.get("evaluation", {}).get("evaluated_at", entry.get("added_at", ""))
                if eval_time:
                    last = datetime.fromisoformat(eval_time)
                    hours_since = (now - last).total_seconds() / 3600
                    if hours_since < interval_hours:
                        continue

                candidates.append((f, entry))
            except Exception:
                continue

        return candidates

    def update_after_re_eval(
        self, backlog_path: Path, new_evaluation: TopicEvaluation
    ) -> None:
        """Update a backlog entry after re-evaluation."""
        try:
            entry = yaml.safe_load(backlog_path.read_text())
            entry["evaluation"] = json.loads(new_evaluation.model_dump_json())
            entry["re_eval_count"] = entry.get("re_eval_count", 0) + 1
            backlog_path.write_text(
                yaml.dump(entry, default_flow_style=False, allow_unicode=True)
            )
        except Exception:
            logger.exception("Failed to update backlog entry: %s", backlog_path)

    def promote_from_backlog(self, backlog_path: Path) -> dict | None:
        """Remove from backlog and return proposal data for pipeline creation."""
        try:
            entry = yaml.safe_load(backlog_path.read_text())
            backlog_path.unlink()
            return entry.get("proposal")
        except Exception:
            logger.exception("Failed to promote from backlog: %s", backlog_path)
            return None


# ── Competitive idea selection ───────────────────────────────────


# Default weights across the 5 EvaluationScores dimensions. Sum = 1.0.
# Override in research_config.yaml::tournament.weights.
_DEFAULT_TOURNAMENT_WEIGHTS: dict[str, float] = {
    "significance":     0.25,
    "novelty":          0.25,
    "differentiation":  0.15,
    "feasibility":      0.20,
    "falsifiability":   0.15,
}


def _tournament_weights(config: dict | None) -> dict[str, float]:
    """Resolve weights: research_config.yaml overrides default, normalised to 1.0."""
    cfg = (config or {}).get("tournament", {}).get("weights", {}) or {}
    weights = dict(_DEFAULT_TOURNAMENT_WEIGHTS)
    for k, v in cfg.items():
        if k in weights:
            try:
                weights[k] = float(v)
            except (TypeError, ValueError):
                continue
    s = sum(weights.values())
    if s <= 0:
        return dict(_DEFAULT_TOURNAMENT_WEIGHTS)
    return {k: v / s for k, v in weights.items()}


def _candidate_score(candidate: dict, weights: dict[str, float]) -> float:
    """Weighted sum over the 5 dims. Missing scores → 0 contribution."""
    scores = candidate.get("scores") or {}
    if not isinstance(scores, dict):
        # Pydantic model — try .model_dump()
        try:
            scores = scores.model_dump()  # type: ignore[union-attr]
        except Exception:
            return 0.0
    total = 0.0
    for dim, w in weights.items():
        v = scores.get(dim, 0)
        try:
            total += float(v) * w
        except (TypeError, ValueError):
            continue
    return total


async def competitive_select(
    candidates: list[dict],
    dispatcher: Any = None,
    top_k: int = 2,
    *,
    config: dict | None = None,
) -> list[dict]:
    """Deterministic top-k selection by weighted EvaluationScores.

    Replaces the previous LLM head-to-head tournament. Rationale: the
    upstream Topic Evaluator already produces ``EvaluationScores`` (5
    dimensions, 0-3 each). Running an LLM tournament on top throws those
    numbers away and reintroduces ordering bias (proposal-A-shown-first
    wins more often). A weighted sort gives the same selection
    deterministically, with O(N log N) cost and full reproducibility.

    Sort key (descending → highest first):
      (weighted_score, scores.total, novelty, -alphabetical_proposal_id)

    The ``dispatcher`` parameter is retained for backward compatibility
    but unused — call sites can drop it.
    """
    del dispatcher  # unused; kept for signature compat
    if len(candidates) <= top_k:
        return list(candidates)

    weights = _tournament_weights(config)

    def _key(c: dict) -> tuple:
        weighted = _candidate_score(c, weights)
        scores = c.get("scores") or {}
        if not isinstance(scores, dict):
            try:
                scores = scores.model_dump()  # type: ignore[union-attr]
            except Exception:
                scores = {}
        total = sum(int(v) for v in scores.values() if isinstance(v, (int, float)))
        novelty = int(scores.get("novelty", 0) or 0)
        # Stable tie-break: alphabetical proposal_id (or title).
        ident = str(c.get("proposal_id") or c.get("title") or "")
        return (-weighted, -total, -novelty, ident)

    ranked = sorted(candidates, key=_key)
    logger.info(
        "competitive_select: %d candidates → top %d (weights=%s)",
        len(candidates), top_k, weights,
    )
    return ranked[:top_k]


async def refine_idea(candidate: dict, dispatcher: Any) -> dict:
    """Refine a winning candidate: sharpen hypothesis, tighten scope.

    Returns a new dict with refined fields merged over the original.
    """
    prompt = (
        "You are refining a research proposal that won a competitive selection.\n\n"
        f"Title: {candidate.get('title', '')}\n"
        f"Question: {candidate.get('research_question', '')}\n"
        f"Hypothesis: {candidate.get('hypothesis', '')}\n"
        f"Motivation: {candidate.get('motivation', '')}\n\n"
        "Improve the proposal:\n"
        "1. Sharpen the hypothesis to be more specific and testable\n"
        "2. Tighten the scope to what can realistically be validated\n"
        "3. Strengthen the motivation with concrete evidence gaps\n\n"
        "Output JSON with keys: title, research_question, hypothesis, motivation"
    )
    try:
        result = await dispatcher.dispatch_via_executor(
            "professor", prompt, "_system_refine"
        )
        if result.get("success"):
            text = (result.get("output") or "").strip()
            import json as _json
            if text.startswith("```"):
                lines = text.split("\n")[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines)
            try:
                refined = _json.loads(text)
                merged = dict(candidate)
                for k in ("title", "research_question", "hypothesis", "motivation"):
                    if k in refined and refined[k]:
                        merged[k] = refined[k]
                return merged
            except _json.JSONDecodeError:
                pass
    except Exception:
        logger.exception("Idea refinement failed")
    return candidate


# ── Utilities ────────────────────────────────────────────────────


def _extract_keywords(title: str, question: str, hypothesis: str) -> list[str]:
    """Extract meaningful keywords for search queries."""
    text = f"{title} {question} {hypothesis}"
    # Remove common stop words, keep 4+ char words
    stop_words = {
        "that", "this", "with", "from", "what", "which", "where", "when",
        "have", "been", "will", "would", "could", "should", "does", "into",
        "more", "than", "also", "each", "they", "them", "their", "these",
        "those", "such", "some", "both", "between", "through", "about",
        "using", "based", "approach", "method", "study", "research",
    }
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    keywords = [w for w in words if w not in stop_words]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for w in keywords:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:8]  # Limit to 8 keywords


def _clamp(value: Any, min_val: int, max_val: int) -> int:
    try:
        return max(min_val, min(max_val, int(value)))
    except (TypeError, ValueError):
        return min_val
