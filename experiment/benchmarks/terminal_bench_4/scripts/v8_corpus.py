"""V8 corpus: each selected TB4 task's pool holds only its own officially failed attempts.

  download   fetch every failure source's official trajectory (or official absence proof) from the Hub
  scan       render, sanitize and score every body for contamination; write prepared/v8_corpus/decontam_scan.json
  decide     apply the calibrated V7 exclusion rules to the scan; write decontamination_decisions.json
  build      write prepared/v8_corpus/<build_id>/{index.json, audit/, pools/<task>/} in the V7 pool format
  verify     re-verify a finished build against its index

The pool format, renderer, absence handling and contamination metrics are the frozen engine's (engine/v7_corpus.py), so the V7 DCI tools, Opus builder and runtime consume these pools unchanged. Successful attempts
are never listed. Hidden tests and reference solutions are read on the host only to flag contaminated
trajectories; their text is never written. Usage (repository root, this benchmark's .venv):
  python -m experiment.benchmarks.terminal_bench_4.scripts.v8_corpus download [--workers 4]
"""
from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from experiment.benchmarks.terminal_bench_4.engine import v7_corpus as vc

BASE = Path(__file__).resolve().parents[1]
MANIFESTS = BASE / "manifests"
DATA = BASE / "data/historical_trials"
SOURCES = MANIFESTS / "v8_failure_sources.json"
DOWNLOAD_AUDIT = MANIFESTS / "v8_download_audit.json"
BENCHMARK = "Terminal-Bench 4.0"
CORPUS = BASE / "prepared/v8_corpus"
SCAN = CORPUS / "decontam_scan.json"
DECISIONS = CORPUS / "decontamination_decisions.json"
# Reaching this benchmark's own tasks, tests, solutions, registry or write-ups (mirrors included). Docker image names
# such as harborframework/terminal-bench:<task>-environment are task metadata, not a source, and do not match.
BENCHMARK_ACCESS = re.compile(
    r"tbench\.ai|(?:hub|registry|www)\.harborframework\.com|harborframework\.com/(?:tasks|datasets|leaderboard)|"
    r"github\.com/[^\s'\"]*/(?:terminal-bench|harbor)\b|huggingface\.co/(?:api/)?datasets/[^\s'\"]*terminal-bench|"
    r"laude-institute|marginlab\.ai|spylab\.ai|original-tasks/|"
    # A dataset name inside an official session or container name (terminal-bench-4-no-prebuild-<task>__<id>__agent)
    # is run metadata, not a source.
    r"terminal-bench-(?:1|2|3|4|science|core)\b(?!-no-prebuild-[\w.-]*__[0-9a-f]{8}__)", re.I)
HARNESS = re.compile(r"test_outputs\.py|solve\.sh|/solution/|/tests/test\.sh")
WORD = re.compile(r"\w")


def model_name(row: dict) -> str:
    return f"{row['model']} ({row['agent']}, {row['effort']})"


def source_rows() -> list[dict]:
    """Failure sources in the row schema V7's fetch/state/build helpers read (agent version: submission date)."""
    data = vc.load(SOURCES)
    dates = {row["id"]: row["metadata"].get("date") for row in vc.load(MANIFESTS / "official_leaderboard.json")["rows"]}
    return [{"task_id": r["task_id"], "trial_id": r["trial_id"], "job_id": r["job_id"], "model_name": model_name(r),
             "model_alias": vc.alias(model_name(r)), "agent_name": r["agent"], "agent_version": dates.get(r["row_id"]) or "",
             "error_type": r["exception_type"], "registry_trajectory_path": r["trajectory_path"], "row_id": r["row_id"]}
            for r in data["rows"]]


