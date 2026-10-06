"""Project-scoped Docker runtime with a hard solver/verifier lifecycle boundary.

No models or containers are started on import. All host writes stay under
``project_root``. The caller supplies a prepared image and an isolated auth file.
"""
from __future__ import annotations

import json
import base64
import hashlib
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


PREFIX = "hangxiao-skill-dci"
OWNER_LABEL = "org.hangxiao.skill-dci.owner"
WORKSPACE = "/tmp_workspace"
CODEX_HOME = "/root/.codex"


def contained(path: Path | str, root: Path | str) -> Path:
    resolved, boundary = Path(path).resolve(), Path(root).resolve()
    if not resolved.is_relative_to(boundary):
        raise ValueError(f"Path is outside the project boundary: {path}")
    return resolved


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    category: str
    prompt: str
    workspace_path: Path
    skills_root: Path
    skills: tuple[str, ...] = ()
    warmup: str = ""
    env_names: tuple[str, ...] = ()
    timeout_seconds: int = 1800
    automated_checks: str = field(default="", repr=False)

    @property
    def exec_dir(self) -> Path:
        return self.workspace_path / "exec"

    @property
    def gt_dir(self) -> Path:
        return self.workspace_path / "gt"


def load_task_spec(task_file: Path, repo_root: Path, *, workspace_root: Path | None = None,
                   timeout_seconds: int | None = None) -> TaskSpec:
    """Parse official sections on the controller only; never pass the raw MD.

    ``workspace_root`` can replace the official repository's ``workspace/``.
    Parsing matches the frozen official task_parser.py section semantics.
    """
    import yaml  # The official runner also requires PyYAML.

    task_file = contained(task_file, repo_root)
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", task_file.read_text(), re.S)
    if not match:
        raise ValueError("Task has no YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError("Task metadata must be a mapping")
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in match.group(2).splitlines():
        heading = re.match(r"^##\s+(.+)$", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current, lines = heading.group(1), []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()

    def section(name: str) -> str:
        value = sections.get(name, "").strip()
        value = re.sub(r"^```[^\n]*\n?", "", value)
        return re.sub(r"\n?```$", "", value).strip()

    raw_workspace = Path(section("Workspace Path"))
    if not section("Workspace Path") or not sections.get("Prompt"):
        raise ValueError("Task must have Prompt and Workspace Path sections")
    if workspace_root is not None:
        parts = raw_workspace.parts
        if raw_workspace.is_absolute() or not parts or parts[0] != "workspace":
            raise ValueError("Workspace relocation requires an official workspace/... path")
        workspace = contained(Path(workspace_root).joinpath(*parts[1:]), workspace_root)
    else:
        workspace = contained(repo_root / raw_workspace, repo_root)

    def names(value: str) -> tuple[str, ...]:
        return tuple(s.strip() for s in value.splitlines() if s.strip() and not s.strip().startswith("#"))

    return TaskSpec(
        task_id=str(metadata.get("id", task_file.stem)), category=task_file.parent.name,
        prompt=sections["Prompt"], workspace_path=workspace, skills_root=repo_root / "skills",
        skills=names(section("Skills")), warmup=section("Warmup"), env_names=names(section("Env")),
        timeout_seconds=int(timeout_seconds if timeout_seconds is not None else metadata.get("timeout_seconds", 120)),
        automated_checks=section("Automated Checks"),
    )


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    readonly: bool = True


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    image: str
    docker_host: str | None = None
    user: str | None = None
    network: str = "none"
    auth_file: Path | None = field(default=None, repr=False)
    auth_readonly: bool = True
    support_mounts: tuple[Mount, ...] = ()
    startup_commands: tuple[str, ...] = ()
    warmup_override: str | None = field(default=None, repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    labels: Mapping[str, str] = field(default_factory=dict)
    readonly_root: bool = True
    memory_limit: str = "16g"
    cpus: float = 4.0
    grading_timeout_seconds: int = 600
    before_freeze_hook: Callable[["TaskRuntime"], Any] | None = field(default=None, repr=False)
    after_restart_hook: Callable[["TaskRuntime"], Any] | None = field(default=None, repr=False)


class RuntimeErrorWithStage(RuntimeError):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")


class TaskRuntime:
    """One disposable, owner-labelled container and one private run directory.

    State flow: new -> prepared -> solver_stopped -> grading -> graded.
    ``freeze_agent`` stops the *whole* container, including escaped background
    processes. Only its inert entrypoint is restarted before grading; warmup is
    not rerun because it may reset task state. A caller may provide a reviewed
    ``grading_setup`` to restore a stateful mock service without resetting data.
    """
    def __init__(self, config: RuntimeConfig, task: TaskSpec, run_dir: Path, run_id: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,90}", run_id):
            raise ValueError("run_id must be a short lowercase Docker-safe identifier")
        if config.network == "host" or config.network.startswith("container:"):
            raise ValueError("Host and foreign-container networking are prohibited")
        self.config, self.task = config, task
        self.run_dir = contained(run_dir, config.project_root)
        if self.run_dir == Path(config.project_root).resolve():
            raise ValueError("run_dir must be a private project subdirectory")
        self.name = f"{PREFIX}-{run_id}"
        self.workspace = self.run_dir / "workspace"
        self.codex_home = self.run_dir / "codex-home"
        self.staging = self.run_dir / "controller-staging"
        self.state = "new"
        self.created = False
        self.freeze_hook_result: Any = None
        self.freeze_hook_error: str | None = None
        self._freeze_hook_attempted = False
        self._check_paths()

    def _check_paths(self) -> None:
        contained(self.task.workspace_path, self.config.project_root)
        contained(self.task.skills_root, self.config.project_root)
        for mount in self.config.support_mounts:
            contained(mount.source, self.config.project_root)
            target = PurePosixPath(mount.target)
            if not target.is_absolute() or ".." in target.parts or str(target) in ("/", "/proc", "/sys", "/dev", "/root"):
                raise ValueError("Unsafe support mount destination")
            if not mount.readonly:
                contained(mount.source, self.run_dir)
            if Path(mount.source).is_socket():
                raise ValueError("Host sockets must not be mounted into the solver")
        if self.config.auth_file is not None:
            contained(self.config.auth_file, self.config.project_root)
            if not self.config.auth_readonly:
                contained(self.config.auth_file, self.run_dir)
        for key in self.config.environment:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("Invalid environment variable name")

    def docker_command(self, *args: str) -> list[str]:
        command = ["docker"]
        if self.config.docker_host:
            command += ["--host", self.config.docker_host]
        return command + list(args)

    def docker(self, *args: str, input_text: str | None = None, timeout: float = 120,
               check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(self.docker_command(*args), input=input_text, capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self._record_docker_failure(args[0], error.stdout, error.stderr)
            raise
        if result.returncode:
            self._record_docker_failure(args[0], result.stdout, result.stderr)
            if check:
                # Never echo raw command arguments, environment, or stderr.
                raise RuntimeErrorWithStage(self.state, f"Docker {args[0]} failed (exit {result.returncode}); inspect private logs")
        return result

    def _record_docker_failure(self, operation: str, stdout: Any, stderr: Any) -> None:
        self.staging.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^a-z0-9_-]", "_", operation.lower()) + "-" + str(time.time_ns())
        for suffix, content in (("stdout", stdout), ("stderr", stderr)):
            path = self.staging / f"docker-{stem}.{suffix}.log"
            with path.open("x", encoding="utf-8") as stream:
                os.chmod(path, 0o600)
                stream.write(content.decode(errors="replace") if isinstance(content, bytes) else str(content or ""))

    def _assert_owned(self) -> None:
        result = self.docker("inspect", "--format", '{{ index .Config.Labels "' + OWNER_LABEL + '" }}', self.name)
        if result.stdout.strip() != PREFIX:
            raise RuntimeErrorWithStage(self.state, "Refusing to mutate an unowned container")

    @staticmethod
    def _mount_arg(source: Path, target: str, readonly: bool) -> str:
        if "," in str(source) or "," in target:
            raise ValueError("Comma in Docker mount path is unsupported")
        return f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")

    def prepare(self, config_text: str) -> None:
        if self.state != "new":
            raise RuntimeErrorWithStage(self.state, "Runtime was already prepared")
        if not self.task.exec_dir.is_dir():
            raise RuntimeErrorWithStage("prepare", "Missing task exec inputs")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        self.workspace.mkdir(exist_ok=False)
        self.codex_home.mkdir(exist_ok=False)
        self.staging.mkdir(exist_ok=False)
        shutil.copytree(self.task.exec_dir, self.workspace, dirs_exist_ok=True, symlinks=True)
        task_tmp = self.task.workspace_path / "tmp"
        if task_tmp.is_dir():
            contained(task_tmp, self.task.workspace_path)
            shutil.copytree(task_tmp, self.workspace / "tmp", dirs_exist_ok=True, symlinks=True)
        (self.codex_home / "config.toml").write_text(config_text)
        skill_dest = self.codex_home / "skills"
        skill_dest.mkdir()
        used: set[str] = set()
        for skill in self.task.skills:
            source = contained(self.task.skills_root / skill, self.task.skills_root)
            leaf = source.name
            if leaf in used or not source.is_dir():
                raise RuntimeErrorWithStage("prepare", "Missing or colliding task skill")
            used.add(leaf)
            shutil.copytree(source, skill_dest / leaf, symlinks=True)
        cache_dir = self.run_dir / "cache"
        cache_dir.mkdir()
        openclaw_home = self.run_dir / "openclaw-home"
        openclaw_home.mkdir()
        browser_pki = self.run_dir / "browser-pki"
        browser_pki.mkdir()
        command = ["run", "--detach", "--name", self.name,
                   "--label", f"{OWNER_LABEL}={PREFIX}", "--label", f"org.hangxiao.skill-dci.task={self.task.task_id}",
                   "--network", self.config.network, "--memory", self.config.memory_limit,
                   "--cpus", str(self.config.cpus), "--pids-limit", "1024",
                   "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
                   "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g", "--tmpfs", "/run:rw,nosuid,nodev,size=64m",
                   "--tmpfs", "/var/tmp:rw,nosuid,nodev,size=512m"]
        if self.config.user is not None:
            command += ["--user", self.config.user]
        if self.config.readonly_root:
            command += ["--read-only"]
        for key, value in self.config.labels.items():
            if not key.startswith("org.hangxiao.skill-dci.") or key in (OWNER_LABEL, "org.hangxiao.skill-dci.task"):
                raise ValueError("Labels must be in the project namespace and may not replace ownership")
            command += ["--label", f"{key}={value}"]
        mounts = [Mount(self.workspace, WORKSPACE, False), Mount(self.codex_home, CODEX_HOME, False),
                  Mount(cache_dir, "/root/.cache", False), Mount(openclaw_home, "/root/.openclaw", False),
                  Mount(browser_pki, "/root/.pki", False),
                  *self.config.support_mounts]
        if self.config.auth_file is not None:
            if not self.config.auth_file.is_file():
                raise RuntimeErrorWithStage("prepare", "Caller-provided auth file is absent")
            mounts.append(Mount(self.config.auth_file, f"{CODEX_HOME}/auth.json", self.config.auth_readonly))
        targets: set[str] = set()
        for mount in mounts:
            if mount.target in targets:
                raise ValueError("Duplicate mount target")
            targets.add(mount.target)
            command += ["--mount", self._mount_arg(Path(mount.source).resolve(), mount.target, mount.readonly)]
        env = {"XDG_CACHE_HOME": "/root/.cache", "PIP_CACHE_DIR": "/root/.cache/pip",
               "npm_config_cache": "/root/.cache/npm", **self.config.environment}
        for key, value in env.items():
            command += ["--env", f"{key}={value}"]
        # Override an image's entrypoint so restarting cannot restart the solver.
        command += ["--entrypoint", "/bin/sh", self.config.image, "-c", "exec sleep infinity"]
        self.docker(*command)
        self.created = True
        self.state = "prepared"
        self._save_state()
        self.run_startup_commands()
        self.run_warmup(self.task.warmup if self.config.warmup_override is None else self.config.warmup_override)

    def _save_state(self) -> None:
        (self.run_dir / "runtime_state.json").write_text(json.dumps({
            "container": self.name, "state": self.state, "task_id": self.task.task_id,
            "image": self.config.image, "docker_host": self.config.docker_host,
            "network": self.config.network, "updated_at_unix": time.time(),
            "original_warmup_sha256": hashlib.sha256(self.task.warmup.encode()).hexdigest(),
            "warmup_override_sha256": (hashlib.sha256(self.config.warmup_override.encode()).hexdigest()
                                       if self.config.warmup_override is not None else None),
        }, indent=2))

    def exec(self, argv: Sequence[str], *, input_text: str | None = None, timeout: float = 120,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        if self.state not in ("prepared", "grading"):
            raise RuntimeErrorWithStage(self.state, "Container execution is not allowed in this phase")
        return self.docker("exec", "--interactive", "--workdir", WORKSPACE, self.name, *argv,
                           input_text=input_text, timeout=timeout, check=check)

    def copy_file_to_container(self, source: Path, target: str) -> None:
        """Write verifier bytes through exec stdin; Docker cp rejects RO roots.

        Only fixed verifier scratch locations are permitted, and no target
        symlink is followed. The solver must already have been stopped.
        """
        if self.state != "grading":
            raise RuntimeErrorWithStage(self.state, "Verifier files require the grading phase")
        if Path(source).is_symlink():
            raise ValueError("Verifier copy requires a regular controller file")
        source = contained(source, self.config.project_root)
        destination = PurePosixPath(target)
        shim = "/root/.openclaw/agents/main/sessions/chat.jsonl"
        if ".." in destination.parts or not (destination.parent == PurePosixPath("/tmp") or target == shim):
            raise ValueError("Verifier copy destination is outside approved scratch paths")
        if not source.is_file() or source.is_symlink():
            raise ValueError("Verifier copy requires a regular controller file")
        script = ("import base64,os,sys; "
                  "data=base64.b64decode(sys.stdin.buffer.read(),validate=True); "
                  "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600); "
                  "stream=os.fdopen(fd,'wb'); stream.write(data); stream.close()")
        self.exec(["python3", "-c", script, target],
                  input_text=base64.b64encode(source.read_bytes()).decode("ascii"), timeout=120)

    def run_startup_commands(self) -> None:
        """Restore trusted infrastructure (e.g. a Unix-socket HTTP relay).

        Commands must not reset task state. They run before task warmup and
        after verifier restart. Include explicit readiness checks when needed.
        """
        for i, command in enumerate(self.config.startup_commands):
            self.run_warmup(command, log_prefix=f".startup_{i}")

    def run_warmup(self, code: str, *, log_prefix: str = ".warmup") -> None:
        # Match official line-by-line warmup handling; each background command
        # is detached explicitly and keeps a task-local log across verifier restart.
        import shlex
        for i, line in enumerate(code.splitlines()):
            command = line.strip()
            if not command or command.startswith("#"):
                continue
            if command.endswith("&"):
                command = "nohup /bin/bash -lc " + shlex.quote(command[:-1].strip()) + f" >> {WORKSPACE}/{log_prefix}_{i}.log 2>&1 < /dev/null &"
            self.exec(["/bin/bash", "-lc", command], timeout=300)

    def freeze_agent(self) -> None:
        if self.state == "solver_stopped":
            return
        if self.state != "prepared":
            raise RuntimeErrorWithStage(self.state, "Expected solver phase before freezing")
        self._assert_owned()
        try:
            if self.config.before_freeze_hook is not None and not self._freeze_hook_attempted:
                self._freeze_hook_attempted = True
                try:
                    self.freeze_hook_result = self.config.before_freeze_hook(self)
                except Exception as error:
                    self.freeze_hook_error = type(error).__name__
                    self._record_hook_failure("capture")
        finally:
            # A capture failure must never leave solver/background processes alive.
            self.docker("stop", "--time", "5", self.name, timeout=30)
            running = self.docker("inspect", "--format", "{{.State.Running}}", self.name).stdout.strip()
            if running != "false":
                raise RuntimeErrorWithStage("freeze", "Solver container has not stopped")
            self.state = "solver_stopped"
            self._save_state()

    def _record_hook_failure(self, stage: str) -> None:
        import traceback
        self.staging.mkdir(parents=True, exist_ok=True)
        path = self.staging / f"mock-state-{stage}-error.log"
        with path.open("w", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            stream.write(traceback.format_exc())

    def _mock_state_error(self, stage: str, error_type: str) -> dict[str, Any]:
        result = {"status": "mock_state_error", "scores": None, "overall_score": None,
                  "stage": stage, "error_type": error_type}
        self.state = "graded"
        self._save_state()
        (self.run_dir / "evaluation.json").write_text(json.dumps(result, indent=2))
        return result

    def grade(self, *, transcript_path: Path, transcript_loader: Path,
              grading_setup: str = "") -> dict[str, Any]:
        if self.state != "solver_stopped":
            raise RuntimeErrorWithStage(self.state, "Stop the entire solver container before introducing gold")
        contained(transcript_path, self.run_dir)
        contained(transcript_loader, self.config.project_root)
        if self.freeze_hook_error is not None:
            return self._mock_state_error("capture", self.freeze_hook_error)
        if not self.task.automated_checks:
            result = {"status": "missing_grader", "scores": None, "overall_score": None}
            (self.run_dir / "evaluation.json").write_text(json.dumps(result, indent=2))
            return result
        self._assert_owned()
        self.docker("start", self.name)
        self.state = "grading"
        self._save_state()
        self.run_startup_commands()
        if self.config.after_restart_hook is not None:
            try:
                self.config.after_restart_hook(self)
            except Exception as error:
                self._record_hook_failure("restore")
                return self._mock_state_error("restore", type(error).__name__)
        # Gold is only copied after all solver processes are dead. The controller
        # directory is never mounted; only these explicitly selected files enter.
        if self.task.gt_dir.is_dir():
            if (self.workspace / "gt").exists() or (self.workspace / "gt").is_symlink():
                raise RuntimeErrorWithStage("grading", "Solver-created gt path would shadow verifier gold")
            shutil.copytree(self.task.gt_dir, self.workspace / "gt", symlinks=False)
        self.copy_file_to_container(transcript_loader, "/tmp/_transcript_loader.py")
        self.copy_file_to_container(transcript_path, "/tmp/solver_transcript.jsonl")
        shim_dir = "/root/.openclaw/agents/main/sessions"
        # Some frozen graders read the historical OpenClaw path directly. It
        # must reside on a caller-provided writable mount when the root is RO.
        shim = self.exec(["mkdir", "-p", shim_dir], check=False)
        if shim.returncode == 0:
            self.copy_file_to_container(transcript_path, f"{shim_dir}/chat.jsonl")
        runner = self.staging / "grade_runner.py"
        runner.write_text("import json\nfrom _transcript_loader import load_transcript\n"
                          "_transcript = load_transcript('/tmp/solver_transcript.jsonl')\n" +
                          self.task.automated_checks +
                          f"\nresult = grade(transcript=_transcript, workspace_path={WORKSPACE!r})\nprint(json.dumps(result))\n")
        self.copy_file_to_container(runner, "/tmp/grade_runner.py")
        if grading_setup:
            self.run_warmup(grading_setup)
        start = time.monotonic()
        try:
            completed = self.exec(["python3", "/tmp/grade_runner.py"],
                                  timeout=self.config.grading_timeout_seconds, check=False)
            (self.run_dir / "grader.stdout.log").write_text(completed.stdout)
            (self.run_dir / "grader.stderr.log").write_text(completed.stderr)
            if completed.returncode:
                result = {"status": "grader_error", "scores": None, "overall_score": None,
                          "exit_code": completed.returncode}
            else:
                scores = parse_grade_output(completed.stdout)
                score = official_score(scores)
                issue = grading_issue(scores)
                result = {"status": issue or ("graded" if score is not None else "missing_score"),
                          "scores": scores, "overall_score": None if issue else score,
                          "raw_overall_score": score, "grader_reported_error": bool(issue)}
                (self.run_dir / "score.json").write_text(json.dumps(scores, ensure_ascii=False, indent=2))
        except subprocess.TimeoutExpired:
            result = {"status": "grader_timeout", "scores": None, "overall_score": None}
        except (ValueError, json.JSONDecodeError) as error:
            result = {"status": "invalid_grader_output", "scores": None, "overall_score": None,
                      "error_type": type(error).__name__}
        result["elapsed_seconds"] = time.monotonic() - start
        self.state = "graded"
        self._save_state()
        (self.run_dir / "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    def cleanup(self) -> None:
        if not self.created:
            return
        self._assert_owned()
        self.docker("rm", "--force", self.name, timeout=60)
        self.created = False
        self.state = "removed"
        self._save_state()



def classify_grader_issue(scores: Mapping[str, Any]) -> str | None:
    """Apply the pre-formal, static audit of all 60 error contracts.

    Explicit judge/transcript faults and missing verifier inputs remain
    unavailable even when a task also emits numeric zero. Only reviewed missing
    or invalid solver artifacts can retain their officially earned zero.
    """
    if any(scores.get(key) for key in ("judge_error", "llm_error", "llm_judge_error", "parse_error")):
        return "judge_error"
    method = str(scores.get("judge_method", "")).strip().lower()
    mode = str(scores.get("mode", "")).strip().lower()
    error = str(scores.get("error", "")).strip().lower() if scores.get("error") else ""
    if method in ("failed", "error", "unavailable"):
        return "judge_error"
    verifier_prefixes = (
        "gt_dir does not exist or is not a directory:",
        "no .tex files found under gt_dir:",
        "missing gt file:", "ground_truth.json not found:",
    )
    if error.startswith(verifier_prefixes):
        return "judge_error"
    missing_audit = {
        "no audit data and results.md not found",
        "no audit data and results.md is empty",
    }
    if method == "rule_fallback" or mode in ("fallback+llm_judge", "fallback_results_md") or error in missing_audit:
        # Official Social getters return the whole audit dict, and return {} on
        # service failure. A legitimate {"calls": []} is truthy and does not enter
        # these fallback branches. Do not infer failure from an empty calls list.
        return "judge_degraded"
    if "fallback" in method or "fallback" in mode:
        return "needs_review"
    if error:
        artifact_errors = {
            "results directory not found", "screenshot.png not found",
            "screenshot.png too small, likely invalid", "results.md not found",
            "results.md is empty",
        }
        artifact_patterns = (
            r"output dir not found: .+", r"missing summary file: .+",
            # Static binding audit: this task's `workspace` denotes an output
            # subdirectory under /tmp_workspace, not the runtime mount itself.
            r"workspace not found: /tmp_workspace/.+", r"invalid json on line [0-9]+:.*",
        )
        reviewed_artifact = error in artifact_errors or any(re.fullmatch(pattern, error, re.S) for pattern in artifact_patterns)
        if reviewed_artifact:
            try:
                # Some official errors use a dictionary union with zero-valued
                # numeric metrics rather than an explicit overall_score field.
                if official_score(scores) == 0:
                    return None
            except ValueError:
                pass
        return "needs_review"
    return None


def grading_issue(scores: Mapping[str, Any]) -> str | None:
    """Backward-compatible name used by the existing runtime and reports."""
    return classify_grader_issue(scores)

def parse_grade_output(stdout: str) -> dict[str, Any]:
    candidates = [stdout.strip(), *reversed(stdout.splitlines())]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Grader did not produce a JSON object")


def official_score(scores: Mapping[str, Any]) -> float | None:
    # Official grading.py prioritizes overall_score, otherwise averages all
    # numeric fields. Missing/error-only scores remain missing, never fabricated 0.
    numeric = {key: value for key, value in scores.items() if isinstance(value, (int, float))}
    if not numeric:
        return None
    score = float(numeric.get("overall_score", sum(numeric.values()) / len(numeric)))
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Official task score is outside [0, 1]")
    return score
