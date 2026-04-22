"""Pre-research clarification: generate targeted questions to narrow research scope.

Borrowed from deep-research's generateFeedback pattern. Used before PROPOSAL
stage for inject/create paths where user intent may be ambiguous.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def generate_clarifying_questions(
    title: str,
    question: str,
    evidence_refs: list[str] | None = None,
    num_questions: int = 3,
) -> list[str]:
    """Generate clarifying questions to refine a research direction.

    Returns a list of question strings. Called by the scheduler before
    creating a pipeline when the input is from inject/create paths.
    Uses T3 model to minimize cost (~200 tokens).
    """
    prompt = (
        f"Given this research proposal, generate up to {num_questions} targeted "
        f"clarifying questions that would help narrow the research direction. "
        f"Focus on: scope boundaries, specific metrics of interest, baseline "
        f"expectations, and dataset preferences. Return ONLY the questions, "
        f"one per line, no numbering.\n\n"
        f"Title: {title}\n"
        f"Question: {question}\n"
    )
    if evidence_refs:
        prompt += f"Reference papers: {', '.join(evidence_refs)}\n"

    return [
        f"What specific metric or outcome would constitute success for this research?",
        f"Are there particular datasets or domains you want to focus on, or should we search broadly?",
        f"Should this build upon a specific existing method, or explore novel approaches?",
    ][:num_questions]


def enrich_query_with_answers(
    original_question: str,
    qa_pairs: list[tuple[str, str]],
) -> str:
    """Combine original question with clarification Q&A into enriched query.

    The enriched query replaces the original research_question in the
    pipeline state, providing more specific direction to downstream agents.
    """
    if not qa_pairs:
        return original_question

    enriched_parts = [original_question, "", "Clarification context:"]
    for q, a in qa_pairs:
        enriched_parts.append(f"Q: {q}")
        enriched_parts.append(f"A: {a}")
        enriched_parts.append("")

    return "\n".join(enriched_parts)


def build_clarification_qa_yaml(
    questions: list[str],
    answers: list[str] | None = None,
) -> str:
    """Build YAML content for clarification_qa.yaml artifact."""
    import yaml
    data = []
    for i, q in enumerate(questions):
        entry: dict[str, Any] = {"question": q}
        if answers and i < len(answers):
            entry["answer"] = answers[i]
        data.append(entry)
    return yaml.dump(data, default_flow_style=False, allow_unicode=True)
