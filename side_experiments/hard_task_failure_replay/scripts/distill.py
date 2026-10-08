"""Summarize each task's failed attempts with gpt-5.6-luna / xhigh (the weaker builder).

One job per task, run in an isolated container exactly like the V6 builder:
read-only failed transcripts and instruction, no tests, no solution, no
successful attempts. The V6 packet, budget rule, container image, CA bundle and
session audit are reused from the main package; only the prompt and the output
schema are specific to this side experiment. Every attempt is kept; a finished
memory is never overwritten.

Usage: python -m side_experiments.hard_task_failure_replay.scripts.distill [task ...]
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from experiment.benchmarks.terminal_bench_2_1.scripts import build_failure_memory as v6
from experiment.shared.codex_backend import CodexSettings, render_config
from experiment.shared.memory.tools import _validate

from .common import CORPUS, MANIFESTS, PROMPTS, RUNS, TB, config, load, sha, write_json

_text = {"type": "string", "minLength": 1, "maxLength": 3000}
_source = {"type": "object", "additionalProperties": False, "required": ["path", "line_start", "line_end"],
           "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 400},
                          "line_start": {"type": "integer", "minimum": 1},
                          "line_end": {"type": "integer", "minimum": 1}}}
_lesson = {"type": "object", "additionalProperties": False,
           "required": ["title", "observation", "likely_cause", "confidence", "next_attempt_check", "sources"],
           "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 200},
                          "observation": _text, "likely_cause": _text,
                          "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                          "next_attempt_check": _text,
                          "sources": {"type": "array", "items": _source, "minItems": 1, "maxItems": 10}}}


def output_schema(max_lessons: int) -> dict:
    lessons = {"type": "array", "items": _lesson, "minItems": 1, "maxItems": max_lessons}
    properties = {"overview": _text, "common_approach": _text, "lessons": lessons,
                  "unexplained": {"type": "string", "maxLength": 3000},
                  "reviewed_sources": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 400},
                                       "maxItems": 200}}
    return {"type": "object", "additionalProperties": False, "required": sorted(properties), "properties": properties}


def validate(value: dict, records: list[dict], max_lessons: int) -> dict:
    _validate(value, output_schema(max_lessons), "summary")
    by_path = {r["path"]: r for r in records}
    for lesson in value["lessons"]:
        for pointer in lesson["sources"]:
            record = by_path.get(pointer["path"])
            if record is None:
                raise ValueError(f"lesson cites a file outside this task's failed attempts: {pointer['path']}")
            if not 1 <= pointer["line_start"] <= pointer["line_end"] <= record["line_count"]:
                raise ValueError(f"invalid line range for {pointer['path']}")
    # reviewed_sources is the builder's self-report. Reading the instruction or the output
    # schema is expected, so extra entries are recorded by reviewed_counts(), never fatal.
    return value


def citable(task_id: str, records: list[dict]) -> list[dict]:
    """Lessons may cite this task's failed attempts and its public instruction (prompt step 3)."""
    instruction = CORPUS / task_id / "instruction.md"
    return records + [{"path": "/experience/instruction.md", "line_count": len(instruction.read_text().splitlines())}]


def reviewed_counts(value: dict, records: list[dict]) -> dict:
    paths = {r["path"] for r in records}
    listed = value["reviewed_sources"]
    return {"reviewed_attempt_files": sum(p in paths for p in listed),
            "reviewed_other_paths": sorted(p for p in listed if p not in paths)}


def render_lessons(task_id: str, value: dict, manifest: dict, builder: dict) -> str:
    models = ", ".join(f"{m} x{n}" for m, n in manifest["models"].items())
    out = [f"# What earlier failed attempts suggest about {task_id}", "",
           f"Written by {builder['model']} ({builder['reasoning_effort']}) after reading {manifest['attempt_count']} "
           f"failed official attempts at this task ({models}). Every attempt scored 0. The summarizer could not "
           "see hidden tests or grading results, so the causes below are hypotheses unless the confidence is "
           "high, and none of these lessons has been verified on this task.", "",
           "## Overview", value["overview"], "", "## What the failed attempts usually did", value["common_approach"],
           "", "## Lessons"]
    for number, lesson in enumerate(value["lessons"], 1):
        sources = "; ".join(f"`{s['path']}:{s['line_start']}-{s['line_end']}`" for s in lesson["sources"])
        out += ["", f"### {number}. {lesson['title']} (confidence: {lesson['confidence']})",
                f"- Observed: {lesson['observation']}", f"- Likely cause: {lesson['likely_cause']}",
                f"- Check or try next time: {lesson['next_attempt_check']}", f"- Sources: {sources}"]
    if value["unexplained"].strip():
        out += ["", "## Not explained by the transcripts", value["unexplained"]]
    return "\n".join(out) + "\n"


