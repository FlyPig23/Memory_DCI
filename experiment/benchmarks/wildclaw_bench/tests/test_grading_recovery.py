"""Synthetic grading recovery; no benchmark task, Docker or model calls."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiment.benchmarks.wildclaw_bench.scripts.evaluate_wildclaw import (
    freeze_solver_evidence, inventory_tree, load_evidence, recover_grade,
    adopt_recovery, effective_evaluation, digest,
)
from experiment.shared.task_runtime import PREFIX, RuntimeConfig, TaskRuntime, TaskSpec


class GradingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="recovery-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.scratch.name)
        self.inputs = self.root / "task-inputs"
        (self.inputs / "exec").mkdir(parents=True)
        (self.inputs / "gt").mkdir()
        (self.inputs / "gt" / "answer.txt").write_text("synthetic gold")
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.task_source = self.root / "synthetic-task.md"
        self.task_source.write_text("synthetic task source, without benchmark data")
        self.loader = self.root / "synthetic-loader.py"
        self.loader.write_text("def load_transcript(path): return []")
        self.task = TaskSpec("synthetic-task", "synthetic", "synthetic prompt", self.inputs,
                             self.skills, automated_checks="def grade(**kwargs): return {'overall_score': 0.5}")
        self.run_dir = self.root / "experiment/benchmarks/wildclaw_bench/runs/dev/synthetic-run"
        self.config = RuntimeConfig(self.root, "synthetic-image")
        self.docker_calls = []
        self.grade_scores = {"overall_score": 0.5}

    def tearDown(self):
        self.scratch.cleanup()

    def docker(self, runtime, *args, **kwargs):
        self.docker_calls.append((args, kwargs))
        if ".Config.Labels" in " ".join(args):
            stdout = PREFIX
        elif "{{.State.Running}}" in args:
            stdout = "false"
        elif args[-2:] == ("python3", "/tmp/grade_runner.py"):
            token = runtime.config.environment["OPENROUTER_API_KEY"]
            self.assertFalse(token.startswith("skill-dci-run-"))
            client_hash = hashlib.sha256(("Bearer " + token).encode()).hexdigest()
            routes = json.loads((self.root / "experiment/benchmarks/wildclaw_bench/runtime/proxy_routes.json").read_text())
            self.assertIn(client_hash, routes["inference_clients"])
            self.assertEqual((runtime.workspace / "deliverable.txt").read_text(), "original solver output")
            stdout = json.dumps(self.grade_scores)
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def factory(self, *args):
        runtime = TaskRuntime(*args)
        runtime.docker = lambda *a, **kw: self.docker(runtime, *a, **kw)
        return runtime

    def config_factory(self, project_root, image, **overrides):
        return RuntimeConfig(project_root, image, **overrides)

    def freeze(self, status="judge_error", score=None):
        runtime = self.factory(self.config, self.task, self.run_dir, "synthetic-run")
        runtime.prepare("")
        (runtime.workspace / "deliverable.txt").write_text("original solver output")
        (runtime.workspace / "external-link").symlink_to("/outside-the-project/missing")
        transcript = self.run_dir / "transcript.openclaw.jsonl"
        transcript.write_text('{"type":"message","message":{"role":"assistant","content":[]}}\n')
        runtime.freeze_agent()
        record = {"task_id": self.task.task_id, "run_id": "synthetic-run", "status": "finished",
                  "execution": {"status": "completed"},
                  "evaluation": {"status": status, "overall_score": score}}
        for name in ("result.json", "run.json"):
            (self.run_dir / name).write_text(json.dumps(record))
        (self.run_dir / "evaluation.json").write_text(json.dumps(record["evaluation"]))
        evidence = freeze_solver_evidence(runtime, transcript_path=transcript, transcript_loader=self.loader,
                                          task_source=self.task_source,
                                          source_sha256=hashlib.sha256(self.task_source.read_bytes()).hexdigest())
        return runtime, evidence

    def recover(self):
        return recover_grade(self.root, self.run_dir, reason="Synthetic proxy repair",
                             runtime_factory=self.factory, config_factory=self.config_factory)

    def test_freeze_is_independent_symlink_safe_and_before_gold(self):
        runtime, evidence = self.freeze()
        self.assertNotEqual((runtime.workspace / "deliverable.txt").stat().st_ino,
                            (evidence / "workspace/deliverable.txt").stat().st_ino)
        self.assertTrue((evidence / "workspace/external-link").is_symlink())
        self.assertFalse((evidence / "workspace/gt").exists())
        self.assertEqual((evidence / "verifier/gt/answer.txt").read_text(), "synthetic gold")
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o700)
        (runtime.workspace / "deliverable.txt").write_text("grader modified live workspace")
        load_evidence(self.run_dir)
        self.assertEqual((evidence / "workspace/deliverable.txt").read_text(), "original solver output")

    def test_controller_staged_gt_root_symlink_is_resolved_within_project(self):
        original = self.inputs / "gt"
        staged_target = self.root / "official-synthetic-gt"
        original.rename(staged_target)
        original.symlink_to(staged_target, target_is_directory=True)
        _, evidence = self.freeze()
        self.assertEqual((evidence / "verifier/gt/answer.txt").read_text(), "synthetic gold")
        self.assertFalse((evidence / "verifier/gt").is_symlink())

    def test_recovers_failed_grading_without_solver_and_preserves_originals(self):
        runtime, evidence = self.freeze()
        originals = {name: (self.run_dir / name).read_bytes() for name in ("result.json", "run.json", "evaluation.json")}
        before = inventory_tree(evidence)
        (runtime.workspace / "deliverable.txt").write_text("grader modified live workspace")
        with patch("experiment.shared.codex_backend.CodexBackend.run", side_effect=AssertionError("Solver must never run")):
            result = self.recover()
        self.assertEqual(result["evaluation"]["status"], "graded")
        self.assertEqual(result["evaluation"]["overall_score"], 0.5)
        self.assertEqual(result["solver_invocations"], 0)
        self.assertTrue(result["source_evidence_unchanged"])
        self.assertFalse(result["adopted"])
        self.assertEqual(inventory_tree(evidence), before)
        self.assertEqual((runtime.workspace / "deliverable.txt").read_text(), "grader modified live workspace")
        for name, content in originals.items():
            self.assertEqual((self.run_dir / name).read_bytes(), content)
        attempts = list((self.run_dir / "grade-recovery").glob("*/runtime/grader.stdout.log"))
        self.assertEqual(len(attempts), 1)
        self.assertFalse(any(a[0] == "cp" for a, _ in self.docker_calls))
        self.assertFalse(any("/opt/codex/codex" in a for a, _ in self.docker_calls))
        self.assertFalse((self.run_dir / ".grade-recovery.lock").exists())
        routes = json.loads((self.root / "experiment/benchmarks/wildclaw_bench/runtime/proxy_routes.json").read_text())
        self.assertEqual(routes["inference_clients"], {})

    def test_valid_zero_cannot_be_retried(self):
        self.freeze(status="graded", score=0)
        before = len(self.docker_calls)
        with self.assertRaisesRegex(ValueError, "including zero"):
            self.recover()
        self.assertEqual(len(self.docker_calls), before)
        self.assertFalse((self.run_dir / "grade-recovery").exists())

    def test_persisted_success_blocks_retry_even_if_outer_record_was_interrupted(self):
        self.freeze()
        (self.run_dir / "evaluation.json").write_text('{"status":"graded","overall_score":0}')
        with self.assertRaisesRegex(ValueError, "including zero"):
            self.recover()

    def test_first_successful_recovery_cannot_be_cherry_picked(self):
        self.freeze()
        self.grade_scores = {"overall_score": 0}
        self.assertEqual(self.recover()["evaluation"]["overall_score"], 0)
        with self.assertRaisesRegex(ValueError, "existing valid recovered grade"):
            self.recover()
        self.assertEqual(len(list((self.run_dir / "grade-recovery").glob("*/attempt.json"))), 1)

    def test_failed_attempt_can_be_recovered_with_all_attempts_preserved(self):
        self.freeze()
        self.grade_scores = {"overall_score": 0, "llm_error": "synthetic unavailable provider"}
        first = self.recover()
        self.assertEqual(first["evaluation"]["status"], "judge_error")
        self.assertIsNone(first["evaluation"]["overall_score"])
        self.grade_scores = {"overall_score": 0.5}
        second = self.recover()
        self.assertEqual(second["evaluation"]["status"], "graded")
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(len(list((self.run_dir / "grade-recovery").glob("*/runtime/score.json"))), 2)

    def test_tampered_frozen_artifact_is_rejected_without_container(self):
        _, evidence = self.freeze()
        (evidence / "workspace/deliverable.txt").write_text("tampered")
        before = len(self.docker_calls)
        with self.assertRaisesRegex(ValueError, "Frozen solver artifacts changed"):
            self.recover()
        self.assertEqual(len(self.docker_calls), before)

    def test_legacy_run_without_snapshot_is_not_silently_reconstructed(self):
        _, evidence = self.freeze()
        (evidence / "manifest.json").unlink()
        before = len(self.docker_calls)
        with self.assertRaises(FileNotFoundError):
            self.recover()
        self.assertEqual(len(self.docker_calls), before)

    def test_unknown_evaluation_failure_is_not_automatically_retried(self):
        self.freeze(status="needs_review", score=None)
        with self.assertRaisesRegex(ValueError, "explicitly identified"):
            self.recover()

    def test_social_replay_rebinds_only_envelope_and_preserves_original_audit(self):
        from experiment.benchmarks.wildclaw_bench.src.mock_state import canonical as mock_canonical
        social_id = "03_Social_Interaction_task_2_synthetic"
        self.task = replace(self.task, task_id=social_id)
        runtime = self.factory(self.config, self.task, self.run_dir, "synthetic-run")
        runtime.prepare("")
        (runtime.workspace / "deliverable.txt").write_text("original solver output")
        responses = {"slack": {"calls": [], "drafts": [{"content": "synthetic draft"}]}}
        snapshot = {"schema_version": 1, "task_id": social_id, "container": runtime.name,
                    "responses": responses, "response_sha256": {
                        "slack": hashlib.sha256(mock_canonical(responses["slack"])).hexdigest()}}
        (runtime.staging / "mock_state.json").write_text(json.dumps(snapshot))
        transcript = runtime.run_dir / "transcript.openclaw.jsonl"
        transcript.write_text("")
        runtime.freeze_agent()
        record = {"task_id": social_id, "run_id": "synthetic-run", "status": "finished",
                  "execution": {"status": "completed"},
                  "evaluation": {"status": "judge_error", "overall_score": None}}
        (self.run_dir / "result.json").write_text(json.dumps(record))
        evidence = freeze_solver_evidence(runtime, transcript_path=transcript, transcript_loader=self.loader,
                                          task_source=self.task_source,
                                          source_sha256=hashlib.sha256(self.task_source.read_bytes()).hexdigest())
        original_snapshot = (evidence / "mock_state.json").read_bytes()

        def replay(recovery_runtime, snapshot_path):
            recovered = json.loads(snapshot_path.read_text())
            self.assertEqual(recovered["container"], recovery_runtime.name)
            self.assertEqual(recovered["recovery_source_container"], runtime.name)
            self.assertEqual(recovered["responses"], responses)
            self.assertEqual(recovered["response_sha256"], snapshot["response_sha256"])
            self.assertEqual(recovery_runtime.state, "grading")

        with patch("experiment.benchmarks.wildclaw_bench.scripts.evaluate_wildclaw.restore_mock_state", side_effect=replay) as restore:
            result = self.recover()
        restore.assert_called_once()
        self.assertEqual(result["evaluation"]["status"], "graded")
        self.assertEqual((evidence / "mock_state.json").read_bytes(), original_snapshot)

    def test_zero_recovery_can_be_explicitly_adopted_without_overwriting_originals(self):
        self.freeze()
        self.grade_scores = {"overall_score": 0}
        attempt = self.recover()
        record = json.loads((self.run_dir / "result.json").read_text())
        original, metadata = effective_evaluation(self.root, self.run_dir, record)
        self.assertEqual(original["status"], "judge_error")
        self.assertIsNone(metadata)
        attempt_path = self.run_dir / "grade-recovery" / attempt["attempt_id"] / "attempt.json"
        preserved = {path: path.read_bytes() for path in (
            self.run_dir / "result.json", self.run_dir / "evaluation.json",
            self.run_dir / "evaluation_recovery.json", attempt_path)}
        adoption = adopt_recovery(self.run_dir, "Verified synthetic provider repair", project_root=self.root)
        self.assertEqual(adoption["evaluation"]["overall_score"], 0)
        self.assertTrue(adoption["adopted"])
        evaluation, metadata = effective_evaluation(self.root, self.run_dir, record)
        self.assertEqual(evaluation["overall_score"], 0)
        self.assertEqual(metadata["source"], "recovery")
        self.assertEqual(metadata["gateway_authorization_hash"], attempt["gateway_authorization_hash"])
        self.assertEqual(metadata["attempt_sha256"], digest(attempt_path))
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual((self.run_dir / "evaluation_adoption.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            adopt_recovery(self.run_dir, "Do not replace prior adoption", project_root=self.root)

    def test_adoption_rejects_candidate_attempt_hash_tampering(self):
        self.freeze()
        self.recover()
        candidate_path = self.run_dir / "evaluation_recovery.json"
        candidate = json.loads(candidate_path.read_text())
        candidate["attempt_sha256"] = "0" * 64
        candidate_path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)
        self.assertFalse((self.run_dir / "evaluation_adoption.json").exists())

    def test_adoption_rejects_an_original_valid_grade_even_if_candidate_exists(self):
        self.freeze()
        self.recover()
        original_path = self.run_dir / "result.json"
        original = json.loads(original_path.read_text())
        original["evaluation"] = {"status": "graded", "overall_score": 0}
        original_path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "valid graded result"):
            adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)
        self.assertFalse((self.run_dir / "evaluation_adoption.json").exists())

    def test_effective_evaluation_rejects_changed_snapshot_after_adoption(self):
        _, evidence = self.freeze()
        self.recover()
        adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)
        (evidence / "workspace/deliverable.txt").write_text("changed source artifact")
        record = json.loads((self.run_dir / "result.json").read_text())
        with self.assertRaisesRegex(ValueError, "evidence is invalid"):
            effective_evaluation(self.root, self.run_dir, record)

    def test_adoption_requires_zero_solver_invocations(self):
        self.freeze()
        result = self.recover()
        path = self.run_dir / "grade-recovery" / result["attempt_id"] / "attempt.json"
        attempt = json.loads(path.read_text())
        attempt["solver_invocations"] = 1
        path.write_text(json.dumps(attempt))
        candidate_path = self.run_dir / "evaluation_recovery.json"
        candidate = json.loads(candidate_path.read_text())
        candidate["attempt_sha256"] = digest(path)
        candidate_path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(ValueError, "zero solver invocations"):
            adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)

    def test_adoption_rejects_nonfinite_boolean_missing_or_out_of_range_scores(self):
        self.freeze()
        result = self.recover()
        path = self.run_dir / "grade-recovery" / result["attempt_id"] / "attempt.json"
        original_attempt = json.loads(path.read_text())
        candidate_path = self.run_dir / "evaluation_recovery.json"
        candidate = json.loads(candidate_path.read_text())
        for invalid in (float("nan"), float("inf"), None, True, -0.1, 1.1):
            with self.subTest(invalid=invalid):
                attempt = json.loads(json.dumps(original_attempt))
                attempt["evaluation"]["overall_score"] = invalid
                path.write_text(json.dumps(attempt))
                candidate["attempt_sha256"] = digest(path)
                candidate["evaluation"] = attempt["evaluation"]
                candidate_path.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError, "graded finite score"):
                    adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)

    def test_adoption_cannot_select_a_later_valid_recovery(self):
        import shutil
        self.freeze()
        first = self.recover()
        source = self.run_dir / "grade-recovery" / first["attempt_id"]
        later = source.with_name("regrade-synthetic-later")
        shutil.copytree(source, later, symlinks=True)
        attempt = json.loads((later / "attempt.json").read_text())
        attempt["attempt_id"] = later.name
        attempt["started_at_unix"] += 60
        (later / "attempt.json").write_text(json.dumps(attempt))
        candidate_path = self.run_dir / "evaluation_recovery.json"
        candidate = json.loads(candidate_path.read_text())
        candidate.update(attempt_path=str(later.relative_to(self.root)), attempt_sha256=digest(later / "attempt.json"))
        candidate_path.write_text(json.dumps(candidate))
        with self.assertRaisesRegex(ValueError, "first valid recovery"):
            adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)

    def test_effective_evaluation_rejects_modified_adoption_or_caller_record(self):
        self.freeze()
        self.recover()
        adopt_recovery(self.run_dir, "Synthetic adoption", project_root=self.root)
        record = json.loads((self.run_dir / "result.json").read_text())
        changed = dict(record, status="modified")
        with self.assertRaisesRegex(ValueError, "Caller record differs"):
            effective_evaluation(self.root, self.run_dir, changed)
        adoption_path = self.run_dir / "evaluation_adoption.json"
        adoption = json.loads(adoption_path.read_text())
        adoption["attempt_sha256"] = "f" * 64
        adoption_path.write_text(json.dumps(adoption))
        with self.assertRaisesRegex(ValueError, "provenance"):
            effective_evaluation(self.root, self.run_dir, record)

    def test_verifier_stdin_copy_is_binary_preserving_and_phase_restricted(self):
        runtime = self.factory(self.config, self.task, self.run_dir, "copy-only")
        source = self.root / "binary.dat"
        source.write_bytes(b"\x00binary\xff\n")
        with self.assertRaises(RuntimeError):
            runtime.copy_file_to_container(source, "/tmp/binary.dat")
        runtime.state = "grading"
        runtime.copy_file_to_container(source, "/tmp/binary.dat")
        import base64
        args, kwargs = self.docker_calls[-1]
        self.assertEqual(args[0], "exec")
        self.assertEqual(base64.b64decode(kwargs["input_text"]), source.read_bytes())
        self.assertIn("O_NOFOLLOW", args[-2])
        with self.assertRaises(ValueError):
            runtime.copy_file_to_container(source, "/root/.codex/auth.json")


if __name__ == "__main__":
    unittest.main()
