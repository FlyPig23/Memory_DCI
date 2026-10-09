"""Synthetic auth-lifetime and inference-evidence checks; no model/network calls."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest

from experiment.benchmarks.terminal_bench_4.engine import health_guard


NOW = 1_800_000_000
SECRET = "synthetic-secret-never-include-in-diagnostics"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def jwt(payload):
    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return ".".join((segment({"alg": "HS256", "typ": "JWT"}), segment(payload), SECRET))


class AuthLifetimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.auth = Path(temporary.name) / "synthetic-auth.json"

    def save_token(self, payload):
        token = jwt(payload)
        write_json(self.auth, {"tokens": {"access_token": token, "refresh_token": SECRET}})
        return token

    def assert_private_rejection(self, **kwargs):
        with self.assertRaises(health_guard.AuthValidityError) as raised:
            health_guard.ensure_auth_lifetime(self.auth, 900, now=NOW, **kwargs)
        diagnostic = str(raised.exception)
        self.assertTrue(diagnostic)
        self.assertNotIn(SECRET, diagnostic)
        if self.auth.exists():
            try:
                token = json.loads(self.auth.read_text())["tokens"]["access_token"]
            except (KeyError, TypeError, json.JSONDecodeError):
                return
            if isinstance(token, str) and token:
                self.assertNotIn(token, diagnostic)

    def test_requires_more_than_solver_review_and_margin(self):
        # Defaults: 900 solver + 180 review + 300 safety margin.
        for remaining in (-1, 0, 1379, 1380):
            with self.subTest(remaining=remaining):
                self.save_token({"exp": NOW + remaining, "private_claim": SECRET})
                self.assert_private_rejection()
        token = self.save_token({"exp": NOW + 1381, "private_claim": SECRET})
        metadata = health_guard.ensure_auth_lifetime(self.auth, 900, now=NOW)
        self.assertIsInstance(metadata, dict)
        serialized = json.dumps(metadata)
        self.assertNotIn(token, serialized)
        self.assertNotIn(SECRET, serialized)

    def test_explicit_review_and_margin_are_included(self):
        self.save_token({"exp": NOW + 940})
        self.assert_private_rejection(review_seconds=30, margin_seconds=10)
        self.save_token({"exp": NOW + 941})
        self.assertIsInstance(health_guard.ensure_auth_lifetime(
            self.auth, 900, review_seconds=30, margin_seconds=10, now=NOW), dict)

    def test_missing_or_malformed_auth_is_generic_and_private(self):
        self.assert_private_rejection()
        for value in ({}, {"tokens": None}, {"tokens": []},
                      {"tokens": {}}, {"tokens": {"access_token": None}},
                      {"tokens": {"access_token": SECRET}},
                      {"tokens": {"access_token": "header.%%%." + SECRET}}):
            with self.subTest(value_type=type(value.get("tokens")).__name__):
                write_json(self.auth, value)
                self.assert_private_rejection()
        self.auth.write_text('{"tokens": ' + SECRET)
        self.assert_private_rejection()

    def test_missing_or_invalid_exp_is_rejected_without_token_disclosure(self):
        for payload in ({}, {"exp": None}, {"exp": True}, {"exp": "1800009999"},
                        {"exp": []}, {"exp": float("nan")}, {"exp": float("inf")}):
            with self.subTest(exp=repr(payload.get("exp"))):
                self.save_token({**payload, "private_claim": SECRET})
                self.assert_private_rejection()


class TrialHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def phase(self, name="agent", *, session=(), events=(), stderr="", usage=None):
        root = self.directory / name
        root.mkdir(parents=True, exist_ok=True)
        write_jsonl(root / "codex.session.jsonl", session)
        write_jsonl(root / "codex.events.jsonl", events)
        (root / "codex.stderr.log").write_text(stderr)
        write_json(root / "usage.json", usage)
        return root

    def inspect(self, *, blocked, phase="agent", actual_inference, failure=None):
        result = health_guard.inspect_trial(self.directory)
        self.assertEqual(result["blocked"], blocked)
        observed = result["phases"][phase]
        self.assertEqual(observed["actual_inference"], actual_inference)
        if failure is not None:
            self.assertIn(failure, observed["failure_kinds"])
        return result

    def test_metadata_and_model_audit_do_not_make_unauthorized_attempt_real(self):
        root = self.phase(session=[
            {"type": "session_meta", "payload": {"model_provider": "openai"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "medium"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                     "content": [{"type": "input_text", "text": "Solve task"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ], events=[
            {"type": "thread.started", "thread_id": "synthetic"},
            {"type": "turn.started"},
            {"type": "error", "message": "401 Unauthorized: refresh_token_reused"},
            {"type": "turn.failed", "error": {"message": "Your access token could not be refreshed."}},
        ], usage={"input_tokens": None, "output_tokens": None, "cached_input_tokens": None})
        write_json(root / "model_audit.json", {"model_effort_verified": True,
                                               "models": ["gpt-5.6-sol"], "efforts": ["medium"]})
        self.inspect(blocked=True, actual_inference=False, failure="authentication")

    def test_tls_failure_in_reviewer_blocks_even_after_genuine_solver_attempt(self):
        self.phase(usage={"input_tokens": 12, "output_tokens": 3})
        self.phase("review", events=[{"type": "turn.failed", "error": {
            "message": "TLS handshake failed: certificate verify failed (unknown issuer)"}}])
        result = self.inspect(blocked=True, phase="review", actual_inference=False, failure="tls")
        self.assertTrue(result["phases"]["agent"]["actual_inference"])

    def test_stderr_network_failure_without_inference_blocks(self):
        self.phase(stderr="ERROR request failed: Connection refused (os error 111)\n")
        self.inspect(blocked=True, actual_inference=False, failure="network")

    def test_assistant_attempt_with_zero_grade_is_preserved_despite_transient_auth(self):
        self.phase(session=[{"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "I inspected the database and tried recovery."}]}}],
            stderr="ERROR transient request: 401 Unauthorized\n")
        write_json(self.directory / "v5_result.json", {
            "harbor": {"verifier_result": {"rewards": {"reward": 0}}}})
        self.inspect(blocked=False, actual_inference=True)

    def test_session_tool_call_counts_as_genuine_execution(self):
        self.phase(session=[{"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "synthetic-call",
            "arguments": json.dumps({"cmd": "sqlite3 --version"})}}],
            stderr="ERROR TLS handshake failed: certificate verify failed\n")
        self.inspect(blocked=False, actual_inference=True)

    def test_completed_cli_command_event_counts_as_genuine_execution(self):
        self.phase(events=[{"type": "item.completed", "item": {
            "id": "item_1", "type": "command_execution", "command": "sqlite3 --version",
            "aggregated_output": "3.46.0", "exit_code": 0, "status": "completed"}}],
            stderr="ERROR network connection reset by peer\n")
        self.inspect(blocked=False, actual_inference=True)

    def test_positive_usage_counts_even_if_message_capture_is_missing(self):
        self.phase(usage={"input_tokens": 7, "output_tokens": 0, "cached_input_tokens": 0},
                   stderr="ERROR request failed: Connection refused (os error 111)\n")
        self.inspect(blocked=False, actual_inference=True)

    def test_null_or_zero_usage_alone_is_not_an_infrastructure_failure(self):
        for usage in (None, {}, {"input_tokens": None, "output_tokens": None},
                      {"input_tokens": 0, "output_tokens": 0}):
            with self.subTest(usage=usage):
                self.phase(usage=usage, events=[{"type": "thread.started", "thread_id": "synthetic"}])
                result = self.inspect(blocked=False, actual_inference=False)
                self.assertEqual(result["phases"]["agent"]["failure_kinds"], [])

    def test_absent_logs_alone_do_not_claim_infrastructure_failure(self):
        self.inspect(blocked=False, actual_inference=False)


if __name__ == "__main__":
    unittest.main()
