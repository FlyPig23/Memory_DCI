"""Zero-inference checks for the side experiment. Run with pytest from the repo root."""
from __future__ import annotations

import json
import random

import pytest

from side_experiments.hard_task_failure_replay.scripts.common import CORPUS, MANIFESTS, TASKS, TB, load
from side_experiments.hard_task_failure_replay.scripts.distill import output_schema, render_lessons, validate
from side_experiments.hard_task_failure_replay.scripts.render import render_body, render_failed_attempt


def test_renderer_matches_pool_body_format():
    rows = [r for r in load(TB / "manifests/pool.json")["trajectories"]
            if r["status"] == "available" and r["source_format"] == "atif_json"]
    random.seed(20261006)
    for row in random.sample(rows, 80):
        source = json.loads((TB / row["source"]).read_text())
        if "data:image/" in (TB / row["source"]).read_text():
            continue  # images are elided on purpose in side-experiment files
        body = (TB / "prepared/pool" / row["pool_file"]).read_text().partition("\n\n")[2]
        assert render_body(source) == body, row["pool_file"]


def test_header_marks_failure_and_elides_images():
    text = render_failed_attempt({"steps": [{"message": "see data:image/png;base64,AAAA"}]}, task_id="t",
                                 model="m", agent="a 1", trial_id="x", version_status="exact")
    assert text.startswith("# Failed attempt at this task: t\n")
    assert "Official reward: 0" in text and "base64,AAAA" not in text and "inline image omitted" in text


@pytest.mark.parametrize("task_id", load(MANIFESTS / "selection.json")["chosen_task_ids"])
def test_corpus_holds_only_scored_failures_of_the_same_task(task_id):
    manifest = load(MANIFESTS / f"corpus_{task_id}.json")
    root = CORPUS / task_id / "experience/failed_attempts"
    files = sorted(p for p in root.rglob("*") if p.is_file())
    assert len(files) == manifest["attempt_count"] > 0
    for record in manifest["records"]:
        assert record["relative"].endswith(f"/{task_id}_0.txt")
        metadata = load(TB / record["source"].replace("trajectory.json", "trial_metadata.json"))
        assert metadata["reward"] == 0 and metadata["is_scored"] is True
        assert metadata["task_name"].endswith("/" + task_id)
    assert not any(p.name.endswith("_1.txt") for p in files)


def test_distill_validation_rejects_foreign_citations():
    records = [{"path": "/experience/failed_attempts/m/x/t_0.txt", "line_count": 10}]
    lesson = {"title": "a", "observation": "b", "likely_cause": "c", "confidence": "low",
              "next_attempt_check": "d", "sources": [{"path": records[0]["path"], "line_start": 1, "line_end": 3}]}
    value = {"overview": "o", "common_approach": "c", "lessons": [lesson], "unexplained": "", "reviewed_sources": []}
    assert validate(value, records, 8) is value
    bad = json.loads(json.dumps(value))
    bad["lessons"][0]["sources"][0]["path"] = "/pool/trajectories/other/x.txt"
    with pytest.raises(ValueError):
        validate(bad, records, 8)
    bad["lessons"][0]["sources"][0] = {"path": records[0]["path"], "line_start": 5, "line_end": 11}
    with pytest.raises(ValueError):
        validate(bad, records, 8)
    text = render_lessons("t", value, {"attempt_count": 1, "models": {"m": 1}},
                          {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"})
    assert "### 1. a (confidence: low)" in text and "t_0.txt:1-3" in text


def test_schema_is_strict():
    schema = output_schema(8)
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and sorted(node["required"]) == sorted(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(schema)


def test_replay_prompt_keeps_instruction_verbatim():
    task_id = load(MANIFESTS / "selection.json")["chosen_task_ids"][0]
    lessons = CORPUS / task_id / "experience/memory/lessons.md"
    if not lessons.exists():
        pytest.skip("lessons are produced by distill.py")
    pytest.importorskip("harbor")  # replay_runtime needs the TB2.1 environment
    from side_experiments.hard_task_failure_replay.scripts import replay_runtime
    instruction = (TASKS / task_id / "instruction.md").read_text()
    prompt = replay_runtime.replay_prompt(instruction, 900, task_id)
    assert prompt.startswith("The official solving deadline for this task is 900 seconds.\n\n")
    assert prompt.endswith(instruction) and lessons.read_text().strip() in prompt


def test_integrity_audit_flags_benchmark_fetches_only(tmp_path):
    from side_experiments.hard_task_failure_replay.scripts.run import integrity
    def events(*commands):
        path = tmp_path / "events.jsonl"
        path.write_text("".join(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                                                                              "command": c}}) + "\n" for c in commands))
        return path
    clean = events("grep -n x /experience/failed_attempts/claude-sonnet-5/a/t_0.txt",
                   "curl -L https://github.com/llvm/llvm-project/releases/download/x.tar.xz")
    assert integrity(clean)["integrity_violation"] is False
    dirty = events("git clone --depth 1 https://github.com/harbor-framework/terminal-bench-science.git /tmp/tbs")
    assert integrity(dirty)["integrity_violation"] is True


