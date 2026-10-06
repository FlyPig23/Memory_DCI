"""Local-only contract tests; never call a model or the benchmark grader."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from experiment.shared.memory.tools import Tools, TOOLS, audit_memory, initialize_memory
from experiment.shared.memory import mcp_server
from experiment.shared.memory.mcp_server import rpc


@pytest.fixture
def runtime(tmp_path):
    pool = tmp_path / "pool"
    (pool / "tasks").mkdir(parents=True)
    for task in ("build-alpha", "build-beta"):
        trajectory = pool / "trajectories" / task / "model-a"
        trajectory.mkdir(parents=True)
        (trajectory / f"{task}_0.7500.md").write_text(
            "task: example\nACTION: use pdftotext\nOBSERVATION: readable output\n")
        (pool / "tasks" / f"{task}.md").write_text(f"{task}\nextract PDF and validate output\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "solver.txt").write_text("command: pdftotext input.pdf output.txt\nverified output has expected text\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Tools(pool, tmp_path / "memory", "test-gamma", evidence_root=evidence, workspace_root=workspace)
    return store


def result(store, name, arguments):
    response = store.call(name, arguments)
    assert not response["isError"], response
    return json.loads(response["content"][0]["text"])


def entry():
    return {"slug": "extract-pdf", "title": "Extract PDF text", "keywords": ["pdf", "pdftotext"],
            "applicability": "A PDF needs searchable text and pdftotext is available.",
            "procedure": ["Extract text.", "Inspect the resulting text before relying on it."],
            "pitfalls": ["Image-only PDFs can produce empty text."],
            "verification": "Check text length and compare a representative page.",
            "status": "source_observed", "sources": [{"path": "/pool/trajectories/build-alpha/model-a/build-alpha_0.7500.md",
                                                         "line_start": 2, "line_end": 3}],
            "evidence_summary": "The historical command produced readable output; not yet verified locally."}


def write(store, item=None, stage="online"):
    return result(store, "distill", {"decision": "write", "stage": stage,
                                    "reason": "Reusable validated extraction strategy.", "entries": [item or entry()]})


def test_initialization_is_empty_and_task_local(runtime):
    audit = audit_memory(runtime.memory)
    assert audit["valid"] and audit["entry_count"] == 0
    assert not audit["has_explicit_final_decision"]
    assert (runtime.memory / "MEMORY.md").is_file()
    assert (runtime.memory / "memory_summary.md").is_file()
    with pytest.raises(ValueError, match="cross-task"):
        initialize_memory(runtime.memory, "other-test")
    other = runtime.memory.parent / "other-memory"
    initialize_memory(other, "other-test")
    write(runtime)
    assert audit_memory(other)["entry_count"] == 0


def test_named_tools_and_strict_schema(runtime):
    assert [tool["name"] for tool in TOOLS] == ["reason", "DCI_search_task", "DCI_search_trajectory", "DCI_search_memory", "distill"]
    assert runtime.call("reason", {"goal": "x", "blocker": "", "next_action": "search", "chain_of_thought": "x"})["isError"]
    assert runtime.call("distill", {"decision": "skip", "stage": "online", "reason": "x"})["isError"]
    assert runtime.call("DCI_search_task", {"query": "x", "command": "pwd", "timeout_seconds": True})["isError"]
    assert runtime.call("DCI_search_task", {"query": "x", "command": "pwd", "timeout_seconds": 31})["isError"]
    result(runtime, "reason", {"goal": "extract PDF", "blocker": "", "next_action": "search task descriptions"})
    assert audit_memory(runtime.memory)["event_counts"]["reason"] == 1


def test_flexible_shell_and_selected_trajectory_scope(runtime):
    found = result(runtime, "DCI_search_task", {"query": "PDF extraction", "command": "rg -n -i 'PDF|text' . | head -2"})
    assert "build-" in found["output"]
    found = result(runtime, "DCI_search_trajectory", {"query": "observed pdftotext output", "command": "rg -n -C 1 'pdftotext' .",
                                                     "selected_task_ids": ["build-alpha"]})
    assert found["cwd"].endswith("trajectories/build-alpha")
    assert "build-alpha_0.7500.md" in found["output"]
    assert "OBSERVATION" in found["output"]
    selected = result(runtime, "DCI_search_trajectory", {"query": "compare", "command": "python3 -c 'import os; print(os.environ[\"DCI_SELECTED_DIRS\"])'",
                                                         "selected_task_ids": ["build-alpha", "build-beta"]})
    assert "build-alpha" in selected["output"] and "build-beta" in selected["output"]
    assert runtime.call("DCI_search_trajectory", {"query": "x", "command": "pwd", "selected_task_ids": ["test-gamma"]})["isError"]
    assert runtime.call("DCI_search_trajectory", {"query": "x", "command": "pwd", "selected_task_ids": ["../tasks"]})["isError"]


def test_write_update_and_synced_navigation(runtime):
    first = write(runtime)
    assert first["entry_paths"] == ["entries/extract-pdf.md"]
    assert audit_memory(runtime.memory)["entry_count"] == 1
    for file in ("MEMORY.md", "memory_summary.md", "entries/extract-pdf.md"):
        assert "pdftotext" in (runtime.memory / file).read_text()
    item = entry()
    item["status"] = "locally_verified"
    item["evidence_summary"] = "Current-task execution produced expected text."
    item["sources"].append({"path": "/evidence/solver.txt", "line_start": 1, "line_end": 2})
    write(runtime, item)
    assert audit_memory(runtime.memory)["entry_count"] == 1
    assert "locally_verified" in (runtime.memory / "memory_summary.md").read_text()
    found = result(runtime, "DCI_search_memory", {"query": "pdf", "command": "rg -n 'locally_verified' entries MEMORY.md"})
    assert "locally_verified" in found["output"]


@pytest.mark.parametrize("patch", [
    {"status": "successful"},
    {"status": "locally_verified"},
    {"slug": "../escape"},
    {"sources": [{"path": "/pool/missing.txt", "line_start": 1, "line_end": 2}]},
    {"sources": [{"path": "/pool/../outside.txt", "line_start": 1, "line_end": 2}]},
    {"sources": [{"path": "/etc/passwd", "line_start": 1, "line_end": 2}]},
    {"sources": [{"path": "/evidence/solver.txt", "line_start": 3, "line_end": 2}]},
    {"sources": [{"path": "/evidence/solver.txt", "line_start": 1, "line_end": 3}]},
])
def test_invalid_entry_does_not_write(runtime, patch):
    item = entry() | patch
    response = runtime.call("distill", {"decision": "write", "stage": "online", "reason": "x", "entries": [item]})
    assert response["isError"]
    assert audit_memory(runtime.memory)["entry_count"] == 0


def test_reject_managed_write_symlink(runtime):
    outside = runtime.memory.parent / "outside.md"
    outside.write_text("do not overwrite")
    (runtime.memory / "entries" / "extract-pdf.md").symlink_to(outside)
    response = runtime.call("distill", {"decision": "write", "stage": "online", "reason": "x", "entries": [entry()]})
    assert response["isError"]
    assert outside.read_text() == "do not overwrite"
    assert not audit_memory(runtime.memory)["valid"]


def test_source_symlink_cannot_escape_allowed_roots(runtime):
    outside = runtime.memory.parent / "outside.txt"
    outside.write_text("not source\n")
    (runtime.pool / "link.txt").symlink_to(outside)
    item = entry() | {"sources": [{"path": "/pool/link.txt", "line_start": 1, "line_end": 1}]}
    assert runtime.call("distill", {"decision": "write", "stage": "online", "reason": "x", "entries": [item]})["isError"]


def test_explicit_no_update_and_final_review(runtime):
    result(runtime, "distill", {"decision": "no_update", "stage": "online", "reason": "No useful lesson yet."})
    assert not audit_memory(runtime.memory)["has_explicit_final_decision"]
    solver_final = result(runtime, "distill", {"decision": "no_update", "stage": "final", "reason": "Nothing to retain from solving."})
    assert solver_final["agent_stage"] == "solve"
    reviewer = Tools(runtime.pool, runtime.memory, runtime.task_id, "review")
    final = result(reviewer, "distill", {"decision": "no_update", "stage": "final", "reason": "No evidence-backed addition beyond existing entries."})
    audit = audit_memory(runtime.memory)
    assert audit["has_explicit_final_decision"]
    assert audit["final_decision"]["event_id"] == final["event_id"]
    assert audit["final_decision"]["decision"] == "no_update"
    assert audit["final_decision"]["agent_stage"] == "review"
    assert reviewer.call("distill", {"decision": "no_update", "stage": "online", "reason": "x"})["isError"]
    assert audit["entry_count"] == 0
    assert runtime.call("distill", {"decision": "no_update", "stage": "online", "reason": "x", "entries": [entry()]})["isError"]


def test_timeout_and_output_clipping(runtime):
    timed = result(runtime, "DCI_search_task", {"query": "timeout test", "command": "sleep 2", "timeout_seconds": .1})
    assert timed["timed_out"] and timed["elapsed_seconds"] < 1.5
    out = result(runtime, "DCI_search_task", {"query": "large output", "command": "python3 -c 'print(\"A\" * 50000)'"})
    assert out["output_truncated"] and len(out["output"]) == 24000
    assert Path(out["captured_output_path"]).stat().st_size > 24000


def test_rpc_and_standalone_stdio(runtime):
    assert rpc(runtime, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    initialized = rpc(runtime, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert initialized["result"]["capabilities"]["tools"] == {"listChanged": False}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "reason", "arguments": {"goal": "x", "blocker": "", "next_action": "search"}}},
    ]
    process = subprocess.run([sys.executable, str(Path(mcp_server.__file__)),
                              "--pool", str(runtime.pool), "--memory", str(runtime.memory),
                              "--task-id", runtime.task_id, "--stage", "solve"],
                             input="\n".join(json.dumps(r) for r in requests) + "\n",
                             text=True, capture_output=True, check=True, timeout=5)
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert len(responses[1]["result"]["tools"]) == 5
    assert not responses[2]["result"]["isError"]
    assert not process.stderr


def test_audit_rejects_fabricated_final_decision_and_unindexed_entries(runtime):
    (runtime.memory / "final_decision.json").write_text(json.dumps({"task_id": runtime.task_id,
        "decision": "no_update", "stage": "final", "reason": "invented", "event_id": "none"}))
    assert not audit_memory(runtime.memory)["valid"]
    (runtime.memory / "final_decision.json").unlink()
    (runtime.memory / "entries" / "untracked.md").write_text("untracked")
    assert not audit_memory(runtime.memory)["valid"]


def test_audit_detects_unsynchronized_direct_edit(runtime):
    write(runtime)
    assert audit_memory(runtime.memory)["valid"]
    (runtime.memory / "entries" / "extract-pdf.md").write_text("changed outside distill")
    assert not audit_memory(runtime.memory)["valid"]
