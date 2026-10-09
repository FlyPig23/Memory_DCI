"""Download V8's task packages at the leaderboard's pinned versions and verify them byte for byte.

Each selected task is fetched with `harbor download terminal-bench/<task>@<revision>` into a fresh staging
directory, its Harbor content hash is recomputed with Harbor's own packager, and it is moved into
prepared/tasks/<task> only when the hash equals the official task version that every source trial was
graded on. Existing verified tasks are kept; a task directory with another hash is refused.
Writes manifests/v8_task_downloads.json. Usage (repository root, TB2.1 venv):
  python -m experiment.benchmarks.terminal_bench_4.scripts.download_tasks [--workers 4]
"""
from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
TASKS = BASE / "prepared/tasks"
MANIFEST = BASE / "manifests/v8_task_downloads.json"


def content_hash(path: Path) -> tuple[str, int]:
    from harbor.publisher.packager import Packager
    digest, files = Packager.compute_content_hash(path)
    return digest, len(files)


def fetch(task: dict) -> dict:
    target = TASKS / task["task_id"]
    if target.exists():
        digest, files = content_hash(target)
        if digest != task["content_hash"]:
            raise ValueError(f"{target} exists with content hash {digest}, expected {task['content_hash']}")
        return {"task_id": task["task_id"], "status": "kept", "content_hash": digest, "files": files}
    harbor = Path(sys.executable).with_name("harbor")
    with tempfile.TemporaryDirectory(prefix="tb4-download-", dir=TASKS.parent) as staging:
        ref = f"terminal-bench/{task['task_id']}@{task['revision']}"
        done = subprocess.run([str(harbor), "download", ref, "-o", staging], capture_output=True, text=True, timeout=1800)
        source = Path(staging) / task["task_id"]
        if done.returncode or not source.is_dir():
            raise RuntimeError(f"{ref}: harbor download failed: {(done.stderr or done.stdout)[-400:]}")
        digest, files = content_hash(source)
        if digest != task["content_hash"]:
            raise ValueError(f"{ref}: downloaded content hash {digest} differs from the official {task['content_hash']}")
        shutil.move(str(source), str(target))
    return {"task_id": task["task_id"], "status": "downloaded", "content_hash": digest, "files": files}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    selection = json.loads((BASE / "manifests/v8_tasks.json").read_text())
    tasks = [row for row in selection["tasks"] if row["selected"]]
    TASKS.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(fetch, tasks))
    MANIFEST.write_text(json.dumps({"verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    "rule": "Harbor content hash of prepared/tasks/<task> equals the official task version",
                                    "tasks": sorted(results, key=lambda row: row["task_id"])}, indent=1) + "\n")
    print(json.dumps({"tasks": len(results), "downloaded": sum(r["status"] == "downloaded" for r in results),
                      "kept": sum(r["status"] == "kept" for r in results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
