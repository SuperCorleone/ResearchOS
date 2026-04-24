"""Tests for TypedEvidenceRef and PipelineDefinition.evidence_refs typed refs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchos.schemas.pipeline import PipelineDefinition, TypedEvidenceRef


class TestTypedEvidenceRef:
    def test_valid_roles(self):
        for role in ("base_paper", "proposal", "ref_code", "related_work", "unknown"):
            ref = TypedEvidenceRef(role=role, path_or_url="some/path")
            assert ref.role == role
            assert ref.path_or_url == "some/path"
            assert ref.sha256 is None

    def test_invalid_role_rejected(self):
        with pytest.raises(ValidationError):
            TypedEvidenceRef(role="bad_role", path_or_url="some/path")

    def test_sha256_optional(self):
        ref = TypedEvidenceRef(role="base_paper", path_or_url="paper.pdf", sha256="abc123")
        assert ref.sha256 == "abc123"

    def test_sha256_defaults_none(self):
        ref = TypedEvidenceRef(role="proposal", path_or_url="proposal.md")
        assert ref.sha256 is None


class TestPipelineDefinitionEvidenceRefs:
    def _make_pipeline(self, evidence_refs):
        return PipelineDefinition(
            pipeline_id="test_pipeline",
            title="Test",
            research_question="Does it work?",
            evidence_refs=evidence_refs,
        )

    def test_bare_string_list_normalises_to_unknown(self):
        pd = self._make_pipeline(["paper1.pdf", "paper2.pdf"])
        assert len(pd.evidence_refs) == 2
        for ref in pd.evidence_refs:
            assert isinstance(ref, TypedEvidenceRef)
            assert ref.role == "unknown"
        assert pd.evidence_refs[0].path_or_url == "paper1.pdf"
        assert pd.evidence_refs[1].path_or_url == "paper2.pdf"

    def test_typed_dict_list_passes_through(self):
        pd = self._make_pipeline([
            {"role": "base_paper", "path_or_url": "paper.pdf"},
            {"role": "proposal", "path_or_url": "proposal.md"},
            {"role": "ref_code", "path_or_url": "https://github.com/example/repo"},
        ])
        assert len(pd.evidence_refs) == 3
        assert pd.evidence_refs[0].role == "base_paper"
        assert pd.evidence_refs[1].role == "proposal"
        assert pd.evidence_refs[2].role == "ref_code"

    def test_mixed_list_normalises_strings(self):
        pd = self._make_pipeline([
            "legacy_paper.pdf",
            {"role": "proposal", "path_or_url": "proposal.md"},
        ])
        assert len(pd.evidence_refs) == 2
        assert pd.evidence_refs[0].role == "unknown"
        assert pd.evidence_refs[0].path_or_url == "legacy_paper.pdf"
        assert pd.evidence_refs[1].role == "proposal"

    def test_typed_evidence_ref_objects_accepted(self):
        refs = [
            TypedEvidenceRef(role="base_paper", path_or_url="paper.pdf"),
            TypedEvidenceRef(role="ref_code", path_or_url="https://github.com/x/y"),
        ]
        pd = self._make_pipeline(refs)
        assert len(pd.evidence_refs) == 2
        assert pd.evidence_refs[0].role == "base_paper"
        assert pd.evidence_refs[1].role == "ref_code"

    def test_empty_list(self):
        pd = self._make_pipeline([])
        assert pd.evidence_refs == []

    def test_default_empty(self):
        pd = PipelineDefinition(
            pipeline_id="p",
            title="T",
            research_question="Q?",
        )
        assert pd.evidence_refs == []

    def test_round_trip_model_dump_model_validate(self):
        pd = self._make_pipeline([
            {"role": "base_paper", "path_or_url": "paper.pdf", "sha256": "deadbeef"},
            "legacy.pdf",
        ])
        dumped = pd.model_dump()
        pd2 = PipelineDefinition.model_validate(dumped)
        assert len(pd2.evidence_refs) == 2
        assert pd2.evidence_refs[0].role == "base_paper"
        assert pd2.evidence_refs[0].sha256 == "deadbeef"
        assert pd2.evidence_refs[1].role == "unknown"
        assert pd2.evidence_refs[1].path_or_url == "legacy.pdf"

    def test_round_trip_json(self):
        pd = self._make_pipeline([
            {"role": "proposal", "path_or_url": "proposal.md"},
        ])
        json_str = pd.model_dump_json()
        pd2 = PipelineDefinition.model_validate_json(json_str)
        assert pd2.evidence_refs[0].role == "proposal"

    def test_invalid_role_in_dict_rejected(self):
        with pytest.raises(ValidationError):
            self._make_pipeline([{"role": "not_a_role", "path_or_url": "x"}])
