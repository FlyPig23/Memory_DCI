#!/usr/bin/env python3
"""Download the frozen public inventories into this project's owned directories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiment"
os.environ["HF_HOME"] = str(EXP / ".cache/huggingface")
os.environ["HF_XET_CACHE"] = str(EXP / ".cache/huggingface/xet")
os.environ["TMPDIR"] = str(EXP / ".cache/tmp")


def atomic_json(path: Path, data: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def checksums(path: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    git = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha.update(block)
            git.update(block)
    return sha.hexdigest(), git.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["tasks", "trajectories", "image"])
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    name = "WildClawBench-Trajectories" if args.kind == "trajectories" else "WildClawBench"
    info = json.loads((EXP / "manifests" / f"source_{name}.json").read_text())
    revision = info["sha"]
    if args.kind == "trajectories":
        split = json.loads((EXP / "manifests/split.json").read_text())
        if len(split["build_task_ids"]) != 36 or len(split["test_task_ids"]) != 24:
            raise SystemExit("The 36/24 split must be frozen before trajectory ingestion")
        destination = EXP / "trajectory library/WildClawBench/trajectory/hf_snapshot"
        files = info["siblings"]
    elif args.kind == "tasks":
        destination = EXP / "trajectory library/WildClawBench/task/hf_snapshot"
        files = [f for f in info["siblings"] if not f["rfilename"].startswith("Images/")]
    else:
        destination = EXP / "runtime/images"
        files = [f for f in info["siblings"] if f["rfilename"] == "Images/wildclawbench-codex-ubuntu_v0.0.tar"]
    destination.mkdir(parents=True, exist_ok=True)
    report = EXP / "manifests" / f"download_{args.kind}.json"
    state = {"kind": args.kind, "repo": "internlm/" + name, "revision": revision,
             "started_at": datetime.now(timezone.utc).isoformat(), "destination": str(destination.relative_to(ROOT)),
             "expected_files": len(files), "expected_bytes": sum(f.get("size", 0) for f in files),
             "status": "downloading", "files": []}
    atomic_json(report, state)
    print(json.dumps({k: v for k, v in state.items() if k != "files"}), flush=True)
    from huggingface_hub import snapshot_download
    started = time.monotonic()
    try:
        snapshot_download(repo_id="internlm/" + name, repo_type="dataset", revision=revision,
                          local_dir=destination, allow_patterns=[f["rfilename"] for f in files],
                          max_workers=args.workers, token=False)
        state["status"] = "verifying"
        atomic_json(report, state)
        for index, entry in enumerate(files, 1):
            local = destination / entry["rfilename"]
            if not local.is_file() or local.stat().st_size != entry["size"]:
                raise ValueError("Missing file or size mismatch: " + entry["rfilename"])
            sha, git = checksums(local)
            expected_lfs = (entry.get("lfs") or {}).get("sha256")
            if expected_lfs and sha != expected_lfs:
                raise ValueError("LFS checksum mismatch: " + entry["rfilename"])
            if not expected_lfs and entry.get("blobId") and git != entry["blobId"]:
                raise ValueError("Git blob checksum mismatch: " + entry["rfilename"])
            state["files"].append({"path": entry["rfilename"], "bytes": local.stat().st_size,
                                   "sha256": sha, "source_blob": entry.get("blobId"), "verified": True})
            if index % 100 == 0:
                atomic_json(report, state)
                print(f"verified {index}/{len(files)}", flush=True)
        state["status"] = "complete"
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        state["elapsed_seconds"] = round(time.monotonic() - started, 2)
        atomic_json(report, state)
        print(f"COMPLETE {args.kind}: {len(files)} files verified", flush=True)
    except BaseException as exc:
        state["status"] = "failed"
        state["error"] = type(exc).__name__ + ": " + str(exc)
        atomic_json(report, state)
        raise


if __name__ == "__main__":
    main()
