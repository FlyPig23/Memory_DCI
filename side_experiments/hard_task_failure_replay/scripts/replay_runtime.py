"""Experience arms: the main vanilla baseline adapter plus read-only experience mounts.

Arms (see ARMS below): failure_replay (r2/r3: failed attempts + v1 Luna lessons),
traj_only (failed attempts only), and mem_<builder> (one builder's v3 failure
analysis only, no transcripts).

Everything except two things is the unchanged main-experiment baseline
(experiment/benchmarks/terminal_bench_2_1/scripts/baseline_runtime.py): the
same pinned Codex, empty MCP configuration, process freeze before grading,
pre-grader snapshot, official verifier and isolation audit. The two
differences are read-only binds of this task's experience under /experience
(which parts depends on the arm), and a prompt that describes them, pastes the
failure analysis when the arm has one, and ends with the official instruction. Other historical paths (/pool, /memory, /corpus, /skills) must
still be absent.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import shutil
import time
from pathlib import Path

from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.trial.hooks import TrialEvent
from harbor.trial.trial import Trial

from experiment.benchmarks.terminal_bench_2_1.scripts import baseline_runtime as base
from experiment.benchmarks.terminal_bench_2_1.scripts.harbor_runtime import bind, clean_private_runtime, command

from .common import CORPUS, PROMPTS, config, load, sha

EXPERIENCE = "/experience"


def arm_spec(arm: str) -> dict:
    """Mount targets -> corpus-relative sources, preamble template and pasted memory file."""
    if arm == "failure_replay":
        return {"preamble": "replay_preamble.txt", "mounts": {EXPERIENCE: "experience"},
                "memory": "experience/memory/lessons.md", "attempts": True}
    if arm == "traj_only":
        return {"preamble": "preamble_traj.txt", "mounts": {EXPERIENCE + "/failed_attempts": "experience/failed_attempts"},
                "memory": None, "attempts": True}
    if arm.startswith("mem_") and arm[4:] in config()["builders"]:
        return {"preamble": "preamble_mem.txt", "mounts": {EXPERIENCE + "/memory": f"memories/{arm[4:]}"},
                "memory": f"memories/{arm[4:]}/lessons.md", "attempts": False}
    if arm.startswith("trajmem_") and arm[8:] in config()["builders"]:
        # The union of traj_only and mem_<builder>: the same transcripts and the same frozen analysis.
        return {"preamble": "preamble_trajmem.txt",
                "mounts": {EXPERIENCE + "/failed_attempts": "experience/failed_attempts",
                           EXPERIENCE + "/memory": f"memories/{arm[8:]}"},
                "memory": f"memories/{arm[8:]}/lessons.md", "attempts": True}
    raise ValueError(f"unknown experience arm: {arm}")


def corpus_identity(task_id: str, arm: str) -> dict:
    spec = arm_spec(arm)
    files = {}
    for target, relative in spec["mounts"].items():
        root = CORPUS / task_id / relative
        if not root.is_dir():
            raise ValueError(f"{task_id}/{arm}: missing {root}")
        files.update({f"{target}/{p.relative_to(root)}": sha(p) for p in sorted(root.rglob("*")) if p.is_file()})
    if spec["memory"] and not (CORPUS / task_id / spec["memory"]).is_file():
        raise ValueError(f"{task_id}/{arm}: the failure analysis has not been written yet")
    return {"arm": arm, "mounts": spec["mounts"], "file_count": len(files),
            "files_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "memory_sha256": sha(CORPUS / task_id / spec["memory"]) if spec["memory"] else None}


def replay_prompt(instruction: str, seconds: float, task_id: str, arm: str = "failure_replay") -> str:
    spec = arm_spec(arm)
    manifest = load(CORPUS.parent / "manifests" / f"corpus_{task_id}.json")
    models = ", ".join(f"{m} x{n}" for m, n in manifest["models"].items())
    memory = (CORPUS / task_id / spec["memory"]).read_text().strip() if spec["memory"] else ""
    preamble = (PROMPTS / spec["preamble"]).read_text().format(
        attempt_count=manifest["attempt_count"], models=models, task_id=task_id, memory=memory)
    head = base.solver_prompt("", seconds)  # identical deadline sentence to the baseline arm
    return head + preamble + "\n\n## Original Terminal-Bench task\n\n" + instruction


async def audit_isolation(agent, environment, *, stage):
    """Baseline isolation checks, plus: /experience is present, read-only and this task's corpus."""
    contract = load(agent.run_dir / "runtime_contract.json")
    observed = await environment.exec(shlex.join(["cat", base.HOME + "/config.toml"]),
                                      timeout_sec=base.INFRA_TIMEOUT_SECONDS, user="root")
    if observed.return_code:
        raise RuntimeError("Cannot read the isolated Codex configuration")
    config_audit = base.validate_config(observed.stdout, workspace=agent.workspace if stage == "after_solver" else None)
    containers = await base.docker_metadata(environment)
    if set(containers) != {MAIN_SERVICE_NAME}:
        raise ValueError("Side experiment supports single-service tasks only")
    main = containers[MAIN_SERVICE_NAME]
    mounts = base.validate_mounts(main["Mounts"], contract["expected_mounts"])
    spec = arm_spec(agent.arm)
    experience = [m for m in mounts if m["target"] == EXPERIENCE or m["target"].startswith(EXPERIENCE + "/")]
    if {m["target"] for m in experience} != set(spec["mounts"]) or not all(m["read_only"] for m in experience):
        raise ValueError("Experience mounts differ from the arm specification or are writable")
    for item in experience:
        if Path(item["source"]).resolve() != (CORPUS / agent.task_id / spec["mounts"][item["target"]]).resolve():
            raise ValueError("Mounted experience belongs to another task or arm")
    probe = ("import json,os; print(json.dumps({'history': [p for p in " + repr(base.HISTORY_PATHS) +
             " if os.path.lexists(p)], 'lessons': os.path.isfile('/experience/memory/lessons.md'),"
             " 'attempts': os.path.isdir('/experience/failed_attempts'),"
             " 'writable': any(os.access(t, os.W_OK) for t in " + repr(sorted(spec["mounts"])) + ")}))")
    result = await environment.exec(shlex.join([base.PYTHON, "-c", probe]), timeout_sec=base.INFRA_TIMEOUT_SECONDS)
    found = json.loads(result.stdout) if not result.return_code else None
    if (not found or found["history"] or found["writable"] or found["lessons"] != bool(spec["memory"])
            or found["attempts"] != spec["attempts"]):
        raise ValueError("Resource probe failed: other history present, wrong experience content, or writable")
    record = {"valid": True, "stage": stage, "condition": agent.arm, "container_id": main["Id"],
              "image_id": main["Image"], "other_history_paths_absent": True,
              "history_paths_checked": list(base.HISTORY_PATHS), "experience_mounts": experience,
              "memory_present": found["lessons"], "failed_attempts_present": found["attempts"],
              "experience_writable": False,
              "mounts_verified": True, "mounts": mounts, "reviewer_enabled": False, **config_audit,
              "official_working_dir": main["Config"].get("WorkingDir"), "official_user": main["Config"].get("User"),
              "compose_project": main["Config"]["Labels"].get("com.docker.compose.project"),
              "container_ids": [main["Id"]],
              "network_names": sorted(main.get("NetworkSettings", {}).get("Networks", {}))}
    base.write_json(agent.run_dir / f"isolation_{stage}.json", record)
    if stage == "before_solver":
        base.write_json(agent.run_dir / "isolation.json", record)
    return record


