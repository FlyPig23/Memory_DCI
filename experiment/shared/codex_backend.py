"""Codex CLI backend using the caller's isolated ChatGPT login.

This adapter never opens an auth file or turns OAuth credentials into API keys.
The CLI runs *inside* TaskRuntime's container, not in the controller workspace.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .task_runtime import CODEX_HOME, WORKSPACE, TaskRuntime, TaskSpec, contained
except ImportError:
    from task_runtime import CODEX_HOME, WORKSPACE, TaskRuntime, TaskSpec, contained


@dataclass(frozen=True)
class MCPServer:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CodexSettings:
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "medium"
    cli_version: str = "0.153.4"
    binary: str = "/opt/codex/codex"
    mcp_servers: tuple[MCPServer, ...] = ()
    # Browser/exec tools still work through the runtime's controlled network.
    # Server-side web search could bypass that egress policy.
    native_web_search: str = "disabled"
    input_images: tuple[str, ...] = ()
    no_tools: bool = False
    inference_only: bool = False


def render_config(settings: CodexSettings) -> str:
    if settings.native_web_search not in ("disabled", "cached", "live"):
        raise ValueError("Unsupported web_search setting")
    if settings.no_tools and settings.mcp_servers:
        raise ValueError("Inference-only configuration cannot expose MCP tools")
    q = json.dumps
    lines = [
        'model_provider = "openai"', f"model = {q(settings.model)}",
        f"model_reasoning_effort = {q(settings.reasoning_effort)}",
        'cli_auth_credentials_store = "file"', 'approval_policy = "never"',
        f'sandbox_mode = {q("read-only" if settings.no_tools else "danger-full-access")}', f"web_search = {q(settings.native_web_search)}",
        '[features]', 'multi_agent = false', 'apps = false', 'plugins = false',
        'remote_plugin = false', 'memories = false',
        f'image_generation = {str(not settings.no_tools).lower()}',
    ]
    if settings.no_tools:
        # These names were verified with the pinned 0.153.4 features list.
        for feature in ("shell_tool", "unified_exec", "shell_snapshot", "browser_use", "browser_use_external",
                        "browser_use_full_cdp_access", "computer_use",
                        "view_image", "workspace_dependencies", "code_mode", "code_mode_host",
                        "sleep_tool", "goals", "skill_search", "skill_mcp_dependency_install",
                        "hooks", "tool_suggest"):
            lines.append(f"{feature} = false")
    names: set[str] = set()
    for server in settings.mcp_servers:
        if not server.name or server.name in names:
            raise ValueError("MCP server names must be unique")
        names.add(server.name)
        lines += [f"\n[mcp_servers.{q(server.name)}]", f"command = {q(server.command)}",
                  f"args = {q(list(server.args))}", 'startup_timeout_sec = 30', 'tool_timeout_sec = 60']
        if server.env:
            lines += [f"[mcp_servers.{q(server.name)}.env]"]
            lines += [f"{q(str(key))} = {q(str(value))}" for key, value in server.env.items()]
    return "\n".join(lines) + "\n"


def build_prompt(task: TaskSpec, *, condition_instructions: str = "") -> str:
    # Only legitimate task skills and the parsed Prompt are rendered. The raw
    # Markdown, Automated Checks, gt and archived trajectories are never read.
    parts = [
        f"You are solving one task in an isolated non-interactive environment. Work in {WORKSPACE}. "
        f"Complete the requested deliverables within {task.timeout_seconds} seconds. "
        "Do not request user input. Keep task outputs inside this task workspace.",
    ]
    if condition_instructions:
        parts.append(condition_instructions.strip())
    for skill in task.skills:
        directory = contained(task.skills_root / skill, task.skills_root)
        skill_path = directory / "SKILL.md"
        if not skill_path.is_file():
            raise ValueError(f"Declared task skill is missing: {skill}")
        content = skill_path.read_text().replace("{baseDir}", f"{CODEX_HOME}/skills/{directory.name}")
        parts.append(f"## Available task skill: {skill}\n\n{content}")
    parts.append("## Task\n\n" + task.prompt)
    return "\n\n".join(parts).strip() + "\n"


def build_exec_argv(settings: CodexSettings) -> list[str]:
    command = [settings.binary, "exec", "--json", "--skip-git-repo-check"]
    command += ["--sandbox", "read-only"] if settings.no_tools else ["--dangerously-bypass-approvals-and-sandbox"]
    command += ["--cd", WORKSPACE]
    for image_path in settings.input_images:
        if not image_path.startswith(WORKSPACE + "/") or ".." in Path(image_path).parts:
            raise ValueError("CLI image input must be inside the isolated task workspace")
        command += ["--image", image_path]
    return command + ["--output-last-message", f"{WORKSPACE}/.codex-last-message.txt", "-"]


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_code: int | None
    elapsed_seconds: float
    transcript_path: Path
    usage: Mapping[str, Any]
    error: str | None = None


class CodexBackend:
    def __init__(self, settings: CodexSettings = CodexSettings()):
        self.settings = settings

    def prepare(self, runtime: TaskRuntime) -> None:
        runtime.prepare(render_config(self.settings))

    def preflight(self, runtime: TaskRuntime) -> dict[str, str]:
        version = runtime.exec([self.settings.binary, "--version"]).stdout.strip()
        if version != f"codex-cli {self.settings.cli_version}":
            raise RuntimeError(f"CLI version mismatch: expected {self.settings.cli_version}")
        status = runtime.exec([self.settings.binary, "login", "status"], check=False)
        # Never persist authentication output, which could change format later.
        if status.returncode or "chatgpt" not in (status.stdout + status.stderr).lower():
            raise RuntimeError("Container Codex is not authenticated with ChatGPT")
        return {"cli_version": version, "authentication": "ChatGPT"}

    def run(self, runtime: TaskRuntime, *, condition_instructions: str = "",
            verify_login: bool = True) -> ExecutionResult:
        if runtime.state != "prepared":
            raise RuntimeError("Prepare the runtime before running Codex")
        if verify_login:
            self.preflight(runtime)
        prompt = runtime.task.prompt if self.settings.inference_only else build_prompt(runtime.task, condition_instructions=condition_instructions)
        (runtime.run_dir / "solver_prompt.txt").write_text(prompt)
        events_file = runtime.run_dir / "codex.events.jsonl"
        stderr_file = runtime.run_dir / "codex.stderr.log"
        command = runtime.docker_command("exec", "--interactive", "--workdir", WORKSPACE,
                                         runtime.name, *build_exec_argv(self.settings))
        start = time.monotonic()
        status, exit_code, error = "execution_error", None, None
        process: subprocess.Popen[str] | None = None
        try:
            with events_file.open("w") as events, stderr_file.open("w") as errors:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events,
                                           stderr=errors, text=True)
                try:
                    process.communicate(input=prompt, timeout=runtime.task.timeout_seconds)
                    exit_code = process.returncode
                    status = "completed" if exit_code == 0 else "execution_error"
                    if exit_code:
                        error = f"Codex exited with status {exit_code}; see private stderr log"
                except subprocess.TimeoutExpired:
                    status, error = "timeout", "Solver exceeded its fixed wall-clock budget"
        finally:
            elapsed = time.monotonic() - start
            # Even a clean Codex exit does not imply its background children
            # stopped. Never inject verifier material before stopping all of them.
            try:
                runtime.freeze_agent()
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
                    exit_code = process.returncode
        session_files = sorted((runtime.codex_home / "sessions").rglob("*.jsonl"))
        transcript = runtime.run_dir / "transcript.openclaw.jsonl"
        # A fresh per-run home should contain one main session. Preserve all raw
        # session files in that home; choose the complete main rollout for grading.
        main_session = select_main_session(session_files)
        source = main_session or events_file
        count = convert_transcript(source, transcript)
        usage = extract_usage(source, cumulative=main_session is not None)
        usage.update({"elapsed_seconds": elapsed, "cost_usd": None,
                      "billing": "ChatGPT subscription; API dollar cost unavailable"})
        if count == 0 and status == "completed":
            status, error = "missing_transcript", "No readable agent transcript was recorded"
        if self.settings.no_tools:
            tool_calls = sum(1 for entry in read_jsonl(transcript)
                             for item in entry.get("message", {}).get("content", [])
                             if isinstance(item, dict) and item.get("type") == "tool_use")
            if tool_calls:
                status, error = "unexpected_tool_call", "Inference request attempted tool execution"
        record = {"status": status, "exit_code": exit_code, "elapsed_seconds": elapsed,
                  "model": self.settings.model, "reasoning_effort": self.settings.reasoning_effort,
                  "cli_version": self.settings.cli_version, "error": error,
                  "transcript_records": count, "session_files": len(session_files),
                  "usage": usage}
        (runtime.run_dir / "execution.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
        (runtime.run_dir / "usage.json").write_text(json.dumps(usage, indent=2))
        return ExecutionResult(status, exit_code, elapsed, transcript, usage, error)


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def select_main_session(paths: Sequence[Path]) -> Path | None:
    # Main Codex sessions have session_meta.source='exec'/'cli'; ignore spawned
    # subagents should a future CLI ignore the disabled feature setting.
    candidates: list[Path] = []
    for path in paths:
        first = next(iter(read_jsonl(path)), {})
        source = first.get("payload", {}).get("source") if isinstance(first.get("payload"), dict) else None
        if isinstance(source, dict) and "subagent" in source:
            continue
        candidates.append(path)
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def _text_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    result: list[dict[str, Any]] = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, str):
            result.append({"type": "text", "text": item})
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind in ("text", "input_text", "output_text", "summary_text"):
                result.append({"type": "text", "text": str(item.get("text", ""))})
            elif kind in ("image", "input_image", "image_url"):
                if isinstance(item.get("source"), dict):
                    source = item["source"]
                else:
                    url = item.get("image_url", item.get("url", ""))
                    source = {"url": url.get("url", "") if isinstance(url, dict) else url}
                result.append({"type": "image", "source": source})
            elif kind in ("tool_use", "tool_result"):
                result.append(item)
    return result


def event_to_openclaw(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    # event_msg repeats response_item text in modern Codex sessions; counting
    # both would duplicate the transcript. Token-count events are handled below.
    if entry.get("type") in ("event_msg", "session_meta", "turn_context"):
        return []
    payload = next((entry[key] for key in ("payload", "item", "message")
                    if isinstance(entry.get(key), dict)), entry)
    kind = payload.get("type")
    role, content = "assistant", []
    if kind == "message" or payload.get("role") in ("user", "assistant", "system", "developer"):
        role = str(payload.get("role", "assistant"))
        content = _text_content(payload.get("content", []))
    elif kind in ("function_call", "custom_tool_call", "tool_call"):
        arguments = payload.get("arguments", payload.get("input", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        content = [{"type": "tool_use", "id": str(payload.get("call_id", payload.get("id", ""))),
                    "name": str(payload.get("name", payload.get("tool_name", "unknown"))), "input": arguments}]
    elif kind in ("function_call_output", "custom_tool_call_output", "tool_result"):
        output = payload.get("output", payload.get("result", ""))
        role = "user"
        if isinstance(output, list):
            tool_content = _text_content(output)
        elif isinstance(output, dict) and isinstance(output.get("content"), list):
            tool_content = _text_content(output["content"])
        else:
            tool_content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        content = [{"type": "tool_result", "tool_use_id": str(payload.get("call_id", payload.get("id", ""))),
                    "content": tool_content}]
    elif kind == "reasoning":
        # Reasoning is not a user-visible assistant statement. Safety graders
        # must not count an internal plan as having informed the user of risk.
        content = [{"type": "thinking", "thinking": block["text"]}
                   for block in _text_content(payload.get("summary", payload.get("content", [])))
                   if block.get("type") == "text"]
    elif kind == "agent_message" and entry.get("type") == "item.completed":
        content = _text_content(payload.get("text", ""))
    elif kind == "command_execution" and entry.get("type") == "item.completed":
        return [
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "tool_use",
             "id": str(payload.get("id", "")), "name": "exec_command", "input": {"cmd": payload.get("command", "")}}]}},
            {"type": "message", "message": {"role": "user", "content": [{"type": "tool_result",
             "tool_use_id": str(payload.get("id", "")), "content": str(payload.get("aggregated_output", ""))}]}},
        ]
    return [{"type": "message", "message": {"role": role, "content": content}}] if content else []


def convert_transcript(source: Path, destination: Path) -> int:
    count = 0
    with destination.open("w", encoding="utf-8") as stream:
        for entry in read_jsonl(source):
            for record in event_to_openclaw(entry):
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    return count


def extract_usage(source: Path, *, cumulative: bool = True) -> dict[str, Any]:
    totals: dict[str, int | None] = {"input_tokens": None, "output_tokens": None,
                                   "cached_input_tokens": None, "reasoning_output_tokens": None,
                                   "total_tokens": None}
    turns = 0
    for entry in read_jsonl(source):
        payload = entry.get("payload", {})
        usage = None
        if cumulative and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            usage = info.get("total_token_usage")
        elif not cumulative and entry.get("type") == "turn.completed":
            usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        turns += 1
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                totals[key] = value if cumulative else int(totals[key] or 0) + value
    # Absence of counters is missing information, not zero usage.
    return {**totals, "usage_available": turns > 0, "usage_events": turns,
            "request_count": None}
