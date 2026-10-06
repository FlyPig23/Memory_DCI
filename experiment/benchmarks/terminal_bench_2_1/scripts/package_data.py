"""Export only TB2.1 frozen task packages, training pool and provenance.

Run against an original project using --source-root. Never writes to that
project, reads live runs, changes the split, or uploads files.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

PREFIX = Path("experiment/benchmarks/terminal_bench_2_1")
DATASET = "FlyPig23/memory_dci"
METADATA = (
    "dataset_pin.json", "split.json", "preclassification.json", "preclassification_split_audit.json",
    "task_files.json", "task_integrity.json", "prepared_tasks.json", "task_metadata.json",
    "official_timeouts.json", "pool.json", "pool_readiness.json", "trajectory_availability_audit.json",
    "trajectory_download_audit.json", "version_audit.json",
)
PATTERNS = {
    "private_key": (b"PRIVATE KEY", re.compile(
        rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----[ \t]*(?:(?:\r?\n|\\n)[ \t]*[A-Za-z0-9+/=]{16,})+(?:\r?\n|\\n)[ \t]*-----END \1-----")),
    "huggingface_token": (b"hf_", re.compile(rb"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}")),
    "openai_key": (b"sk-", re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    "github_token": (b"gh", re.compile(rb"(?<![A-Za-z0-9])(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}")),
    "aws_access_key": (b"AKIA", re.compile(rb"AKIA[0-9A-Z]{16}")),
    "aws_session_key": (b"ASIA", re.compile(rb"ASIA[0-9A-Z]{16}")),
    "google_api_key": (b"AIza", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    "google_oauth_token": (b"ya29.", re.compile(rb"ya29\.[0-9A-Za-z_-]{30,}")),
    "azure_sas": (b".core.windows.net", re.compile(rb'https?://[^\s<>"\\]+\.core\.windows\.net/[^\s<>"\\]*[?&]sig=[^&\s<>"\\]+')),
    "slack_token": (b"xox", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    "gitlab_token": (b"glpat-", re.compile(rb"glpat-[A-Za-z0-9_-]{20,}")),
    "stripe_key": (b"sk_live_", re.compile(rb"sk_live_[A-Za-z0-9]{20,}")),
    "npm_token": (b"npm_", re.compile(rb"npm_[A-Za-z0-9]{36}")),
    "jwt": (b"eyJ", re.compile(rb"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    "bearer_token": (None, re.compile(rb"(?i)\bBearer [A-Za-z0-9._~+/-]{20,}")),
    "credential_assignment": (None, re.compile(
        rb"(?i)(?P<prefix>(?:OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|HF_TOKEN|access_token|refresh_token|client_secret|service[-_]key)[\\\"'\t ]*[:=][\\\"'\t ]*)(?P<secret>[A-Za-z0-9_+/=.-]{24,})")),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()


def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def files(root):
    answer = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Unexpected link in frozen data: {path}")
        if path.is_file():
            answer.append(path)
    return answer


def local_credentials(root):
    values = set()

    def visit(value, name=""):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, key)
        elif isinstance(value, str) and len(value) >= 16 and any(
                word in name.lower() for word in ("token", "secret", "password", "key")):
            values.add(value.encode())

    auth = root / "experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json"
    if auth.is_file():
        visit(json.loads(auth.read_bytes()))
    key = root / "experiment/benchmarks/wildclaw_bench/runtime/inference-private/service-key"
    if key.is_file() and len(key.read_bytes().strip()) >= 16:
        values.add(key.read_bytes().strip())
    return values


def benchmark_fixtures(base, expected):
    relative = "prepared/tasks/sanitize-git-repo/tests/test_outputs.py"
    data = (base / relative).read_bytes()
    if sha(data) != expected[relative]:
        raise ValueError("Public benchmark fixture source differs from official file hash")
    values, records = set(), []
    for statement in ast.parse(data).body:
        if (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id.startswith("FAKE_"):
                    value = statement.value.value.encode()
                    values.update((value, value.lower()))
                    records.append({"name": target.id, "value_sha256": sha(value),
                                    "source": relative, "source_sha256": sha(data)})
    if len(records) != 5:
        raise ValueError("Unexpected public benchmark fake-credential constants")
    return values, records


def sanitize(data, known, fixtures=()):
    counts = Counter()
    for value in known:
        if value in data:
            counts["known_local_credential"] += data.count(value)
            data = data.replace(value, b"[REDACTED_KNOWN_LOCAL_CREDENTIAL]")
    lower = None
    for name, (marker, pattern) in PATTERNS.items():
        if marker is not None and marker not in data:
            continue
        if marker is None:
            lower = data.lower() if lower is None else lower
            if name == "bearer_token" and b"bearer " not in lower:
                continue
            if name == "credential_assignment" and not any(
                    p in lower for p in (b"api_key", b"hf_token", b"access_token", b"refresh_token", b"client_secret", b"service_key", b"service-key")):
                continue
        replacement = b"[REDACTED_" + name.upper().encode() + b"]"
        n = 0
        def replace(match):
            nonlocal n
            value = match.group("secret") if name == "credential_assignment" else match.group()
            if value in fixtures:
                return match.group()
            n += 1
            return match.group("prefix") + replacement if name == "credential_assignment" else replacement
        data = pattern.sub(replace, data)
        if n:
            counts[name] += n
            lower = None
    return data, counts


def write_public_documentation(destination):
    """Finish the browseable card and pinned upstream license without rebuilding archives."""
    manifest = json.loads((destination / "manifest.json").read_bytes())
    manifest["scan"]["credential_patterns_checked"] = sorted(PATTERNS)
    split = json.loads((destination / "task_split.json").read_bytes())
    redactions = json.loads((destination / "redactions.json").read_bytes())
    provenance = json.loads((destination / "provenance.json").read_bytes())
    license_bytes = (Path(__file__).resolve().parents[1] / "docs/TERMINAL_BENCH_LICENSE").read_bytes()
    license_sha = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
    if sha(license_bytes) != license_sha:
        raise ValueError("Bundled upstream task license differs from the reviewed copy")
    atomic(destination / "TERMINAL_BENCH_LICENSE", license_bytes)
    provenance["task_license_copy"] = {
        "file": "TERMINAL_BENCH_LICENSE", "sha256": license_sha,
        "source_url": "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/main/LICENSE",
        "retrieved_date": "2026-10-05"}
    atomic(destination / "provenance.json", encode(provenance))
    category = {}
    for row in split["tasks"]:
        key = (row["class_id"], row["class_label"])
        category.setdefault(key, Counter())[row["split"]] += 1
    table = "\n".join(f"| {key[0]} | {key[1]} | {counts['training']} | {counts['test']} |"
                      for key, counts in sorted(category.items()))
    archive_table = "\n".join(
        f"| [{name}]({name}) | {row['bytes']:,} | {row['file_count']:,} | `{row['sha256']}` |"
        for name, row in manifest["archives"].items())
    modified_pool = sum(row["path"].startswith(str(PREFIX / "prepared/pool") + "/")
                        for row in redactions["files"])
    changes = ", ".join(f"{key}: {value}" for key, value in sorted(redactions["counts"].items()))
    card = f"""# Terminal-Bench 2.1 frozen inputs

