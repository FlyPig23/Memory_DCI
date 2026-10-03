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
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATASET = "FlyPig23/memory_dci"
BENCHMARK = "https://github.com/InternLM/WildClawBench.git"
BENCHMARK_COMMIT = "316334ccc4a87b9b5635ad73da99b4dfc0b3887e"


def sha256(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes destination: {path}")
    return path


def download(url, destination, expected=None):
    if destination.is_file() and expected and sha256(destination) == expected:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=120) as response:
            shutil.copyfileobj(response, output, 1024 * 1024)
        if expected and sha256(temporary) != expected:
            raise ValueError(f"Download checksum mismatch: {destination.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore(archive, root, records):
    expected = {row["path"]: row for row in records}
    if len(expected) != len(records):
        raise ValueError("Duplicate manifest paths")
    observed = set()
    with tarfile.open(archive, "r:gz") as handle:
        # This dataset only uses regular files: never restore archive links.
        for member in handle:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or member.name not in expected or member.name in observed):
                raise ValueError(f"Unexpected archive member: {member.name}")
            observed.add(member.name)
            row = expected[member.name]
            target = root.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
                raise ValueError(f"Unsafe destination: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if (not target.is_file() or target.stat().st_size != row["bytes"]
                        or sha256(target) != row["sha256"]):
                    raise ValueError(f"Existing file differs; use a fresh checkout/destination: {member.name}")
                continue
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore-part", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as output, handle.extractfile(member) as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if temporary.stat().st_size != row["bytes"] or sha256(temporary) != row["sha256"]:
                    raise ValueError(f"Restored file checksum mismatch: {member.name}")
                temporary.chmod(0o755 if member.mode & 0o111 else 0o644)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    if observed != set(expected):
        raise ValueError("Archive is missing manifest files")


def stage_test_inputs(root):
    split = json.loads((root / "experiment/manifests/split.json").read_text())
    tasks = json.loads((root / "experiment/manifests/task_manifest.json").read_text())["tasks"]
    snapshot = root / "experiment/trajectory library/WildClawBench/task/hf_snapshot"
    for task in tasks:
        if task["task_id"] not in split["test_task_ids"]:
            continue
        relative = PurePosixPath(task["public_workspace_relpath"])
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
                or relative.parts[0] != "workspace"):
            raise ValueError("Unsafe public workspace path in task metadata")
        _contained(snapshot, root)
        source = _contained(snapshot.joinpath(*relative.parts), snapshot)
        target = _contained(root / "experiment/runtime/task_inputs" / Path(*relative.parts[1:]), root)
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
    destination = root / "experiment/vendor/WildClawBench"
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
    parser.add_argument("--revision", default="main", help="Use a dataset commit SHA for an immutable download")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--local-dataset", type=Path, help="Verify and restore an already downloaded/exported dataset")
    parser.add_argument("--include-evidence", action="store_true")
    parser.add_argument("--fetch-benchmark", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    cache = args.local_dataset or args.cache_dir or root / ".cache/memory_dci"
    base = f"https://huggingface.co/datasets/{args.repo_id}/resolve/{urllib.parse.quote(args.revision, safe='')}/"
    if not args.local_dataset:
        download(base + "manifest.json", cache / "manifest.json")
    manifest = json.loads((cache / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("repo_id") != args.repo_id:
        raise ValueError("Unexpected dataset manifest")
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
    print("Build the V5 pool with: python3 -m experiment.variants.dci_memory.pool")
    print("Historical evidence excludes raw sessions/workspaces; full original audits require the local originals.")


if __name__ == "__main__":
    main()
