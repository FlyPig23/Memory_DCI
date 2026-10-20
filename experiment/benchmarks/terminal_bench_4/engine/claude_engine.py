"""Claude Code CLI as solver and reviewer of the V8 workflow.

Same prompts, five MCP tools, mounts, deadlines and evidence boundaries as the Codex path in
harbor_runtime.py; only the CLI differs. A pinned native Claude Code binary is mounted read-only, like the
pinned Codex binary. No credential enters a container: the CLI talks to http://127.0.0.1:RELAY_PORT, which
relay_exec.py forwards to a mounted unix socket; the operator's relay behind that socket adds the login.

Host settings (environment):
  V8_CLAUDE_BINARY        native linux Claude Code executable, version CLI_VERSION
  V8_CLAUDE_RELAY_SOCKET  unix socket that accepts the Anthropic API protocol
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

CLI_VERSION = "2.1.292"
BINARY = "/opt/claude/claude"
RELAY_EXEC = "/opt/v5/relay_exec.py"
RELAY_SOCKET = "/opt/v5-relay/relay.sock"
RELAY_PORT = 18191
EVENTS, STDERR = "claude.events.jsonl", "claude.stderr.log"
# Parity with the Codex settings: no server-side web search, no sub-agents. WebFetch summarises pages with
# another model, so it is off as well; curl and friends in the shell stay available, as for Codex.
DISALLOWED_TOOLS = ("WebSearch", "WebFetch", "Task")


def host_file(variable):
    path = Path(os.environ.get(variable, "")).expanduser()
    if not os.environ.get(variable) or not path.exists():
        raise FileNotFoundError(f"{variable} must name an existing path for the Claude engine")
    return path.resolve()


def mounts(bind):
    return [bind(host_file("V8_CLAUDE_BINARY"), BINARY), bind(host_file("V8_CLAUDE_RELAY_SOCKET"), RELAY_SOCKET, False),
            bind(Path(__file__).with_name("relay_exec.py"), RELAY_EXEC)]


def mcp_config(name, python, arguments):
    return json.dumps({"mcpServers": {name: {"command": python, "args": list(arguments)}}}, indent=2) + "\n"


def command(python, home, model, effort):
    """The prompt arrives on stdin and the working directory is the caller's cwd."""
    return [python, RELAY_EXEC, str(RELAY_PORT), RELAY_SOCKET, BINARY, "-p", "--model", model, "--effort", effort,
            "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
            "--mcp-config", home + "/mcp.json", "--strict-mcp-config", "--disallowedTools", *DISALLOWED_TOOLS]


def environment(home):
    # CLAUDE_CONFIG_DIR, not HOME: the task's own HOME must stay what the official image set.
    # IS_SANDBOX lets the CLI skip permission prompts as root, as Harbor's own Claude Code agent does.
    return {"CLAUDE_CONFIG_DIR": home, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{RELAY_PORT}",
            "ANTHROPIC_AUTH_TOKEN": "relay", "IS_SANDBOX": "1", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
            "MCP_TIMEOUT": "30000", "MCP_TOOL_TIMEOUT": "60000"}


def same_model(observed, requested):
    """The CLI may append a snapshot date or a context-window tag; a different model never matches."""
    return isinstance(observed, str) and re.fullmatch(re.escape(requested) + r"(-\d{8})?(\[[^\]]*\])?", observed) is not None


def _events(path):
    if not Path(path).is_file():
        return
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def convert_transcript(source, destination):
    """Stream-json assistant/user events already carry text, thinking, tool_use and tool_result blocks."""
    count = 0
    with Path(destination).open("w", encoding="utf-8") as stream:
        for event in _events(source):
            message = event.get("message")
            if event.get("type") not in ("assistant", "user") or not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}] if content else []
            if content:
                stream.write(json.dumps({"type": "message", "message": {
                    "role": message.get("role", event["type"]), "content": content}}, ensure_ascii=False) + "\n")
                count += 1
    return count


def capture_session(logs, model, effort, tools):
    """Usage and model audit from the CLI's own event stream; writes usage.json, model_audit.json and the
    review transcript. input_tokens includes cached tokens, as in the Codex usage records."""
    from .harbor_runtime import write_json
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    source = logs / EVENTS
    init_model, result, fallbacks, turns = None, None, [], 0
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0}
    seen = set()
    for event in _events(source):
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init_model = event.get("model")
        elif kind == "system" and event.get("subtype") == "model_refusal_fallback":
            fallbacks.append({k: event.get(k) for k in ("original_model", "fallback_model", "api_refusal_category")})
        elif kind == "result":
            result = event
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            usage = message.get("usage")
            # One API response is streamed as several events with the same id and the same usage.
            if isinstance(usage, dict) and message.get("id") not in seen:
                seen.add(message.get("id"))
                turns += 1
                read, write = usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0
                totals["input_tokens"] += (usage.get("input_tokens") or 0) + read + write
                totals["cached_input_tokens"] += read
                totals["cache_write_input_tokens"] += write
                totals["output_tokens"] += usage.get("output_tokens") or 0
    model_usage = (result or {}).get("modelUsage") or {}
    if model_usage:  # the CLI's final ledger is authoritative when the run reached its end
        totals = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0}
        for row in model_usage.values():
            read, write = row.get("cacheReadInputTokens") or 0, row.get("cacheCreationInputTokens") or 0
            totals["input_tokens"] += (row.get("inputTokens") or 0) + read + write
            totals["cached_input_tokens"] += read
            totals["cache_write_input_tokens"] += write
            totals["output_tokens"] += row.get("outputTokens") or 0
    available = bool(model_usage) or turns > 0
    usage = {**({k: v for k, v in totals.items()} if available else {k: None for k in totals}),
             "total_tokens": totals["input_tokens"] + totals["output_tokens"] if available else None,
             "usage_available": available, "usage_events": turns, "request_count": turns or None,
             "usage_source": "result.modelUsage" if model_usage else "assistant events (run did not reach its result)",
             "total_cost_usd": (result or {}).get("total_cost_usd"), "num_turns": (result or {}).get("num_turns"),
             "model_usage": model_usage}
    # Any other model that produced output (a refusal fallback, a helper model) fails the audit.
    stray = [m for m in model_usage if not same_model(m, model) and (model_usage[m] or {}).get("outputTokens")]
    verified = same_model(init_model, model) and not fallbacks and not stray and available
    observed = {*model_usage, init_model, *(f.get("fallback_model") for f in fallbacks)} - {None}
    audit = {"models": [model] if verified else sorted(observed),
             "efforts": [effort],
             "effort_source": "--effort flag on the recorded command line (the CLI does not echo it)",
             "model_effort_verified": verified, "init_model": init_model, "models_used": sorted(model_usage),
             "other_models_with_output": stray, "refusal_fallbacks": fallbacks,
             "result_subtype": (result or {}).get("subtype"), "is_error": (result or {}).get("is_error"),
             "cli": "claude-code " + CLI_VERSION, "initial_distilled_skills": 0, "global_memory": False, "tools": tools}
    write_json(logs / "usage.json", usage)
    write_json(logs / "model_audit.json", audit)
    convert_transcript(source, logs / "transcript.openclaw.jsonl")
    final = (result or {}).get("result")
    if isinstance(final, str):
        (logs / "final.txt").write_text(final)
    return usage, audit
