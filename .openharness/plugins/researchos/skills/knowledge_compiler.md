---
name: knowledge_compiler
description: "Compile raw content or pipeline artifacts into structured knowledge wiki pages."
---

# Knowledge Compiler

You compile raw inputs and validated pipeline artifacts into structured knowledge wiki pages.

## When to Use

- When new files appear in `/knowledge/raw/`
- When a pipeline milestone is completed and findings should be preserved
- When the scheduler or professor requests knowledge compilation

## Input Sources

1. **Raw files**: `/knowledge/raw/*.md`, `*.txt`, `*.pdf` (text extracted)
2. **Pipeline artifacts**: `/artifacts/pipeline_<id>/analysis_report.md`, `hypothesis.md`, `results.json`
3. **Milestone reports**: `/artifacts/pipeline_<id>/milestone_*.md`

## Output

Write structured wiki pages to the appropriate subdirectory:

- **Concepts** → `/knowledge/wiki/concepts/<topic>.md`
- **Methods** → `/knowledge/wiki/methods/<method>.md`
- **Experiments** → `/knowledge/wiki/experiments/<experiment>.md`
- **Failures** → `/knowledge/wiki/failures/<failure>.md`
- **Open Problems** → `/knowledge/wiki/open_problems/<problem>.md`

## Wiki Page Format

Every wiki page MUST follow this format:

```markdown
---
title: "<Page Title>"
tags: [<tag1>, <tag2>]
source: "<path to source file or pipeline>"
created: "<ISO date>"
---

# <Page Title>

## Summary
<2-3 sentence overview>

## Details
<main content>

## Key Takeaways
- <takeaway 1>
- <takeaway 2>

## Related Pages
- [[<related page path>]]

## Source
- <original source path or pipeline reference>
```

## Rules

1. Do NOT modify existing wiki pages -- only create new ones
2. If a page on the same topic exists, create a new version with a suffix (e.g., `_v2`)
3. Always include `source` in frontmatter for traceability
4. Extract key information: methods, metrics, conclusions, failures
5. Tag pages appropriately for searchability
6. Negative results go to `/knowledge/wiki/failures/` -- they are valuable knowledge

## Workflow

1. Read the source material
2. Identify the category (concept, method, experiment, failure, open problem)
3. Extract structured information
4. Write the wiki page with proper frontmatter
5. Update `/knowledge/index/topic_map.md` if it exists (append new entry)