def docker_argv(support: dict, root: Path, attempt: Path, home: Path, name: str) -> list[str]:
    args = ["docker", "run", "--name", name, "--interactive", "--init", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--workdir", "/work",
            "--env", "CODEX_HOME=/opt/builder-home", "--env", "HOME=/opt/builder-home",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PATH=/opt/codex:/opt/builder-python/bin:/usr/local/bin:/usr/bin:/bin",
            "--env", "SSL_CERT_FILE=/opt/builder-ca.crt", "--env", "CODEX_CA_CERTIFICATE=/opt/builder-ca.crt"]
    mounts = [(root / "experience/failed_attempts", "/experience/failed_attempts", True),
              (root / "instruction.md", "/experience/instruction.md", True),
              (attempt, "/work", False), (home, "/opt/builder-home", False),
              (TB / "runtime/bin", "/opt/codex", True), (Path(support["python_root"]), "/opt/builder-python", True),
              (TB / "prepared/transport/ca-certificates.crt", "/opt/builder-ca.crt", True)]
    for source, target, readonly in mounts:
        args += ["--mount", f"type=bind,src={Path(source).resolve()},dst={target}" + (",readonly" if readonly else "")]
    return args + [support["image_id"], "/opt/codex/codex", "exec", "--json", "--skip-git-repo-check",
                   "--dangerously-bypass-approvals-and-sandbox", "--cd", "/work",
                   "--output-schema", "/work/output_schema.json", "--output-last-message", "/work/result.json", "-"]


def collect(attempt: Path, records: list[dict], max_lessons: int, started: float, deadline: float) -> tuple[dict, str]:
    errors = []
    for name in ("result.json", "summary.json"):
        path = attempt / name
        if not path.is_file():
            errors.append(f"{name}: missing")
            continue
        if name == "summary.json" and not started <= path.stat().st_mtime <= deadline:
            errors.append(f"{name}: written outside the attempt window")
            continue
        try:
            return validate(load(path), records, max_lessons), name
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {str(exc)[:300]}")
    raise ValueError("; ".join(errors))


def run_job(task_id: str, support: dict, auth_file: Path) -> dict:
    settings = config()["builder"]
    root = CORPUS / task_id
    memory = root / "experience/memory"
    if (memory / "lessons.md").exists():
        return {"task_id": task_id, "status": "complete", "reused": True}
    manifest = load(MANIFESTS / f"corpus_{task_id}.json")
    records = [{**r, "pool_file": r["relative"], "status": "available"} for r in manifest["records"]]
    job_dir = RUNS / "distill" / task_id
    job_dir.mkdir(parents=True, exist_ok=True)
    attempt = job_dir / f"attempt-{len(list(job_dir.glob('attempt-*'))) + 1:03d}"
    attempt.mkdir()
    home = RUNS / "distill/private" / task_id / attempt.name
    home.mkdir(parents=True, mode=0o700)
    budget = v6.job_budget(records)
    packet, coverage = v6.evidence_packet(records, root / "experience", settings["packet_chars"])
    prompt = (PROMPTS / "distill_prompt.txt").read_text().format(
        task_id=task_id, attempt_count=manifest["attempt_count"],
        models=", ".join(f"{m} x{n}" for m, n in manifest["models"].items()),
        timeout_seconds=budget["timeout_seconds"], max_lessons=settings["max_lessons"], packet=packet)
    write_json(attempt / "output_schema.json", output_schema(settings["max_lessons"]))
    write_json(attempt / "packet_coverage.json", coverage)
    (attempt / "prompt.txt").write_text(prompt)
    codex_config = render_config(CodexSettings(model=v6.MODEL, reasoning_effort=v6.EFFORT)).replace(
        "image_generation = true", "image_generation = false")
    codex_config += "browser_use = false\nbrowser_use_external = false\nbrowser_use_full_cdp_access = false\ncomputer_use = false\n"
    (home / "config.toml").write_text(codex_config)
    name = "side-replay-distill-" + uuid.uuid4().hex[:12]
    result = {"task_id": task_id, "attempt": attempt.name, "builder_model": v6.MODEL,
              "builder_reasoning_effort": v6.EFFORT, "timeout_seconds": budget["timeout_seconds"],
              "budget_basis": budget["budget_basis"], "image_id": support["image_id"],
              "corpus_manifest_sha256": sha(MANIFESTS / f"corpus_{task_id}.json"),
              "prompt_sha256": sha(attempt / "prompt.txt"), "container_name": name,
              "received_tests_or_solutions": False, "received_successful_attempts": False}
    write_json(attempt / "attempt.json", {**result, "status": "running"})
    started, timed_out, exit_code = time.time(), False, None
    try:
        shutil.copyfile(auth_file, home / "auth.json")
        (home / "auth.json").chmod(0o600)
        with (attempt / "codex.events.jsonl").open("w") as events, (attempt / "codex.stderr.log").open("w") as errors:
            process = subprocess.Popen(docker_argv(support, root, attempt, home, name), stdin=subprocess.PIPE,
                                       stdout=events, stderr=errors, text=True)
            try:
                process.communicate(input=prompt, timeout=budget["timeout_seconds"])
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        (home / "auth.json").unlink(missing_ok=True)
    usage, audit = v6.audit_session(home, attempt)
    result.update({"elapsed_seconds": time.time() - started, "exit_code": exit_code, "timed_out": timed_out,
                   "usage": usage, "model_audit": audit})
    try:
        if not audit["model_effort_verified"]:
            raise ValueError("builder model or effort could not be verified")
        value, origin = collect(attempt, citable(task_id, records), settings["max_lessons"], started,
                                started + budget["timeout_seconds"])
    except ValueError as exc:
        result.update(status="failed", error=str(exc)[:1000])
        write_json(attempt / "attempt.json", result)
        return result
    return finish(task_id, attempt, result, value, origin, records, manifest, settings)


