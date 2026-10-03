"""Offline controller-hook tests and a local immutable replay integration test."""
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from experiment.src import mock_state

TASK = "03_Social_Interaction_task_4_chat_thread_consolidation"
PAYLOAD = {"calls": [{"endpoint": "/slack/messages/get"}, {"endpoint": "/slack/drafts/save"}],
           "drafts": [{"to": "reviewer", "content": "saved draft"}], "sent_messages": []}


class FakeRuntime:
    def __init__(self, root, task=TASK):
        self.task = SimpleNamespace(task_id=task)
        self.name = "hangxiao-skill-dci-private-test"
        self.state = "prepared"
        self.config = SimpleNamespace(project_root=Path(__file__).resolve().parents[2])
        self.run_dir = root
        self.staging = root / "controller-staging"
        self.calls = []
        self.fail_capture = False

    def exec(self, argv, **kwargs):
        self.calls.append(("exec", argv, kwargs))
        if argv == ["python3", "-c", mock_state.CAPTURE_SCRIPT]:
            if self.fail_capture:
                raise RuntimeError("service is down")
            return SimpleNamespace(stdout=json.dumps({"responses": {"slack": PAYLOAD}, "captured_at_unix": 1.0}))
        if argv == ["python3", "-c", mock_state.VERIFY_REPLAY_SCRIPT]:
            return SimpleNamespace(stdout='{"status":"mock_audit_replayed","services":["slack"]}')
        return SimpleNamespace(stdout="")

    def copy_file_to_container(self, source, target):
        self.calls.append(("copy_file", source, target))

    def docker(self, *args):
        self.calls.append(("docker", args))


def test_capture_is_before_stop_fixed_endpoint_and_controller_only(tmp_path):
    runtime = FakeRuntime(tmp_path)
    snapshot_path = mock_state.capture_mock_state(runtime)
    assert snapshot_path.parent == runtime.staging
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["responses"]["slack"] == PAYLOAD
    assert snapshot["response_sha256"]["slack"] == hashlib.sha256(mock_state.canonical(PAYLOAD)).hexdigest()
    request = json.loads(runtime.calls[0][2]["input_text"])
    assert request["endpoints"] == {"slack": [9110, "/slack/audit"]}
    with pytest.raises(FileExistsError):
        mock_state.capture_mock_state(runtime)


