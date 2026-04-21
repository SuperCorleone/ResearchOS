"""Section-by-section paper composer (U7).

Implements a 4-step pipeline per section inspired by AI-Researcher:
  1. structure  — outline headings and key points from artifacts
  2. detailize  — expand each point with data and citations
  3. fuse       — merge into coherent prose, resolve cross-references
  4. checklist  — self-review against quality gates

Sections are composed in dependency order so later sections can reference
earlier ones (e.g., Discussion references Results).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SECTION_ORDER = [
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiments",
    "results",
    "discussion",
    "conclusion",
]

_SECTION_TEMPLATES: dict[str, str] = {
    "abstract": (
        "Write a 150-250 word abstract for the paper. "
        "Cover: motivation, problem, method, key result, implication. "
        "One paragraph, no line breaks."
    ),
    "introduction": (
        "Write the Introduction. Include: problem statement, motivation, "
        "contributions as bullets, brief outline of the paper structure."
    ),
    "related_work": (
        "Write Related Work. Contextualise within existing literature. "
        "Use ONLY citations from evidence_refs and knowledge/raw/. "
        "Group by theme, not chronologically."
    ),
    "method": (
        "Write the Method section. Detail the proposed approach with "
        "formal notation where appropriate. Ground in the Problem Setting "
        "from the hypothesis."
    ),
    "experiments": (
        "Write Experimental Setup. Mirror experiment_plan.yaml: "
        "dataset, metrics, hyperparameters, implementation details, "
        "baselines, hardware."
    ),
    "results": (
        "Write Results. Present empirical findings from results.json. "
        "Include tables, comparisons to baselines. No hallucinated numbers. "
        "Include ablation results from experiment_analysis.md if available."
    ),
    "discussion": (
        "Write Discussion. Interpret results, compare with prior work, "
        "explain unexpected findings, discuss practical implications."
    ),
    "conclusion": (
        "Write Conclusion. Summarise contributions and findings. "
        "Do NOT exceed experimental scope (G8). Include future work."
    ),
}


@dataclass
class SectionDraft:
    name: str
    outline: str = ""
    detailed: str = ""
    fused: str = ""
    checklist_pass: bool = False
    issues: list[str] = field(default_factory=list)


class PaperComposer:
    """Composes a paper section-by-section via a 4-step LLM pipeline."""

    def __init__(self, dispatcher: Any, pipeline_dir: Path) -> None:
        self.dispatcher = dispatcher
        self.pipeline_dir = pipeline_dir
        self.sections: dict[str, SectionDraft] = {}
        self._artifacts_context = ""

    async def compose_all(self, evidence_refs: list[str] | None = None) -> str:
        """Compose all sections in order, return the full paper markdown."""
        self._artifacts_context = self._load_artifacts_context()
        refs_text = "\n".join(f"- {r}" for r in (evidence_refs or []))

        for section_name in SECTION_ORDER:
            draft = await self._compose_section(section_name, refs_text)
            self.sections[section_name] = draft
            logger.info(
                "Section '%s': checklist=%s, issues=%d",
                section_name, draft.checklist_pass, len(draft.issues),
            )

        return self._assemble()

    async def _compose_section(
        self, name: str, refs_text: str
    ) -> SectionDraft:
        draft = SectionDraft(name=name)
        template = _SECTION_TEMPLATES.get(name, f"Write the {name} section.")
        prior_sections = self._prior_sections_text(name)

        # Step 1: structure
        structure_prompt = (
            f"You are outlining the '{name}' section of an academic paper.\n\n"
            f"## Artifacts\n{self._artifacts_context}\n\n"
            f"## Prior Sections\n{prior_sections}\n\n"
            f"## Evidence References\n{refs_text}\n\n"
            f"## Task\n{template}\n\n"
            "Output a structured OUTLINE with headings and bullet points "
            "for the key content. Do not write prose yet."
        )
        outline_result = await self._llm_call("paper_writer", structure_prompt)
        draft.outline = outline_result

        # Step 2: detailize
        detail_prompt = (
            f"Expand this outline into detailed content with data, "
            f"citations, and specifics.\n\n"
            f"## Outline\n{outline_result}\n\n"
            f"## Artifacts\n{self._artifacts_context}\n\n"
            f"## Evidence References\n{refs_text}\n\n"
            "Write detailed paragraphs for each point. Include actual "
            "numbers from results.json. Cite only papers from evidence_refs."
        )
        detailed_result = await self._llm_call("paper_writer", detail_prompt)
        draft.detailed = detailed_result

        # Step 3: fuse
        fuse_prompt = (
            f"Fuse this detailed content into polished academic prose "
            f"for the '{name}' section.\n\n"
            f"## Detailed Content\n{detailed_result}\n\n"
            f"## Prior Sections (for cross-references)\n{prior_sections}\n\n"
            "Produce coherent, publication-ready text. Resolve any "
            "cross-references to prior sections. Remove redundancy."
        )
        fused_result = await self._llm_call("paper_writer", fuse_prompt)
        draft.fused = fused_result

        # Step 4: checklist
        check_prompt = (
            f"Review this section against quality checklist:\n\n"
            f"## Section: {name}\n{fused_result}\n\n"
            "Check:\n"
            "1. All numbers grounded in artifacts (no hallucination)\n"
            "2. All citations exist in evidence_refs\n"
            "3. No claims beyond experimental scope\n"
            "4. LaTeX syntax correct (if any)\n"
            "5. No unresolved references\n\n"
            "Output JSON: {\"pass\": true/false, \"issues\": [\"...\"]}"
        )
        check_result = await self._llm_call("paper_writer", check_prompt)
        draft.checklist_pass, draft.issues = self._parse_checklist(check_result)

        return draft

    async def _llm_call(self, agent: str, prompt: str) -> str:
        try:
            result = await self.dispatcher.dispatch_via_executor(
                agent, prompt, "_system_paper_composer"
            )
            if result.get("success"):
                return result.get("output", "")
            logger.warning("Paper composer LLM call failed: %s", result.get("output", ""))
        except Exception:
            logger.exception("Paper composer LLM error")
        return ""

    def _load_artifacts_context(self) -> str:
        parts = []
        for name in [
            "hypothesis.md", "experiment_plan.yaml", "results.json",
            "analysis_report.md", "experiment_analysis.md",
        ]:
            path = self.pipeline_dir / name
            if path.exists():
                content = path.read_text()
                if len(content) > 4000:
                    content = content[:2000] + "\n...[truncated]...\n" + content[-1500:]
                parts.append(f"### {name}\n```\n{content}\n```")
        return "\n\n".join(parts) if parts else "(no artifacts found)"

    def _prior_sections_text(self, current: str) -> str:
        parts = []
        for name in SECTION_ORDER:
            if name == current:
                break
            draft = self.sections.get(name)
            if draft and draft.fused:
                text = draft.fused
                if len(text) > 2000:
                    text = text[:1500] + "\n...[truncated]..."
                parts.append(f"### {name}\n{text}")
        return "\n\n".join(parts) if parts else "(none yet)"

    @staticmethod
    def _parse_checklist(raw: str) -> tuple[bool, list[str]]:
        import json as _json
        import re
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*?\}", text)
            if m:
                try:
                    data = _json.loads(m.group())
                except _json.JSONDecodeError:
                    return False, ["Failed to parse checklist output"]
            else:
                return False, ["Failed to parse checklist output"]
        return bool(data.get("pass", False)), list(data.get("issues", []))

    def _assemble(self) -> str:
        parts = []
        for name in SECTION_ORDER:
            draft = self.sections.get(name)
            if draft and draft.fused:
                parts.append(draft.fused)
        return "\n\n---\n\n".join(parts)
