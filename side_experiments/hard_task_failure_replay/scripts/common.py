"""Paths and small helpers shared by the side-experiment scripts.

The side experiment reads the main TB2.1 package (task packages, historical
trials, pinned runtime) but never writes into it. Everything it produces stays
under this directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
TB = ROOT / "experiment/benchmarks/terminal_bench_2_1"
TASKS = TB / "prepared/tasks"
POOL = TB / "prepared/pool"
TRIALS = TB / "data/historical_trials"
CONFIG = HERE / "config.json"
MANIFESTS = HERE / "manifests"
CORPUS = HERE / "corpus"
RUNS = HERE / "runs"
PROMPTS = HERE / "prompts"


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load(path: Path):
    return json.loads(Path(path).read_text())


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def config() -> dict:
    return load(CONFIG)
