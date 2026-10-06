"""Synthetic hook lifecycle checks; no Docker, model, or task data calls."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiment.shared.task_runtime import PREFIX, RuntimeConfig, TaskRuntime, TaskSpec, grading_issue


class MockStateHookTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="hook-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.scratch.name)
        self.inputs = self.root / "inputs"
        (self.inputs / "exec").mkdir(parents=True)
        (self.inputs / "gt").mkdir()
        (self.inputs / "gt" / "answer.txt").write_text("synthetic verifier data")
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.task = TaskSpec("toy", "synthetic", "Toy task", self.inputs, self.skills,
                             automated_checks="def grade(**kwargs): return {'overall_score': 0.5}")
        self.calls = []

    def tearDown(self):
        self.scratch.cleanup()

    def runtime(self, **hooks):
        runtime = TaskRuntime(RuntimeConfig(self.root, "synthetic", **hooks), self.task,
                              self.root / "run", "hooks")
        runtime.run_dir.mkdir()
        runtime.workspace.mkdir()
        runtime.staging.mkdir()
        runtime.state = "prepared"
        return runtime

    def docker(self, *args, **kwargs):
        self.calls.append(args[0])
        stdout = (PREFIX if ".Config.Labels" in " ".join(args) else
                  "false" if "{{.State.Running}}" in args else
                  '{"overall_score": 0.5}' if "python3" in args else "")
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def grade(self, runtime):
        transcript = runtime.run_dir / "transcript.jsonl"
        transcript.write_text("")
        loader = self.root / "loader.py"
        loader.write_text("def load_transcript(path): return []")
        return runtime.grade(transcript_path=transcript, transcript_loader=loader)

    def test_capture_precedes_stop_and_restore_precedes_gold(self):
        def capture(runtime):
            self.assertEqual(runtime.state, "prepared")
            self.assertNotIn("stop", self.calls)
            self.calls.append("capture")
            return "snapshot-id"

        def restore(runtime):
            self.assertEqual(runtime.state, "grading")
            self.assertEqual(runtime.freeze_hook_result, "snapshot-id")
            self.assertFalse((runtime.workspace / "gt").exists())
            self.calls.append("restore")

        runtime = self.runtime(before_freeze_hook=capture, after_restart_hook=restore)
        with patch.object(runtime, "docker", side_effect=self.docker), patch.object(
                runtime, "run_startup_commands", side_effect=lambda: self.calls.append("relay")):
            runtime.freeze_agent()
            runtime.freeze_agent()
            result = self.grade(runtime)
        self.assertEqual(self.calls.count("capture"), 1)
        self.assertLess(self.calls.index("capture"), self.calls.index("stop"))
        self.assertLess(self.calls.index("start"), self.calls.index("relay"))
        self.assertLess(self.calls.index("relay"), self.calls.index("restore"))
        self.assertEqual(result["overall_score"], 0.5)

    def test_capture_failure_still_stops_and_never_runs_grader(self):
        def capture(runtime):
            raise RuntimeError("private synthetic capture detail")

        runtime = self.runtime(before_freeze_hook=capture)
        with patch.object(runtime, "docker", side_effect=self.docker):
            runtime.freeze_agent()
            self.assertEqual(runtime.state, "solver_stopped")
            result = self.grade(runtime)
        self.assertIn("stop", self.calls)
        self.assertNotIn("start", self.calls)
        self.assertEqual(result["status"], "mock_state_error")
        self.assertEqual(result["stage"], "capture")
        self.assertIsNone(result["overall_score"])
        self.assertFalse((runtime.workspace / "gt").exists())
        self.assertNotIn("private synthetic", json.dumps(result))
        self.assertEqual((runtime.staging / "mock-state-capture-error.log").stat().st_mode & 0o777, 0o600)

    def test_restore_failure_never_introduces_gold_or_runs_grader(self):
        def restore(runtime):
            raise RuntimeError("private synthetic restore detail")

        runtime = self.runtime(after_restart_hook=restore)
        with patch.object(runtime, "docker", side_effect=self.docker), patch.object(runtime, "run_startup_commands"):
            runtime.freeze_agent()
            result = self.grade(runtime)
        self.assertEqual(result["status"], "mock_state_error")
        self.assertEqual(result["stage"], "restore")
        self.assertNotIn("cp", self.calls)
        self.assertFalse((runtime.workspace / "gt").exists())

    def test_unknown_errors_require_review_and_known_artifact_zeros_remain(self):
        self.assertEqual(grading_issue({"overall_score": 0, "error": "unknown failure"}), "needs_review")
        self.assertIsNone(grading_issue({"overall_score": 0, "error": "results.md is empty"}))
        self.assertEqual(grading_issue({"overall_score": 0.5, "error": "results.md is empty"}), "needs_review")


if __name__ == "__main__":
    unittest.main()
