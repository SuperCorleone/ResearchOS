# Automated Paper Audit Report
**Pipeline**: mitigating_model_uncertainty_i
**Overall**: FAIL
**num_error_corrections (retry budget)**: 5

## G4: Numerical Consistency
**Verdict**: FAIL
**Numbers found**: 9, **Matched**: 2, **Unmatched**: 7

| Line | Value | Status | Detail |
|------|-------|--------|--------|
| 62 | 42 | MATCH | Matches results.config.split_seed = 42.0 |
| 65 | 42 | MATCH | Matches results.config.split_seed = 42.0 |
| 65 | 123 | NO_SOURCE | No matching value found in results.json |
| 65 | 456. | NO_SOURCE | No matching value found in results.json |
| 74 | 0.02 | NO_SOURCE | No matching value found in results.json |
| 77 | 10% | NO_SOURCE | No matching value found in results.json |
| 118 | 22 | NO_SOURCE | No matching value found in results.json |
| 118 | 10.114 | NO_SOURCE | No matching value found in results.json |
| 118 | 524844.3528056 | NO_SOURCE | No matching value found in results.json |

## G5: Statistical Reporting Completeness
**Verdict**: FAIL (0/5 items present)

| Item | Found | Count |
|------|-------|-------|
| effect_size | MISSING | 0 |
| p_value | MISSING | 0 |
| confidence_interval | MISSING | 0 |
| test_statistic | MISSING | 0 |
| sample_size | MISSING | 0 |

**Missing items**: effect_size, p_value, confidence_interval, test_statistic, sample_size

## Figure Quality Audit (VLM)
No figures found to audit.

## Citation integrity
**Verdict**: PASS
**Total cite keys**: 0, **Resolved**: 0, **Hallucinated**: 0, **Unevidenced**: 0

## LaTeX lint
_No .tex file found; lint skipped._
