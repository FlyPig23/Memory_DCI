# Static grader error-contract audit

Before formal execution, `scripts/audit_grader_error_contracts.py` checked the
sealed source hashes and parsed only the `Automated Checks` sections of all
60 official tasks. It executed no task code, read no gold files, emitted no
task prompts or answer constants, and changed no grading rubric.

`manifests/grader_error_contracts.json` contains 81 selected error/mode contracts,
including tasks with no such fields. Rows identify the task, error field, direct
error string or redacted dynamic template, AST location, enclosing function and
exception-handler shape. Task source, checks, extractor and runtime-classifier
hashes record provenance. The extra `parse_error` field is included because it
appears in transcript-loader error records; such records are not automatically
top-level scores.

`task_runtime.classify_grader_issue` applies the following distinctions while
preserving unchanged raw grader output:

| Contract | Evaluation treatment |
| --- | --- |
| Reviewed missing/invalid solver artifact plus official numeric zero | Valid zero |
| `judge_error`, `llm_error`, `llm_judge_error`, or top-level transcript `parse_error` | `judge_error` |
| Missing `gt_dir`, missing GT file, missing `ground_truth.json`, or no expected GT TeX files | `judge_error` |
| `judge_method=failed/error/unavailable` | `judge_error` |
| `rule_fallback`, `fallback_results_md`, `fallback+llm_judge` | `judge_degraded` |
| Exact `No audit data and results.md ...` error variants | `judge_degraded` |
| Unknown error/fallback or contradictory nonzero artifact-failure score | `needs_review` |

Reviewed artifact messages include the original five results/screenshot errors,
missing output directory, missing summary file, and invalid JSON at a numbered
line. Some official returns merge their error dictionary with zero-valued
numeric metrics instead of setting `overall_score`; these still retain the
officially computed zero. The ambiguous `workspace not found` message is valid
only for an output child of `/tmp_workspace`, as confirmed by a static binding
comparison that did not expose the task's actual output-path constant.

The Social audit getter returns the complete JSON dictionary, and returns `{}`
when its request fails. Its fallback tests the dictionary, not `calls`: a valid
`{"calls": []}` is truthy and remains an available audit. Empty call history and
the known `not_run_no_assistant_text` / `not_run_summary_gate` conditions are not
classified as provider failures.

No generic rule turns every reported zero into a valid task failure. Unknown
messages remain reviewable, and independent inference-gateway failure tracking
is still required because some official graders silently swallow API errors.

The extractor and classifier tests use synthetic snippets and error responses;
they never run a benchmark grader. Existing development-only transcript checks
remain separate from this all-task static error-schema audit.
