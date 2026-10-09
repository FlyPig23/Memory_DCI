"""V8 runner: an oracle preflight, then three V7-runtime rollouts of every selected Terminal-Bench 4.0 task.

  plan     build and print the protocol (no Docker, no model)
  run      freeze the protocol on first use and run every pending job with bounded concurrency
  status   recompute every finished trial's validity and the pass rates; never runs anything
  _job     internal: one job in a fresh process (oracle or rollout)

Each task is opened in turn: its environment and verifier images are pulled once, Harbor's oracle agent runs the
official reference solution (it must score 1, otherwise the task's rollouts are skipped and reported), then the
three rollouts run with the unchanged V7 runtime (five DCI tools, this task's own failed official trajectories
in /pool, its frozen Opus failure analysis as /memory, the 180 s post-grading reviewer). The task's images are
removed once its jobs finish, and at most --open-tasks tasks hold images at a time. Official agent and verifier
timeouts are kept (verifier policy official), as on the official leaderboard. Rollouts are never retried; an
interrupted job leaves its directory for review instead of being rerun silently.
Usage (repository root, this benchmark's .venv):
  python -m experiment.benchmarks.terminal_bench_4.scripts.run_v8 run --run-name v8_... --corpus <build_id>
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

from experiment.benchmarks.terminal_bench_4.engine import harbor_runtime as shared
from experiment.benchmarks.terminal_bench_4.engine import health_guard, protocol, runtime
from experiment.benchmarks.terminal_bench_4.engine import run as engine_run

BASE = Path(__file__).resolve().parents[1]
ROLLOUTS = 3
SCRIPTS = ("v8_sources.py", "download_tasks.py", "v8_corpus.py", "run_v8.py")


def load(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_dir(run_name: str) -> Path:
    if not run_name or "/" in run_name or run_name.startswith("."):
        raise ValueError("Run name must be a plain directory name")
    return BASE / "runs" / run_name


def build_plan(run_name: str, corpus: str, *, rollouts: int = ROLLOUTS, verifier_policy: str = "official",
               oracle: bool = True, model: str | None = None, effort: str | None = None) -> dict:
    run_dir(run_name)
    if rollouts < 1:
        raise ValueError("rollouts must be at least 1")
    from . import v8_corpus
    v8_corpus.verify(corpus)
    root = BASE / "prepared/v8_corpus" / corpus
    index, seeds = load(root / "index.json"), load(root / "seeds_index.json")
    selection = load(BASE / "manifests/v8_tasks.json")
    rows = {row["task_id"]: row for row in selection["tasks"]}
    versions = {task["task_id"]: task for task in load(BASE / "manifests/tb4_tasks.json")["tasks"]}
    tasks = []
    for task_id in selection["selected"]:
        path = BASE / "prepared/tasks" / task_id
        spec = tomllib.loads((path / "task.toml").read_text())
        record, seed_row = index["tasks"][task_id], seeds["tasks"][task_id]
        seed = None
        if seed_row.get("seed_path"):
            seed_path = protocol.local_path(BASE, seed_row["seed_path"])
            seed = {"path": seed_row["seed_path"], "manifest_sha256": seed_row["manifest_sha256"],
                    "entry_count": seed_row["entry_count"], "files_sha256": protocol.inventory(seed_path)}
        version = versions[task_id]
        tasks.append({"task_id": task_id, "source_path": f"prepared/tasks/{task_id}",
                      "files_sha256": protocol.inventory(path),
                      "official_agent_timeout_seconds": spec["agent"]["timeout_sec"],
                      "official_verifier_timeout_seconds": spec["verifier"]["timeout_sec"],
                      "images": [version["environment_config"]["docker_image"],
                                 (version["verifier_config"].get("environment") or {}).get("docker_image")],
                      "official_sol": {"passes": rows[task_id]["official_sol_passes"],
                                       "trials": rows[task_id]["official_sol_scored"]},
                      "pool": {"path": record["pool_path"], "manifest_sha256": record["pool_manifest_sha256"],
                               "files_sha256": record["pool_files_sha256"], "trajectory_count": record["trajectory_count"]},
                      "memory_seed": seed, "memory_seed_reason": seed_row.get("reason")})
    settings = protocol.settings(BASE, model=model, effort=effort, verifier_policy=verifier_policy)
    sources = {**protocol.sources(BASE, "v7"),
               **{f"experiment/benchmarks/terminal_bench_4/scripts/{name}": sha(BASE / "scripts" / name) for name in SCRIPTS}}
    inputs = {f"manifests/{name}": sha(BASE / "manifests" / name)
              for name in ("v8_tasks.json", "tb4_tasks.json", "v8_failure_sources.json", "v8_download_audit.json")}
    inputs.update({f"prepared/v8_corpus/{corpus}/index.json": sha(root / "index.json"),
                   f"prepared/v8_corpus/{corpus}/seeds_index.json": sha(root / "seeds_index.json"),
                   "prepared/v8_corpus/decontamination_decisions.json": sha(BASE / "prepared/v8_corpus/decontamination_decisions.json")})
    jobs = []
    for task in tasks:
        if oracle:
            jobs.append({"job_id": f"{task['task_id']}__oracle", "kind": "oracle", "task_id": task["task_id"]})
        jobs += [{"job_id": f"{task['task_id']}__r{k}", "kind": "rollout", "task_id": task["task_id"], "rollout": k}
                 for k in range(1, rollouts + 1)]
    return {"schema_version": 1, "benchmark": "Terminal-Bench 4.0", "experiment": "V8", "run_name": run_name,
            "condition": "v7 runtime, same-task failed official trajectories + Opus 5.5 High failure analysis",
            "corpus": corpus, "rollouts": rollouts, "oracle_preflight": oracle, "settings": settings,
            "sources_sha256": sources, "inputs_sha256": inputs, "tasks": tasks, "jobs": jobs}


def extend_plan(parent: dict, fresh: dict, rollouts: int) -> dict:
    """Rollouts parent+1..rollouts of every task whose oracle passed in the parent run, under its frozen inputs.

    The tasks, pools, seeds, settings, inputs and the solver runtime must equal the parent's; only the
    run_v8.py controller may differ (this function itself). Tasks whose parent oracle failed stay excluded.
    """
    if rollouts <= parent["rollouts"]:
        raise ValueError("An extension adds rollouts beyond the parent run's")
    controller = "experiment/benchmarks/terminal_bench_4/scripts/run_v8.py"
    runtime = {k: v for k, v in parent["sources_sha256"].items() if k != controller}
    fresh_runtime = {k: v for k, v in fresh["sources_sha256"].items() if k != controller}
    if ((fresh["settings"], fresh["inputs_sha256"], fresh["tasks"]) != (parent["settings"], parent["inputs_sha256"], parent["tasks"])
            or fresh_runtime != runtime):
        raise ValueError("Tasks, inputs, settings or the solver runtime differ from the parent run")
    oracles = {j["task_id"]: job_state(parent, j) for j in parent["jobs"] if j["kind"] == "oracle"}
    if any(state not in ("passed", "oracle_failed") for state in oracles.values()):
        raise ValueError("Every parent oracle needs a verdict before the run is extended")
    kept = [t for t in parent["tasks"] if oracles.get(t["task_id"], "passed") == "passed"]
    jobs = [{"job_id": f"{t['task_id']}__r{k}", "kind": "rollout", "task_id": t["task_id"], "rollout": k}
            for t in kept for k in range(parent["rollouts"] + 1, rollouts + 1)]
    return {**fresh, "rollouts": rollouts, "oracle_preflight": False, "parent_run": parent["run_name"],
            "parent_protocol_sha256": sha(run_dir(parent["run_name"]) / "protocol.json"),
            "excluded_by_parent_oracle": sorted(t for t, state in oracles.items() if state == "oracle_failed"),
            "tasks": kept, "jobs": jobs}


# ----- one job (fresh process) -----

async def oracle_job(plan: dict, job: dict) -> dict:
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
    from harbor.trial.trial import Trial
    task = next(t for t in plan["tasks"] if t["task_id"] == job["task_id"])
    directory = run_dir(plan["run_name"]) / "oracle"
    trial_dir = directory / job["job_id"]
    if trial_dir.exists():
        raise FileExistsError(f"{trial_dir} exists; oracle jobs are not rerun silently")
    started = time.time()
    trial = await Trial.create(TrialConfig(task=TaskConfig(path=BASE / task["source_path"]), trial_name=job["job_id"],
                                           trials_dir=directory, agent=AgentConfig(name="oracle"),
                                           environment=EnvironmentConfig(type="docker", delete=True),
                                           verifier=VerifierConfig()))
    result = (await trial.run()).model_dump(mode="json")
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    row = {"job_id": job["job_id"], "task_id": task["task_id"], "reward": reward,
           "exception": (result.get("exception_info") or {}).get("exception_type"),
           "seconds": round(time.time() - started, 1), "passed": reward == 1}
    # No reward means no verdict (e.g. an image pull failed): leave the task for review, never mark it failed
    write_json(trial_dir / ("oracle.json" if reward is not None else "oracle_error.json"), row)
    return row


async def rollout_job(plan: dict, job: dict, auth_file: Path) -> dict:
    # Each job is a new process. Apply the frozen settings before the solver or
    # reviewer builds its configuration; the result audit checks the actual model.
    for module in (shared, runtime):
        module.MODEL = plan["settings"]["model"]
        module.EFFORT = plan["settings"]["reasoning_effort"]
        module.CLI_VERSION = plan["settings"]["cli_version"]
    task = next(t for t in plan["tasks"] if t["task_id"] == job["task_id"])
    trial_dir = run_dir(plan["run_name"]) / "trials" / job["job_id"]
    path = BASE / task["source_path"]
    if protocol.inventory(path) != task["files_sha256"]:
        raise ValueError("Task package changed after the protocol was frozen")
    pool = protocol.local_path(BASE, task["pool"]["path"])
    if protocol.inventory(pool) != task["pool"]["files_sha256"]:
        raise ValueError("V8 task pool changed after the protocol was frozen")
    seed = None
    if task["memory_seed"]:
        seed = protocol.local_path(BASE, task["memory_seed"]["path"])
        if protocol.inventory(seed) != task["memory_seed"]["files_sha256"]:
            raise ValueError("V8 task seed changed after the protocol was frozen")
    health_guard.ensure_auth_lifetime(auth_file, task["official_agent_timeout_seconds"], shared.REVIEW_SECONDS)
    image = await engine_run.resolve_image(plan["settings"]["review_image"], None)
    await shared.run_trial(path, trial_dir, pool, task["task_id"], python_root=shared.python_installation(),
                           auth_file=auth_file, review_image=image["image_id"],
                           environment_config=runtime.environment_for(path),
                           verifier_policy=plan["settings"]["verifier_policy"], memory_seed=seed, condition="v7")
    row = summary(plan, job)
    write_json(trial_dir / "completion.json", row)
    return row


def summary(plan: dict, job: dict) -> dict:
    task = next(t for t in plan["tasks"] if t["task_id"] == job["task_id"])
    trial_dir = run_dir(plan["run_name"]) / "trials" / job["job_id"]
    row = engine_run.summary(trial_dir, "v7", task=task, settings=plan["settings"], memory_seed=task["memory_seed"])
    return {**row, "job_id": job["job_id"], "task_id": task["task_id"], "rollout": job["rollout"]}


# ----- scheduling -----

def job_state(plan: dict, job: dict) -> str:
    root = run_dir(plan["run_name"])
    if job["kind"] == "oracle":
        directory = root / "oracle" / job["job_id"]
        if (directory / "oracle.json").is_file():
            return "passed" if load(directory / "oracle.json")["passed"] else "oracle_failed"
    else:
        directory = root / "trials" / job["job_id"]
        if (directory / "completion.json").is_file():
            return "done"
    return "incomplete" if directory.exists() else "pending"


def remove_images(task: dict) -> None:
    for image in filter(None, task["images"]):
        subprocess.run(["docker", "image", "rm", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def free_gb() -> float:
    return shutil.disk_usage("/var/lib/docker" if Path("/var/lib/docker").exists() else "/").free / 1e9


def spawn_job(plan: dict, job: dict, auth_file: Path, log):
    return subprocess.Popen([sys.executable, "-m", __spec__.name, "_job", "--run-name", plan["run_name"], "--job",
                             job["job_id"], "--auth-file", str(auth_file)], stdout=log, stderr=subprocess.STDOUT,
                            cwd=BASE.parents[2])


def execute(plan: dict, *, workers: int, open_tasks: int, auth_file: Path, min_free_gb: float = 20.0,
            spawn=spawn_job, sleep=time.sleep, disk=free_gb, cleanup=remove_images) -> None:
    if workers < 1 or open_tasks < 1:
        raise ValueError("workers and open_tasks must both be at least 1")
    root = run_dir(plan["run_name"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = root / "protocol.json"
        if saved.exists() and load(saved) != plan:
            raise ValueError("Protocol, inputs or code changed since this run was frozen; choose a new run name")
        if not saved.exists():
            write_json(saved, plan)
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        order = [t["task_id"] for t in plan["tasks"]]
        tasks = {t["task_id"]: t for t in plan["tasks"]}
        jobs = {t: [j for j in plan["jobs"] if j["task_id"] == t] for t in order}
        running, opened, closed = {}, [], set()
        while True:
            states = {j["job_id"]: job_state(plan, j) for j in plan["jobs"]}

            def ready(task_id):
                return all(states[j["job_id"]] == "passed" for j in jobs[task_id] if j["kind"] == "oracle")

            def blocked(task_id):  # a failed oracle, or one that stopped without a verdict, never releases rollouts
                return any(j["kind"] == "oracle" and j["job_id"] not in running
                           and states[j["job_id"]] in ("oracle_failed", "incomplete") for j in jobs[task_id])

            def finished(task_id):
                return not any(j["job_id"] in running for j in jobs[task_id]) and (
                    blocked(task_id) or all(states[j["job_id"]] != "pending" for j in jobs[task_id]))

            for task_id in list(opened):  # close a finished task and release its images
                if finished(task_id):
                    cleanup(tasks[task_id])
                    opened.remove(task_id)
                    closed.add(task_id)
            for task_id in order:  # open tasks in order while there is room
                if task_id in opened or task_id in closed or len(opened) >= open_tasks:
                    continue
                if finished(task_id):
                    closed.add(task_id)
                    continue
                if disk() < min_free_gb:
                    break
                opened.append(task_id)
            launchable = [job for task_id in opened for job in jobs[task_id]
                          if job["job_id"] not in running and states[job["job_id"]] == "pending"
                          and (job["kind"] == "oracle" or ready(task_id))]
            for job in launchable[:max(0, workers - len(running))]:
                log = (logs / f"{job['job_id']}.log").open("w")
                running[job["job_id"]] = (spawn(plan, job, auth_file, log), log)
                print(json.dumps({"event": "launch", "job": job["job_id"], "at": time.time()}), flush=True)
            for job_id, (process, log) in list(running.items()):
                if process.poll() is not None:
                    log.close()
                    del running[job_id]
                    print(json.dumps({"event": "exit", "job": job_id, "code": process.returncode,
                                      "state": job_state(plan, next(j for j in plan["jobs"] if j["job_id"] == job_id))}),
                          flush=True)
            pending = [j for j in plan["jobs"] if states[j["job_id"]] == "pending" and j["job_id"] not in running
                       and not blocked(j["task_id"])]
            if not running and not pending:
                for task_id in opened:
                    cleanup(tasks[task_id])
                break
            if not running and not opened and pending:
                raise RuntimeError(f"Less than {min_free_gb} GB free for Docker images; no task can be opened")
            sleep(15)


def status(run_name: str) -> dict:
    root = run_dir(run_name)
    plan = load(root / "protocol.json")
    tasks, rows, totals = {}, [], Counter()
    for task in plan["tasks"]:
        oracle = next((j for j in plan["jobs"] if j["task_id"] == task["task_id"] and j["kind"] == "oracle"), None)
        oracle_state = job_state(plan, oracle) if oracle else "skipped"
        rewards = []
        for job in (j for j in plan["jobs"] if j["task_id"] == task["task_id"] and j["kind"] == "rollout"):
            state = job_state(plan, job)
            if state == "done":
                row = summary(plan, job)
                saved = load(root / "trials" / job["job_id"] / "completion.json")
                if row != saved:
                    row = {**row, "valid": False, "reasons": [*row.get("reasons", []), "completion_mismatch"]}
                rows.append(row)
                rewards.append(row["reward"] if row["valid"] else None)
            totals[state] += 1
        tasks[task["task_id"]] = {"oracle": oracle_state, "rollouts": rewards,
                                  "passes": sum(1 for r in rewards if r == 1), "official_sol": task["official_sol"]}
    if plan.get("parent_run"):  # an extension reports parent and added rollouts together, under the parent's oracles
        parent = status(plan["parent_run"])
        tasks = {task_id: {**row, "rollouts": row["rollouts"] + tasks.get(task_id, {"rollouts": []})["rollouts"]}
                 for task_id, row in parent["tasks"].items()}
        for row in tasks.values():
            row["passes"] = sum(1 for r in row["rollouts"] if r == 1)
        rows += [{**r, "valid": False} for r in parent["invalid"]]
        totals.update(parent["jobs"])
    counted = [t for t in tasks.values() if t["oracle"] in ("passed", "skipped")]
    scored = [r for t in counted for r in t["rollouts"] if r is not None]
    return {"run_name": run_name, "jobs": dict(totals), "tasks_with_oracle_pass": len(counted),
            "oracle_failed": sorted(k for k, t in tasks.items() if t["oracle"] == "oracle_failed"),
            "oracle_without_verdict": sorted(k for k, t in tasks.items() if t["oracle"] == "incomplete"),
            "valid_rollouts": len(scored), "passed_rollouts": sum(1 for r in scored if r == 1),
            "pass_rate": round(sum(1 for r in scored if r == 1) / len(scored), 4) if scored else None,
            "official_sol_on_same_tasks": {"passes": sum(t["official_sol"]["passes"] for t in counted),
                                           "trials": sum(t["official_sol"]["trials"] for t in counted)},
            "tasks": tasks, "invalid": [r for r in rows if not r["valid"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "run", "status", "_job"))
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--corpus", help="prepared/v8_corpus/<build_id>, with exported seeds")
    parser.add_argument("--rollouts", type=int, default=ROLLOUTS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--open-tasks", type=int, default=2)
    parser.add_argument("--no-oracle", action="store_true")
    parser.add_argument("--extends", help="parent run: add rollouts up to --rollouts under its frozen inputs")
    parser.add_argument("--job")
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--model", help="Solver/reviewer model; default from config.json")
    parser.add_argument("--effort", help="Solver/reviewer reasoning effort; default from config.json")
    args = parser.parse_args(argv)
    auth = (args.auth_file or Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json"))).expanduser().resolve()
    if args.command == "status":
        print(json.dumps(status(args.run_name), indent=2))
        return 0
    if args.command == "_job":
        plan = load(run_dir(args.run_name) / "protocol.json")
        job = next(j for j in plan["jobs"] if j["job_id"] == args.job)
        if job["kind"] == "oracle":
            print(json.dumps(asyncio.run(oracle_job(plan, job))))
        else:
            print(json.dumps(asyncio.run(rollout_job(plan, job, auth))))
        return 0
    plan = build_plan(args.run_name, args.corpus, rollouts=args.rollouts, oracle=not args.no_oracle,
                      model=args.model, effort=args.effort)
    if args.extends:
        plan = extend_plan(load(run_dir(args.extends) / "protocol.json"), plan, args.rollouts)
    if args.command == "plan":
        print(json.dumps({k: v for k, v in plan.items() if k not in ("tasks", "jobs")}, indent=2))
        print(json.dumps({"tasks": len(plan["tasks"]), "jobs": len(plan["jobs"])}))
        return 0
    execute(plan, workers=args.workers, open_tasks=args.open_tasks, auth_file=auth)
    print(json.dumps({k: v for k, v in status(args.run_name).items() if k not in ("tasks", "invalid")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
