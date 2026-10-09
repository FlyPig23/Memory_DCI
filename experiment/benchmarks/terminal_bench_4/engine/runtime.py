"""Portable TB2.1 entry point: official task limits and task-local V5 memory."""
from __future__ import annotations

import json
from pathlib import Path

from . import harbor_runtime as shared
from .protocol import finite_positive, inspect_task

MODEL, EFFORT, CLI_VERSION = shared.MODEL, shared.EFFORT, shared.CLI_VERSION
REVIEW_SECONDS = shared.REVIEW_SECONDS
HARNESS_IMPORT = "experiment.benchmarks.terminal_bench_4.engine.harbor_runtime"
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