def task_rows() -> dict[str, dict]:
    """The row fields V7's build_task reads, for each selected task (no researcher preclassification)."""
    selection = vc.load(MANIFESTS / "v8_tasks.json")
    versions = {t["task_id"]: t for t in vc.load(MANIFESTS / "tb4_tasks.json")["tasks"]}
    rows = {}
    for task_id in selection["selected"]:
        directory = BASE / "prepared/tasks" / task_id
        rows[task_id] = {"task_id": task_id, "source_path": f"prepared/tasks/{task_id}",
                         "instruction_sha256": vc.sha(directory / "instruction.md"),
                         "task_version_id": versions[task_id]["task_version_id"]}
    return rows


def _states(rows: list[dict]) -> dict:
    states = {r["trial_id"]: vc.trial_state(r, DATA) for r in rows}
    bad = sorted(t for t, s in states.items() if s["status"] in ("invalid", "unresolved"))
    if bad:
        raise ValueError(f"{len(bad)} failure sources are unresolved or invalid (first {bad[0]}); run download first")
    return states


def _scan_task(task_id: str, row: dict, sources: list[dict], states: dict) -> list[dict]:
    """Contamination evidence per published body: metrics, line numbers and matched source strings only."""
    task_dir, _ = vc.task_instruction(BASE, row)
    references, _ = vc.reference_material(task_dir)
    known = vc.package_data.local_credentials(vc.ROOT)
    out = []
    for number, source in enumerate(sorted(sources, key=lambda s: (vc.alias(s["model_name"]), s["trial_id"])), 1):
        state = states[source["trial_id"]]
        if state["status"] not in ("available", "raw_fallback"):
            out.append({"trial_id": source["trial_id"], "task_id": task_id, "status": state["status"]})
            continue
        item = {"attempt_id": f"attempt-{number:03d}", "trial_id": source["trial_id"], "model_name": source["model_name"],
                "model_alias": vc.alias(source["model_name"]), "status": state["status"]}
        try:
            prepared = vc.prepare_attempt(item, source, state, row, data_root=DATA, references=references, known=known,
                                          fixtures=set(), decision={"decision": "keep", "reason": "scan only"})
        except ValueError as exc:  # e.g. a credential candidate the sanitizer cannot clear: never publish unreviewed
            out.append({"trial_id": source["trial_id"], "task_id": task_id, "attempt_id": item["attempt_id"],
                        "model_name": source["model_name"], "status": state["status"], "scan_error": str(exc)[:300]})
            continue
        text = prepared["data"].decode("utf-8", "replace")
        lines = text.split("\n")
        access = sorted({(n, m.group(0).lower()) for n, line in enumerate(lines, 1) for m in BENCHMARK_ACCESS.finditer(line)})
        harness = sorted({(n, m.group(0)) for n, line in enumerate(lines, 1) for m in HARNESS.finditer(line)})
        check = prepared["decontamination"]
        out.append({"trial_id": source["trial_id"], "task_id": task_id, "attempt_id": item["attempt_id"],
                    "model_name": source["model_name"], "status": state["status"], "lines": len(lines),
                    "fetch_hits": check["fetch_hits"], "test_file_refs": check["test_file_refs"],
                    "solution_overlap": check["solution_overlap"], "tests_overlap_max_run": check["tests_overlap_max_run"],
                    "solution_coverage": check["review"]["solution_coverage"],
                    "solution_overlap_max_run": check["review"]["solution_overlap_max_run"],
                    "frozen_rule_flagged": check["flagged"], "frozen_rule_reasons": check["reasons"],
                    "benchmark_access": [{"line": n, "match": m} for n, m in access[:20]],
                    "harness_refs": [{"line": n, "match": m} for n, m in harness[:20]]})
    return out


