# user_approve/ — pipeline mitigating_model_uncertainty_i

FAST_FAIL is complete for an inject-mode pipeline. Review the files below, write your opinion in `feedback.md`, then run the continue command.

## Files
- `papers/` — reference PDFs copied from knowledge/raw/
- `proposal.md` — research question + initial hypothesis
- `fast_fail_report.md` — critic analysis + pre-MAIN review
- `professor_suggestion.md` — Professor's fix recommendation
- `feedback.md` — **edit this** with your modification opinions

## Continue
```
python scripts/run_research.py continue --pipeline mitigating_model_uncertainty_i
```
The fixer agent will follow your directives exactly and, on success, unblock the pipeline into the MAIN stage.