def test_capture_failure_is_explicit_and_does_not_invent_empty_audit(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.fail_capture = True
    with pytest.raises(RuntimeError, match="must not silently"):
        mock_state.capture_mock_state(runtime)
    assert not (runtime.staging / "mock_state.json").exists()
    record = json.loads((runtime.staging / "mock_state_error.json").read_text())
    assert record["status"] == "mock_audit_capture_failed"


def test_restore_requires_verifier_phase_and_same_run(tmp_path):
    runtime = FakeRuntime(tmp_path)
    path = mock_state.capture_mock_state(runtime)
    with pytest.raises(RuntimeError, match="verifier"):
        mock_state.restore_mock_state(runtime, path)
    runtime.state = "grading"
    result = mock_state.restore_mock_state(runtime, path)
    assert result["status"] == "mock_audit_replayed"
    assert sum(call[0] == "copy_file" for call in runtime.calls) == 2
    assert not any(call[0] == "docker" and call[1][0] == "cp" for call in runtime.calls)
    assert not any("fixtures" in str(call) or "/reset" in str(call) for call in runtime.calls)


def test_tampered_snapshot_is_rejected_before_docker_copy(tmp_path):
    runtime = FakeRuntime(tmp_path)
    path = mock_state.capture_mock_state(runtime)
    snapshot = json.loads(path.read_text())
    snapshot["responses"]["slack"]["drafts"] = []
    path.write_text(json.dumps(snapshot))
    runtime.state = "grading"
    before = len(runtime.calls)
    with pytest.raises(ValueError, match="hash mismatch"):
        mock_state.restore_mock_state(runtime, path)
    assert len(runtime.calls) == before


def test_non_social_tasks_have_no_mock_hooks(tmp_path):
    runtime = FakeRuntime(tmp_path, task="01_Productivity_Flow_task_3_bibtex")
    assert mock_state.preflight_mock_state(runtime) == {"status": "not_required", "services": []}
    assert mock_state.capture_mock_state(runtime) is None
    assert mock_state.restore_mock_state(runtime, None)["status"] == "not_required"
    assert runtime.calls == []


def test_service_contract_is_fixed_from_public_task_api():
    assert mock_state.services_for_task("03_Social_Interaction_task_1_meeting_negotiation") == ("gmail", "calendar")
    assert mock_state.services_for_task("03_Social_Interaction_task_5_chat_escalation_routing") == ("slack", "contacts")
    with pytest.raises(ValueError):
        mock_state.services_for_task("03_Social_Interaction_task_99_unknown")


def test_replay_returns_exact_audit_and_rejects_state_mutation():
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_state.replay_handler(PAYLOAD, "/slack/audit"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/slack/audit", timeout=3) as response:
            assert json.load(response) == PAYLOAD
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/slack/reset", data=b"{}", method="POST"), timeout=3)
        assert error.value.code == 405
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/slack/messages", timeout=3)
        assert error.value.code == 404
        with urlopen(base + "/slack/audit", timeout=3) as response:
            assert json.load(response) == PAYLOAD
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_preflight_checks_fixed_endpoints_and_saves_only_metadata(tmp_path):
    runtime = FakeRuntime(tmp_path)
    record = mock_state.preflight_mock_state(runtime)
    assert record["status"] == "mock_preflight_passed"
    assert record["services"] == ["slack"]
    assert record["call_counts"] == {"slack": len(PAYLOAD["calls"])}
    path = runtime.staging / "mock_preflight.json"
    saved = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "saved draft" not in saved and "reviewer" not in saved
    assert "responses" not in json.loads(saved)
    assert not (runtime.staging / "mock_state.json").exists()
    request = json.loads(runtime.calls[0][2]["input_text"])
    assert request["endpoints"] == {"slack": [9110, "/slack/audit"]}
    assert runtime.calls[0][1] == ["python3", "-c", mock_state.CAPTURE_SCRIPT]
    # The final post-solver capture remains a separate operation.
    assert mock_state.capture_mock_state(runtime).name == "mock_state.json"


@pytest.mark.parametrize("responses", [
    {}, {"slack": {"calls": "wrong type"}},
    {"slack": {"calls": []}, "unexpected": {"calls": []}}, None,
])
def test_preflight_rejects_bad_contract_without_final_snapshot(tmp_path, responses):
    runtime = FakeRuntime(tmp_path)
    runtime.exec = lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps({"responses": responses}))
    with pytest.raises(mock_state.MockPreflightError, match="solver was not started") as error:
        mock_state.preflight_mock_state(runtime)
    assert error.value.stage == "prepare/mock_preflight"
    record = json.loads((runtime.staging / "mock_preflight.json").read_text())
    assert record["status"] == "mock_preflight_failed"
    assert record["error_type"] == "ValueError"
    assert not (runtime.staging / "mock_state.json").exists()
    assert not (runtime.staging / "mock_state_error.json").exists()


def test_preflight_accepts_empty_audit_calls(tmp_path):
    runtime = FakeRuntime(tmp_path)
    runtime.exec = lambda *_args, **_kwargs: SimpleNamespace(stdout='{"responses":{"slack":{"calls":[]}}}')
    record = mock_state.preflight_mock_state(runtime)
    assert record["status"] == "mock_preflight_passed"
    assert record["call_counts"] == {"slack": 0}


def test_failed_mock_environment_stops_real_runner_before_auth_or_model(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from unittest.mock import Mock
    from experiment.scripts import run_wildclaw as runner

    manifests = tmp_path / "experiment/manifests"
    manifests.mkdir(parents=True)
    (manifests / "split.json").write_text(json.dumps({"dev_task_ids": [TASK], "test_task_ids": []}))
    source = tmp_path / "experiment/vendor/WildClawBench/tasks/synthetic.md"
    source.parent.mkdir(parents=True)
    source.write_text("Synthetic source; no benchmark content")
    (manifests / "task_manifest.json").write_text(json.dumps({"tasks": [{
        "task_id": TASK, "source_path": "tasks/synthetic.md",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]}))
    run_dir = tmp_path / "experiment/runs/dev/synthetic-preflight"
    runtime = FakeRuntime(run_dir)
    runtime.fail_capture = True
    runtime.cleanup = Mock()
    backend = SimpleNamespace(prepare=Mock(), preflight=Mock(), run=Mock())
    auth_copy = Mock()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_task_spec", lambda *_args, **_kwargs: SimpleNamespace(task_id=TASK))
    monkeypatch.setattr(runner, "reviewed_warmup", lambda _task: "")
    monkeypatch.setattr(runner, "make_runtime_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "TaskRuntime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(runner, "CodexBackend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(runner, "copy_runtime_auth", auth_copy)
    monkeypatch.setattr(runner, "inference_client", lambda *_args, **_kwargs: nullcontext(
        SimpleNamespace(api_key="synthetic-client", authorization_sha256="0" * 64)))
    with pytest.raises(mock_state.MockPreflightError):
        runner.run_one(TASK, "A0", "dev", 1, "synthetic-preflight")
    backend.prepare.assert_called_once_with(runtime)
    auth_copy.assert_not_called()
    backend.preflight.assert_not_called()
    backend.run.assert_not_called()
    runtime.cleanup.assert_called_once_with()
    record = json.loads((run_dir / "result.json").read_text())
    assert record["status"] == "infrastructure_error"
    assert record["mock_preflight"]["status"] == "mock_preflight_failed"
    assert record["error"]["type"] == "MockPreflightError"
    assert "execution" not in record
    assert not (runtime.staging / "mock_state.json").exists()
