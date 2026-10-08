"""Combine every valid run into one task x arm table (counted rewards) plus builder costs.

Counted reward is 0 for a trial that fetched the benchmark's own sources or tests.
Runs: r2/r3 (control, failure_replay with v1 Luna xhigh lessons), the v3 rounds
(control, traj_only, mem_<builder> with v3/v4 analyses) and the tmr rounds (trajmem_<builder>:
failed transcripts plus the same analyses). Writes manifests/results_all.json and prints a
compact table. Usage: python -m ...scripts.summarize v3r1 v3r2 v3r2x v3r3 tmr1 tmr2 tmr3
(v3r2x holds the counted re-run of v3r2 mem_sol56/gcode-to-text; same-window anchors such as v3r4 or tma1 are
summarised separately with `run report --run-name <run>`, not merged into these cells).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict

from .common import HERE, MANIFESTS, RUNS, config, load, write_json

BASE_RUNS = ["r2_20261007", "r3_20261007"]
from .run import ARMS as ARM_ORDER  # control, failure_replay, traj_only, mem_<builder>..., trajmem_<builder>...


def main() -> int:
    runs = BASE_RUNS + sys.argv[1:]
    for run in runs:
        subprocess.run([sys.executable, "-m", "side_experiments.hard_task_failure_replay.scripts.run", "report",
                        "--run-name", run], cwd=HERE.parents[1], stdout=subprocess.DEVNULL, check=True)
    rows = [{"run": run, **row} for run in runs for row in load(RUNS / run / "report.json")["rows"]]
    tasks = load(MANIFESTS / "selection.json")["chosen_task_ids"]
    cells = defaultdict(list)
    for row in rows:
        cells[(row["task_id"], row["arm"])].append(row)
    table = {task: {arm: {"passes": sum((r["counted_reward"] or 0) for r in cells[(task, arm)]),
                          "trials": len(cells[(task, arm)]),
                          "unscored": sum(r["counted_reward"] is None for r in cells[(task, arm)]),
                          "integrity_violations": sum(bool(r["integrity_violation"]) for r in cells[(task, arm)]),
                          "by_run": {r["run"]: r["counted_reward"] for r in cells[(task, arm)]}}
                    for arm in ARM_ORDER if cells[(task, arm)]} for task in tasks}
    totals = {arm: {"passes": sum(table[t][arm]["passes"] for t in tasks if arm in table[t]),
                    "trials": sum(table[t][arm]["trials"] for t in tasks if arm in table[t]),
                    "unscored": sum(table[t][arm]["unscored"] for t in tasks if arm in table[t]),
                    "tasks_ever_solved": sum(1 for t in tasks if arm in table[t] and table[t][arm]["passes"] > 0),
                    "solver_minutes": round(sum(r["solver_seconds"] or 0 for r in rows if r["arm"] == arm) / 60, 1),
                    "trials_reading_transcripts": sum(bool(r.get("transcript_commands")) for r in rows if r["arm"] == arm),
                    "trials_reading_memory_file": sum(bool(r.get("memory_commands")) for r in rows if r["arm"] == arm),
                    "verifier_infrastructure_failures": sum(bool(r.get("verifier_infrastructure_failure"))
                                                            for r in rows if r["arm"] == arm),
                    "solver_tokens_M": round(sum(r["tokens"] or 0 for r in rows if r["arm"] == arm) / 1e6, 2)}
              for arm in ARM_ORDER if any(r["arm"] == arm for r in rows)}
    builders = {}
    for builder_id, builder in config()["builders"].items():
        version = builder.get("prompt_version", "v3")
        memos = [load(MANIFESTS / f"memory_{version}_{builder_id}_{t}.json") for t in tasks
                 if (MANIFESTS / f"memory_{version}_{builder_id}_{t}.json").exists()]
        # Costs count every attempt, including ones whose output failed validation and was retried.
        attempts = [load(p) for t in tasks for p in sorted((RUNS / f"distill_{version}" / builder_id / t).glob("attempt-*/attempt.json"))]
        usage = [a.get("usage") or {} for a in attempts]
        builders[builder_id] = {"model": builder["model"], "effort": builder["effort"], "prompt_version": version,
                                "memories": len(memos),
                                "attempts": len(attempts), "failed_attempts": sum(a.get("status") != "complete" for a in attempts),
                                "lessons": [m.get("lesson_count") for m in memos],
                                "chars": [m.get("lessons_chars") for m in memos],
                                "minutes": round(sum(a.get("elapsed_seconds") or 0 for a in attempts) / 60, 1),
                                "tokens_M": round(sum((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                                                      + (u.get("cache_read_input_tokens") or 0)
                                                      + (u.get("cache_creation_input_tokens") or 0) for u in usage) / 1e6, 2),
                                "cost_usd": round(sum(u.get("total_cost_usd") or 0 for u in usage), 2) or None}
    write_json(MANIFESTS / "results_all.json", {"runs": runs, "table": table, "totals": totals, "builders": builders,
                                                 "counting_rule": "integrity violation counts as 0", "rows": rows})
    def fmt(cell):  # passes/scored trials, then "+n?" for trials still running or without a reward
        if not cell:
            return "-"
        scored = cell["trials"] - cell["unscored"]
        return f"{int(cell['passes'])}/{scored}" + (f" +{cell['unscored']}?" if cell["unscored"] else "")

    print(f"{'task':28}" + "".join(f"{arm:>18}" for arm in ARM_ORDER))
    for task in tasks:
        print(f"{task:28}" + "".join(f"{fmt(table[task].get(arm)):>18}" for arm in ARM_ORDER))
    print(f"{'TOTAL':28}" + "".join(f"{fmt(totals.get(a)):>18}" for a in ARM_ORDER))
    for builder_id, info in builders.items():
        print(builder_id, json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
