"""Unit tests for researchos.runtime.alignment_validator helpers.

Schema-aware checks are not yet wired (run_validator is stubbed); these
tests cover the pure-Python primitives only.
"""

from __future__ import annotations

import pytest

from researchos.runtime.alignment_validator import (
    DerefError,
    deref,
    is_not_stated,
    match_quote,
    normalize_quote,
    quote_token_count,
)


class TestNormalizeQuote:
    def test_collapses_whitespace(self):
        assert normalize_quote("hello   world") == "hello world"
        assert normalize_quote("a\nb\tc") == "a b c"

    def test_strips_outer_whitespace(self):
        assert normalize_quote("  x  ") == "x"

    def test_rejoins_hyphenated_linebreak(self):
        assert normalize_quote("infor-\nmation") == "information"

    def test_does_not_rejoin_uppercase_after_break(self):
        # Only lowercase follow-ups are joined; UpperCase implies new word
        assert normalize_quote("part-\nWhole") == "part- Whole"

    def test_expands_ligatures(self):
        assert normalize_quote("efﬁcient") == "efficient"
        assert normalize_quote("ﬂow") == "flow"

    def test_handles_non_string(self):
        assert normalize_quote(None) == ""  # type: ignore[arg-type]
        assert normalize_quote(123) == ""   # type: ignore[arg-type]


class TestMatchQuote:
    def test_basic_substring(self):
        assert match_quote("the dataset", "We use the dataset of foo")

    def test_match_with_normalisation(self):
        # The paper has hyphenation + ligature artefacts; quote is clean
        paper = "The classiﬁcation accuracy was measured."
        assert match_quote("classification accuracy", paper)

    def test_case_fold_fallback(self):
        assert match_quote("Stacking", "we used stacking ensemble")

    def test_no_match(self):
        assert not match_quote("transformer", "we used random forests")

    def test_empty_quote_returns_false(self):
        assert not match_quote("", "anything")


class TestQuoteTokenCount:
    def test_counts_after_normalisation(self):
        assert quote_token_count("hello   world") == 2
        assert quote_token_count("infor-\nmation theory is fun") == 4


class TestDeref:
    @pytest.fixture
    def specs(self):
        return {
            "paper_spec": {
                "dataset": {"name": "rescueRobot_450", "size": 450},
                "baseline_method": {
                    "method_steps": [
                        {"id": "S1", "name": "monitor"},
                        {"id": "S2", "name": "analyze"},
                    ]
                },
            },
            "proposal_spec": {
                "scope": {
                    "out_of_scope_rqs": [
                        {"id": "RQ3", "reason": "out of scope"},
                    ]
                }
            },
        }

    def test_leaf_scalar(self, specs):
        assert deref("paper_spec.dataset.name", specs, expect="leaf") == "rescueRobot_450"
        assert deref("paper_spec.dataset.size", specs, expect="leaf") == 450

    def test_subtree(self, specs):
        result = deref("paper_spec.dataset", specs, expect="subtree")
        assert isinstance(result, dict)
        assert result["name"] == "rescueRobot_450"

    def test_array_index_by_id(self, specs):
        result = deref(
            "paper_spec.baseline_method.method_steps[id=S2]",
            specs,
            expect="subtree",
        )
        assert result["name"] == "analyze"

    def test_array_index_in_proposal(self, specs):
        result = deref(
            "proposal_spec.scope.out_of_scope_rqs[id=RQ3]",
            specs,
            expect="subtree",
        )
        assert result["reason"] == "out of scope"

    def test_leaf_expected_but_subtree_returned(self, specs):
        with pytest.raises(DerefError, match="expected leaf scalar"):
            deref("paper_spec.dataset", specs, expect="leaf")

    def test_subtree_expected_but_leaf_returned(self, specs):
        with pytest.raises(DerefError, match="expected dict or list"):
            deref("paper_spec.dataset.name", specs, expect="subtree")

    def test_unknown_root(self, specs):
        with pytest.raises(DerefError, match="unknown spec root"):
            deref("foo.bar", specs)

    def test_missing_segment(self, specs):
        with pytest.raises(DerefError, match="missing key 'nope'"):
            deref("paper_spec.dataset.nope", specs)

    def test_array_id_not_found(self, specs):
        with pytest.raises(DerefError, match="no array element"):
            deref(
                "paper_spec.baseline_method.method_steps[id=S99]",
                specs,
                expect="subtree",
            )

    def test_non_dotted_path_rejected(self, specs):
        with pytest.raises(DerefError, match="must be dotted"):
            deref("paper_spec", specs)


class TestIsNotStated:
    def test_none(self):
        assert is_not_stated(None)

    def test_canonical_string(self):
        assert is_not_stated("NOT_STATED")
        assert is_not_stated("not_stated")
        assert is_not_stated("  NOT_STATED  ")
        assert is_not_stated("NOT STATED")

    def test_real_value(self):
        assert not is_not_stated("rescueRobot_450")
        assert not is_not_stated(0)
        assert not is_not_stated(False)
        assert not is_not_stated([])  # empty list ≠ NOT_STATED
