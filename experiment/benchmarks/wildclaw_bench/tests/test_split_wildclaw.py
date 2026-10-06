"""Leakage and reproducibility tests for the frozen pilot split."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/split_wildclaw.py"
SPEC = importlib.util.spec_from_file_location("split_wildclaw", SCRIPT)
splitter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(splitter)
ROOT = Path(__file__).resolve().parents[4]


def test_hamilton_rounding_keeps_exact_36_and_fixed_ties():
    counts = {key: value[2] for key, value in splitter.CATEGORIES.items()}
    assert splitter.largest_remainder_quotas(counts) == {
        "01": 6, "02": 7, "03": 4, "04": 7, "05": 6, "06": 6,
    }


def test_public_export_cannot_carry_evaluator_fields(tmp_path):
    source = tmp_path / "repo"
    path = source / "tasks/01_Productivity_Flow/01_Productivity_Flow_task_1_example.md"
    path.parent.mkdir(parents=True)
    path.write_text("source may contain an evaluator section", encoding="utf-8")
    private_marker = "DO_NOT_EXPORT_EVALUATOR_48921"
    parsed = {"task_id": "01_Productivity_Flow_task_1_example", "prompt": "Read the public input.",
              "workspace_path": str(source / "workspace/01_Productivity_Flow/task_1_example"),
              "automated_checks": private_marker, "env": private_marker,
              "warmup": private_marker, "skills": private_marker, "unexpected": private_marker}
    public = splitter.public_task(parsed, source, path)
    assert private_marker not in json.dumps(public)
    assert public["prompt"] == parsed["prompt"]
    assert public["prompt_sha256"] == splitter.sha256(parsed["prompt"].encode())
    assert set(public) == {"task_id", "category_id", "category_name", "category_directory",
                           "source_path", "source_sha256", "prompt", "prompt_sha256",
                           "public_workspace_relpath", "public_exec_relpath", "public_resources"}


def toy_universe():
    tasks = [{"task_id": task_id, "category_id": category, "family_id": family}
             for task_id, category, family in (("a1", "A", "shared"), ("b1", "B", "shared"),
                                               ("a2", "A", "a2"), ("b2", "B", "b2"))]
    families = [{"family_id": "shared", "task_ids": ["a1", "b1"]},
                {"family_id": "a2", "task_ids": ["a2"]},
                {"family_id": "b2", "task_ids": ["b2"]}]
    return tasks, families


def test_cross_category_family_is_never_split_and_input_order_does_not_matter():
    tasks, families = toy_universe()
    for seed in range(20):
        build, test, audit = splitter.grouped_split(tasks, families, {"A": 1, "B": 1}, seed)
        assert audit["feasible_assignment_count"] == 2
        assert ("a1" in build) == ("b1" in build)
        assert set(build).isdisjoint(test)
        assert len(build) == len(test) == 2
        assert (build, test, audit) == splitter.grouped_split(list(reversed(tasks)), list(reversed(families)), {"B": 1, "A": 1}, seed)


def test_infeasible_quota_fails_instead_of_breaking_family():
    tasks, families = toy_universe()
    families = [{"family_id": "whole_A", "task_ids": ["a1", "a2"]},
                {"family_id": "whole_B", "task_ids": ["b1", "b2"]}]
    with pytest.raises(ValueError, match="No family-disjoint"):
        splitter.grouped_split(tasks, families, {"A": 1, "B": 1}, 123)


def test_incomplete_or_duplicated_family_membership_is_rejected():
    tasks, families = toy_universe()
    with pytest.raises(ValueError, match="partition"):
        splitter.grouped_split(tasks, families[:-1], {"A": 1, "B": 1}, 123)
    with pytest.raises(ValueError, match="partition"):
        splitter.grouped_split(tasks, families + [families[0]], {"A": 1, "B": 1}, 123)


def test_dev_exclusion_removes_unselected_siblings_and_cross_category_members():
    tasks, _ = toy_universe()
    # This deliberately makes all four build tasks one family. Whichever one
    # per category is selected, all four must be absent from its retrieval book.
    tasks = [{**task, "family_id": "same_family"} for task in tasks]
    result = splitter.choose_dev(tasks, [task["task_id"] for task in tasks], 17)
    assert len(result["dev_task_ids"]) == 2
    assert len(result["dev_excluded_family_task_ids"]) == 4
    assert result["dev_corpus_task_ids"] == []


def test_private_asset_inventory_fields_cannot_enter_public_audit(tmp_path):
    task = {"task_id": "a", "public_exec_relpath": "workspace/01_Test/task_1/exec"}
    inventory = {"sha": splitter.AUDITED_TASK_REVISION, "siblings": [
        {"rfilename": "workspace/01_Test/task_1/gt/secret.json", "size": 1, "blobId": "PRIVATE_GT_MARKER"},
        {"rfilename": "workspace/01_Test/task_1/private_eval/secret.py", "size": 1, "blobId": "PRIVATE_EVAL_MARKER"},
        {"rfilename": "workspace/01_Test/task_1/exec/input.png", "size": 7,
         "blobId": "pointer_git_hash", "lfs": {"sha256": "a" * 64}},
    ]}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(inventory), encoding="utf-8")
    metadata, files = splitter.load_public_inventory(path, [task])
    assert len(files) == 1
    assert files[0]["hash_algorithm"] == "sha256"
    assert files[0]["hash"] == "a" * 64
    assert "PRIVATE_" not in json.dumps([metadata, files, task])


def test_existing_frozen_artifacts_cannot_be_changed(tmp_path):
    splitter.write_artifacts({"split.json": {"build": ["a"]}}, tmp_path)
    original = (tmp_path / "split.json").read_bytes()
    splitter.write_artifacts({"split.json": {"build": ["a"]}}, tmp_path)
    with pytest.raises(ValueError, match="Frozen artifact differs"):
        splitter.write_artifacts({"split.json": {"build": ["b"]}}, tmp_path)
    assert (tmp_path / "split.json").read_bytes() == original


@pytest.fixture(scope="module")
def real_artifacts():
    source = ROOT / "experiment/benchmarks/wildclaw_bench/vendor/WildClawBench"
    inventory = ROOT / "experiment/benchmarks/wildclaw_bench/manifests/source_WildClawBench.json"
    if not source.exists() or not inventory.exists():
        pytest.skip("Pinned benchmark source/inventory unavailable")
    return splitter.create_artifacts(source, inventory)


def test_actual_60_tasks_are_group_disjoint_and_have_exact_24_test(real_artifacts):
    manifest, family_doc, split = (real_artifacts[name] for name in ("task_manifest.json", "task_families.json", "split.json"))
    assert len(manifest["tasks"]) == 60
    assert len(split["build_task_ids"]) == 36
    assert len(split["test_task_ids"]) == 24
    assert split["formal_solve_count"] == 24
    assert split["formal_conditions"] == ["A0"]
    assert len(split["dev_task_ids"]) == 6
    assert len(split["dev_corpus_task_ids"]) <= 30
    splitter.validate_split(manifest["tasks"], family_doc["families"], split)
    assert split["manifest_sha256"] == splitter.sha256(splitter.canonical_bytes(manifest))
    assert split["families_sha256"] == splitter.sha256(splitter.canonical_bytes(family_doc))


def test_actual_partition_validator_detects_dev_family_contamination(real_artifacts):
    manifest, family_doc, split = (real_artifacts[name] for name in ("task_manifest.json", "task_families.json", "split.json"))
    broken = deepcopy(split)
    leaked_id = broken["dev_excluded_family_task_ids"][0]
    broken["dev_corpus_task_ids"].append(leaked_id)
    with pytest.raises(ValueError, match="Dev family leaks|Invalid dev"):
        splitter.validate_split(manifest["tasks"], family_doc["families"], broken)


def test_actual_output_is_byte_reproducible(real_artifacts):
    recreated = splitter.create_artifacts(ROOT / "experiment/benchmarks/wildclaw_bench/vendor/WildClawBench", ROOT / "experiment/benchmarks/wildclaw_bench/manifests/source_WildClawBench.json")
    for name, value in real_artifacts.items():
        assert splitter.canonical_bytes(value) == splitter.canonical_bytes(recreated[name])
