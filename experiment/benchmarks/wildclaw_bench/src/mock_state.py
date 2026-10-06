"""Controller-owned mock-service audit capture and immutable verifier replay.

Capture after the main solver stops producing work and immediately before the
whole-container stop. Restore only after the inert container restarts for
verification. Neither hook reloads fixtures or executes a task-provided script.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

ENDPOINTS = {
    "gmail": (9100, "/gmail/audit"),
    "calendar": (9101, "/calendar/audit"),
    "contacts": (9103, "/contacts/audit"),
    "slack": (9110, "/slack/audit"),
}
TASK_SERVICES = {1: ("gmail", "calendar"), 2: ("slack",), 3: ("slack",),
                 4: ("slack",), 5: ("slack", "contacts"), 6: ("slack", "contacts")}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def services_for_task(task_id: str) -> tuple[str, ...]:
    match = re.fullmatch(r"03_Social_Interaction_task_(\d+)_[a-z0-9_]+", task_id)
    if not match:
        return ()
    number = int(match.group(1))
    if number not in TASK_SERVICES:
        raise ValueError("Unknown Social task service contract")
    return TASK_SERVICES[number]


def checked_path(path: Path, root: Path) -> Path:
    result = path.resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Mock-state file is outside the private runtime directory")
    return result


# This fixed script bypasses proxies and refuses redirects. Endpoint selection is
# controlled by this module's allowlist, never by solver arguments or workspace.
CAPTURE_SCRIPT = r'''
import json,sys,time,urllib.request
spec=json.loads(sys.stdin.read())
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
responses={}
for name,endpoint in spec['endpoints'].items():
    port,path=endpoint
    request=urllib.request.Request('http://127.0.0.1:'+str(port)+path,method='GET')
    with opener.open(request,timeout=10) as stream:
        raw=stream.read(spec['max_bytes']+1)
    if len(raw)>spec['max_bytes']: raise ValueError('audit response exceeded fixed size limit')
    value=json.loads(raw)
    if not isinstance(value,dict) or not isinstance(value.get('calls'),list):
        raise ValueError('invalid audit response shape')
    responses[name]=value
print(json.dumps({'responses':responses,'captured_at_unix':time.time()}))
'''



class MockPreflightError(RuntimeError):
    """A required mock contract failed before credentials or solver startup."""
    def __init__(self, record: dict[str, Any]):
        self.record = record
        self.stage = "prepare/mock_preflight"
        super().__init__("prepare/mock_preflight: Required Social mock service is unavailable or invalid; solver was not started")


def preflight_mock_state(runtime) -> dict[str, Any]:
    """Read fixed audit endpoints once before solver authentication/startup.

    Persist metadata only. The final solver audit snapshot is still captured by
    capture_mock_state after solving; this check cannot create or replace it.
    """
    services = services_for_task(runtime.task.task_id)
    if not services:
        return {"status": "not_required", "services": []}
    if runtime.state != "prepared":
        raise RuntimeError("Mock preflight requires the prepared, pre-solver runtime phase")
    staging = checked_path(runtime.staging, runtime.run_dir)
    staging.mkdir(parents=True, exist_ok=True)
    destination = staging / "mock_preflight.json"
    if os.path.lexists(destination):
        raise FileExistsError("A mock preflight has already been recorded for this run")
    record = {"schema_version": 1, "stage": "prepare/mock_preflight",
              "task_id": runtime.task.task_id, "container": runtime.name,
              "services": list(services), "attempts": 1, "checked_at_unix": time.time()}
    started = time.monotonic()
    failure = None
    try:
        spec = {"endpoints": {name: ENDPOINTS[name] for name in services}, "max_bytes": MAX_RESPONSE_BYTES}
        result = runtime.exec(["python3", "-c", CAPTURE_SCRIPT], input_text=json.dumps(spec),
                              timeout=15 * len(services), check=True)
        document = json.loads(result.stdout)
        responses = document.get("responses") if isinstance(document, dict) else None
        if not isinstance(responses, dict) or set(responses) != set(services):
            raise ValueError("Mock preflight service set differs from the required contract")
        for value in responses.values():
            if not isinstance(value, dict) or not isinstance(value.get("calls"), list):
                raise ValueError("Mock preflight requires an audit object with calls:list")
        record.update(status="mock_preflight_passed",
                      call_counts={name: len(responses[name]["calls"]) for name in services})
    except Exception as error:
        failure = error
        record.update(status="mock_preflight_failed", error_type=type(error).__name__)
    record["elapsed_seconds"] = time.monotonic() - started
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(record) + b"\n")
    if failure is not None:
        raise MockPreflightError(record) from failure
    return record


def validate_snapshot(snapshot: Mapping[str, Any], task_id: str | None = None) -> None:
    if snapshot.get("schema_version") != 1:
        raise ValueError("Unsupported mock snapshot schema")
    if task_id is not None and snapshot.get("task_id") != task_id:
        raise ValueError("Snapshot belongs to another task")
    services = services_for_task(str(snapshot.get("task_id", "")))
    if not services or set(snapshot.get("responses", {})) != set(services):
        raise ValueError("Snapshot service set does not match the public task contract")
    for name in services:
        value = snapshot["responses"][name]
        if not isinstance(value, dict) or not isinstance(value.get("calls"), list):
            raise ValueError("Invalid audit payload")
        expected = snapshot.get("response_sha256", {}).get(name)
        if hashlib.sha256(canonical(value)).hexdigest() != expected:
            raise ValueError("Audit payload hash mismatch")


def capture_mock_state(runtime) -> Path | None:
    """Capture fixed /audit GET responses into controller-only storage.

    The caller must still freeze the container in a finally block if this raises.
    Do not run this as a tool available to the solver, or defer it until restart.
    """
    services = services_for_task(runtime.task.task_id)
    if not services:
        return None
    if runtime.state != "prepared":
        raise RuntimeError("Capture audit while the original mock services are alive")
    staging = checked_path(runtime.staging, runtime.run_dir)
    staging.mkdir(parents=True, exist_ok=True)
    destination = staging / "mock_state.json"
    if destination.exists():
        raise FileExistsError("An audit snapshot has already been frozen for this run")
    spec = {"endpoints": {name: ENDPOINTS[name] for name in services}, "max_bytes": MAX_RESPONSE_BYTES}
    try:
        result = runtime.exec(["python3", "-c", CAPTURE_SCRIPT], input_text=json.dumps(spec),
                              timeout=15 * len(services), check=True)
        captured = json.loads(result.stdout)
        responses = captured["responses"]
        snapshot = {"schema_version": 1, "task_id": runtime.task.task_id,
                    "container": runtime.name, "captured_at_unix": captured["captured_at_unix"],
                    "responses": responses,
                    "response_sha256": {name: hashlib.sha256(canonical(value)).hexdigest() for name, value in responses.items()},
                    "capture_policy": "Controller GET before whole-container stop; no fixture reload or service reset."}
        validate_snapshot(snapshot, runtime.task.task_id)
    except Exception as error:
        failure = staging / "mock_state_error.json"
        fd = os.open(failure, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"status": "mock_audit_capture_failed", "task_id": runtime.task.task_id,
                       "services": list(services), "error_type": type(error).__name__}, stream)
        raise RuntimeError("Mock audit capture failed; grading must not silently use fallback") from error
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(snapshot) + b"\n")
    return destination


def replay_handler(payload: dict[str, Any], expected_path: str):
    response = canonical(payload)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlsplit(self.path).path != expected_path:
                self.send_error(404, "Only the frozen audit endpoint is available")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_POST(self):
            self.send_error(405, "Verifier replay is immutable; task actions/reset are disabled")

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        def log_message(self, *_args):
            pass

    return Handler


def serve_snapshot(snapshot_path: Path) -> None:
    snapshot = json.loads(snapshot_path.read_text())
    validate_snapshot(snapshot)
    servers = []
    try:
        for name in services_for_task(snapshot["task_id"]):
            port, path = ENDPOINTS[name]
            server = ThreadingHTTPServer(("127.0.0.1", port), replay_handler(snapshot["responses"][name], path))
            servers.append(server)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        while True:
            time.sleep(60)
    finally:
        for server in servers:
            server.server_close()


VERIFY_REPLAY_SCRIPT = r'''
import hashlib,json,sys,time,urllib.request
spec=json.loads(sys.stdin.read())
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for name,endpoint in spec['endpoints'].items():
    port,path=endpoint
    for attempt in range(50):
        try:
            with opener.open('http://127.0.0.1:'+str(port)+path,timeout=2) as stream:
                value=json.loads(stream.read())
            raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            if hashlib.sha256(raw).hexdigest()!=spec['hashes'][name]:
                raise ValueError('replay hash mismatch')
            break
        except Exception:
            if attempt==49: raise
            time.sleep(0.1)
print(json.dumps({'status':'mock_audit_replayed','services':sorted(spec['endpoints'])}))
'''


def restore_mock_state(runtime, snapshot_path: Path | None) -> dict[str, Any]:
    """Call after grade's inert restart, before invoking any official grader."""
    services = services_for_task(runtime.task.task_id)
    if not services:
        return {"status": "not_required", "services": []}
    if runtime.state != "grading":
        raise RuntimeError("Restore only in the stopped-solver verifier phase")
    if snapshot_path is None:
        raise ValueError("A Social task requires a captured audit snapshot")
    snapshot_path = checked_path(snapshot_path, runtime.staging)
    snapshot = json.loads(snapshot_path.read_text())
    validate_snapshot(snapshot, runtime.task.task_id)
    if snapshot.get("container") != runtime.name:
        raise ValueError("Snapshot belongs to another run")
    source = checked_path(Path(__file__), runtime.config.project_root)
    runtime.copy_file_to_container(source, "/tmp/_mock_state_replay.py")
    runtime.copy_file_to_container(snapshot_path, "/tmp/_mock_state_snapshot.json")
    runtime.exec(["/bin/sh", "-c", "nohup python3 /tmp/_mock_state_replay.py --serve /tmp/_mock_state_snapshot.json > /tmp/_mock_state_replay.log 2>&1 < /dev/null &"])
    spec = {"endpoints": {name: ENDPOINTS[name] for name in services}, "hashes": snapshot["response_sha256"]}
    result = runtime.exec(["python3", "-c", VERIFY_REPLAY_SCRIPT], input_text=json.dumps(spec),
                          timeout=120, check=True)
    record = json.loads(result.stdout)
    record["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    record["task_id"] = runtime.task.task_id
    path = checked_path(runtime.staging / "mock_state_replay.json", runtime.run_dir)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(record) + b"\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path, required=True)
    serve_snapshot(parser.parse_args().serve)