class FailureReplayAgent(base.TerminalBenchBaselineAgent):
    def __init__(self, *args, arm="failure_replay", **kwargs):
        super().__init__(*args, **kwargs)
        self.arm = arm

    @staticmethod
    def name():
        return "terminal-bench-failure-replay-codex"

    async def setup(self, environment):
        # Same steps as the baseline setup; only the isolation audit differs.
        if self.mcp_servers or self.skills_dir:
            raise ValueError("Side experiment tasks may not inject MCP or skills")
        containers = await base.docker_metadata(environment)
        main = containers[MAIN_SERVICE_NAME]
        base.write_json(self.run_dir / "environment_identity.json", {
            "container_ids": [c["Id"] for c in containers.values()],
            "compose_project": main["Config"]["Labels"].get("com.docker.compose.project"),
            "network_names": sorted(main.get("NetworkSettings", {}).get("Networks", {})), "image_id": main["Image"]})
        where = await environment.exec("pwd", timeout_sec=base.INFRA_TIMEOUT_SECONDS)
        self.workspace = (where.stdout or "").strip()
        if where.return_code or not self.workspace.startswith("/") or "\n" in self.workspace:
            raise ValueError("Task lacks a unique absolute workspace")
        if {p.name for p in self.private_home.iterdir()} != {"auth.json"}:
            raise ValueError("Codex home must initially contain only isolated authentication")
        (self.private_home / "config.toml").write_text(base.make_config())
        identity = await environment.exec("id -u; id -g", timeout_sec=base.INFRA_TIMEOUT_SECONDS)
        uid, gid = [int(v) for v in identity.stdout.splitlines()]
        prepared = await environment.exec(f"chown -R {uid}:{gid} {base.HOME} && chmod 700 {base.HOME} && "
                                          f"chmod 600 {base.HOME}/auth.json", timeout_sec=base.INFRA_TIMEOUT_SECONDS, user="root")
        if prepared.return_code:
            raise RuntimeError("Cannot prepare the private Codex home")
        version = await environment.exec("/opt/codex/codex --version", timeout_sec=base.INFRA_TIMEOUT_SECONDS,
                                         env={"CODEX_HOME": base.HOME})
        if version.return_code or version.stdout.strip() != f"codex-cli {base.CLI_VERSION}":
            raise ValueError("Pinned Codex CLI version mismatch")
        probe = await environment.exec(shlex.join([base.PYTHON, "-c", "import sys; assert sys.version_info[:2] == (3,12)"]),
                                       timeout_sec=base.INFRA_TIMEOUT_SECONDS)
        if probe.return_code:
            raise RuntimeError("Pinned standalone Python is unavailable")
        await audit_isolation(self, environment, stage="before_solver")
        base.write_json(self.logs_dir / "setup.json", {"workspace": self.workspace, "cli_version": base.CLI_VERSION,
                        "model": base.MODEL, "reasoning_effort": base.EFFORT, "fresh_codex_home": True,
                        "custom_tools": [], "arm": self.arm, "experience_mounts": sorted(arm_spec(self.arm)["mounts"])})

    async def run(self, instruction, environment, context: AgentContext):
        contract = load(self.run_dir / "runtime_contract.json")
        deadline = contract["solver_timeout_seconds"]
        prompt = replay_prompt(instruction, deadline, self.task_id, self.arm)
        (self.run_dir / "control/task.txt").write_text(instruction)
        (self.run_dir / "control/solver_prompt.txt").write_text(prompt)
        base.write_json(self.run_dir / "prompt_audit.json", {
            "official_instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
            "solver_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "official_instruction_included_verbatim": prompt.endswith(instruction),
            "arm": self.arm, "additional_resources": sorted(arm_spec(self.arm)["mounts"]),
            "memory_pasted": bool(arm_spec(self.arm)["memory"]), "custom_tools": [], "reviewer": False})
        log_dir = str(self.environment_logs_dir)
        argv = [base.PYTHON, base.SUPERVISOR, "supervise", "--prompt", base.CONTROL + "/solver_prompt.txt",
                "--events", log_dir + "/codex.events.jsonl", "--stderr", log_dir + "/codex.stderr.log",
                "--timeout", str(deadline), "--", *base.codex_command(self.workspace, log_dir + "/final.txt")]
        started, status, code = time.monotonic(), "cancelled", None
        try:
            result = await environment.exec('export PATH="$PATH:/opt/baseline-bin"; ' + shlex.join(argv),
                                            cwd=self.workspace, env={"CODEX_HOME": base.HOME}, timeout_sec=None)
            code = result.return_code
            status = "completed" if code == 0 else "timeout" if code == 124 else "execution_error"
            if code == 124:
                raise TimeoutError("Official solver deadline exceeded")
            if code:
                raise base.NonZeroAgentExitCodeError(f"Codex exited {code}")
        finally:
            base.write_json(self.logs_dir / "execution.json", {"status": status, "exit_code": code,
                            "elapsed_seconds": time.monotonic() - started, "model": base.MODEL,
                            "reasoning_effort": base.EFFORT})
            context.metadata = {"model": base.MODEL, "reasoning_effort": base.EFFORT, "status": status}


