"""Download, restore and verify frozen V8 inputs and optional scored evidence.

No credentials, Docker containers, graders or models are used. Conflicting local
files are rejected. --include-evidence also verifies the original 175 receipts.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import urllib.parse

from experiment.shared.dataset_io import _contained, download, restore, sha256
from .package_data import PREFIX, CORPUS, RUNS, source_references

ROOT = Path(__file__).resolve().parents[4]
REPO = "FlyPig23/memory_dci"
SUBDIRECTORY = "terminal_bench_4"
RELEASE_REVISION = "db28540352886750100bd94e45a09bb80adae92b"
RELEASE_MANIFEST_SHA256 = "44cf6e30834056f475be6901125ea6de81a0af91c1d4daf6c0e753f4d560e4f7"
DEFAULT_BUNDLES = ("metadata", "tasks", "corpus", "sources")
PUBLIC_MANIFEST = "manifests/v8_publication_manifest.json"


def load(path):
    return json.loads(Path(path).read_bytes())


def validate_manifest(manifest):
    if (manifest.get("schema_version") != 1 or manifest.get("dataset_repo") != REPO
            or manifest.get("dataset_subdirectory") != SUBDIRECTORY or manifest.get("archive_prefix") != str(PREFIX)):
        raise ValueError("Unexpected V8 publication manifest")
    paths = set()
    for row in manifest["files"]:
        path = PurePosixPath(row["path"])
        if (path.is_absolute() or ".." in path.parts or not path.is_relative_to(PurePosixPath(PREFIX))
                or path.as_posix() != row["path"] or row["path"] in paths
                or row["bundle"] not in (*DEFAULT_BUNDLES, "evidence")):
            raise ValueError("Invalid or duplicate V8 archive path")
        paths.add(row["path"])
    for bundle in (*DEFAULT_BUNDLES, "evidence"):
        if sum(r["bundle"] == bundle for r in manifest["files"]) != manifest["archives"][bundle + ".tar.gz"]["file_count"]:
            raise ValueError("Archive file count differs from manifest")


def task_content_hash(directory, root, hashes):
    """Harbor hashes POSIX path strings in lexicographic order, not Path tuples."""
    outer = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if path.is_symlink():
            raise ValueError("Task contains a symlink")
        if path.is_file():
            digest = hashes.get(str(path.relative_to(root)))
            if digest is None:
                raise ValueError("Task contains a file absent from the publication")
            outer.update((str(path.relative_to(directory)) + "\0" + digest + "\n").encode())
    return outer.hexdigest()


def verify_receipts(root, manifest):
    """Recompute score and check original receipt hashes, allowing declared redactions only."""
    base = root / PREFIX
    records = {row["path"]: row for row in manifest["files"]}
    audit = load(base / "manifests/v8_report_audit_20261008.json")
    trials = audit["metrics"]["trial_records"]
    per_task = {}
    seen = set()
    for trial in trials:
        identity = trial["task_id"], trial["rollout"]
        if identity in seen:
            raise ValueError("Duplicate task/rollout receipt")
        seen.add(identity)
        for field in ("completion", "result"):
            relative = trial[field + "_path"]
            if records[str(PREFIX / relative)]["source_sha256"] != trial[field + "_sha256"]:
                raise ValueError("Original result receipt hash differs from report audit")
        completion = load(base / trial["completion_path"])
        result = load(base / trial["result_path"])
        reward = result["harbor"]["verifier_result"]["rewards"]["reward"]
        if (reward not in (0, 1) or completion["reward"] != reward or completion["valid"] is not True
                or completion["task_id"] != trial["task_id"] or completion["rollout"] != trial["rollout"]
                or reward != trial["reward"] or trial["valid"] is not True):
            raise ValueError("Result/completion/audit scores or task identity disagree")
        directory = Path(trial["completion_path"]).parent
        for name, digest in trial["audit_sha256"].items():
            if records[str(PREFIX / directory / name)]["source_sha256"] != digest:
                raise ValueError("Original trial audit receipt differs")
        per_task.setdefault(trial["task_id"], {})[trial["rollout"]] = reward
    if (len(trials) != 175 or set(per_task) != set(manifest["evaluated_task_ids"])
            or any(set(rows) != {1, 2, 3, 4, 5} for rows in per_task.values())):
        raise ValueError("Expected the frozen 35 tasks, each with five scored receipts")
    official = load(base / "manifests/official_trials.json")["trials"]
    versions = {t["task_id"]: t["content_hash"] for t in load(base / "manifests/tb4_tasks.json")["tasks"]}
    sol = [r for r in official if r["model"] == "GPT-5.6 Sol" and r["task_id"] in per_task
           and r["task_content_hash"] == versions[r["task_id"]] and r["reward"] in (0, 1)]
    if (len(sol) != 175 or Counter(r["task_id"] for r in sol) != Counter({task: 5 for task in per_task})
            or len({r["trial_id"] for r in sol}) != 175):
        raise ValueError("Historical Sol comparator does not contain five unique same-version trials per task")
    pooled_ids = set()
    for task in manifest["selected_task_ids"]:
        row = load(base / f"prepared/v8_corpus/{CORPUS}/audit/{task}.json")
        pooled_ids.update(r["trial_id"] for r in row["records"] if r.get("pool_file"))
    failures = {r["trial_id"] for r in sol if r["reward"] == 0}
    if len(failures) != 158 or not failures <= pooled_ids:
        raise ValueError("Historical Sol failure/query-pool overlap differs")
    for task, rewards in per_task.items():
        expected = audit["metrics"]["per_task"][task]
        if ([rewards[k] for k in range(1, 6)] != expected["v8_rollouts"]
                or sum(r["reward"] for r in sol if r["task_id"] == task) != expected["official_sol_passes"]):
            raise ValueError("Per-task report row differs from original receipts")
    # The report references original source paths; relocated source files are
    # resolved through the immutable publication map, not recreated as workflows.
    snapshots = {r["original_path"]: r for r in manifest["source_snapshot"]["files"]}
    refs = source_references(audit)
    for relative, digest in refs.items():
        path = (base / relative).resolve().relative_to(root.resolve()).as_posix()
        row = records.get(path)
        if row is None and path in snapshots:
            row = records[snapshots[path]["published_path"]]
        if row is None or row["source_sha256"] != digest:
            raise ValueError(f"Audited source is missing or differs: {relative}")
    parent, extension = [load(base / "runs" / name / "protocol.json") for name in RUNS]
    if extension["parent_protocol_sha256"] != sha256(base / "runs" / RUNS[0] / "protocol.json"):
        raise ValueError("The extension is not bound to the original protocol")
    oracle = load(base / "runs" / RUNS[0] / "oracle/cad-model__oracle/oracle.json")
    if oracle["passed"] or oracle["reward"] != 0 or extension["excluded_by_parent_oracle"] != ["cad-model"]:
        raise ValueError("Frozen oracle exclusion differs")
    score = sum(sum(rows.values()) for rows in per_task.values())
    reference = sum(r["reward"] for r in sol)
    if (score, reference) != (66, 17):
        raise ValueError("Published formal score differs from 66/175 versus 17/175")
    return {"v8_passes": score, "v8_trials": len(trials), "official_sol_passes": reference,
            "official_sol_trials": len(sol), "official_sol_failures_in_query_pool": len(failures),
            "explicit_report_source_hashes": len(refs), "oracle_excluded": ["cad-model"],
            "score_scope": "Original official scores, including the documented bun r1 test-coverage defect"}


def verify_dataset(root, manifest, *, include_evidence=False):
    root = Path(root).resolve()
    validate_manifest(manifest)
    bundles = set(DEFAULT_BUNDLES) | ({"evidence"} if include_evidence else set())
    hashes = {}
    for row in manifest["files"]:
        if row["bundle"] not in bundles:
            continue
        path = _contained(root / row["path"], root)
        if not path.is_file() or path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            raise ValueError(f"Restored publication file differs: {row['path']}")
        hashes[row["path"]] = row["sha256"]
    base = root / PREFIX
    for relative in ("prepared/tasks", "prepared/v8_corpus", "source_snapshots/v8_original"):
        directory = base / relative
        expected = {name for name in hashes if PurePosixPath(name).is_relative_to(PurePosixPath(PREFIX / relative))}
        paths = list(directory.rglob("*"))
        if (any(p.is_symlink() for p in paths)
                or {str(p.relative_to(root)) for p in paths if p.is_file()} != expected):
            raise ValueError(f"Unexpected files or links in frozen input directory: {relative}")
    for source in manifest["source_snapshot"]["files"]:
        if hashes.get(source["published_path"]) != source["sha256"]:
            raise ValueError("Original source snapshot differs from its publication map")
    selection = load(base / "manifests/v8_tasks.json")["selected"]
    evaluated = manifest["evaluated_task_ids"]
    if (selection != manifest["selected_task_ids"] or len(selection) != 36 or len(set(selection)) != 36
            or len(evaluated) != 35 or set(selection) - set(evaluated) != {"cad-model"}
            or len(set(evaluated)) != 35):
        raise ValueError("Frozen selected/evaluated task identities or order differ")
    versions = {r["task_id"]: r for r in load(base / "manifests/tb4_tasks.json")["tasks"]}
    for task in selection:
        directory = base / "prepared/tasks" / task
        if task_content_hash(directory, root, hashes) != versions[task]["content_hash"]:
            raise ValueError("Official task package content hash differs")
    corpus = base / "prepared/v8_corpus" / manifest["corpus"]
    index = load(corpus / "index.json")
    if (index["selection_sha256"] != sha256(base / "manifests/v8_tasks.json")
            or index["source_manifest_sha256"] != sha256(base / "manifests/v8_failure_sources.json")
            or index["decisions_sha256"] != sha256(base / "prepared/v8_corpus/decontamination_decisions.json")
            or set(index["tasks"]) != set(selection)):
        raise ValueError("Corpus is not bound to the frozen selection and provenance")
    for task, row in index["tasks"].items():
        pool = base / row["pool_path"]
        inventory = {str(p.relative_to(pool)): hashes.get(str(p.relative_to(root)))
                     for p in pool.rglob("*") if p.is_file()}
        if (inventory != row["pool_files_sha256"] or any(p.is_symlink() for p in pool.rglob("*"))
                or sha256(pool / "manifest.json") != row["pool_manifest_sha256"]):
            raise ValueError("Query-pool inventory differs from its frozen index")
    count = sum(r["trajectory_count"] for r in index["tasks"].values())
    seeds = load(corpus / "seeds_index.json")["tasks"]
    entries = sum(r["entry_count"] for r in seeds.values())
    if count != 3755 or entries != 291 or set(seeds) != set(selection):
        raise ValueError("Expected 3,755 trajectories and 291 seed entries covering 36 tasks")
    for row in seeds.values():
        if sha256(base / row["seed_path"] / "manifest.json") != row["manifest_sha256"]:
            raise ValueError("Seed manifest differs")
    result = {"selected_tasks": len(selection), "evaluated_tasks": len(evaluated), "trajectories": count,
              "seed_entries": entries, "verified_files": len(hashes), "evidence_restored": include_evidence,
              "unavailable_historical_source_versions": manifest["source_snapshot"]["unavailable_historical_versions"]}
    if include_evidence:
        result["results"] = verify_receipts(root, manifest)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--revision", default=RELEASE_REVISION)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--local-dataset", type=Path)
    parser.add_argument("--include-evidence", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    saved = root / PREFIX / PUBLIC_MANIFEST
    if args.verify_only:
        manifest = load((args.local_dataset / "manifest.json") if args.local_dataset else saved)
        print(json.dumps(verify_dataset(root, manifest, include_evidence=args.include_evidence), indent=2))
        return
    cache = (args.local_dataset or args.cache_dir or root / ".cache/memory_dci/terminal_bench_4").resolve()
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{urllib.parse.quote(args.revision, safe='')}/{SUBDIRECTORY}/"
    if not args.local_dataset:
        download(url + "manifest.json", cache / "manifest.json",
                 RELEASE_MANIFEST_SHA256 if args.revision == RELEASE_REVISION else None)
    manifest = load(cache / "manifest.json")
    validate_manifest(manifest)
    for row in manifest["files"]:
        _contained(root / row["path"], root)
    for bundle in (*DEFAULT_BUNDLES, *(("evidence",) if args.include_evidence else ())):
        name = bundle + ".tar.gz"
        spec, archive = manifest["archives"][name], cache / name
        if not args.local_dataset:
            download(url + name, archive, spec["sha256"])
        if archive.stat().st_size != spec["bytes"] or sha256(archive) != spec["sha256"]:
            raise ValueError(f"Archive checksum differs: {name}")
        restore(archive, root, [row for row in manifest["files"] if row["bundle"] == bundle])
        print(f"Verified and restored {name}", flush=True)
    data = (cache / "manifest.json").read_bytes()
    _contained(saved, root)
    if saved.exists() and saved.read_bytes() != data:
        raise ValueError("An existing V8 publication manifest differs")
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_bytes(data)
    print(json.dumps(verify_dataset(root, manifest, include_evidence=args.include_evidence), indent=2))


if __name__ == "__main__":
    main()
