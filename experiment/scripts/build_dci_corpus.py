#!/usr/bin/env python3
"""Build frozen DCI views from the union of the pinned public trace formats.

Only build-task session/chat/score contents are read. Test coverage is determined
from filenames and the parquet task_id/model_name projection. Never extract an
archive wholesale: test trajectories and scores remain sealed in the snapshot.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
from typing import Any
import zipfile

REVISION = "d2816016a7a7b41fa6b7ba368b28ddafcb54fd93"
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-?]*[ -/]*[@-~]")
EVALUATOR = re.compile(r"(?i)(?:/gt/|/private_eval(?:/|\b)|##\s*Automated Checks|\b(?:automated_checks|private_eval|test_patch|gold_answer|gold_solution)\b|(?:^|[/\s])(?:grading\.py|score\.json)(?:\b|$))")
SECRET_TOKEN = re.compile(r"\b(?:sk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*")
SECRET_ASSIGNMENT = re.compile(r'''(?ix)(["']?(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET_KEY)|authorization)["']?\s*[:=]\s*["']?)([^\s"'`,;{}]+)''')
IMAGE_DATA_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", re.DOTALL)
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def hash_file(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.sha256() if algorithm == "sha256" else hashlib.sha1()
    if algorithm == "git_blob_sha1":
        h.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_text(value: str, counts: Counter) -> str:
    value, n = CONTROL.subn("", value)
    counts["control_characters"] += n
    value, n = SECRET_TOKEN.subn("[REDACTED_SECRET]", value)
    counts["secret_tokens"] += n
    value, n = BEARER.subn("Bearer [REDACTED_SECRET]", value)
    counts["bearer_tokens"] += n

    def assignment(match):
        candidate = match.group(2)
        if candidate.startswith(("$", "[REDACTED_", "os.environ", "os.getenv", "process.env")):
            return match.group(0)
        counts["secret_assignments"] += 1
        return match.group(1) + "[REDACTED_SECRET]"

    return SECRET_ASSIGNMENT.sub(assignment, value)


def clean_structure(value: Any, counts: Counter) -> Any:
    if isinstance(value, str):
        return clean_text(value, counts)
    if isinstance(value, list):
        return [clean_structure(item, counts) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if re.search(r"(?i)^(?:automated_checks|private_eval|gold_answer|gold_solution|test_patch)$", str(key)):
                counts["evaluator_fields"] += 1
                continue
            if re.search(r"(?i)(?:api_?key|access_?token|refresh_?token|password|authorization|secret)$", str(key)):
                counts["secret_fields"] += 1
                result[key] = "[REDACTED_SECRET]"
            else:
                result[key] = clean_structure(item, counts)
        return result
    return value


def store_image(block: dict[str, Any], root: Path, counts: Counter) -> dict[str, Any] | None:
    mime, data = None, None
    if block.get("type") == "image" and isinstance(block.get("data"), str):
        mime, data = block.get("mimeType", block.get("mime_type", "image/png")), block["data"]
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        mime, data = source.get("media_type", "image/png"), source.get("data")
    image_url = block.get("image_url", block.get("url"))
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if isinstance(image_url, str):
        match = IMAGE_DATA_URL.match(image_url)
        if match:
            mime, data = match.groups()
    if data is None:
        return None
    if not isinstance(data, str) or not str(mime).startswith("image/"):
        counts["invalid_image_payloads"] += 1
        return None
    try:
        decoded = base64.b64decode(re.sub(r"\s+", "", data), validate=True)
    except (ValueError, base64.binascii.Error):
        counts["invalid_image_payloads"] += 1
        return None
    image_hash = digest(decoded)
    ext = MIME_EXT.get(str(mime), ".bin")
    relative = f"images/{image_hash}{ext}"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(decoded)
    return {"image_id": image_hash, "path": relative, "mime_type": mime, "sha256": image_hash}


def parse_jsonl(data: bytes) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Non-object trace record at line {number}")
            records.append(value)
    return records


def normalize_trace(data: bytes, root: Path) -> dict[str, Any]:
    records = parse_jsonl(data)
    images: dict[str, dict[str, Any]] = {}
    counts: Counter = Counter()
    blocked_calls, calls, results = set(), set(), set()
    rendered = []
    header = records[0] if records and records[0].get("type") == "session" else {}
    error_seen = False
    final_assistant = False
    message_count = 0
    for record_number, record in enumerate(records, 1):
        if record.get("type") == "compaction":
            summary = record.get("summary")
            if isinstance(summary, str):
                rendered.append(f"[record {record_number:06d}] PREEXISTING_COMPACTION\n" + clean_text(summary, counts))
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            if "role" in record:
                message = record
            else:
                continue
        message_count += 1
        role = str(message.get("role", "unknown"))
        call_id = message.get("toolCallId", message.get("tool_call_id"))
        if call_id:
            results.add(str(call_id))
        if call_id in blocked_calls:
            counts["evaluator_tool_results"] += 1
            rendered.append(f"[record {record_number:06d}] {role} [REDACTED_EVALUATOR_TOOL_RESULT]")
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = [{"type": "structured", "value": content}]
        parts = []
        for block in content:
            if not isinstance(block, dict):
                block = {"type": "text", "text": str(block)}
            kind = block.get("type", "unknown")
            if kind in ("toolCall", "tool_call", "function_call"):
                identifier = str(block.get("id", block.get("call_id", "")))
                if identifier:
                    calls.add(identifier)
                arguments = block.get("arguments", block.get("input", {}))
                raw_text = json.dumps(arguments, ensure_ascii=False)
                if EVALUATOR.search(raw_text):
                    blocked_calls.add(identifier)
                    counts["evaluator_actions"] += 1
                    parts.append("ACTION [REDACTED_EVALUATOR_ACCESS]")
                    continue
                parts.append("ACTION " + clean_text(str(block.get("name", "unknown")), counts) + "\n" + json.dumps(clean_structure(arguments, counts), ensure_ascii=False, sort_keys=True))
            elif kind in ("image", "image_url", "input_image"):
                image = store_image(block, root, counts)
                if image:
                    images[image["image_id"]] = image
                    parts.append(f"IMAGE image_id={image['image_id']} path={image['path']} mime_type={image['mime_type']}")
                else:
                    counts["unresolved_images"] += 1
                    parts.append("IMAGE [UNRESOLVED_OR_COMPACT_PLACEHOLDER]")
            else:
                text = block.get("text", block.get("thinking", block.get("summary")))
                if text is None:
                    safe_block = {k: v for k, v in block.items() if k not in ("thinkingSignature", "signature", "data")}
                    text = json.dumps(clean_structure(safe_block, counts), ensure_ascii=False, sort_keys=True)
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
                if EVALUATOR.search(text):
                    counts["evaluator_text_blocks"] += 1
                    parts.append("[REDACTED_EVALUATOR_PAYLOAD]")
                else:
                    parts.append(("THINKING\n" if kind in ("thinking", "reasoning") else "") + clean_text(text, counts))
        if message.get("errorMessage") or message.get("stopReason") == "error":
            error_seen = True
            parts.append("ERROR " + clean_text(str(message.get("errorMessage", "model error")), counts))
        final_assistant = role == "assistant" and message.get("stopReason") in ("stop", "end_turn")
        rendered.append(f"[record {record_number:06d}] role={role}" + (f" tool_call_id={call_id}" if call_id else "") + "\n" + "\n".join(parts))
    if not message_count:
        raise ValueError("Trace has no message events")
    declared = header.get("trace_status")
    inferred = "error" if error_seen else "interrupted" if calls - results else "completed" if final_assistant else "unknown"
    return {"body": "\n\n".join(rendered) + "\n", "images": sorted(images.values(), key=lambda item: item["image_id"]),
            "record_count": len(records), "message_count": message_count,
            "trace_status": declared if declared in ("completed", "error", "interrupted") else inferred,
            "trace_status_source": "official_session_header" if declared else "conservative_event_inference",
            "sanitization": dict(counts), "header": header}


def safe_member_identity(name: str, task_ids: set[str]) -> tuple[str, str, str] | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe archive member path")
    for index, part in enumerate(path.parts):
        if part in task_ids and len(path.parts) >= index + 3:
            return part, path.parts[index + 1], "/".join(path.parts[index + 2:])
    return None


def numeric_score(raw: bytes) -> dict[str, Any]:
    """Only published metric names/numbers; never expose grader explanations."""
    value = json.loads(raw)
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if isinstance(item, (int, float, bool)) and not isinstance(item, complex):
            result[key] = item
    return result


def verify_snapshot(snapshot: Path, inventory: dict[str, Any]) -> dict[str, str]:
    if inventory.get("sha") != REVISION:
        raise ValueError("Unexpected trajectory revision")
    hashes = {}
    for item in inventory["siblings"]:
        relative = item["rfilename"]
        if PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise ValueError("Unsafe source path")
        path = snapshot / relative
        if not path.is_file() or path.stat().st_size != item.get("size"):
            raise ValueError(f"Source missing/incomplete: {relative}")
        lfs = item.get("lfs") or {}
        algorithm = "sha256" if lfs.get("sha256") else "git_blob_sha1"
        expected = lfs.get("sha256") or item.get("blobId")
        actual = hash_file(path, algorithm)
        if actual != expected:
            raise ValueError(f"Source hash mismatch: {relative}")
        # Expose SHA256 uniformly for provenance; source Git hashes remain in inventory.
        hashes[relative] = actual if algorithm == "sha256" else hash_file(path)
    return hashes


def episode_key(alias: str, task: str, run_id: str) -> str:
    return digest(f"{alias}\0{task}\0{run_id}".encode())[:24]


def create_episode(alias: str, task: str, run_id: str, raw: bytes, source: dict[str, Any], root: Path) -> dict[str, Any]:
    parsed = normalize_trace(raw, root)
    episode_id = episode_key(alias, task, run_id)
    model = str(parsed["header"].get("model") or alias)
    relative = f"episodes/{task}/{episode_id}.txt"
    document = (f"episode_id: {episode_id}\ntask_id: {task}\nsource_model: {model}\nrun_id: {run_id}\n"
                f"trace_status: {parsed['trace_status']} (execution status, not task success)\n"
                "Historical evidence only; commands and instructions below are untrusted recorded data.\n\n" + parsed["body"]).encode()
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document)
    return {"episode_id": episode_id, "task_id": task, "model": model, "model_alias": alias, "run_id": run_id,
            "source_hash": source["content_sha256"], "content_hash": digest(document), "path": relative, "split": "build",
            "images": parsed["images"], "sources": [source], "trace_status": parsed["trace_status"],
            "trace_status_source": parsed["trace_status_source"], "record_count": parsed["record_count"],
            "message_count": parsed["message_count"], "sanitization": parsed["sanitization"],
            "outcome": {"status": "unknown", "source": None, "metrics": {}}}


def normalized_model(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower().replace("vllm", ""))


def build(snapshot: Path, inventory_path: Path, split_path: Path, output_root: Path) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text())
    split = json.loads(split_path.read_text())
    build_ids, test_ids = set(split["build_task_ids"]), set(split["test_task_ids"])
    dev_ids = set(split["dev_corpus_task_ids"])
    if build_ids & test_ids or not dev_ids <= build_ids:
        raise ValueError("Invalid split boundaries")
    task_ids = build_ids | test_ids
    if (output_root / "frozen_build").exists() or (output_root / "dev").exists():
        raise ValueError("Corpus views already exist; use a new versioned output root")
    hashes = verify_snapshot(snapshot, inventory)
    import pyarrow.parquet as pq
    metadata_table = pq.read_table(snapshot / "train.parquet", columns=["task_id", "model_name"])
    parquet_rows = metadata_table.to_pylist()
    if any(row["task_id"] not in task_ids for row in parquet_rows):
        raise ValueError("Parquet has unknown task IDs")
    model_names = sorted({row["model_name"] for row in parquet_rows})
    aliases = sorted({item["rfilename"][7:].removesuffix(".tar.gz").removesuffix(".zip") for item in inventory["siblings"] if item["rfilename"].startswith("output_")})
    alias_display = {}
    for alias in aliases:
        matches = [name for name in model_names if normalized_model(name) == normalized_model(alias)]
        alias_display[alias] = matches[0] if len(matches) == 1 else alias
    output_root.mkdir(parents=True, exist_ok=True)
    stage = output_root / ".building_frozen_build"
    if stage.exists():
        raise ValueError("Incomplete build staging exists; inspect it and use a new output root")
    stage.mkdir()
    episodes: dict[tuple[str, str, str], dict[str, Any]] = {}
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    for alias in aliases:
        for task in sorted(task_ids):
            coverage[(alias, task)] = {"model_alias": alias, "model": alias_display[alias], "task_id": task,
                                       "partition": "build" if task in build_ids else "sealed_test",
                                       "session_files": [], "archive_runs": [], "parquet_rows": 0}
    for row in parquet_rows:
        matches = [alias for alias, name in alias_display.items() if name == row["model_name"]]
        if len(matches) == 1:
            coverage[(matches[0], row["task_id"])]["parquet_rows"] += 1
    failures = []
    # Prefer original-image official exported sessions; only build session bytes
    # are decoded. Test source hashes were checked without JSON interpretation.
    for item in inventory["siblings"]:
        relative = item["rfilename"]
        if not relative.startswith("sessions/") or not relative.endswith(".jsonl"):
            continue
        _, alias, filename = relative.split("/", 2)
        task = filename.removesuffix(".jsonl")
        if (alias, task) not in coverage:
            raise ValueError(f"Unknown session source: {relative}")
        coverage[(alias, task)]["session_files"].append({"path": relative, "sha256": hashes[relative]})
        if task not in build_ids:
            continue
        raw = (snapshot / relative).read_bytes()
        try:
            first = json.loads(raw.splitlines()[0])
            if first.get("task_id") != task:
                raise ValueError("Session header task does not match path")
            run_id = str(first["run_id"])
            source = {"kind": "session", "path": relative, "content_sha256": hashes[relative], "revision": REVISION}
            episode = create_episode(alias, task, run_id, raw, source, stage)
            episodes[(alias, task, run_id)] = episode
            alias_display[alias] = episode["model"]
        except (ValueError, KeyError, UnicodeError) as error:
            failures.append({"source": relative, "task_id": task, "type": type(error).__name__, "reason": "Cannot parse build session; raw archive fallback attempted"})
    # Read only build chat/score members; do not extract task_output or summaries.
    for alias in aliases:
        archive_name = next(item["rfilename"] for item in inventory["siblings"] if item["rfilename"] in (f"output_{alias}.tar.gz", f"output_{alias}.zip"))
        archive_path = snapshot / archive_name
        is_zip = archive_name.endswith(".zip")
        archive = zipfile.ZipFile(archive_path) if is_zip else tarfile.open(archive_path, "r:gz")
        run_members: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        with archive:
            members = archive.infolist() if is_zip else archive.getmembers()
            for member in members:
                name = member.filename if is_zip else member.name
                identity = safe_member_identity(name, task_ids)
                if identity is None:
                    continue
                task, run_id, leaf = identity
                if leaf not in ("chat.jsonl", "score.json", "usage.json"):
                    continue
                if is_zip and member.is_dir() or not is_zip and not member.isfile():
                    continue
                run_members[(task, run_id)][leaf] = member
            for (task, run_id), mapping in sorted(run_members.items()):
                coverage[(alias, task)]["archive_runs"].append({"run_id": run_id, "archive": archive_name,
                      "archive_sha256": hashes[archive_name],
                      "members": {leaf: m.filename if is_zip else m.name for leaf, m in sorted(mapping.items())}})
                if task not in build_ids:
                    continue
                key = (alias, task, run_id)
                chat = mapping.get("chat.jsonl")
                if chat is not None:
                    stream = archive.open(chat) if is_zip else archive.extractfile(chat)
                    with stream:
                        raw = stream.read()
                    source = {"kind": "archive_chat", "archive": archive_name, "archive_sha256": hashes[archive_name],
                              "member": chat.filename if is_zip else chat.name, "content_sha256": digest(raw), "revision": REVISION}
                    if key in episodes:
                        episodes[key]["sources"].append(source)
                    else:
                        try:
                            episodes[key] = create_episode(alias, task, run_id, raw, source, stage)
                        except (ValueError, KeyError, UnicodeError) as error:
                            failures.append({"source": source["member"], "task_id": task, "type": type(error).__name__, "reason": "Cannot parse build archive chat"})
                if key in episodes and "score.json" in mapping:
                    score_member = mapping["score.json"]
                    stream = archive.open(score_member) if is_zip else archive.extractfile(score_member)
                    with stream:
                        raw_score = stream.read()
                    try:
                        metrics = numeric_score(raw_score)
                        episodes[key]["outcome"] = {"status": "official_metrics_available" if metrics else "unknown",
                            "source": {"archive": archive_name, "archive_sha256": hashes[archive_name],
                                       "member": score_member.filename if is_zip else score_member.name,
                                       "sha256": digest(raw_score)}, "metrics": metrics,
                            "success_label": None, "note": "No global success threshold inferred from execution status or mixed metric scales."}
                    except (ValueError, UnicodeError):
                        failures.append({"task_id": task, "source": archive_name, "type": "score_parse_error", "reason": "Build score kept unknown"})
    # If a format lacks a run, use only build-filtered compact rows as explicit
    # fallback, with unresolved images visible in coverage instead of fabricated.
    present = {(alias, task) for alias, task, _ in episodes}
    missing = [(alias, task) for alias in aliases for task in sorted(build_ids) if (alias, task) not in present]
    if missing:
        wanted_tasks = sorted({task for _, task in missing})
        table = pq.read_table(snapshot / "train.parquet", columns=["task_id", "model_name", "trajectory"], filters=[("task_id", "in", wanted_tasks)])
        for row in table.to_pylist():
            task = row["task_id"]
            if task not in build_ids:
                raise ValueError("Parquet filter returned a test trajectory")
            matches = [alias for alias, name in alias_display.items() if name == row["model_name"]]
            if len(matches) != 1 or (matches[0], task) not in missing:
                continue
            alias = matches[0]
            messages = json.loads(row["trajectory"]) if isinstance(row["trajectory"], str) else row["trajectory"]
            raw = b"\n".join(json.dumps({"type": "message", "message": msg}, ensure_ascii=False).encode() for msg in messages)
            run_id = "compact_parquet_only"
            source = {"kind": "parquet_build_filtered_fallback", "path": "train.parquet", "archive_sha256": hashes["train.parquet"], "content_sha256": digest(raw), "revision": REVISION}
            episode = create_episode(alias, task, run_id, raw, source, stage)
            episode["limitations"] = ["Compact fallback; images may remain placeholders and raw run identity is unavailable."]
            episodes[(alias, task, run_id)] = episode
    ordered = sorted(episodes.values(), key=lambda item: (item["task_id"], item["model_alias"], item["run_id"]))
    for episode in ordered:
        if episode["task_id"] not in build_ids:
            raise AssertionError("Test task entered corpus")
        episode["model"] = alias_display[episode["model_alias"]]
    manifest = {"schema_version": 1, "dataset_revision": REVISION, "split_sha256": hash_file(split_path),
                "source_inventory_sha256": hash_file(inventory_path), "view": "frozen_build", "task_ids": sorted(build_ids),
                "episodes": ordered, "representation": "Normalized original messages/actions; no embeddings, summaries or semantic distillation.",
                "sanitizer": {"version": 1, "policy": "Redact credential patterns, control characters, evaluator paths/fields and linked evaluator tool results; preserve build-task self-generated reasoning/content.",
                              "limitation": "Heuristic redaction is not a proof of absence of every secret or evaluator-derived fact."}}
    (stage / "manifest.json").write_bytes(encode_json(manifest))
    dev_stage = output_root / ".building_dev"
    if dev_stage.exists():
        raise ValueError("Dev staging already exists")
    dev_stage.mkdir()
    dev_episodes = [episode for episode in ordered if episode["task_id"] in dev_ids]
    paths = {episode["path"] for episode in dev_episodes}
    paths.update(image["path"] for episode in dev_episodes for image in episode["images"])
    for relative in sorted(paths):
        destination = dev_stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # EvidenceStore requires independent single-link regular files.
        shutil.copyfile(stage / relative, destination)
    dev_manifest = {**manifest, "view": "dev", "task_ids": sorted(dev_ids), "episodes": dev_episodes,
                    "excluded_task_ids": split["dev_excluded_family_task_ids"]}
    (dev_stage / "manifest.json").write_bytes(encode_json(dev_manifest))
    for row in coverage.values():
        row["model"] = alias_display[row["model_alias"]]
        row["canonical_episode_count"] = sum(e["task_id"] == row["task_id"] and e["model_alias"] == row["model_alias"] for e in ordered) if row["partition"] == "build" else None
    missing_build = [row for row in coverage.values() if row["partition"] == "build" and not row["canonical_episode_count"]]
    summary = {"schema_version": 1, "dataset_revision": REVISION, "split_sha256": hash_file(split_path),
               "status": "complete" if not missing_build else "complete_with_missing_build_trajectories",
               "source_files_verified": len(hashes), "models": alias_display,
               "build_task_count": len(build_ids), "test_task_count_sealed": len(test_ids), "dev_corpus_task_count": len(dev_ids),
               "build_episode_count": len(ordered), "dev_episode_count": len(dev_episodes),
               "build_images": len({image["image_id"] for e in ordered for image in e["images"]}),
               "build_episodes_with_official_metrics": sum(e["outcome"]["status"] == "official_metrics_available" for e in ordered),
               "parse_issues": failures, "missing_build_cells": missing_build,
               "cells": sorted(coverage.values(), key=lambda item: (item["model_alias"], item["task_id"])),
               "test_content_policy": "No test session/chat/score JSON decoded; only filenames, source hashes and parquet task/model columns used.",
               "dedup_policy": "Same model alias/task/run_id exported session and archive chat linked into one episode; different run IDs retained.",
               "build_manifest_sha256": hash_file(stage / "manifest.json"), "dev_manifest_sha256": hash_file(dev_stage / "manifest.json")}
    (output_root / "coverage.json").write_bytes(encode_json(summary))
    stage.rename(output_root / "frozen_build")
    dev_stage.rename(output_root / "dev")
    return {key: summary[key] for key in ("status", "source_files_verified", "build_task_count", "test_task_count_sealed", "dev_corpus_task_count", "build_episode_count", "dev_episode_count", "build_images", "build_episodes_with_official_metrics")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("experiment/trajectory library/WildClawBench/trajectory/hf_snapshot"))
    parser.add_argument("--inventory", type=Path, default=Path("experiment/manifests/source_WildClawBench-Trajectories.json"))
    parser.add_argument("--split", type=Path, default=Path("experiment/manifests/split.json"))
    parser.add_argument("--output-root", type=Path, default=Path("experiment/corpus"))
    args = parser.parse_args()
    print(json.dumps(build(args.snapshot, args.inventory, args.split, args.output_root), indent=2))


if __name__ == "__main__":
    main()
