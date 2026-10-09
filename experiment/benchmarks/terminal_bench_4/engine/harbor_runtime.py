"""Pinned Harbor adapter: one V5 solve, official grading, isolated review.

This module never starts work on import. The official task environment, services,
resources, agent timeout and verifier are owned by Harbor. No baseline is run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import tarfile
import tempfile
import time
import tomllib
import uuid

from harbor.agents.base import BaseAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.agent.context import AgentContext
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.trial.hooks import TrialEvent
from harbor.trial.trial import Trial

from experiment.shared.codex_backend import (
    CodexSettings, MCPServer, convert_transcript, extract_usage, read_jsonl,
    render_config, select_main_session,
)
from experiment.shared.memory.tools import audit_memory, initialize_memory

ROOT = Path(__file__).resolve().parents[4]
BASE = Path(__file__).resolve().parents[1]
SHARED = ROOT / "experiment/shared/memory"
MODEL, EFFORT, CLI_VERSION = "gpt-5.6-sol", "medium", "0.153.4"
PYTHON = "/opt/v5-python/bin/python3"
SERVER = "/opt/v5/serve_memory.py"
HOME = "/opt/v5-home"
CONTROL = "/opt/v5-control"
REVIEW_SECONDS = 180
TOOLS = ["reason", "DCI_search_task", "DCI_search_trajectory", "DCI_search_memory", "distill"]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def hash_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob("*")) if p.is_file() and not p.is_symlink()}


def bind(source, target, readonly=True):
    value = {"type": "bind", "source": str(Path(source).resolve()), "target": target}
    if readonly:
        value["read_only"] = True
    return value


def python_installation():
    candidates = [p for p in (BASE / "runtime/python").glob("cpython-3.12.*-linux-*-gnu")
                  if not p.is_symlink() and (p / "bin/python3").exists()]
    if len(candidates) != 1:
        raise ValueError("Provide python_root: expected one pinned standalone CPython installation")
    return candidates[0]


def solver_instructions(pool, workspace, *, initial_memory=None):
    """Reuse V5's retrieval workflow with TB2.1 resources and review ordering."""
    tasks = len([p for p in (Path(pool) / "trajectories").iterdir() if p.is_dir()])
    traces = len(list((Path(pool) / "trajectories").rglob("*.txt")))
    prompt = (SHARED / "prompt.txt").read_text()
    if initial_memory is not None:
        prompt = prompt.replace('V5 — task-local evidence memory', 'V6 — seeded task-local evidence memory')
        prompt = prompt.replace('/memory: initially empty experience memory belonging ONLY to this test task.',
            f"/memory: this task's private copy of {initial_memory['entry_count']} frozen failure lessons "
            "distilled from historical TRAINING trajectories before testing.")
        prompt = prompt.replace('Your environment contains two experience resources:',
                                'Your environment contains the following experience resources:')
        prompt += ("\nThe same initial failure-memory library is copied independently into every test task. "
                   "No updates from another test task are inherited. Use DCI_search_memory to find relevant "
                   "failure lessons when useful under the workflow above; all original training trajectories "
                   "remain available for checking source evidence. A failed source run does not prove its "
                   "root cause. Distinguish observed failures, causal hypotheses, and untested fixes; check "
                   "applicability before adopting a lesson. A source_observed label is not evidence that a "
                   "proposed repair works on the current task. If a historical lesson suggests checking "
                   "grading or collection feedback, use only evidence already publicly provided by the "
                   "current task; this never authorizes seeking hidden tests, scores or grader files.\n")
    prompt = prompt.replace("36 training-task descriptions", f"{tasks} training-task descriptions")
    prompt = prompt.replace("432 original training trajectories", f"{traces} original training trajectories")
    prompt = prompt.replace("/pool/trajectories/<task_id>/<model_alias>/<task_id>_<score>.txt:",
        "/pool/trajectories/<task_id>/<model_alias>/<trial_id>/<task_id>_<score>.txt:")
    prompt = prompt.replace("The final filename number is the source",
        "These are searchable text renderings preserving every field of the source ATIF records; raw originals are stored separately. The final filename number is the source")
    prompt = prompt.replace("Keep task deliverables in /tmp_workspace.",
        f"Initial working directory: {workspace}. Follow the task's explicit output paths; do not relocate its deliverables.")
    prompt = prompt.replace(
        "A separate review\nwill inspect your frozen visible evidence before grading; you never see the\nhidden score during solving or memory writing.",
        "A separate review runs after official grading, but sees only a read-only\n"
        "snapshot of visible evidence captured before grading. Neither solving nor\n"
        "memory review receives hidden scores or grader feedback.")
    prompt += ("\nA source score shown as null, None, or unscored means no official score is available, not zero.\n"
               "If a task card marks a record NO TRAJECTORY BODY AVAILABLE, skip that record;\n"
               "do not repeatedly try to retrieve its missing body. Other available records may still be useful.\n")
    return prompt


