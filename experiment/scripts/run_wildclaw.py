#!/usr/bin/env python3
"""Run the isolated V1 (A0) baseline with a fresh, independent protocol seal."""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiment.src.codex_backend import CodexBackend, CodexSettings
from experiment.src.task_runtime import TaskRuntime, load_task_spec
from experiment.src.runtime_support import make_runtime_config, copy_runtime_auth
from experiment.src.mock_state import (capture_mock_state, restore_mock_state,
                                       preflight_mock_state, MockPreflightError)
from experiment.src.inference_access import inference_client
from experiment.scripts.evaluate_wildclaw import freeze_solver_evidence
from experiment.src.experiment_protocol import (MODEL, EFFORT, IMAGE, COMMON, atomic_json,
    protocol, sha256)


def grading_setup(task) -> str:
    # Explicit, reviewed restarts will be added during construction-set QA.
    path = ROOT / "experiment/manifests/grading_startup.json"
    return json.loads(path.read_text()).get(task.task_id, "") if path.exists() else ""


def restore_captured_mock(runtime):
    if runtime.freeze_hook_result is not None:
        return restore_mock_state(runtime, runtime.freeze_hook_result)
    return None


def reviewed_warmup(task) -> str:
    path = ROOT / "experiment/manifests/warmup_dependencies.json"
    rows = json.loads(path.read_text())["tasks"]
    row = next(row for row in rows if row["task_id"] == task.task_id)
    if hashlib.sha256(task.warmup.encode()).hexdigest() != row["warmup_sha256"]:
        raise RuntimeError("Original warmup differs from the dependency audit")
    if row["mixed_lines_requiring_manual_review"]:
        raise RuntimeError("Warmup contains unreviewed mixed installation and state commands")
    validation = ROOT / "experiment/manifests/runtime_dependency_validation.json"
    if not validation.exists() or not json.loads(validation.read_text()).get("passed"):
        raise RuntimeError("Runtime dependency validation is required before skipping installation commands")
    return row["state_setup_preserve"]


def check_gateway_evaluation(record, run_dir, grade_started):
    events = ROOT / "experiment/runtime/inference/events.jsonl"
    errors = []
    if events.exists():
        for line in events.read_text().splitlines():
            row = json.loads(line)
            if row.get("authorization_sha256") == record["gateway_authorization_hash"] and row.get("status") == "error" and row.get("started_at_unix", 0) >= grade_started:
                errors.append(row)
    if errors:
        evaluation = record["evaluation"]
        evaluation.update(status="judge_error", raw_overall_score=evaluation.get("raw_overall_score", evaluation.get("overall_score")),
                          overall_score=None, gateway_errors=errors)
        atomic_json(run_dir / "evaluation.json", evaluation)


