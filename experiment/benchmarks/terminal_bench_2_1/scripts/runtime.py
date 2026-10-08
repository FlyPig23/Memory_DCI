"""Portable TB2.1 entry point: official task limits and task-local V5 memory."""
from __future__ import annotations

import json
from pathlib import Path

from . import harbor_runtime as shared
from .protocol import finite_positive, inspect_task

MODEL, EFFORT, CLI_VERSION = shared.MODEL, shared.EFFORT, shared.CLI_VERSION
REVIEW_SECONDS = shared.REVIEW_SECONDS
HARNESS_IMPORT = "experiment.benchmarks.terminal_bench_2_1.scripts.harbor_runtime"
BASE = Path(__file__).resolve().parents[1]


def python_installation():
    """The reused interpreter is an explicit, content-frozen runtime dependency."""
    return shared.python_installation()


def environment_for(task_path):
    config = inspect_task(task_path)
    # Minimal official images can lack trust roots. Supply only the Codex
    # transport bundle without editing the task image or system certificate store.
    transport = BASE / 'prepared/transport/docker-compose-ca.yaml'
    if not transport.is_file():
        raise ValueError('Pinned Codex TLS transport bundle is missing')
    result = {'extra_docker_compose': [str(transport)]}
    environments = [config.get("environment", {})]
    verifier = config.get("verifier", {}).get("environment")
    if verifier:
        environments.append(verifier)
    if any(e.get("gpus", 0) for e in environments):
        raise ValueError("The fixed 36-task release supports CPU task environments only")
    return result


async def run_trial(task_path, trial_dir, pool_path, task_id, *, python_root=None,
                    auth_file=None, review_image, environment_config=None, verifier_policy="official", memory_seed=None):
    """One fresh V5 rollout; the shared runtime refuses existing directories."""
    inspect_task(task_path)
    pool = Path(pool_path)
    manifest = json.loads((pool / "manifest.json").read_text())
    training = set(manifest["training_task_ids"])
    if task_id in training or (pool / "tasks" / f"{task_id}.md").exists():
        raise ValueError("Held-out task occurs in the training pool")
    if manifest.get("cross_task_memory") is not False:
        raise ValueError("Pool must explicitly disable cross-task memory")
    return await shared.run_trial(
        task_path, trial_dir, pool, task_id,
        python_root=python_root or python_installation(), auth_file=auth_file,
        review_image=review_image, environment_config=environment_config, verifier_policy=verifier_policy,
        memory_seed=memory_seed)
