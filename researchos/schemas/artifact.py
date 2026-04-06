"""Artifact metadata schemas."""

from __future__ import annotations

from pydantic import BaseModel


class ArtifactManifest(BaseModel):
    """Metadata stored alongside an artifact file."""

    pipeline_id: str
    step: str
    filename: str
    created_at: str
    checksum: str | None = None
    schema_version: str = "1.0"


class ResultsArtifact(BaseModel):
    """Schema for ``results.json`` -- enforced by hooks."""

    metrics: dict[str, float]
    config: dict[str, object]
    seed: int
    runtime_seconds: float | None = None
    notes: str = ""