This directory adds the current Memory DCI baseline and V5 workflow inputs to `FlyPig23/memory_dci`. The existing WildClaw data at the dataset root is separate and unchanged.

The frozen split is **53 training tasks and 36 held-out test tasks**, covering all 89 tasks from [Terminal-Bench 2.1 registry revision 6](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/6). Registry package version `2.0.2` belongs to benchmark **2.1**. Browse exact task identities, versions, families and the ordered test schedule in [task_split.json](task_split.json) or [task_split.csv](task_split.csv). Split SHA-256: `{manifest['split_sha256']}`. The split bytes and 36-task order match the original baseline and V5 protocols; their source hashes are in [manifest.json](manifest.json).

The split was frozen with seed 20260908 before the experiment. Fourteen researcher-defined categories and 54 fine families describe task transfer structure. Twenty-nine multi-task families deliberately have members in both partitions; this is a task-level holdout, not an unseen-family evaluation.

| Category | Description | Training | Test |
|---|---|---:|---:|
{table}
| Total | | 53 | 36 |

The V5 pool contains **53 training task cards and 5,785 public historical trajectory bodies** from 5,830 source records. Forty-five bodies were unavailable from the official source; 5,782 bodies use rendered ATIF and 3 use the native fallback. All available bodies match the exact training task version. No held-out task trajectory is included. Baseline execution needs only metadata and task packages; V5 also needs the pool. This release contains inputs and provenance, not outcomes from the current live experiments.

| Archive | Compressed bytes | Files | SHA-256 |
|---|---:|---:|---|
{archive_table}

