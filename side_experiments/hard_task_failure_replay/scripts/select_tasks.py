"""Label hard tasks from official leaderboard outcomes and pick the side-experiment tasks.

No model is called. Training-task outcomes come from the main pool audit
(manifests/pool.json); test-task outcomes come from the official per-condition
means. Both cover 22 leaderboard conditions x 5 attempts per task.

Rule (frozen in config.json): rank every task by the GPT-5.6-family pass rate
(Luna and Terra, the solver's own model family), then by the overall pass rate.
Keep tasks that are CPU-only, single-service, solved at least once on the
leaderboard, and that have enough failed attempts with transcripts.
"""
from __future__ import annotations

import collections
import csv
import json
import sys
import tomllib
from pathlib import Path

from .common import MANIFESTS, TASKS, TB, TRIALS, config, load, sha, write_json


def outcome_table() -> dict:
    stats = collections.defaultdict(lambda: {"all": [0, 0], "gpt": [0, 0], "gpt56": [0, 0],
                                             "failed_with_body": 0, "split": None})
    for row in load(TB / "manifests/pool.json")["trajectories"]:
        if row.get("is_scored") is not True or row.get("score") not in (0, 1):
            continue
        item = stats[row["task_id"]]
        item["split"] = "training"
        for key, ok in (("all", True), ("gpt", row["model"].startswith("gpt")),
                        ("gpt56", row["model"].startswith("gpt-5.6"))):
            if ok:
                item[key][0] += row["score"]
                item[key][1] += 1
        if row["score"] == 0 and row["status"] == "available" and row["source_format"] == "atif_json":
            item["failed_with_body"] += 1
    for row in csv.DictReader(open(TB / "reports/official_test_task_means.csv")):
        try:
            mean = float(row["exact_version_observed_attempt_mean"])
            count = int(row["exact_version_attempt_count"])
        except ValueError:
            continue
        item = stats[row["task_id"]]
        item["split"] = "test"
        passes = round(mean * count)
        for key, ok in (("all", True), ("gpt", row["model"].startswith("GPT")),
                        ("gpt56", row["model"].startswith("GPT-5.6"))):
            if ok:
                item[key][0] += passes
                item[key][1] += count
    for row in csv.DictReader(open(TB / "reports/official_test_trials.csv")):
        if (row["is_scored"] == "True" and row["reward"] in ("0", "0.0") and row["version_status"] == "exact"
                and (TRIALS / row["trial_id"] / "trajectory.json").is_file()):
            stats[row["task_id"]]["failed_with_body"] += 1
    return stats


def task_facts(task_id: str) -> dict:
    spec = tomllib.loads((TASKS / task_id / "task.toml").read_text())
    environment = spec.get("environment", {})
    return {"agent_timeout_sec": spec["agent"]["timeout_sec"],
            "cpu_only": not environment.get("gpus") and not spec.get("verifier", {}).get("environment", {}).get("gpus"),
            "single_service": not (TASKS / task_id / "environment/docker-compose.yaml").exists(),
            "cpus": environment.get("cpus"), "memory_mb": environment.get("memory_mb")}


def select(settings: dict) -> dict:
    stats = outcome_table()
    rule = settings["selection"]
    table = []
    for task_id, item in stats.items():
        rate = lambda key: item[key][0] / item[key][1] if item[key][1] else None
        facts = task_facts(task_id)
        row = {"task_id": task_id, "split": item["split"],
               "overall": f"{item['all'][0]}/{item['all'][1]}", "overall_rate": round(rate("all"), 4),
               "gpt": f"{item['gpt'][0]}/{item['gpt'][1]}",
               "gpt56": f"{item['gpt56'][0]}/{item['gpt56'][1]}", "gpt56_rate": rate("gpt56"),
               "failed_with_body": item["failed_with_body"], **facts}
        row["hard"] = row["overall_rate"] <= rule["hard_overall_rate_max"] or row["gpt56_rate"] == 0
        reasons = []
        if not facts["cpu_only"]:
            reasons.append("needs GPU")
        if not facts["single_service"]:
            reasons.append("multi-service")
        if item["all"][0] < rule["min_official_passes"]:
            reasons.append("never solved officially")
        if item["failed_with_body"] < rule["min_failed_transcripts"]:
            reasons.append("too few failed transcripts")
        row["eligible"], row["ineligible_reasons"] = not reasons, reasons
        table.append(row)
    table.sort(key=lambda r: (r["gpt56_rate"] if r["gpt56_rate"] is not None else 1, r["overall_rate"], r["task_id"]))
    chosen = [r["task_id"] for r in table if r["hard"] and r["eligible"]][:rule["n_tasks"]]
    return {"rule": rule, "chosen_task_ids": chosen,
            "hard_task_count": sum(r["hard"] for r in table), "ranking": table,
            "inputs_sha256": {name: sha(TB / name) for name in (
                "manifests/pool.json", "reports/official_test_task_means.csv", "reports/official_test_trials.csv")}}


def main() -> int:
    result = select(config())
    write_json(MANIFESTS / "selection.json", result)
    print(f"hard tasks: {result['hard_task_count']}; chosen: {', '.join(result['chosen_task_ids'])}")
    for row in result["ranking"][:12]:
        flag = "*" if row["task_id"] in result["chosen_task_ids"] else " "
        print(f"{flag} {row['task_id']:32} {row['split']:8} all {row['overall']:>7}  gpt5.6 {row['gpt56']:>5}"
              f"  failed transcripts {row['failed_with_body']:>3}  {', '.join(row['ineligible_reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