def finish(task_id, attempt, result, value, origin, records, manifest, settings) -> dict:
    memory = CORPUS / task_id / "experience/memory"
    memory.mkdir(parents=True, exist_ok=True)
    write_json(memory / "lessons.json", value)
    (memory / "lessons.md").write_text(render_lessons(task_id, value, manifest, settings))
    result.update(status="complete", result_origin=origin, lesson_count=len(value["lessons"]),
                  lessons_sha256=sha(memory / "lessons.md"), **reviewed_counts(value, records))
    write_json(attempt / "attempt.json", result)
    write_json(MANIFESTS / f"memory_{task_id}.json", result)
    return result


def recollect(task_id: str) -> dict:
    """Re-validate a finished attempt's unchanged output after a collector fix. No model call."""
    settings = config()["builder"]
    attempt = sorted((RUNS / "distill" / task_id).glob("attempt-*"))[-1]
    result = load(attempt / "attempt.json")
    if result.get("status") != "failed" or not result.get("model_audit", {}).get("model_effort_verified"):
        raise SystemExit(f"{task_id}: only a model-verified attempt that failed collection can be re-collected")
    manifest = load(MANIFESTS / f"corpus_{task_id}.json")
    if sha(MANIFESTS / f"corpus_{task_id}.json") != result["corpus_manifest_sha256"]:
        raise SystemExit(f"{task_id}: corpus changed since the attempt")
    records = [{**r, "pool_file": r["relative"], "status": "available"} for r in manifest["records"]]
    started = (attempt / "prompt.txt").stat().st_mtime  # written just before the container started
    value, origin = collect(attempt, citable(task_id, records), settings["max_lessons"], started,
                            started + result["timeout_seconds"])
    result["first_collection"] = {"status": "failed", "error": result.pop("error", None)}
    result["recollected_after_validator_fix"] = {"at_unix": time.time(), "model_called": False,
        "reason": ("collector fix: reviewed_sources is a non-fatal self-report, and lessons may cite the public "
                   "instruction; citations to failed attempts are still checked line by line")}
    return finish(task_id, attempt, result, value, origin, records, manifest, settings)


def main() -> int:
    selection = load(MANIFESTS / "selection.json")
    if sys.argv[1:2] == ["--recollect"]:
        for task in sys.argv[2:]:
            result = recollect(task)
            print(json.dumps({k: result.get(k) for k in ("task_id", "status", "lesson_count", "reviewed_attempt_files",
                                                         "reviewed_other_paths")}, ensure_ascii=False))
        return 0
    tasks = sys.argv[1:] or selection["chosen_task_ids"]
    auth_file = Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser().resolve()
    support = v6.support(TB, v6.DEFAULT_IMAGE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        for result in pool.map(lambda task: run_job(task, support, auth_file), tasks):
            print(json.dumps({k: result.get(k) for k in ("task_id", "status", "lesson_count", "elapsed_seconds",
                                                         "timed_out", "error", "reused")}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
