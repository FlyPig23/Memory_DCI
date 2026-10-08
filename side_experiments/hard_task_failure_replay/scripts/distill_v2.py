"""Failure summaries (prompt v3), one per (builder model, task).

v3 = v2 minus every statement about how grading works (see runs/invalid/distill_v2_grading_note).

Same inputs and isolation as distill.py (this task's failed attempts and public
instruction only), but a revised prompt (prompts/distill_prompt_v3.txt) and output
structure: requirements, approach families with an approach-vs-execution verdict,
shared assumptions, verification gaps, lessons with an instruction-based check, reusable
details and a short decision guide. Every builder gets the identical prompt, packet,
schema and budget; only the model differs. All builders run at medium effort.

Outputs: corpus/<task>/memories/<builder>/lessons.{json,md}; attempts under
runs/distill_v3/<builder>/<task>/attempt-NNN; provenance in manifests/memory_v3_<builder>_<task>.json.
Usage: python -m side_experiments.hard_task_failure_replay.scripts.distill_v2 --builders luna56 sonnet55 [--tasks ...]
       python -m side_experiments.hard_task_failure_replay.scripts.distill_v2 --recollect BUILDER TASK [--accept-fallback]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path

from experiment.benchmarks.terminal_bench_2_1.scripts import build_failure_memory as v6
from experiment.shared.memory.tools import _validate

from . import builders
from .common import CORPUS, MANIFESTS, PROMPTS, RUNS, TB, config, load, sha, write_json

MAX_LESSONS, PACKET_CHARS = 8, 90000
PROMPT_VERSION = "v3"  # default; a builder may pin another version in config.json ("prompt_version")


def prompt_version(builder: dict) -> str:
    """v4 = v3 plus one rule: never recommend passing a check by changing the environment it runs in."""
    return builder.get("prompt_version", PROMPT_VERSION)


def _s(limit: int, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": limit}


def _list(items: dict, low: int, high: int) -> dict:
    return {"type": "array", "items": items, "minItems": low, "maxItems": high}


def _object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": sorted(properties), "properties": properties}


SOURCE = _object({"path": _s(400), "line_start": {"type": "integer", "minimum": 1},
                  "line_end": {"type": "integer", "minimum": 1}})
FAILURE_MODES = ["spec_misread", "hidden_requirement", "environment_or_runtime", "implementation_bug",
                 "verification_gap", "deliverable_format", "time_budget", "infrastructure", "unknown"]


def output_schema() -> dict:
    return _object({
        "task_summary": _s(700),
        "decision_guide": _object({"first_steps": _list(_s(300), 1, 5), "avoid": _list(_s(300), 1, 6),
                                   "before_finishing": _list(_s(300), 1, 6)}),
        "requirements": _list(_object({"requirement": _s(300), "how_attempts_handled_it": _s(500),
                                       "risk": {"type": "string", "enum": ["high", "medium", "low"]}}), 1, 10),
        "approach_families": _list(_object({
            "name": _s(120), "attempt_count": {"type": "integer", "minimum": 0}, "furthest_progress": _s(500),
            "where_it_broke": _s(600),
            "verdict": {"type": "string", "enum": ["approach_likely_wrong", "execution_likely_wrong", "unclear"]},
            "evidence": _s(500), "sources": _list(SOURCE, 1, 6)}), 1, 6),
        "shared_assumptions": _list(_object({"assumption": _s(400), "why_suspect": _s(500),
                                             "sources": _list(SOURCE, 0, 4)}), 0, 6),
        "verification_gaps": _list(_object({"check_used": _s(400), "what_it_misses": _s(500)}), 0, 6),
        "lessons": _list(_object({
            "title": _s(160), "failure_mode": {"type": "string", "enum": FAILURE_MODES},
            "attempts_affected": {"type": "integer", "minimum": 0}, "observed_failure": _s(700),
            "cause_hypothesis": _s(600),
            "cause_confidence": {"type": "string", "enum": ["observed", "hypothesis", "unknown"]},
            "what_to_do_instead": _s(700), "check_that_would_catch_it": _s(500),
            "sources": _list(SOURCE, 1, 8)}), 1, MAX_LESSONS),
        "reusable_details": _list(_object({"what": _s(200), "detail": _s(900), "caveat": _s(300, 0),
                                           "sources": _list(SOURCE, 1, 4)}), 0, 5),
        "unexplained": _s(1500, 0),
        "reviewed_sources": _list(_s(400), 0, 200),
    })


def citable(task_id: str, records: list[dict]) -> dict[str, int]:
    instruction = CORPUS / task_id / "instruction.md"
    return {**{r["path"]: r["line_count"] for r in records},
            "/experience/instruction.md": len(instruction.read_text().splitlines())}


def canonical_path(path: str, lines: dict[str, int]) -> str:
    """Complete a citation that dropped the leading '/', '/experience/' or '/experience/failed_attempts/' prefix,
    but only when it names exactly one citable file; line ranges are still checked strictly."""
    if path in lines:
        return path
    bare = path.lstrip("/")
    bare = bare[len("experience/"):] if bare.startswith("experience/") else bare
    bare = bare[len("failed_attempts/"):] if bare.startswith("failed_attempts/") else bare
    if bare == "instruction.md" and "/experience/instruction.md" in lines:
        return "/experience/instruction.md"
    matches = [p for p in lines if p.endswith("/" + bare) and p.startswith("/experience/failed_attempts/")]
    return matches[0] if len(matches) == 1 and bare.count("/") >= 2 else path


def validate(value: dict, lines: dict[str, int]) -> dict:
    _validate(value, output_schema(), "summary")
    pointers = [s for section in ("approach_families", "shared_assumptions", "lessons", "reusable_details")
                for item in value[section] for s in item["sources"]]
    for pointer in pointers:
        pointer["path"] = canonical_path(pointer["path"], lines)
        count = lines.get(pointer["path"])
        if count is None:
            raise ValueError(f"cites a file outside this task's failed attempts and instruction: {pointer['path']}")
        if not 1 <= pointer["line_start"] <= pointer["line_end"] <= count:
            raise ValueError(f"invalid line range for {pointer['path']}")
    return value


def _ref(sources: list[dict]) -> str:
    def short(path: str) -> str:
        parts = path.split("/")
        return "instruction" if path.endswith("instruction.md") else f"{parts[-3]}/{parts[-2][:8]}"
    return "; ".join(f"{short(s['path'])} L{s['line_start']}-{s['line_end']}" for s in sources)


def render(task_id: str, value: dict, manifest: dict, builder: dict) -> str:
    models = ", ".join(f"{m} x{n}" for m, n in manifest["models"].items())
    guide = value["decision_guide"]
    out = [f"# What {manifest['attempt_count']} failed attempts at {task_id} suggest",
           "", f"Failure analysis written by {builder['model']} ({builder['effort']} effort) from {manifest['attempt_count']} "
           f"officially graded attempts by other agents ({models}). Every attempt scored 0. The analyst never saw "
           "the hidden tests or grading output, so causes are hypotheses unless marked observed. Nothing here has "
           "been verified on this task.", "", "## Read this first"]
    out += [f"- First: {x}" for x in guide["first_steps"]]
    out += [f"- Avoid: {x}" for x in guide["avoid"]]
    out += [f"- Before finishing: {x}" for x in guide["before_finishing"]]
    out += ["", "## The task as the analyst reads it", value["task_summary"], "", "## Requirements to watch"]
    out += [f"- [{r['risk']}] {r['requirement']} Attempts: {r['how_attempts_handled_it']}" for r in value["requirements"]]
    out += ["", "## How the failed attempts went"]
    for family in value["approach_families"]:
        out += [f"- **{family['name']}** ({family['attempt_count']} attempts; verdict: {family['verdict'].replace('_', ' ')}). "
                f"Furthest: {family['furthest_progress']} Broke: {family['where_it_broke']} "
                f"Evidence: {family['evidence']} [{_ref(family['sources'])}]"]
    if value["shared_assumptions"]:
        out += ["", "## What (nearly) every attempt assumed"]
        out += [f"- {a['assumption']} Why suspect: {a['why_suspect']}" + (f" [{_ref(a['sources'])}]" if a["sources"] else "")
                for a in value["shared_assumptions"]]
    if value["verification_gaps"]:
        out += ["", "## Checks that could not detect the problem"]
        out += [f"- {g['check_used']} Misses: {g['what_it_misses']}" for g in value["verification_gaps"]]
    out += ["", "## Lessons"]
    for number, lesson in enumerate(value["lessons"], 1):
        out += ["", f"### {number}. {lesson['title']}",
                f"({lesson['failure_mode'].replace('_', ' ')}; {lesson['attempts_affected']} attempts; cause {lesson['cause_confidence']})",
                f"- Observed: {lesson['observed_failure']}", f"- Likely cause: {lesson['cause_hypothesis']}",
                f"- Do instead: {lesson['what_to_do_instead']}", f"- Check: {lesson['check_that_would_catch_it']}",
                f"- Sources: {_ref(lesson['sources'])}"]
    if value["reusable_details"]:
        out += ["", "## Reusable details from the failed attempts"]
        out += [f"- **{d['what']}**: {d['detail']}" + (f" Caveat: {d['caveat']}" if d["caveat"] else "") +
                f" [{_ref(d['sources'])}]" for d in value["reusable_details"]]
    if value["unexplained"].strip():
        out += ["", "## Still unexplained", value["unexplained"]]
    return "\n".join(out) + "\n"


def _inputs(task_id: str) -> tuple[dict, list[dict]]:
    manifest = load(MANIFESTS / f"corpus_{task_id}.json")
    return manifest, [{**r, "pool_file": r["relative"], "status": "available"} for r in manifest["records"]]


def collect(attempt: Path, lines: dict[str, int], started: float, deadline: float) -> tuple[dict, str]:
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
            return validate(json.loads(path.read_text()), lines), name
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {str(exc)[:300]}")
    raise ValueError("; ".join(errors))


def finish(task_id: str, builder: dict, attempt: Path, result: dict, value: dict, origin: str, manifest: dict) -> dict:
    out = CORPUS / task_id / "memories" / builder["id"]
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "lessons.json", value)
    (out / "lessons.md").write_text(render(task_id, value, manifest, builder))
    result.update(status="complete", result_origin=origin, lesson_count=len(value["lessons"]),
                  lessons_sha256=sha(out / "lessons.md"), lessons_chars=len((out / "lessons.md").read_text()))
    write_json(attempt / "attempt.json", result)
    write_json(MANIFESTS / f"memory_{prompt_version(builder)}_{builder['id']}_{task_id}.json", result)
    return result


def run_job(task_id: str, builder_id: str, support: dict) -> dict:
    builder = builders.spec(builder_id)
    if (CORPUS / task_id / "memories" / builder_id / "lessons.md").exists():
        return {"task_id": task_id, "builder": builder_id, "status": "complete", "reused": True}
    manifest, records = _inputs(task_id)
    job = RUNS / f"distill_{prompt_version(builder)}" / builder_id / task_id
    job.mkdir(parents=True, exist_ok=True)
    attempt = job / f"attempt-{len(list(job.glob('attempt-*'))) + 1:03d}"
    attempt.mkdir()
    home = RUNS / f"distill_{prompt_version(builder)}/private" / builder_id / task_id / attempt.name
    home.mkdir(parents=True, mode=0o700)
    budget = v6.job_budget(records)
    packet, coverage = v6.evidence_packet(records, CORPUS / task_id / "experience", PACKET_CHARS)
    prompt = (PROMPTS / f"distill_prompt_{prompt_version(builder)}.txt").read_text().format(
        task_id=task_id, attempt_count=manifest["attempt_count"],
        models=", ".join(f"{m} x{n}" for m, n in manifest["models"].items()),
        timeout_seconds=budget["timeout_seconds"], max_lessons=MAX_LESSONS, packet=packet)
    schema = attempt / "output_schema.json"
    write_json(schema, output_schema())
    write_json(attempt / "packet_coverage.json", coverage)
    (attempt / "prompt.txt").write_text(prompt)
    result = {"task_id": task_id, "builder": builder_id, "engine": builder["engine"], "model": builder["model"],
              "effort": builder["effort"], "prompt_version": prompt_version(builder), "attempt": attempt.name,
              "timeout_seconds": budget["timeout_seconds"], "budget_basis": budget["budget_basis"],
              "image_id": support["image_id"], "corpus_manifest_sha256": sha(MANIFESTS / f"corpus_{task_id}.json"),
              "prompt_sha256": sha(attempt / "prompt.txt"), "received_tests_or_solutions": False,
              "received_successful_attempts": False}
    write_json(attempt / "attempt.json", {**result, "status": "running"})
    mounts = [(CORPUS / task_id / "experience/failed_attempts", "/experience/failed_attempts", True),
              (CORPUS / task_id / "instruction.md", "/experience/instruction.md", True)]
    outcome = builders.run(builder, support, mounts, attempt, home, prompt, budget["timeout_seconds"], schema=schema)
    result.update(outcome)
    try:
        if not outcome["model_audit"]["model_effort_verified"]:
            raise ValueError("builder model could not be verified")
        value, origin = collect(attempt, citable(task_id, records), outcome["started_at_unix"],
                                outcome["started_at_unix"] + budget["timeout_seconds"])
    except ValueError as exc:
        result.update(status="failed", error=str(exc)[:1000])
        write_json(attempt / "attempt.json", result)
        return result
    return finish(task_id, builder, attempt, result, value, origin, manifest)


def recollect(builder_id: str, task_id: str, accept_fallback: bool = False) -> dict:
    """Re-validate a finished attempt's unchanged output with the current collector. No model call.

    accept_fallback admits an attempt whose only audit failure is Claude Code's automatic switch to another
    model after a safeguard refusal (user decision, 2026-10-07). The writer models and their output-token
    shares are recorded, and the lessons header names both models."""
    builder = builders.spec(builder_id)
    attempt = sorted((RUNS / f"distill_{prompt_version(builder)}" / builder_id / task_id).glob("attempt-*"))[-1]
    result = load(attempt / "attempt.json")
    audit = result.get("model_audit", {})
    fallback_ok = (accept_fallback and audit.get("refusal_fallbacks") and audit.get("init_model") == builder["model"])
    if result.get("status") != "failed" or not (audit.get("model_effort_verified") or fallback_ok):
        raise SystemExit("only a model-verified (or, with --accept-fallback, refusal-fallback) failed attempt can be re-collected")
    if fallback_ok:
        usage = next(json.loads(line) for line in (attempt / "claude.events.jsonl").read_text().splitlines()
                     if json.loads(line).get("type") == "result")["modelUsage"]
        tokens = {m: (u or {}).get("outputTokens") or 0 for m, u in usage.items()}
        result["accepted_refusal_fallback"] = {
            "decision": "user accepted the fallback output as this builder's memory (2026-10-07)",
            "fallbacks": audit["refusal_fallbacks"], "output_tokens_by_model": tokens,
            "output_share_by_model": {m: round(n / max(1, sum(tokens.values())), 3) for m, n in tokens.items()}}
        fallback_models = sorted({f["fallback_model"] for f in audit["refusal_fallbacks"]})
        builder = {**builder, "model": f"{builder['model']}, continued by {', '.join(fallback_models)} after a safeguard refusal"}
    manifest, records = _inputs(task_id)
    value, origin = collect(attempt, citable(task_id, records), result["started_at_unix"],
                            result["started_at_unix"] + result["timeout_seconds"])
    result["first_collection"] = {"status": "failed", "error": result.pop("error", None)}
    result["recollected"] = {"at_unix": time.time(), "model_called": False}
    return finish(task_id, builder, attempt, result, value, origin, manifest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--builders", nargs="+")
    parser.add_argument("--tasks", nargs="+")
    parser.add_argument("--recollect", nargs=2, metavar=("BUILDER", "TASK"))
    parser.add_argument("--accept-fallback", action="store_true", help="with --recollect: admit a refusal-fallback attempt")
    args = parser.parse_args(argv)
    if args.recollect:
        row = recollect(*args.recollect, accept_fallback=args.accept_fallback)
        print(json.dumps({k: v for k, v in row.items() if k in ("task_id", "builder", "status", "lesson_count", "error")}))
        return 0
    tasks = args.tasks or load(MANIFESTS / "selection.json")["chosen_task_ids"]
    jobs = [(t, b) for b in args.builders for t in tasks]
    support = v6.support(TB, v6.DEFAULT_IMAGE)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for row in pool.map(lambda job: run_job(job[0], job[1], support), jobs):
            print(json.dumps({k: row.get(k) for k in ("builder", "task_id", "status", "lesson_count", "lessons_chars",
                                                       "elapsed_seconds", "timed_out", "error", "reused")}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