From a checkout of [Memory_DCI](https://github.com/FlyPig23/Memory_DCI), restore and verify everything with:

```bash
python3 -m experiment.benchmarks.terminal_bench_2_1.scripts.download_data
```

Add `--without-pool` for baseline inputs, `--revision DATASET_COMMIT` to select an immutable publication, or `--local-dataset /path/to/terminal_bench_2_1` for downloaded archives. `--root /path/to/fresh/checkout` changes the destination; `--verify-only` checks existing restored inputs. The downloader checks archive and per-file SHA-256, task registry digests, split identity/order, and training-only pool membership; conflicting local data is rejected. It never starts an experiment. Archive paths restore under `experiment/benchmarks/terminal_bench_2_1/`.

Public export sanitization changed **{modified_pool} trajectory files** and updated their pool-manifest hashes. Replacements: {changes}. [redactions.json](redactions.json) records source and exported hashes for every changed file; original data is not claimed byte-identical for those derivatives. All official task package files remain byte-identical. Five synthetic `FAKE_*` credential constants explicitly defined in the official `sanitize-git-repo` test fixture are preserved because they are benchmark inputs; their source file and value hashes are recorded as scanner exceptions. No known local credentials or unexpected scanned credential patterns remain. Generated OpenSSL private keys are redacted from trajectory bodies. Authentication files, private homes, current local runs, raw duplicate downloads, and caches are excluded.

Task definitions come from the [official task repository](https://github.com/harbor-framework/terminal-bench-2-1), which uses [Apache-2.0](TERMINAL_BENCH_LICENSE). Existing file-level notices are preserved. This is **not** a blanket Apache license for third-party historical trajectories or embedded assets. The current Hub trajectory pages do not provide an independently identified blanket redistribution license; source-specific rights and the [Hub terms](https://hub.harborframework.com/terms) continue to apply. Per-record source URLs, task versions and source hashes are retained in the archived manifests. See [provenance.json](provenance.json) for source details and the upstream license hash.
"""
    atomic(destination / "README.md", card.encode())
    manifest["supplementary_files"] = {
        name: {"sha256": hash_file(destination / name), "bytes": (destination / name).stat().st_size}
        for name in ("README.md", "TERMINAL_BENCH_LICENSE", "provenance.json", "redactions.json", "task_split.json", "task_split.csv")}
    atomic(destination / "manifest.json", encode(manifest))


def package(source_root, destination):
    source_root, destination = source_root.resolve(), destination.resolve()
    if destination.is_relative_to(source_root):
        raise ValueError("Export destination must be outside the source project")
    base = source_root / PREFIX
    split_bytes = (base / "manifests/split.json").read_bytes()
    split = json.loads(split_bytes)
    train, test = split["training_task_ids"], split["test_task_ids"]
    if len(train) != len(set(train)) or len(test) != len(set(test)):
        raise ValueError("Unexpected split")
    if len(train) != 53 or len(test) != 36 or set(train) & set(test):
        raise ValueError("Expected disjoint frozen 53/36 split")
    schedules = {}
    for name in ("protocol.json", "baseline_protocol.json"):
        raw = (base / "manifests" / name).read_bytes()
        doc = json.loads(raw)
        if [row["task_id"] for row in doc["tasks"]] != test:
            raise ValueError(f"Frozen {name} differs from test order")
        schedules[name] = sha(raw)
    pool_source = (base / "manifests/pool.json").read_bytes()
    pool = json.loads(pool_source)
    if set(pool["training_task_ids"]) != set(train) or pool["split_sha256"] != sha(split_bytes):
        raise ValueError("Training pool is not bound to the frozen split")
    if set(pool["test_task_ids_excluded"]) != set(test):
        raise ValueError("Pool test exclusion differs from split")
    known = local_credentials(source_root)
    destination.mkdir(parents=True, exist_ok=True)
    inventory, redactions, totals, archives = [], [], Counter(), {}
    pool_updates = {}
    selections = {
        "tasks": files(base / "prepared/tasks"),
        "pool": files(base / "prepared/pool"),
        "metadata": [base / "manifests" / name for name in METADATA],
    }
    task_rows = json.loads((base / "manifests/task_files.json").read_bytes())
    official = {f"prepared/tasks/{task['task_name']}/{f['path']}": f["sha256"]
                for task in task_rows for f in task["files"]}
    fixtures, fixture_records = benchmark_fixtures(base, official)
    if {str(p.relative_to(base)) for p in selections["tasks"]} != set(official):
        raise ValueError("Official task package inventory differs")
    if {str(p.relative_to(base / "prepared/pool")) for p in selections["pool"]} != set(pool["files_sha256"]):
        raise ValueError("Training pool inventory differs")
    for bundle, paths in selections.items():
        archive = destination / f"{bundle}.tar.gz"
        fd, name = tempfile.mkstemp(prefix=f".{bundle}.", suffix=".tar.gz.part", dir=destination)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
                with tarfile.open(fileobj=gz, mode="w|") as tar:
                    for path in sorted(paths):
                        original = path.read_bytes()
                        relative = str(path.relative_to(source_root))
                        if bundle == "tasks" and sha(original) != official[str(path.relative_to(base))]:
                            raise ValueError(f"Official task file differs: {relative}")
                        if bundle == "pool" and sha(original) != pool["files_sha256"][str(path.relative_to(base / "prepared/pool"))]:
                            raise ValueError(f"Frozen pool file differs: {relative}")
                        data, counts = sanitize(original, known, fixtures)
                        if bundle in {"tasks", "metadata"} and data != original:
                            raise ValueError(f"Credential candidate in exact official/config data requires review: {relative}")
                        if counts:
                            totals.update(counts)
                            redactions.append({"path": relative, "source_sha256": sha(original),
                                               "export_sha256": sha(data), "replacements": dict(counts)})
                            pool_updates[str(path.relative_to(base / "prepared/pool"))] = sha(data)
                        if bundle == "metadata" and path.name == "pool.json" and pool_updates:
                            exported = json.loads(original)
                            for name, value in pool_updates.items():
                                exported["files_sha256"][name] = value
                            for row in exported["trajectories"]:
                                if row.get("pool_file") in pool_updates:
                                    row["original_pool_sha256"] = row["pool_sha256"]
                                    row["pool_sha256"] = pool_updates[row["pool_file"]]
                            exported["public_export"] = {"sanitized_files": len(pool_updates),
                                "source_manifest_sha256": sha(original), "changes": "Credential material redacted; consult public redactions.json."}
                            data = encode(exported)
                            redactions.append({"path": relative, "source_sha256": sha(original),
                                               "export_sha256": sha(data), "change": "Update exported pool hashes while retaining source hashes."})
                        if sanitize(data, known, fixtures)[1]:
                            raise ValueError(f"Credential pattern remains after sanitizing: {relative}")
                        item = tarfile.TarInfo(relative)
                        item.size, item.mode, item.mtime = len(data), 0o644, 0
                        if path.stat().st_mode & 0o111:
                            item.mode = 0o755
                        tar.addfile(item, io.BytesIO(data))
                        inventory.append({"bundle": bundle, "path": relative, "bytes": len(data),
                                          "sha256": sha(data), "source_sha256": sha(original)})
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        archives[archive.name] = {"bytes": archive.stat().st_size, "sha256": hash_file(archive),
                                  "file_count": len(paths), "uncompressed_bytes": sum(r["bytes"] for r in inventory if r["bundle"] == bundle)}
        print(json.dumps({"completed": bundle, **archives[archive.name]}), flush=True)
    manifest = {"schema_version": 1, "dataset_repo": DATASET, "dataset_subdirectory": "terminal_bench_2_1",
                "benchmark": "Terminal-Bench 2.1", "archive_prefix": str(PREFIX), "archives": archives,
                "files": inventory, "training_task_ids": train, "test_task_ids": test,
                "split_sha256": sha(split_bytes), "original_protocol_sha256": schedules,
                "source_records": pool["source_record_count"], "trajectory_bodies": pool["trajectory_count"],
                "confirmed_unavailable_bodies": pool["confirmed_unavailable_count"],
                "scan": {"unexpected_credential_patterns_remaining": 0, "known_local_credentials_checked": True,
                         "public_benchmark_fixture_exceptions": fixture_records, "replacement_counts": dict(totals)}}
    atomic(destination / "manifest.json", encode(manifest))
    atomic(destination / "redactions.json", encode({"counts": dict(totals), "files": redactions}))
    atomic(destination / "task_split.json", split_bytes)
    output = io.StringIO(newline="")
    columns = ("task_id", "split", "test_order", "class_id", "class_label", "fine_family", "task_version_id", "task_revision")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in split["tasks"]:
        writer.writerow({**{key: row.get(key, "") for key in columns},
                         "test_order": test.index(row["task_id"]) + 1 if row["task_id"] in test else ""})
    atomic(destination / "task_split.csv", output.getvalue().encode())
    atomic(destination / "provenance.json", encode({
        "dataset_pin": json.loads((base / "manifests/dataset_pin.json").read_bytes()),
        "task_license": "Apache-2.0; upstream task repository, not a blanket license for third-party trajectories/assets",
        "task_source": "https://github.com/harbor-framework/terminal-bench-2-1",
        "trajectory_source": "https://hub.harborframework.com/",
        "trajectory_license": "No independent blanket redistribution license was identified for current Hub third-party trajectories; retain source-specific rights and terms.",
        "source_terms": "https://hub.harborframework.com/terms",
        "omitted": ["live local runs/results", "credentials and private homes", "raw duplicate historical downloads", "Docker images and environment caches"],
        "scope": "All 89 fixed official task packages, exact 53/36 split, prepared training pool and source/version manifests. Not a completed experiment-results release."}))
    write_public_documentation(destination)
    print(json.dumps({"ready": str(destination), "files": len(inventory), "redacted_files": len(redactions),
                      "scan": manifest["scan"]}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    package(args.source_root, args.destination)
