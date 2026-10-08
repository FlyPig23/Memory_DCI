#!/usr/bin/env python3
"""Build V6's frozen training-failure seed, without mounting any test material.

The model runs only for ``build``. ``plan`` and ``status`` never call a model.
One task-grouped job receives snippets of every eligible failed trajectory, then
may inspect the original files. All decisions, omissions and attempts survive.
No reward-based retries or test-task selection occur. A failed infrastructure
attempt can be retried explicitly; successful jobs are immutable and reused.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiment.shared.codex_backend import (
    CodexSettings, convert_transcript, extract_usage, read_jsonl, render_config,
    select_main_session,
)
from experiment.shared.memory.tools import _entry, _validate

BASE = Path(__file__).resolve().parents[1]
MODEL, EFFORT, CLI_VERSION = "gpt-5.6-luna", "xhigh", "0.153.4"
ALLOWED_SOURCE_STATUSES = {"available", "official_raw_unparsed"}
DEFAULT_IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
PINNED_SPLIT_SHA256 = "4f437d77ebd35e420fe8dbd02fa34395f91c8f9a7d541f1d61798482e52938b6"
INFRASTRUCTURE = re.compile(
    r"(?:at capacity|capacity exceeded|rate limit|429\b|too many requests|usage limit|"
    r"connection (?:reset|refused|closed)|error sending request|network is unreachable|"
    r"failed to connect|service unavailable|502\b|503\b|504\b|unauthorized|"
    r"authentication|token expired|refresh token|quota exceeded)", re.I)


def sha(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path: Path):
    return json.loads(Path(path).read_text())


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def checked_file(root: Path, relative: str) -> Path:
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError("Unsafe pool path")
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Pool root cannot be a symlink")
    path = root
    for piece in part.parts:
        path = path / piece
        if path.is_symlink():
            raise ValueError("Pool paths cannot contain symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Missing or escaping pool source")
    return path


def candidates(pool_audit: dict, training_task_ids: list[str], pool: Path) -> tuple[list[dict], dict]:
    """Select only scored failures; trust neither path spelling nor score alone."""
    training = set(training_task_ids)
    seen = set()
    selected, excluded = [], Counter()
    for row in pool_audit["trajectories"]:
        if row.get("task_id") not in training:
            raise ValueError("Pool audit contains a non-training task")
        if type(row.get("score")) not in (int, float) or row.get("score") != 0 or row.get("is_scored") is not True:
            excluded["not_scored_failure"] += 1
            continue
        if row.get("status") not in ALLOWED_SOURCE_STATUSES:
            excluded["body_unavailable"] += 1
            continue
        relative = row.get("pool_file", "")
        parts = PurePosixPath(relative).parts
        if len(parts) < 4 or parts[:2] != ("trajectories", row["task_id"]):
            raise ValueError("Source path does not match its training task")
        if relative in seen:
            raise ValueError("Duplicate trajectory source")
        seen.add(relative)
        path = checked_file(pool, relative)
        digest = sha(path)
        if digest != row["pool_sha256"]:
            raise ValueError("Trajectory differs from frozen pool audit")
        # Decode strictly: a raw-unparsed export may still be a readable text source.
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            excluded["empty_body"] += 1
            continue
        selected.append({"task_id": row["task_id"], "trial_id": row["trial_id"],
                         "model": row.get("model"), "score": 0, "is_scored": True,
                         "status": row["status"], "path": "/pool/" + relative,
                         "pool_file": relative, "sha256": digest,
                         "line_count": len(lines), "bytes": path.stat().st_size})
    selected.sort(key=lambda item: (item["task_id"], item["trial_id"]))
    return selected, dict(sorted(excluded.items()))


ADAPTIVE_BUDGET = {"mode": "adaptive_failure_volume_v1", "base_seconds": 300,
                   "seconds_per_trajectory": 30, "seconds_per_ceil_mebibyte": 60,
                   "minimum_seconds": 600, "maximum_seconds": 3600,
                   "rationale": "Heuristic wall-clock ceiling from failed-source count and size; not empirically optimized or a target duration."}


def budget_policy(timeout_seconds: int | None) -> dict:
    if timeout_seconds is None:
        return copy.deepcopy(ADAPTIVE_BUDGET)
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600:
        raise ValueError("Explicit fixed builder budget must be 60..3600 seconds")
    return {"mode": "fixed_override", "timeout_seconds": timeout_seconds}


def job_budget(records: list[dict], timeout_seconds: int | None = None) -> dict:
    policy = budget_policy(timeout_seconds)
    count, size = len(records), sum(row["bytes"] for row in records)
    mib = (size + 1024 * 1024 - 1) // (1024 * 1024)
    unbounded = 300 + 30 * count + 60 * mib
    seconds = max(600, min(3600, unbounded)) if timeout_seconds is None else timeout_seconds
    return {"candidate_bytes": size, "timeout_seconds": seconds,
            "budget_basis": {"mode": policy["mode"], "candidate_count": count,
                             "candidate_bytes": size, "ceil_mebibytes": mib,
                             "unbounded_seconds": unbounded if timeout_seconds is None else timeout_seconds}}


def job_timeout(plan: dict, job: dict) -> int:
    """Read modern per-job budgets or the unchanged legacy global budget."""
    seconds = job.get("timeout_seconds", plan.get("timeout_seconds_per_job"))
    if type(seconds) is not int or not 60 <= seconds <= 3600:
        raise ValueError("Invalid frozen job timeout")
    return seconds


def build_plan(base: Path = BASE, *, timeout_seconds: int | None = None,
               packet_chars: int = 90000) -> dict:
    policy = budget_policy(timeout_seconds)
    if not 10000 <= packet_chars <= 200000:
        raise ValueError("Packet budget must be 10000..200000 characters")
    base = Path(base)
    split_path = base / "manifests/split.json"
    if sha(split_path) != PINNED_SPLIT_SHA256:
        raise ValueError("Use the frozen Terminal-Bench 2.1 split")
    split = load(split_path)
    training = sorted(split["training_task_ids"])
    if len(training) != 53 or len(set(training)) != 53:
        raise ValueError("Expected exactly 53 unique training tasks")
    pool = base / "prepared/pool"
    public = load(pool / "manifest.json")
    audit_path = base / "manifests/pool.json"
    audit = load(audit_path)
    if sorted(public["training_task_ids"]) != training or sorted(audit["training_task_ids"]) != training:
        raise ValueError("Pool training IDs disagree with frozen split")
    if audit.get("split_sha256") != PINNED_SPLIT_SHA256:
        raise ValueError("Pool audit was generated for another split")
    records, excluded = candidates(audit, training, pool)
    cards = {}
    for task in training:
        path = checked_file(pool, f"tasks/{task}.md")
        digest = sha(path)
        if digest != audit["files_sha256"].get(f"tasks/{task}.md"):
            raise ValueError("Task card differs from frozen pool")
        cards[task] = digest
    groups = [{"task_id": task, "candidate_count": sum(r["task_id"] == task for r in records),
               "candidate_paths": [r["path"] for r in records if r["task_id"] == task]}
              for task in training if any(r["task_id"] == task for r in records)]
    for group in groups:
        group.update(job_budget([r for r in records if r["task_id"] == group["task_id"]], timeout_seconds))
    source_files = [Path(__file__), Path(__file__).with_name("failure_memory_prompt.txt"),
                    ROOT / "experiment/shared/codex_backend.py", ROOT / "experiment/shared/memory/tools.py",
                    ROOT / "experiment/shared/memory/seed.py"]
    return {"schema_version": 1, "benchmark": "Terminal-Bench 2.1", "purpose": "V6 initial failure memory",
            "builder_model": MODEL, "builder_reasoning_effort": EFFORT, "cli_version": CLI_VERSION,
            "timeout_seconds_per_job": timeout_seconds, "budget_policy": policy,
            "packet_character_budget_per_job": packet_chars,
            "source_selection": "failed_training_trajectories", "training_task_ids": training,
            "candidate_count": len(records), "candidate_bytes": sum(r["bytes"] for r in records),
            "job_count": len(groups), "tasks_without_failure_candidates": [t for t in training if t not in {g["task_id"] for g in groups}],
            "excluded_record_counts": excluded, "source_status_counts": dict(Counter(r["status"] for r in records)),
            "input_hashes": {"split.json": sha(split_path), "pool.json": sha(audit_path),
                             "public_pool_manifest.json": sha(pool / "manifest.json")},
            "task_card_sha256": cards, "source_code_sha256": {str(p.relative_to(ROOT)): sha(p) for p in source_files},
            "candidate_records": records, "jobs": groups,
            "coverage_note": "Every candidate gets bounded deterministic excerpts. Full text is available for targeted reads; extraction is not exhaustive."}


def line_excerpt(lines: list[str], budget: int) -> tuple[str, list[int]]:
    """Prioritize terminal error observations and final state, preserving line IDs."""
    matcher = re.compile(r"traceback|exception|error:|failed|failure|assertion|not found|timeout|out of memory|killed|permission denied", re.I)
    hits = [i for i, line in enumerate(lines) if matcher.search(line)]
    # Last three failure clusters and tail are deterministic, independent of test tasks.
    priority = []
    for i in reversed(hits[-3:]):
        priority.extend(range(max(0, i - 2), min(len(lines), i + 4)))
    priority.extend(range(max(0, len(lines) - 14), len(lines)))
    priority.extend(range(min(8, len(lines))))
    chosen, used = {}, 0
    for i in priority:
        if i in chosen:
            continue
        line = lines[i]
        rendered = f"L{i + 1}: {line[:450]}" + (" [LONG LINE TRUNCATED]" if len(line) > 450 else "") + "\n"
        if used + len(rendered) <= budget:
            chosen[i] = rendered
            used += len(rendered)
    return "".join(chosen[i] for i in sorted(chosen)), [i + 1 for i in sorted(chosen)]


def evidence_packet(records: list[dict], pool: Path, total_chars: int) -> tuple[str, dict]:
    if not records:
        raise ValueError("A builder job requires at least one source")
    headers = [f"\n## {r['path']}\ntrial={r['trial_id']}; reward=0; source={r['status']}; original_lines={r['line_count']}\n" for r in records]
    available = total_chars - sum(map(len, headers))
    if available < 100 * len(records):
        raise ValueError("Packet budget cannot give every candidate an excerpt")
    each = available // len(records)
    chunks, coverage = [], []
    for record, header in zip(records, headers):
        lines = checked_file(pool, record["pool_file"]).read_text().splitlines()
        excerpt, numbers = line_excerpt(lines, each)
        chunks.append(header + excerpt)
        coverage.append({"path": record["path"], "excerpted_original_lines": numbers,
                         "total_original_lines": len(lines), "source_sha256": record["sha256"]})
    packet = "".join(chunks)
    assert len(packet) <= total_chars
    return packet, {"candidate_count": len(records), "packet_chars": len(packet),
                    "excerpt_coverage": coverage, "complete_reading_claimed": False}


def output_schema() -> dict:
    entry = copy.deepcopy(_entry)
    entry["required"] = sorted(entry["properties"])
    entry["properties"]["status"]["enum"] = ["source_observed", "uncertain"]
    text = {"type": "string", "minLength": 1, "maxLength": 12000}
    lesson = {"type": "object", "additionalProperties": False, "properties": {
        "entry": entry, "observed_failure": text, "cause_hypothesis": text,
        "cause_confidence": {"type": "string", "enum": ["observed", "hypothesis", "unknown"]},
        "proposed_fix": text, "fix_evidence": {"type": "string", "enum": ["unverified", "observed_recovery"]},
        "failure_kind": {"type": "string", "enum": ["task_failure", "infrastructure", "unknown"]}}}
    lesson["required"] = sorted(lesson["properties"])
    properties = {"decision": {"type": "string", "enum": ["write", "no_update"]}, "reason": text,
                  "lessons": {"type": "array", "items": lesson, "maxItems": 6},
                  "reviewed_sources": {"type": "array", "items": text, "maxItems": 200}}
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": sorted(properties)}


def task_slug_namespace(task_id: str) -> str:
    """Keep existing short legal IDs; give transformed IDs a stable hash suffix.

    Punctuation folding alone would merge names such as task.1 and task-1.
    The original spelling is hashed, and long IDs are shortened before adding
    the suffix so the lesson index and some of its own slug always fit.
    """
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", task_id) and len(task_id) <= 63:
        return task_id
    readable = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-") or "task"
    suffix = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return readable[:50].rstrip("-") + "-" + suffix


def validate_output(value: dict, records: list[dict], task_id: str) -> list[dict]:
    _validate(value, output_schema(), "builder_output")
    if (value["decision"] == "write") != bool(value["lessons"]):
        raise ValueError("write requires lessons; no_update requires an empty list")
    allowed = {r["path"]: r for r in records}
    if len(value["reviewed_sources"]) != len(set(value["reviewed_sources"])):
        raise ValueError("Duplicate declared reviewed source")
    if set(value["reviewed_sources"]) - allowed.keys():
        raise ValueError("Builder claimed a source outside its failed training task")
    entries = []
    for number, lesson in enumerate(value["lessons"], 1):
        entry = copy.deepcopy(lesson["entry"])
        for pointer in entry["sources"]:
            source = allowed.get(pointer["path"])
            if source is None:
                raise ValueError("Lesson cites a non-candidate source")
            if not 1 <= pointer["line_start"] <= pointer["line_end"] <= source["line_count"]:
                raise ValueError("Lesson's original source line range is invalid")
        # Namespacing makes repeated generic lesson titles harmless across tasks.
        prefix = f"{task_slug_namespace(task_id)}-{number}-"
        stem = entry["slug"][:80 - len(prefix)]
        entry["slug"] = (prefix + stem).rstrip("-")
        entry["evidence_summary"] = (
            f"Observed failure: {lesson['observed_failure']}\n"
            f"Failure kind: {lesson['failure_kind']}\n"
            f"Cause ({lesson['cause_confidence']}): {lesson['cause_hypothesis']}\n"
            f"Proposed fix ({lesson['fix_evidence']}): {lesson['proposed_fix']}\n"
            f"Source interpretation: {entry['evidence_summary']}\n"
            "All cited trajectories received reward 0. No future test task has verified this lesson.")
        # Hypotheses can be useful, but must remain visibly uncertain to the reader.
        if lesson["cause_confidence"] != "observed" or lesson["failure_kind"] == "unknown":
            entry["status"] = "uncertain"
        _validate(entry, _entry, "normalized_entry")
        entries.append(entry)
    return entries


def prepare_inputs(work: Path, plan: dict, pool: Path) -> Path:
    target = work / "inputs/pool"
    target.mkdir(parents=True, exist_ok=True)
    files = {r["pool_file"]: r["sha256"] for r in plan["candidate_records"]}
    files.update({f"tasks/{task}.md": digest for task, digest in plan["task_card_sha256"].items()})
    allowed_existing = set(files) | {"manifest.json"}
    for existing in target.rglob("*"):
        if existing.is_symlink() or (existing.is_file() and str(existing.relative_to(target)) not in allowed_existing):
            raise ValueError("Builder input directory contains unexpected material")
    for relative, digest in files.items():
        source = checked_file(pool, relative)
        destination = target / relative
        if destination.exists():
            if sha(destination) != digest:
                raise ValueError("Existing builder input changed")
            continue
        if sha(source) != digest:
            raise ValueError("Frozen source changed during input preparation")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o444)
    public = {key: plan[key] for key in ("training_task_ids", "candidate_count", "source_selection")}
    public["records"] = plan["candidate_records"]
    public["scope"] = "Historical failed training trajectories only; no current test artifacts or outcomes"
    path = target / "manifest.json"
    if path.exists() and load(path) != public:
        raise ValueError("Builder public manifest changed")
    if not path.exists():
        write_json(path, public)
        path.chmod(0o444)
    return target


def audit_session(home: Path, attempt: Path) -> tuple[dict, dict]:
    session = select_main_session(list((home / "sessions").rglob("*.jsonl")))
    if session:
        shutil.copyfile(session, attempt / "codex.session.jsonl")
    source = attempt / "codex.session.jsonl" if session else attempt / "codex.events.jsonl"
    usage = extract_usage(source, cumulative=session is not None)
    convert_transcript(source, attempt / "transcript.openclaw.jsonl")
    models, efforts = set(), set()
    for event in read_jsonl(source):
        if event.get("type") == "turn_context":
            payload = event.get("payload", {})
            models.add(payload.get("model"))
            efforts.add(payload.get("effort", payload.get("reasoning_effort")))
    audit = {"models": sorted(models, key=str), "efforts": sorted(efforts, key=str),
             "requested_model": MODEL, "requested_effort": EFFORT,
             "model_effort_verified": models == {MODEL} and efforts == {EFFORT},
             "global_memory": False, "initial_distilled_skills": 0}
    write_json(attempt / "model_audit.json", audit)
    write_json(attempt / "usage.json", usage)
    return usage, audit


def support(base: Path, image: str) -> dict:
    binary = base / "runtime/bin/codex"
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != "codex-cli " + CLI_VERSION:
        raise ValueError("Pinned builder Codex version mismatch")
    python = list((base / "runtime/python").glob("*/bin/python3"))
    if len(python) != 1:
        raise ValueError("Expected one pinned standalone Python installation")
    certificate = base / "prepared/transport/ca-certificates.crt"
    if not certificate.is_file():
        raise ValueError("Prepared transport CA bundle missing")
    inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                               text=True, capture_output=True)
    if inspected.returncode:
        subprocess.run(["docker", "pull", image], check=True, timeout=600)
        inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                   check=True, text=True, capture_output=True)
    python_root = python[0].parents[1]
    return {"image_id": inspected.stdout.strip(), "cli_version": version,
            "python_root": str(python_root), "python_version": subprocess.check_output([str(python[0]), "--version"], text=True).strip(),
            "binary_sha256": {p.name: sha(p) for p in sorted((base / "runtime/bin").iterdir()) if p.is_file()},
            "python_installation_sha256": {str(p.relative_to(python_root)): sha(p) for p in sorted(python_root.rglob("*"))
                                           if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"},
            "ca_sha256": sha(certificate)}


def docker_argv(base: Path, support_info: dict, input_pool: Path, attempt: Path,
                home: Path, name: str) -> list[str]:
    args = ["docker", "run", "--name", name, "--interactive", "--init", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--workdir", "/work",
            "--env", "CODEX_HOME=/opt/builder-home", "--env", "HOME=/opt/builder-home",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PATH=/opt/codex:/opt/builder-python/bin:/usr/local/bin:/usr/bin:/bin",
            "--env", "SSL_CERT_FILE=/opt/builder-ca.crt", "--env", "CODEX_CA_CERTIFICATE=/opt/builder-ca.crt"]
    mounts = [(input_pool, "/pool", True), (attempt, "/work", False), (home, "/opt/builder-home", False),
              (base / "runtime/bin", "/opt/codex", True),
              (Path(support_info["python_root"]), "/opt/builder-python", True),
              (base / "prepared/transport/ca-certificates.crt", "/opt/builder-ca.crt", True)]
    for source, dest, readonly in mounts:
        if "," in str(source):
            raise ValueError("Docker bind paths containing commas are unsupported")
        args += ["--mount", f"type=bind,src={source.resolve()},dst={dest}" + (",readonly" if readonly else "")]
    return args + [support_info["image_id"], "/opt/codex/codex", "exec", "--json", "--skip-git-repo-check",
                   "--dangerously-bypass-approvals-and-sandbox", "--cd", "/work",
                   "--output-schema", "/work/output_schema.json", "--output-last-message", "/work/result.json", "-"]


RESULT_FILES = ("result.json", "failure_memory.json")


def collect_output(attempt: Path, records: list[dict], task_id: str, *, execution: dict) -> tuple[dict, list[dict], dict]:
    """Validate native delivery, then a fixed checkpoint name; never alter either.

    A timeout is an execution outcome, not proof that a fully-written checkpoint
    is unusable. Recovery requires a checkpoint present within the original
    budget. mtime evidence is recorded as filesystem evidence, not a claim of
    cryptographic proof that the model read every cited line.
    """
    if not execution.get("model_audit", {}).get("model_effort_verified"):
        raise ValueError("Unverified builder model cannot supply a checkpoint")
    started = execution.get("started_at_unix")
    if started is None:
        # This file is written before process launch; the derived deadline is
        # conservative for legacy attempts that did not record a wall timestamp.
        started = (attempt / "prompt.txt").stat().st_mtime
        basis = "prompt_file_mtime_before_process_launch"
    else:
        basis = "recorded_process_launch_wall_clock"
    deadline = started + execution["timeout_seconds"]
    failures = []
    for filename in RESULT_FILES:
        path = attempt / filename
        if not path.exists():
            failures.append({"filename": filename, "classification": "missing"})
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Builder output must be a regular file without symlinks")
        origin = {"result_file": filename, "result_origin": "native_final" if filename == "result.json" else "checkpoint",
                  "origin_sha256": sha(path), "source_mtime_unix": path.stat().st_mtime,
                  "started_at_unix": started, "deadline_unix": deadline, "deadline_basis": basis,
                  "source_after_start": path.stat().st_mtime >= started,
                  "source_pre_deadline": path.stat().st_mtime <= deadline,
                  "prior_candidates": copy.deepcopy(failures)}
        if (execution.get("timed_out") or filename == "failure_memory.json") and not (origin["source_pre_deadline"] and origin["source_after_start"]):
            failures.append({"filename": filename, "classification": "outside_attempt_time_window", "sha256": origin["origin_sha256"]})
            continue
        try:
            raw = load(path)
            entries = validate_output(raw, records, task_id)
        except (ValueError, KeyError, TypeError) as exc:
            failures.append({"filename": filename, "classification": "invalid_output", "sha256": origin["origin_sha256"],
                             "error_type": type(exc).__name__, "validation_error": str(exc)[:500]})
            continue
        return raw, entries, origin
    raise ValueError("No valid in-budget builder output: " + json.dumps(failures, sort_keys=True))


def apply_exclusions(entries: list[dict], exclusions: list[dict]) -> list[dict]:
    if not isinstance(exclusions, list):
        raise ValueError("Audited exclusions must be a list")
    indexes = set()
    for item in exclusions:
        if not isinstance(item, dict) or set(item) != {"index", "reason"}:
            raise ValueError("Each exclusion needs exactly index and reason")
        index, reason = item["index"], item["reason"]
        if type(index) is not int or not 0 <= index < len(entries) or index in indexes:
            raise ValueError("Invalid or duplicate excluded lesson index")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("An excluded lesson requires an explicit audit reason")
        indexes.add(index)
    return [entry for n, entry in enumerate(entries) if n not in indexes]


def completed_fields(attempt: Path, raw: dict, entries: list[dict], records: list[dict], origin: dict) -> dict:
    cited = {s["path"] for e in entries for s in e["sources"]}
    return {"status": "complete", "decision": raw["decision"], "entry_count": len(entries),
            "declared_reviewed_sources": raw["reviewed_sources"], "cited_sources": sorted(cited),
            "uncited_candidate_count": len(records) - len(cited), "complete_reading_claimed": False,
            "result_file": origin["result_file"], "result_origin": origin,
            "result_sha256": sha(attempt / origin["result_file"]), "entries_sha256": sha(attempt / "entries.json"),
            "packet_sha256": sha(attempt / "evidence_packet.txt"), "session_sha256": sha(attempt / "codex.session.jsonl")}


def plan_by_digest(work: Path, digest: str) -> dict:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid frozen plan digest")
    active = work / "plan.json"
    path = active if sha(active) == digest else work / "plan_history" / (digest + ".json")
    if path.is_symlink() or not path.is_file() or sha(path) != digest:
        raise ValueError("Original frozen attempt plan is unavailable or changed")
    return load(path)


def attempt_matches_plan(original: dict, plan: dict) -> bool:
    matches = [job for job in plan["jobs"] if job["task_id"] == original["task_id"]]
    if len(matches) != 1:
        return False
    job = matches[0]
    count = sum(r["task_id"] == original["task_id"] for r in plan["candidate_records"])
    return (original.get("timeout_seconds") == job_timeout(plan, job) and
            original.get("source_paths") == job["candidate_paths"] and original.get("candidate_count") == count)


def resolve_attempt_plan(work: Path, attempt: Path, original: dict) -> tuple[dict, str, str]:
    """Keep legacy recovery on its original budget after a prospective amendment."""
    if original.get("plan_sha256"):
        digest = original["plan_sha256"]
        plan = plan_by_digest(work, digest)
        basis = "attempt_recorded_plan_digest"
    else:
        relative = str((attempt / "attempt.json").relative_to(work))
        artifact_hash = sha(attempt / "attempt.json")
        digest = None
        for receipt_path in sorted((work / "plan_history").glob("amendment-*.json")):
            receipt = load(receipt_path)
            if receipt.get("preserved_job_artifact_sha256", {}).get(relative) == artifact_hash:
                candidate = plan_by_digest(work, receipt["old_plan_sha256"])
                if attempt_matches_plan(original, candidate):
                    digest, plan = receipt["old_plan_sha256"], candidate
                    break
        if digest is not None:
            basis = "amendment_binds_preserved_legacy_attempt"
        else:
            digest, plan = sha(work / "plan.json"), load(work / "plan.json")
            basis = "unchanged_active_legacy_plan"
    if not attempt_matches_plan(original, plan):
        raise ValueError("Saved attempt budget or candidate coverage differs from its original frozen plan")
    return plan, digest, basis


def budget_retry_authorized(work: Path, attempt: Path, original: dict, plan: dict, job: dict) -> bool:
    """Require a recorded prospective increase that already preserved this attempt."""
    if original.get("status") != "budget_exhausted" or job_timeout(plan, job) <= original["timeout_seconds"]:
        return False
    resolve_attempt_plan(work, attempt, original)
    relative = str((attempt / "attempt.json").relative_to(work))
    artifact_hash = sha(attempt / "attempt.json")
    active_digest = sha(work / "plan.json")
    receipts = [load(path) for path in sorted((work / "plan_history").glob("amendment-*.json"))]
    # Walk the active amendment lineage so a receipt on another branch cannot authorize a retry.
    seen = set()
    while active_digest not in seen:
        seen.add(active_digest)
        matches = [receipt for receipt in receipts if receipt.get("new_plan_sha256") == active_digest]
        if not matches:
            return False
        if len(matches) != 1:
            raise ValueError("Ambiguous plan amendment lineage")
        receipt = matches[0]
        if (receipt.get("budget_update_accepted") is True and
                receipt.get("preserved_job_artifact_sha256", {}).get(relative) == artifact_hash):
            new_plan = plan_by_digest(work, receipt["new_plan_sha256"])
            new_job = next(j for j in new_plan["jobs"] if j["task_id"] == job["task_id"])
            change = receipt.get("job_budget_changes", {}).get(job["task_id"], {})
            if (change.get("new_seconds") == job_timeout(new_plan, new_job) == job_timeout(plan, job) and
                    change["new_seconds"] > original["timeout_seconds"]):
                return True
        active_digest = receipt["old_plan_sha256"]
    return False


def recover_checkpoint(work: Path, task_id: str, *, excluded_lessons: dict[int, str] | None = None) -> dict:
    """Recover an existing attempt without a model call or rewriting raw records."""
    work = Path(work).resolve()
    plan = load(work / "plan.json")
    matches = [job for job in plan["jobs"] if job["task_id"] == task_id]
    if len(matches) != 1:
        raise ValueError("Recovery task is not one frozen builder job")
    job = matches[0]
    records = [r for r in plan["candidate_records"] if r["task_id"] == task_id]
    exclusions = [{"index": index, "reason": reason} for index, reason in sorted((excluded_lessons or {}).items())]
    with (work / "builder.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = completion(work, job, records)
        if done is not None:
            if done[0].get("excluded_lessons", []) != exclusions:
                raise ValueError("An existing completion cannot be silently recurred or recurated")
            return done[0]
        attempts = sorted((work / "jobs" / task_id).glob("attempt-*/attempt.json"))
        if not attempts:
            raise ValueError("No saved attempt exists to recover")
        attempt = attempts[-1].parent
        original = load(attempt / "attempt.json")
        if original.get("status") not in {"budget_exhausted", "output_invalid", "execution_failed"}:
            raise ValueError("Only an exhausted or delivery-failed saved attempt is recoverable")
        if original.get("task_id") != task_id or original.get("attempt") != attempt.name:
            raise ValueError("Saved attempt identity mismatch")
        original_plan, original_plan_digest, original_plan_basis = resolve_attempt_plan(work, attempt, original)
        audit = load(attempt / "model_audit.json")
        if audit != original.get("model_audit") or not audit.get("model_effort_verified") or audit.get("models") != [MODEL] or audit.get("efforts") != [EFFORT]:
            raise ValueError("Recovered attempt has no matching exact-model audit")
        # Recovery also rechecks the actual retained candidate files.
        for record in records:
            if sha(checked_file(work / "inputs/pool", record["pool_file"])) != record["sha256"]:
                raise ValueError("A retained training source changed before recovery")
        raw, all_entries, origin = collect_output(attempt, records, task_id, execution=original)
        entries = apply_exclusions(all_entries, exclusions)
        if not entries and raw["decision"] == "write":
            raise ValueError("Recovery cannot silently remove every proposed lesson")
        immutable = {name: sha(attempt / name) for name in
                     ("attempt.json", "codex.session.jsonl", "codex.events.jsonl", "model_audit.json",
                      "prompt.txt", "evidence_packet.txt") if (attempt / name).is_file()}
        immutable.update({name: sha(attempt / name) for name in RESULT_FILES if (attempt / name).is_file()})
        receipt = {"schema_version": 1, "kind": "zero_model_checkpoint_recovery", "task_id": task_id,
                   "attempt": attempt.name, "original_execution_status": original["status"],
                   "original_attempt_sha256": immutable["attempt.json"], "preserved_artifact_sha256": immutable,
                   "origin": origin, "excluded_lessons": exclusions, "raw_lesson_count": len(all_entries),
                   "accepted_lesson_count": len(entries), "model_call_performed": False,
                   "original_plan_sha256": original_plan_digest, "original_plan_resolution": original_plan_basis,
                   "plan_sha256_at_recovery": sha(work / "plan.json")}
        if (attempt / "entries.json").exists() or (attempt / "recovery.json").exists():
            raise ValueError("Recovery artifacts already exist without completion; inspect instead of overwriting")
        write_json(attempt / "entries.json", entries)
        receipt["entries_sha256"] = sha(attempt / "entries.json")
        write_json(attempt / "recovery.json", receipt)
        result = {**original, **completed_fields(attempt, raw, entries, records, origin),
                  "execution_status": original["status"], "excluded_lessons": exclusions,
                  "recovery_sha256": sha(attempt / "recovery.json"), "zero_model_recovery": True}
        write_json(attempt.parent / "completion.json", result)
        completion(work, job, records)
        return result


def validate_budget_plan(plan: dict) -> None:
    policy = plan.get("budget_policy")
    if policy is None:  # Legacy fixed-budget manifest.
        for job in plan["jobs"]:
            job_timeout(plan, job)
        return
    fixed = policy.get("timeout_seconds") if policy.get("mode") == "fixed_override" else None
    if policy != budget_policy(fixed):
        raise ValueError("Unknown or modified builder budget policy")
    if plan.get("timeout_seconds_per_job") != fixed:
        raise ValueError("Global budget metadata disagrees with policy")
    for job in plan["jobs"]:
        records = [row for row in plan["candidate_records"] if row["task_id"] == job["task_id"]]
        if any(job.get(key) != value for key, value in job_budget(records, fixed).items()):
            raise ValueError("Per-job budget must be derived from frozen candidate count and bytes")


def without_budget_fields(plan: dict) -> dict:
    result = copy.deepcopy(plan)
    result.pop("budget_policy", None)
    result.pop("timeout_seconds_per_job", None)
    for job in result["jobs"]:
        for key in ("timeout_seconds", "candidate_bytes", "budget_basis"):
            job.pop(key, None)
    return result


def freeze_plan(work: Path, plan: dict, *, accept_code_update: bool = False,
                accept_budget_update: bool = False) -> None:
    """Explicitly amend future budgets/code; never rewrite past attempt budgets."""
    path = work / "plan.json"
    validate_budget_plan(plan)
    if not path.exists():
        write_json(path, plan)
        return
    original = load(path)
    if original == plan:
        return
    if (work / "seed_receipt.json").exists():
        raise ValueError("A finished frozen seed cannot receive code or budget amendments")
    old_noncode = {k: v for k, v in original.items() if k != "source_code_sha256"}
    new_noncode = {k: v for k, v in plan.items() if k != "source_code_sha256"}
    if (without_budget_fields(old_noncode) != without_budget_fields(new_noncode) or
            set(original.get("source_code_sha256", {})) != set(plan.get("source_code_sha256", {}))):
        raise ValueError("Amendment cannot change source data, model, packet policy, or the set of frozen source files")
    code_changed = original["source_code_sha256"] != plan["source_code_sha256"]
    budget_changed = old_noncode != new_noncode
    if code_changed and not accept_code_update:
        raise ValueError("Builder code changed; explicitly accept with --accept-code-update")
    if budget_changed and not accept_budget_update:
        raise ValueError("Builder budget changed; explicitly accept with --accept-budget-update")
    old_digest = sha(path)
    history = work / "plan_history"
    history.mkdir(exist_ok=True)
    archived = history / (old_digest + ".json")
    if archived.exists() and sha(archived) != old_digest:
        raise ValueError("Archived plan was modified")
    if not archived.exists():
        shutil.copyfile(path, archived)
    artifacts = {str(p.relative_to(work)): sha(p) for p in sorted((work / "jobs").rglob("*"))
                 if p.is_file() and p.name in {"attempt.json", "completion.json", "recovery.json", "entries.json", *RESULT_FILES}}
    new_bytes = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode()
    old_jobs = {job["task_id"]: job for job in original["jobs"]}
    changes = {job["task_id"]: {"old_seconds": job_timeout(original, old_jobs[job["task_id"]]),
                                  "new_seconds": job_timeout(plan, job)} for job in plan["jobs"]
               if job_timeout(original, old_jobs[job["task_id"]]) != job_timeout(plan, job)}
    receipt = {"schema_version": 1, "kind": "explicit_builder_budget_amendment" if budget_changed else "explicit_collector_code_amendment", "accepted_at_unix": time.time(),
               "old_plan_sha256": old_digest, "new_plan_sha256": hashlib.sha256(new_bytes).hexdigest(),
               "old_source_code_sha256": original["source_code_sha256"], "new_source_code_sha256": plan["source_code_sha256"],
               "preserved_job_artifact_sha256": artifacts, "data_model_unchanged": True,
               "data_model_budget_unchanged": not budget_changed,
               "budget_update_accepted": budget_changed and accept_budget_update,
               "code_update_accepted": code_changed and accept_code_update,
               "old_budget_policy": original.get("budget_policy", {"mode": "legacy_fixed", "timeout_seconds": original.get("timeout_seconds_per_job")}),
               "new_budget_policy": plan.get("budget_policy", {"mode": "legacy_fixed", "timeout_seconds": plan.get("timeout_seconds_per_job")}),
               "job_budget_changes": changes, "applies_to": "future_attempts_only_existing_attempt_budgets_unchanged",
               "changed_source_files": [name for name in plan["source_code_sha256"] if plan["source_code_sha256"][name] != original["source_code_sha256"][name]],
               "reason": ("User-authorized prospective distillation-budget amendment based on failure-source volume; " if budget_changed else "") +
                         "Explicit collector/prompt code repairs are recorded separately. Source data, model and packet policy remain fixed; original prompts, outputs, timing and failed attempts are preserved."}
    # Append the receipt before changing the active plan, and retain its exact old bytes.
    receipt_path = history / f"amendment-{len(list(history.glob('amendment-*.json'))) + 1:03d}.json"
    write_json(receipt_path, receipt)
    write_json(path, plan)


def run_job(base: Path, work: Path, input_pool: Path, plan: dict, job: dict,
            support_info: dict, auth_file: Path) -> dict:
    job_dir = work / "jobs" / job["task_id"]
    job_dir.mkdir(parents=True, exist_ok=True)
    attempts = sorted(job_dir.glob("attempt-*"))
    attempt = job_dir / f"attempt-{len(attempts) + 1:03d}"
    attempt.mkdir()
    home = work / "private" / job["task_id"] / attempt.name
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    records = [r for r in plan["candidate_records"] if r["task_id"] == job["task_id"]]
    timeout_seconds = job_timeout(plan, job)
    packet, coverage = evidence_packet(records, input_pool, plan["packet_character_budget_per_job"])
    write_json(attempt / "packet_coverage.json", coverage)
    write_json(attempt / "output_schema.json", output_schema())
    (attempt / "evidence_packet.txt").write_text(packet)
    template = Path(__file__).with_name("failure_memory_prompt.txt").read_text()
    prompt = template.format(task_id=job["task_id"], timeout_seconds=timeout_seconds, packet=packet,
                             candidate_count=len(records), candidate_bytes=sum(r["bytes"] for r in records))
    (attempt / "prompt.txt").write_text(prompt)
    config = render_config(CodexSettings(model=MODEL, reasoning_effort=EFFORT)).replace("image_generation = true", "image_generation = false")
    config += "browser_use = false\nbrowser_use_external = false\nbrowser_use_full_cdp_access = false\ncomputer_use = false\n"
    (home / "config.toml").write_text(config)
    name = "tb21-v6-build-" + uuid.uuid4().hex[:16]
    command = docker_argv(base, support_info, input_pool, attempt, home, name)
    result = {"task_id": job["task_id"], "attempt": attempt.name, "status": "running",
              "candidate_count": len(records), "source_paths": job["candidate_paths"],
              "image_id": support_info["image_id"], "timeout_seconds": timeout_seconds,
              "plan_sha256": sha(work / "plan.json"),
              "budget_basis": job.get("budget_basis", {"mode": "legacy_fixed"}),
              "received_test_artifacts": False, "container_name": name}
    write_json(attempt / "attempt.json", result)
    result["started_at_unix"] = time.time()
    started, process, exit_code, timed_out = time.monotonic(), None, None, False
    execution_exception = None
    try:
        shutil.copyfile(auth_file, home / "auth.json")
        (home / "auth.json").chmod(0o600)
        with (attempt / "codex.events.jsonl").open("w") as events, (attempt / "codex.stderr.log").open("w") as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True)
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    except (OSError, subprocess.SubprocessError) as exc:
        execution_exception = type(exc).__name__
    finally:
        try:
            subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
        finally:
            (home / "auth.json").unlink(missing_ok=True)
    # A failed Docker launch can leave empty logs but is still an explicit attempt.
    for name in ("codex.events.jsonl", "codex.stderr.log"):
        (attempt / name).touch(exist_ok=True)
    result.update({"elapsed_seconds": time.monotonic() - started, "exit_code": exit_code, "timed_out": timed_out})
    usage, audit = audit_session(home, attempt)
    result.update({"usage": usage, "model_audit": audit})
    if execution_exception:
        result.update({"status": "infrastructure_failed", "error_type": execution_exception})
    elif timed_out:
        result["status"] = "budget_exhausted"
    elif exit_code != 0:
        logs = (attempt / "codex.stderr.log").read_text() + (attempt / "codex.events.jsonl").read_text()
        result["status"] = "infrastructure_failed" if INFRASTRUCTURE.search(logs) else "execution_failed"
    elif not audit["model_effort_verified"]:
        result["status"] = "model_audit_failed"
    else:
        result["status"] = "completed_process"
    result["execution_status"] = result["status"]
    if result["status"] in {"completed_process", "budget_exhausted"} and audit["model_effort_verified"]:
        try:
            raw, entries, origin = collect_output(attempt, records, job["task_id"], execution=result)
            write_json(attempt / "entries.json", entries)
            result.update(completed_fields(attempt, raw, entries, records, origin))
            result["checkpoint_recovery"] = origin["result_origin"] == "checkpoint" or timed_out
        except (ValueError, KeyError, FileNotFoundError, TypeError) as exc:
            result.update({"status": "budget_exhausted" if timed_out else "output_invalid",
                           "error_type": type(exc).__name__, "validation_error": str(exc)[:1000]})
    write_json(attempt / "attempt.json", result)
    if result["status"] == "complete":
        write_json(job_dir / "completion.json", result)
    return result


def completion(work: Path, job: dict, records: list[dict]) -> tuple[dict, list[dict]] | None:
    path = work / "jobs" / job["task_id"] / "completion.json"
    if not path.exists():
        return None
    result = load(path)
    attempt = path.parent / result["attempt"]
    if not re.fullmatch(r"attempt-\d{3}", result["attempt"]):
        raise ValueError("Invalid completed attempt path")
    if result["status"] != "complete" or not result["model_audit"]["model_effort_verified"]:
        raise ValueError("Saved builder completion is invalid")
    result_file = result.get("result_file", "result.json")
    if result_file not in RESULT_FILES:
        raise ValueError("Invalid saved output filename")
    for name, key in ((result_file, "result_sha256"), ("entries.json", "entries_sha256"),
                      ("evidence_packet.txt", "packet_sha256"), ("codex.session.jsonl", "session_sha256")):
        if (attempt / name).is_symlink() or sha(attempt / name) != result[key]:
            raise ValueError("Completed builder artifact changed")
    entries = validate_output(load(attempt / result_file), records, job["task_id"])
    origin = result.get("result_origin")
    if origin is not None:
        if (origin.get("result_file") != result_file or origin.get("origin_sha256") != result["result_sha256"] or
                origin.get("source_pre_deadline") != (origin["source_mtime_unix"] <= origin["deadline_unix"]) or
                origin.get("source_after_start") != (origin["source_mtime_unix"] >= origin["started_at_unix"])):
            raise ValueError("Saved result origin disagrees with file or timing evidence")
        if result.get("timed_out") and not (origin.get("source_pre_deadline") and origin.get("source_after_start")):
            raise ValueError("Accepted timed-out output was outside the original budget")
    exclusions = result.get("excluded_lessons", [])
    if exclusions and not result.get("recovery_sha256"):
        raise ValueError("Exclusions require an immutable recovery receipt")
    if result.get("recovery_sha256"):
        recovery = attempt / "recovery.json"
        if recovery.is_symlink() or sha(recovery) != result["recovery_sha256"]:
            raise ValueError("Checkpoint recovery receipt changed")
        receipt = load(recovery)
        if (receipt.get("excluded_lessons") != exclusions or receipt.get("task_id") != job["task_id"] or
                receipt.get("attempt") != result["attempt"] or receipt.get("model_call_performed") is not False or
                receipt.get("origin") != result.get("result_origin") or receipt.get("entries_sha256") != result["entries_sha256"]):
            raise ValueError("Checkpoint recovery receipt disagrees with completion")
        for filename, digest in receipt["preserved_artifact_sha256"].items():
            if PurePosixPath(filename).name != filename or (attempt / filename).is_symlink() or sha(attempt / filename) != digest:
                raise ValueError("Original recovered attempt artifact changed")
        original = load(attempt / "attempt.json")
        if receipt.get("original_attempt_sha256") != sha(attempt / "attempt.json") or receipt.get("original_execution_status") != original["status"] or result.get("execution_status") != original["status"]:
            raise ValueError("Original execution outcome was not preserved")
        if original.get("timed_out") and not receipt["origin"].get("source_pre_deadline"):
            raise ValueError("Timed-out recovery lacks in-budget checkpoint evidence")
        if result.get("elapsed_seconds") != original.get("elapsed_seconds") or result.get("timed_out") != original.get("timed_out") or result.get("model_audit") != original.get("model_audit"):
            raise ValueError("Recovery changed original timing or model provenance")
        if receipt.get("raw_lesson_count") != len(entries):
            raise ValueError("Recovery receipt's raw lesson count disagrees")
        if receipt.get("original_plan_sha256"):
            original_plan = plan_by_digest(work, receipt["original_plan_sha256"])
            if not attempt_matches_plan(original, original_plan):
                raise ValueError("Recovery no longer matches its original plan and budget")
    entries = apply_exclusions(entries, exclusions)
    if result.get("recovery_sha256") and receipt.get("accepted_lesson_count") != len(entries):
        raise ValueError("Recovery receipt's accepted lesson count disagrees")
    if entries != load(attempt / "entries.json"):
        raise ValueError("Saved normalized entries disagree with raw builder output")
    return result, entries


def status(work: Path) -> dict:
    work = Path(work)
    if not (work / "plan.json").exists():
        return {"status": "not_started", "work_dir": str(work)}
    plan = load(work / "plan.json")
    jobs, entries, cited, usage = [], 0, set(), Counter()
    for job in plan["jobs"]:
        path = work / "jobs" / job["task_id"]
        done = path / "completion.json"
        attempts = sorted(path.glob("attempt-*/attempt.json"))
        current = load(done) if done.exists() else (load(attempts[-1]) if attempts else {"status": "pending"})
        jobs.append({"task_id": job["task_id"], "status": current["status"], "attempt_count": len(attempts),
                     "planned_timeout_seconds": job_timeout(plan, job),
                     "attempt_timeout_seconds": current.get("timeout_seconds"),
                     "candidate_bytes": job.get("candidate_bytes", sum(r["bytes"] for r in plan["candidate_records"] if r["task_id"] == job["task_id"])),
                     "entry_count": current.get("entry_count", 0)})
        if done.exists():
            entries += current["entry_count"]
            cited.update(current["cited_sources"])
        for attempt in attempts:
            u = load(attempt).get("usage", {})
            for key, value in u.items():
                if isinstance(value, (int, float)):
                    usage[key] += value
    failed = any(job["status"] not in {"complete", "pending", "running"} for job in jobs)
    return {"status": "frozen" if (work / "seed_receipt.json").exists() else
            ("paused" if (work / "pause_requested").exists() else ("needs_review" if failed else "building")),
            "builder_model": MODEL, "builder_reasoning_effort": EFFORT,
            "budget_policy": plan.get("budget_policy", {"mode": "legacy_fixed", "timeout_seconds": plan.get("timeout_seconds_per_job")}),
            "total_jobs": len(jobs), "complete_jobs": sum(j["status"] == "complete" for j in jobs),
            "candidate_count": plan["candidate_count"], "cited_candidate_count": len(cited),
            "uncited_candidate_count": plan["candidate_count"] - len(cited), "entry_count": entries,
            "usage_including_failed_attempts": dict(usage), "jobs": jobs}


def build(base: Path, work: Path, seed: Path, *, auth_file: Path, image: str,
          timeout_seconds: int | None, packet_chars: int, max_jobs: int | None,
          retry_infrastructure: bool = False, accept_code_update: bool = False,
          accept_budget_update: bool = False, retry_budget_exhausted: bool = False) -> dict:
    from experiment.shared.memory.seed import create_seed, validate_seed
    work, seed = Path(work).resolve(), Path(seed).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if work == seed or seed.is_relative_to(work / "inputs"):
        raise ValueError("Seed destination must not overwrite builder inputs")
    if max_jobs is not None and max_jobs < 1:
        raise ValueError("max-jobs must be positive")
    with (work / "builder.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = build_plan(base, timeout_seconds=timeout_seconds, packet_chars=packet_chars)
        freeze_plan(work, plan, accept_code_update=accept_code_update, accept_budget_update=accept_budget_update)
        pool = base / "prepared/pool"
        prepared = prepare_inputs(work, plan, pool)
        supported = support(base, image)
        if (work / "support.json").exists() and load(work / "support.json") != supported:
            raise ValueError("Pinned builder runtime changed")
        write_json(work / "support.json", supported)
        count, all_entries, all_completions = 0, [], []
        for job in plan["jobs"]:
            records = [r for r in plan["candidate_records"] if r["task_id"] == job["task_id"]]
            done = completion(work, job, records)
            if done is None:
                if (work / "pause_requested").exists():
                    return status(work)
                if max_jobs is not None and count >= max_jobs:
                    return status(work)
                attempts = sorted((work / "jobs" / job["task_id"]).glob("attempt-*/attempt.json"))
                if attempts:
                    previous = load(attempts[-1])
                    infrastructure_retry = retry_infrastructure and previous["status"] == "infrastructure_failed"
                    budget_retry = retry_budget_exhausted and budget_retry_authorized(work, attempts[-1].parent, previous, plan, job)
                    if not infrastructure_retry and not budget_retry:
                        raise RuntimeError(f"{job['task_id']}: preserved {previous['status']} attempt; retries require an explicit infrastructure flag or an accepted strictly larger budget plus --retry-budget-exhausted")
                print(json.dumps({"event": "builder_job_started", "task_id": job["task_id"],
                                  "candidate_count": len(records), "timeout_seconds": job_timeout(plan, job)}, ensure_ascii=False), flush=True)
                result = run_job(base, work, prepared, plan, job, supported, auth_file)
                count += 1
                print(json.dumps({"event": "builder_job_finished", "task_id": job["task_id"],
                                  "status": result["status"], "entry_count": result.get("entry_count", 0),
                                  "elapsed_seconds": round(result["elapsed_seconds"], 2)}, ensure_ascii=False), flush=True)
                write_json(work / "status.json", status(work))
                if result["status"] != "complete":
                    raise RuntimeError(f"Builder stopped at {job['task_id']}: {result['status']}; see preserved attempt")
                done = completion(work, job, records)
            all_completions.append(done[0])
            all_entries.extend(done[1])
        if (work / "pause_requested").exists():
            return status(work)
        if not all_entries:
            raise RuntimeError("All builder jobs returned no_update; V6 cannot start with an empty seed")
        provenance = {"builder_model": MODEL, "builder_reasoning_effort": EFFORT,
                      "source_selection": "failed_training_trajectories", "model_audit_verified": True,
                      "builder_plan_sha256": sha(work / "plan.json"), "builder_support_sha256": sha(work / "support.json"),
                      "builder_job_count": len(all_completions), "candidate_count": plan["candidate_count"],
                      "coverage": status(work),
                      "plan_history_sha256": {p.name: sha(p) for p in sorted((work / "plan_history").glob("*.json"))},
                      "completion_sha256": {j["task_id"]: sha(work / "jobs" / j["task_id"] / "completion.json") for j in plan["jobs"]}}
        if seed.exists():
            # Seed creation is write-once; this branch only verifies an already-finished build.
            if not (work / "seed_receipt.json").exists():
                raise ValueError("Seed already exists without this builder's receipt")
            validated = validate_seed(seed, pool=pool, training_task_ids=plan["training_task_ids"])
            receipt = load(work / "seed_receipt.json")
            if receipt["seed_manifest_sha256"] != sha(seed / "manifest.json"):
                raise ValueError("Frozen seed changed")
        else:
            created = create_seed(seed, all_entries, pool=pool, training_task_ids=plan["training_task_ids"], provenance=provenance)
            validated = validate_seed(seed, pool=pool, training_task_ids=plan["training_task_ids"])
            receipt = {"seed_dir": str(seed), "seed_manifest_sha256": sha(seed / "manifest.json"),
                       "entry_count": len(all_entries), "builder_plan_sha256": sha(work / "plan.json"),
                       "validated": validated}
            write_json(work / "seed_receipt.json", receipt)
        write_json(work / "status.json", status(work))
        return status(work)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "build", "status"))
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--work-dir", type=Path, default=BASE / "runs/v6_memory_build_20261005")
    parser.add_argument("--seed-dir", type=Path, default=BASE / "prepared/memory_seeds/v6_luna_xhigh_20261005")
    parser.add_argument("--auth-file", type=Path, default=Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser())
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--timeout-seconds", type=int, default=None,
                        help="Explicit fixed override (60..3600 sec); default derives each job budget from failed-source count and bytes")
    parser.add_argument("--packet-chars", type=int, default=90000)
    parser.add_argument("--max-jobs", type=int)
    parser.add_argument("--retry-infrastructure", action="store_true")
    parser.add_argument("--accept-code-update", action="store_true", help="Archive old protocol and explicitly accept source-code-only collector repairs")
    parser.add_argument("--accept-budget-update", action="store_true", help="Archive old protocol and explicitly accept prospective per-job distillation budget changes")
    parser.add_argument("--retry-budget-exhausted", action="store_true", help="Retry a preserved budget failure only after an accepted strictly larger budget")
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = build_plan(args.base, timeout_seconds=args.timeout_seconds, packet_chars=args.packet_chars)
        # Keep console output small; full plan is frozen by build, not silently written by plan.
        result = {key: value for key, value in result.items() if key not in {"candidate_records", "task_card_sha256", "source_code_sha256"}}
        result["jobs"] = [{k: v for k, v in j.items() if k != "candidate_paths"} for j in result["jobs"]]
    elif args.command == "status":
        result = status(args.work_dir)
    else:
        result = build(args.base, args.work_dir, args.seed_dir, auth_file=args.auth_file.expanduser().resolve(),
                       image=args.image, timeout_seconds=args.timeout_seconds, packet_chars=args.packet_chars,
                       max_jobs=args.max_jobs, retry_infrastructure=args.retry_infrastructure,
                       accept_code_update=args.accept_code_update, accept_budget_update=args.accept_budget_update,
                       retry_budget_exhausted=args.retry_budget_exhausted)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