def test_lessons_may_cite_the_public_instruction():
    from side_experiments.hard_task_failure_replay.scripts.distill import citable
    task_id = load(MANIFESTS / "selection.json")["chosen_task_ids"][0]
    records = citable(task_id, [{"path": "/experience/failed_attempts/m/x/t_0.txt", "line_count": 10}])
    lesson = {"title": "a", "observation": "b", "likely_cause": "c", "confidence": "low", "next_attempt_check": "d",
              "sources": [{"path": "/experience/instruction.md", "line_start": 1, "line_end": 1}]}
    value = {"overview": "o", "common_approach": "c", "lessons": [lesson], "unexplained": "",
             "reviewed_sources": ["/experience/instruction.md", "/work/output_schema.json"]}
    assert validate(value, records, 8) is value


def _v2_sample(path):
    src = [{"path": path, "line_start": 1, "line_end": 2}]
    return {"task_summary": "t", "decision_guide": {"first_steps": ["a"], "avoid": ["b"], "before_finishing": ["c"]},
            "requirements": [{"requirement": "r", "how_attempts_handled_it": "h", "risk": "high"}],
            "approach_families": [{"name": "n", "attempt_count": 3, "furthest_progress": "f", "where_it_broke": "w",
                                   "verdict": "execution_likely_wrong", "evidence": "e", "sources": src}],
            "shared_assumptions": [{"assumption": "s", "why_suspect": "y", "sources": []}],
            "verification_gaps": [{"check_used": "c", "what_it_misses": "m"}],
            "lessons": [{"title": "l", "failure_mode": "verification_gap", "attempts_affected": 2, "observed_failure": "o",
                         "cause_hypothesis": "c", "cause_confidence": "hypothesis", "what_to_do_instead": "d",
                         "check_that_would_catch_it": "k", "sources": src}],
            "reusable_details": [], "unexplained": "", "reviewed_sources": []}


def test_v2_schema_validation_and_render():
    from side_experiments.hard_task_failure_replay.scripts import distill_v2
    path = "/experience/failed_attempts/m/12345678-aaaa/t_0.txt"
    value = _v2_sample(path)
    assert distill_v2.validate(value, {path: 5}) is value
    text = distill_v2.render("t", value, {"attempt_count": 3, "models": {"m": 3}}, {"model": "x", "effort": "medium"})
    assert "## Read this first" in text and "- First: a" in text and "m/12345678 L1-2" in text
    bad = _v2_sample("/pool/trajectories/other.txt")
    with pytest.raises(ValueError):
        distill_v2.validate(bad, {path: 5})
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False and sorted(node["required"]) == sorted(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(distill_v2.output_schema())


def test_arm_specs_mount_only_their_resources():
    pytest.importorskip("harbor")
    from side_experiments.hard_task_failure_replay.scripts.replay_runtime import arm_spec
    assert set(arm_spec("traj_only")["mounts"]) == {"/experience/failed_attempts"} and not arm_spec("traj_only")["memory"]
    assert set(arm_spec("mem_sonnet55")["mounts"]) == {"/experience/memory"} and not arm_spec("mem_sonnet55")["attempts"]
    # trajmem_<builder> is exactly traj_only's transcripts plus mem_<builder>'s analysis.
    for builder in ("luna56", "terra56", "sol56", "sonnet55", "astra6", "opus55", "opus55v4"):
        both, traj, mem = arm_spec(f"trajmem_{builder}"), arm_spec("traj_only"), arm_spec(f"mem_{builder}")
        assert both["mounts"] == {**traj["mounts"], **mem["mounts"]}
        assert both["memory"] == mem["memory"] and both["attempts"] is True
    with pytest.raises(ValueError):
        arm_spec("mem_unknown")
    with pytest.raises(ValueError):
        arm_spec("trajmem_unknown")


def test_trajmem_prompt_gives_transcripts_and_the_analysis_without_naming_its_writer():
    pytest.importorskip("harbor")
    from side_experiments.hard_task_failure_replay.scripts import replay_runtime
    from side_experiments.hard_task_failure_replay.scripts.campaign import ready
    from side_experiments.hard_task_failure_replay.scripts.run import ARMS
    task_id = load(MANIFESTS / "selection.json")["chosen_task_ids"][0]
    instruction = (TASKS / task_id / "instruction.md").read_text()
    for builder in ("luna56", "opus55v4"):
        arm = f"trajmem_{builder}"
        assert arm in ARMS and ready(arm, task_id)
        analysis = (CORPUS / task_id / "memories" / builder / "lessons.md").read_text().strip()
        prompt = replay_runtime.replay_prompt(instruction, 900, task_id, arm)
        assert prompt.startswith("The official solving deadline for this task is 900 seconds.\n\n")
        assert prompt.endswith(instruction) and analysis in prompt
        assert f"/experience/failed_attempts/<model>/<trial_id>/{task_id}_0.txt" in prompt
        assert "/experience/memory/lessons.md" in prompt and "not available" not in prompt
        # The preamble does not name the analysis' writer (the models list names the failed solvers). The pasted
        # lessons.md keeps its own header, which does name its writer, byte for byte as in mem_<builder>.
        assert "a failure analysis written by another model after it read these" in prompt
        assert "(gpt-5.6-luna) after it read" not in prompt
        identity = replay_runtime.corpus_identity(task_id, arm)
        assert identity["mounts"] == replay_runtime.arm_spec(arm)["mounts"]
        assert identity["memory_sha256"] == replay_runtime.corpus_identity(task_id, f"mem_{builder}")["memory_sha256"]
