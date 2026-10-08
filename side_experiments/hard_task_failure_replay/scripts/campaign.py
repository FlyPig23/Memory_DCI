"""Run rounds of arms x tasks with a concurrency cap; one fresh process per trial.

Jobs are ordered round by round, and within a round task by task, so all arms of a
task start close together and see similar provider latency. A memory arm starts only
after its failure analysis exists. Detach it so it outlives the starting shell:
  setsid nohup <venv python> -m side_experiments.hard_task_failure_replay.scripts.campaign \
    --prefix v3r --rounds 3 --arms traj_only mem_luna56 mem_terra56 mem_sol56 mem_sonnet55 \
    --extra-first-round control > runs/campaign_v3.log 2>&1 < /dev/null &
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from .common import CORPUS, HERE, MANIFESTS, RUNS, load
from .run import ARMS


def ready(arm: str, task: str) -> bool:
    builder = arm[4:] if arm.startswith("mem_") else arm[8:] if arm.startswith("trajmem_") else None
    return builder is None or (CORPUS / task / "memories" / builder / "lessons.md").exists()


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S', time.gmtime())} {message}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--first-round", type=int, default=1)
    parser.add_argument("--arms", nargs="+", required=True, choices=ARMS)
    parser.add_argument("--extra-first-round", nargs="*", default=[], choices=ARMS)
    parser.add_argument("--tasks", nargs="*")
    parser.add_argument("--parallel", type=int, default=15)
    parser.add_argument("--wait-minutes", type=float, default=150)
    args = parser.parse_args(argv)
    tasks = args.tasks or load(MANIFESTS / "selection.json")["chosen_task_ids"]
    preflight = {r["task_id"]: r["reward"] for r in load(MANIFESTS / "preflight.json")["results"]}
    if any(preflight.get(t) != 1 for t in tasks):
        raise SystemExit("every task's reference solution must pass the oracle preflight")
    jobs = []
    for number in range(args.first_round, args.first_round + args.rounds):
        arms = (args.extra_first_round if number == args.first_round else []) + args.arms
        jobs += [(f"{args.prefix}{number}", arm, task) for task in tasks for arm in arms]
    log(f"PLAN {len(jobs)} trials, parallel {args.parallel}")
    pending, running, give_up = list(jobs), {}, time.time() + args.wait_minutes * 60
    while pending or running:
        for job in list(pending):
            if len(running) >= args.parallel:
                break
            run, arm, task = job
            if not ready(arm, task):
                if time.time() > give_up:
                    pending.remove(job)
                    log(f"SKIP {run} {arm} {task}: failure analysis missing")
                continue
            if (RUNS / run / arm / f"{run}-{arm}-{task}").exists():
                pending.remove(job)
                log(f"EXISTS {run} {arm} {task}")
                continue
            logfile = RUNS / run / arm / "_logs" / f"{task}.log"
            logfile.parent.mkdir(parents=True, exist_ok=True)
            running[job] = subprocess.Popen(
                [sys.executable, "-m", "side_experiments.hard_task_failure_replay.scripts.run", "_trial",
                 "--arm", arm, "--run-name", run, task],
                stdout=logfile.open("w"), stderr=subprocess.STDOUT, cwd=HERE.parents[1])
            pending.remove(job)
            log(f"LAUNCH {run} {arm} {task}")
        time.sleep(10)
        for job, process in list(running.items()):
            if process.poll() is not None:
                log(f"DONE {job[0]} {job[1]} {job[2]} exit={process.returncode}")
                del running[job]
    log("ALL_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