def run_one(task_id: str, arm: str, phase: str, timeout: int, run_id: str) -> dict:
    if arm != "A0":
        raise ValueError("Only the V1 baseline (A0) is retained")
    frozen = json.loads((ROOT / "experiment/manifests/v1_protocol.json").read_text()) if phase == "formal" else None
    split = json.loads((ROOT / "experiment/manifests/split.json").read_text())
    allowed = split["dev_task_ids"] if phase == "dev" else split["test_task_ids"]
    if task_id not in allowed:
        raise ValueError("Task is outside this phase's sealed allowlist")
    manifest = json.loads((ROOT / "experiment/manifests/task_manifest.json").read_text())
    metadata = next(t for t in manifest["tasks"] if t["task_id"] == task_id)
    repo = ROOT / "experiment/vendor/WildClawBench"
    task_file = repo / metadata["source_path"]
    if sha256(task_file) != metadata["source_sha256"]:
        raise ValueError("Frozen task source changed")
    task = load_task_spec(task_file, repo, workspace_root=ROOT / "experiment/runtime/task_inputs", timeout_seconds=timeout)
    skill_copy = ROOT / "experiment/runtime/skills"
    if skill_copy.is_dir():
        task = replace(task, skills_root=skill_copy)
    run_dir = ROOT / "experiment/runs" / phase / run_id
    if (run_dir / "result.json").exists():
        return json.loads((run_dir / "result.json").read_text())
    if (run_dir / "run.json").exists():
        raise RuntimeError("Existing incomplete run requires explicit recovery; solver will not be silently repeated")
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"task_id": task_id, "condition": arm, "phase": phase, "run_id": run_id,
              "started_at": time.time(), "timeout_seconds": timeout, "status": "starting"}
    atomic_json(run_dir / "run.json", record)
    rt = None
    try:
        with inference_client(ROOT, run_id) as gateway_client:
            gateway_key = gateway_client.api_key
            record["gateway_authorization_hash"] = gateway_client.authorization_sha256
            run_image = frozen["image_id"] if frozen else IMAGE
            config = make_runtime_config(ROOT, run_image,
                grading_timeout_seconds=1800,
                warmup_override=reviewed_warmup(task),
                before_freeze_hook=capture_mock_state,
                after_restart_hook=restore_captured_mock,
                environment={"OPENROUTER_API_KEY": gateway_key,
                             "OPENROUTER_BASE_URL": "http://api.hangxiao.internal/v1"})
            rt = TaskRuntime(config, task, run_dir, run_id)
            backend = CodexBackend(CodexSettings(model=frozen["model"] if frozen else MODEL,
                reasoning_effort=frozen["reasoning_effort"] if frozen else EFFORT,
                cli_version=frozen["cli_version"] if frozen else "0.153.4", mcp_servers=()))
            backend.prepare(rt)
            try:
                record["mock_preflight"] = preflight_mock_state(rt)
            except MockPreflightError as error:
                record["mock_preflight"] = error.record
                raise
            copy_runtime_auth(rt)
            record["preflight"] = backend.preflight(rt)
            record["status"] = "solving"
            atomic_json(run_dir / "run.json", record)
            execution = backend.run(rt, condition_instructions=frozen["conditions"]["A0"] if frozen else COMMON, verify_login=False)
            record["execution"] = {**asdict(execution), "transcript_path": str(execution.transcript_path.relative_to(ROOT))}
            evidence = freeze_solver_evidence(rt, transcript_path=execution.transcript_path,
                transcript_loader=repo / "src/utils/transcript_loader.py", task_source=task_file,
                source_sha256=metadata["source_sha256"])
            record["solver_evidence"] = str(evidence.relative_to(ROOT))
            record["status"] = "grading"
            atomic_json(run_dir / "run.json", record)
            grade_started = time.time()
            record["evaluation"] = rt.grade(transcript_path=execution.transcript_path,
                transcript_loader=repo / "src/utils/transcript_loader.py", grading_setup=grading_setup(task))
            check_gateway_evaluation(record, run_dir, grade_started)
            record["status"] = "finished"
    except Exception as error:
        record["status"] = "infrastructure_error"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if rt is not None:
            try:
                rt.cleanup()
            except Exception as error:
                record["cleanup_error"] = type(error).__name__
        record["finished_at"] = time.time()
        atomic_json(run_dir / "result.json", record)
        atomic_json(run_dir / "run.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("dev", "formal"), required=True)
    parser.add_argument("--task")
    parser.add_argument("--condition", choices=("A0",))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--dev-tag", default="v1")
    args = parser.parse_args()
    frozen_path = ROOT / "experiment/manifests/v1_protocol.json"
    if args.freeze:
        if args.phase != "formal" or frozen_path.exists():
            parser.error("Freeze requires formal phase and no existing protocol")
        if any((ROOT / "experiment/runs/formal").glob("*")):
            parser.error("Historical runs are read-only; prepare V1 in a fresh checkout without saved runs")
        validation = ROOT / "experiment/manifests/runtime_dependency_validation.json"
        if not validation.exists() or not json.loads(validation.read_text()).get("passed"):
            parser.error("Runtime dependency validation is required before freezing")
        atomic_json(frozen_path, protocol(ROOT, args.timeout))
        print(json.dumps({"frozen": str(frozen_path), "sha256": sha256(frozen_path)}))
        return
    if args.phase == "dev":
        if not args.task or not args.condition:
            parser.error("Development runs require --task and --condition")
        short = hashlib.sha256(args.task.encode()).hexdigest()[:12]
        run_id = f"dev-{args.dev_tag}-{short}-{args.condition.lower()}"
        rows = [(args.task, args.condition, args.timeout, run_id)]
    else:
        if args.task or args.condition:
            parser.error("Formal execution follows the whole frozen schedule")
        frozen = json.loads(frozen_path.read_text())
        for path, digest in frozen["hashes"].items():
            if sha256(ROOT / path) != digest:
                raise RuntimeError("Code or sources changed after protocol freeze: " + path)
        rows = [(r["task_id"], r["condition"], frozen["timeout_seconds"],
                 f"formal-{r['index']:02d}-{r['condition'].lower()}") for r in frozen["schedule"]]
    for task_id, arm, timeout, run_id in rows:
        result = run_one(task_id, arm, args.phase, timeout, run_id)
        print(json.dumps({"run_id": run_id, "task_id": task_id, "condition": arm,
                          "status": result["status"], "evaluation": result.get("evaluation")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
