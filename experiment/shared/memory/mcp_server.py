#!/usr/bin/env python3
"""Dependency-free MCP stdio server for the V5 per-task container."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if len(Path(__file__).resolve().parents) > 3:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

try:
    from experiment.shared.memory.tools import Tools, TOOLS
except ImportError:
    from tools import Tools, TOOLS

MAX_REQUEST = 1024 * 1024


def rpc(store: Tools, request):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if "id" not in request:
        return None
    identity = request["id"]
    params = request.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32602, "message": "Invalid params"}}
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "wildclaw-v5-dci-memory", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = store.call(params.get("name"), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": identity, "result": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", required=True)
    parser.add_argument("--memory", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--stage", choices=["solve", "review"], default="solve")
    args = parser.parse_args()
    store = Tools(args.pool, args.memory, args.task_id, args.stage)
    while line := sys.stdin.buffer.readline(MAX_REQUEST + 1):
        if len(line) > MAX_REQUEST:
            # Exit rather than dispatch a truncated request or misframe its suffix.
            print("MCP request exceeds byte limit", file=sys.stderr)
            return 2
        try:
            request = json.loads(line)
            response = rpc(store, request)
        except (ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
