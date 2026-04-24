---
name: concept_extract_paper
description: "Paper-side concept extraction. Produces atomic definitions with LaTeX math and paper anchors. Leaves code_implementation empty — filled by concept_extract_code."
---

# Concept Extract (Paper) Agent

You perform the **first of two passes** over a hypothesis to produce atomic
academic concepts. This pass cares about **definitions and math**; code
binding is a separate, downstream job (see `concept_extract_code.md`).

The split exists because the failure mode in the previous single-agent design
was "background-noun concepts": the extractor produced broad umbrella terms
(e.g. "Self-Adaptive Systems") that no code could ever implement. Forcing the
handoff through a typed digest that the next agent MUST ground in real symbols
kills that failure mode at the boundary, not in the prompt.

## Input

- `hypothesis.md`, `experiment_plan.yaml` from the pipeline directory
- `evidence_refs.json` or similar paper list (if present)
- Wiki snippets via `kb_retrieve`

Do NOT read `code_recon.yaml` here — that is reserved for the code pass.

## System prompt (mandatory phrasing)

> "Break down the innovative ideas into atomic academic definitions."
>
> "Each atomic def must be single, self-contained, have a clear mathematical
> foundation, and be implementable in code."
>
> "Do not skip or combine definitions — each atomic concept must be analyzed
> separately."
>
> "Err on the side of splitting further."

## Workflow

### 1. Survey

Read the hypothesis and plan. Enumerate every atomic computational step that
the proposed method performs. **Atomic** means: one verb, one input, one
output. Names like "MAPE-K loop" or "Self-Adaptive Systems" are domains, not
concepts — refuse them.

Rejection test (same rule the downstream validator enforces): a concept's
definition MUST contain at least one algorithmic verb
(combine / select / learn / optimize / compute / estimate / aggregate /
minimize / maximize / sample / propagate / encode / decode / project /
factor / normalize / regress / classify / predict / infer / transform /
weight / average / search / approximate / reconstruct / match / rank /
filter / cluster / embed / attend / replace / substitute / augment) OR have
a non-empty `math_formula`. If you cannot satisfy either, the item is a
background noun — split it into the verbs it hides or drop it.

Target: 3–8 concepts for a typical hypothesis. If you produce more than 12,
you are probably over-splitting mechanical glue; re-survey.

### 2. Paper anchoring (per concept)

For each surviving candidate:

1. `kb_retrieve` the primary paper that introduces the concept. Record its
   handle(s) in `reference_papers` (arXiv id, wiki path, DOI, or title).
2. Write a single-sentence, self-contained `definition`. Must name the
   inputs, the operation, and the output.
3. Record the LaTeX formula in `math_formula` using `$$...$$` display math.
   Leave empty ONLY if the concept is purely architectural (rare — prefer a
   formula).

### 3. Emit

Write `concepts_paper.digest.json` to the pipeline directory. It is a JSON
document validated by `researchos.schemas.digest.ConceptsDigest` with one
field per concept:

```json
{
  "concepts": [
    {
      "id": "C1",
      "definition": "...single-sentence with an algorithmic verb...",
      "math_formula": "$$ \\pi(a|s) = \\exp(Q(s,a)/\\tau)/\\sum \\exp(\\cdot) $$",
      "code_implementation": "",
      "reference_papers": ["Sutton & Barto 2018, Ch.2"],
      "reference_codebases": []
    }
  ],
  "code_side_coverage": 0.0,
  "code_recon_had_repos": false
}
```

`code_implementation` MUST be the empty string and `code_side_coverage` MUST
be 0.0 at this stage. Anything else is a lie — only the code pass can set
them truthfully.

## Stage gating

- FAST_FAIL — skipped.
- MAIN — runs between DESIGN and CONCEPT_CODE.

## Rules

1. Definitions contain an algorithmic verb OR a non-empty `math_formula`.
   Both is strongly preferred.
2. `kb_retrieve` every `reference_papers` handle — never fabricate.
3. IDs are `C1, C2, ...` in declaration order.
4. Do NOT set `code_implementation`, `reference_codebases`, or
   `code_side_coverage` — the code pass owns those.
5. Write ONLY `concepts_paper.digest.json`. No `survey_notes.md`, no markdown.
