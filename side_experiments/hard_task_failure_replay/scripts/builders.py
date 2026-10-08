"""Builder engines for the failure summaries.

Two engines run the same prompt in the same kind of isolated container (read-only
corpus and instruction, writable /work, no tests, no solutions):
  codex  - GPT models through the pinned Codex CLI and the user's Codex login.
  claude - Claude models through the Claude Code CLI in headless (-p) mode. Only a
           short-lived OAuth access token enters the container (as an environment
           variable); the refresh token never leaves the host, so the container
           cannot rotate the host login.
Each engine returns the attempt's usage and an audit of which model actually ran.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from experiment.shared.codex_backend import (
    CodexSettings, convert_transcript, extract_usage, read_jsonl, render_config, select_main_session,
)

from .common import TB, config

CLAUDE_BINARY = Path("~/.vscode-server/extensions/anthropic.claude-code-2.1.292-linux-x64/resources/native-binary/claude").expanduser()
CLAUDE_CREDENTIALS = Path("~/.claude/.credentials.json").expanduser()


def spec(builder_id: str) -> dict:
    return {"id": builder_id, **config()["builders"][builder_id]}


def claude_access_token(min_hours: float) -> str:
    """Read the host's current OAuth access token; refuse if it would expire mid-job."""
    oauth = json.loads(CLAUDE_CREDENTIALS.read_text())["claudeAiOauth"]
    remaining = oauth["expiresAt"] / 1000 - time.time()
    if remaining < min_hours * 3600:
        raise RuntimeError(f"Claude access token expires in {remaining / 3600:.2f} h; refresh the host login first")
    return oauth["accessToken"]


def _base_args(name: str, support: dict, mounts: list[tuple[Path, str, bool]], env: list[str]) -> list[str]:
    args = ["docker", "run", "--name", name, "--interactive", "--init", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--workdir", "/work",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PATH=/opt/codex:/opt/claude:/opt/builder-python/bin:/usr/local/bin:/usr/bin:/bin",
            "--env", "SSL_CERT_FILE=/opt/builder-ca.crt", "--env", "CODEX_CA_CERTIFICATE=/opt/builder-ca.crt",
            "--env", "NODE_EXTRA_CA_CERTS=/opt/builder-ca.crt"]
    for item in env:
        args += ["--env", item]
    base = [(TB / "runtime/bin", "/opt/codex", True), (Path(support["python_root"]), "/opt/builder-python", True),
            (TB / "prepared/transport/ca-certificates.crt", "/opt/builder-ca.crt", True)]
    for source, target, readonly in base + mounts:
        args += ["--mount", f"type=bind,src={Path(source).resolve()},dst={target}" + (",readonly" if readonly else "")]
    return args


def codex_command(builder: dict, support: dict, mounts, attempt: Path, home: Path, name: str,
                  schema: Path | None) -> tuple[list[str], dict]:
    settings = render_config(CodexSettings(model=builder["model"], reasoning_effort=builder["effort"]))
    settings = settings.replace("image_generation = true", "image_generation = false")
    settings += "browser_use = false\nbrowser_use_external = false\nbrowser_use_full_cdp_access = false\ncomputer_use = false\n"
    (home / "config.toml").write_text(settings)
    args = _base_args(name, support, mounts + [(attempt, "/work", False), (home, "/opt/builder-home", False)],
                      ["CODEX_HOME=/opt/builder-home", "HOME=/opt/builder-home"])
    args += [support["image_id"], "/opt/codex/codex", "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox", "--cd", "/work"]
    if schema is not None:
        args += ["--output-schema", "/work/" + schema.name]
    return args + ["--output-last-message", "/work/result.json", "-"], {}


