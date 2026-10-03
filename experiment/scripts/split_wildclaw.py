#!/usr/bin/env python3
"""Freeze a prompt-only, family-disjoint WildClawBench pilot split.

This script never loads trajectories, scores, task assets, or evaluator payloads.
The reviewed official parser reads task Markdown, but only an explicit public
field allowlist is retained. HF input auditing uses exec file names and hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import importlib.util
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

SCHEMA_VERSION = 1
SEED = 20260908
AUDITED_CODE_REVISION = "316334ccc4a87b9b5635ad73da99b4dfc0b3887e"
AUDITED_TASK_REVISION = "75f945578aa00cbdb8f46e4d42e4f4e98f704b4f"
CATEGORIES = {
    "01": ("Productivity Flow", "01_Productivity_Flow", 10),
    "02": ("Code Intelligence", "02_Code_Intelligence", 12),
    "03": ("Social Interaction", "03_Social_Interaction", 6),
    "04": ("Search & Retrieval", "04_Search_Retrieval", 11),
    "05": ("Creative Synthesis", "05_Creative_Synthesis", 11),
    "06": ("Safety Alignment", "06_Safety_Alignment", 10),
}
# Decisions were made from public prompts and resource metadata before selecting
# a split. A shared model/library alone is not a near-duplicate task family.
FAMILY_RULES = [
    ("paper_digest", "01", (1, 10), "Conservative template group: the same six-way paper classification and paper-content extraction workflow. Corpus identity is not asserted."),
    ("sam3_predictions", "02", (1, 2), "Same SAM3 inference code, test image and predictions output; inference construction and debugging can reveal overlapping answers."),
    ("jigsaw", "02", (3, 4, 5), "Near-identical jigsaw prompt/output template, with grid sizes and difficulty changed."),
    ("connect_the_dots", "02", (7, 12), "Numbered-point connection template with difficulty/grouping changed."),
    ("link_a_pix", "02", (8, 9), "Same Link-a-Pix rules and outputs with different difficulty/input representations."),
    ("academic_homepage", "02", (10, 11), "Same academic-homepage template and screenshot workflow; a reference screenshot is byte-identical across the two tasks."),
    ("project_status", "03", (4, 6), "Conservative workflow-template group: reconcile revised project messages and draft the current status for a stakeholder. Projects and business facts differ."),
    ("cpython_introduction", "04", (4, 10), "Near-identical budgeted CPython feature-introduction/version/PR evidence-chain template."),
    ("football_events", "05", (1, 2), "The preparation script copies the same match video; goal/event timestamps directly overlap between report and highlights."),
    ("product_launch_video", "05", (5, 11), "Conservative shared-content group: the same launch recording supplies product extraction and the first-two-minute transcription/dubbing task."),
    ("repository_push", "06", (2, 3), "Near-identical push of the same mm_agents project; shared repository content and closely related credential scenarios."),
]


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes())


def largest_remainder_quotas(counts: dict[str, int], total_build: int = 36) -> dict[str, int]:
    """Exact integer Hamilton quotas; category ID ascending breaks ties."""
    total = sum(counts.values())
    if total <= 0 or not 0 <= total_build <= total:
        raise ValueError("Invalid total or build target")
    result = {k: n * total_build // total for k, n in counts.items()}
    order = sorted(counts, key=lambda k: (-(counts[k] * total_build % total), k))
    for key in order[:total_build - sum(result.values())]:
        result[key] += 1
    return result


def git_output(source_dir: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source_dir), *args])


def verify_pinned_file(source_dir: Path, path: Path) -> None:
    relative = path.relative_to(source_dir).as_posix()
    committed = git_output(source_dir, "show", f"{AUDITED_CODE_REVISION}:{relative}")
    if path.read_bytes() != committed:
        raise ValueError(f"Reviewed source changed: {relative}")


def load_official_parser(source_dir: Path):
    parser_path = source_dir / "src/utils/task_parser.py"
    verify_pinned_file(source_dir, parser_path)
    spec = importlib.util.spec_from_file_location("wildclaw_reviewed_task_parser", parser_path)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot import official task parser")
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = previous
    return module.parse_task_md


def public_task(parsed: dict[str, Any], source_dir: Path, path: Path) -> dict[str, Any]:
    """Allowlist the parser result; never serialize checks/env/warmup/skills."""
    task_id = str(parsed["task_id"])
    category_id = path.parent.name[:2]
    name, directory, _ = CATEGORIES[category_id]
    if path.parent.name != directory or not task_id.startswith(directory + "_task_"):
        raise ValueError(f"Task ID/category mismatch: {task_id}")
    prompt = parsed["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"Missing public prompt: {task_id}")
    workspace = Path(parsed["workspace_path"]).resolve().relative_to(source_dir)
    urls = sorted(set(re.findall(r"https?://[^\s`<>\[\]，。；（）]+", prompt)))
    urls = [url.rstrip(").,;") for url in urls]
    return {
        "task_id": task_id,
        "category_id": category_id,
        "category_name": name,
        "category_directory": directory,
        "source_path": path.relative_to(source_dir).as_posix(),
        "source_sha256": file_sha256(path),
        "prompt": prompt,
        "prompt_sha256": sha256(prompt.encode("utf-8")),
        "public_workspace_relpath": workspace.as_posix(),
        "public_exec_relpath": (workspace / "exec").as_posix(),
        "public_resources": [{"kind": "prompt_url", "value": url} for url in urls],
    }


def extract_tasks(source_dir: Path) -> list[dict[str, Any]]:
    source_dir = source_dir.resolve()
    revision = git_output(source_dir, "rev-parse", "HEAD").decode().strip()
    if revision != AUDITED_CODE_REVISION:
        raise ValueError("Code revision changed; a new prompt/family audit is required")
    parse = load_official_parser(source_dir)
    tasks = []
    for path in sorted((source_dir / "tasks").glob("[0-9][0-9]_*/[0-9][0-9]_*.md")):
        verify_pinned_file(source_dir, path)
        tasks.append(public_task(parse(path), source_dir, path))
    ids = [task["task_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate task IDs")
    expected = {key: value[2] for key, value in CATEGORIES.items()}
    if dict(Counter(t["category_id"] for t in tasks)) != expected:
        raise ValueError("Official 60-task universe changed")
    return sorted(tasks, key=lambda t: t["task_id"])


def load_public_inventory(path: Path, tasks: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("sha") != AUDITED_TASK_REVISION:
        raise ValueError("Task resource revision changed; a new resource audit is required")
    by_root = {t["public_exec_relpath"]: t for t in tasks}
    public_files = []
    for item in raw["siblings"]:
        name = item["rfilename"]
        parts = PurePosixPath(name).parts
        # Only workspace/<category>/<task>/exec/... is public input metadata.
        if len(parts) < 5 or parts[0] != "workspace" or parts[3] != "exec":
            continue
        root = "/".join(parts[:4])
        if root not in by_root:
            raise ValueError(f"Unmapped public resource: {name}")
        if ".." in parts or name.startswith("/"):
            raise ValueError("Unsafe resource path")
        task = by_root[root]
        lfs = item.get("lfs") or {}
        digest = lfs.get("sha256") or item.get("blobId")
        if digest is None:
            raise ValueError(f"Resource has no source digest: {name}")
        record = {
            "task_id": task["task_id"], "path": name, "size": item.get("size"),
            "hash_algorithm": "sha256" if lfs.get("sha256") else "git_blob_sha1",
            "hash": digest,
        }
        public_files.append(record)
    public_files.sort(key=lambda item: item["path"])
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in public_files:
        by_task[item["task_id"]].append(item)
    for task in tasks:
        records = by_task[task["task_id"]]
        task["public_input_inventory"] = {
            "file_count": len(records), "bytes": sum(r["size"] or 0 for r in records),
            "metadata_sha256": sha256(canonical_bytes(records)),
            "verification": "remote_metadata_only; byte verification belongs to download stage",
        }
    metadata = {
        "dataset": "internlm/WildClawBench", "revision": raw["sha"],
        "inventory_sha256": file_sha256(path),
        "public_exec_inventory_sha256": sha256(canonical_bytes(public_files)),
        "public_exec_file_count": len(public_files),
        "archive_member_audit": "Not inspected; no archive contents or gold were read",
    }
    return metadata, public_files


def build_families(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {}
    for task in tasks:
        number = int(re.search(r"_task_(\d+)_", task["task_id"]).group(1))
        index[(task["category_id"], number)] = task
    families = []
    assigned = set()
    for label, category, numbers, rationale in FAMILY_RULES:
        members = [index[(category, number)]["task_id"] for number in numbers]
        if assigned.intersection(members):
            raise ValueError("Family rules overlap; merge them explicitly")
        family_id = f"{category}_{label}"
        families.append({"family_id": family_id, "task_ids": sorted(members), "rationale": rationale,
                         "basis": "public_prompt_and_public_resource_metadata",
                         "prompt_sha256": {index[(category, number)]["task_id"]: index[(category, number)]["prompt_sha256"] for number in numbers}})
        assigned.update(members)
    for task in tasks:
        if task["task_id"] not in assigned:
            families.append({"family_id": task["task_id"], "task_ids": [task["task_id"]],
                             "rationale": "No near-duplicate task identified in the reviewed public prompts/input metadata.",
                             "basis": "public_prompt_and_public_resource_metadata",
                             "prompt_sha256": {task["task_id"]: task["prompt_sha256"]}})
    owner = {task_id: f["family_id"] for f in families for task_id in f["task_ids"]}
    for task in tasks:
        task["family_id"] = owner[task["task_id"]]
    return sorted(families, key=lambda f: f["family_id"])


def resource_overlap_audit(public_files: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_digest: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in public_files:
        if item["size"]:
            by_digest[(item["hash_algorithm"], item["hash"])].append(item)
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (algorithm, digest), records in sorted(by_digest.items()):
        for pair in itertools.combinations(sorted(set(r["task_id"] for r in records)), 2):
            pairs[pair].append({"hash_algorithm": algorithm, "hash": digest,
                                "paths": sorted(r["path"] for r in records if r["task_id"] in pair)})
    by_id = {t["task_id"]: t for t in tasks}
    result = []
    for pair, matches in sorted(pairs.items()):
        same_family = by_id[pair[0]]["family_id"] == by_id[pair[1]]["family_id"]
        if same_family:
            reason = "Task content/template overlap is independently established; keep the complete family together."
        elif all(t.startswith("03_") for t in pair):
            reason = "Shared mock_services/_base.py transport implementation; distinct scenario data and objectives do not make all social tasks duplicates."
        elif all(t.startswith("06_") for t in pair) and any("risk_os_operation" in t for t in pair):
            reason = "Shared third-party repository files in a deletion-risk workspace; task requires different behavior from pushing a repository."
        else:
            reason = "Incidental metadata/placeholder file match, not shared task answer content."
        result.append({"task_ids": list(pair), "same_family": same_family,
                       "shared_nonempty_hash_count": len(matches), "matches": matches,
                       "decision": "group" if same_family else "shared_resource_only", "rationale": reason})
    return result


def grouped_split(tasks: list[dict[str, Any]], families: list[dict[str, Any]], quotas: dict[str, int], seed: int) -> tuple[list[str], list[str], dict[str, Any]]:
    """Count all feasible grouped assignments, then select a hash-seeded rank.

    The DP handles families spanning categories. It fails rather than silently
    breaking a family or changing category quotas when no exact split exists.
    """
    by_id = {task["task_id"]: task for task in tasks}
    categories = sorted(quotas)
    members = [task_id for family in families for task_id in family["task_ids"]]
    if len(members) != len(set(members)) or set(members) != set(by_id):
        raise ValueError("Families must partition the complete task universe")
    ordered = sorted(families, key=lambda f: (min(by_id[t]["category_id"] for t in f["task_ids"]), f["family_id"]))
    weights = [tuple(sum(by_id[t]["category_id"] == category for t in f["task_ids"]) for category in categories) for f in ordered]
    suffix = [tuple(0 for _ in categories) for _ in range(len(ordered) + 1)]
    for i in range(len(ordered) - 1, -1, -1):
        suffix[i] = tuple(a + b for a, b in zip(weights[i], suffix[i + 1]))

    @lru_cache(maxsize=None)
    def count(i: int, remaining: tuple[int, ...]) -> int:
        if any(r < 0 or r > available for r, available in zip(remaining, suffix[i])):
            return 0
        if i == len(ordered):
            return int(not any(remaining))
        included = tuple(r - w for r, w in zip(remaining, weights[i]))
        return count(i + 1, remaining) + count(i + 1, included)

    remaining = tuple(quotas[category] for category in categories)
    solutions = count(0, remaining)
    if not solutions:
        raise ValueError("No family-disjoint assignment satisfies the exact quotas; revise the design explicitly, never split a family")
    seed_material = f"wildclaw-family-split-v1:{seed}".encode()
    rank = int.from_bytes(hashlib.sha256(seed_material).digest(), "big") % solutions
    selected_rank = rank
    build = []
    for i, family in enumerate(ordered):
        excluded_count = count(i + 1, remaining)
        if rank >= excluded_count:
            rank -= excluded_count
            remaining = tuple(r - w for r, w in zip(remaining, weights[i]))
            build.extend(family["task_ids"])
    if any(remaining) or rank:
        raise AssertionError("Internal assignment decoding error")
    build = sorted(build)
    test = sorted(set(by_id) - set(build))
    return build, test, {"algorithm": "exact_category_family_subset_count_then_sha256_seeded_rank_v1",
                         "category_order": categories, "family_order": [f["family_id"] for f in ordered],
                         "feasible_assignment_count": solutions, "selected_assignment_rank": selected_rank,
                         "seed_material": seed_material.decode(),
                         "tie_break": "UTF-8 task/family/category identifiers sorted ascending"}


def choose_dev(tasks: list[dict[str, Any]], build_ids: list[str], seed: int) -> dict[str, list[str]]:
    build_set = set(build_ids)
    build = [task for task in tasks if task["task_id"] in build_set]
    dev = []
    for category in sorted({task["category_id"] for task in tasks}):
        candidates = [task for task in build if task["category_id"] == category]
        if not candidates:
            raise ValueError(f"No build task available for category {category} dev")
        chosen = min(candidates, key=lambda t: (sha256(f"wildclaw-dev-v1:{seed}:{t['task_id']}".encode()), t["task_id"]))
        dev.append(chosen["task_id"])
    dev_families = {t["family_id"] for t in build if t["task_id"] in dev}
    excluded = sorted(t["task_id"] for t in build if t["family_id"] in dev_families)
    return {"dev_task_ids": sorted(dev), "dev_excluded_family_task_ids": excluded,
            "dev_corpus_task_ids": sorted(build_set - set(excluded))}


def validate_split(tasks: list[dict[str, Any]], families: list[dict[str, Any]], split: dict[str, Any]) -> None:
    universe = {t["task_id"] for t in tasks}
    lists = ["build_task_ids", "test_task_ids", "dev_task_ids", "dev_excluded_family_task_ids", "dev_corpus_task_ids"]
    for key in lists:
        if len(split[key]) != len(set(split[key])) or not set(split[key]) <= universe:
            raise ValueError(f"Invalid task IDs in {key}")
    build, test = set(split[lists[0]]), set(split[lists[1]])
    if build & test or build | test != universe:
        raise ValueError("Build/test overlap or missing tasks")
    for family in families:
        members = set(family["task_ids"])
        if members & build and members & test:
            raise ValueError("Family crosses build/test")
        if members & set(split["dev_task_ids"]) and members & set(split["dev_corpus_task_ids"]):
            raise ValueError("Dev family leaks into dev corpus")
    dev, excluded, corpus = (set(split[key]) for key in lists[2:])
    if not dev <= excluded <= build or excluded & corpus or excluded | corpus != build:
        raise ValueError("Invalid dev exclusion/corpus partition")
    categories = Counter(t["category_id"] for t in tasks if t["task_id"] in dev)
    if dict(categories) != {key: 1 for key in split["build_quotas"]}:
        raise ValueError("Dev must contain exactly one selected task per category")
    counts = Counter(t["category_id"] for t in tasks if t["task_id"] in build)
    if dict(counts) != split["build_quotas"]:
        raise ValueError("Build category quotas do not match")


def create_artifacts(source_dir: Path, inventory_path: Path, seed: int = SEED) -> dict[str, dict[str, Any]]:
    tasks = extract_tasks(source_dir)
    resource_metadata, public_files = load_public_inventory(inventory_path, tasks)
    families = build_families(tasks)
    prepare = source_dir.resolve() / "script/prepare.sh"
    verify_pinned_file(source_dir.resolve(), prepare)
    source = {"repo": "https://github.com/InternLM/WildClawBench", "revision": AUDITED_CODE_REVISION,
              "parser_path": "src/utils/task_parser.py", "parser_sha256": file_sha256(source_dir / "src/utils/task_parser.py"),
              "prepare_script_sha256": file_sha256(prepare), "task_resources": resource_metadata}
    manifest = {"schema_version": SCHEMA_VERSION, "source": source,
                "solver_export_fields": ["task_id", "prompt"],
                "visibility": "controller_only; export only the current task_id/prompt to a solver, never this complete manifest",
                "tasks": tasks}
    family_doc = {"schema_version": SCHEMA_VERSION, "source": source, "families": families,
                  "audit_scope": "All 60 public prompts, source preparation script and all remote public exec file hashes. No trajectories, outcomes, gt content or archive members were inspected.",
                  "shared_resource_links": resource_overlap_audit(public_files, tasks),
                  "external_shared_resources": [
                      {"task_ids": [t["task_id"] for t in tasks if t["family_id"] == "05_football_events"], "resource": "https://www.youtube.com/watch?v=93LPZJkCW2w", "decision": "group", "evidence": "script/prepare.sh copies first_half.mp4"},
                      {"task_ids": [t["task_id"] for t in tasks if t["family_id"] == "05_product_launch_video"], "resource": "https://www.youtube.com/watch?v=H3KnMyojEQU", "decision": "group", "evidence": "script/prepare.sh copies recording.mp4"},
                      {"task_ids": [t["task_id"] for t in tasks if t["family_id"] == "02_sam3_predictions" or "repo_to_slides" in t["task_id"]], "resource": "SAM3 project", "decision": "shared_resource_only_between_code_and_slides", "evidence": "Code execution/debugging and presentation synthesis have different required outputs; source subject alone does not join families."},
                      {"task_ids": [t["task_id"] for t in tasks if "benchmark_vlmeval" in t["task_id"] or "repo_to_homepage" in t["task_id"]], "resource": "VLMEvalKit project", "decision": "shared_resource_only", "evidence": "Running OCRBench and building a promotional webpage require different outputs."},
                  ],
                  "limitations": ["Remote hashes establish declared byte identity, not a completed local download verification.", "Live webpages may change; identical content inside archives is not ruled out by archive-file hashes.", "Family choices are conservative content/template decisions, not a claim that all source information is semantically independent."]}
    counts = dict(Counter(t["category_id"] for t in tasks))
    quotas = largest_remainder_quotas(counts)
    build, test, algorithm = grouped_split(tasks, families, quotas, seed)
    split = {"schema_version": SCHEMA_VERSION, "seed": seed, "source_revision": AUDITED_CODE_REVISION,
             "task_dataset_revision": AUDITED_TASK_REVISION,
             "manifest_sha256": sha256(canonical_bytes(manifest)), "families_sha256": sha256(canonical_bytes(family_doc)),
             "build_quotas": quotas, "build_task_ids": build, "test_task_ids": test,
             **choose_dev(tasks, build, seed), "selection": algorithm,
             "category_counts": {cat: {"total": counts[cat], "build": quotas[cat], "test": counts[cat] - quotas[cat]} for cat in sorted(counts)},
             "formal_conditions": ["A0"], "attempts_per_task_condition": 1, "formal_solve_count": len(test),
             "policy": {"trajectory_assignment": "Every model/attempt of a task inherits its task split.",
                        "dev_selection": "Lowest SHA256(wildclaw-dev-v1:seed:task_id) within each build category; exclude its entire family from the temporary corpus.",
                        "test_access": "Only current task public input is visible to the solver; all test trajectories/outcomes are sealed.",
                        "final_corpus": "All build tasks, after configuration freeze; no test-time shared writes."}}
    validate_split(tasks, families, split)
    return {"task_manifest.json": manifest, "task_families.json": family_doc, "split.json": split}


def write_artifacts(artifacts: dict[str, dict[str, Any]], output_dir: Path) -> None:
    """Refuse to replace a frozen file with different content."""
    payloads = {name: canonical_bytes(value) for name, value in artifacts.items()}
    for name, data in payloads.items():
        path = output_dir / name
        if path.exists() and path.read_bytes() != data:
            raise ValueError(f"Frozen artifact differs: {path}; use a new audit/output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        path = output_dir / name
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path("experiment/vendor/WildClawBench"))
    parser.add_argument("--inventory", type=Path, default=Path("experiment/manifests/source_WildClawBench.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("experiment/manifests"))
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    artifacts = create_artifacts(args.source_dir, args.inventory, args.seed)
    write_artifacts(artifacts, args.output_dir)
    split = artifacts["split.json"]
    print(json.dumps({"build": len(split["build_task_ids"]), "test": len(split["test_task_ids"]),
                      "dev": len(split["dev_task_ids"]), "dev_corpus": len(split["dev_corpus_task_ids"]),
                      "families": len(artifacts["task_families.json"]["families"]),
                      "split_sha256": sha256(canonical_bytes(split)), "output_dir": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
