"""Audit every V7 Opus builder attempt for outside access, and every published analysis for hidden-material overlap.

The builder container needs egress for the model API, so isolation is verified after the fact from the full Claude
Code stream (every tool call and tool result is logged). Unlike the export gate in v7_memory.py, which checks only
the attempt each seed was built from, this scans every attempt, including rejected ones, and also records benchmark
source strings that appear in tool results (trajectory text the builder read). Read-only; prints no test or
solution text. Usage (repo root, TB2.1 venv):
  python -m experiment.benchmarks.terminal_bench_2_1.scripts.v7_builder_audit \
      runs/v7_memory_build_20261007 prepared/v7_corpus/final_20261007
Writes reports/v7_same_task_replay/builder_audit.json and builder_stats.json (per-job cost, time, lessons, seed
entries, models, citation trims and tool calls of the attempt each seed came from; local, git-ignored).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from . import v7_corpus, v7_memory
from .v7_memory import NETWORK_CODE, _strings, executed_outside

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "reports/v7_same_task_replay"
# Broader than the export gate's pattern: tool results contain trajectory text, which may name these hosts.
BENCHMARK_SOURCE = re.compile(
    r"tbench\.ai|harbor[-_]?framework|laude[-_]institute|marginlab\.ai|spylab\.ai|original-tasks/|supabase\.co|"
    r"huggingface\.co/datasets|github\.com/[^\s'\"]*terminal-bench|terminal-bench-(?:1|2|science|core)", re.I)


def stream(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def tool_texts(events_path):
    """(kind, text) for every tool input and tool result in a Claude Code stream-json log."""
    for event in stream(events_path):
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                yield "input:" + str(block.get("name")), json.dumps(block.get("input"), ensure_ascii=False)
            elif block.get("type") == "tool_result":
                content = block.get("content")
                yield "result", content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def audit_attempt(events):
    inputs_outside, source_hits, result_source_hits, tools = [], set(), set(), {}
    for kind, text in tool_texts(events):
        tools[kind] = tools.get(kind, 0) + 1
        if kind == "input:Bash":
            inputs_outside += executed_outside(json.loads(text).get("command", ""))
        if kind.startswith("input:"):
            if kind in ("input:Write", "input:Edit"):
                written = json.loads(text)
                body = written.get("content") or written.get("new_string") or ""
                if re.search(r"\.(py|sh|js|mjs|pl|rb)$", written.get("file_path", "")) and NETWORK_CODE.search(body):
                    inputs_outside.append("network code written to a script")
            source_hits |= {m.group(0).lower() for m in BENCHMARK_SOURCE.finditer(text)}
        else:
            result_source_hits |= {m.group(0).lower() for m in BENCHMARK_SOURCE.finditer(text)}
    return {"tool_calls": tools, "outside_access_in_inputs": sorted(set(inputs_outside)),
            "benchmark_source_in_inputs": sorted(source_hits), "benchmark_source_in_results": sorted(result_source_hits)}


def builder_stats(work: Path, corpus: Path) -> dict:
    """Per-job builder facts for the report, re-hashed through v7_memory.completion (refuses changed artifacts)."""
    plan = v7_memory.load(work / "plan.json")
    seeds = v7_memory.load(corpus / "seeds_index.json")["tasks"]
    index = v7_memory.load(corpus / "index.json")["tasks"]
    rows, tools = [], {}
    for job in plan["jobs"]:
        task = job["task_id"]
        attempts = v7_memory._attempts(work, task)
        done, analysis = v7_memory.completion(work, job)
        audit = done["model_audit"]
        for name, count in done["tool_audit"]["tool_use_counts"].items():
            tools[name] = tools.get(name, 0) + count
        rows.append({"task_id": task, "failed_trajectories": index[task]["trajectory_count"],
                     "timeout_seconds": job["timeout_seconds"], "attempts": [a["status"] for a in attempts],
                     "cost_usd": round(sum((a.get("usage") or {}).get("total_cost_usd") or 0 for a in attempts), 3),
                     "elapsed_seconds": round(sum(a.get("elapsed_seconds") or 0 for a in attempts)),
                     "completed_attempt_seconds": round(done.get("elapsed_seconds") or 0),
                     "lessons": len(analysis["lessons"]), "reusable_details": len(analysis["reusable_details"]),
                     "seed_entries": seeds[task]["entry_count"], "models_used": audit["models_used"],
                     "output_share_by_model": audit["output_share_by_model"],
                     "refusal_fallbacks": len(audit["refusal_fallbacks"]),
                     "nul_trims": sum("nul_trim" in c for c in done["path_completions"]),
                     "path_completions": sum("nul_trim" not in c for c in done["path_completions"]),
                     "tool_calls": done["tool_audit"]["tool_use_counts"],
                     "unexpected_tool_uses": done["tool_audit"]["unexpected_tool_uses"]})
    return {"work_dir": str(work.relative_to(BASE)), "jobs": len(rows), "attempts": sum(len(r["attempts"]) for r in rows),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 2),
            "total_attempt_hours": round(sum(r["elapsed_seconds"] for r in rows) / 3600, 2),
            "lessons": sum(r["lessons"] for r in rows), "seed_entries": sum(r["seed_entries"] for r in rows),
            "strict_model_audit_verified_jobs": sum(r["refusal_fallbacks"] == 0 for r in rows),
            "tool_calls_total": tools, "rows": rows}


def main(argv=None):
    argv = argv or sys.argv[1:]
    work, corpus = BASE / argv[0], BASE / argv[1]
    rows = []
    for job in sorted((work / "jobs").iterdir()):
        task = job.name
        attempts = [{"attempt": attempt.name, **audit_attempt(attempt / "claude.events.jsonl")}
                    for attempt in sorted(job.glob("attempt-*")) if (attempt / "claude.events.jsonl").exists()]
        analysis_path = corpus / "analyses" / task / "analysis.json"
        overlap = None
        if analysis_path.exists():
            text = "\n".join(_strings(json.loads(analysis_path.read_text())))
            references, _ = v7_corpus.reference_material(BASE / "prepared/tasks" / task)
            metrics = v7_corpus.contamination(text, references)
            overlap = {k: metrics.get(k) for k in ("fetch_hits", "test_file_refs", "solution_overlap",
                                                   "tests_overlap_max_run", "flagged", "reasons")}
            overlap["review"] = metrics.get("review")
        rows.append({"task_id": task, "attempts": attempts, "analysis_overlap": overlap})
    flagged = [r["task_id"] for r in rows if any(a["outside_access_in_inputs"] or a["benchmark_source_in_inputs"]
                                                 for a in r["attempts"]) or (r["analysis_overlap"] or {}).get("flagged")]
    out = {"work_dir": argv[0], "corpus": argv[1], "jobs": len(rows), "flagged_for_review": flagged, "rows": rows}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "builder_audit.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    stats = builder_stats(work, corpus)
    (OUT / "builder_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in stats.items() if k != "rows"}, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))
    for r in rows:
        for a in r["attempts"]:
            if a["outside_access_in_inputs"] or a["benchmark_source_in_inputs"] or a["benchmark_source_in_results"]:
                print(r["task_id"], a["attempt"], "outside:", a["outside_access_in_inputs"], "src_in:",
                      a["benchmark_source_in_inputs"], "src_in_results:", a["benchmark_source_in_results"])
        if r["analysis_overlap"] and r["analysis_overlap"].get("flagged"):
            print(r["task_id"], "analysis overlap:", r["analysis_overlap"])


if __name__ == "__main__":
    main()