def scan(workers: int = 8) -> dict:
    rows, tasks = source_rows(), task_rows()
    states = _states(rows)
    by_task = {task: [r for r in rows if r["task_id"] == task] for task in tasks}
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        futures = {task: pool.submit(_scan_task, task, tasks[task], by_task[task], states) for task in tasks}
        results = [item for task in sorted(futures) for item in futures[task].result()]
    value = {"schema_version": 1, "kind": "v8_decontam_scan", "created_at": vc.now(),
             "source_manifest_sha256": vc.sha(SOURCES), "download_audit_sha256": vc.sha(DOWNLOAD_AUDIT),
             "benchmark_access_pattern": BENCHMARK_ACCESS.pattern, "harness_pattern": HARNESS.pattern,
             "solution_shingle_tokens": vc.SOLUTION_SHINGLE, "tests_shingle_tokens": vc.TESTS_SHINGLE,
             "items": results}
    CORPUS.mkdir(parents=True, exist_ok=True)
    vc.save(SCAN, value)
    return value


def tests_overlap_words(text: str, references: dict) -> int:
    """Longest run shared with the hidden tests, counted in word tokens. The V7 metric counts every punctuation
    character as a token, so one '# -----' separator line, common to most code, already makes a 70-token run."""
    tokens = vc.TOKEN.findall(text)
    marked = vc._positions(tokens, references["tests"], vc.TESTS_SHINGLE)
    best, start = 0, None
    for position, i in enumerate(marked):
        if start is None or i != marked[position - 1] + 1:
            if start is not None:
                end = marked[position - 1] + vc.TESTS_SHINGLE
                best = max(best, sum(1 for token in tokens[start:end] if WORD.match(token)))
            start = i
    if start is not None:
        best = max(best, sum(1 for token in tokens[start:marked[-1] + vc.TESTS_SHINGLE] if WORD.match(token)))
    return best


def visible_paths(instruction: str) -> set[str]:
    """Harness-like path prefixes the official instruction itself shows (e.g. /app/solution/ for a task whose
    deliverable is /app/solution/output.json): any path under them is a task path, not the grading harness."""
    shown = set()
    for path in re.findall(r"[\w./-]*(?:test_outputs\.py|solve\.sh|/solution/|/tests/test\.sh)[\w./-]*", instruction):
        for name in HARNESS.findall(path):
            prefix = path[:path.index(name) + len(name)]
            if prefix != name:  # a bare harness name in the instruction would whitelist nothing specific
                shown.add(prefix)
    return shown


def _refine_task(task_id: str, row: dict, items: list[dict], sources: dict, states: dict) -> list[dict]:
    """Re-read flagged bodies with the calibrated TB4 patterns and the word-counted test overlap."""
    task_dir, instruction = vc.task_instruction(BASE, row)
    references, _ = vc.reference_material(task_dir)
    known = vc.package_data.local_credentials(vc.ROOT)
    shown = visible_paths(instruction)
    out = []
    for item in items:
        source = sources[item["trial_id"]]
        base = {k: item[k] for k in ("attempt_id", "trial_id", "model_name", "status")}
        prepared = vc.prepare_attempt({**base, "model_alias": vc.alias(source["model_name"])}, source,
                                      states[item["trial_id"]], row, data_root=DATA, references=references,
                                      known=known, fixtures=set(), decision={"decision": "keep", "reason": "refine"})
        text = prepared["data"].decode("utf-8", "replace")
        lines = text.split("\n")
        access = sorted({(n, m.group(0).lower()) for n, line in enumerate(lines, 1) for m in BENCHMARK_ACCESS.finditer(line)})
        harness = sorted({(n, m.group(0)) for n, line in enumerate(lines, 1) for m in HARNESS.finditer(line)
                          if not any(prefix in line for prefix in shown if prefix.endswith(m.group(0)))})
        out.append({**item, "benchmark_access": [{"line": n, "match": m} for n, m in access[:20]],
                    "harness_refs": [{"line": n, "match": m} for n, m in harness[:20]],
                    "tests_overlap_max_words": tests_overlap_words(text, references), "refined": True})
    return out


