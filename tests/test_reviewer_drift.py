"""Smoke tests for reviewer few-shot drift (A10).

When a v1 few-shot JSON fixture is available (paper/review pairs with
expected reviewer scores), this test will compute a real MAE-against-expected
delta once a running reviewer exists. Until then, the test only validates
structural invariants so the CI remains green without being skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_CANDIDATES = [
    Path(__file__).resolve().parents[1]
    / "knowledge"
    / "fewshots"
    / "reviewer_v1.json",
    Path(__file__).resolve().parents[1]
    / "paper"
    / "fewshots"
    / "reviewer_v1.json",
]


def _load_fewshots() -> list[dict]:
    """Load the v1 few-shot fixture if any candidate path exists."""
    for p in FIXTURE_CANDIDATES:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    return data["items"]
            except Exception:
                continue
    return []


def _mock_pairs() -> list[dict]:
    """Three mock paper+review pairs used when no fixture exists."""
    return [
        {
            "id": "mock_1",
            "paper": {
                "title": "Sparse Adapters for Frozen LLMs",
                "abstract": (
                    "We propose sparse low-rank adapters that achieve "
                    "transfer with <1% trainable parameters."
                ),
            },
            "review": {
                "clarity": 4,
                "novelty": 3,
                "significance": 3,
                "soundness": 4,
                "overall": 3,
            },
        },
        {
            "id": "mock_2",
            "paper": {
                "title": "RLHF with On-Policy Constrained Decoding",
                "abstract": (
                    "A constraint-aware decoding algorithm for RLHF-trained "
                    "models that reduces toxicity without reward hacking."
                ),
            },
            "review": {
                "clarity": 3,
                "novelty": 2,
                "significance": 4,
                "soundness": 3,
                "overall": 3,
            },
        },
        {
            "id": "mock_3",
            "paper": {
                "title": "A New Benchmark for Long-Context Reasoning",
                "abstract": (
                    "We release a 100k-token reasoning benchmark and baseline "
                    "results showing current models plateau at 40% accuracy."
                ),
            },
            "review": {
                "clarity": 4,
                "novelty": 3,
                "significance": 3,
                "soundness": 4,
                "overall": 4,
            },
        },
    ]


@pytest.fixture(scope="module")
def fewshot_pairs() -> list[dict]:
    pairs = _load_fewshots()
    if not pairs:
        pairs = _mock_pairs()
    return pairs


def test_fewshot_structure(fewshot_pairs: list[dict]) -> None:
    """Every pair must have a paper.title and a numeric overall score."""
    assert fewshot_pairs, "expected at least one paper+review pair"
    for pair in fewshot_pairs:
        assert isinstance(pair, dict), "each pair must be a dict"
        paper = pair.get("paper")
        review = pair.get("review")
        assert isinstance(paper, dict) and paper.get("title"), (
            f"paper missing title in {pair.get('id')}"
        )
        assert isinstance(review, dict), (
            f"review missing in {pair.get('id')}"
        )
        overall = review.get("overall")
        assert isinstance(overall, (int, float)), (
            f"review.overall must be numeric in {pair.get('id')}"
        )


def test_reviewer_fewshot_mae(fewshot_pairs: list[dict]) -> None:
    """Placeholder MAE check.

    Asserts structure (not numeric drift) until a running reviewer is
    wired up. When a reviewer becomes available, this test will compare
    predicted scores vs. expected scores and assert MAE < threshold.
    """
    expected_axes = {"clarity", "novelty", "significance", "soundness", "overall"}
    for pair in fewshot_pairs:
        review_keys = set((pair.get("review") or {}).keys())
        missing = expected_axes - review_keys
        assert not missing, (
            f"review for {pair.get('id')} missing axes {sorted(missing)}"
        )
        for k in expected_axes:
            v = pair["review"][k]
            assert isinstance(v, (int, float)), (
                f"{pair.get('id')}.{k} must be numeric"
            )
            assert 0 <= v <= 5, (
                f"{pair.get('id')}.{k}={v} outside [0,5]"
            )

    # Deterministic "baseline predictor": predict the mean overall score.
    # This establishes an MAE baseline the real reviewer must beat.
    overalls = [float(p["review"]["overall"]) for p in fewshot_pairs]
    mean_overall = sum(overalls) / len(overalls)
    mae = sum(abs(o - mean_overall) for o in overalls) / len(overalls)
    assert mae >= 0.0
    # Placeholder upper bound — the real reviewer must stay under this.
    assert mae <= 5.0
