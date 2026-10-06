#!/usr/bin/env python3
"""Restore verified Memory DCI inputs and optional lean experiment evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
DATASET = "FlyPig23/memory_dci"
SUBDIRECTORY = "wildclaw_bench"
RELEASE_REVISION = "5cd1cf968d544bd8cb619b1fccc3265704fcc85d"
RELEASE_MANIFEST_SHA256 = "3d9ae5c8ca9a4d0bfb9658299929a85b9e42da6a038bf5dd4ff77a6a28a0c862"
BENCHMARK = "https://github.com/InternLM/WildClawBench.git"
BENCHMARK_COMMIT = "316334ccc4a87b9b5635ad73da99b4dfc0b3887e"


from experiment.shared.dataset_io import sha256, _contained, download, restore


def stage_test_inputs(root):
    split = json.loads((root / "experiment/benchmarks/wildclaw_bench/manifests/split.json").read_text())
    tasks = json.loads((root / "experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json").read_text())["tasks"]
    snapshot = root / "experiment/benchmarks/wildclaw_bench/trajectory library/WildClawBench/task/hf_snapshot"
    for task in tasks:
        if task["task_id"] not in split["test_task_ids"]:
            continue
        relative = PurePosixPath(task["public_workspace_relpath"])
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                or relative.parts[0] != "workspace"):
            raise ValueError("Unsafe public workspace path in task metadata")
        _contained(snapshot, root)
        source = _contained(snapshot.joinpath(*relative.parts), snapshot)
        target = _contained(root / "experiment/benchmarks/wildclaw_bench/runtime/task_inputs" / Path(*relative.parts[1:]), root)
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Unexpected link in task workspace")
        _contained(target / "exec", root).mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob("*")):
            relative_input = path.relative_to(source)
            if relative_input.parts[0] == "gt":
                continue
            _contained(path, source)
            if path.is_symlink():
                raise ValueError("Unexpected link in public task input")
            if not path.is_file():
                continue
            destination = _contained(target / relative_input, root)
            if destination.is_symlink():
                raise ValueError("Unexpected link in public task input")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if sha256(destination) != sha256(path):
                    raise ValueError(f"Existing task input differs: {destination}")
            else:
                shutil.copy2(path, destination)
        ground_truth = _contained(source / "gt", snapshot)
        target_gt = target / "gt"
        _contained(target_gt.parent, root)
        if ground_truth.is_dir():
            if target_gt.is_symlink():
                if target_gt.resolve() != ground_truth.resolve():
                    raise ValueError("Existing grader-only ground-truth link differs")
            elif target_gt.exists():
                raise ValueError("Existing ground truth is not the expected isolated link")
            else:
                target_gt.symlink_to(ground_truth.resolve(), target_is_directory=True)


def fetch_benchmark(root):
    destination = root / "experiment/benchmarks/wildclaw_bench/vendor/WildClawBench"
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", BENCHMARK,
                        str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "checkout", "--detach", BENCHMARK_COMMIT], check=True)
    actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
    if actual != BENCHMARK_COMMIT:
        raise ValueError("Existing benchmark checkout is not the pinned revision")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repo-id", default=DATASET)
    parser.add_argument("--revision", default=RELEASE_REVISION, help="Defaults to the frozen layout publication; override with a dataset commit SHA")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--local-dataset", type=Path, help="Verify and restore an already downloaded/exported dataset")
    parser.add_argument("--include-evidence", action="store_true")
    parser.add_argument("--fetch-benchmark", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    cache = args.local_dataset or args.cache_dir or root / ".cache/memory_dci/wildclaw_bench"
    base = f"https://huggingface.co/datasets/{args.repo_id}/resolve/{urllib.parse.quote(args.revision, safe='')}/{SUBDIRECTORY}/"
    if not args.local_dataset:
        download(base + "manifest.json", cache / "manifest.json",
                 RELEASE_MANIFEST_SHA256 if args.revision == RELEASE_REVISION else None)
    manifest = json.loads((cache / "manifest.json").read_text())
    if (manifest.get("schema_version") != 1 or manifest.get("repo_id") != args.repo_id
            or manifest.get("layout_version") != 2 or manifest.get("dataset_subdirectory") != SUBDIRECTORY):
        raise ValueError("Unexpected dataset manifest")
    for row in manifest["files"]:
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.is_relative_to(PurePosixPath("experiment/benchmarks/wildclaw_bench")):
            raise ValueError("Dataset file is outside the WildClawBench directory")
    for bundle in ["inputs"] + (["evidence"] if args.include_evidence else []):
        name = bundle + ".tar.gz"
        archive = cache / name
        metadata = manifest["archives"][name]
        if not args.local_dataset:
            download(base + name, archive, metadata["sha256"])
        if archive.stat().st_size != metadata["bytes"] or sha256(archive) != metadata["sha256"]:
            raise ValueError(f"Archive checksum mismatch: {name}")
        rows = [r for r in manifest["files"] if r["bundle"] == bundle]
        restore(archive, root, rows)
        print(f"Restored and verified {len(rows)} files from {name}")
    stage_test_inputs(root)
    if args.fetch_benchmark:
        fetch_benchmark(root)
    print("Ready: 432 training trajectories, 381 images and 24 staged test tasks.")
    print("Build the V5 pool with: python3 -m experiment.benchmarks.wildclaw_bench.variants.dci_memory.pool")
    print("Historical evidence excludes raw sessions/workspaces; full original audits require the local originals.")


if __name__ == "__main__":
    main()
