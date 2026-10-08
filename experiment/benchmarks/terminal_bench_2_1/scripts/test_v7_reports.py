"""No-model tests for the V7 report tools: the cross-arm comparison and the builder audit."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from . import v7_builder_audit, v7_compare


def write_lines(path: Path, events) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def command(text):
    return {"type": "item.completed", "item": {"type": "command_execution", "command": text}}


class CompareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def trial(self, *events):
        trial = self.root / f"trial-{len(list(self.root.iterdir()))}"
        write_lines(trial / "agent/codex.events.jsonl", events)
        return trial

    def test_benchmark_source_rule_flags_fetches_not_ordinary_downloads(self):
        clean = self.trial(command("pip install numpy"), command("curl -L https://github.com/llvm/llvm-project/x.tar.xz"),
                           {"type": "item.started", "item": {"type": "command_execution",
                                                             "command": "git clone https://github.com/laude-institute/terminal-bench"}})
        self.assertEqual(v7_compare.source_access(clean), [])
        dirty = self.trial(command("git clone -q https://github.com/laude-institute/terminal-bench.git /tmp/tb"),
                           command("sed -n 1,80p /tmp/terminal-bench-1-main/original-tasks/x/tests/test_outputs.py"))
        self.assertEqual(v7_compare.source_access(dirty),
                         ["github.com/laude-institute/terminal-bench", "original-tasks/", "terminal-bench-1"])

    def test_commands_include_file_changes(self):
        trial = self.trial(command("ls"), {"type": "item.completed", "item": {
            "type": "file_change", "changes": [{"path": "/app/filter.py"}]}})
        self.assertEqual(v7_compare.commands(trial / "agent/codex.events.jsonl"), ["ls", "file_change /app/filter.py"])

    def test_paired_counts_wins_losses_and_skips_missing_references(self):
        rows = [{"task_id": "a", "counted_reward": 1.0, "base": 0.0}, {"task_id": "b", "counted_reward": 0.0, "base": 1.0},
                {"task_id": "c", "counted_reward": 1.0, "base": 1.0}, {"task_id": "d", "counted_reward": 1.0, "base": None}]
        self.assertEqual(v7_compare.paired(rows, "base", "baseline"),
                         {"n": 3, "v7_passed": 2.0, "baseline_passed": 2.0, "wins": ["a"], "losses": ["b"], "ties": 1})


class BuilderAuditTests(unittest.TestCase):
    def stream(self, *blocks):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        events = [{"type": "assistant" if block["type"] == "tool_use" else "user", "message": {"content": [block]}}
                  for block in blocks]
        return write_lines(Path(temporary.name) / "claude.events.jsonl", events)

    def test_quoted_patterns_are_not_access_but_executed_programs_are(self):
        events = self.stream(
            {"type": "tool_use", "name": "Bash", "input": {"command": 'grep -n "pip3 install\\|curl" /pool/x.txt'}},
            {"type": "tool_result", "content": "attempt fetched https://www.tbench.ai/tasks/x"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "curl -s https://example.org"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/work/fetch.py", "content": "import urllib.request"}})
        audit = v7_builder_audit.audit_attempt(events)
        self.assertEqual(audit["outside_access_in_inputs"], ["curl", "network code written to a script"])
        self.assertEqual(audit["benchmark_source_in_inputs"], [])
        self.assertEqual(audit["benchmark_source_in_results"], ["tbench.ai"])
        self.assertEqual(audit["tool_calls"], {"input:Bash": 2, "result": 1, "input:Write": 1})


if __name__ == "__main__":
    unittest.main()