def _replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Prompt template drifted; expected one occurrence of: {old[:60]!r}")
    return text.replace(old, new)


def solver_instructions_v7(pool, workspace, *, initial_memory=None):
    """V7: the V5/V6 text with only the sentences that describe the corpus rewritten.

    The pool holds only this task's own failed official attempts, and the optional seed was written
    from them. Workflow steps, integrity rules and the rest of the wording stay verbatim.
    """
    tasks = len([p for p in (Path(pool) / "trajectories").iterdir() if p.is_dir()])
    traces = len(list((Path(pool) / "trajectories").rglob("*.txt")))
    prompt = solver_instructions(pool, workspace, initial_memory=initial_memory)
    edits = [
        (f"- /pool/tasks: {tasks} training-task descriptions and trajectory navigation cards.",
         "- /pool/tasks: this task's own description and trajectory navigation card (the only task card)."),
        ("/pool/trajectories/<task_id>/<model_alias>/<trial_id>/<task_id>_<score>.txt:",
         "/pool/trajectories/<task_id>/<model_alias>/<attempt_id>/<task_id>_<score>.txt:"),
        (f"{traces} original training trajectories.",
         f"{traces} original trajectories of officially graded FAILED attempts at THIS task by other AI agents "
         "(official score 0); successful attempts are held out and not included."),
        ("query over training-task\n   descriptions.", "query over the task card\n   (here only this task's own card)."),
        ("investigate selected similar training tasks' files.",
         "investigate the selected task's files (here this task's failed attempts)."),
        ("B. If no similar training task is found,", "B. If no similar task is found,"),
        ("relevant training trajectories again.", "relevant trajectories again."),
    ]
    if initial_memory is None:
        edits.append(("V5 — task-local evidence memory", "V7 — task-local evidence memory"))
    else:
        edits += [
            ("V6 — seeded task-local evidence memory", "V7 — seeded task-local evidence memory"),
            (f"/memory: this task's private copy of {initial_memory['entry_count']} frozen failure lessons "
             "distilled from historical TRAINING trajectories before testing.",
             f"/memory: this task's private copy of {initial_memory['entry_count']} frozen failure-analysis entries "
             "written before testing by another model from this task's failed attempts above."),
            ("The same initial failure-memory library is copied independently into every test task.",
             "This initial failure analysis was written only from this task's own failed attempts and is not "
             "copied into any other test task."),
            ("all original training trajectories remain available for checking source evidence",
             "the original failed trajectories remain available for checking source evidence"),
        ]
    for old, new in edits:
        prompt = _replace_once(prompt, old, new)
    return prompt


def review_instructions_v7():
    """V5/V6 review prompt with only the pool description rewritten for a same-task pool."""
    prompt = (SHARED / "review_prompt.txt").read_text()
    prompt = _replace_once(prompt, "- /pool: the same read-only training-task/trajectory pool.",
                           "- /pool: the same read-only pool of this task's own officially failed attempts.")
    return _replace_once(prompt, "all 432 trajectories", "all pool trajectories")


def make_config(task_id, workspace, stage, pool_scope=None):
    arguments = (SERVER, "mcp", "--pool", "/pool", "--memory", "/memory",
                 "--task-id", task_id, "--stage", stage, "--workspace", workspace)
    if pool_scope is not None:
        arguments += ("--pool-scope", pool_scope)
    server = MCPServer("v5", PYTHON, arguments)
    return render_config(CodexSettings(model=MODEL, reasoning_effort=EFFORT,
        cli_version=CLI_VERSION, binary="/opt/codex/codex", mcp_servers=(server,)))


def codex_command(workspace, output):
    return ["/opt/codex/codex", "exec", "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--cd", workspace,
            "--output-last-message", output, "-"]


