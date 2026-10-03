"""Validate the one observed native Codex workspace-trust config append.

This is post-run evidence checking. It neither writes configuration nor changes
the solver. Original initial and final hashes remain separately visible.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from experiment.src.codex_backend import CodexSettings, render_config

TRUST_APPEND = b'\n[projects."/tmp_workspace"]\ntrust_level = "trusted"\n'


def validate_config_transition(config_path: Path, solver: dict, plan: dict) -> dict:
    if plan.get("mcp_servers") != []:
        raise ValueError("Expected a frozen native protocol with no MCP")
    initial = render_config(CodexSettings(model=plan["model"],
        reasoning_effort=plan["reasoning_effort"], cli_version=plan["cli_version"],
        mcp_servers=())).encode("utf-8")
    digest = lambda data: hashlib.sha256(data).hexdigest()
    initial_sha = digest(initial)
    if solver.get("config_sha256") != initial_sha or solver.get("mcp_servers") != []:
        raise ValueError("Native initial config evidence does not match frozen rendering")
    final = Path(config_path).read_bytes()
    if final == initial:
        transition = "unchanged"
    elif final == initial + TRUST_APPEND:
        transition = "exact_workspace_trust_append"
    else:
        raise ValueError("Native final config differs beyond the exact workspace trust append")
    return {"status": "passed", "initial_config_sha256": initial_sha,
        "final_config_sha256": digest(final), "transition": transition,
        "appended_bytes": len(TRUST_APPEND) if transition != "unchanged" else 0,
        "accepted_append_utf8": TRUST_APPEND.decode("utf-8") if transition != "unchanged" else "",
        "model_and_mcp_configuration_unchanged": True,
        "scope": "Exact saved initial/final configuration bytes; not a claim of continuous config monitoring"}


def audit_solver(run_dir: Path, plan: dict, *, write: bool = False) -> dict:
    """Validate saved solver model and config without rewriting historical evidence."""
    import json
    from experiment.src.codex_backend import read_jsonl, select_main_session
    from experiment.src.experiment_protocol import atomic_json

    session = select_main_session(list((run_dir / "codex-home/sessions").rglob("*.jsonl")))
    models, efforts = set(), set()
    if session is not None:
        for row in read_jsonl(session):
            if row.get("type") == "turn_context":
                payload = row.get("payload", {})
                models.add(payload.get("model"))
                efforts.add(payload.get("effort", payload.get("reasoning_effort")))
    if models != {plan["model"]} or efforts != {plan["reasoning_effort"]}:
        raise ValueError("Actual solver model/effort differs from protocol or is unobserved")
    proof = json.loads((run_dir / "native_environment.json").read_text())
    config = validate_config_transition(run_dir / "codex-home/config.toml", proof["solver"], plan)
    result = {"models": sorted(models), "efforts": sorted(efforts), "config": config}
    if write:
        atomic_json(run_dir / "solver_audit.json", result)
    return result
