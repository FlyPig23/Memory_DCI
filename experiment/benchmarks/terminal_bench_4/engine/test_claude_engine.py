import json
from pathlib import Path
import tempfile
import unittest

from experiment.benchmarks.terminal_bench_4.engine import claude_engine, harbor_runtime, health_guard, protocol


def events(logs, model_usage, *, init="claude-sonnet-5", fallback=False, error=False):
    rows = [{"type": "system", "subtype": "init", "model": init},
            {"type": "assistant", "message": {"id": "m1", "role": "assistant", "usage": {
                "input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 7},
                "content": [{"type": "text", "text": "plan"}, {"type": "tool_use", "id": "t1", "name": "Bash",
                                                              "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]}}]
    if fallback:
        rows.append({"type": "system", "subtype": "model_refusal_fallback", "original_model": init,
                     "fallback_model": "claude-opus-5", "api_refusal_category": "bio"})
    rows.append({"type": "result", "subtype": "success", "is_error": error, "result": "done", "total_cost_usd": 0.5,
                 "num_turns": 2, "modelUsage": model_usage})
    Path(logs, claude_engine.EVENTS).write_text("".join(json.dumps(r) + "\n" for r in rows))


USAGE = {"inputTokens": 3, "outputTokens": 7, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 10}


class ClaudeEngineTest(unittest.TestCase):
    def test_same_model_accepts_only_suffixes_of_the_requested_model(self):
        for name in ("claude-sonnet-5", "claude-sonnet-5-20260601", "claude-sonnet-5[1m]"):
            self.assertTrue(claude_engine.same_model(name, "claude-sonnet-5"))
        for name in ("claude-sonnet-5-5", "claude-opus-5", None):
            self.assertFalse(claude_engine.same_model(name, "claude-sonnet-5"))

    def test_capture_session_writes_usage_audit_and_review_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            events(directory, {"claude-sonnet-5": USAGE})
            usage, audit = claude_engine.capture_session(directory, "claude-sonnet-5", "medium", ["reason"])
            self.assertEqual((usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"]), (113, 100, 7))
            self.assertEqual((audit["models"], audit["efforts"], audit["model_effort_verified"]),
                             (["claude-sonnet-5"], ["medium"], True))
            transcript = [json.loads(line) for line in Path(directory, "transcript.openclaw.jsonl").read_text().splitlines()]
            self.assertEqual([row["message"]["role"] for row in transcript], ["assistant", "user"])
            self.assertEqual(Path(directory, "final.txt").read_text(), "done")
            # The run-health check counts this as real inference.
            self.assertTrue(health_guard._positive_usage(json.loads(Path(directory, "usage.json").read_text())))

    def test_another_model_with_output_fails_the_audit(self):
        cases = [dict(model_usage={"claude-sonnet-5": USAGE, "claude-haiku-4-5": USAGE}),
                 dict(model_usage={"claude-sonnet-5": USAGE}, fallback=True),
                 dict(model_usage={"claude-sonnet-5-5": USAGE}, init="claude-sonnet-5-5")]
        for case in cases:
            with tempfile.TemporaryDirectory() as directory:
                events(directory, **case)
                _, audit = claude_engine.capture_session(directory, "claude-sonnet-5", "medium", [])
                self.assertFalse(audit["model_effort_verified"])
                self.assertNotEqual(audit["models"], ["claude-sonnet-5"])

    def test_engine_switch_selects_the_cli_and_keeps_codex_the_default(self):
        self.assertEqual(harbor_runtime.ENGINE, "codex")
        self.assertEqual(harbor_runtime.codex_command("/w", "/o")[0], "/opt/codex/codex")
        self.assertNotIn("engine", protocol.settings())
        self.assertEqual(protocol.settings(engine="claude", model="claude-sonnet-5")["cli_version"], claude_engine.CLI_VERSION)
        harbor_runtime.ENGINE = "claude"
        try:
            command = harbor_runtime.codex_command("/w", "/o")
            self.assertEqual(command[1:5], [claude_engine.RELAY_EXEC, "18191", claude_engine.RELAY_SOCKET, claude_engine.BINARY])
            self.assertEqual(command[-3:], ["WebSearch", "WebFetch", "Task"])
            config = json.loads(harbor_runtime.make_config("t", "/w", "solve", pool_scope="same_task"))
            self.assertEqual(config["mcpServers"]["v5"]["args"][:2], [harbor_runtime.SERVER, "mcp"])
            self.assertEqual(harbor_runtime.config_name(), "mcp.json")
            self.assertNotIn("HOME", harbor_runtime.cli_environment())
            self.assertTrue(harbor_runtime.forbidden_evidence("/opt/claude/claude"))
        finally:
            harbor_runtime.ENGINE = "codex"

    def test_copy_fallback_extracts_like_docker_cp(self):
        import io, os, tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src/sub").mkdir(parents=True)
            (root / "src/sub/run.sh").write_text("echo hi\n")
            os.chmod(root / "src/sub/run.sh", 0o755)
            os.symlink("sub/run.sh", root / "src/link")
            # The archive the mounted Python writes inside the container.
            with tarfile.open(root / "copy.tar", "w") as stream:
                stream.add(root / "src", arcname=".")
                escape = tarfile.TarInfo("../outside.txt")
                stream.addfile(escape, io.BytesIO(b""))
            with self.assertRaises(tarfile.OutsideDestinationError):
                harbor_runtime.extract_like_cp(root / "copy.tar", root / "bad")
            with tarfile.open(root / "copy.tar", "w") as stream:
                stream.add(root / "src", arcname=".")
            harbor_runtime.extract_like_cp(root / "copy.tar", root / "out")
            self.assertEqual((root / "out/sub/run.sh").read_text(), "echo hi\n")
            self.assertEqual((root / "out/sub/run.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(root / "out/link"), "sub/run.sh")


if __name__ == "__main__":
    unittest.main()