def refine(workers: int = 8) -> dict:
    """Second pass over bodies the first scan flagged; first-pass metrics stay in each item for the record."""
    value = vc.load(SCAN)
    rows, tasks = source_rows(), task_rows()
    sources = {r["trial_id"]: r for r in rows}
    states = _states(rows)
    todo = [i for i in value["items"] if "attempt_id" in i and not i.get("scan_error")
            and (i["benchmark_access"] or i["harness_refs"] or (i["tests_overlap_max_run"] or 0) >= 60)]
    with concurrent.futures.ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(_refine_task, task, tasks[task], [i for i in todo if i["task_id"] == task], sources, states)
                   for task in sorted({i["task_id"] for i in todo})]
        refined = {i["trial_id"]: i for future in futures for i in future.result()}
    value["items"] = [refined.get(i.get("trial_id"), i) if "attempt_id" in i else i for i in value["items"]]
    value["refinement"] = {"benchmark_access_pattern": BENCHMARK_ACCESS.pattern,
                           "harness_rule": "harness-like paths shown by the official instruction are task paths",
                           "tests_overlap_rule": "longest run shared with the hidden tests, counted in word tokens",
                           "bodies": len(refined)}
    vc.save(SCAN, value)
    return value["refinement"]


def calibrated_rule(item: dict) -> tuple[str, str]:
    """V7's calibrated exclusion rule, applied mechanically; every exclusion is re-read by hand before a build."""
    if item.get("scan_error"):
        return "exclude", "could not be published safely: " + item["scan_error"]
    if item["benchmark_access"]:
        return "exclude", "benchmark_source_access: " + ", ".join(sorted({h["match"] for h in item["benchmark_access"]}))
    if item["harness_refs"]:
        return "exclude", "names benchmark harness files: " + ", ".join(sorted({h["match"] for h in item["harness_refs"]}))
    if (item["solution_coverage"] or 0) >= 0.5:
        return "exclude", f"covers {item['solution_coverage']:.2f} of the reference solution"
    words = item.get("tests_overlap_max_words", item["tests_overlap_max_run"])
    if (words or 0) >= 60:
        return "exclude", f"shares a run of {words} words with the hidden tests"
    if item["frozen_rule_flagged"]:
        return "keep", ("calibrated V7 rule: only natural overlap or generic search (solution coverage "
                        f"{item['solution_coverage']:.3f}, longest solution run {item['solution_overlap_max_run']}, "
                        f"longest tests run {item['tests_overlap_max_run']}, fetch hits {item['fetch_hits']}, "
                        f"test-path refs {item['test_file_refs']}); no benchmark source or harness file")
    return "", ""


def decide(reviews: Path | None = None) -> dict:
    """Decisions for every flagged body; a reviewed file (trial_id -> {decision, reason}) overrides the rule."""
    value = vc.load(SCAN)
    if value["source_manifest_sha256"] != vc.sha(SOURCES):
        raise ValueError("The scan belongs to another failure-source manifest; re-run scan")
    overrides = vc.load(reviews) if reviews else {}
    decisions, counts = {}, Counter()
    for item in value["items"]:
        if "attempt_id" not in item:
            continue
        choice, reason = calibrated_rule(item)
        if item["trial_id"] in overrides:
            choice, reason = overrides[item["trial_id"]]["decision"], "reviewed: " + overrides[item["trial_id"]]["reason"]
        if choice:
            decisions[item["trial_id"]] = {"decision": choice, "reason": reason, "task_id": item["task_id"]}
            counts[choice] += 1
    vc.save(DECISIONS, decisions)
    return {"decisions": dict(counts), "path": str(DECISIONS.relative_to(BASE))}


def _build_task(task_id: str, row: dict, sources: list[dict], states: dict, decisions: dict, *, staging: Path,
                build_id: str, known) -> dict:
    record, audit = vc.build_task(row, sources, states, decisions, base=BASE, data_root=DATA,
                                  pool=staging / "pools" / task_id,
                                  pool_path=f"prepared/v8_corpus/{build_id}/pools/{task_id}",
                                  build_id=build_id, known=known, fixtures=(set(), []), benchmark=BENCHMARK)
    vc.save(staging / "audit" / f"{task_id}.json", audit)
    return record


