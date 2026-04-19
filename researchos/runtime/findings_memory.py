"""Findings memory service (A7).

Persists and retrieves small structured "cards" representing research
artifacts. Seven ``kinds`` are supported, matching the seven-slot memory
model: papers, ideas, decisions, episodes, knowledge, templates, findings.

Each card is stored as a Markdown file with YAML frontmatter under
``{root}/{kind}s/{id}.md`` and is additionally appended to an index file
``{root}/index.jsonl`` for quick listing.

Default ``root`` = ``project_root / "knowledge" / "findings"``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MEMORY_KINDS: tuple[str, ...] = (
    "papers",
    "ideas",
    "decisions",
    "episodes",
    "knowledge",
    "templates",
    "findings",
)

MemoryKind = Literal[
    "papers",
    "ideas",
    "decisions",
    "episodes",
    "knowledge",
    "templates",
    "findings",
]


class FindingCard(BaseModel):
    """A single memory card."""

    id: str
    kind: MemoryKind
    title: str
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    source_pipeline: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _kind_dir_name(kind: str) -> str:
    """Return the directory name for a given kind.

    The kinds are already plural (papers, ideas, ...). We preserve them
    verbatim as directory names to avoid double-pluralisation.
    """
    return kind


class MemoryService:
    """On-disk memory service backing :class:`FindingCard` objects."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            root = Path.cwd() / "knowledge" / "findings"
        self.root = Path(root)
        self.index_path = self.root / "index.jsonl"

    # -- I/O -----------------------------------------------------------

    def save(self, card: FindingCard) -> Path:
        """Persist *card* to disk, appending a row to the jsonl index."""
        if card.kind not in MEMORY_KINDS:
            raise ValueError(
                f"Unknown kind {card.kind!r}; expected one of {MEMORY_KINDS}"
            )
        kind_dir = self.root / _kind_dir_name(card.kind)
        kind_dir.mkdir(parents=True, exist_ok=True)
        out_path = kind_dir / f"{card.id}.md"

        frontmatter = {
            "id": card.id,
            "kind": card.kind,
            "title": card.title,
            "tags": list(card.tags),
            "source_pipeline": card.source_pipeline,
            "created_at": card.created_at,
        }
        content = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + (card.body or "")
        )
        out_path.write_text(content, encoding="utf-8")

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.index_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "id": card.id,
                            "kind": card.kind,
                            "title": card.title,
                            "tags": list(card.tags),
                            "source_pipeline": card.source_pipeline,
                            "created_at": card.created_at,
                            "path": str(out_path.relative_to(self.root)),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as exc:
            logger.warning("Failed to append findings index: %s", exc)
        return out_path

    def load(self, kind: str, id: str) -> FindingCard | None:
        """Read a card back from disk, or ``None`` if missing."""
        if kind not in MEMORY_KINDS:
            return None
        path = self.root / _kind_dir_name(kind) / f"{id}.md"
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            fm, body = _split_frontmatter(text)
            return FindingCard(
                id=fm.get("id", id),
                kind=fm.get("kind", kind),
                title=fm.get("title", ""),
                body=body,
                tags=list(fm.get("tags") or []),
                source_pipeline=fm.get("source_pipeline"),
                created_at=fm.get(
                    "created_at",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception as exc:
            logger.warning("Failed to load card %s/%s: %s", kind, id, exc)
            return None

    # -- Search --------------------------------------------------------

    def search(
        self,
        query: str,
        kind: str | None = None,
        top_k: int = 15,
        stage: str | None = None,
    ) -> list[FindingCard]:
        """Substring search with optional stage-biased ranking.

        If ``kind`` is provided, restricts to that slot. If ``stage`` is
        provided, applies :meth:`_stage_bias` as a ranking multiplier.
        """
        q = (query or "").lower().strip()
        kinds = (kind,) if kind else MEMORY_KINDS
        if kind is not None and kind not in MEMORY_KINDS:
            return []

        scored: list[tuple[float, FindingCard]] = []
        for k in kinds:
            kdir = self.root / _kind_dir_name(k)
            if not kdir.exists():
                continue
            for path in sorted(kdir.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                fm, body = _split_frontmatter(text)
                haystack = (
                    (fm.get("title", "") or "")
                    + "\n"
                    + (body or "")
                    + "\n"
                    + " ".join(fm.get("tags") or [])
                ).lower()
                if q and q not in haystack:
                    continue
                # base score = number of substring hits (at least 1 when q is non-empty)
                base = 1.0 if not q else max(1, haystack.count(q))
                bias = self._stage_bias(k, stage) if stage else 1.0
                score = base * bias
                card = FindingCard(
                    id=fm.get("id", path.stem),
                    kind=fm.get("kind", k),
                    title=fm.get("title", ""),
                    body=body,
                    tags=list(fm.get("tags") or []),
                    source_pipeline=fm.get("source_pipeline"),
                    created_at=fm.get(
                        "created_at",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                scored.append((score, card))

        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]

    # -- Ranking helper ------------------------------------------------

    def _stage_bias(self, kind: str, stage: str) -> float:
        """Stage-aware ranking bias.

        - scout    → papers/knowledge favoured
        - experiment → ideas/episodes favoured
        - analysis → findings favoured
        Other kinds get 1.0 (neutral).
        """
        stage_norm = (stage or "").lower()
        table = {
            "scout": {"papers": 1.5, "knowledge": 1.4},
            "experiment": {"ideas": 1.5, "episodes": 1.4},
            "analysis": {"findings": 1.6},
        }
        return table.get(stage_norm, {}).get(kind, 1.0)


# -- helpers -------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file with YAML frontmatter into (fm_dict, body)."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm_str = text[4:end]
            body = text[end + len("\n---") :]
            if body.startswith("\n"):
                body = body[1:]
            if body.startswith("\n"):
                body = body[1:]
            try:
                fm = yaml.safe_load(fm_str) or {}
                if not isinstance(fm, dict):
                    fm = {}
            except Exception:
                fm = {}
            return fm, body
    return {}, text
