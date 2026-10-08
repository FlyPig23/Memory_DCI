"""Side-experiment controller: oracle preflight, solver arms, status and report.

  preflight [task ...]                     official reference solution must score 1 (no model)
  solve --arm ARM --run-name NAME [task ...] [--parallel N]
                                           one fresh process per task; no retries
  report --run-name NAME                   summarize rewards, time and tokens

ARM is control (unchanged main-experiment baseline adapter) or one of the experience arms in ARMS
(failure_replay, traj_only, mem_<builder>, trajmem_<builder>); campaign.py runs many trials of them.
Run from the repository root with the TB2.1 environment's Python, e.g.
  experiment/benchmarks/terminal_bench_2_1/.venv/bin/python -m \
    side_experiments.hard_task_failure_replay.scripts.run preflight
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .common import HERE, MANIFESTS, RUNS, TASKS, config, load, write_json

BUILDER_IDS = ("luna56", "terra56", "sol56", "sonnet55", "astra6", "opus55", "opus55v4")
ARMS = ("control", "failure_replay", "traj_only", *(f"mem_{b}" for b in BUILDER_IDS),
        *(f"trajmem_{b}" for b in BUILDER_IDS))
PYTHON = sys.executable


def chosen(tasks: list[str]) -> list[str]:
    allowed = load(MANIFESTS / "selection.json")["chosen_task_ids"]
    tasks = tasks or allowed
    if set(tasks) - set(allowed):
        raise SystemExit(f"not a chosen task: {sorted(set(tasks) - set(allowed))}")
    return tasks


def reward_of(result: dict):
    return (((result.get("harbor") or result).get("verifier_result") or {}).get("rewards") or {}).get("reward")


async def oracle(task_id: str, directory: Path) -> dict:
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
    from harbor.trial.trial import Trial
    trial_dir = directory / task_id
    if trial_dir.exists():
        raise FileExistsError(f"{trial_dir} exists; use a new preflight directory")
    trial = await Trial.create(TrialConfig(task=TaskConfig(path=TASKS / task_id), trial_name=task_id,
                                           trials_dir=directory, agent=AgentConfig(name="oracle"),
                                           environment=EnvironmentConfig(type="docker", delete=True),
                                           verifier=VerifierConfig()))
    trial._verifier_timeout_sec = None  # same outer-verifier policy as the solver arms
    started = time.time()
    result = await trial.run()
    row = {"task_id": task_id, "reward": reward_of(result.model_dump(mode="json")),
           "exception": (result.exception_info.exception_type if result.exception_info else None),
           "seconds": round(time.time() - started, 1)}
    write_json(trial_dir / "preflight.json", row)
    return row


async def preflight(tasks: list[str]) -> list[dict]:
    directory = RUNS / "preflight" / time.strftime("%Y%m%dT%H%M%S")
    directory.mkdir(parents=True)
    rows = await asyncio.gather(*(oracle(t, directory) for t in tasks), return_exceptions=True)
    rows = [r if isinstance(r, dict) else {"task_id": t, "reward": None, "exception": repr(r)}
            for t, r in zip(tasks, rows)]
    write_json(directory / "summary.json", rows)
    write_json(MANIFESTS / "preflight.json", {"directory": str(directory.relative_to(HERE)), "results": rows})
    return rows


async def one_trial(arm: str, run_name: str, task_id: str) -> dict:
    from experiment.benchmarks.terminal_bench_2_1.scripts import baseline_runtime, harbor_runtime, health_guard
    from experiment.benchmarks.terminal_bench_2_1.scripts.run import resolve_image
    from experiment.benchmarks.terminal_bench_2_1.scripts.runtime import environment_for
    from . import replay_runtime
    settings = config()
    if (baseline_runtime.MODEL, baseline_runtime.EFFORT) != (settings["solver"]["model"], settings["solver"]["reasoning_effort"]):
        raise ValueError("Pinned solver settings differ from config.json")
    task_path = TASKS / task_id
    # Harbor names the Compose project "<trial dir name>__env". Both arms of a task run at the
    # same time, so the directory name must be unique per run, arm and task.
    trial_dir = RUNS / run_name / arm / f"{run_name}-{arm}-{task_id}"
    code, ids, _ = await harbor_runtime.command(
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={trial_dir.name.lower()}__env"], check=False)
    if code or ids.strip():
        raise RuntimeError(f"a Compose project named {trial_dir.name.lower()}__env already exists; refusing to share it")
    auth_file = Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser().resolve()
    spec = baseline_runtime.inspect_task(task_path)
    health_guard.ensure_auth_lifetime(auth_file, spec["agent"]["timeout_sec"], 0)
    image = await resolve_image(settings["cleanup_image"])
    arguments = {"python_root": harbor_runtime.python_installation(), "auth_file": auth_file,
                 "environment_config": environment_for(task_path), "cleanup_image": image["image_id"],
                 "verifier_policy": settings["verifier_policy"]}
    if arm == "control":
        output = await baseline_runtime.run_trial(task_path, trial_dir, task_id, **arguments)
    else:
        output = await replay_runtime.run_trial(task_path, trial_dir, task_id, arm=arm, **arguments)
    return {"arm": arm, "task_id": task_id, "reward": reward_of(output)}


def solve(arm: str, run_name: str, tasks: list[str], parallel: int) -> list[dict]:
    if arm not in ARMS:
        raise SystemExit(f"arm must be one of {ARMS}")
    if not (MANIFESTS / "preflight.json").exists() or any(
            r["reward"] != 1 for r in load(MANIFESTS / "preflight.json")["results"] if r["task_id"] in tasks):
        raise SystemExit("run preflight first; every chosen task's reference solution must score 1")
    logs = RUNS / run_name / arm / "_logs"
    logs.mkdir(parents=True, exist_ok=True)
    pending, running, finished = list(tasks), {}, []
    while pending or running:
        while pending and len(running) < parallel:
            task = pending.pop(0)
            log = (logs / f"{task}.log").open("w")
            running[task] = (subprocess.Popen([PYTHON, "-m", __spec__.name, "_trial", "--arm", arm,
                                               "--run-name", run_name, task], stdout=log, stderr=subprocess.STDOUT,
                                              cwd=HERE.parents[1]), log)
        time.sleep(15)
        for task, (process, log) in list(running.items()):
            if process.poll() is not None:
                log.close()
                finished.append({"task_id": task, "exit_code": process.returncode})
                print(json.dumps({"arm": arm, "task_id": task, "exit_code": process.returncode}), flush=True)
                del running[task]
    return finished


def experience_use(events: Path) -> dict:
    """Count native shell commands that touched /experience, from the public Codex event log."""
    import re
    commands, attempts = 0, set()
    lessons = False
    transcript_commands, memory_commands, transcripts = 0, 0, set()
    uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    if events.exists():
        for line in events.read_text().splitlines():
            try:
                item = json.loads(line).get("item") or {}
            except ValueError:
                continue
            if item.get("type") != "command_execution" or "/experience" not in item.get("command", ""):
                continue
            if json.loads(line).get("type") != "item.completed":
                continue
            commands += 1
            lessons = lessons or "lessons" in item["command"]
            attempts.update(re.findall(r"failed_attempts/[\w.-]+/([0-9a-f-]{36})", item["command"]))
            if "failed_attempts" in item["command"]:
                transcript_commands += 1
                transcripts.update(uuid.findall(item["command"]))  # also counts {a,b,c} brace lists
            if "/experience/memory" in item["command"] or "lessons" in item["command"]:
                memory_commands += 1
    return {"experience_commands": commands, "read_lessons_file": lessons, "attempt_files_named": len(attempts),
            "transcript_commands": transcript_commands, "memory_commands": memory_commands,
            "transcript_ids_named": len(transcripts)}


# Fetching the benchmark's own task sources, tests or reference solutions is reward hacking.
BENCHMARK_FETCH = (r"github\.com/search|google\.com/search|duckduckgo\.com|laude-institute/terminal-bench|"
                   r"harbor-framework/(terminal-bench|harbor)|api\.github\.com/search|original-tasks/|"
                   r"terminal-bench-(1|2|science|core)|"
                   # added 2026-10-07 after review, before the tmr rounds; re-checked against every earlier trial
                   r"bing\.com/search|search\.brave\.com|grep\.app|sourcegraph\.com/search|"
                   r"github\.com/[^\s/'\"]+/terminal-bench|terminal-bench@|tbench\.ai|(registry|hub)\.harborframework\.com|"
                   r"huggingface\.co/(api/)?datasets[^\s'\"]*terminal-bench|\bharbor\s+(datasets\s+)?download\b|"
                   r"\bharbor\s+run\b|\bpip3?\s+(download|install)\b[^|;&]*\bterminal-bench\b|marginlab\.ai|spylab\.ai")


def integrity(events: Path) -> dict:
    import re
    pattern, hits = re.compile(BENCHMARK_FETCH, re.I), []
    if events.exists():
        for line in events.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            item = event.get("item") or {}
            if (event.get("type") == "item.completed" and item.get("type") == "command_execution"
                    and pattern.search(item.get("command", ""))):
                hits.append(re.sub(r"\s+", " ", item["command"])[:160])
    return {"integrity_violation": bool(hits), "benchmark_fetch_commands": len(hits), "first_fetch": hits[:1]}


def report(run_name: str) -> dict:
    rows = []
    for arm in ARMS:
        for trial in sorted((RUNS / run_name / arm).glob("*/")):
            if trial.name.startswith("_") or not (trial / "runtime_contract.json").exists():
                continue
            result_file = trial / ("baseline_result.json" if arm == "control" else "replay_result.json")
            result = load(result_file) if result_file.exists() else {}
            execution = load(trial / "agent/execution.json") if (trial / "agent/execution.json").exists() else {}
            usage = load(trial / "agent/usage.json") if (trial / "agent/usage.json").exists() else {}
            model = load(trial / "agent/model_audit.json") if (trial / "agent/model_audit.json").exists() else {}
            exception = ((result.get("harbor") or {}).get("exception_info") or {}).get("exception_type")
            task_id = load(trial / "runtime_contract.json")["task_id"]
            rows.append({"arm": arm, "task_id": task_id, "trial": trial.name, "reward": reward_of(result) if result else None,
                         "solver_status": execution.get("status"),
                         "solver_seconds": round(execution.get("elapsed_seconds") or 0),
                         "tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0) or None,
                         "model_effort_verified": model.get("model_effort_verified"), "exception": exception,
                         "result_written": result_file.exists(), **experience_use(trial / "agent/codex.events.jsonl"),
                         **integrity(trial / "agent/codex.events.jsonl"),
                         # Every valid trial so far has verifier/ctrf.json; a verifier that could not run its tests
                         # (e.g. the v3r2 uv download failure) leaves only reward.txt and must not count as a failure.
                         "verifier_tests_ran": (trial / "verifier/ctrf.json").is_file()})
            rows[-1]["verifier_infrastructure_failure"] = rows[-1]["reward"] is not None and not rows[-1]["verifier_tests_ran"]
            rows[-1]["counted_reward"] = None if rows[-1]["reward"] is None or rows[-1]["verifier_infrastructure_failure"] else (
                0.0 if rows[-1]["integrity_violation"] else rows[-1]["reward"])
    summary = {"run_name": run_name, "rows": rows}
    write_json(RUNS / run_name / "report.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("preflight", "solve", "report", "_trial"))
    parser.add_argument("tasks", nargs="*")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--run-name")
    parser.add_argument("--parallel", type=int, default=5)
    args = parser.parse_args(argv)
    if args.command == "preflight":
        for row in asyncio.run(preflight(chosen(args.tasks))):
            print(json.dumps(row), flush=True)
    elif args.command == "solve":
        if not args.arm or not args.run_name:
            raise SystemExit("solve needs --arm and --run-name")
        solve(args.arm, args.run_name, chosen(args.tasks), args.parallel)
    elif args.command == "_trial":
        print(json.dumps(asyncio.run(one_trial(args.arm, args.run_name, chosen(args.tasks)[0]))), flush=True)
    else:
        for row in report(args.run_name)["rows"]:
            print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