async def run_trial(task_path, trial_dir, task_id, *, python_root, auth_file, environment_config,
                    cleanup_image, verifier_policy="unlimited", arm="failure_replay"):
    """One fresh failure-replay rollout, mirroring baseline_runtime.run_trial step for step."""
    task_path, trial_dir, python_root = map(lambda p: Path(p).resolve(), (task_path, trial_dir, python_root))
    if trial_dir.exists():
        raise FileExistsError("Refuse to overwrite or rerun an existing trial")
    spec = base.inspect_task(task_path)
    verifier_timeout = None if verifier_policy == "unlimited" else spec["verifier"]["timeout_sec"]
    identity = corpus_identity(task_id, arm)
    trial_dir.mkdir(parents=True)
    (trial_dir / "control").mkdir()
    home = trial_dir / "private/solve-home"
    home.mkdir(parents=True, mode=0o700)
    shutil.copyfile(auth_file, home / "auth.json")
    os.chmod(home / "auth.json", 0o600)
    contract = {"condition": arm, "task_id": task_id, "model": base.MODEL,
                "reasoning_effort": base.EFFORT, "cli_version": base.CLI_VERSION, "harbor_version": "0.23.0",
                "solver_timeout_seconds": spec["agent"]["timeout_sec"],
                "official_verifier_timeout_seconds": spec["verifier"]["timeout_sec"],
                "verifier_timeout_seconds": verifier_timeout, "verifier_timeout_policy": verifier_policy,
                "reviewer_enabled": False, "tool_names": [], "mcp_servers": [],
                "historical_resources": [f"{target} <- corpus/{task_id}/{source} (read-only)"
                                         for target, source in arm_spec(arm)["mounts"].items()],
                "experience_corpus": identity, "global_memory": False, "solver_rollouts": 1,
                "native_codex_tools_preserved": True, "task_services_paused": False}
    config = dict(environment_config or {})
    mounts = base.support_mounts(home, trial_dir / "control", python_root)
    for target, source in arm_spec(arm)["mounts"].items():
        mounts.append(bind(CORPUS / task_id / source, target))
    config.update(type="docker", delete=False, kwargs={"keep_containers": True}, mounts=mounts)
    trial, frozen = None, None
    try:
        trial = await Trial.create(TrialConfig(
            task=TaskConfig(path=task_path), trial_name=trial_dir.name, trials_dir=trial_dir.parent,
            agent=AgentConfig(import_path=__name__ + ":FailureReplayAgent", model_name=base.MODEL,
                              override_setup_timeout_sec=base.SETUP_TIMEOUT_SECONDS,
                              kwargs={"run_dir": str(trial_dir), "task_id": task_id, "arm": arm}),
            environment=EnvironmentConfig(**config), verifier=VerifierConfig()))
        if trial._agent_timeout_sec != spec["agent"]["timeout_sec"]:
            raise ValueError("Effective solver deadline differs from official task")
        trial._verifier_timeout_sec = verifier_timeout
        expected = list(trial._agent_env_mounts)
        expected.append(bind(base.BASE / "prepared/transport/ca-certificates.crt", "/opt/v5-ca/ca-certificates.crt"))
        contract.update(expected_mounts=expected, effective_solver_timeout_verified=True)
        base.write_json(trial_dir / "runtime_contract.json", contract)

        async def agent_end(_event):
            nonlocal frozen
            frozen = {"taken_before_hidden_grading": True, "solver_stopped": False, "snapshot_valid": False,
                      "task_services_kept_running": True}
            try:
                stopped = await trial.agent_environment.exec(shlex.join([base.PYTHON, base.SUPERVISOR, "freeze"]),
                                                             timeout_sec=base.INFRA_TIMEOUT_SECONDS, user="root")
                if stopped.return_code:
                    raise RuntimeError("Cannot stop Codex controls before hidden grading")
                frozen.update(solver_stopped=True, stopped_solver_processes=json.loads(stopped.stdout))
                await trial.agent_environment.exec(f"chown -R {os.getuid()}:{os.getgid()} {base.HOME}",
                                                   timeout_sec=base.INFRA_TIMEOUT_SECONDS, user="root")
                base.capture_session(home, trial.agent.logs_dir)
                if trial.result.agent_result is not None:
                    trial.agent.populate_context_post_run(trial.result.agent_result)
                try:
                    await audit_isolation(trial.agent, trial.agent_environment, stage="after_solver")
                except Exception as exc:
                    base.write_json(trial_dir / "isolation_after_solver.json", {"valid": False, "error": str(exc)})
                try:
                    manifest = await base.snapshot_public(trial)
                    frozen.update(snapshot_valid=all(r["status"] in {"copied", "absent_verified"} for r in manifest["paths"]))
                except Exception as exc:
                    frozen.update(snapshot_error=str(exc))
            except Exception as exc:
                frozen.update(boundary_error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                base.write_json(trial_dir / "freeze.json", frozen)

        async def verification_start(_event):
            if not frozen or not frozen.get("solver_stopped"):
                raise RuntimeError("Solver was not stopped before grading")

        trial.add_hook(TrialEvent.AGENT_END, agent_end)
        trial.add_hook(TrialEvent.VERIFICATION_START, verification_start)
        result = await trial.run()
        output = {"task_id": task_id, "condition": arm, "harbor": result.model_dump(mode="json"),
                  "freeze": frozen, "isolation": load(trial_dir / "isolation.json") if (trial_dir / "isolation.json").exists() else None,
                  "model_audit": load(trial_dir / "agent/model_audit.json") if (trial_dir / "agent/model_audit.json").exists() else None,
                  "usage": load(trial_dir / "agent/usage.json") if (trial_dir / "agent/usage.json").exists() else None}
        base.write_json(trial_dir / "replay_result.json", output)
        await remove_stopped_containers(output)
        return output
    finally:
        await clean_private_runtime(trial_dir, cleanup_image)


async def remove_stopped_containers(output):
    """Delete this trial's stopped task container and network after the result is written."""
    ids = (output.get("isolation") or {}).get("container_ids") or []
    for container in ids:
        await command(["docker", "rm", "--force", "--volumes", container], check=False, timeout=120)
    for network in (output.get("isolation") or {}).get("network_names") or []:
        await command(["docker", "network", "rm", network], check=False, timeout=60)
