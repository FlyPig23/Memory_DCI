"""Synthetic error-contract checks; never execute any benchmark grader."""
import json
from pathlib import Path

import pytest

from experiment.scripts.audit_grader_error_contracts import code_section, extract_contracts
from experiment.src.task_runtime import classify_grader_issue, official_score


@pytest.mark.parametrize("error", [
    "results directory not found", "screenshot.png not found",
    "screenshot.png too small, likely invalid", "results.md not found", "results.md is empty",
    "output dir not found: /tmp_workspace/synthetic-output",
    "missing summary file: /tmp_workspace/synthetic-output/summary.jsonl",
    "workspace not found: /tmp_workspace/synthetic-output",
    "invalid json on line 3: Expecting property name",
])
def test_reviewed_solver_artifact_failure_preserves_zero(error):
    scores = {"overall_score": 0.0, "error": error}
    assert classify_grader_issue(scores) is None
    assert official_score(scores) == 0.0


@pytest.mark.parametrize("error", [
    "output dir not found: synthetic-output",
    "missing summary file: synthetic-summary.jsonl",
    "invalid json on line 2: synthetic invalid JSON",
])
def test_union_of_zero_metrics_is_also_an_official_zero(error):
    scores = {"synthetic_metric_a": 0.0, "synthetic_metric_b": 0.0, "error": error}
    assert "overall_score" not in scores
    assert official_score(scores) == 0.0
    assert classify_grader_issue(scores) is None


@pytest.mark.parametrize("error", [
    "gt_dir does not exist or is not a directory: /tmp_workspace/gt",
    "no .tex files found under gt_dir: /tmp_workspace/gt",
    "missing gt file: /tmp_workspace/gt/synthetic.json",
    "ground_truth.json not found: /tmp_workspace/gt/ground_truth.json",
])
def test_missing_verifier_inputs_cannot_be_reported_as_task_zero(error):
    assert classify_grader_issue({"overall_score": 0, "error": error}) == "judge_error"
    assert classify_grader_issue({"error": error}) == "judge_error"


@pytest.mark.parametrize("field", ["judge_error", "llm_error", "llm_judge_error", "parse_error"])
def test_explicit_top_level_judge_or_transcript_errors_are_unavailable(field):
    assert classify_grader_issue({"overall_score": 0, field: "synthetic failure"}) == "judge_error"
    assert classify_grader_issue({"overall_score": 0, field: None}) is None


@pytest.mark.parametrize("marker", [
    {"judge_method": "rule_fallback"}, {"mode": "fallback_results_md"},
    {"mode": "fallback+llm_judge"},
    {"error": "No audit data and results.md not found"},
    {"error": "No audit data and results.md is empty"},
])
def test_failed_audit_or_rule_fallback_stays_degraded(marker):
    assert classify_grader_issue({"overall_score": 0, **marker}) == "judge_degraded"


@pytest.mark.parametrize("marker", [
    {"mode": "audit", "calls": []}, {"mode": "audit+llm_judge", "calls": []},
    {"judge_method": "not_run_no_assistant_text"}, {"judge_method": "not_run_summary_gate"},
])
def test_empty_audit_or_deliverable_gate_is_not_a_provider_failure(marker):
    assert classify_grader_issue({"overall_score": 0, **marker}) is None


@pytest.mark.parametrize("scores", [
    {"overall_score": 0, "error": "unrecognized dependency or artifact failure"},
    {"overall_score": 0, "error": "missing something else"},
    {"overall_score": 0, "error": "workspace not found: /tmp_workspace"},
    {"overall_score": 0.5, "error": "results.md is empty"},
    {"error": "results.md is empty"},
    {"overall_score": 0, "mode": "unrecognized_fallback"},
    {"overall_score": 0, "judge_method": "unrecognized_fallback"},
])
def test_unknown_or_inconsistent_errors_require_review(scores):
    assert classify_grader_issue(scores) == "needs_review"


def test_extractor_ignores_prompt_and_answer_constants_and_does_not_execute():
    markdown = '''---
id: synthetic
---
## Prompt
PRIVATE_PROMPT_SENTINEL
## Automated Checks
```python
raise RuntimeError("THIS_MODULE_MUST_NEVER_EXECUTE")
expected_answer = "PRIVATE_ANSWER_SENTINEL"
def grade(**kwargs):
    score, judge_method = 0, "rule_fallback"
    return {"overall_score": 0, "error": f"missing summary file: {kwargs['path']}"}
```
## Ground Truth
PRIVATE_GOLD_SENTINEL
'''
    rows = extract_contracts(code_section(markdown))
    serialized = json.dumps(rows)
    assert "PRIVATE_" not in serialized
    assert "THIS_MODULE_MUST_NEVER_EXECUTE" not in serialized
    assert {row["field"] for row in rows} == {"error", "judge_method"}
    assert any(row["value"] == {"kind": "literal", "value": "rule_fallback"} for row in rows)
    assert any(row["value"] == {"kind": "template", "value": "missing summary file: {dynamic}"} for row in rows)


def test_static_manifest_covers_all_tasks_without_task_inputs():
    # Read only the generated error-schema manifest, never source tasks or gold.
    path = Path(__file__).resolve().parents[1] / "manifests/grader_error_contracts.json"
    manifest = json.loads(path.read_text())
    assert manifest["task_count"] == len(manifest["tasks"]) == 60
    assert len({row["task_id"] for row in manifest["tasks"]}) == 60
    assert sum(len(row["error_contracts"]) for row in manifest["tasks"]) == 81
    for task in manifest["tasks"]:
        assert set(task) == {"task_id", "source_sha256", "checks_sha256", "error_contracts"}
        for row in task["error_contracts"]:
            assert row["field"] in manifest["selected_fields"]
