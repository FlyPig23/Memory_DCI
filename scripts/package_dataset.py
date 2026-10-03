#!/usr/bin/env python3
"""Build the public, credential-sanitized Memory DCI dataset from local evidence.

The explicit allowlist deliberately excludes runtime credentials, caches, raw
Codex homes, mutable workspaces, retired experiments, and duplicate archives.
This command never uploads data or changes the source experiment files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO = "FlyPig23/memory_dci"
SOURCE_REVISIONS = {
    "benchmark_code": {"url": "https://github.com/InternLM/WildClawBench",
                       "revision": "316334ccc4a87b9b5635ad73da99b4dfc0b3887e", "license": "MIT"},
    "task_data": {"url": "https://huggingface.co/datasets/internlm/WildClawBench",
                  "revision": "75f945578aa00cbdb8f46e4d42e4f4e98f704b4f", "license": "MIT"},
    "trajectories": {"url": "https://huggingface.co/datasets/internlm/WildClawBench-Trajectories",
                     "revision": "d2816016a7a7b41fa6b7ba368b28ddafcb54fd93", "license": "MIT"},
    "ripgrep": {"url": "https://github.com/BurntSushi/ripgrep/tree/e89fff89ac",
                "revision": "e89fff89ac", "version": "15.2.0", "license": "MIT OR Unlicense"},
}
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----", re.S),
    "huggingface_token": re.compile(rb"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}"),
    "openai_key": re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"(?<![A-Za-z0-9])(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    "jwt": re.compile(rb"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "bearer_token": re.compile(rb"(?i)\bBearer [A-Za-z0-9._~+/-]{20,}"),
    "credential_assignment": re.compile(
        rb"(?i)(?P<prefix>(?:OPENROUTER_API_KEY|OPENAI_API_KEY|HF_TOKEN|access_token|refresh_token|client_secret|service[-_]key)[\\\"'\t ]*[:=][\\\"'\t ]*)(?P<secret>[A-Za-z0-9_+/=.-]{24,})"),
}
RUN_FILES = {
    "result.json", "run.json", "evaluation.json", "score.json", "usage.json", "execution.json",
    "protocol.json", "solver_prompt.txt", "transcript.openclaw.jsonl", "codex.events.jsonl",
    "native_environment.json", "runtime_state.json", "solver_audit.json", "workflow_audit.json",
    "model_audit.json", "memory_review.json", "memory_initial.json", "solver_final_decision.json",
    "grader.stdout.log", "grader.stderr.log", "codex.stderr.log", "isolation.json", "resume.json",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def tree(path):
    return sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()
                  and not {"__pycache__", ".cache", ".git"}.intersection(p.relative_to(path).parts)
                  and p.name != ".DS_Store")


def selected_runs(root):
    groups = {
        "V1": sorted((root / "experiment/runs/formal").glob("*-a0")),
        "V1.1": sorted((root / "experiment/runs/vanilla_sol/formal").glob("v11-formal-*")),
        "V5": sorted((root / "experiment/runs/dci_memory/formal").glob("dci5-formal-*")),
    }
    for version, runs in groups.items():
        if len(runs) != 24 or any(not (p / "result.json").is_file() for p in runs):
            raise ValueError(f"Expected 24 completed {version} results")
    return groups


def known_credentials(root, runs):
    # Read only for exclusion; credential values never leave process memory.
    paths = [root / "experiment/runtime/codex_auth/auth.json"]
    for directory in runs:
        paths += [directory / "codex-home/auth.json", directory / "review/codex-home/auth.json"]
    values = set()

    def visit(value, name=""):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, key)
        elif isinstance(value, list):
            for item in value:
                visit(item, name)
        elif isinstance(value, str) and len(value) >= 16 and any(
                key in name.lower() for key in ("token", "key", "secret", "password")):
            values.add(value.encode())

    for path in paths:
        if path.is_file():
            visit(json.loads(path.read_text()))
    service_key = root / "experiment/runtime/inference-private/service-key"
    if service_key.is_file() and len(service_key.read_bytes().strip()) >= 16:
        values.add(service_key.read_bytes().strip())
    return re.compile(b"|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))) if values else None


def selections(root, groups):
    inputs = {str(p.relative_to(root)): p for p in tree(root / "experiment/corpus/frozen_build")}
    for name in ("split.json", "task_manifest.json", "task_families.json", "sources.json",
                 "dependency_resources.json", "grader_error_contracts.json",
                 "runtime_dependency_validation.json", "warmup_dependencies.json", "runtime_skill_patches.json"):
        p = root / "experiment/manifests" / name
        inputs[str(p.relative_to(root))] = p
    p = root / "experiment/reports/source_model_scores/scores.json"
    inputs[str(p.relative_to(root))] = p
    split = json.loads((root / "experiment/manifests/split.json").read_text())
    tasks = json.loads((root / "experiment/manifests/task_manifest.json").read_text())["tasks"]
    snapshot = root / "experiment/trajectory library/WildClawBench/task/hf_snapshot"
    for task in tasks:
        if task["task_id"] in split["test_task_ids"]:
            for p in tree(snapshot / task["public_workspace_relpath"]):
                inputs[str(p.relative_to(root))] = p
    for p in tree(root / "experiment/runtime/skills"):
        inputs[str(p.relative_to(root))] = p
    for name in ("rg", "ripgrep-LICENSE-MIT", "ripgrep-UNLICENSE"):
        p = root / "experiment/runtime/bin" / name
        inputs[str(p.relative_to(root))] = p
    evidence = {}
    for runs in groups.values():
        for directory in runs:
            for base in (directory, directory / "review"):
                for name in RUN_FILES:
                    p = base / name
                    if p.is_file() and not p.is_symlink():
                        evidence[str(p.relative_to(root))] = p
            for name in ("memory", "memory_after_solver", "memory_before_review", "review/memory",
                         "controller-resume", "controller-usage"):
                for p in tree(directory / name):
                    evidence[str(p.relative_to(root))] = p
            for name in ("solver-evidence/manifest.json", "solver-evidence/frozen_task.json"):
                p = directory / name
                if p.is_file():
                    evidence[str(p.relative_to(root))] = p
    for variant in ("vanilla_sol", "dci_memory"):
        p = root / "experiment/runs" / variant / "batch.json"
        evidence[str(p.relative_to(root))] = p
    protocols = {
        "v1": root / "experiment/manifests/formal_protocol.json",
        "v1_1": root / "experiment/variants/vanilla_sol/prepared/formal/protocol.json",
        "v5": root / "experiment/variants/dci_memory/prepared/formal/protocol.json",
    }
    for version, p in protocols.items():
        evidence[f"experiment/evidence/frozen_protocols/{version}.json"] = p
    return {"inputs": inputs, "evidence": evidence}


def pack(root, destination):
    root, destination = root.resolve(), destination.resolve()
    if destination.is_relative_to(root):
        raise ValueError("Dataset destination must be outside the source repository")
    groups = selected_runs(root)
    known = known_credentials(root, [p for rows in groups.values() for p in rows])
    selections_by_bundle = selections(root, groups)
    destination.mkdir(parents=True, exist_ok=True)
    inventory, redactions, archives = [], [], {}
    counts = Counter()

    def sanitize(data, path):
        original = data
        local_counts = {}
        for name, pattern in {"known_local_credential": known, **PATTERNS}.items():
            if pattern is not None:
                replacement = b"[REDACTED_" + name.upper().encode() + b"]"
                if name == "credential_assignment":
                    data, n = pattern.subn(lambda m: m.group("prefix") + replacement, data)
                else:
                    data, n = pattern.subn(replacement, data)
                if n:
                    local_counts[name] = n
                    counts[name] += n
        if local_counts:
            redactions.append({"path": path, "original_sha256": digest(original),
                               "export_sha256": digest(data), "replacements": local_counts})
        return data

    for bundle, selection in selections_by_bundle.items():
        corpus_updates = {}
        archive = destination / f"{bundle}.tar.gz"
        with tempfile.NamedTemporaryFile(prefix=bundle + ".", suffix=".tar.gz.part", dir=destination,
                                         delete=False) as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            temporary_archive = Path(raw.name)
            with tarfile.open(fileobj=gz, mode="w|") as tar:
                corpus_manifest = "experiment/corpus/frozen_build/manifest.json"
                names = sorted(selection, key=lambda p: (p == corpus_manifest, p))
                for name in names:
                    source = selection[name]
                    original = source.read_bytes()
                    data = sanitize(original, name)
                    if name.startswith("experiment/corpus/frozen_build/episodes/") and data != original:
                        corpus_updates[str(source.relative_to(root / "experiment/corpus/frozen_build"))] = digest(data)
                    if name == corpus_manifest and corpus_updates:
                        doc = json.loads(data)
                        for episode in doc["episodes"]:
                            if episode["path"] in corpus_updates:
                                episode["original_content_hash"] = episode["content_hash"]
                                episode["content_hash"] = corpus_updates[episode["path"]]
                        doc["public_export"] = {"sanitized_trajectories": len(corpus_updates),
                                                "changes": "Private-key/token material redacted; see dataset redactions.json."}
                        data = encoded(doc)
                        redactions.append({"path": name, "original_sha256": digest(original),
                                           "export_sha256": digest(data), "change": "Update sanitized episode hashes; preserve original hashes."})
                    # Scan the exact outgoing bytes, after transformation.
                    if any(p.search(data) for p in PATTERNS.values()) or (known and known.search(data)):
                        raise ValueError(f"Credential pattern remains in export: {name}")
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
                    info.mtime = 0
                    tar.addfile(info, io.BytesIO(data))
                    inventory.append({"bundle": bundle, "path": name, "bytes": len(data),
                                      "sha256": digest(data), "source_sha256": digest(original)})
        temporary_archive.replace(archive)
        archives[archive.name] = {"bytes": archive.stat().st_size, "sha256": digest(archive.read_bytes()),
                                  "file_count": sum(r["bundle"] == bundle for r in inventory),
                                  "uncompressed_bytes": sum(r["bytes"] for r in inventory if r["bundle"] == bundle)}
    manifest = {"schema_version": 1, "repo_id": REPO, "archives": archives, "files": inventory,
                "versions": {version: len(runs) for version, runs in groups.items()},
                "training_tasks": 36, "training_trajectories": 432, "training_images": 381,
                "test_tasks": 24, "sources": SOURCE_REVISIONS,
                "scan": {"credential_patterns_remaining": 0, "replacement_counts": dict(counts),
                         "known_local_credentials_checked": True},
                "limitations": ["Lean evidence omits raw Codex sessions, credentials, solver workspaces, caches and model weights.",
                                "Historical full-session/workspace audits cannot be rerun from this lean export alone.",
                                "Credential sanitization changes bytes; original and exported hashes are recorded separately."]}
    (destination / "manifest.json").write_bytes(encoded(manifest))
    (destination / "redactions.json").write_bytes(encoded({"files": redactions, "counts": dict(counts)}))
    (destination / "provenance.json").write_bytes(encoded({"sources": SOURCE_REVISIONS,
        "scope": "Frozen 36/24 WildClawBench split, sanitized normalized training corpus, 24 test inputs, 72 local runs.",
        "license_note": "Upstream datasets and benchmark code declare MIT. Included third-party task assets retain their original rights; no new rights to those assets are asserted."}))
    license_path = root / "experiment/vendor/WildClawBench/LICENSE"
    if license_path.is_file():
        (destination / "UPSTREAM_LICENSE.txt").write_bytes(license_path.read_bytes())
    (destination / "README.md").write_text(DATASET_CARD, encoding="utf-8")
    print(json.dumps({"destination": str(destination), "archives": archives,
                      "scan": manifest["scan"], "redacted_files": len(redactions)}, indent=2))


DATASET_CARD = """---
language:
- en
- zh
tags:
- agents
- memory
- wildclawbench
- reproducibility
size_categories:
- n<1K
---
# Memory DCI