def build(build_id: str, workers: int = 8) -> dict:
    out = CORPUS / build_id
    if not vc.BUILD_ID.fullmatch(build_id) or out.exists():
        raise FileExistsError(f"Refuse to overwrite or misname a V8 corpus build: {out}")
    rows, tasks = source_rows(), task_rows()
    states = _states(rows)
    decisions, decisions_sha = vc.load_decisions(DECISIONS, {"rows": rows})
    flagged = {item["trial_id"] for item in vc.load(SCAN)["items"] if item.get("frozen_rule_flagged")}
    if flagged - set(decisions):
        raise ValueError(f"{len(flagged - set(decisions))} flagged bodies have no decision; run decide")
    known = vc.package_data.local_credentials(vc.ROOT)
    staging = Path(tempfile.mkdtemp(prefix=f".{build_id}.building.", dir=CORPUS))
    try:
        staging.chmod(0o755)
        # Each task writes only its own pool and audit file, so tasks build independently.
        with concurrent.futures.ProcessPoolExecutor(workers) as pool:
            futures = {task_id: pool.submit(_build_task, task_id, tasks[task_id],
                                            [r for r in rows if r["task_id"] == task_id], states, decisions,
                                            staging=staging, build_id=build_id, known=known)
                       for task_id in tasks}
            records = {task_id: futures[task_id].result() for task_id in sorted(futures)}
        index = {"schema_version": 1, "kind": vc.INDEX_KIND, "benchmark": BENCHMARK, "build_id": build_id,
                 "created_at": vc.now(), "complete": True, "selection_sha256": vc.sha(MANIFESTS / "v8_tasks.json"),
                 "source_manifest_sha256": vc.sha(SOURCES), "download_audit_sha256": vc.sha(DOWNLOAD_AUDIT),
                 "decisions_sha256": decisions_sha, "tasks": records}
        vc.save(staging / "index.json", index)
        for path in staging.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


def verify(build_id: str) -> dict:
    root = CORPUS / build_id
    index = vc.load(root / "index.json")
    if index.get("kind") != vc.INDEX_KIND or index.get("benchmark") != BENCHMARK:
        raise ValueError("Not a V8 corpus index")
    if index["selection_sha256"] != vc.sha(MANIFESTS / "v8_tasks.json") or index["source_manifest_sha256"] != vc.sha(SOURCES):
        raise ValueError("The corpus was built from other selection or source manifests")
    if index["decisions_sha256"] != (vc.sha(DECISIONS) if DECISIONS.exists() else None):
        raise ValueError("Decontamination decisions changed after the build")
    if set(index["tasks"]) != set(vc.load(MANIFESTS / "v8_tasks.json")["selected"]):
        raise ValueError("The corpus covers a different task set")
    for task_id, record in index["tasks"].items():
        pool = BASE / record["pool_path"]
        checked = vc.verify_task_pool(pool, task_id)
        if (checked["pool_manifest_sha256"] != record["pool_manifest_sha256"]
                or checked["files_sha256"] != record["pool_files_sha256"]
                or checked["trajectory_count"] != record["trajectory_count"]):
            raise ValueError(f"{task_id}: pool differs from its index record")
    return {"build_id": build_id, "tasks": len(index["tasks"]),
            "trajectories": sum(r["trajectory_count"] for r in index["tasks"].values()),
            "excluded": sum(r["excluded_count"] for r in index["tasks"].values())}


def metadata(row: dict) -> dict:
    """What fetch_trial saves as trial_metadata.json; V7's _metadata check binds a body to this failed row."""
    return {"id": row["trial_id"], "job_id": row["job_id"], "task_name": "terminal-bench/" + row["task_id"],
            "model_name": row["model_name"], "reward": 0, "is_scored": True, "error_type": row["error_type"],
            "trajectory_path": row["registry_trajectory_path"], "leaderboard_row_id": row["row_id"]}


