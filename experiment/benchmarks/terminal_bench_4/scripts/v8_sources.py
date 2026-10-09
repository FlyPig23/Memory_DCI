"""Freeze the official Terminal-Bench 4.0 leaderboard data that V8 is built from, and select V8's tasks.

Reads only public Harbor Hub tables with Harbor's own published anonymous key; no user credentials, no
trajectory bodies, tests or solutions. Writes, under manifests/:

  official_leaderboard.json   the leaderboard's rows (condition metadata and metrics)
  official_trials.json        every row's trials: task, content hash, reward, exception, trajectory path
  tb4_tasks.json              the task versions of the leaderboard's dataset version, with their configs
  v8_tasks.json               per-task official GPT-5.6 Sol (Codex, max) results, selection and exclusions
  v8_failure_sources.json     scored reward-0 trials of the selected tasks across every condition

Selection: official GPT-5.6 Sol (Codex, max) passes fewer than half of its trials on the task. Excluded:
tasks needing a GPU or TPU, an MCP sidecar service, several steps, or extra environment variables (the
runner supports single-service CPU tasks only). Failure sources keep only trials graded on exactly the
leaderboard's task version; successes are never listed.

Usage (repository root, TB2.1 venv):
  python -m experiment.benchmarks.terminal_bench_4.scripts.v8_sources [--replace]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = Path(__file__).resolve().parents[1]
MANIFESTS = BASE / "manifests"
LEADERBOARD = "4-0-0"
SELECT = {"model": "GPT-5.6 Sol", "agent": "Codex", "effort": "max"}
THRESHOLD = 0.5
USER_AGENT = "TerminalBench4-PublicResearchArchive/1.0"
CHUNK = 80


def _client():
    from harbor.auth.constants import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
    return SUPABASE_URL.rstrip("/") + "/rest/v1", {
        "apikey": SUPABASE_PUBLISHABLE_KEY, "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
        "User-Agent": USER_AGENT}


def get(path: str, attempts: int = 4):
    base, headers = _client()
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{base}/{path}", headers=headers), timeout=120) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name: str, value, *, replace: bool) -> Path:
    path = MANIFESTS / name
    data = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if path.exists() and not replace:
        old = json.loads(path.read_text())
        if {k: v for k, v in old.items() if k != "retrieved_at"} != {k: v for k, v in value.items() if k != "retrieved_at"}:
            raise ValueError(f"{path} exists with different content; pass --replace to refresh the frozen snapshot")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    return path


def leaderboard_rows() -> tuple[dict, list[dict]]:
    board = get(f"leaderboard?select=id,name,title,package_id&name=eq.{LEADERBOARD}")
    if len(board) != 1:
        raise ValueError(f"expected one leaderboard named {LEADERBOARD}, found {len(board)}")
    rows = get(f"leaderboard_row?select=id,metadata,metrics,status&leaderboard_id=eq.{board[0]['id']}&limit=1000")
    return board[0], sorted(rows, key=lambda row: row["id"])


def condition(row: dict) -> dict:
    meta = row["metadata"]
    return {"row_id": row["id"], "model": meta["model_display"]["label"], "agent": meta["agent_display"]["label"],
            "effort": meta.get("reasoning_effort"), "date": meta.get("date")}


def row_trials(row: dict) -> list[dict]:
    links = get(f"leaderboard_row_trial?select=trial_id&row_id=eq.{row['id']}&limit=5000")
    ids = sorted({link["trial_id"] for link in links})
    out = []
    for i in range(0, len(ids), CHUNK):
        query = urllib.parse.urlencode({"id": "in.(" + ",".join(ids[i:i + CHUNK]) + ")",
                                        "select": "id,job_id,task_name,task_content_hash,rewards,exception_type,"
                                                  "trajectory_path,status"})
        out += get(f"trial?{query}")
    if len(out) != len(ids):
        raise ValueError(f"row {row['id']}: {len(ids)} linked trials but {len(out)} readable")
    return [{**condition(row), "trial_id": t["id"], "job_id": t["job_id"], "task_id": t["task_name"].split("/")[-1],
             "task_name": t["task_name"], "task_content_hash": t["task_content_hash"],
             "reward": (t.get("rewards") or {}).get("reward"), "exception_type": t.get("exception_type"),
             "trajectory_path": t.get("trajectory_path"), "status": t.get("status")} for t in out]


def task_versions(package_id: str, board_id: str) -> tuple[dict, list[dict]]:
    link = get(f"leaderboard_dataset_version?select=dataset_version_id&leaderboard_id=eq.{board_id}")
    if len(link) != 1:
        raise ValueError("the leaderboard must pin exactly one dataset version")
    version = get(f"dataset_version?select=id,package_id,revision,published_at,content_hash"
                  f"&id=eq.{link[0]['dataset_version_id']}")[0]
    if version["package_id"] != package_id:
        raise ValueError("leaderboard dataset version belongs to another package")
    fields = ("task_version(id,revision,content_hash,environment_config,verifier_config,agent_config,"
              "multi_step_reward_strategy,required_env,package(name))")
    tasks = get(f"dataset_version_task?select=task_version_id,{fields}&dataset_version_id=eq.{version['id']}&limit=1000")
    out = []
    for item in tasks:
        tv = item["task_version"]
        out.append({"task_id": tv["package"]["name"], "task_version_id": tv["id"], "revision": tv["revision"],
                    "content_hash": tv["content_hash"], "environment_config": tv["environment_config"],
                    "verifier_config": tv["verifier_config"], "agent_config": tv["agent_config"],
                    "multi_step_reward_strategy": tv["multi_step_reward_strategy"], "required_env": tv["required_env"]})
    return version, sorted(out, key=lambda item: item["task_id"])


def exclusion(task: dict) -> str | None:
    env = task["environment_config"] or {}
    verifier_env = (task["verifier_config"] or {}).get("environment") or {}
    if env.get("gpus") or env.get("tpu") or verifier_env.get("gpus") or verifier_env.get("tpu"):
        return "needs_gpu_or_tpu"
    if env.get("mcp_servers"):
        return "needs_mcp_sidecar_service"
    if task["multi_step_reward_strategy"]:
        return "multi_step_task"
    if task["required_env"]:
        return "needs_extra_environment_variables"
    return None


def select(trials: list[dict], tasks: list[dict]) -> list[dict]:
    hashes = {task["task_id"]: task["content_hash"] for task in tasks}
    by_task = defaultdict(list)
    for trial in trials:
        if (trial["model"], trial["agent"], trial["effort"]) == (SELECT["model"], SELECT["agent"], SELECT["effort"]):
            by_task[trial["task_id"]].append(trial)
    rows = []
    for task in tasks:
        own = by_task.get(task["task_id"], [])
        scored = [t for t in own if t["reward"] is not None and t["task_content_hash"] == hashes[task["task_id"]]]
        passes = sum(float(t["reward"]) > 0 for t in scored)
        rate = passes / len(scored) if scored else None
        excluded = exclusion(task)
        rows.append({"task_id": task["task_id"], "revision": task["revision"], "content_hash": task["content_hash"],
                     "official_sol_trials": len(own), "official_sol_scored": len(scored), "official_sol_passes": passes,
                     "official_sol_rate": rate, "hard": rate is not None and rate < THRESHOLD,
                     "excluded": excluded, "selected": rate is not None and rate < THRESHOLD and excluded is None,
                     "agent_timeout_sec": (task["agent_config"] or {}).get("timeout_sec"),
                     "verifier_timeout_sec": (task["verifier_config"] or {}).get("timeout_sec"),
                     "verifier_environment_mode": (task["verifier_config"] or {}).get("environment_mode")})
    return rows


def failure_sources(trials: list[dict], tasks: list[dict], selected: set[str]) -> tuple[list[dict], dict]:
    hashes = {task["task_id"]: task["content_hash"] for task in tasks}
    seen, rows, counts = set(), [], Counter()
    for trial in sorted(trials, key=lambda t: (t["task_id"], t["trial_id"])):
        if trial["task_id"] not in selected or trial["trial_id"] in seen:
            continue
        seen.add(trial["trial_id"])
        if trial["reward"] is None:
            counts["unscored"] += 1
            continue
        if float(trial["reward"]) > 0:
            counts["success_excluded"] += 1
            continue
        if trial["task_content_hash"] != hashes[trial["task_id"]]:
            counts["other_task_version"] += 1
            continue
        counts["failed"] += 1
        rows.append({key: trial[key] for key in ("task_id", "trial_id", "job_id", "row_id", "model", "agent", "effort",
                                                  "exception_type", "trajectory_path", "task_content_hash")})
    return rows, dict(counts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replace", action="store_true", help="refresh an existing frozen snapshot")
    args = parser.parse_args(argv)
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    board, rows = leaderboard_rows()
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        per_row = list(pool.map(row_trials, rows))
    trials = sorted((t for group in per_row for t in group), key=lambda t: (t["row_id"], t["trial_id"]))
    version, tasks = task_versions(board["package_id"], board["id"])
    selection = select(trials, tasks)
    chosen = {row["task_id"] for row in selection if row["selected"]}
    sources, counts = failure_sources(trials, tasks, chosen)
    provenance = {"retrieved_at": retrieved, "hub": "Harbor Hub public REST tables (anonymous publishable key)",
                  "leaderboard": {k: board[k] for k in ("id", "name", "title", "package_id")},
                  "dataset_version": version}
    paths = [
        save("official_leaderboard.json", {**provenance, "rows": rows}, replace=args.replace),
        save("official_trials.json", {**provenance, "trial_count": len(trials), "trials": trials}, replace=args.replace),
        save("tb4_tasks.json", {**provenance, "task_count": len(tasks), "tasks": tasks}, replace=args.replace),
    ]
    snapshot = {path.name: sha(path) for path in paths}
    paths.append(save("v8_tasks.json", {**provenance, "inputs_sha256": snapshot, "selection_condition": SELECT,
                                        "threshold": f"official success rate < {THRESHOLD}",
                                        "tasks": selection, "selected": sorted(chosen)}, replace=args.replace))
    paths.append(save("v8_failure_sources.json", {**provenance, "inputs_sha256": snapshot, "counts": counts,
                                                  "selection": "scored reward-0 trials of the selected tasks on the "
                                                               "leaderboard's exact task version, every condition",
                                                  "rows": sources}, replace=args.replace))
    hard = [row for row in selection if row["hard"]]
    print(json.dumps({"conditions": len(rows), "trials": len(trials), "tasks": len(tasks), "hard": len(hard),
                      "excluded": {row["task_id"]: row["excluded"] for row in hard if row["excluded"]},
                      "selected": len(chosen), "failure_sources": counts,
                      "written": [str(path.relative_to(BASE)) for path in paths]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
