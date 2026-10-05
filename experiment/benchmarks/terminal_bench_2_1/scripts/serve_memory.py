#!/usr/bin/env python3
"""Terminal-Bench adapter for the existing five V5 MCP tools.

No Harbor/third-party dependencies: this runs in the separately mounted Python.
The pool and task memory are isolated by the container, not by shell filtering.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def load_tools(path: Path):
    spec = importlib.util.spec_from_file_location("v5_shared_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_store(library, pool, memory, task_id, stage, workspace, evidence="/evidence"):
    # Copy descriptors so importing the historical module never edits its files.
    descriptors = copy.deepcopy(library.TOOLS)
    build_count = len([p for p in (Path(pool) / "trajectories").iterdir() if p.is_dir()])
    for tool in descriptors:
        if tool["name"] == "DCI_search_trajectory":
            tool["inputSchema"]["properties"]["selected_task_ids"]["maxItems"] = max(1, build_count)
    library.TOOLS = descriptors
    class TerminalTools(library.Tools):
        def _source(self, source):
            given = Path(source["path"])
            path = given
            for alias, root in self.source_roots.items():
                if given.is_relative_to(alias):
                    path = root / given.relative_to(alias)
                    break
            # Check both the spelling and final target: a workspace symlink
            # must not turn a private credential into admissible evidence.
            for candidate in (given, path.resolve()):
                if any(part in {".codex", ".ssh", ".aws", "auth.json"} for part in candidate.parts) or any(
                        candidate == Path(prefix) or Path(prefix) in candidate.parents
                        for prefix in ("/opt/v5-home", "/opt/v5-control", "/tests", "/logs/verifier")):
                    raise ValueError("Private runtime and grading files are not memory evidence")
            return super()._source(source)

    store = TerminalTools(pool, memory, task_id, stage, evidence_root=evidence,
                          workspace_root=workspace)
    # Actual task directories are not always /tmp_workspace. Preserve pool
    # precedence when the official task's working directory happens to be '/'.
    store.source_roots = {"/pool": Path(pool).resolve(),
                          "/evidence": Path(evidence).resolve()}
    if stage == "solve":
        store.source_roots[str(Path(workspace))] = Path(workspace).resolve()
    return store, descriptors


def rpc(store, descriptors, request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in request:
        return None
    identity, method = request["id"], request.get("method")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32602, "message": "Invalid params"}}
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "terminal-bench-v5-memory", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": descriptors}
    elif method == "tools/call":
        result = store.call(params.get("name"), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": identity, "result": result}


def processes():
    """PID and kernel start tick avoid confusing PID reuse with a startup service."""
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            result[int(entry.name)] = {"parent": int(fields[1]), "start": int(fields[19])}
        except (OSError, ValueError, IndexError):
            pass
    return result


def freeze(baseline: Path):
    """End identified harness processes, preserving all task-created services.

    A newly started server may be the solution itself. Process age is therefore
    never a reason to kill it. The baseline parameter is retained for the small
    command interface; only exact pinned runtime commands are selected here.
    """
    killed = set()
    for _ in range(4):
        current = processes()
        protected = {1}
        cursor = os.getpid()
        while cursor and cursor not in protected:
            protected.add(cursor)
            cursor = current.get(cursor, {}).get("parent", 0)
        targets = []
        for pid in current:
            if pid in protected:
                continue
            try:
                args = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                argv = [x.decode(errors="replace") for x in args if x]
            except OSError:
                continue
            if argv and (argv[0] in {"/opt/codex/codex", "/opt/codex/codex-code-mode-host"}
                    or len(argv) > 2 and argv[1:3] == ["/opt/v5/serve_memory.py", "mcp"]):
                targets.append(pid)
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
    return {"stopped_runtime_process_count": len(killed), "preserved_initial_services": True,
            "preserved_task_created_services": True}


def supervise(args):
    """End only the CLI controller; task services must survive for grading."""
    with open(args.prompt, "rb") as prompt, open(args.events, "wb") as events, open(args.stderr, "wb") as errors:
        proc = subprocess.Popen(args.command, stdin=prompt, stdout=events, stderr=errors,
                                start_new_session=True)
        try:
            return proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return 124
        finally:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    server = sub.add_parser("mcp")
    for name in ("pool", "memory", "task-id", "workspace"):
        server.add_argument("--" + name, required=True)
    server.add_argument("--stage", choices=["solve", "review"], default="solve")
    server.add_argument("--library", default="/opt/v5/tools.py")
    server.add_argument("--evidence", default="/evidence")
    base = sub.add_parser("baseline")
    base.add_argument("path")
    stop = sub.add_parser("freeze")
    stop.add_argument("path")
    run = sub.add_parser("supervise")
    for name in ("prompt", "events", "stderr"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--timeout", type=float, required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == "baseline":
        Path(args.path).write_text(json.dumps(processes()))
        return 0
    if args.mode == "freeze":
        print(json.dumps(freeze(Path(args.path))))
        return 0
    if args.mode == "supervise":
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]
        return supervise(args)
    store, descriptors = make_store(load_tools(Path(args.library)), args.pool, args.memory,
                                    args.task_id, args.stage, args.workspace, args.evidence)
    while line := sys.stdin.buffer.readline(1024 * 1024 + 1):
        if len(line) > 1024 * 1024:
            return 2
        try:
            response = rpc(store, descriptors, json.loads(line))
        except (ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
