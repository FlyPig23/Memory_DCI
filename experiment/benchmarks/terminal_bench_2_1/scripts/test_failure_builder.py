"""Zero-model safety/coverage tests for the offline V6 memory builder."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os

from experiment.benchmarks.terminal_bench_2_1.scripts import build_failure_memory as builder


class FailureBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
        self.relative = "trajectories/train-a/source/trial-1/train-a_0.txt"
        path = self.pool / self.relative
        path.parent.mkdir(parents=True)
        path.write_text("Official reward: 0\ncommand: compile\nerror: header missing\nprocess exited 1\nNo recovery observed.\n")
        self.row = {"task_id": "train-a", "trial_id": "trial-1", "model": "historical-model",
                    "score": 0, "is_scored": True, "status": "available",
                    "pool_file": self.relative, "pool_sha256": builder.sha(path)}
        self.records, _ = builder.candidates({"trajectories": [self.row]}, ["train-a"], self.pool)
        entry = {"slug": "verify-headers", "title": "Check missing build headers",
                 "keywords": ["compile", "header"], "applicability": "A compiler reports missing headers.",
                 "procedure": ["Inspect the exact compiler error and check whether the required header exists."],
                 "pitfalls": ["Do not assume the build succeeded because the command was issued."],
                 "verification": "Rerun the failing command and inspect its return code.",
                 "status": "source_observed", "sources": [{"path": "/pool/" + self.relative, "line_start": 2, "line_end": 4}],
                 "evidence_summary": "The compile command returned a missing-header error."}
        self.output = {"decision": "write", "reason": "Observed build failure supports a verification check.",
                       "reviewed_sources": ["/pool/" + self.relative],
                       "lessons": [{"entry": entry, "observed_failure": "Compilation exited with status 1.",
                                    "cause_hypothesis": "The development headers may be absent.", "cause_confidence": "hypothesis",
                                    "proposed_fix": "Check headers before considering package installation.",
                                    "fix_evidence": "unverified", "failure_kind": "task_failure"}]}

    def test_selection_retains_raw_failures_and_excludes_success_and_unscored(self):
        rows = []
        for n, changes in enumerate([{}, {"score": 1}, {"is_scored": False}, {"score": None},
                                     {"status": "official_raw_unparsed"}, {"score": False}]):
            row = {**self.row, **changes, "trial_id": str(n)}
            row["pool_file"] = self.relative.replace("trial-1", "trial-" + str(n))
            path = self.pool / row["pool_file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text((self.pool / self.relative).read_text())
            row["pool_sha256"] = builder.sha(path)
            rows.append(row)
        records, excluded = builder.candidates({"trajectories": rows}, ["train-a"], self.pool)
        self.assertEqual(len(records), 2)
        self.assertEqual({r["status"] for r in records}, {"available", "official_raw_unparsed"})
        self.assertEqual(excluded["not_scored_failure"], 4)

    def test_selection_rejects_held_out_and_hash_changes(self):
        for changes in ({"task_id": "test-a"}, {"pool_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                builder.candidates({"trajectories": [{**self.row, **changes}]}, ["train-a"], self.pool)

    def test_selection_rejects_path_escape_and_symlink(self):
        with self.assertRaises(ValueError):
            builder.checked_file(self.pool, "../secret")
        source = self.pool / self.relative
        text = source.read_text()
        outside = self.root / "outside"
        outside.write_text(text)
        source.unlink()
        source.symlink_to(outside)
        with self.assertRaises(ValueError):
            builder.candidates({"trajectories": [self.row]}, ["train-a"], self.pool)

    def test_packet_budget_all_sources_and_original_line_numbers(self):
        records = []
        for n in range(25):
            relative = self.relative.replace("trial-1", f"case-{n}")
            path = self.pool / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(["normal line"] * 100 + ["error: expected library missing", "x" * 1000, "last return 1"]))
            records.append({**self.records[0], "path": "/pool/" + relative, "pool_file": relative, "line_count": 103})
        packet, coverage = builder.evidence_packet(records, self.pool, 10000)
        self.assertLessEqual(len(packet), 10000)
        self.assertEqual(len(coverage["excerpt_coverage"]), 25)
        self.assertFalse(coverage["complete_reading_claimed"])
        self.assertIn("L101: error:", packet)
        for r in records:
            self.assertIn(r["path"], packet)

    def test_output_namespaced_and_hypothesis_uncertain(self):
        entries = builder.validate_output(self.output, self.records, "train-a")
        self.assertEqual(entries[0]["status"], "uncertain")
        self.assertEqual(entries[0]["slug"], "train-a-1-verify-headers")
        self.assertIn("Proposed fix (unverified)", entries[0]["evidence_summary"])
        self.assertIn("reward 0", entries[0]["evidence_summary"])

    def test_dotted_task_namespace_is_legal_and_keeps_literal_source_paths(self):
        task = "install-windows-3.11"
        value = copy.deepcopy(self.output)
        source = self.records[0]["path"].replace("train-a", task)
        records = [{**self.records[0], "task_id": task, "path": source}]
        value["reviewed_sources"] = [source]
        value["lessons"][0]["entry"]["sources"][0]["path"] = source
        entry = builder.validate_output(value, records, task)[0]
        self.assertRegex(entry["slug"], r"^install-windows-3-11-[a-f0-9]{12}-1-verify-headers$")
        self.assertEqual(entry["sources"][0]["path"], source)
        self.assertEqual(value["reviewed_sources"], [source])
        self.assertNotEqual(builder.task_slug_namespace(task), builder.task_slug_namespace("install-windows-3-11"))
        self.assertNotEqual(builder.task_slug_namespace(task), builder.task_slug_namespace("install-windows-3_11"))
        # Only the managed slug is normalized; path typos remain invalid.
        for field in ("reviewed", "cited"):
            invalid = copy.deepcopy(value)
            if field == "reviewed":
                invalid["reviewed_sources"] = [source.replace("3.11", "3-11")]
            else:
                invalid["lessons"][0]["entry"]["sources"][0]["path"] = source.replace("3.11", "3-11")
            with self.subTest(field=field), self.assertRaises(ValueError):
                builder.validate_output(invalid, records, task)

    def test_namespace_preserves_normal_outputs_and_long_ids_keep_distinct_indexes(self):
        value = copy.deepcopy(self.output)
        value["lessons"][0]["entry"]["slug"] = "x" * 80
        for task in ("train-a", "a" * 63):
            entry = builder.validate_output(value, self.records, task)[0]
            old = f"{task}-1-{'x' * max(1, 77 - len(task))}"[:80].rstrip("-")
            self.assertEqual(entry["slug"], old)
        value["lessons"].append(copy.deepcopy(value["lessons"][0]))
        entries = builder.validate_output(value, self.records, "a" * 150)
        self.assertNotEqual(entries[0]["slug"], entries[1]["slug"])
        for entry in entries:
            self.assertLessEqual(len(entry["slug"]), 80)
            self.assertRegex(entry["slug"], r"^[a-z0-9][a-z0-9-]{0,79}$")
        self.assertNotEqual(builder.task_slug_namespace("a" * 150), builder.task_slug_namespace("a" * 149 + "b"))

    def test_output_rejects_test_citations_local_verification_and_fake_ranges(self):
        for mutation in ("test_path", "line_range", "local_status", "reviewed_source"):
            value = copy.deepcopy(self.output)
            entry = value["lessons"][0]["entry"]
            if mutation == "test_path":
                entry["sources"][0]["path"] = "/pool/trajectories/test-a/file.txt"
            elif mutation == "line_range":
                entry["sources"][0]["line_end"] = 9999
            elif mutation == "local_status":
                entry["status"] = "locally_verified"
            else:
                value["reviewed_sources"] = ["/tests/test_outputs.py"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                builder.validate_output(value, self.records, "train-a")

    def test_no_update_permitted_only_without_lessons(self):
        empty = {"decision": "no_update", "reason": "No reliable reusable lesson.", "lessons": [], "reviewed_sources": []}
        self.assertEqual(builder.validate_output(empty, self.records, "train-a"), [])
        empty["decision"] = "write"
        with self.assertRaises(ValueError):
            builder.validate_output(empty, self.records, "train-a")

    def test_input_copy_has_no_success_test_or_host_manifest(self):
        cards = self.pool / "tasks"
        cards.mkdir()
        (cards / "train-a.md").write_text("Training instruction only.")
        (cards / "test-a.md").write_text("HELD OUT INSTRUCTION")
        (self.pool / "manifest.json").write_text(json.dumps({"test_task_ids": ["test-a"]}))
        plan = {"candidate_records": self.records, "training_task_ids": ["train-a"], "candidate_count": 1,
                "source_selection": "failed_training_trajectories", "task_card_sha256": {"train-a": builder.sha(cards / "train-a.md")}}
        target = builder.prepare_inputs(self.root / "work", plan, self.pool)
        self.assertFalse((target / "tasks/test-a.md").exists())
        self.assertNotIn("test-a", (target / "manifest.json").read_text())
        self.assertTrue((target / self.relative).is_file())
        (target / "unexpected.txt").write_text("bad")
        with self.assertRaises(ValueError):
            builder.prepare_inputs(self.root / "work", plan, self.pool)

    def test_docker_mounts_contain_only_isolated_inputs_and_support(self):
        argv = builder.docker_argv(self.root / "base", {"python_root": str(self.root / "python"), "image_id": "sha256:test"},
                                  self.root / "failure-only-pool", self.root / "attempt", self.root / "home", "test-builder")
        mounts = [argv[n + 1] for n, value in enumerate(argv) if value == "--mount"]
        self.assertEqual(len(mounts), 6)
        self.assertTrue(any("dst=/pool,readonly" in m for m in mounts))
        self.assertFalse(any("/prepared/tasks" in m or "dst=/tests" in m for m in mounts))
        self.assertIn("--read-only", argv)
        self.assertIn("--output-schema", argv)

    def test_completed_result_mutation_prevents_reuse(self):
        work = self.root / "work"
        path = work / "jobs/train-a/attempt-001"
        path.mkdir(parents=True)
        entries = builder.validate_output(self.output, self.records, "train-a")
        builder.write_json(path / "result.json", self.output)
        builder.write_json(path / "entries.json", entries)
        (path / "evidence_packet.txt").write_text("packet")
        (path / "codex.session.jsonl").write_text("session")
        done = {"attempt": "attempt-001", "status": "complete", "model_audit": {"model_effort_verified": True},
                "result_sha256": builder.sha(path / "result.json"), "entries_sha256": builder.sha(path / "entries.json"),
                "packet_sha256": builder.sha(path / "evidence_packet.txt"), "session_sha256": builder.sha(path / "codex.session.jsonl")}
        builder.write_json(path.parent / "completion.json", done)
        self.assertEqual(builder.completion(work, {"task_id": "train-a"}, self.records)[1], entries)
        (path / "entries.json").write_text("[]")
        with self.assertRaises(ValueError):
            builder.completion(work, {"task_id": "train-a"}, self.records)

    def recovery_fixture(self):
        work = self.root / "recovery-work"
        attempt = work / "jobs/train-a/attempt-001"
        attempt.mkdir(parents=True)
        source = work / "inputs/pool" / self.relative
        source.parent.mkdir(parents=True)
        source.write_bytes((self.pool / self.relative).read_bytes())
        job = {"task_id": "train-a", "candidate_paths": [r["path"] for r in self.records]}
        plan = {"jobs": [job], "candidate_records": self.records, "timeout_seconds_per_job": 300,
                "source_code_sha256": {"builder.py": "old"}, "candidate_count": 1}
        builder.write_json(work / "plan.json", plan)
        value = copy.deepcopy(self.output)
        value["lessons"].append(copy.deepcopy(value["lessons"][0]))
        builder.write_json(attempt / "failure_memory.json", value)
        audit = {"model_effort_verified": True, "models": [builder.MODEL], "efforts": [builder.EFFORT]}
        builder.write_json(attempt / "model_audit.json", audit)
        original = {"task_id": "train-a", "attempt": "attempt-001", "status": "budget_exhausted",
                    "timed_out": True, "elapsed_seconds": 301.5, "timeout_seconds": 300, "started_at_unix": 100.0,
                    "model_audit": audit, "candidate_count": 1, "source_paths": job["candidate_paths"]}
        builder.write_json(attempt / "attempt.json", original)
        for filename in ("prompt.txt", "evidence_packet.txt", "codex.session.jsonl", "codex.events.jsonl"):
            (attempt / filename).write_text(filename)
        os.utime(attempt / "failure_memory.json", (350, 350))
        return work, attempt, plan, original

    def test_zero_model_timeout_recovery_preserves_original_bytes_and_curation(self):
        work, attempt, plan, original = self.recovery_fixture()
        original_bytes = (attempt / "attempt.json").read_bytes()
        raw_bytes = (attempt / "failure_memory.json").read_bytes()
        with patch.object(builder.subprocess, "run", side_effect=AssertionError("No subprocess allowed")), \
                patch.object(builder.subprocess, "Popen", side_effect=AssertionError("No model allowed")):
            done = builder.recover_checkpoint(work, "train-a", excluded_lessons={1: "Duplicate unsupported claim excluded by review."})
        self.assertTrue(done["timed_out"])
        self.assertEqual(done["elapsed_seconds"], 301.5)
        self.assertEqual(done["execution_status"], "budget_exhausted")
        self.assertEqual(done["entry_count"], 1)
        self.assertEqual(done["result_file"], "failure_memory.json")
        self.assertFalse((attempt / "result.json").exists())
        self.assertEqual((attempt / "attempt.json").read_bytes(), original_bytes)
        self.assertEqual((attempt / "failure_memory.json").read_bytes(), raw_bytes)
        self.assertEqual(len(builder.completion(work, plan["jobs"][0], self.records)[1]), 1)
        self.assertEqual(builder.recover_checkpoint(work, "train-a", excluded_lessons={1: "Duplicate unsupported claim excluded by review."}), done)
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a", excluded_lessons={0: "Different exclusion."})

    def test_recovery_receipt_tampering_is_rejected(self):
        work, attempt, plan, _ = self.recovery_fixture()
        builder.recover_checkpoint(work, "train-a", excluded_lessons={1: "Original reason."})
        receipt = builder.load(attempt / "recovery.json")
        receipt["excluded_lessons"][0]["reason"] = "Silently changed reason"
        builder.write_json(attempt / "recovery.json", receipt)
        with self.assertRaises(ValueError):
            builder.completion(work, plan["jobs"][0], self.records)

    def test_collector_prefers_native_then_valid_checkpoint_without_overwrites(self):
        work, attempt, _, original = self.recovery_fixture()
        (attempt / "result.json").write_text('{"broken": true}')
        os.utime(attempt / "result.json", (360, 360))
        original_native = (attempt / "result.json").read_bytes()
        raw, entries, origin = builder.collect_output(attempt, self.records, "train-a", execution=original)
        self.assertEqual(origin["result_file"], "failure_memory.json")
        self.assertEqual(origin["prior_candidates"][0]["classification"], "invalid_output")
        self.assertIn("builder_output missing", origin["prior_candidates"][0]["validation_error"])
        self.assertEqual((attempt / "result.json").read_bytes(), original_native)
        builder.write_json(attempt / "result.json", self.output)
        os.utime(attempt / "result.json", (360, 360))
        _, entries, origin = builder.collect_output(attempt, self.records, "train-a", execution=original)
        self.assertEqual(origin["result_file"], "result.json")
        self.assertEqual(len(entries), 1)

    def test_recovery_rejects_late_checkpoint_and_bad_citation(self):
        work, attempt, _, original = self.recovery_fixture()
        os.utime(attempt / "failure_memory.json", (401, 401))
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")
        os.utime(attempt / "failure_memory.json", (99, 99))
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")
        value = builder.load(attempt / "failure_memory.json")
        value["lessons"][0]["entry"]["sources"][0]["path"] = "/pool/trajectories/test-task/hidden.txt"
        builder.write_json(attempt / "failure_memory.json", value)
        os.utime(attempt / "failure_memory.json", (350, 350))
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")
        self.assertFalse((attempt / "entries.json").exists())
        self.assertFalse((attempt.parent / "completion.json").exists())

    def test_recovery_rejects_inflated_budget_and_wrong_model(self):
        work, attempt, _, original = self.recovery_fixture()
        changed = {**original, "timeout_seconds": 9999}
        builder.write_json(attempt / "attempt.json", changed)
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")
        changed = copy.deepcopy(original)
        changed["model_audit"]["models"] = ["another-model"]
        builder.write_json(attempt / "attempt.json", changed)
        builder.write_json(attempt / "model_audit.json", changed["model_audit"])
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")

    def test_code_only_migration_archives_plan_and_job_hashes(self):
        work, attempt, old, _ = self.recovery_fixture()
        old_bytes = (work / "plan.json").read_bytes()
        old_sha = builder.sha(work / "plan.json")
        amended = {**old, "source_code_sha256": {"builder.py": "new"}}
        with self.assertRaises(ValueError):
            builder.freeze_plan(work, amended)
        builder.freeze_plan(work, amended, accept_code_update=True)
        self.assertEqual((work / "plan_history" / (old_sha + ".json")).read_bytes(), old_bytes)
        receipt = builder.load(work / "plan_history/amendment-001.json")
        self.assertEqual(receipt["new_plan_sha256"], builder.sha(work / "plan.json"))
        self.assertEqual(receipt["preserved_job_artifact_sha256"][str((attempt / "attempt.json").relative_to(work))], builder.sha(attempt / "attempt.json"))
        for changed in ({**amended, "timeout_seconds_per_job": 600}, {**amended, "candidate_count": 999}):
            with self.assertRaises(ValueError):
                builder.freeze_plan(work, changed, accept_code_update=True)
        self.assertEqual(builder.load(work / "plan.json"), amended)

    def test_status_flags_unresolved_failed_attempt(self):
        work, _, _, _ = self.recovery_fixture()
        self.assertEqual(builder.status(work)["status"], "needs_review")

    def adaptive_plan(self, old):
        plan = copy.deepcopy(old)
        plan["timeout_seconds_per_job"] = None
        plan["budget_policy"] = builder.budget_policy(None)
        for job in plan["jobs"]:
            records = [r for r in plan["candidate_records"] if r["task_id"] == job["task_id"]]
            job.update(builder.job_budget(records))
        return plan

    def test_adaptive_budget_responds_to_count_and_bytes_and_has_ceiling(self):
        self.assertEqual(builder.job_budget([{"bytes": 1}])["timeout_seconds"], 600)
        self.assertEqual(builder.job_budget([{"bytes": 1}] * 20)["timeout_seconds"], 960)
        self.assertEqual(builder.job_budget([{"bytes": 20 * 1024 * 1024}])["timeout_seconds"], 1530)
        self.assertEqual(builder.job_budget([{"bytes": 1024 * 1024}] * 100)["timeout_seconds"], 3600)
        self.assertEqual(builder.job_budget([{"bytes": 1024 * 1024}] * 100, 180)["timeout_seconds"], 180)
        with self.assertRaises(ValueError):
            builder.job_budget([{"bytes": 1}], 3601)

    def test_budget_amendment_requires_separate_flag_and_preserves_original_budget(self):
        work, attempt, old, original = self.recovery_fixture()
        original_bytes = (attempt / "attempt.json").read_bytes()
        adaptive = self.adaptive_plan(old)
        with self.assertRaises(ValueError):
            builder.freeze_plan(work, adaptive, accept_code_update=True)
        builder.freeze_plan(work, adaptive, accept_budget_update=True)
        receipt = builder.load(work / "plan_history/amendment-001.json")
        self.assertTrue(receipt["budget_update_accepted"])
        self.assertEqual(receipt["job_budget_changes"]["train-a"], {"old_seconds": 300, "new_seconds": 600})
        self.assertEqual((attempt / "attempt.json").read_bytes(), original_bytes)
        resolved, digest, basis = builder.resolve_attempt_plan(work, attempt, original)
        self.assertEqual(resolved["timeout_seconds_per_job"], 300)
        self.assertEqual(basis, "amendment_binds_preserved_legacy_attempt")
        self.assertEqual(digest, receipt["old_plan_sha256"])
        item = builder.status(work)["jobs"][0]
        self.assertEqual(item["planned_timeout_seconds"], 600)
        self.assertEqual(item["attempt_timeout_seconds"], 300)
        self.assertEqual(item["candidate_bytes"], self.records[0]["bytes"])

    def test_increased_budget_does_not_retroactively_accept_late_checkpoint(self):
        work, attempt, old, _ = self.recovery_fixture()
        os.utime(attempt / "failure_memory.json", (450, 450))  # Old deadline 400; new ceiling would be 700.
        builder.freeze_plan(work, self.adaptive_plan(old), accept_budget_update=True)
        with self.assertRaises(ValueError):
            builder.recover_checkpoint(work, "train-a")
        self.assertFalse((attempt.parent / "completion.json").exists())

    def test_budget_retry_needs_preserved_failure_and_accepted_increase(self):
        work, attempt, old, original = self.recovery_fixture()
        adaptive = self.adaptive_plan(old)
        self.assertFalse(builder.budget_retry_authorized(work, attempt, original, adaptive, adaptive["jobs"][0]))
        builder.freeze_plan(work, adaptive, accept_budget_update=True)
        self.assertTrue(builder.budget_retry_authorized(work, attempt, original, adaptive, adaptive["jobs"][0]))
        self.assertFalse(builder.budget_retry_authorized(work, attempt, original, old, old["jobs"][0]))
        changed = {**original, "status": "infrastructure_failed"}
        self.assertFalse(builder.budget_retry_authorized(work, attempt, changed, adaptive, adaptive["jobs"][0]))
        # An amendment does not authorize retries of attempts added after that amendment.
        copied = attempt.parent / "attempt-002"
        copied.mkdir()
        builder.write_json(copied / "attempt.json", {**original, "attempt": "attempt-002", "plan_sha256": builder.sha(work / "plan.json"), "timeout_seconds": 600})
        self.assertFalse(builder.budget_retry_authorized(work, copied, builder.load(copied / "attempt.json"), adaptive, adaptive["jobs"][0]))

    def test_completed_output_is_unchanged_and_reusable_after_budget_amendment(self):
        work, attempt, old, _ = self.recovery_fixture()
        done = builder.recover_checkpoint(work, "train-a", excluded_lessons={1: "Duplicate observation."})
        names = ("attempt.json", "failure_memory.json", "entries.json", "recovery.json")
        original_hashes = {name: builder.sha(attempt / name) for name in names}
        completion_hash = builder.sha(attempt.parent / "completion.json")
        adaptive = self.adaptive_plan(old)
        builder.freeze_plan(work, adaptive, accept_budget_update=True)
        self.assertEqual(builder.completion(work, adaptive["jobs"][0], self.records)[0], done)
        self.assertEqual({name: builder.sha(attempt / name) for name in names}, original_hashes)
        self.assertEqual(builder.sha(attempt.parent / "completion.json"), completion_hash)

    def test_budget_amendment_rejects_data_changes_manual_formula_and_finished_seed(self):
        work, attempt, old, _ = self.recovery_fixture()
        adaptive = self.adaptive_plan(old)
        bad = copy.deepcopy(adaptive)
        bad["candidate_records"][0]["sha256"] = "x" * 64
        with self.assertRaises(ValueError):
            builder.freeze_plan(work, bad, accept_budget_update=True)
        bad = copy.deepcopy(adaptive)
        bad["jobs"][0]["timeout_seconds"] = 999
        with self.assertRaises(ValueError):
            builder.freeze_plan(work, bad, accept_budget_update=True)
        builder.write_json(work / "seed_receipt.json", {"frozen": True})
        with self.assertRaises(ValueError):
            builder.freeze_plan(work, adaptive, accept_budget_update=True)
        self.assertEqual(builder.load(work / "plan.json"), old)

    def test_pause_stops_before_new_model_job(self):
        work, _, old, _ = self.recovery_fixture()
        (work / "pause_requested").touch()
        with patch.object(builder, "build_plan", return_value=old), \
                patch.object(builder, "prepare_inputs", return_value=work / "inputs/pool"), \
                patch.object(builder, "support", return_value={}), \
                patch.object(builder, "run_job", side_effect=AssertionError("Paused builders must not start models")):
            result = builder.build(self.root, work, self.root / "new-seed", auth_file=self.root / "unused-auth",
                                   image="unused", timeout_seconds=None, packet_chars=90000, max_jobs=None)
        self.assertEqual(result["status"], "paused")

    def test_pause_stops_before_seed_export(self):
        work, _, old, _ = self.recovery_fixture()
        builder.recover_checkpoint(work, "train-a", excluded_lessons={1: "Duplicate observation."})
        (work / "pause_requested").touch()
        with patch.object(builder, "build_plan", return_value=old), \
                patch.object(builder, "prepare_inputs", return_value=work / "inputs/pool"), \
                patch.object(builder, "support", return_value={}), \
                patch("experiment.shared.memory.seed.create_seed", side_effect=AssertionError("Paused builder must not export seed")):
            result = builder.build(self.root, work, self.root / "new-seed", auth_file=self.root / "unused-auth",
                                   image="unused", timeout_seconds=None, packet_chars=90000, max_jobs=None)
        self.assertEqual(result["status"], "paused")


if __name__ == "__main__":
    unittest.main()