def download(workers: int = 4, limit: int | None = None) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError("Use 1-4 bounded network workers")
    rows = source_rows()
    DATA.mkdir(parents=True, exist_ok=True)
    with (MANIFESTS / "v8_downloader.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another V8 downloader owns v8_downloader.lock") from None
        states = {r["trial_id"]: vc.trial_state(r, DATA) for r in rows}
        pending = [r for r in rows if states[r["trial_id"]]["status"] in ("unresolved", "invalid")]
        if limit is not None:
            pending = pending[:limit]
        actions = {r["trial_id"]: ("reused", None) for r in rows if r not in pending}

        def one(row):
            return row, vc.fetch_trial(row, metadata(row), DATA)

        with concurrent.futures.ThreadPoolExecutor(workers) as executor:
            for done, (row, result) in enumerate(executor.map(one, pending), 1):
                actions[row["trial_id"]] = (result["action"], result["reason"])
                states[row["trial_id"]] = vc.trial_state(row, DATA)
                if done % 100 == 0:
                    print(json.dumps({"progress": done, "pending": len(pending)}), flush=True)
        trials = []
        for row in rows:
            state = states[row["trial_id"]]
            status = "unresolved" if state["status"] in ("unresolved", "invalid") else state["status"]
            action, reason = actions.get(row["trial_id"], ("not_attempted", None))
            trials.append({"task_id": row["task_id"], "trial_id": row["trial_id"], "status": status, "action": action,
                           "sha256": state["source_sha256"], "bytes": state["source_bytes"],
                           "retrieved_at": state["retrieved_at"], "url": state["source_url"],
                           "source_path": state["source_path"],
                           "reason": (reason or state["reason"]) if status == "unresolved" else state["source_reason"]})
        counts = Counter(t["status"] for t in trials)
        audit = {"schema_version": 1, "kind": "v8_download_audit", "updated_at": vc.now(),
                 "source_manifest_sha256": vc.sha(SOURCES), "data_root": "data/historical_trials",
                 "counts": {k: counts.get(k, 0) for k in ("available", "raw_fallback", "unavailable", "unresolved")},
                 "complete": not counts.get("unresolved"), "trials": trials}
        vc.save(DOWNLOAD_AUDIT, audit)
    return audit


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("download", help="fetch failure-source trajectories from the official Hub")
    fetch.add_argument("--workers", type=int, default=4)
    fetch.add_argument("--limit", type=int)
    scanner = sub.add_parser("scan", help="contamination metrics for every downloaded body (host only)")
    scanner.add_argument("--workers", type=int, default=8)
    refiner = sub.add_parser("refine", help="second pass over flagged bodies with the calibrated TB4 patterns")
    refiner.add_argument("--workers", type=int, default=8)
    decider = sub.add_parser("decide", help="apply the calibrated rule; --reviews overrides individual bodies")
    decider.add_argument("--reviews", type=Path)
    builder = sub.add_parser("build")
    builder.add_argument("--build-id", required=True)
    builder.add_argument("--workers", type=int, default=8)
    sub.add_parser("verify").add_argument("--build-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "download":
        audit = download(args.workers, args.limit)
        print(json.dumps({"counts": audit["counts"], "complete": audit["complete"]}))
    elif args.command == "scan":
        items = scan(args.workers)["items"]
        print(json.dumps({"bodies": sum("attempt_id" in i for i in items),
                          "frozen_rule_flagged": sum(bool(i.get("frozen_rule_flagged")) for i in items),
                          "benchmark_access": sum(bool(i.get("benchmark_access")) for i in items),
                          "harness_refs": sum(bool(i.get("harness_refs")) for i in items)}))
    elif args.command == "refine":
        print(json.dumps(refine(args.workers)))
    elif args.command == "decide":
        print(json.dumps(decide(args.reviews)))
    elif args.command == "build":
        index = build(args.build_id, args.workers)
        print(json.dumps({"tasks": len(index["tasks"]), "trajectories": sum(r["trajectory_count"] for r in index["tasks"].values())}))
    else:
        print(json.dumps(verify(args.build_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
