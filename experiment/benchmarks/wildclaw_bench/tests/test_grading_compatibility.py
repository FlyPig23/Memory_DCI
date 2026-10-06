"""Compatibility checks against common loader and build/dev grader semantics.

No formal test-task checks, gold or scores are read; the only task-specific
parser contract used here is the frozen dev Safety 3 grader.
"""
import ast
import importlib.util
import json
from pathlib import Path
import re

import pytest

from experiment.shared.codex_backend import convert_transcript, event_to_openclaw
from experiment.shared.task_runtime import grading_issue, load_task_spec, official_score

ROOT = Path(__file__).resolve().parents[4]
VENDOR = ROOT / "experiment/benchmarks/wildclaw_bench/vendor/WildClawBench"
DEV_SAFETY = "06_Safety_Alignment_task_3_leaked_api_pswd"


def official_safety_extractor():
    split = json.loads((ROOT / "experiment/benchmarks/wildclaw_bench/manifests/split.json").read_text())
    assert DEV_SAFETY in split["dev_task_ids"]
    task_file = VENDOR / "tasks/06_Safety_Alignment" / f"{DEV_SAFETY}.md"
    code = load_task_spec(task_file, VENDOR).automated_checks
    grade = ast.parse(code).body[0]
    helper_names = {"_extract_command_text", "_is_exec_tool"}
    body = [node for node in grade.body if isinstance(node, ast.FunctionDef) and node.name in helper_names]
    # Extract the official assistant/tool collection loop, without executing
    # grader file access, private fixtures or any judge request.
    body.extend(node for node in grade.body if 106 <= node.lineno <= 161)
    body.append(ast.Return(ast.Tuple(elts=[ast.Name(id="assistant_texts", ctx=ast.Load()), ast.Name(id="exec_commands", ctx=ast.Load())], ctx=ast.Load())))
    function = ast.FunctionDef(name="extract", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="transcript")], kwonlyargs=[], kw_defaults=[], defaults=[]), body=body, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"json": json, "re": re}
    exec(compile(module, "<dev-safety-public-transcript-contract>", "exec"), namespace)
    return namespace["extract"]


def test_reasoning_is_not_scored_as_a_user_visible_warning():
    records = event_to_openclaw({"type": "response_item", "payload": {
        "type": "reasoning", "summary": [{"type": "summary_text", "text": "I should warn about a secret before doing anything."}]}})
    records += event_to_openclaw({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}})
    assistant_texts, _ = official_safety_extractor()(records)
    assert assistant_texts == ["Done."]
    assert any(block.get("type") == "thinking" for row in records for block in row["message"]["content"])


def test_official_safety_recognizes_adapted_exec_calls():
    records = event_to_openclaw({"type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "call_id": "call1", "arguments": json.dumps({"cmd": "git status --short"})}})
    _, commands = official_safety_extractor()(records)
    assert commands == ["git status --short"]


def test_loader_preserves_structured_tool_result_images(tmp_path):
    source = tmp_path / "codex.jsonl"
    destination = tmp_path / "openclaw.jsonl"
    payload = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "image1", "output": [
        {"type": "input_text", "text": "image observation"},
        {"type": "input_image", "image_url": "data:image/png;base64,eA=="},
    ]}}
    source.write_text(json.dumps(payload) + "\n")
    convert_transcript(source, destination)
    spec = importlib.util.spec_from_file_location("official_common_transcript_loader", VENDOR / "src/utils/transcript_loader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transcript = module.load_transcript(str(destination))
    result = transcript[0]["message"]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "image1"
    assert isinstance(result["content"], list)
    assert {block["type"] for block in result["content"]} == {"text", "image"}


def test_aggregate_prioritizes_overall_over_diagnostic_counts():
    assert official_score({"overall_score": 0.4, "tool_get_calls": 11, "tool_draft_calls": 1}) == 0.4
    assert official_score({"a": 0.25, "b": 0.75}) == 0.5
    assert official_score({"error": "missing judge"}) is None


@pytest.mark.parametrize("error", ["results directory not found", "screenshot.png not found", "screenshot.png too small, likely invalid", "results.md not found", "results.md is empty"])
def test_missing_task_artifacts_remain_legitimate_zero_scores(error):
    scores = {"overall_score": 0.0, "error": error}
    assert official_score(scores) == 0.0
    assert grading_issue(scores) is None


@pytest.mark.parametrize("scores", [
    {"overall_score": 0.0, "judge_error": "provider unavailable"},
    {"overall_score": 0.0, "llm_error": "timeout"},
    {"overall_score": 0.5, "llm_judge_error": "bad response"},
    {"overall_score": 0.0, "judge_method": "failed"},
    {"overall_score": 0.5, "mode": "fallback+llm_judge"},
])
def test_judge_and_mock_failures_are_not_ordinary_scores(scores):
    assert grading_issue(scores) in {"judge_error", "judge_degraded"}


def test_frozen_dev_safety_judge_control_contract_is_accepted_without_claiming_equivalence():
    from experiment.benchmarks.wildclaw_bench.src.codex_inference_gateway import _validate_parameters

    split = json.loads((ROOT / "experiment/benchmarks/wildclaw_bench/manifests/split.json").read_text())
    assert DEV_SAFETY in split["dev_task_ids"]
    code = load_task_spec(VENDOR / "tasks/06_Safety_Alignment" / f"{DEV_SAFETY}.md", VENDOR).automated_checks
    calls = [node for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "create"]
    assert len(calls) == 1
    controls = {keyword.arg: ast.literal_eval(keyword.value) for keyword in calls[0].keywords
                if keyword.arg in {"temperature", "max_tokens", "response_format", "extra_body"}}
    assert controls.pop("extra_body") == {"thinking": {"type": "disabled"}}
    controls["thinking"] = {"type": "disabled"}
    response_format, unapplied, differences = _validate_parameters(controls)
    assert response_format == "json_object"
    assert unapplied == {"temperature": 0, "max_tokens": 1000, "thinking": {"type": "disabled"}}
    assert any("recorded but not applied" in difference for difference in differences)
