"""Download and verify the public TB2.1 inputs; never start experiments."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import urllib.parse

from experiment.shared.dataset_io import _contained, download, restore, sha256

ROOT = Path(__file__).resolve().parents[4]
PREFIX = Path("experiment/benchmarks/terminal_bench_2_1")
REPO = "FlyPig23/memory_dci"
SUBDIRECTORY = "terminal_bench_2_1"
RELEASE_REVISION = "5cd1cf968d544bd8cb619b1fccc3265704fcc85d"
RELEASE_MANIFEST_SHA256 = "07554e497a524c998a0fcb16bf3f3f629d613c03035507bea28e604b1a4b1d95"


def load(path):
    return json.loads(path.read_bytes())


def verify_dataset(root, *, without_pool=False):
    base = root / PREFIX
    split = load(base / "manifests/split.json")
    training, test = split["training_task_ids"], split["test_task_ids"]
    if (len(training) != len(set(training)) or len(training) != 53 or len(test) != len(set(test))
            or len(test) != 36 or set(training) & set(test)
            or {t["task_id"] for t in split["tasks"]} != set(training) | set(test)):
        raise ValueError("Expected the disjoint fixed 53/36 task split")
    if sha256(base / "manifests/preclassification.json") != split["preclassification_sha256"]:
        raise ValueError("Preclassification differs from the frozen split")
    rows = load(base / "manifests/task_files.json")
    if len(rows) != 89 or {r["task_name"] for r in rows} != set(training) | set(test):
        raise ValueError("Official task identities differ from the split")
    for task in rows:
        directory = base / "prepared/tasks" / task["task_name"]
        expected = {f["path"]: f for f in task["files"]}
        actual = {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}
        if actual != set(expected) or any(p.is_symlink() for p in directory.rglob("*")):
            raise ValueError(f"Task package inventory differs: {task['task_name']}")
        digest = hashlib.sha256()
        for name, row in sorted(expected.items()):
            file = directory / name
            value = sha256(file)
            if file.stat().st_size != row["size"] or value != row["sha256"]:
                raise ValueError(f"Official task file differs: {task['task_name']}/{name}")
            digest.update((name + "\0" + value + "\n").encode())
        if digest.hexdigest() != task["registry_content_hash"]:
            raise ValueError(f"Official task package digest differs: {task['task_name']}")
    if not without_pool:
        directory = base / "prepared/pool"
        audit = load(base / "manifests/pool.json")
        visible = load(directory / "manifest.json")
        if audit["split_sha256"] != sha256(base / "manifests/split.json"):
            raise ValueError("Pool is not bound to the frozen split")
        if set(audit["training_task_ids"]) != set(training) or set(audit["test_task_ids_excluded"]) != set(test):
            raise ValueError("Pool training/test identities differ")
        if (set(visible["training_task_ids"]) != set(training) or visible["cross_task_memory"] is not False
                or "test_task_ids" in visible or "test_task_ids_excluded" in visible):
            raise ValueError("Visible pool violates training-only isolation")
        actual = {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}
        if actual != set(audit["files_sha256"]) or any(p.is_symlink() for p in directory.rglob("*")):
            raise ValueError("Pool file inventory differs")
        if any(sha256(directory / name) != value for name, value in audit["files_sha256"].items()):
            raise ValueError("Pool file checksum differs")
        if ({p.stem for p in (directory / "tasks").glob("*.md")} != set(training)
                or {p.name for p in (directory / "trajectories").iterdir()} != set(training)):
            raise ValueError("Training pool includes unexpected task identities")
        bodies = [row for row in audit["trajectories"] if row.get("pool_file")]
        if len(bodies) != 5785 or any(row["task_id"] not in training for row in bodies):
            raise ValueError("Expected 5,785 training trajectory bodies")
    return {"official_tasks": 89, "training_tasks": len(training), "test_tasks": len(test),
            "ordered_test_task_ids": test, "pool_restored": not without_pool}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--revision", default=RELEASE_REVISION, help="Hugging Face revision (defaults to the frozen publication commit)")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--local-dataset", type=Path, help="Directory containing this TB2.1 export's manifest and archives")
    parser.add_argument("--without-pool", action="store_true", help="Restore metadata and official tasks for the baseline only")
    parser.add_argument("--verify-only", action="store_true", help="Validate already-restored inputs without downloading")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.verify_only:
        print(json.dumps(verify_dataset(root, without_pool=args.without_pool), indent=2))
        return
    cache = (args.local_dataset or args.cache_dir or root / ".cache/memory_dci/terminal_bench_2_1").resolve()
    base_url = f"https://huggingface.co/datasets/{REPO}/resolve/{urllib.parse.quote(args.revision, safe='')}/{SUBDIRECTORY}/"
    if not args.local_dataset:
        download(base_url + "manifest.json", cache / "manifest.json",
                 RELEASE_MANIFEST_SHA256 if args.revision == RELEASE_REVISION else None)
    manifest = load(cache / "manifest.json")
    if (manifest.get("schema_version") != 1 or manifest.get("dataset_repo") != REPO
            or manifest.get("dataset_subdirectory") != SUBDIRECTORY or manifest.get("archive_prefix") != str(PREFIX)):
        raise ValueError("Unexpected TB2.1 dataset manifest")
    all_records = manifest["files"]
    for row in all_records:
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts or not path.is_relative_to(PurePosixPath(PREFIX)):
            raise ValueError("Dataset path is outside the TB2.1 namespace")
        _contained(root / Path(*path.parts), root)
    for bundle in ["metadata", "tasks"] + ([] if args.without_pool else ["pool"]):
        name = bundle + ".tar.gz"
        expected = manifest["archives"][name]
        archive = cache / name
        if not args.local_dataset:
            download(base_url + name, archive, expected["sha256"])
        if archive.stat().st_size != expected["bytes"] or sha256(archive) != expected["sha256"]:
            raise ValueError(f"Archive checksum mismatch: {name}")
        records = [r for r in all_records if r["bundle"] == bundle]
        if len(records) != expected["file_count"]:
            raise ValueError("Manifest archive/file count mismatch")
        restore(archive, root, records)
        print(f"Verified and restored {len(records)} files from {name}", flush=True)
    result = verify_dataset(root, without_pool=args.without_pool)
    split_path = root / PREFIX / "manifests/split.json"
    if (sha256(split_path) != manifest["split_sha256"]
            or result["ordered_test_task_ids"] != manifest["test_task_ids"]):
        raise ValueError("Restored split/order differs from the published export")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
