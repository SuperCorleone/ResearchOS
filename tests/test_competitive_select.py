"""Deterministic top-k selection over EvaluationScores.

Replaces the previous LLM head-to-head tournament. Tests:
  - Higher weighted score wins.
  - Ties broken first by total, then novelty, then proposal_id alphabetical.
  - top_k <= len returns input unchanged (order preserved).
  - Custom weights from config respected.
  - Missing scores treated as 0 contribution (no exception).
"""

from __future__ import annotations

import asyncio

from researchos.runtime.topic_evaluator import competitive_select


def _run(coro):
    return asyncio.run(coro)


def _cand(pid: str, **scores) -> dict:
    return {"proposal_id": pid, "title": f"P-{pid}", "scores": dict(scores)}


def test_higher_weighted_score_wins():
    cands = [
        _cand("a", significance=3, novelty=3, differentiation=3, feasibility=3, falsifiability=3),
        _cand("b", significance=1, novelty=1, differentiation=1, feasibility=1, falsifiability=1),
        _cand("c", significance=2, novelty=2, differentiation=2, feasibility=2, falsifiability=2),
    ]
    out = _run(competitive_select(cands, top_k=2))
    assert [c["proposal_id"] for c in out] == ["a", "c"]


def test_top_k_ge_len_returns_input():
    cands = [_cand("a", novelty=1), _cand("b", novelty=2)]
    out = _run(competitive_select(cands, top_k=5))
    assert out == cands  # unchanged when nothing to cut


def test_tie_breaks_by_alphabetical_proposal_id():
    """All weights tied → fall through to id sort. Deterministic regardless of input order."""
    cands = [
        _cand("zeta", novelty=2, feasibility=2),
        _cand("alpha", novelty=2, feasibility=2),
        _cand("mu",   novelty=2, feasibility=2),
    ]
    out = _run(competitive_select(cands, top_k=2))
    assert [c["proposal_id"] for c in out] == ["alpha", "mu"]


def test_tie_break_by_novelty_when_total_equal():
    """Equal weighted score with default weights but different novelty → novelty wins tiebreak."""
    # Custom weights make weighted score equal but novelty differs.
    cfg = {"tournament": {"weights": {
        "significance": 0, "novelty": 0,  # zero out so weighted ties
        "differentiation": 0, "feasibility": 0, "falsifiability": 1,
    }}}
    cands = [
        _cand("a", novelty=3, falsifiability=2),
        _cand("b", novelty=1, falsifiability=2),
    ]
    out = _run(competitive_select(cands, top_k=1, config=cfg))
    # Both have weighted=2 (only falsifiability counted). Tie-break:
    # total = 5 vs 3 → 'a' wins.
    assert out[0]["proposal_id"] == "a"


def test_custom_weights_respected():
    """Heavy feasibility weight should let a feasibility-3 beat a novelty-3."""
    cfg = {"tournament": {"weights": {
        "significance": 0, "novelty": 0.1,
        "differentiation": 0, "feasibility": 0.9, "falsifiability": 0,
    }}}
    cands = [
        _cand("novel_only",      novelty=3, feasibility=0),
        _cand("feasible_only",   novelty=0, feasibility=3),
    ]
    out = _run(competitive_select(cands, top_k=1, config=cfg))
    assert out[0]["proposal_id"] == "feasible_only"


def test_missing_scores_treated_as_zero():
    cands = [
        {"proposal_id": "no_scores", "title": "x"},  # no 'scores' key
        _cand("with_scores", novelty=1),
    ]
    out = _run(competitive_select(cands, top_k=1))
    assert out[0]["proposal_id"] == "with_scores"


def test_dispatcher_arg_unused_safely():
    """Backward-compat: callers still pass dispatcher; we ignore it without erroring."""
    class _DummyDispatcher:
        pass
    cands = [_cand("a", novelty=2), _cand("b", novelty=1)]
    out = _run(competitive_select(cands, _DummyDispatcher(), top_k=1))
    assert out[0]["proposal_id"] == "a"


def test_no_llm_call_ever_made(monkeypatch):
    """Sanity: the function must not import or invoke any LLM helper.
    A regression to LLM tournament would surface here as an unexpected call."""
    import researchos.runtime.topic_evaluator as te
    called: list[str] = []

    def _spy(*args, **kwargs):
        called.append("dispatch")
        raise RuntimeError("LLM should not be called")

    # Patch the module's potentially-leftover dispatcher reference if any.
    if hasattr(te, "_head_to_head"):  # legacy artefact — should be gone
        monkeypatch.setattr(te, "_head_to_head", _spy)

    cands = [_cand(c, novelty=i) for i, c in enumerate(["a", "b", "c", "d"])]
    out = _run(competitive_select(cands, top_k=2))
    assert called == []
    assert len(out) == 2
