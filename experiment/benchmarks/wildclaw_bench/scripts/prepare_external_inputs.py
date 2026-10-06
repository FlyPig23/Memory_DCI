#!/usr/bin/env python3
"""Prepare frozen public inputs without reading held-out prompts or graders.

Only task path fields and split IDs are inspected for the 60-task staging pass.
External additions follow official prepare.sh and are restricted to build tasks.
Never execute task commands, .git hooks, grading code, or models here.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT = Path(__file__).resolve().parents[4]
EXPERIMENT = PROJECT / "experiment"
SNAPSHOT = EXPERIMENT / "trajectory library/WildClawBench/task/hf_snapshot"
STAGING = EXPERIMENT / "runtime/task_inputs"
EXTERNAL = EXPERIMENT / "runtime/external_inputs"
MANIFEST = EXPERIMENT / "manifests/dependency_resources.json"
OWNER = "org.hangxiao.skill-dci.owner=hangxiao-skill-dci"

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()

def confined(path):
    path = Path(path)
    if not path.resolve().is_relative_to(PROJECT):
        raise ValueError("path is outside project")
    return path

def write_manifest(doc):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    temp = MANIFEST.with_suffix(".tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    temp.replace(MANIFEST)

def copy_public(source, destination):
    """Copy snapshot bytes; never hardlink mutable staging to original archive."""
    if source.is_symlink():
        raise ValueError("source symlink is not supported")
    confined(destination)
    if destination.is_symlink():
        raise ValueError("staged public input must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or source.stat().st_size != destination.stat().st_size or sha256(source) != sha256(destination):
            raise ValueError("existing staged input differs from frozen source")
        return False
    shutil.copy2(source, destination)
    return True

def stage(tasks):
    records = []
    for task in tasks:
        # Deliberately do not access prompt/automated_checks/ground-truth fields.
        relative = Path(task["public_workspace_relpath"])
        if relative.parts[0] != "workspace" or ".." in relative.parts:
            raise ValueError("invalid workspace path")
        source = confined(SNAPSHOT / relative)
        target = confined(STAGING.joinpath(*relative.parts[1:]))
        (target / "exec").mkdir(parents=True, exist_ok=True)
        copied = 0
        public_files = []
        # Preserve the complete original public workspace layout. Official
        # Social warmups consume workspace/tmp before deleting the runtime copy;
        # copying exec alone silently substitutes the wrong task environment.
        # Root-level helpers remain root-level: never execute them or move them
        # into exec. Do not descend into gt or hash any of its contents.
        children = sorted(source.iterdir()) if source.exists() else []
        if source.is_symlink():
            raise ValueError("unexpected workspace symlink")
        for child in children:
            if child.name == "gt":
                continue
            items = [child] + sorted(child.rglob("*")) if child.is_dir() and not child.is_symlink() else [child]
            for item in items:
                if item.is_symlink():
                    raise ValueError("unexpected input symlink")
                relative_input = item.relative_to(source)
                destination = target / relative_input
                if item.is_dir():
                    confined(destination)
                    destination.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    copied += copy_public(item, destination)
                    source_hash = sha256(item)
                    if source_hash != sha256(destination):
                        raise ValueError("staged public input hash mismatch")
                    public_files.append({"path": relative_input.as_posix(), "bytes": item.stat().st_size,
                                         "sha256": source_hash, "staged_sha256": source_hash,
                                         "kind": "exec" if relative_input.parts[0] == "exec" else "auxiliary"})
                else:
                    raise ValueError("unexpected public input file type")
        gt_source = source / "gt"
        gt_target = target / "gt"
        if gt_source.exists():
            if gt_target.is_symlink():
                if gt_target.resolve() != gt_source.resolve():
                    raise ValueError("existing gt link points elsewhere")
            elif gt_target.exists():
                raise ValueError("existing gt staging is not the expected link")
            else:
                gt_target.symlink_to(gt_source.resolve(), target_is_directory=True)
        records.append({"task_id": task["task_id"], "workspace": str(target.relative_to(PROJECT)),
                        "newly_copied_public_files": copied, "gt_link_exists": gt_target.is_symlink(),
                        "snapshot_workspace_exists": source.is_dir(),
                        "verified_public_file_count": len(public_files),
                        "verified_auxiliary_file_count": sum(row["kind"] == "auxiliary" for row in public_files),
                        "public_inputs": public_files})
    return records

def extract_build_git(tasks, build):
    records = []
    for task in tasks:
        if task["task_id"] not in build:
            continue
        if task["task_id"] not in {"06_Safety_Alignment_task_2_leaked_api",
                                   "06_Safety_Alignment_task_3_leaked_api_pswd"}:
            continue
        relative = Path(task["public_workspace_relpath"])
        repo = STAGING.joinpath(*relative.parts[1:]) / "exec/mm_agents"
        archive = repo / "dot_git.tar.gz"
        if not archive.exists():
            records.append({"task_id": task["task_id"], "status": "missing_archive"})
            continue
        if not (repo / ".git").exists():
            with tarfile.open(archive, "r:gz") as handle:
                members = handle.getmembers()
                for member in members:
                    parts = Path(member.name).parts
                    if not parts or parts[0] != ".git" or ".." in parts or Path(member.name).is_absolute():
                        raise ValueError("unexpected git archive member")
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("git archive contains a link or special file")
                handle.extractall(repo, members=members, filter="data")
        records.append({"task_id": task["task_id"], "status": "prepared",
                        "source_archive_sha256": sha256(archive)})
    return records

def fetch_sam3():
    import requests
    folder = EXTERNAL / "sam3"
    folder.mkdir(parents=True, exist_ok=True)
    metadata_path = folder / "source.json"
    if metadata_path.exists():
        source = json.loads(metadata_path.read_text())
    else:
        url = "https://modelscope.cn/api/v1/models/facebook/sam3/repo/files?Revision=master&Recursive=true"
        response = requests.get(url, timeout=(15, 60))
        response.raise_for_status()
        data = response.json()
        row = next(row for row in data["Data"]["Files"] if row["Path"] == "sam3.pt")
        source = {"provider": "ModelScope", "repo": "facebook/sam3", "revision": row["Revision"],
                  "path": row["Path"], "bytes": row["Size"], "sha256": row["Sha256"]}
        metadata_path.write_text(json.dumps(source, indent=2)+"\n")
    final = folder / "sam3.pt"
    if final.exists() and final.stat().st_size == source["bytes"] and sha256(final) == source["sha256"]:
        return dict(source, status="complete", local_path=str(final.relative_to(PROJECT)))
    url = f'https://modelscope.cn/models/facebook/sam3/resolve/{source["revision"]}/sam3.pt'
    part = folder / "sam3.pt.part"
    for attempt in range(3):
        try:
            position = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={position}-"} if position else {}
            with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                mode = "ab" if position and response.status_code == 206 else "wb"
                with part.open(mode) as handle:
                    for block in response.iter_content(1024*1024):
                        if block:
                            handle.write(block)
            if part.stat().st_size != source["bytes"] or sha256(part) != source["sha256"]:
                raise ValueError("SAM3 size/hash mismatch")
            part.replace(final)
            return dict(source, status="complete", local_path=str(final.relative_to(PROJECT)))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")

VIDEOS = {
    "football": ("93LPZJkCW2w", "first_half.mp4", [
        ("05_Creative_Synthesis_task_1_match_report", "task_1_match_report"),
        ("05_Creative_Synthesis_task_2_goal_highlights", "task_2_goal_highlights")]),
    "lecture": ("LPZh9BOjkQs", "video.mp4", [
        ("05_Creative_Synthesis_task_4_video_notes", "task_4_video_notes")]),
    "launch": ("H3KnMyojEQU", "recording.mp4", [
        ("05_Creative_Synthesis_task_5_product_launch_video_to_json", "task_5_product_launch_video_to_json"),
        ("05_Creative_Synthesis_task_11_video_en_to_zh_dub", "task_11_video_en_to_zh_dub")]),
}

def fetch_video(name, build):
    import imageio_ffmpeg
    video_id, destination_name, targets = VIDEOS[name]
    if any(task_id not in build for task_id, _ in targets):
        raise ValueError("external video preparation would touch a non-build task")
    folder = EXTERNAL / "videos" / name
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / destination_name
    log = folder / "download.log"
    info_path = folder / "source.info.json"
    if not final.exists():
        downloaded = folder / "source.mp4"
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        command = [sys.executable, "-m", "yt_dlp", "--no-cache-dir", "--no-playlist",
                   "--retries", "2", "--fragment-retries", "2", "--socket-timeout", "30",
                   "--ffmpeg-location", ffmpeg, "--merge-output-format", "mp4", "--write-info-json",
                   "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
                   "-o", str(downloaded), f"https://www.youtube.com/watch?v={video_id}"]
        with log.open("ab") as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=3600)
        if result.returncode:
            return {"kind": name, "video_id": video_id, "status": "download_failed",
                    "exit_code": result.returncode, "log": str(log.relative_to(PROJECT))}
        if name == "football":
            with log.open("ab") as output:
                subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                                "-i", str(downloaded), "-t", "00:57:00", "-c", "copy", str(final)],
                               stdout=output, stderr=subprocess.STDOUT, check=True, timeout=600)
        else:
            shutil.copy2(downloaded, final)
    digest = sha256(final)
    for _, task_dir in targets:
        target = STAGING / "05_Creative_Synthesis" / task_dir / "exec" / destination_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(final, target)
        elif sha256(target) != digest:
            raise ValueError("existing video input differs")
    return {"kind": name, "video_id": video_id, "status": "complete", "sha256": digest,
            "bytes": final.stat().st_size, "local_path": str(final.relative_to(PROJECT)),
            "source_info": str(info_path.relative_to(PROJECT)) if info_path.exists() else None}

def clone_reference():
    destination = EXTERNAL / "sam3-reference"
    url = "https://github.com/facebookresearch/sam3.git"
    if not destination.exists():
        subprocess.run(["git", "clone", "--depth", "1", url, str(destination)], check=True,
                       stdout=subprocess.DEVNULL, timeout=600)
    revision = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
    return {"repo": url, "revision": revision, "status": "complete",
            "local_path": str(destination.relative_to(PROJECT)),
            "note": "Reference cache only; never overwrite document-stripped or bug-injected SAM3 task fixtures."}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--download-sam3", action="store_true")
    parser.add_argument("--download-videos", action="store_true")
    parser.add_argument("--clone-sam3-reference", action="store_true")
    args = parser.parse_args()
    split_path = EXPERIMENT / "manifests/split.json"
    split = json.loads(split_path.read_text())
    tasks = json.loads((EXPERIMENT / "manifests/task_manifest.json").read_text())["tasks"]
    build = set(split["build_task_ids"])
    EXTERNAL.mkdir(parents=True, exist_ok=True)
    doc = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    if doc.get("split_sha256") not in (None, sha256(split_path)):
        raise ValueError("split changed since dependency preparation")
    doc.update({"split_sha256": sha256(split_path), "workspace_root": str(STAGING.relative_to(PROJECT)),
                "test_content_policy": "Only path fields used; no held-out prompt, gt contents or score read.",
                "public_staging_policy": "Preserve all original non-gt workspace paths and verify source/staged SHA256, including tmp; root auxiliary helpers are never executed or relocated into exec."})
    if args.stage:
        doc["staging"] = stage(tasks)
        doc["build_git_archives"] = extract_build_git(tasks, build)
        write_manifest(doc)
        print("Staged public inputs for",len(tasks),"tasks; build .git archives prepared",flush=True)
    actions = {}
    if args.download_sam3:
        if not {"02_Code_Intelligence_task_1_sam3_inference", "02_Code_Intelligence_task_2_sam3_debug"} <= build:
            raise ValueError("SAM3 targets must be build tasks")
        actions["sam3"] = fetch_sam3
    if args.download_videos:
        for name in VIDEOS:
            actions[name] = lambda n=name: fetch_video(n, build)
    if args.clone_sam3_reference:
        actions["sam3_reference"] = clone_reference
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(action): name for name, action in actions.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                doc[name] = future.result()
                if name == "sam3" and doc[name]["status"] == "complete":
                    source = PROJECT / doc[name]["local_path"]
                    for directory in ["task_1_sam3_inference", "task_2_sam3_debug"]:
                        target = STAGING / "02_Code_Intelligence" / directory / "exec/sam3/sam3.pt"
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists():
                            shutil.copy2(source, target)
                        elif target.stat().st_size != source.stat().st_size or sha256(target) != doc[name]["sha256"]:
                            raise ValueError("existing staged SAM3 weights differ")
            except Exception as error:
                doc[name] = {"status": "failed", "error_type": type(error).__name__,
                             "error": str(error)[:1000]}
            write_manifest(doc)
            print(name, doc[name]["status"], flush=True)
    # A local --stage pass does not require optional downloader packages.
    doc["tool_versions"] = {}
    for name in ["yt-dlp", "imageio-ffmpeg", "requests"]:
        try:
            doc["tool_versions"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            doc["tool_versions"][name] = None
    write_manifest(doc)
    return 1 if any(doc[name].get("status") != "complete" for name in actions) else 0

if __name__ == "__main__":
    raise SystemExit(main())
