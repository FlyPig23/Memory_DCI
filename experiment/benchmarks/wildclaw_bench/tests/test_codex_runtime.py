"""Offline behavioral checks; never launch Docker or invoke a model."""
from __future__ import annotations

import json
import subprocess
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from experiment.shared.codex_backend import (
    CodexSettings, MCPServer, build_exec_argv, build_prompt, convert_transcript,
    extract_usage, render_config, event_to_openclaw,
)
from experiment.shared.task_runtime import (
    OWNER_LABEL, PREFIX, Mount, RuntimeConfig, RuntimeErrorWithStage,
    TaskRuntime, TaskSpec, load_task_spec, official_score,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # User-authorized workspace only, including test caches and scratch data.
        self.scratch = tempfile.TemporaryDirectory(prefix="runtime-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.scratch.name)
        self.inputs = self.root / "inputs"
        (self.inputs / "exec").mkdir(parents=True)
        (self.inputs / "exec" / "input.txt").write_text("public")
        (self.inputs / "gt").mkdir()
        (self.inputs / "gt" / "answer.txt").write_text("PRIVATE_GOLD")
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.task = TaskSpec("task1", "category", "Solve the public task", self.inputs,
                             self.skills, automated_checks="def grade(**kwargs):\n    return {'overall_score': 0.5}")
        self.config = RuntimeConfig(self.root, "example@sha256:fixed", docker_host="unix:///private.sock")

    def tearDown(self):
        self.scratch.cleanup()

    def runtime(self):
        return TaskRuntime(self.config, self.task, self.root / "run", "test-1")

    def test_auth_is_not_an_openrouter_key_and_mcp_is_optional(self):
        baseline = tomllib.loads(render_config(CodexSettings()))
        self.assertEqual(baseline["model_provider"], "openai")
        self.assertNotIn("model_providers", baseline)
        self.assertNotIn("mcp_servers", baseline)
        self.assertEqual(baseline["web_search"], "disabled")
        treatment = tomllib.loads(render_config(CodexSettings(mcp_servers=(
            MCPServer("dci", "python3", ("/opt/proxy.py",), {"DCI_ENDPOINT": "http://evidence:8080"}),))))
        self.assertEqual(treatment["mcp_servers"]["dci"]["command"], "python3")
        self.assertEqual(build_exec_argv(CodexSettings())[-1], "-")

    def test_prompt_and_initial_mounts_do_not_expose_grading(self):
        runtime = self.runtime()
        calls = []

        def fake_docker(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "container-id\n", "")

        with patch.object(runtime, "docker", side_effect=fake_docker):
            runtime.prepare(render_config(CodexSettings()))
        self.assertFalse((runtime.workspace / "gt").exists())
        self.assertEqual((runtime.workspace / "input.txt").read_text(), "public")
        mounts = " ".join(calls[0])
        self.assertNotIn(str(self.inputs), mounts)
        self.assertNotIn("PRIVATE_GOLD", build_prompt(self.task))
        self.assertNotIn("def grade", build_prompt(self.task))
        self.assertIn("--read-only", calls[0])
        self.assertIn(f"{OWNER_LABEL}={PREFIX}", calls[0])
        self.assertIn("--entrypoint", calls[0])

    def test_gold_injection_requires_whole_container_stop(self):
        runtime = self.runtime()
        runtime.state = "prepared"
        transcript = runtime.run_dir / "transcript.jsonl"
        loader = self.root / "transcript_loader.py"
        with patch.object(runtime, "docker") as docker:
            with self.assertRaises(RuntimeErrorWithStage):
                runtime.grade(transcript_path=transcript, transcript_loader=loader)
            docker.assert_not_called()
        self.assertFalse((runtime.workspace / "gt").exists())

    def test_freeze_stops_before_state_transition(self):
        runtime = self.runtime()
        runtime.run_dir.mkdir()
        runtime.state = "prepared"
        calls = []

        def fake_docker(*args, **kwargs):
            calls.append(args)
            stdout = PREFIX if ".Config.Labels" in " ".join(args) else "false"
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with patch.object(runtime, "docker", side_effect=fake_docker):
            runtime.freeze_agent()
        self.assertEqual(runtime.state, "solver_stopped")
        self.assertEqual(calls[1], ("stop", "--time", "5", runtime.name))
        self.assertNotIn("start", [c[0] for c in calls])

    def test_startup_is_restored_for_grading_without_replaying_task_warmup(self):
        runtime = self.runtime()
        runtime.run_dir.mkdir()
        runtime.workspace.mkdir()
        runtime.staging.mkdir()
        runtime.state = "solver_stopped"
        transcript = runtime.run_dir / "transcript.jsonl"
        transcript.write_text("")
        loader = self.root / "loader.py"
        loader.write_text("def load_transcript(path): return []")
        calls = []

        def fake_docker(*args, **kwargs):
            calls.append(args)
            if args[0] == "start":
                self.assertFalse((runtime.workspace / "gt").exists())
            if ".Config.Labels" in " ".join(args):
                stdout = PREFIX
            elif "python3" in args:
                stdout = '{"overall_score": 0.5}'
            else:
                stdout = ""
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with patch.object(runtime, "docker", side_effect=fake_docker), \
             patch.object(runtime, "run_startup_commands") as startup, \
             patch.object(runtime, "run_warmup") as warmup:
            result = runtime.grade(transcript_path=transcript, transcript_loader=loader)
            startup.assert_called_once_with()
            warmup.assert_not_called()
        self.assertEqual(result["overall_score"], 0.5)
        self.assertEqual(runtime.state, "graded")
        self.assertTrue((runtime.workspace / "gt" / "answer.txt").is_file())

    def test_mutation_of_foreign_container_is_rejected(self):
        runtime = self.runtime()
        runtime.created = True
        with patch.object(runtime, "docker", return_value=subprocess.CompletedProcess([], 0, "someone-else", "")) as docker:
            with self.assertRaises(RuntimeErrorWithStage):
                runtime.cleanup()
            self.assertEqual(docker.call_count, 1)

    def test_paths_and_host_network_are_rejected(self):
        with self.assertRaises(ValueError):
            TaskRuntime(self.config, self.task, self.root.parent / "outside", "test")
        with self.assertRaises(ValueError):
            TaskRuntime(RuntimeConfig(self.root, "image", network="host"), self.task, self.root / "run", "test")
        with self.assertRaises(ValueError):
            TaskRuntime(RuntimeConfig(self.root, "image", support_mounts=(Mount(self.root, "/root", True),)),
                        self.task, self.root / "run", "test")
        self.assertEqual(self.runtime().docker_command("info")[:3], ["docker", "--host", "unix:///private.sock"])

    def test_partial_scores_and_missing_scores_remain_distinct(self):
        self.assertEqual(official_score({"overall_score": 0.75, "a": 0, "b": 0}), 0.75)
        self.assertEqual(official_score({"a": 0.5, "b": 1}), 0.75)
        self.assertIsNone(official_score({"error": "judge unavailable"}))
        with self.assertRaises(ValueError):
            official_score({"overall_score": float("nan")})

    def test_codex_transcript_avoids_duplicate_events_and_handles_custom_tools(self):
        source, target = self.root / "session.jsonl", self.root / "transcript.jsonl"
        records = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Final answer"}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Final answer"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
             "call_id": "1", "input": "*** patch ***"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "1", "output": "done"}},
        ]
        source.write_text("\n".join(json.dumps(r) for r in records))
        self.assertEqual(convert_transcript(source, target), 3)
        parsed = [json.loads(line) for line in target.read_text().splitlines()]
        self.assertEqual(parsed[1]["message"]["content"][0]["name"], "apply_patch")
        self.assertEqual(parsed[2]["message"]["content"][0]["content"], "done")

    def test_usage_uses_last_cumulative_value_not_sum(self):
        source = self.root / "usage.jsonl"
        source.write_text("\n".join(json.dumps({"type": "event_msg", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"input_tokens": i, "output_tokens": i // 10}}}})
            for i in [100, 200, 200]))
        usage = extract_usage(source)
        self.assertEqual(usage["input_tokens"], 200)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertIsNone(usage["reasoning_output_tokens"])

    def test_reasoning_is_not_a_visible_warning_and_tool_images_keep_structure(self):
        records = event_to_openclaw({"type": "response_item", "payload": {
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "I should warn about a risk"}]}})
        blocks = records[0]["message"]["content"]
        self.assertEqual(blocks[0]["type"], "thinking")
        self.assertFalse(any(block.get("type") == "text" for block in blocks))
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "synthetic"}}
        output = event_to_openclaw({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "img1", "output": [image]}})
        self.assertEqual(output[0]["message"]["content"][0]["content"], [image])

    def test_failed_docker_output_is_private_and_not_in_exception(self):
        runtime = self.runtime()
        with patch("experiment.shared.task_runtime.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, "synthetic stdout", "SYNTHETIC_SECRET")):
            with self.assertRaises(RuntimeErrorWithStage) as error:
                runtime.docker("exec", runtime.name, "false")
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        logs = list(runtime.staging.glob("docker-*.log"))
        self.assertEqual(len(logs), 2)
        self.assertTrue(any("SYNTHETIC_SECRET" in path.read_text() for path in logs))
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in logs))

    def test_judge_fault_with_numeric_zero_is_not_scored_as_task_failure(self):
        from experiment.shared.task_runtime import grading_issue
        for marker in ("judge_error", "llm_error", "llm_judge_error"):
            self.assertEqual(grading_issue({"overall_score": 0, marker: "dependency unavailable"}), "judge_error")
        self.assertEqual(grading_issue({"overall_score": 0, "judge_method": "failed"}), "judge_error")
        self.assertEqual(grading_issue({"overall_score": 0, "mode": "fallback+llm_judge"}), "judge_degraded")
        self.assertIsNone(grading_issue({"overall_score": 0, "judge_error": None}))

    def test_reviewed_warmup_override_is_used_and_both_hashes_are_recorded(self):
        config = replace(self.config, warmup_override="echo reviewed", user="123:123")
        task = replace(self.task, warmup="echo original")
        runtime = TaskRuntime(config, task, self.root / "run", "override")
        with patch.object(runtime, "docker", return_value=subprocess.CompletedProcess([], 0, "container", "")) as docker, \
             patch.object(runtime, "run_warmup") as warmup:
            runtime.prepare(render_config(CodexSettings()))
        warmup.assert_called_once_with("echo reviewed")
        self.assertIn("--user", docker.call_args.args)
        state = json.loads((runtime.run_dir / "runtime_state.json").read_text())
        self.assertEqual(len(state["original_warmup_sha256"]), 64)
        self.assertNotEqual(state["original_warmup_sha256"], state["warmup_override_sha256"])

    def test_task_loader_splits_private_sections(self):
        path = self.root / "tasks" / "category" / "task.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nid: toy\ntimeout_seconds: 50\n---\n## Prompt\nPublic question\n"
                        "## Workspace Path\n```\nworkspace/category/toy\n```\n"
                        "## Automated Checks\n```python\nPRIVATE_GOLD\n```\n")
        task = load_task_spec(path, self.root, workspace_root=self.inputs, timeout_seconds=100)
        self.assertEqual(task.prompt, "Public question")
        self.assertEqual(task.automated_checks, "PRIVATE_GOLD")
        self.assertEqual(task.timeout_seconds, 100)
        self.assertNotIn("PRIVATE_GOLD", repr(task))


if __name__ == "__main__":
    unittest.main()