def claude_command(builder: dict, support: dict, mounts, attempt: Path, home: Path, name: str,
                   schema: Path | None, timeout_seconds: float) -> tuple[list[str], dict]:
    token = claude_access_token(min_hours=timeout_seconds / 3600 + 0.25)
    args = _base_args(name, support, mounts + [(attempt, "/work", False), (home, "/opt/builder-home", False),
                                               (CLAUDE_BINARY, "/opt/claude/claude", True)],
                      ["HOME=/opt/builder-home", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER=1",
                       "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1", "DISABLE_TELEMETRY=1"])
    args += [support["image_id"], "/opt/claude/claude", "-p", "--model", builder["model"], "--effort", builder["effort"],
             "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if schema is not None:
        args += ["--json-schema", schema.read_text()]
    # Variadic list last: no web access and no sub-agents (which could run other models).
    args += ["--disallowedTools", "WebSearch", "WebFetch", "Task"]
    # The prompt arrives on stdin; the token is passed through the environment, never argv.
    return args, {"CLAUDE_CODE_OAUTH_TOKEN": token}


def run(builder: dict, support: dict, mounts, attempt: Path, home: Path, prompt: str,
        timeout_seconds: float, schema: Path | None = None) -> dict:
    """Run one builder job; always remove the container and any copied credential."""
    name = f"side-replay-{builder['id']}-" + uuid.uuid4().hex[:10]
    auth_copy = None
    if builder["engine"] == "codex":
        auth_copy = home / "auth.json"
        auth_copy.write_bytes(Path(os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser().read_bytes())
        auth_copy.chmod(0o600)
        argv, extra_env = codex_command(builder, support, mounts, attempt, home, name, schema)
        events_name = "codex.events.jsonl"
    else:
        argv, extra_env = claude_command(builder, support, mounts, attempt, home, name, schema, timeout_seconds)
        events_name = "claude.events.jsonl"
    started, timed_out, exit_code = time.time(), False, None
    try:
        with (attempt / events_name).open("w") as events, (attempt / "builder.stderr.log").open("w") as errors:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True,
                                       env={**os.environ, **extra_env})
            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if auth_copy is not None:
            auth_copy.unlink(missing_ok=True)
    usage, audit = (audit_codex(home, attempt, builder) if builder["engine"] == "codex"
                    else audit_claude(attempt / events_name, attempt, builder))
    return {"container_name": name, "started_at_unix": started, "elapsed_seconds": time.time() - started,
            "exit_code": exit_code, "timed_out": timed_out, "usage": usage, "model_audit": audit}


def audit_codex(home: Path, attempt: Path, builder: dict) -> tuple[dict, dict]:
    session = select_main_session(list((home / "sessions").rglob("*.jsonl")))
    if session:
        (attempt / "codex.session.jsonl").write_bytes(session.read_bytes())
    source = attempt / "codex.session.jsonl" if session else attempt / "codex.events.jsonl"
    usage = extract_usage(source, cumulative=session is not None)
    convert_transcript(source, attempt / "transcript.openclaw.jsonl")
    models, efforts = set(), set()
    for event in read_jsonl(source):
        if event.get("type") == "turn_context":
            payload = event.get("payload", {})
            models.add(payload.get("model"))
            efforts.add(payload.get("effort", payload.get("reasoning_effort")))
    verified = models == {builder["model"]} and efforts == {builder["effort"]}
    return usage, {"engine": "codex", "models": sorted(models, key=str), "efforts": sorted(efforts, key=str),
                   "requested_model": builder["model"], "requested_effort": builder["effort"],
                   "model_effort_verified": verified}


def audit_claude(events_path: Path, attempt: Path, builder: dict) -> tuple[dict, dict]:
    init_model, result, fallbacks = None, None, []
    for line in events_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init_model = event.get("model")
        if event.get("type") == "system" and event.get("subtype") == "model_refusal_fallback":
            fallbacks.append({k: event.get(k) for k in ("original_model", "fallback_model", "api_refusal_category")})
        if event.get("type") == "result":
            result = event
    model_usage = (result or {}).get("modelUsage") or {}
    usage = {"usage_available": result is not None, **((result or {}).get("usage") or {}),
             "total_cost_usd": (result or {}).get("total_cost_usd"), "num_turns": (result or {}).get("num_turns")}
    other = sorted(m for m in model_usage if not m.startswith(builder["model"]))
    if result and isinstance(result.get("structured_output"), (dict, list)):
        (attempt / "result.json").write_text(json.dumps(result["structured_output"], ensure_ascii=False, indent=2))
    elif result and isinstance(result.get("result"), str) and result["result"].strip().startswith("{"):
        (attempt / "result.json").write_text(result["result"])
    # Claude Code silently continues on another model after a safeguard refusal; that output is not the
    # requested builder's work, so any other model with output tokens fails the audit.
    verified = bool(result) and init_model is not None and init_model.startswith(builder["model"]) and \
        any(m.startswith(builder["model"]) for m in model_usage) and not fallbacks and \
        not any((model_usage[m] or {}).get("outputTokens") for m in other)
    return usage, {"engine": "claude", "init_model": init_model, "models_used": sorted(model_usage),
                   "other_models_used": other, "refusal_fallbacks": fallbacks, "requested_model": builder["model"],
                   "requested_effort": builder["effort"], "effort_source": "--effort flag (not echoed by the CLI)",
                   "model_effort_verified": verified, "result_subtype": (result or {}).get("subtype"),
                   "is_error": (result or {}).get("is_error")}