Minimal reproducibility data for [Memory_DCI](https://github.com/FlyPig23/Memory_DCI):
V1 and V1.1 baselines, and the V5 task-local memory workflow.

`inputs.tar.gz` contains the fixed 36-training/24-test split, 432 normalized
training trajectories from 12 models, 381 referenced images, task metadata,
training-score provenance, the 24 test workspaces including grader-only ground
truth, official task-interface skills, and the pinned ripgrep executable with
its upstream license files. Test trajectories never enter the
training pool; the harness mounts ground truth only for grading.

`evidence.tar.gz` contains the 72 original run results, solver transcripts and
tool events, V5 per-task memory snapshots/reviewer decisions, and historical
protocols. It omits credentials, raw Codex homes/sessions, duplicated output
workspaces, caches, model weights, and retired V2–V4 / Terminal Bench runs.
This is a lean evidence export: full raw-session and workspace integrity audits
cannot be reproduced solely from it. Existing score summaries remain original.

Every archived file has a size and SHA-256 in `manifest.json`. Credential-like
material is removed before publication; `redactions.json` records affected
paths, original/export hashes, and replacement categories, never removed values.
The exported corpus manifest records updated hashes and original hashes for
changed trajectories. The sanitized export is **not byte-identical** to the
original experiment inputs; freshly built pool hashes therefore differ.

## Sources

- [WildClawBench code](https://github.com/InternLM/WildClawBench/tree/316334ccc4a87b9b5635ad73da99b4dfc0b3887e)
- [Task data](https://huggingface.co/datasets/internlm/WildClawBench/tree/75f945578aa00cbdb8f46e4d42e4f4e98f704b4f)
- [Official trajectories](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories/tree/d2816016a7a7b41fa6b7ba368b28ddafcb54fd93)

The upstream datasets and benchmark code declare MIT; see `provenance.json`
and `UPSTREAM_LICENSE.txt`. Third-party task assets retain their original rights.
This dataset does not assert a new blanket license over those assets.

## Restore

From a clone of Memory_DCI, install its requirements and run:

```bash
python3 scripts/download_data.py
python3 scripts/download_data.py --include-evidence
python3 scripts/report_results.py
```

The downloader verifies archive and per-file hashes, restores original relative
paths, stages public test inputs separately from grader-only ground truth, and
can fetch the pinned upstream benchmark using `--fetch-benchmark`. Docker,
Codex authentication and the runtime image are separate prerequisites documented
in the code repository. No token is required for this public dataset.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    pack(args.root.resolve(), args.destination.resolve())


if __name__ == "__main__":
    main()