async def command(argv, *, timeout=120, check=True, stdout_path=None):
    output = open(stdout_path, "wb") if stdout_path else asyncio.subprocess.PIPE
    try:
        proc = await asyncio.create_subprocess_exec(*map(str, argv), stdout=output, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        if check and proc.returncode:
            # Command errors may include authentication information. Keep the
            # summary generic; never print auth material or full environment.
            raise RuntimeError(f"{argv[0]} operation failed (exit {proc.returncode})")
        return proc.returncode, (stdout or b"").decode(errors="replace"), (stderr or b"").decode(errors="replace")
    finally:
        if stdout_path:
            output.close()


async def clean_private_runtime(root, image):
    """Remove copied auth, also after task setup fails before the freeze hook.

    Task-root-owned bind directories need a narrowly mounted cleanup container;
    the official workspace, host credentials and pool are never mounted here.
    """
    root = Path(root)
    paths = [p for p in (root / "private/solve-home", root / "private/review-home", root / "memory") if p.exists()]
    needs_ownership = any(p.stat().st_uid != os.getuid() for p in paths)
    if not needs_ownership:
        try:
            for home in paths:
                if home.name.endswith("-home"):
                    (home / "auth.json").unlink(missing_ok=True)
            return
        except PermissionError:
            needs_ownership = True
    if needs_ownership:
        script = """
import os, pathlib, sys
uid, gid = map(int, sys.argv[1:3])
for name in sys.argv[3:]:
    root = pathlib.Path(name)
    if root.name.endswith('-home'):
        (root / 'auth.json').unlink(missing_ok=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for child in dirs + files:
            os.chown(pathlib.Path(directory) / child, uid, gid, follow_symlinks=False)
        os.chown(directory, uid, gid, follow_symlinks=False)
"""
        argv = ["docker", "run", "--rm", "--network", "none", "--read-only"]
        targets = []
        for path in paths:
            target = "/cleanup/" + path.name
            targets.append(target)
            argv += ["--mount", f"type=bind,src={path},dst={target}"]
        argv += [image, "python", "-c", script, str(os.getuid()), str(os.getgid()), *targets]
        await command(argv, timeout=60)


def capture_session(home, logs):
    logs = Path(logs)
    logs.mkdir(parents=True, exist_ok=True)
    source = select_main_session(list((Path(home) / "sessions").rglob("*.jsonl")))
    events = logs / "codex.events.jsonl"
    if source:
        shutil.copyfile(source, logs / "codex.session.jsonl")
    source = logs / "codex.session.jsonl" if source else events
    usage = extract_usage(source, cumulative=source.name == "codex.session.jsonl")
    convert_transcript(source, logs / "transcript.openclaw.jsonl")
    models, efforts = set(), set()
    for event in read_jsonl(source):
        if event.get("type") == "turn_context":
            payload = event.get("payload", {})
            models.add(payload.get("model"))
            efforts.add(payload.get("effort", payload.get("reasoning_effort")))
    audit = {"models": sorted(models, key=str), "efforts": sorted(efforts, key=str),
             "model_effort_verified": models == {MODEL} and efforts == {EFFORT},
             "initial_distilled_skills": 0, "global_memory": False, "tools": TOOLS}
    write_json(logs / "usage.json", usage)
    write_json(logs / "model_audit.json", audit)
    return usage, audit


class TerminalBenchV5Agent(BaseAgent):
    """Harbor BaseAgent using the project's pinned native Codex binary."""

    def __init__(self, *args, run_dir, pool_path, task_id, condition=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_dir, self.pool = Path(run_dir), Path(pool_path)
        self.task_id = task_id
        # None keeps V5/V6 behaviour; 'v7' selects the same-task pool scope and prompt text.
        self.condition = condition
        self.workspace = None
        self.private_home = self.run_dir / "private/solve-home"

    @property
    def pool_scope(self):
        return "same_task" if self.condition == "v7" else None

    @staticmethod
    def name():
        return "terminal-bench-v5-codex"

    def version(self):
        return CLI_VERSION

    async def setup(self, environment):
        if self.mcp_servers or self.skills_dir:
            raise ValueError("Task-supplied MCP/skills need an explicit protocol review; never silently discard them")
        where = await environment.exec("pwd", timeout_sec=10)
        self.workspace = (where.stdout or "").strip()
        if not self.workspace.startswith("/") or "\n" in self.workspace:
            raise ValueError("Task has no unambiguous absolute working directory")
        (self.private_home / "config.toml").write_text(make_config(self.task_id, self.workspace, "solve",
                                                                   pool_scope=self.pool_scope))
        identity = await environment.exec("id -u; id -g", timeout_sec=10)
        uid, gid = [int(v) for v in identity.stdout.splitlines()]
        ready = await environment.exec(
            f"chown -R {uid}:{gid} {HOME} /memory && chmod 700 {HOME} && chmod 600 {HOME}/auth.json",
            timeout_sec=30, user="root")
        if ready.return_code:
            raise RuntimeError("Cannot prepare task-private Codex home/memory")
        version = await environment.exec("/opt/codex/codex --version", timeout_sec=20,
                                         env={"CODEX_HOME": HOME})
        if version.return_code or version.stdout.strip() != f"codex-cli {CLI_VERSION}":
            raise RuntimeError("Pinned Codex CLI version mismatch")
        python_probe = await environment.exec(f"{PYTHON} -c 'import sys; assert sys.version_info[:2] == (3, 12)'", timeout_sec=20)
        if python_probe.return_code:
            raise RuntimeError("Standalone Python cannot run in this task image")
        initial = json.loads((self.run_dir / "memory_initial.json").read_text())
        seeded = initial if initial.get('seed_manifest_sha256') else None
        instructions = solver_instructions_v7 if self.condition == "v7" else solver_instructions
        (self.run_dir / "control/solver_prompt.txt").write_text(
            instructions(self.pool, self.workspace, initial_memory=seeded))
        baseline = await environment.exec(f"{PYTHON} -c " + shlex.quote(
            "import runpy,json; m=runpy.run_path('/opt/v5/serve_memory.py'); print(json.dumps(m['processes']()))"), timeout_sec=15)
        if baseline.return_code:
            raise RuntimeError("Cannot record initial task process baseline")
        write_json(self.run_dir / "control/baseline.json", json.loads(baseline.stdout))
        write_json(self.logs_dir / "setup.json", {"workspace": self.workspace, "cli_version": CLI_VERSION,
                   "model": MODEL, "reasoning_effort": EFFORT, "initial_entries": initial['entry_count'],
                   "seed_manifest_sha256": initial.get('seed_manifest_sha256'),
                   "task_environment_python_path_unchanged": True})

    async def run(self, instruction, environment, context: AgentContext):
        text = (self.run_dir / "control/solver_prompt.txt").read_text()
        text += "\n\n## Original Terminal-Bench task\n\n" + instruction
        (self.run_dir / "control/solver_prompt.txt").write_text(text)
        (self.run_dir / "control/task.txt").write_text(instruction)
        deadline = json.loads((self.run_dir / "runtime_contract.json").read_text())["solver_timeout_seconds"]
        text = f"The official solving deadline for this task is {deadline:g} seconds, including online memory work.\n\n" + text
        (self.run_dir / "control/solver_prompt.txt").write_text(text)
        log_dir = str(self.environment_logs_dir)
        argv = [PYTHON, SERVER, "supervise", "--prompt", CONTROL + "/solver_prompt.txt",
                "--events", log_dir + "/codex.events.jsonl", "--stderr", log_dir + "/codex.stderr.log",
                "--timeout", str(deadline), "--", *codex_command(self.workspace, log_dir + "/final.txt")]
        started = time.monotonic()
        status, code = "cancelled", None
        try:
            result = await environment.exec('export PATH="$PATH:/opt/v5-bin"; ' + shlex.join(argv), cwd=self.workspace,
                env={"CODEX_HOME": HOME}, timeout_sec=None)
            code = result.return_code
            status = "completed" if code == 0 else "timeout" if code == 124 else "execution_error"
            if code == 124:
                raise asyncio.TimeoutError("Pinned solver deadline exceeded")
            if code:
                raise NonZeroAgentExitCodeError(f"Codex exited {code}")
        finally:
            elapsed = time.monotonic() - started
            write_json(self.logs_dir / "execution.json", {"status": status, "exit_code": code,
                       "elapsed_seconds": elapsed, "model": MODEL, "reasoning_effort": EFFORT})
            context.metadata = {"model": MODEL, "reasoning_effort": EFFORT, "status": status}

    def populate_context_post_run(self, context):
        usage_file = self.logs_dir / "usage.json"
        if usage_file.exists():
            usage = json.loads(usage_file.read_text())
            context.n_input_tokens = usage.get("input_tokens")
            context.n_output_tokens = usage.get("output_tokens")
            context.n_cache_tokens = usage.get("cached_input_tokens")


def forbidden_evidence(path):
    p = PurePosixPath(path)
    if ".." in p.parts:
        return True
    prefixes = ("/pool", "/memory", "/tests", "/logs/verifier", HOME, CONTROL,
                "/opt/codex", "/opt/v5", "/opt/v5-python", "/proc", "/sys", "/dev", "/run")
    return any(p == PurePosixPath(x) or PurePosixPath(x) in p.parents for x in prefixes) or any(
        part in {".codex", ".ssh", ".aws", ".azure", ".kube", "auth.json"} for part in p.parts)


def extract_public_archive(archive, destination, original_root, *, regular_file_only=False):
    """Copy regular files only. Never follow source symlinks or expose credentials."""
    destination = Path(destination)
    copied = 0
    with tarfile.open(archive) as stream:
        for member in stream:
            # docker cp archives include the requested basename once.
            relative = PurePosixPath(member.name)
            parts = relative.parts[1:]
            if regular_file_only and parts:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe path in container snapshot")
            original = PurePosixPath(original_root).joinpath(*parts)
            if forbidden_evidence(original) or not member.isfile():
                continue
            target = destination.joinpath(*parts) if parts else destination
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            copied += 1
    return copied


async def docker_snapshot(container_id, source, destination, *, regular_file_only=False):
    with tempfile.TemporaryDirectory(prefix="tb21-snapshot-") as temporary:
        archive = Path(temporary) / "snapshot.tar"
        code, _, _ = await command(["docker", "cp", f"{container_id}:{source}", "-"],
                                     timeout=600, check=False, stdout_path=archive)
        if code:
            return {"path": source, "status": "absent_or_copy_failed"}
        count = extract_public_archive(archive, destination, source, regular_file_only=regular_file_only)
        return {"path": source, "status": "copied", "files": count}


async def freeze_and_snapshot(trial):
    environment, agent = trial.agent_environment, trial.agent
    if not hasattr(environment, "_run_docker_compose_command"):
        raise NotImplementedError("This snapshot adapter requires Docker Compose; supply a reviewed provider adapter")
    frozen = await environment.exec(f"{PYTHON} {SERVER} freeze {CONTROL}/baseline.json", timeout_sec=30, user="root")
    if frozen.return_code:
        raise RuntimeError("Cannot stop solver processes before evidence freezing")
    # Restore ownership of private bind trees, never the task service/workspace.
    await environment.exec(f"chown -R {os.getuid()}:{os.getgid()} {HOME} /memory", timeout_sec=60, user="root")
    ids = await environment._run_docker_compose_command(["ps", "--all", "--quiet"], timeout_sec=30)
    container_ids = ids.stdout.split()
    if not container_ids:
        raise RuntimeError("No task containers to freeze")
    _, info, _ = await command(["docker", "inspect", *container_ids])
    containers = json.loads(info)
    by_service = {c["Config"]["Labels"].get("com.docker.compose.service"): c for c in containers}
    main = by_service[MAIN_SERVICE_NAME]
    workspace = agent.workspace
    changed_files = []
    if workspace in ("/", "/root", "/home", "/tmp", "/opt", "/usr", "/var"):
        _, diff, _ = await command(["docker", "diff", main["Id"]])
        candidates = []
        for line in diff.splitlines():
            kind, _, path = line.partition(" ")
            if kind in ("A", "C") and not forbidden_evidence(path) and not path.startswith(
                    ("/usr/", "/var/", "/etc/", "/bin/", "/lib/", "/sbin/")):
                candidates.append(path)
        # Determine regular files without pausing. Do not recursively archive
        # a changed system directory (e.g. /root) on the fallback path.
        probe = "import json,os,stat,sys; print(json.dumps([p for p in json.loads(sys.argv[1]) if os.path.lexists(p) and stat.S_ISREG(os.lstat(p).st_mode)]))"
        found = await environment.exec(shlex.join([PYTHON, "-c", probe, json.dumps(candidates)]), timeout_sec=30, user="root")
        if found.return_code:
            raise RuntimeError("Cannot identify visible changed task files")
        changed_files = json.loads(found.stdout)
    # Never pause live services: wall clocks advance during Docker pause and
    # benchmark liveness/latency checks would observe an artificial outage.
    capture_started = time.time()
    evidence = agent.run_dir / "evidence"
    evidence.mkdir()
    records = []
    try:
        capture_session(agent.private_home, agent.logs_dir)
        shutil.copyfile(agent.logs_dir / "transcript.openclaw.jsonl", evidence / "transcript.jsonl")
        shutil.copyfile(agent.run_dir / "control/task.txt", evidence / "task.txt")
        if workspace not in ("/", "/root", "/home", "/tmp", "/opt", "/usr", "/var"):
            records.append(await docker_snapshot(main["Id"], workspace, evidence / "workspace"))
        else:
            # Broad system directories are not review inputs. Capture changed
            # task files individually; their original paths are recorded.
            for path in changed_files:
                records.append(await docker_snapshot(main["Id"], path,
                    evidence / "workspace" / path.lstrip("/"), regular_file_only=True))
        for item in trial.task.config.artifacts:
            source = item if isinstance(item, str) else item.source
            service = MAIN_SERVICE_NAME if isinstance(item, str) else item.service or MAIN_SERVICE_NAME
            path = str(PurePosixPath(workspace) / source) if not source.startswith("/") else source
            if forbidden_evidence(path):
                records.append({"path": path, "status": "excluded_private_or_grading_path"})
                continue
            container = by_service.get(service)
            if container:
                records.append(await docker_snapshot(container["Id"], path,
                    evidence / "artifacts" / service / path.lstrip("/")))
        # Harbor's publish directory is a public artifact, unlike verifier logs.
        records.append(await docker_snapshot(main["Id"], "/logs/artifacts", evidence / "published"))
        write_json(evidence / "snapshot_manifest.json", {"workspace": workspace, "paths": records,
                   "symlinks_followed": False, "taken_before_hidden_grading": True,
                   "services_paused": [], "task_services_kept_running": True,
                   "snapshot_consistency": "individual file copies while task services remain live",
                   "capture_started_at": capture_started, "capture_finished_at": time.time(),
                   "files_sha256": hash_tree(evidence)})
        write_json(agent.run_dir / "freeze.json", {"stopped_solver_processes": json.loads(frozen.stdout),
                   "services_paused": [], "services_preserved": sorted(by_service),
                   "task_services_kept_running": True, "evidence_manifest": "evidence/snapshot_manifest.json",
                   "taken_before_hidden_grading": True})
        return evidence, []
    except BaseException:
        raise


async def review_frozen_memory(agent, evidence, *, python_root, auth_file, review_image):
    root = agent.run_dir
    review = root / "review"
    review.mkdir()
    memory = review / "memory"
    shutil.copytree(root / "memory", memory)
    (memory / "final_decision.json").unlink(missing_ok=True)
    home = root / "private/review-home"
    home.mkdir(mode=0o700)
    shutil.copyfile(auth_file, home / "auth.json")
    os.chmod(home / "auth.json", 0o600)
    (home / "config.toml").write_text(make_config(agent.task_id, "/review", "review", pool_scope=agent.pool_scope))
    if agent.condition == "v7":
        prompt = review_instructions_v7()
    else:
        prompt = (SHARED / "review_prompt.txt").read_text().replace("all 432 trajectories", "all training trajectories")
    prompt += "\nAdditional frozen artifacts are indexed by /evidence/snapshot_manifest.json. Follow only relevant paths.\n"
    (review / "prompt.txt").write_text(prompt)
    mounts = support_mounts(agent.pool, memory, home, root / "control", python_root)
    mounts += [bind(evidence, "/evidence"), bind(review, "/review", False),
               bind(BASE / "prepared/transport/ca-certificates.crt", "/opt/v5-ca/ca-certificates.crt")]
    name = "tb21-v5-review-" + uuid.uuid4().hex[:16]
    argv = ["docker", "run", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}",
            "--init", "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
            "--workdir", "/review", "--env", "CODEX_HOME=" + HOME,
            "--env", "SSL_CERT_FILE=/opt/v5-ca/ca-certificates.crt",
            "--env", "CODEX_CA_CERTIFICATE=/opt/v5-ca/ca-certificates.crt"]
    for mount in mounts:
        argv += ["--mount", "type=bind,src=" + mount["source"] + ",dst=" + mount["target"] +
                 (",readonly" if mount.get("read_only") else "")]
    argv += [review_image, "bash", "-c", 'export PATH="$PATH:/opt/v5-bin"; exec "$@"', "v5-review",
             PYTHON, SERVER, "supervise", "--prompt", "/review/prompt.txt",
             "--events", "/review/codex.events.jsonl", "--stderr", "/review/codex.stderr.log",
             "--timeout", str(REVIEW_SECONDS), "--", *codex_command("/review", "/review/final.txt")]
    before = hash_tree(evidence)
    started = time.monotonic()
    code, error = None, None
    try:
        code, _, _ = await command(argv, timeout=REVIEW_SECONDS + 30, check=False)
    except Exception as exc:
        error = type(exc).__name__
    finally:
        await command(["docker", "rm", "--force", name], check=False)
        (home / "auth.json").unlink(missing_ok=True)
    usage, model = capture_session(home, review)
    audit = audit_memory(memory)
    decision = audit.get("final_decision")
    unchanged = before == hash_tree(evidence)
    result = {"exit_code": code, "error": error, "elapsed_seconds": time.monotonic() - started,
              "timeout_seconds": REVIEW_SECONDS, "usage": usage, "model_audit": model,
              "memory_audit": audit, "evidence_unchanged": unchanged,
              "received_hidden_grade": False, "contributes_to_solver_score": False,
              "phase": "after_official_grading_using_pre_grader_snapshot",
              "valid": code == 0 and audit["valid"] and model["model_effort_verified"] and unchanged and bool(decision)
                       and decision.get("agent_stage") == "review"}
    write_json(root / "memory_review.json", result)
    return result


def support_mounts(pool, memory, home, control, python_root):
    rg = BASE / "runtime/bin/rg"
    if not rg.is_file() or not os.access(rg, os.X_OK):
        raise FileNotFoundError("Pinned static ripgrep binary is required for the DCI search tools")
    return [bind(pool, "/pool"), bind(memory, "/memory", False), bind(home, HOME, False),
            bind(control, CONTROL), bind(BASE / "runtime/bin", "/opt/codex"),
            bind(python_root, "/opt/v5-python"), bind(SHARED / "tools.py", "/opt/v5/tools.py"),
            bind(Path(__file__).with_name("serve_memory.py"), SERVER), bind(rg, "/opt/v5-bin/rg")]


async def run_trial(task_path, trial_dir, pool_path, task_id, *, python_root=None,
                    auth_file=None, review_image="python:3.12-slim-bookworm", environment_config=None,
                    keep_environment=False, verifier_policy="official", memory_seed=None, condition=None):
    """Execute ONE fresh official task; callers own scheduling, split and consent.

    Returns a JSON-compatible dict with Harbor result and separate review data.
    No retries. Existing trial directories are refused. Provider overrides are
    accepted, but evidence capture requires a Compose-compatible adapter.
    GPU requirements remain intact; unsupported providers fail rather than
    silently running a GPU task on CPU.

    condition None keeps the V5/V6 inference from memory_seed. 'v7' replaces only the corpus:
    the pool must hold exactly this task's own failed official attempts, and the optional seed is
    a same-task failure analysis.
    """
    condition = condition or ("v6" if memory_seed is not None else "v5")
    if condition not in ("v5", "v6", "v7"):
        raise ValueError("Unknown runtime condition")
    task_path, trial_dir, pool = map(lambda p: Path(p).resolve(), (task_path, trial_dir, pool_path))
    if trial_dir.exists():
        raise FileExistsError("Refuse to overwrite or rerun an existing trial")
    if not (pool / "manifest.json").is_file() or not (pool / "tasks").is_dir():
        raise ValueError("Prepared build-only pool is incomplete")
    pool_record = None
    if condition == "v7":
        from .v7_corpus import verify_task_pool
        pool_record = verify_task_pool(pool, task_id)
    elif (pool / "trajectories" / task_id).exists():
        raise ValueError("Test task occurs in training pool")
    python_root = Path(python_root or python_installation()).resolve()
    auth_file = Path(auth_file or os.environ.get("CODEX_AUTH_FILE", "~/.codex/auth.json")).expanduser().resolve()
    if verifier_policy not in {"official", "unlimited"}:
        raise ValueError("Unknown verifier policy")
    task_spec = tomllib.loads((task_path / "task.toml").read_text())
    if task_spec.get("steps"):
        raise NotImplementedError("Multi-step tasks need per-step freeze/review semantics")
    solver_timeout = task_spec.get("agent", {}).get("timeout_sec")
    if not isinstance(solver_timeout, (float, int)) or solver_timeout <= 0:
        raise ValueError("Task must provide its official finite agent timeout")
    trial_dir.mkdir(parents=True)
    (trial_dir / "control").mkdir()
    home = trial_dir / "private/solve-home"
    home.mkdir(parents=True, mode=0o700)
    initial = None
    if condition == "v7" and memory_seed is not None:
        from .v7_seed import initialize_same_task_memory
        initial = initialize_same_task_memory(trial_dir / "memory", task_id, memory_seed, pool=pool)
    elif memory_seed is None:
        initialize_memory(trial_dir / "memory", task_id)
        initial = audit_memory(trial_dir / "memory")
    else:
        from experiment.shared.memory.seed import initialize_seeded_memory
        initial = initialize_seeded_memory(trial_dir / "memory", task_id, memory_seed,
            pool=pool, training_task_ids=json.loads((pool / 'manifest.json').read_text())['training_task_ids'])
    write_json(trial_dir / "memory_initial.json", initial)
    write_json(trial_dir / "runtime_contract.json", {"model": MODEL, "reasoning_effort": EFFORT,
        "cli_version": CLI_VERSION, "harbor_version": "0.23.0", "solver_timeout_seconds": solver_timeout,
        "review_timeout_seconds": REVIEW_SECONDS,
        "official_verifier_timeout_seconds": task_spec["verifier"]["timeout_sec"],
        "verifier_timeout_seconds": None if verifier_policy == "unlimited" else task_spec["verifier"]["timeout_sec"],
        "verifier_timeout_policy": verifier_policy,
        "review_order": "after_official_grading_using_pre_grader_snapshot",
        "submission_boundary": "stop Codex control processes; preserve initial and task-created services",
        "task_services_paused": False, "snapshot_atomic_across_services": False,
        "task_id": task_id, "pool_manifest_sha256": hashlib.sha256((pool / "manifest.json").read_bytes()).hexdigest(),
        "cross_task_memory": False, "initial_skills": 0, "tool_names": TOOLS,
        "condition": condition,
        "initial_memory_entries": initial['entry_count'],
        "seed_manifest_sha256": initial.get('seed_manifest_sha256')})
    if condition == "v7":
        contract = json.loads((trial_dir / "runtime_contract.json").read_text())
        contract.update({"pool_scope": "same_task", "pool_trajectory_count": pool_record["trajectory_count"],
                         "pool_files_sha256_digest": hashlib.sha256(json.dumps(
                             pool_record["files_sha256"], sort_keys=True).encode()).hexdigest()})
        write_json(trial_dir / "runtime_contract.json", contract)
    config = dict(environment_config or {})
    if config.get("mounts"):
        raise ValueError("Additional arbitrary host mounts require a separate reviewed adapter")
    if not config.get("import_path"):
        config.setdefault("type", "docker")
    config["delete"] = not keep_environment
    config["mounts"] = support_mounts(pool, trial_dir / "memory", home, trial_dir / "control", python_root)
    trial = None
    review_result = None
    frozen_evidence = None
    try:
        shutil.copyfile(auth_file, home / "auth.json")
        os.chmod(home / "auth.json", 0o600)
        # Resolve the review image before starting the solver timer.
        probe, _, _ = await command(["docker", "image", "inspect", review_image], check=False)
        if probe:
            await command(["docker", "pull", review_image], timeout=600)
        _, image_id, _ = await command(["docker", "image", "inspect", "--format", "{{.Id}}", review_image])
        review_image = image_id.strip()
        contract = json.loads((trial_dir / "runtime_contract.json").read_text())
        contract["review_image_id"] = review_image
        write_json(trial_dir / "runtime_contract.json", contract)
        agent_kwargs = {"run_dir": str(trial_dir), "pool_path": str(pool), "task_id": task_id}
        if condition == "v7":
            agent_kwargs["condition"] = condition
        trial = await Trial.create(TrialConfig(
            task=TaskConfig(path=task_path), trial_name=trial_dir.name, trials_dir=trial_dir.parent,
            agent=AgentConfig(import_path=__name__ + ":TerminalBenchV5Agent", model_name=MODEL,
                              kwargs=agent_kwargs),
            environment=EnvironmentConfig(**config), verifier=VerifierConfig()))

        if trial._agent_timeout_sec != solver_timeout or trial._verifier_timeout_sec != task_spec["verifier"]["timeout_sec"]:
            raise ValueError("Effective Harbor task deadlines differ from official limits")
        if verifier_policy == "unlimited":
            trial._verifier_timeout_sec = None

        async def agent_end(_event):
            nonlocal review_result, frozen_evidence
            try:
                frozen_evidence, _ = await freeze_and_snapshot(trial)
            except Exception as exc:
                review_result = {"valid": False, "error": type(exc).__name__, "received_hidden_grade": False,
                                 "reason": "Pre-grader evidence snapshot failed; review was not run"}
                write_json(trial_dir / "memory_review.json", review_result)

        trial.add_hook(TrialEvent.AGENT_END, agent_end)
        result = await trial.run()
        # Harbor collects artifacts and grades without waiting for review.
        # This review container has no mount of the result, verifier logs or
        # task environment; its only execution evidence predates grading.
        if frozen_evidence is not None:
            try:
                review_result = await review_frozen_memory(trial.agent, frozen_evidence,
                    python_root=python_root, auth_file=home / "auth.json", review_image=review_image)
            except Exception as exc:
                review_result = {"valid": False, "error": type(exc).__name__, "received_hidden_grade": False}
                write_json(trial_dir / "memory_review.json", review_result)
        await clean_private_runtime(trial_dir, review_image)
        output = {"task_id": task_id, "harbor": result.model_dump(mode="json"), "review": review_result,
                  "memory": audit_memory(trial_dir / "memory"),
                  "freeze": json.loads((trial_dir / "freeze.json").read_text()) if (trial_dir / "freeze.json").exists() else None}
        write_json(trial_dir / f"{condition}_result.json", output)
        return output
    finally:
        await clean_private_runtime(trial_dir, review_image)
