#!/usr/bin/env python3
"""Recover infrastructure-failed grading from frozen evidence, without a solver."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiment.src.task_runtime import TaskRuntime, TaskSpec, contained, classify_grader_issue, official_score
from experiment.src.runtime_support import make_runtime_config
from experiment.src.mock_state import restore_mock_state, services_for_task, validate_snapshot
from experiment.src.inference_access import inference_client


RECOVERABLE = {
    "judge_error", "judge_degraded", "grader_error", "grader_timeout",
    "invalid_grader_output", "mock_state_error",
}
# These are controller paths, never solver deliverables. The normal controller
# puts most of them outside workspace; exclude them defensively on import.
EXCLUDED_ROOT_NAMES = {"gt", "controller-staging", "grade_runner.py", "_transcript_loader.py",
                       "solver_transcript.jsonl", "_mock_state_replay.py", "_mock_state_snapshot.json"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def private_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory_tree(root: Path, *, exclude: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Hash regular files; record symlink text without reading their targets.

    Directory symlinks are not traversed, and O_NOFOLLOW protects regular-file
    opens. The solver container must already be stopped. Special files are rejected.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Evidence root must be a real directory")
    inventory: dict[str, dict[str, Any]] = {}

    def walk(directory: Path, relative: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            if relative == Path(".") and entry.name in (exclude or set()):
                continue
            key = (relative / entry.name).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                inventory[key] = {"kind": "symlink", "target": os.readlink(entry.path)}
            elif stat.S_ISDIR(metadata.st_mode):
                inventory[key] = {"kind": "directory", "mode": mode}
                walk(Path(entry.path), relative / entry.name)
            elif stat.S_ISREG(metadata.st_mode):
                fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("Evidence changed file type while hashing")
                    h = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(block)
                inventory[key] = {"kind": "file", "mode": mode, "size": metadata.st_size, "sha256": h.hexdigest()}
            else:
                raise ValueError("Evidence contains an unsupported special file: " + key)

    walk(root, Path("."))
    return inventory


def copy_tree(source: Path, destination: Path, *, exclude: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Independent file copies, never hardlinks; preserve symlinks as symlinks."""
    before = inventory_tree(source, exclude=exclude)
    destination.mkdir(parents=True, exist_ok=False)
    for name, item in before.items():
        origin, target = source / name, destination / name
        if item["kind"] == "directory":
            target.mkdir()
        elif item["kind"] == "symlink":
            target.symlink_to(item["target"])
        else:
            fd = os.open(origin, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.chmod(target, item["mode"])
    # Apply directory modes after all children have been written.
    for name, item in reversed(list(before.items())):
        if item["kind"] == "directory":
            os.chmod(destination / name, item["mode"])
    if inventory_tree(source, exclude=exclude) != before or inventory_tree(destination) != before:
        raise RuntimeError("Evidence changed during copying")
    return before


def freeze_solver_evidence(runtime: TaskRuntime, *, transcript_path: Path,
                           transcript_loader: Path, task_source: Path,
                           source_sha256: str) -> Path:
    """Snapshot the stopped solver before first grading; never execute anything.

    Call once after backend.run and before runtime.grade. Incomplete snapshots
    remain visibly incomplete; they are never valid recovery sources.
    """
    if runtime.state != "solver_stopped":
        raise RuntimeError("Freeze evidence only after the entire solver container has stopped")
    root = runtime.config.project_root
    transcript_path = contained(transcript_path, runtime.run_dir)
    transcript_loader = contained(transcript_loader, root)
    task_source = contained(task_source, root)
    if any(path.is_symlink() for path in (transcript_path, transcript_loader, task_source)):
        raise ValueError("Frozen controller files must not be symlinks")
    if digest(task_source) != source_sha256:
        raise ValueError("Task source differs from the sealed source hash")
    evidence = runtime.run_dir / "solver-evidence"
    evidence.mkdir(mode=0o700, exist_ok=False)
    os.chmod(runtime.run_dir, 0o700)
    workspace_inventory = copy_tree(runtime.workspace, evidence / "workspace", exclude=EXCLUDED_ROOT_NAMES)
    shutil.copyfile(transcript_path, evidence / "transcript.openclaw.jsonl")
    shutil.copyfile(transcript_loader, evidence / "transcript_loader.py")
    verifier = evidence / "verifier"
    verifier.mkdir()
    # The controller stages official gt roots as project-local symlinks. Resolve
    # this trusted root within the project; solver symlinks remain unfollowed.
    gt_root = contained(runtime.task.gt_dir, root)
    gt_inventory = copy_tree(gt_root, verifier / "gt") if gt_root.is_dir() else {}
    if not (verifier / "gt").exists():
        (verifier / "gt").mkdir()
    task_data = {
        "task_id": runtime.task.task_id, "category": runtime.task.category,
        "prompt": runtime.task.prompt, "timeout_seconds": runtime.task.timeout_seconds,
        "automated_checks": runtime.task.automated_checks,
        "source_path": str(task_source.relative_to(root.resolve())), "source_sha256": source_sha256,
        "rubric_sha256": hashlib.sha256(runtime.task.automated_checks.encode()).hexdigest(),
        "original_workspace_path": str(runtime.task.workspace_path.relative_to(root.resolve())),
    }
    private_json(evidence / "frozen_task.json", task_data)
    snapshot = runtime.staging / "mock_state.json"
    mock_hash = None
    if services_for_task(runtime.task.task_id):
        if runtime.freeze_hook_error or not snapshot.is_file() or snapshot.is_symlink():
            raise RuntimeError("Social recovery requires a successful original audit snapshot")
        state = json.loads(snapshot.read_text())
        validate_snapshot(state, runtime.task.task_id)
        if state.get("container") != runtime.name:
            raise ValueError("Mock snapshot does not belong to the original solver container")
        shutil.copyfile(snapshot, evidence / "mock_state.json")
        mock_hash = digest(evidence / "mock_state.json")
    manifest = {
        "schema_version": 1, "status": "frozen", "frozen_at_unix": time.time(),
        "task_id": runtime.task.task_id, "source_container": runtime.name,
        "source_image": runtime.config.image, "workspace": workspace_inventory,
        "workspace_sha256": hashlib.sha256(canonical(workspace_inventory)).hexdigest(),
        "verifier_gt": gt_inventory,
        "transcript_sha256": digest(evidence / "transcript.openclaw.jsonl"),
        "transcript_loader_sha256": digest(evidence / "transcript_loader.py"),
        "frozen_task_sha256": digest(evidence / "frozen_task.json"),
        "mock_state_sha256": mock_hash,
        "policy": "Independent pre-grading copies; no hardlinks; symlinks preserved without following targets.",
    }
    private_json(evidence / "manifest.json", manifest)
    return evidence


def load_evidence(run_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    evidence = contained(run_dir / "solver-evidence", run_dir)
    manifest = json.loads((evidence / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("status") != "frozen":
        raise ValueError("Recovery requires a complete pre-grading evidence snapshot")
    task = json.loads((evidence / "frozen_task.json").read_text())
    checks = {
        "transcript.openclaw.jsonl": "transcript_sha256",
        "transcript_loader.py": "transcript_loader_sha256", "frozen_task.json": "frozen_task_sha256",
    }
    if manifest.get("mock_state_sha256"):
        checks["mock_state.json"] = "mock_state_sha256"
    for name, key in checks.items():
        path = evidence / name
        if path.is_symlink() or not path.is_file() or digest(path) != manifest.get(key):
            raise ValueError("Frozen evidence file changed: " + name)
    if inventory_tree(evidence / "workspace") != manifest["workspace"]:
        raise ValueError("Frozen solver artifacts changed")
    if inventory_tree(evidence / "verifier/gt") != manifest["verifier_gt"]:
        raise ValueError("Frozen verifier data changed")
    if hashlib.sha256(canonical(manifest["workspace"])).hexdigest() != manifest["workspace_sha256"]:
        raise ValueError("Workspace inventory digest mismatch")
    if task.get("task_id") != manifest.get("task_id") or hashlib.sha256(task["automated_checks"].encode()).hexdigest() != task["rubric_sha256"]:
        raise ValueError("Frozen rubric identity mismatch")
    return evidence, manifest, task


def assert_recoverable(run_dir: Path, record: Mapping[str, Any], *, check_recoveries: bool = True) -> None:
    evaluation = record.get("evaluation") or {}
    status = evaluation.get("status")
    stored_evaluation = run_dir / "evaluation.json"
    stored_status = json.loads(stored_evaluation.read_text()).get("status") if stored_evaluation.is_file() else None
    if status == "graded" or stored_status == "graded":
        raise ValueError("A valid graded result, including zero, cannot be retried")
    if status not in RECOVERABLE and not (record.get("status") == "infrastructure_error" and not evaluation):
        raise ValueError("Only explicitly identified grading infrastructure failures are recoverable")
    if not isinstance(record.get("execution"), dict):
        raise ValueError("No completed solver execution record; solver recovery is not supported")
    if status == "mock_state_error" and evaluation.get("stage") == "capture":
        raise ValueError("Missing original in-memory state cannot be reconstructed by regrading")
    if not check_recoveries:
        return
    for result_path in (run_dir / "grade-recovery").glob("*/attempt.json"):
        previous = json.loads(result_path.read_text())
        runtime_evaluation = result_path.parent / "runtime/evaluation.json"
        completed_status = (json.loads(runtime_evaluation.read_text()).get("status")
                            if runtime_evaluation.is_file() else None)
        if (previous.get("evaluation") or {}).get("status") == "graded" or completed_status == "graded":
            raise ValueError("An existing valid recovered grade cannot be retried to select another score")


def gateway_failures(project_root: Path, authorization_hash: str, started: float,
                     finished: float) -> list[dict[str, Any]]:
    path = project_root / "experiment/runtime/inference/events.jsonl"
    requests: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # Another request may be appending its final line.
        if row.get("authorization_sha256") != authorization_hash or not started <= row.get("started_at_unix", 0) <= finished:
            continue
        requests[row.get("request_id", "unknown")] = row
    return [row for row in requests.values() if row.get("status") != "completed"]


def recover_grade(project_root: Path, run_dir: Path, *, reason: str,
                  image: str | None = None, docker_host: str | None = None,
                  grading_timeout_seconds: int = 1800,
                  runtime_factory: Callable[..., TaskRuntime] = TaskRuntime,
                  config_factory: Callable[..., Any] = make_runtime_config) -> dict[str, Any]:
    """Create one grader-only attempt; original run files are never overwritten."""
    project_root = project_root.resolve()
    run_dir = contained(run_dir, project_root / "experiment/runs")
    if not reason.strip():
        raise ValueError("A concrete infrastructure-repair reason is required")
    original_record = run_dir / "result.json"
    record = json.loads(original_record.read_text())
    assert_recoverable(run_dir, record)
    evidence, manifest, frozen_task = load_evidence(run_dir)
    if frozen_task["task_id"] != record["task_id"]:
        raise ValueError("Snapshot task differs from the recorded solver run")
    original_hashes = {name: digest(run_dir / name) for name in ("result.json", "run.json", "evaluation.json", "score.json") if (run_dir / name).is_file()}
    source_inventory = inventory_tree(evidence)
    # One controller at a time prevents two concurrent retries selecting a score.
    lock = run_dir / ".grade-recovery.lock"
    lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    attempt_id = "regrade-" + uuid.uuid4().hex[:20]
    attempt = run_dir / "grade-recovery" / attempt_id
    runtime: TaskRuntime | None = None
    attempt_record: dict[str, Any] = {}
    clients = ExitStack()
    try:
        assert_recoverable(run_dir, json.loads(original_record.read_text()))
        attempt.mkdir(parents=True, mode=0o700)
        os.chmod(attempt.parent, 0o700)
        client = clients.enter_context(inference_client(project_root, attempt_id))
        marker = client.api_key
        authorization_hash = client.authorization_sha256
        attempt_record = {
            "schema_version": 1, "attempt_id": attempt_id, "source_run_id": record["run_id"],
            "task_id": record["task_id"], "reason": reason, "started_at_unix": time.time(),
            "status": "preparing", "solver_invocations": 0, "adopted": False,
            "source_evidence_manifest_sha256": digest(evidence / "manifest.json"),
            "source_workspace_sha256": manifest["workspace_sha256"],
            "source_transcript_sha256": manifest["transcript_sha256"],
            "source_rubric_sha256": frozen_task["rubric_sha256"],
            "source_record_sha256": original_hashes["result.json"],
            "source_image": manifest["source_image"], "grading_image": image or manifest["source_image"],
            "gateway_authorization_hash": authorization_hash,
        }
        private_json(attempt / "attempt.json", attempt_record)
        inputs = attempt / "inputs"
        copy_tree(evidence / "workspace", inputs / "exec")
        copy_tree(evidence / "verifier/gt", inputs / "gt")
        skills = attempt / "empty-skills"
        skills.mkdir()
        task = TaskSpec(frozen_task["task_id"], frozen_task["category"], frozen_task["prompt"],
                        inputs, skills, skills=(), warmup="", automated_checks=frozen_task["automated_checks"])

        def restore(runtime_instance):
            if services_for_task(task.task_id):
                restore_mock_state(runtime_instance, runtime_instance.staging / "mock_state.json")

        config = config_factory(project_root, image or manifest["source_image"], docker_host=docker_host,
                                warmup_override="", auth_file=None, before_freeze_hook=None,
                                after_restart_hook=restore, grading_timeout_seconds=grading_timeout_seconds,
                                environment={"OPENROUTER_API_KEY": marker,
                                             "OPENROUTER_BASE_URL": "http://api.hangxiao.internal/v1"})
        # The grader uses its explicit provider bridge; it has no CLI auth or DCI
        # server. Remove even the optional standalone Codex binary support mount.
        config = replace(config, support_mounts=tuple(m for m in config.support_mounts if not m.target.startswith("/opt/codex")))
        runtime = runtime_factory(config, task, attempt / "runtime", attempt_id)
        runtime.prepare("")
        transcript = runtime.run_dir / "transcript.openclaw.jsonl"
        shutil.copyfile(evidence / "transcript.openclaw.jsonl", transcript)
        if services_for_task(task.task_id):
            snapshot = json.loads((evidence / "mock_state.json").read_text())
            validate_snapshot(snapshot, task.task_id)
            if snapshot.get("container") != manifest["source_container"]:
                raise ValueError("Original mock snapshot container mismatch")
            # Rebind only envelope ownership to this disposable verifier. Audit
            # payloads and their hashes remain exactly the original frozen data.
            snapshot["container"] = runtime.name
            snapshot["recovery_source_container"] = manifest["source_container"]
            private_json(runtime.staging / "mock_state.json", snapshot)
            attempt_record["mock_state_source_sha256"] = manifest["mock_state_sha256"]
        runtime.freeze_agent()  # Stops the inert setup container; no solver exists.
        grade_started = time.time()
        attempt_record["grading_started_at_unix"] = grade_started
        evaluation = runtime.grade(transcript_path=transcript, transcript_loader=evidence / "transcript_loader.py")
        failures = gateway_failures(project_root, authorization_hash, grade_started, time.time())
        if failures:
            evaluation = dict(evaluation, status="judge_error", overall_score=None,
                              raw_overall_score=evaluation.get("raw_overall_score", evaluation.get("overall_score")),
                              gateway_errors=failures)
            private_json(runtime.run_dir / "evaluation.json", evaluation)
        attempt_record.update(status="finished", evaluation=evaluation)
    except Exception as error:
        attempt_record.update(status="infrastructure_error", error_type=type(error).__name__)
        if attempt.exists():
            import traceback
            log = attempt / "recovery.stderr.log"
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(traceback.format_exc())
    finally:
        if runtime is not None:
            try:
                runtime.cleanup()
            except Exception as error:
                attempt_record["cleanup_error"] = type(error).__name__
        try:
            clients.close()
        except Exception as error:
            attempt_record["inference_client_cleanup_error"] = type(error).__name__
        try:
            unchanged = (inventory_tree(evidence) == source_inventory and all(
                digest(run_dir / name) == value for name, value in original_hashes.items()))
            attempt_record["source_evidence_unchanged"] = unchanged
            if not unchanged:
                attempt_record.update(status="source_integrity_error", evaluation={
                    "status": "source_integrity_error", "overall_score": None, "scores": None})
            attempt_record["finished_at_unix"] = time.time()
            if attempt.exists():
                private_json(attempt / "attempt.json", attempt_record)
                private_json(run_dir / "evaluation_recovery.json", {
                    "adopted": False, "attempt_path": str(attempt.relative_to(project_root)),
                    "attempt_sha256": digest(attempt / "attempt.json"),
                    "status": attempt_record["status"], "evaluation": attempt_record.get("evaluation"),
                    "source_evidence_unchanged": unchanged,
                })
        finally:
            os.close(lock_fd)
            lock.unlink()
    return attempt_record



def _read_recovery_json(path: Path, boundary: Path) -> dict[str, Any]:
    """Read a controller record without following symlinks or path traversal."""
    path, boundary = Path(path).absolute(), boundary.resolve()
    if ".." in path.parts or not path.is_relative_to(boundary):
        raise ValueError("Recovery record path is outside its expected boundary")
    current = boundary
    for part in path.relative_to(boundary).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Recovery records must not be symlinks")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("Recovery record is missing or unreadable: " + path.name) from error
    if not isinstance(value, dict):
        raise ValueError("Recovery record must be a JSON object")
    return value


def _valid_graded_evaluation(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("status") != "graded":
        return False
    score = value.get("overall_score")
    return type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1


def _validated_recovery_candidate(project_root: Path, run_dir: Path,
                                  record: Mapping[str, Any]) -> dict[str, Any]:
    original = _read_recovery_json(run_dir / "result.json", run_dir)
    if canonical(record) != canonical(original):
        raise ValueError("Caller record differs from the original result.json")
    assert_recoverable(run_dir, original, check_recoveries=False)
    candidate_path = run_dir / "evaluation_recovery.json"
    candidate = _read_recovery_json(candidate_path, run_dir)
    if candidate.get("adopted") is not False or candidate.get("status") != "finished" or candidate.get("source_evidence_unchanged") is not True:
        raise ValueError("Recovery candidate is not a finished, unchanged, unadopted result")
    relative = Path(str(candidate.get("attempt_path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid candidate attempt path")
    attempt_dir = project_root / relative
    if attempt_dir.parent != run_dir / "grade-recovery":
        raise ValueError("Candidate attempt must be a direct recovery child of the source run")
    attempt_path = attempt_dir / "attempt.json"
    attempt = _read_recovery_json(attempt_path, run_dir / "grade-recovery")
    if digest(attempt_path) != candidate.get("attempt_sha256"):
        raise ValueError("Candidate attempt SHA256 mismatch")
    if attempt.get("attempt_id") != attempt_dir.name or attempt.get("source_run_id") != original.get("run_id") or attempt.get("task_id") != original.get("task_id"):
        raise ValueError("Recovery attempt run/task identity mismatch")
    if attempt.get("status") != "finished" or attempt.get("adopted") is not False or attempt.get("source_evidence_unchanged") is not True:
        raise ValueError("Recovery attempt is not a finished unchanged candidate")
    if type(attempt.get("solver_invocations")) is not int or attempt["solver_invocations"] != 0:
        raise ValueError("Recovery attempt must have zero solver invocations")
    if digest(run_dir / "result.json") != attempt.get("source_record_sha256"):
        raise ValueError("Recovery source-record SHA256 mismatch")
    evidence, manifest, task = load_evidence(run_dir)
    expected = {
        "source_evidence_manifest_sha256": digest(evidence / "manifest.json"),
        "source_workspace_sha256": manifest["workspace_sha256"],
        "source_transcript_sha256": manifest["transcript_sha256"],
        "source_rubric_sha256": task["rubric_sha256"],
    }
    if task["task_id"] != original["task_id"] or any(attempt.get(key) != value for key, value in expected.items()):
        raise ValueError("Recovery frozen-source lineage mismatch")
    evaluation = attempt.get("evaluation")
    if not _valid_graded_evaluation(evaluation):
        raise ValueError("Recovery requires a graded finite score in [0, 1]")
    if candidate.get("evaluation") != evaluation:
        raise ValueError("Candidate evaluation differs from its hashed attempt")
    runtime_evaluation = _read_recovery_json(attempt_dir / "runtime/evaluation.json", attempt_dir)
    if runtime_evaluation != evaluation:
        raise ValueError("Recovery runtime evaluation differs from its hashed attempt")
    scores = evaluation.get("scores")
    if not isinstance(scores, dict) or classify_grader_issue(scores) is not None or official_score(scores) != evaluation["overall_score"]:
        raise ValueError("Recovery score is not a valid unchanged official evaluation")
    if _read_recovery_json(attempt_dir / "runtime/score.json", attempt_dir) != scores:
        raise ValueError("Recovery raw score differs from the hashed evaluation")
    started = attempt.get("started_at_unix")
    if type(started) not in (int, float) or not math.isfinite(started):
        raise ValueError("Recovery attempt has no valid start timestamp")
    # Earlier failed attempts remain available, but never choose among valid
    # scores. Include runtime evaluations in case an outer record was interrupted.
    for prior_path in (run_dir / "grade-recovery").glob("*/attempt.json"):
        if prior_path == attempt_path:
            continue
        prior = _read_recovery_json(prior_path, run_dir / "grade-recovery")
        prior_runtime_path = prior_path.parent / "runtime/evaluation.json"
        prior_runtime = (_read_recovery_json(prior_runtime_path, prior_path.parent)
                         if prior_runtime_path.exists() else None)
        if _valid_graded_evaluation(prior.get("evaluation")) or _valid_graded_evaluation(prior_runtime):
            prior_started = prior.get("started_at_unix")
            if type(prior_started) not in (int, float) or not math.isfinite(prior_started) or prior_started <= started:
                raise ValueError("Only the first valid recovery may be adopted; no score selection is allowed")
    return {"candidate_sha256": digest(candidate_path), "attempt_sha256": digest(attempt_path),
            "source_record_sha256": attempt["source_record_sha256"], **expected,
            "attempt_path": str(attempt_dir.relative_to(project_root)), "attempt_id": attempt["attempt_id"],
            "source_run_id": attempt["source_run_id"], "task_id": attempt["task_id"],
            "evaluation": evaluation,
            "evaluation_sha256": hashlib.sha256(canonical(evaluation)).hexdigest(),
            "gateway_authorization_hash": attempt.get("gateway_authorization_hash"),
            "grading_started_at_unix": attempt.get("grading_started_at_unix"),
            "finished_at_unix": attempt.get("finished_at_unix")}


def adopt_recovery(run_dir: Path, reason: str, *, project_root: Path = ROOT) -> dict[str, Any]:
    """Explicitly adopt the first valid recovery by writing one independent record.

    Never modifies original result/evaluation, candidate, attempt or evidence.
    """
    project_root = project_root.resolve()
    run_dir = contained(run_dir, project_root / "experiment/runs")
    if not reason.strip():
        raise ValueError("An explicit adoption reason is required")
    lock = run_dir / ".grade-recovery.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        adoption_path = run_dir / "evaluation_adoption.json"
        if os.path.lexists(adoption_path):
            raise FileExistsError("An adoption already exists and will not be overwritten")
        original = _read_recovery_json(run_dir / "result.json", run_dir)
        validated = _validated_recovery_candidate(project_root, run_dir, original)
        adoption = {"schema_version": 1, "adopted": True, "source": "recovery",
                    "reason": reason, "adopted_at_unix": time.time(), **validated}
        private_json(adoption_path, adoption)
        return adoption
    finally:
        os.close(fd)
        lock.unlink()


def effective_evaluation(project_root: Path, run_dir: Path,
                         record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read an original grade or a fully verified, explicitly adopted recovery.

    No adoption returns (original evaluation, None). Invalid adoption raises
    ValueError; callers must report evidence_error rather than falling back or
    counting an unverified score. This function performs no writes.
    """
    project_root = project_root.resolve()
    run_dir = contained(run_dir, project_root / "experiment/runs")
    path = run_dir / "evaluation_adoption.json"
    if not os.path.lexists(path):
        return record.get("evaluation"), None
    try:
        adoption = _read_recovery_json(path, run_dir)
        if adoption.get("schema_version") != 1 or adoption.get("adopted") is not True or adoption.get("source") != "recovery":
            raise ValueError("Invalid adoption schema")
        if not isinstance(adoption.get("reason"), str) or not adoption["reason"].strip():
            raise ValueError("Adoption reason is missing")
        adopted_at = adoption.get("adopted_at_unix")
        if type(adopted_at) not in (int, float) or not math.isfinite(adopted_at):
            raise ValueError("Invalid adoption timestamp")
        validated = _validated_recovery_candidate(project_root, run_dir, record)
        if any(adoption.get(key) != value for key, value in validated.items()):
            raise ValueError("Adoption provenance no longer matches its verified recovery")
        metadata = {key: adoption[key] for key in (
            "source", "adopted", "attempt_id", "attempt_path", "attempt_sha256",
            "source_run_id", "source_record_sha256", "source_evidence_manifest_sha256",
            "reason", "adopted_at_unix", "gateway_authorization_hash",
            "grading_started_at_unix", "finished_at_unix")}
        metadata["adoption_sha256"] = digest(path)
        return validated["evaluation"], metadata
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Adopted recovery evidence is invalid: " + str(error)) from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--reason", required=True, help="Infrastructure repair or explicit adoption reason")
    parser.add_argument("--adopt", action="store_true", help="Validate and explicitly adopt the first valid recovery; do not run a grader")
    parser.add_argument("--image", help="Reviewed verifier image; default preserves the original image")
    parser.add_argument("--docker-host")
    parser.add_argument("--grading-timeout", type=int, default=1800)
    args = parser.parse_args()
    run_dir = args.run_dir if args.run_dir.is_absolute() else args.project_root / args.run_dir
    if args.adopt:
        result = adopt_recovery(run_dir, args.reason, project_root=args.project_root)
        print(json.dumps({key: result[key] for key in ("source_run_id", "attempt_id", "adopted", "reason")}, ensure_ascii=False))
        return
    result = recover_grade(args.project_root, run_dir, reason=args.reason, image=args.image,
                           docker_host=args.docker_host, grading_timeout_seconds=args.grading_timeout)
    print(json.dumps({key: result.get(key) for key in ("attempt_id", "source_run_id", "status", "evaluation", "adopted")}, ensure_ascii=False))
    if result["status"] != "finished":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
