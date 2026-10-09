"""Write V7's same-task failure analyses with Claude Code; freeze them as per-task seeds.

``plan`` and ``status`` never call a model, and ``export`` only converts finished
analyses. ``build`` runs one headless Claude Code job (claude-opus-5-5, requested
effort high) per test task that has failed trajectories, in an isolated container
that mounts only that task's failure-only pool: no tests, solutions, task README,
CSV or registry. A refusal fallback to another model is accepted and recorded
(user decision). Every attempt is preserved; completed jobs are immutable and are
reused only after re-hashing.
"""
from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures
import copy
import dataclasses
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import threading
import time
from typing import Callable
import uuid

from experiment.shared.memory.tools import MAX_TEXT, _entry, _validate

from . import build_failure_memory as v6
from . import protocol, v7_seed

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parents[2]
MODEL, EFFORT = v7_seed.REQUESTED_MODEL, v7_seed.REQUESTED_EFFORT
ENGINE = "claude_code_cli_headless"
SOURCE_SELECTION = v7_seed.SOURCE_SELECTION
MAX_LESSONS, PACKET_CHARS, MAX_SOURCES = 8, 90000, 20
PROMPT_PATH = Path(__file__).resolve().with_name("v7_memory_prompt.txt")
SOURCE_FILES = (Path(__file__).resolve(), Path(__file__).resolve().with_name("v7_seed.py"),
                Path(__file__).resolve().with_name("build_failure_memory.py"),
                ROOT / "experiment/shared/memory/tools.py", ROOT / "experiment/shared/memory/seed.py")
CLAUDE_GLOB = "~/.vscode-server/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/claude"
CLAUDE_CREDENTIALS = Path("~/.claude/.credentials.json").expanduser()
TOKEN_MARGIN_SECONDS, TOKEN_POLL_SECONDS, TOKEN_MAX_WAIT_SECONDS = 15 * 60, 60, 45 * 60
# The analysis needs only shell/file reads and the structured result. Remove web access and every tool that
# can start, schedule or message other agents (the Claude Code 2.1.292 init lists them all).
DISALLOWED_TOOLS = ("WebSearch", "WebFetch", "Task", "Agent", "Workflow", "RemoteTrigger", "SendMessage",
                    "ListAgents", "TaskStop", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup",
                    "Skill", "EnterWorktree", "ExitWorktree", "NotebookEdit", "ReportFindings")
NO_MODEL_STATUSES = frozenset({"token_unavailable", "infrastructure_failed"})
# Statuses that end a job for this run without using up a model attempt: the account or the operator, not the
# analysis, was the problem. A later `build` resumes them.
NON_COUNTING_STATUSES = NO_MODEL_STATUSES | {"api_unavailable", "interrupted"}
API_FAILURE = re.compile(r"usage limit|rate limit|rate_limit|\b429\b|overloaded|\b529\b|\b401\b|unauthori[sz]ed|"
                         r"token (?:has )?expired|invalid (?:api key|bearer)|authentication|credit balance|quota", re.I)
_STOP = threading.Event()  # set on SIGINT/SIGTERM or when the API stops serving requests
MAX_RESULT_BYTES = 2 * 1024 * 1024
CLOCK_SLACK_SECONDS = 2.0  # filesystem timestamps use a coarse clock
ARTIFACTS = ("prompt.txt", "evidence_packet.txt", "packet_coverage.json", "work/output_schema.json",
             "claude.events.jsonl", "builder.stderr.log", "analysis.json")
POOL_TOP = {"manifest.json", "README.md", "tasks", "trajectories"}
POOL_MANIFEST_KEYS = {"schema_version", "benchmark", "kind", "task_id", "trajectory_count", "source_record_count",
                      "confirmed_unavailable_count", "raw_unparsed_trajectory_count",
                      "successful_trajectories_included", "trajectory_format", "trajectory_path",
                      "mutable_memory", "cross_task_memory"}
ATTEMPT = re.compile(r"attempt-\d{3,}")  # f"attempt-{n:03d}" grows past 999
ALIAS = re.compile(r"[A-Za-z0-9._-]+")
IMAGE = re.compile(r"images/attempt-\d{3,}/[0-9a-f]{64}\.(?:png|jpeg|webp)")
CARD_STATEMENT = re.compile(r"(\d+) officially graded failed attempts at this task \(official reward 0\)\. "
                            r"Successful attempts are not included\.")
NO_BODY = "NO TRAJECTORY BODY AVAILABLE (official source confirmed)"
SECTIONS = ("approach_families", "shared_assumptions", "lessons", "reusable_details")
FAILURE_MODES = ["spec_misread", "hidden_requirement", "environment_or_runtime", "implementation_bug",
                 "verification_gap", "deliverable_format", "time_budget", "infrastructure", "unknown"]
EXPECTED_TOOLS = {"Bash", "Read", "Write", "Edit", "Grep", "Glob", "StructuredOutput", "ToolSearch", "TodoWrite"}
NETWORK_PROGRAMS = {"curl", "wget", "nc", "ncat", "netcat", "telnet", "ssh", "scp", "sftp", "rsync", "ftp",
                    "aria2c", "lynx", "links", "w3m", "gh", "huggingface-cli"}
INSTALLERS = {"pip", "pip3", "uv", "npm", "npx", "yarn", "pnpm", "conda", "apt", "apt-get", "gem", "cargo", "go"}
OVERVIEW_KEYWORDS = ["overview", "decision guide", "requirements", "approach families", "shared assumptions",
                     "verification gaps"]
DETAIL_KEYWORDS = ["reusable details", "commands", "parameters", "environment traps"]
NO_GAPS = ("The analysis recorded no verification gaps; check every requirement of the instruction directly "
           "before finishing.")
TRUNCATED = "\n[truncated]"
_ACTIVE_CONTAINERS: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


class TokenUnavailable(RuntimeError):
    """The host login cannot cover a job; the job fails without a model call."""


def load(path: Path):
    return json.loads(Path(path).read_text())


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _path(base: Path, value) -> Path:
    """CLI paths are relative to the benchmark directory, like protocol's --task-corpus."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base) / path).absolute()


def _display(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return str(path)


def _log(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


# ----- Output schema (side-experiment v4 schema plus lesson keywords) -----

def _s(limit: int, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": limit}


def _list(items: dict, low: int, high: int) -> dict:
    return {"type": "array", "items": items, "minItems": low, "maxItems": high}


def _object(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": sorted(properties), "properties": properties}


def output_schema() -> dict:
    source = _object({"path": _s(400), "line_start": {"type": "integer", "minimum": 1},
                      "line_end": {"type": "integer", "minimum": 1}})
    schema = _object({
        "task_summary": _s(700),
        "decision_guide": _object({"first_steps": _list(_s(300), 1, 5), "avoid": _list(_s(300), 1, 6),
                                   "before_finishing": _list(_s(300), 1, 6)}),
        "requirements": _list(_object({"requirement": _s(300), "how_attempts_handled_it": _s(500),
                                       "risk": {"type": "string", "enum": ["high", "medium", "low"]}}), 1, 10),
        "approach_families": _list(_object({
            "name": _s(120), "attempt_count": {"type": "integer", "minimum": 0}, "furthest_progress": _s(500),
            "where_it_broke": _s(600),
            "verdict": {"type": "string", "enum": ["approach_likely_wrong", "execution_likely_wrong", "unclear"]},
            "evidence": _s(500), "sources": _list(source, 1, 6)}), 1, 6),
        "shared_assumptions": _list(_object({"assumption": _s(400), "why_suspect": _s(500),
                                             "sources": _list(source, 0, 4)}), 0, 6),
        "verification_gaps": _list(_object({"check_used": _s(400), "what_it_misses": _s(500)}), 0, 6),
        "lessons": _list(_object({
            "title": _s(160), "keywords": _list(_s(60), 1, 8),
            "failure_mode": {"type": "string", "enum": FAILURE_MODES},
            "attempts_affected": {"type": "integer", "minimum": 0}, "observed_failure": _s(700),
            "cause_hypothesis": _s(600),
            "cause_confidence": {"type": "string", "enum": ["observed", "hypothesis", "unknown"]},
            "what_to_do_instead": _s(700), "check_that_would_catch_it": _s(500),
            "sources": _list(source, 1, 8)}), 1, MAX_LESSONS),
        "reusable_details": _list(_object({"what": _s(200), "detail": _s(900), "caveat": _s(300, 0),
                                           "sources": _list(source, 1, 4)}), 0, 5),
        "unexplained": _s(1500, 0),
        "reviewed_sources": _list(_s(400), 0, 200),
    })
    return copy.deepcopy(schema)


def schema_text() -> str:
    return json.dumps(output_schema(), ensure_ascii=False, indent=2) + "\n"


# ----- Validation with canonical path completion -----

def citable(job: dict) -> dict[str, int]:
    """Only this task's readable failed trajectories and its own card can be cited."""
    return {**{record["path"]: record["line_count"] for record in job["records"]},
            job["card"]["path"]: job["card"]["line_count"]}


def canonical_path(path: str, lines: dict[str, int]) -> str:
    """Complete a missing leading '/' or '/pool/' prefix, only onto exactly one citable file."""
    if path in lines:
        return path
    bare = path[1:] if path.startswith("/") else path
    bare = bare[len("pool/"):] if bare.startswith("pool/") else bare
    matches = [candidate for candidate in lines if candidate == "/pool/" + bare]
    return matches[0] if len(matches) == 1 else path


def validate_analysis(value: dict, job: dict) -> tuple[dict, list[dict]]:
    """Validate a model analysis without editing it; return a checked copy and its path completions."""
    # A JSON round trip (not deepcopy) breaks shared sub-objects, so every completion is recorded.
    checked = json.loads(json.dumps(value, ensure_ascii=False))
    _validate(checked, output_schema(), "analysis")
    lines, nul_lines = citable(job), job.get("nul_lines", {})
    completions = []
    for section in SECTIONS:
        for item_index, item in enumerate(checked[section]):
            for source_index, pointer in enumerate(item["sources"]):
                original = pointer["path"]
                pointer["path"] = canonical_path(original, lines)
                if pointer["path"] != original:
                    completions.append({"section": section, "item": item_index, "source": source_index,
                                        "original": original, "completed": pointer["path"]})
                count = lines.get(pointer["path"])
                if count is None:
                    raise ValueError(f"{section}[{item_index}] cites a file outside this task's failed attempts "
                                     f"and card: {original[:200]}")
                if not 1 <= pointer["line_start"] <= pointer["line_end"] <= count:
                    raise ValueError(f"{section}[{item_index}] has an invalid line range for {pointer['path']}")
                # A seed refuses binary windows, and the model only saw NUL bytes as U+2400: narrow the window
                # to its longest NUL-free run (the text is untouched) so every valid analysis converts.
                kept = _text_window(pointer["line_start"], pointer["line_end"], nul_lines.get(pointer["path"], ()))
                if kept is None:
                    raise ValueError(f"{section}[{item_index}] cites only binary NUL lines of {pointer['path']}")
                if kept != (pointer["line_start"], pointer["line_end"]):
                    completions.append({"section": section, "item": item_index, "source": source_index,
                                        "nul_trim": {"cited": [pointer["line_start"], pointer["line_end"]],
                                                     "kept": list(kept)}})
                    pointer["line_start"], pointer["line_end"] = kept
    return checked, completions


def _text_window(first: int, last: int, nul_lines) -> tuple[int, int] | None:
    """The longest run of non-NUL lines inside [first, last] (earliest on a tie), or None if every line is NUL."""
    binary = {n for n in nul_lines if first <= n <= last}
    best, start = None, None
    for n in range(first, last + 2):
        if n <= last and n not in binary:
            start = n if start is None else start
        elif start is not None:
            if best is None or n - start > best[1] - best[0] + 1:
                best = (start, n - 1)
            start = None
    return best


# ----- Corpus, pool verification and plan -----

def _corpus_verifier() -> Callable | None:
    """Also apply Part A's pool verifier when that module exists (defence in depth)."""
    try:
        from . import v7_corpus
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.endswith("v7_corpus"):
            return None
        raise
    return getattr(v7_corpus, "verify_task_pool", None)


def _head_lines(path: Path, count: int) -> list[str]:
    with path.open("rb") as stream:
        data = stream.read(16384)
    return [line.rstrip("\r") for line in data.decode("utf-8", errors="replace").split("\n")[:count]]


def _check_card(path: Path, task_id: str, trajectories: list[str], manifest: dict) -> None:
    """The solver/builder-visible card may list only scored failures of this task, without Hub ids."""
    try:
        lines = path.read_bytes().decode("utf-8").split("\n")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{task_id}: task card is not UTF-8") from exc
    headings = [n for n, line in enumerate(lines) if line == "## Historical trajectories"]
    if lines[0] != f"# {task_id}" or not headings:
        raise ValueError(f"{task_id}: task card lacks its title or trajectory section")
    tail = lines[headings[-1] + 1:]
    statement = CARD_STATEMENT.fullmatch(next((line for line in tail if line.strip()), ""))
    rows = [line for line in tail if line.startswith("- ")]
    if statement is None or int(statement.group(1)) != len(rows) or len(rows) != manifest["source_record_count"]:
        raise ValueError(f"{task_id}: task card must state its failed-attempt count and exclude successes")
    listed = []
    for row in rows:
        fields = row[2:].split(" | ")
        if ("score=0" not in fields or "scored=True" not in fields
                or any(field.startswith("trial=") for field in fields)
                or not any(field.startswith("attempt=attempt-") for field in fields)):
            raise ValueError(f"{task_id}: task card row is not an anonymized scored failure: {row[:200]}")
        if fields[0].startswith("/pool/"):
            listed.append(fields[0][len("/pool/"):])
        elif fields[0] != NO_BODY:
            raise ValueError(f"{task_id}: unexpected task card row: {row[:200]}")
    if sorted(listed) != sorted(trajectories):
        raise ValueError(f"{task_id}: task card rows differ from the pool's trajectory files")


def verify_pool(pool: Path, task_id: str, record: dict) -> dict:
    """Refuse anything except this task's frozen failure-only pool before a builder may read it."""
    pool = Path(pool)
    if pool.is_symlink() or not pool.is_dir():
        raise ValueError(f"{task_id}: pool must be a regular directory")
    for path in pool.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"{task_id}: pool contains a link or special file: {path.relative_to(pool)}")
    top = {path.name for path in pool.iterdir()}
    if not POOL_TOP <= top <= POOL_TOP | {"images"}:
        raise ValueError(f"{task_id}: unexpected pool top level: {sorted(top)}")
    if not all((pool / name).is_dir() for name in ("tasks", "trajectories")):
        raise ValueError(f"{task_id}: pool tasks/ and trajectories/ must be directories")
    files = protocol.inventory(pool)
    if files != record.get("pool_files_sha256") or files.get("manifest.json") != record.get("pool_manifest_sha256"):
        raise ValueError(f"{task_id}: pool differs from the frozen corpus index")
    manifest = load(pool / "manifest.json")
    if (not isinstance(manifest, dict) or set(manifest) != POOL_MANIFEST_KEYS
            or manifest["kind"] != v7_seed.POOL_KIND or manifest["task_id"] != task_id
            or manifest["successful_trajectories_included"] is not False or manifest["cross_task_memory"] is not False
            or any(type(manifest[key]) is not int for key in ("trajectory_count", "source_record_count"))):
        raise ValueError(f"{task_id}: pool manifest is not this task's failure-only manifest")
    if ({path.name for path in (pool / "tasks").iterdir()} != {f"{task_id}.md"}
            or {path.name for path in (pool / "trajectories").iterdir()} != {task_id}
            or not (pool / "trajectories" / task_id).is_dir()):
        raise ValueError(f"{task_id}: pool must hold only this task's card and trajectory directory")
    trajectories = []
    for relative in files:
        parts = PurePosixPath(relative).parts
        if relative in {"manifest.json", "README.md", f"tasks/{task_id}.md"} or IMAGE.fullmatch(relative):
            continue
        if (len(parts) != 5 or parts[:2] != ("trajectories", task_id) or not ALIAS.fullmatch(parts[2])
                or not ATTEMPT.fullmatch(parts[3]) or parts[4] != f"{task_id}_0.txt"):
            raise ValueError(f"{task_id}: unexpected pool file: {relative}")
        header = _head_lines(pool / relative, 4)
        if (len(header) < 4 or header[0] != f"# Historical failed attempt at this task: {task_id}"
                or header[2] != f"Official reward: 0; attempt ID: {parts[3]}"
                or v7_seed._header_value(header[3], "Official scored execution") != "True"):
            raise ValueError(f"{task_id}: trajectory header is not an anonymized scored failure: {relative}")
        trajectories.append(relative)
    if len(trajectories) != manifest["trajectory_count"] or len(trajectories) != record.get("trajectory_count"):
        raise ValueError(f"{task_id}: trajectory count differs from manifest or index")
    _check_card(pool / "tasks" / f"{task_id}.md", task_id, trajectories, manifest)
    return {"pool_manifest_sha256": files["manifest.json"], "files_sha256": files, "trajectories": trajectories}


def _pool_dir(base: Path, corpus: Path, task_id: str, record: dict) -> Path:
    expected = corpus / "pools" / task_id
    relative = record.get("pool_path")
    if not isinstance(relative, str) or protocol.local_path(base, relative).resolve() != expected.resolve():
        raise ValueError(f"{task_id}: index pool_path does not name this corpus's pool")
    return expected


def _card_record(pool: Path, task_id: str) -> dict:
    relative = f"tasks/{task_id}.md"
    data = v6.checked_file(pool, relative).read_bytes()
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{task_id}: task card is not UTF-8") from exc
    return {"path": "/pool/" + relative, "pool_file": relative, "sha256": hashlib.sha256(data).hexdigest(),
            "line_count": len(data.splitlines()), "bytes": len(data)}


def pool_records(pool: Path, trajectories: list[str]) -> tuple[list[dict], list[dict], Counter, dict]:
    """Records use the anonymized attempt id as trial_id; Hub trial ids never reach a builder."""
    records, unreadable, models, nul_lines = [], [], Counter(), {}
    for relative in trajectories:
        data = v6.checked_file(pool, relative).read_bytes()
        parts = PurePosixPath(relative).parts
        header = data[:16384].decode("utf-8", errors="replace").split("\n")
        named = re.match(r"Model: (.+?); agent: ", header[1]) if len(header) > 1 else None
        models[named.group(1) if named else parts[2]] += 1
        digest = hashlib.sha256(data).hexdigest()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            # The packet reader and a seed both need text; such a file stays mounted but uncitable.
            unreadable.append({"pool_file": relative, "reason": "not_utf8", "sha256": digest, "bytes": len(data)})
            continue
        # Byte line splitting matches the seed's line-window check exactly.
        lines = data.splitlines()
        record = {"path": "/pool/" + relative, "trial_id": parts[3], "status": "available",
                  "line_count": len(lines), "pool_file": relative, "sha256": digest, "bytes": len(data)}
        binary = [number for number, line in enumerate(lines, 1) if b"\x00" in line]
        if binary:
            nul_lines[record["path"]] = binary
        records.append(record)
    return records, unreadable, models, nul_lines


def build_plan(corpus, *, base: Path = BASE) -> dict:
    """Read and hash the corpus only; one job per task with at least one readable failed trajectory."""
    base = Path(base)
    corpus = _path(base, corpus)
    index_path = corpus / "index.json"
    index = load(index_path)
    if (not isinstance(index, dict) or index.get("schema_version") != 1 or index.get("kind") != "v7_same_task_corpus"
            or not isinstance(index.get("tasks"), dict) or not index["tasks"]):
        raise ValueError("Not a V7 same-task corpus index")
    verifier = _corpus_verifier()
    jobs, empty, unreadable_only = [], [], []
    for task_id in sorted(index["tasks"]):
        v7_seed._task_id(task_id)
        record = index["tasks"][task_id]
        pool = _pool_dir(base, corpus, task_id, record)
        checked = verify_pool(pool, task_id, record)
        if verifier is not None:
            verifier(pool, task_id, record)
        if not checked["trajectories"]:
            empty.append(task_id)
            continue
        records, unreadable, models, nul_lines = pool_records(pool, checked["trajectories"])
        if not records:
            unreadable_only.append(task_id)
            continue
        ordered = dict(sorted(models.items(), key=lambda item: (-item[1], item[0])))
        jobs.append({"task_id": task_id, "pool_path": record["pool_path"],
                     "pool_manifest_sha256": checked["pool_manifest_sha256"],
                     "pool_inventory_sha256": _digest(checked["files_sha256"]),
                     "attempt_count": len(checked["trajectories"]), "models": ordered,
                     "models_text": ", ".join(f"{model} x{count}" for model, count in ordered.items()),
                     "card": _card_record(pool, task_id), "records": records,
                     "unreadable_trajectories": unreadable, "nul_lines": nul_lines, **v6.job_budget(records)})
    return {"schema_version": 1, "kind": "v7_failure_analysis_plan", "benchmark": index.get("benchmark", "Terminal-Bench 2.1"),
            "corpus": _display(base, corpus), "build_id": index.get("build_id"),
            "corpus_complete": index.get("complete", True),
            "corpus_index_sha256": v6.sha(index_path), "builder_engine": ENGINE,
            "builder_model_requested": MODEL, "builder_effort_requested": EFFORT,
            "disallowed_tools": list(DISALLOWED_TOOLS), "max_lessons": MAX_LESSONS,
            "packet_character_budget_per_job": PACKET_CHARS, "budget_policy": v6.budget_policy(None),
            "source_selection": SOURCE_SELECTION, "prompt_template_sha256": v6.sha(PROMPT_PATH),
            "output_schema_sha256": _digest(output_schema()),
            "source_code_sha256": {path.relative_to(ROOT).as_posix(): v6.sha(path) for path in SOURCE_FILES},
            "job_count": len(jobs), "jobs": jobs, "tasks_without_trajectories": empty,
            "tasks_without_readable_trajectories": unreadable_only}


def _without_code(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key != "source_code_sha256"}


def freeze_plan(work: Path, plan: dict, *, accept_code_update: bool = False) -> None:
    """Freeze data, prompt, schema, model and budgets; code-only repairs need an explicit receipt."""
    path = work / "plan.json"
    if not path.exists():
        v6.write_json(path, plan)
        return
    frozen = load(path)
    if frozen == plan:
        return
    if (_without_code(frozen) != _without_code(plan)
            or set(frozen.get("source_code_sha256", {})) != set(plan["source_code_sha256"])):
        raise ValueError("Frozen builder plan differs (corpus, pools, prompt, schema, model or budget); "
                         "use a new work directory")
    if not accept_code_update:
        raise ValueError("Builder source code changed since the plan was frozen; pass --accept-code-update")
    history = work / "plan_history"
    history.mkdir(exist_ok=True)
    old_digest = v6.sha(path)
    archived = history / f"{old_digest}.json"
    if not archived.exists():
        archived.write_bytes(path.read_bytes())
    old, new = frozen["source_code_sha256"], plan["source_code_sha256"]
    receipt = {"schema_version": 1, "kind": "explicit_builder_code_amendment", "accepted_at_unix": time.time(),
               "old_plan_sha256": old_digest, "old_source_code_sha256": old, "new_source_code_sha256": new,
               "changed_source_files": sorted(name for name in new if new[name] != old[name]),
               "applies_to": "future attempts; completed jobs stay bound by their own artifact hashes"}
    v6.write_json(history / f"amendment-{len(list(history.glob('amendment-*.json'))) + 1:03d}.json", receipt)
    v6.write_json(path, plan)


# ----- Claude Code engine -----

def find_claude_binary(pattern: str = CLAUDE_GLOB) -> Path:
    """Use the newest VS Code extension binary; support.json pins it for the whole build."""
    matches = [Path(match) for match in glob.glob(os.path.expanduser(pattern))]
    if not matches:
        raise ValueError(f"No Claude Code binary matches {pattern}")

    def version(path: Path) -> tuple:
        found = re.search(r"anthropic\.claude-code-(\d+(?:\.\d+)*)-linux-x64", str(path))
        return tuple(int(part) for part in found.group(1).split(".")) if found else ()
    return max(matches, key=lambda path: (version(path), str(path)))


def claude_binary_info(binary: Path) -> dict:
    binary = Path(binary).expanduser().absolute()
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Claude Code binary must be an executable regular file")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"}
    version = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True,
                             timeout=60, env=env).stdout.strip()
    return {"claude_binary_path": str(binary), "claude_version": version, "claude_binary_sha256": v6.sha(binary)}


def runtime_root(base: Path) -> Path:
    """Pinned runtime assets (rg, Python, CA) live in the TB2.1 install; another benchmark (V8) reuses them."""
    return Path(base) if (Path(base) / "runtime/bin").is_dir() else BASE


def support(base: Path, binary: Path, image: str = v6.DEFAULT_IMAGE) -> dict:
    """Pinned image, Python and CA from the V6 helper, plus the Claude binary identity."""
    return {"schema_version": 1, "image": image, **v6.support(runtime_root(base), image), **claude_binary_info(binary)}


def resolve_support(work: Path, support_fn: Callable[[], dict]) -> dict:
    supported = json.loads(json.dumps(support_fn()))
    path = work / "support.json"
    if path.exists() and load(path) != supported:
        raise ValueError("Builder runtime (Claude binary, version, image, Python or CA) changed; "
                         "refuse to continue this build")
    if not path.exists():
        v6.write_json(path, supported)
    return supported


def claude_access_token(credentials: Path, min_seconds: float, *, now: Callable[[], float] = time.time,
                        sleep: Callable[[float], None] = time.sleep, poll_seconds: float = TOKEN_POLL_SECONDS,
                        max_wait_seconds: float = TOKEN_MAX_WAIT_SECONDS) -> str:
    """Return the host's OAuth access token once it outlives the job; never log or persist it.

    Only the host login refreshes the token (the container never sees a refresh token),
    so wait for a refresh, polling, then give up without any model call.
    """
    waited = 0.0
    while True:
        try:
            oauth = json.loads(Path(credentials).read_text())["claudeAiOauth"]
            token, expires = oauth["accessToken"], oauth["expiresAt"]
            if not isinstance(token, str) or not token or type(expires) not in (int, float):
                raise ValueError("invalid credential record")
            remaining = expires / 1000 - now()
            if remaining >= min_seconds:
                return token
            reason = f"access token lifetime {remaining:.0f} s is below the required {min_seconds:.0f} s"
        except (OSError, ValueError, KeyError, TypeError):
            reason = "Claude credentials are missing or unreadable"
        if waited >= max_wait_seconds:
            raise TokenUnavailable(f"{reason} after waiting {waited:.0f} s for a host login refresh")
        sleep(poll_seconds)
        waited += poll_seconds


@dataclasses.dataclass(frozen=True)
class EngineRequest:
    """One builder invocation. The token is never serialized and is hidden from repr."""
    task_id: str
    container_name: str
    base: Path
    pool: Path
    attempt: Path
    work: Path
    home: Path
    prompt: str
    schema_text: str
    timeout_seconds: int
    support: dict
    token: str = dataclasses.field(repr=False)


def docker_argv(base: Path, support_info: dict, pool: Path, work: Path, home: Path, name: str,
                schema: str) -> list[str]:
    args = ["docker", "run", "--name", name, "--interactive", "--init", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--workdir", "/work",
            "--env", "HOME=/opt/builder-home", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PATH=/opt/claude:/opt/builder-bin:/opt/builder-python/bin:/usr/local/bin:/usr/bin:/bin",
            "--env", "SSL_CERT_FILE=/opt/builder-ca.crt", "--env", "NODE_EXTRA_CA_CERTS=/opt/builder-ca.crt",
            "--env", "DISABLE_AUTOUPDATER=1", "--env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "--env", "DISABLE_TELEMETRY=1",
            # Name only: Docker copies the value from the client environment, never from argv.
            "--env", "CLAUDE_CODE_OAUTH_TOKEN"]
    mounts = [(pool, "/pool", True), (work, "/work", False), (home, "/opt/builder-home", False),
              (Path(support_info["python_root"]), "/opt/builder-python", True),
              (Path(support_info["claude_binary_path"]), "/opt/claude/claude", True),
              # Pinned static ripgrep, as for the solver: the prompt asks for rg reads on /pool.
              (runtime_root(base) / "runtime/bin/rg", "/opt/builder-bin/rg", True),
              (runtime_root(base) / "prepared/transport/ca-certificates.crt", "/opt/builder-ca.crt", True)]
    for source, target, readonly in mounts:
        if "," in str(source):
            raise ValueError("Docker bind paths containing commas are unsupported")
        args += ["--mount", f"type=bind,src={Path(source).resolve()},dst={target}" + (",readonly" if readonly else "")]
    return args + [support_info["image_id"], "/opt/claude/claude", "-p", "--model", MODEL, "--effort", EFFORT,
                   "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
                   "--json-schema", schema, "--disallowedTools", *DISALLOWED_TOOLS]


def _remove_container(name: str) -> str | None:
    try:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__
    return None


def stop_active_containers() -> None:
    with _ACTIVE_LOCK:
        names = sorted(_ACTIVE_CONTAINERS)
    for name in names:
        _remove_container(name)


def claude_engine(request: EngineRequest) -> dict:
    """Run Claude Code headless in the isolated container; always remove the container."""
    argv = docker_argv(request.base, request.support, request.pool, request.work, request.home,
                       request.container_name, request.schema_text)
    started, exit_code, timed_out, error, process = time.monotonic(), None, False, None, None
    with _ACTIVE_LOCK:
        _ACTIVE_CONTAINERS.add(request.container_name)
    try:
        with (request.attempt / "claude.events.jsonl").open("w") as events, \
                (request.attempt / "builder.stderr.log").open("w") as errors:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True,
                                       encoding="utf-8", env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": request.token})
            try:
                process.communicate(input=request.prompt, timeout=request.timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    except (OSError, subprocess.SubprocessError) as exc:
        error = type(exc).__name__
    finally:
        cleanup = _remove_container(request.container_name)
        with _ACTIVE_LOCK:
            _ACTIVE_CONTAINERS.discard(request.container_name)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    return {"container_name": request.container_name, "exit_code": exit_code, "timed_out": timed_out,
            "execution_error": error, "cleanup_error": cleanup, "elapsed_seconds": time.monotonic() - started}


# ----- Stream audit -----

def _events(path: Path):
    """Split on newlines only: a JSON line may carry a raw U+2028 that str.splitlines would break."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return
    for line in data.split(b"\n"):
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def audit_events(path: Path) -> tuple[dict, dict, dict | None]:
    """Record which models wrote output; effort cannot be verified because the CLI does not echo it."""
    init_model, final, fallbacks, writers = None, None, [], Counter()
    for event in _events(path):
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "system" and subtype == "init" and init_model is None:
            init_model = event.get("model")
        elif kind == "system" and subtype == "model_refusal_fallback":
            fallbacks.append({key: event.get(key) for key in ("original_model", "fallback_model", "api_refusal_category")})
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            model = event["message"].get("model")
            if isinstance(model, str):
                writers[model] += 1
        elif kind == "result":
            final = event
    usage_by_model = final.get("modelUsage") if final and isinstance(final.get("modelUsage"), dict) else {}
    tokens = {model: _count((value or {}).get("outputTokens")) for model, value in usage_by_model.items()
              if isinstance(value, dict) or value is None}
    total = sum(tokens.values())
    real_writers = {model for model in writers if not (model.startswith("<") and model.endswith(">"))}
    fallback_models = {item["fallback_model"] for item in fallbacks if isinstance(item.get("fallback_model"), str)}
    requested = v7_seed.is_requested_model
    strict = (final is not None and requested(init_model) and not fallbacks
              and any(requested(model) and count > 0 for model, count in tokens.items())
              and all(requested(model) for model, count in tokens.items() if count > 0)
              and all(requested(model) for model in real_writers))
    usage = {"usage_available": final is not None,
             **(final.get("usage") if final and isinstance(final.get("usage"), dict) else {}),
             "total_cost_usd": final.get("total_cost_usd") if final else None,
             "num_turns": final.get("num_turns") if final else None}
    audit = {"engine": ENGINE, "requested_model": MODEL, "init_model": init_model,
             "models_used": sorted(set(tokens) | real_writers | fallback_models),
             "assistant_message_models": dict(sorted(writers.items())),
             "output_tokens_by_model": tokens if final else None,
             "output_share_by_model": ({model: round(count / total, 3) for model, count in tokens.items()}
                                       if final and total else ({} if final else None)),
             "refusal_fallbacks": fallbacks, "strict_model_audit_verified": strict,
             "effort_requested": EFFORT, "effort_verified": False,
             "effort_source": "--effort flag (not echoed by the CLI)", "result_event_present": final is not None,
             "result_subtype": final.get("subtype") if final else None,
             "is_error": final.get("is_error") if final else None}
    return usage, audit, final


def _command_segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, respecting quotes (grep "curl|wget" is one command)."""
    operators = "();<>|&`"
    try:
        lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=operators)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = re.sub(r"[();<>|&`\n]", " ; ", command).split()
    segments, current = [], []
    for token in tokens:
        if token and set(token) <= set(operators):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    return segments + [current] if current else segments


def _network_words(words: list[str]) -> bool:
    prefixes = {"$", "!", "{", "do", "then", "else", "sudo", "env", "nohup", "command", "exec", "time", "xargs",
                "timeout"}
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in prefixes
                     or re.fullmatch(r"-\w+|\d+[smh]?", words[0])):
        words = words[1:]
    if not words:
        return False
    program = PurePosixPath(words[0]).name
    if program in NETWORK_PROGRAMS:
        return True
    if program == "git" and len(words) > 1 and words[1] in {"clone", "fetch", "pull", "ls-remote", "submodule"}:
        return True
    if program in INSTALLERS and any(word in {"install", "download", "add", "get", "fetch"} for word in words[1:3]):
        return True
    return program.startswith("python") and re.search(
        r"urllib|http\.client|requests|socket|urlopen|httpx|aiohttp", " ".join(words[1:])) is not None


def tool_audit(path: Path) -> dict:
    """Record tool use and likely network commands for review; this is not an acceptance gate."""
    counts, network = Counter(), []
    for event in _events(path):
        if event.get("type") != "assistant" or not isinstance(event.get("message"), dict):
            continue
        for block in event["message"].get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name"))
            counts[name] += 1
            command = (block.get("input") or {}).get("command") if isinstance(block.get("input"), dict) else None
            if name == "Bash" and isinstance(command, str) and any(
                    _network_words(words) for words in _command_segments(command)):
                network.append(command[:300])
    return {"tool_use_counts": dict(sorted(counts.items())),
            "unexpected_tool_uses": {name: n for name, n in sorted(counts.items()) if name not in EXPECTED_TOOLS},
            "network_command_count": len(network), "network_commands": network[:20],
            "policy": "recorded for review; not an automatic acceptance gate"}


# ----- Attempts, collection and immutable completions -----

def _load_output(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("builder output must be a regular file")
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError("builder output is too large")
    return json.loads(path.read_bytes().decode("utf-8"))


def collect(attempt: Path, final: dict | None, job: dict, started: float, deadline: float):
    """Prefer the final result, then /work/summary.json written in the attempt window; never rewrite either."""
    failures, candidates = [], []
    structured = final.get("structured_output") if final else None
    text = final.get("result") if final else None
    if isinstance(structured, dict):
        (attempt / "result.json").write_text(json.dumps(structured, ensure_ascii=False, indent=2) + "\n")
        candidates.append(("result.json", "structured_output"))
    elif isinstance(text, str) and text.strip():
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            (attempt / "result.json").write_text(text)
            candidates.append(("result.json", "result_text"))
        else:
            failures.append({"source": "result_text", "classification": "not_a_json_object"})
    else:
        failures.append({"source": "final_result", "classification": "missing"})
    summary = attempt / "work" / "summary.json"
    if summary.is_symlink() or summary.exists():
        if summary.is_symlink() or not summary.is_file():
            failures.append({"file": "work/summary.json", "classification": "not_a_regular_file"})
        elif not started - CLOCK_SLACK_SECONDS <= summary.stat().st_mtime <= deadline:
            failures.append({"file": "work/summary.json", "classification": "outside_attempt_window",
                             "mtime_unix": summary.stat().st_mtime})
        else:
            candidates.append(("work/summary.json", "summary_checkpoint"))
    else:
        failures.append({"file": "work/summary.json", "classification": "missing"})
    for relative, source in candidates:
        path = attempt / relative
        try:
            raw = _load_output(path)
            analysis, completions = validate_analysis(raw, job)
        except (ValueError, KeyError, TypeError) as exc:
            failures.append({"file": relative, "source": source, "classification": "invalid_output",
                             "sha256": v6.sha(path), "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            continue
        origin = {"file": relative, "source": source, "sha256": v6.sha(path), "mtime_unix": path.stat().st_mtime,
                  "started_at_unix": started, "deadline_unix": deadline, "prior_candidates": failures}
        return raw, analysis, completions, origin
    raise ValueError("No valid analysis: " + json.dumps(failures, sort_keys=True))


def _attempts(work: Path, task_id: str) -> list[dict]:
    return [load(path) for path in sorted((work / "jobs" / task_id).glob("attempt-*/attempt.json"))]


def run_attempt(base: Path, work: Path, pool: Path, plan: dict, job: dict, support_info: dict, *,
                engine: Callable[[EngineRequest], dict], token_provider: Callable[[float], str],
                max_attempts: int) -> dict:
    """One fresh attempt directory; the container may write only <attempt>/work."""
    task_id = job["task_id"]
    template = PROMPT_PATH.read_bytes()
    if hashlib.sha256(template).hexdigest() != plan["prompt_template_sha256"]:
        raise ValueError("Builder prompt changed after the plan was frozen")
    if _digest(protocol.inventory(pool)) != job["pool_inventory_sha256"]:
        raise ValueError(f"{task_id}: pool changed after the plan was frozen")
    job_dir = work / "jobs" / task_id
    job_dir.mkdir(parents=True, exist_ok=True)
    attempt = job_dir / f"attempt-{len(list(job_dir.glob('attempt-*'))) + 1:03d}"
    attempt.mkdir()
    mounted = attempt / "work"
    mounted.mkdir()
    (work / "private").mkdir(mode=0o700, exist_ok=True)
    home = work / "private" / task_id / attempt.name
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    packet, coverage = v6.evidence_packet(job["records"], pool, plan["packet_character_budget_per_job"])
    # A NUL inside an API prompt is unsafe; show it visibly without changing line numbers.
    nul_count = packet.count("\x00")
    packet = packet.replace("\x00", "\u2400")
    schema = schema_text()
    (mounted / "output_schema.json").write_text(schema)
    v6.write_json(attempt / "packet_coverage.json", coverage)
    (attempt / "evidence_packet.txt").write_text(packet)
    prompt = template.decode("utf-8").format(
        task_id=task_id, attempt_count=job["attempt_count"], models=job["models_text"],
        max_lessons=plan["max_lessons"], timeout_seconds=job["timeout_seconds"], packet=packet)
    (attempt / "prompt.txt").write_text(prompt)
    inputs = {name: v6.sha(attempt / name) for name in ARTIFACTS[:4]}
    result = {"schema_version": 1, "task_id": task_id, "attempt": attempt.name, "status": "running",
              "builder_engine": ENGINE, "requested_model": MODEL, "requested_effort": EFFORT,
              "max_attempts": max_attempts, "timeout_seconds": job["timeout_seconds"],
              "budget_basis": job["budget_basis"], "plan_sha256": v6.sha(work / "plan.json"),
              "job_sha256": _digest(job), "pool_manifest_sha256": job["pool_manifest_sha256"],
              "attempt_count": job["attempt_count"], "candidate_count": len(job["records"]),
              "image_id": support_info.get("image_id"), "claude_version": support_info.get("claude_version"),
              "claude_binary_sha256": support_info.get("claude_binary_sha256"), "input_sha256": inputs,
              "packet_nul_replacements": nul_count, "received_tests_or_solutions": False,
              "received_successful_attempts": False, "model_call_performed": False}
    v6.write_json(attempt / "attempt.json", result)
    if _STOP.is_set():
        result.update(status="interrupted", error="build stopped before this attempt started")
        v6.write_json(attempt / "attempt.json", result)
        return result
    try:
        token = token_provider(job["timeout_seconds"] + TOKEN_MARGIN_SECONDS)
    except TokenUnavailable as exc:
        result.update(status="token_unavailable", error=str(exc)[:500])
        v6.write_json(attempt / "attempt.json", result)
        return result
    binary = support_info.get("claude_binary_path")
    if binary and (not Path(binary).is_file() or v6.sha(Path(binary)) != support_info.get("claude_binary_sha256")):
        result.update(status="infrastructure_failed", error="Claude Code binary changed or vanished during the build")
        v6.write_json(attempt / "attempt.json", result)
        return result
    request = EngineRequest(task_id=task_id, container_name="tb21-v7-build-" + uuid.uuid4().hex[:16], base=base,
                            pool=pool, attempt=attempt, work=mounted, home=home, prompt=prompt,
                            schema_text=schema, timeout_seconds=job["timeout_seconds"], support=support_info,
                            token=token)
    started = time.time()
    result.update(started_at_unix=started, container_name=request.container_name, model_call_performed=True)
    v6.write_json(attempt / "attempt.json", result)
    try:
        outcome = engine(request)
    finally:
        (home / ".claude" / ".credentials.json").unlink(missing_ok=True)  # never keep a credential copy
    for name in ("claude.events.jsonl", "builder.stderr.log"):
        (attempt / name).touch(exist_ok=True)
    # The model can read its own environment; never keep the access token in any record it might have printed.
    result["token_scrubbed_occurrences"] = scrub_token(token, [attempt, home])
    result.update({key: outcome.get(key) for key in ("exit_code", "timed_out", "execution_error", "cleanup_error",
                                                     "elapsed_seconds")})
    usage, audit, final = audit_events(attempt / "claude.events.jsonl")
    result.update(usage=usage, model_audit=audit, tool_audit=tool_audit(attempt / "claude.events.jsonl"))
    deadline = started + job["timeout_seconds"]
    changed = sorted(name for name, digest in inputs.items()
                     if (attempt / name).is_symlink() or not (attempt / name).is_file() or v6.sha(attempt / name) != digest)
    analysis = None
    if audit["init_model"] is None:
        # No session ever started (Docker error, missing image, mount failure): no model call took place.
        result.update(status="infrastructure_failed", model_call_performed=False,
                      error=f"builder container could not start a Claude Code session: exit {outcome.get('exit_code')}, "
                            f"error {outcome.get('execution_error')}")
    elif changed:
        result.update(status="inputs_modified", error="builder modified its inputs: " + ", ".join(changed))
    else:
        try:
            raw, analysis, completions, origin = collect(attempt, final, job, started, deadline)
        except ValueError as exc:
            status = ("budget_exhausted" if outcome.get("timed_out") else
                      "execution_failed" if outcome.get("exit_code") not in (0, None) or outcome.get("execution_error")
                      else "output_invalid")
            if _STOP.is_set() and status != "output_invalid":
                status = "interrupted"
            elif api_failure(final, attempt):
                status = "api_unavailable"
            result.update(status=status, error=str(exc)[:2000])
        else:
            if not v7_seed.is_requested_model(audit["init_model"]):
                result.update(status="model_audit_failed", error="Claude Code did not start the requested model")
                analysis = None
            else:
                v6.write_json(attempt / "analysis.json", analysis)
                result.update(status="complete", result_origin=origin, path_completions=completions,
                              lesson_count=len(analysis["lessons"]),
                              reusable_detail_count=len(analysis["reusable_details"]),
                              artifact_sha256={name: v6.sha(attempt / name)
                                               for name in (*ARTIFACTS, origin["file"])})
    v6.write_json(attempt / "attempt.json", result)
    if result["status"] == "complete":
        v6.write_json(job_dir / "completion.json", {**result, "attempt_sha256": v6.sha(attempt / "attempt.json")})
    return result


def api_failure(final: dict | None, attempt: Path) -> bool:
    """The API refused the session (limits, overload, auth): an account problem, not an analysis failure."""
    texts = [(attempt / "builder.stderr.log").read_text(errors="replace")[-20000:]]
    if final:
        texts.append(json.dumps({k: final.get(k) for k in ("result", "api_error_status", "subtype", "is_error")}))
        if not final.get("is_error") and not final.get("api_error_status"):
            texts = texts[:1]
    return any(API_FAILURE.search(text) for text in texts)


def scrub_token(token: str, roots: list[Path]) -> int:
    """Replace the exact token bytes in every regular file under roots; return the number of replacements."""
    if not token:
        return 0
    needle, total = token.encode(), 0
    for root in roots:
        for path in [root] if root.is_file() else sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            count = data.count(needle)
            if count:
                mode = path.stat().st_mode
                path.chmod(mode | 0o200)
                path.write_bytes(data.replace(needle, b"[REDACTED_CLAUDE_ACCESS_TOKEN]"))
                path.chmod(mode)
                total += count
    return total


def completion(work: Path, job: dict) -> tuple[dict, dict] | None:
    """Re-hash a completed job and re-validate its raw output; refuse any change."""
    task_id = job["task_id"]
    path = work / "jobs" / task_id / "completion.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{task_id}: completion must be a regular file")
    done = load(path)
    if (not isinstance(done, dict) or done.get("status") != "complete" or done.get("task_id") != task_id
            or not isinstance(done.get("attempt"), str) or not ATTEMPT.fullmatch(done["attempt"])):
        raise ValueError(f"{task_id}: saved completion is invalid")
    if done.get("job_sha256") != _digest(job):
        raise ValueError(f"{task_id}: completed job no longer matches its frozen plan job")
    attempt = path.parent / done["attempt"]
    origin = done.get("result_origin") or {}
    artifacts = done.get("artifact_sha256") or {}
    if (origin.get("file") not in {"result.json", "work/summary.json"} or artifacts.get(origin["file"]) != origin.get("sha256")
            or not set(ARTIFACTS) <= set(artifacts)):
        raise ValueError(f"{task_id}: completion lacks its artifact hashes")
    for name, digest in {**artifacts, "attempt.json": done.get("attempt_sha256")}.items():
        target = attempt / name
        if (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or target.is_symlink()
                or not target.is_file() or v6.sha(target) != digest):
            raise ValueError(f"{task_id}: completed builder artifact changed: {name}")
    if {key: value for key, value in done.items() if key != "attempt_sha256"} != load(attempt / "attempt.json"):
        raise ValueError(f"{task_id}: completion disagrees with its attempt record")
    if not v7_seed.is_requested_model((done.get("model_audit") or {}).get("init_model")):
        raise ValueError(f"{task_id}: completion was not started by the requested model")
    analysis, completions = validate_analysis(_load_output(attempt / origin["file"]), job)
    if analysis != load(attempt / "analysis.json") or completions != done.get("path_completions"):
        raise ValueError(f"{task_id}: saved analysis disagrees with the raw builder output")
    return done, analysis


# ----- Published analysis files -----

def _write_bytes(path: Path, data: bytes, mode: int = 0o444) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _writers(audit: dict) -> str:
    shares = audit.get("output_share_by_model") or {}
    text = (", ".join(f"{model} ({share:.1%} of output tokens)" for model, share in
                      sorted(shares.items(), key=lambda item: (-item[1], item[0])))
            or ", ".join(audit.get("models_used") or []) or "an unrecorded model")
    if audit.get("refusal_fallbacks"):
        text += " after a Claude Code safeguard-refusal fallback"
    return text


def _ref(sources: list[dict]) -> str:
    def short(path: str) -> str:
        parts = path.split("/")
        return "card" if path.endswith(".md") else f"{parts[-3]}/{parts[-2]}"
    return "; ".join(f"{short(s['path'])} L{s['line_start']}-{s['line_end']}" for s in sources)


def render_analysis(task_id: str, value: dict, job: dict, audit: dict) -> str:
    count, guide = job["attempt_count"], value["decision_guide"]
    out = [f"# What {count} failed attempts at {task_id} suggest", "",
           f"Failure analysis requested from {MODEL} ({EFFORT} effort); written by {_writers(audit)}. "
           f"It reads {count} officially graded attempts by other agents ({job['models_text']}). Every attempt "
           "scored 0. The analyst never saw the hidden tests or grading output, so causes are hypotheses unless "
           "marked observed. Nothing here has been verified on this task.", "", "## Read this first"]
    out += [f"- First: {x}" for x in guide["first_steps"]]
    out += [f"- Avoid: {x}" for x in guide["avoid"]]
    out += [f"- Before finishing: {x}" for x in guide["before_finishing"]]
    out += ["", "## The task as the analyst reads it", value["task_summary"], "", "## Requirements to watch"]
    out += [f"- [{r['risk']}] {r['requirement']} Attempts: {r['how_attempts_handled_it']}" for r in value["requirements"]]
    out += ["", "## How the failed attempts went"]
    for family in value["approach_families"]:
        out += [f"- **{family['name']}** ({family['attempt_count']} attempts; verdict: {family['verdict'].replace('_', ' ')}). "
                f"Furthest: {family['furthest_progress']} Broke: {family['where_it_broke']} "
                f"Evidence: {family['evidence']} [{_ref(family['sources'])}]"]
    if value["shared_assumptions"]:
        out += ["", "## What (nearly) every attempt assumed"]
        out += [f"- {a['assumption']} Why suspect: {a['why_suspect']}" + (f" [{_ref(a['sources'])}]" if a["sources"] else "")
                for a in value["shared_assumptions"]]
    if value["verification_gaps"]:
        out += ["", "## Checks that could not detect the problem"]
        out += [f"- {g['check_used']} Misses: {g['what_it_misses']}" for g in value["verification_gaps"]]
    out += ["", "## Lessons"]
    for number, lesson in enumerate(value["lessons"], 1):
        out += ["", f"### {number}. {lesson['title']}",
                f"({lesson['failure_mode'].replace('_', ' ')}; {lesson['attempts_affected']} attempts; cause {lesson['cause_confidence']})",
                f"- Observed: {lesson['observed_failure']}", f"- Likely cause: {lesson['cause_hypothesis']}",
                f"- Do instead: {lesson['what_to_do_instead']}", f"- Check: {lesson['check_that_would_catch_it']}",
                f"- Keywords: {', '.join(lesson['keywords'])}", f"- Sources: {_ref(lesson['sources'])}"]
    if value["reusable_details"]:
        out += ["", "## Reusable details from the failed attempts"]
        out += [f"- **{d['what']}**: {d['detail']}" + (f" Caveat: {d['caveat']}" if d["caveat"].strip() else "") +
                f" [{_ref(d['sources'])}]" for d in value["reusable_details"]]
    if value["unexplained"].strip():
        out += ["", "## Still unexplained", value["unexplained"]]
    return "\n".join(out) + "\n"


def publish_analysis(corpus: Path, work: Path, job: dict, done: dict, analysis: dict) -> Path:
    """Copy the validated analysis into <corpus>/analyses/<task>; never replace a different one."""
    task_id = job["task_id"]
    root = corpus / "analyses"
    target = root / task_id
    for path in (root, target):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("analysis destination must be a regular directory")
    target.mkdir(parents=True, exist_ok=True)
    data = (work / "jobs" / task_id / done["attempt"] / "analysis.json").read_bytes()
    markdown = render_analysis(task_id, analysis, job, done["model_audit"]).encode()
    if {path.name for path in target.iterdir()} - {"analysis.json", "analysis.md"}:
        raise ValueError(f"{task_id}: unexpected files in the published analysis directory")
    existing = target / "analysis.json"
    if existing.is_symlink() or (existing.exists() and existing.read_bytes() != data):
        raise ValueError(f"{task_id}: refuse to replace a different published analysis")
    if not existing.exists():
        _write_bytes(existing, data)
    rendered = target / "analysis.md"  # a derived view; re-rendered if the renderer changed
    if rendered.is_symlink() or not rendered.exists() or rendered.read_bytes() != markdown:
        _write_bytes(rendered, markdown)
    return target


# ----- Conversion to memory entries -----

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def entry_slug(task_id: str, number: int, title: str) -> str:
    prefix = _slug(task_id)[:60].rstrip("-") or "task"
    return f"{prefix}-{number}-{_slug(title)}"[:80].rstrip("-")


def _merge_sources(sources: list[dict], slug: str, report: dict) -> list[dict]:
    seen, merged = set(), []
    for source in sources:
        key = (source["path"], source["line_start"], source["line_end"])
        if key not in seen:
            seen.add(key)
            merged.append({"path": key[0], "line_start": key[1], "line_end": key[2]})
    if len(merged) > MAX_SOURCES:
        report["caps"].append({"slug": slug, "field": "sources", "available": len(merged), "kept": MAX_SOURCES})
    return merged[:MAX_SOURCES]


def _clip(text: str, slug: str, field: str, report: dict) -> str:
    if len(text) <= MAX_TEXT:
        return text
    report["truncations"].append({"slug": slug, "field": field, "original_chars": len(text), "kept_chars": MAX_TEXT})
    return text[:MAX_TEXT - len(TRUNCATED)] + TRUNCATED


def convert_analysis(task_id: str, value: dict, attempt_count: int) -> tuple[list[dict], dict]:
    """Deterministically turn one validated analysis into schema-valid memory entries."""
    families, assumptions, lessons, details = (value[section] for section in SECTIONS)
    report, entries, n = {"caps": [], "truncations": []}, [], attempt_count
    guide, gaps = value["decision_guide"], value["verification_gaps"]
    slug = entry_slug(task_id, 0, "overview")
    evidence = [value["task_summary"], "", "Requirements:"]
    evidence += [f"- [{r['risk']}] {r['requirement']} — {r['how_attempts_handled_it']}" for r in value["requirements"]]
    evidence += ["", "Approach families:"]
    evidence += [f"- {f['name']} ({f['attempt_count']} attempts; {f['verdict'].replace('_', ' ')}): furthest "
                 f"{f['furthest_progress']}; broke {f['where_it_broke']}; evidence {f['evidence']}" for f in families]
    if value["unexplained"].strip():
        evidence += ["", f"Unexplained: {value['unexplained']}"]
    verification = "\n".join(f"{number}. {gap['check_used']} — misses: {gap['what_it_misses']}"
                             for number, gap in enumerate(gaps, 1)) or NO_GAPS
    pooled = ([s for f in families for s in f["sources"]] + [s for a in assumptions for s in a["sources"]]
              + [s for lesson in lessons for s in lesson["sources"]])
    entries.append({
        "slug": slug, "title": f"Failure analysis overview: {task_id}", "keywords": [task_id, *OVERVIEW_KEYWORDS],
        "applicability": f"Only the current task {task_id}. Summarises its {n} officially failed attempts. "
                         "Read this first, then the lesson entries.",
        "procedure": [f"First: {x}" for x in guide["first_steps"]] + [f"Before finishing: {x}" for x in guide["before_finishing"]],
        "pitfalls": [f"Avoid: {x}" for x in guide["avoid"]] +
                    [f"Shared assumption (suspect): {a['assumption']} — {a['why_suspect']}" for a in assumptions],
        "verification": _clip(verification, slug, "verification", report), "status": "uncertain",
        "sources": _merge_sources(pooled, slug, report),
        "evidence_summary": _clip("\n".join(evidence), slug, "evidence_summary", report)})
    for number, lesson in enumerate(lessons, 1):
        slug = entry_slug(task_id, number, lesson["title"])
        mode = lesson["failure_mode"].replace("_", " ")
        status = ("source_observed" if lesson["cause_confidence"] == "observed" and lesson["failure_mode"] != "unknown"
                  else "uncertain")
        evidence = (f"Observed failure: {lesson['observed_failure']}\n"
                    f"Failure mode: {mode}\n"
                    f"Attempts affected (analyst count, unverified): {lesson['attempts_affected']} of {n}\n"
                    f"Cause ({lesson['cause_confidence']}): {lesson['cause_hypothesis']}\n"
                    f"Proposed fix (unverified): {lesson['what_to_do_instead']}\n"
                    "All cited attempts at this same task received reward 0; nothing here was verified on this task.")
        entries.append({
            "slug": slug, "title": lesson["title"], "keywords": [*lesson["keywords"], task_id, mode],
            "applicability": f"Only the current task {task_id}; lesson {number} of {len(lessons)} from its {n} failed attempts.",
            "procedure": [lesson["what_to_do_instead"]], "pitfalls": [lesson["observed_failure"]],
            "verification": lesson["check_that_would_catch_it"], "status": status,
            "sources": _merge_sources(lesson["sources"], slug, report),
            "evidence_summary": _clip(evidence, slug, "evidence_summary", report)})
    if details:
        slug = entry_slug(task_id, 9, "reusable details")
        entries.append({
            "slug": slug, "title": f"Reusable details: {task_id}", "keywords": [task_id, *DETAIL_KEYWORDS],
            "applicability": f"Only the current task {task_id}. Concrete details recorded from its {n} officially "
                             "failed attempts.",
            "procedure": [f"{d['what']}: {d['detail']}" + (f" (caveat: {d['caveat']})" if d["caveat"].strip() else "")
                          for d in details],
            "pitfalls": [],
            "verification": "Re-check each detail in the current environment before relying on it; details may "
                            "come from failed attempts.",
            "status": "uncertain", "sources": _merge_sources([s for d in details for s in d["sources"]], slug, report),
            "evidence_summary": f"Recorded by the failure analysis from this task's {n} officially failed attempts "
                                "(reward 0). Nothing here was verified on this task."})
    for entry in entries:
        _validate(entry, _entry, f"entry {entry['slug']}")
    if len({entry["slug"] for entry in entries}) != len(entries):
        raise ValueError(f"{task_id}: converted entries have duplicate slugs")
    return entries, report


# ----- Build, status and export -----

def _lock(work: Path):
    lock = (work / "builder.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(f"Another V7 builder or export holds {work / 'builder.lock'}") from None
    return lock


def run_job(base: Path, work: Path, corpus: Path, pool: Path, plan: dict, job: dict, support_info: dict, *,
            engine: Callable, token_provider: Callable, max_attempts: int) -> dict:
    """Run fresh attempts until one validates or the model-attempt cap is reached."""
    task_id = job["task_id"]
    while True:
        if _STOP.is_set():
            return {"task_id": task_id, "status": "interrupted"}
        attempts = _attempts(work, task_id)
        used = sum(bool(item.get("model_call_performed")) and item.get("status") not in NON_COUNTING_STATUSES
                   for item in attempts)
        if used >= max_attempts:
            return {"task_id": task_id, "status": "failed", "model_attempts": used,
                    "last_attempt_status": attempts[-1].get("status") if attempts else None}
        _log({"event": "builder_attempt_started", "task_id": task_id, "timeout_seconds": job["timeout_seconds"],
              "model_attempts_used": used, "max_attempts": max_attempts})
        result = run_attempt(base, work, pool, plan, job, support_info, engine=engine,
                             token_provider=token_provider, max_attempts=max_attempts)
        _log({"event": "builder_attempt_finished", "task_id": task_id, "attempt": result["attempt"],
              "status": result["status"], "elapsed_seconds": round(result.get("elapsed_seconds") or 0, 1),
              "total_cost_usd": (result.get("usage") or {}).get("total_cost_usd"),
              "refusal_fallbacks": len((result.get("model_audit") or {}).get("refusal_fallbacks") or [])})
        if result["status"] == "complete":
            done = completion(work, job)
            publish_analysis(corpus, work, job, *done)
            return {"task_id": task_id, "status": "complete", "attempt": result["attempt"]}
        if result["status"] == "api_unavailable":
            _STOP.set()  # the account cannot serve requests now: stop scheduling instead of burning attempts
        if result["status"] in NON_COUNTING_STATUSES:
            return {"task_id": task_id, "status": result["status"], "attempt": result["attempt"]}


def build(corpus, work_dir, *, base: Path = BASE, workers: int = 6, tasks: list[str] | None = None,
          max_attempts: int = 2, engine: Callable[[EngineRequest], dict] | None = None,
          support_fn: Callable[[], dict] | None = None, credentials: Path = CLAUDE_CREDENTIALS,
          token_provider: Callable[[float], str] | None = None, claude_binary: Path | None = None,
          accept_code_update: bool = False) -> dict:
    """Run every missing job (or the selected ones) in parallel workers; reuse verified completions."""
    base = Path(base)
    corpus, work = _path(base, corpus), _path(base, work_dir)
    if not 1 <= workers <= 16 or not 1 <= max_attempts <= 5:
        raise ValueError("workers must be 1..16 and max-attempts 1..5")
    if work.resolve().is_relative_to(corpus.resolve()) or corpus.resolve().is_relative_to(work.resolve()):
        raise ValueError("The builder work directory must be separate from the corpus")
    engine = engine or claude_engine
    token_provider = token_provider or (lambda seconds: claude_access_token(credentials, seconds))
    support_fn = support_fn or (lambda: support(base, Path(claude_binary) if claude_binary else find_claude_binary()))
    work.mkdir(parents=True, exist_ok=True)
    with _lock(work):
        plan = build_plan(corpus, base=base)
        freeze_plan(work, plan, accept_code_update=accept_code_update)
        jobs = {job["task_id"]: job for job in plan["jobs"]}
        unknown = sorted(set(tasks or []) - set(jobs))
        if unknown:
            raise ValueError(f"No builder job for task(s) {unknown}; they are unknown or have no readable failures")
        selected = [jobs[task] for task in sorted(set(tasks or jobs))]
        rows, pending = [], []
        for job in selected:
            done = completion(work, job)
            if done is None:
                pending.append(job)
                continue
            publish_analysis(corpus, work, job, *done)
            rows.append({"task_id": job["task_id"], "status": "complete", "attempt": done[0]["attempt"], "reused": True})
        if pending:
            support_info = resolve_support(work, support_fn)
            _STOP.clear()
            previous = None
            if threading.current_thread() is threading.main_thread():
                previous = signal.signal(signal.SIGTERM, _raise_interrupt)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(pending)))
            try:
                futures = [executor.submit(run_job, base, work, corpus, _pool_dir(base, corpus, job["task_id"], job),
                                           plan, job, support_info, engine=engine, token_provider=token_provider,
                                           max_attempts=max_attempts) for job in pending]
                for future in concurrent.futures.as_completed(futures):
                    rows.append(future.result())
            except (KeyboardInterrupt, SystemExit):
                # Stop means stop: no queued job starts and no killed attempt is retried or counted.
                _STOP.set()
                executor.shutdown(wait=False, cancel_futures=True)
                stop_active_containers()
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                if previous is not None:
                    signal.signal(signal.SIGTERM, previous)
        summary = status(work, lock_held=False)
        v6.write_json(work / "status.json", summary)
        return {**summary, "selected_tasks": [job["task_id"] for job in selected],
                "this_run": sorted(rows, key=lambda row: row["task_id"])}


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def _lock_held(work: Path) -> bool:
    path = work / "builder.lock"
    if not path.is_file():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def status(work_dir, *, base: Path = BASE, lock_held: bool | None = None) -> dict:
    """Summarise jobs without a model call or any re-hash: complete/failed/pending, attempts, fallbacks, cost."""
    work = _path(base, work_dir)
    if not (work / "plan.json").is_file():
        return {"status": "not_started", "work_dir": str(work)}
    plan = load(work / "plan.json")
    running = _lock_held(work) if lock_held is None else lock_held
    rows, totals, cost = [], Counter(), 0.0
    for job in plan["jobs"]:
        job_dir = work / "jobs" / job["task_id"]
        attempts = _attempts(work, job["task_id"])
        done = load(job_dir / "completion.json") if (job_dir / "completion.json").is_file() else None
        last = attempts[-1] if attempts else {}
        state = ("complete" if done else "pending" if not attempts else
                 "running" if last.get("status") == "running" and running else "failed")
        fallbacks = [item for attempt in attempts for item in (attempt.get("model_audit") or {}).get("refusal_fallbacks") or []]
        spent = sum((attempt.get("usage") or {}).get("total_cost_usd") or 0 for attempt in attempts)
        audit = (done or {}).get("model_audit") or {}
        rows.append({"task_id": job["task_id"], "status": state, "attempts": len(attempts),
                     "model_attempts": sum(bool(attempt.get("model_call_performed")) for attempt in attempts),
                     "last_attempt_status": last.get("status"), "last_error": (last.get("error") or "")[:300] or None,
                     "refusal_fallbacks": len(fallbacks),
                     "fallback_models": sorted({str(item.get("fallback_model")) for item in fallbacks}),
                     "models_used": audit.get("models_used"),
                     "strict_model_audit_verified": audit.get("strict_model_audit_verified"),
                     "lesson_count": (done or {}).get("lesson_count"), "cost_usd": round(spent, 4),
                     "failed_attempts_on_record": job["attempt_count"], "timeout_seconds": job["timeout_seconds"]})
        totals[state] += 1
        cost += spent
    return {"status": "complete" if totals["complete"] == len(rows) else ("running" if running else "incomplete"),
            "work_dir": str(work), "corpus": plan["corpus"], "builder_model_requested": MODEL,
            "builder_effort_requested": EFFORT, "total_jobs": len(rows), "complete": totals["complete"],
            "failed": totals["failed"], "pending": totals["pending"], "running": totals["running"],
            "attempts": sum(row["attempts"] for row in rows),
            "jobs_with_refusal_fallbacks": sum(row["refusal_fallbacks"] > 0 for row in rows),
            "strict_model_audit_verified_jobs": sum(row["strict_model_audit_verified"] is True for row in rows),
            "total_cost_usd": round(cost, 4), "tasks_without_trajectories": plan["tasks_without_trajectories"],
            "tasks_without_readable_trajectories": plan.get("tasks_without_readable_trajectories", []), "jobs": rows}


# ----- Export gate: the builder had API egress, so verify isolation from the logged stream -----

BENCHMARK_SOURCE = re.compile(
    r"tbench\.ai|harbor[-_]?framework|laude[-_]institute|marginlab\.ai|spylab\.ai|original-tasks/|supabase\.co|"
    r"huggingface\.co/datasets/[^\s'\"]*terminal-bench|github\.com/[^\s'\"]*terminal-bench|terminal-bench-(?:1|2|science|core)", re.I)
HARNESS_FILES = re.compile(r"test_outputs\.py|solve\.sh|/solution/")
NETWORK_CODE = re.compile(r"urllib|requests\.|http\.client|httpx|aiohttp|socket\.|/dev/tcp|fetch\(|XMLHttpRequest")
_PROGRAM = re.compile(r"(?:^|[;&|()`]|\$\()\s*(?:sudo\s+|env\s+\S+\s+)*(curl|wget|ncat|nc|telnet|ssh|scp|rsync|pip3?|"
                      r"apt(?:-get)?|git|npm|npx|node|claude|/opt/claude/claude)\b")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\1\b", re.S)


def executed_outside(command: str) -> list[str]:
    """Network programs a Bash command really invokes; quoted grep patterns and written file bodies do not count."""
    hits = set()
    for _, body in _HEREDOC.findall(command):
        head = command.split("<<")[0].split("\n")[-1]
        if re.search(r"\b(python3?|node|bash|sh)\b", head) and NETWORK_CODE.search(body):
            hits.add("network code in an executed heredoc")
    bare = _HEREDOC.sub("<<HEREDOC", command)
    bare = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", bare)
    hits |= {m.group(1) for m in _PROGRAM.finditer(bare)}
    if re.search(r"\bpython3?\s+-c\b", command) and NETWORK_CODE.search(command):
        hits.add("network code in python -c")
    return sorted(hits)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def isolation_gate(base: Path, work: Path, task_id: str, done: dict, analysis: dict) -> dict:
    """Refuse-by-default facts for one completed analysis: outside access in its stream, hidden-material overlap."""
    outside, sources = set(), set()
    for event in _events(work / "jobs" / task_id / done["attempt"] / "claude.events.jsonl"):
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            data = block.get("input") or {}
            text = json.dumps(data, ensure_ascii=False)
            sources |= {m.group(0).lower() for m in BENCHMARK_SOURCE.finditer(text)}
            if block.get("name") == "Bash":
                outside |= set(executed_outside(str(data.get("command", ""))))
            if (block.get("name") in ("Write", "Edit") and re.search(r"\.(py|sh|js|mjs|pl|rb)$", str(data.get("file_path", "")))
                    and NETWORK_CODE.search(str(data.get("content") or data.get("new_string") or ""))):
                outside.add("network code written to a script")
    from . import v7_corpus
    text = "\n".join(_strings(analysis))
    references, _ = v7_corpus.reference_material(base / "prepared/tasks" / task_id)
    metrics = v7_corpus.contamination(text, references)
    coverage = (metrics.get("review") or {}).get("solution_coverage") or 0
    harness = sorted({m.group(0) for m in HARNESS_FILES.finditer(text)})
    analysis_sources = sorted({m.group(0).lower() for m in BENCHMARK_SOURCE.finditer(text)})
    reasons = ([f"outside access: {sorted(outside)}"] if outside else []) + \
              ([f"benchmark source in tool inputs: {sorted(sources)}"] if sources else []) + \
              ([f"analysis covers {coverage:.2f} of the reference solution"] if coverage >= 0.5 else []) + \
              ([f"analysis shares {metrics['tests_overlap_max_run']} tokens with hidden tests"]
               if (metrics.get("tests_overlap_max_run") or 0) >= 60 else []) + \
              ([f"analysis names harness files {harness}"] if harness else []) + \
              ([f"analysis cites benchmark sources {analysis_sources}"] if analysis_sources else [])
    return {"flagged": bool(reasons), "reasons": reasons, "outside_access": sorted(outside),
            "benchmark_source_in_inputs": sorted(sources), "solution_coverage": coverage,
            "solution_overlap_max_run": (metrics.get("review") or {}).get("solution_overlap_max_run"),
            "tests_overlap_max_run": metrics.get("tests_overlap_max_run")}


def export(corpus, work_dir, *, base: Path = BASE, allow_missing: bool = False) -> dict:
    """Convert every completed analysis into <corpus>/seeds/<task> and write seeds_index.json."""
    base = Path(base)
    corpus, work = _path(base, corpus), _path(base, work_dir)
    if not (work / "plan.json").is_file():
        raise ValueError("No frozen builder plan in the work directory")
    with _lock(work):
        frozen = load(work / "plan.json")
        plan = build_plan(corpus, base=base)  # re-verifies every pool against the index
        if _without_code(plan) != _without_code(frozen):
            raise ValueError("Corpus, pools or builder settings changed since the plan was frozen")
        index = load(corpus / "index.json")
        jobs = {job["task_id"]: job for job in plan["jobs"]}
        completed, missing = {}, []
        for task_id in sorted(index["tasks"]):
            if task_id in plan["tasks_without_trajectories"]:
                continue
            done = completion(work, jobs[task_id]) if task_id in jobs else None
            if done is None:
                missing.append(task_id)
            else:
                completed[task_id] = done
        if missing and not allow_missing:
            raise ValueError(f"No completed builder job for {missing}; rerun build or pass --allow-missing")
        reviews_path = work / "export_reviews.json"  # {task_id: reason} written by a human after reading the stream
        reviews = load(reviews_path) if reviews_path.is_file() else {}
        gates = {task_id: isolation_gate(base, work, task_id, *done) for task_id, done in completed.items()}
        blocked = {task_id: gate["reasons"] for task_id, gate in gates.items() if gate["flagged"] and task_id not in reviews}
        if blocked:
            raise ValueError(f"Isolation gate flagged {blocked}; review the streams and record decisions in {reviews_path}")
        support_sha = v6.sha(work / "support.json") if (work / "support.json").is_file() else None
        rows, created, reused = {}, [], []
        for task_id in sorted(index["tasks"]):
            if task_id in plan["tasks_without_trajectories"]:
                rows[task_id] = {"seed_path": None, "reason": "no_available_failed_trajectories"}
                continue
            if task_id not in completed:
                rows[task_id] = {"seed_path": None, "reason": "builder_failed"}
                continue
            job, (done, analysis) = jobs[task_id], completed[task_id]
            publish_analysis(corpus, work, job, done, analysis)
            entries, report = convert_analysis(task_id, analysis, job["attempt_count"])
            audit = done["model_audit"]
            provenance = {
                "builder_model_requested": MODEL, "builder_effort_requested": EFFORT,
                "models_used": audit["models_used"], "refusal_fallbacks": audit["refusal_fallbacks"],
                "strict_model_audit_verified": audit["strict_model_audit_verified"],
                "source_selection": SOURCE_SELECTION, "task_id": task_id, "init_model": audit["init_model"],
                "output_tokens_by_model": audit["output_tokens_by_model"],
                "output_share_by_model": audit["output_share_by_model"], "effort_verified": False,
                "builder_engine": ENGINE, "claude_version": done.get("claude_version"),
                "claude_binary_sha256": done.get("claude_binary_sha256"), "builder_attempt": done["attempt"],
                "builder_plan_sha256": v6.sha(work / "plan.json"), "builder_support_sha256": support_sha,
                "completion_sha256": v6.sha(work / "jobs" / task_id / "completion.json"),
                "analysis_sha256": done["artifact_sha256"]["analysis.json"],
                "entries_sha256": _digest(entries), "corpus_index_sha256": plan["corpus_index_sha256"],
                "corpus_complete": plan["corpus_complete"], "failed_attempt_count": job["attempt_count"],
                "path_completions": len(done["path_completions"]),
                "conversion": report, "network_command_count": done["tool_audit"]["network_command_count"],
                "isolation_gate": {**gates[task_id], "review": reviews.get(task_id)}}
            pool, seed = corpus / "pools" / task_id, corpus / "seeds" / task_id
            if seed.exists():
                manifest = v7_seed.validate_seed(seed, pool=pool, task_id=task_id)
                kept = manifest["provenance"]
                if any(kept.get(key) != provenance[key] for key in ("completion_sha256", "analysis_sha256", "entries_sha256")):
                    raise ValueError(f"{task_id}: refuse to overwrite an existing seed built from another analysis")
                reused.append(task_id)
            else:
                manifest = v7_seed.create_seed(seed, entries, pool=pool, task_id=task_id, provenance=provenance)
                v7_seed.validate_seed(seed, pool=pool, task_id=task_id)
                created.append(task_id)
            pool_path = PurePosixPath(index["tasks"][task_id]["pool_path"])
            rows[task_id] = {"seed_path": (pool_path.parent.parent / "seeds" / task_id).as_posix(),
                             "manifest_sha256": v6.sha(seed / "manifest.json"), "entry_count": manifest["entry_count"]}
        value = {"schema_version": 1, "tasks": rows}
        path = corpus / "seeds_index.json"
        changed = not path.is_file() or load(path) != value
        if changed:
            v6.write_json(path, value)
        return {"seeds_index": str(path), "seeds_index_sha256": v6.sha(path), "seeds_index_changed": changed,
                "seeds_created": created, "seeds_reused": reused, "missing": missing, "tasks": rows}


def _plan_view(plan: dict) -> dict:
    keep = ("corpus", "build_id", "corpus_complete", "builder_engine", "builder_model_requested", "builder_effort_requested",
            "disallowed_tools", "max_lessons", "job_count", "tasks_without_trajectories",
            "tasks_without_readable_trajectories")
    return {**{key: plan[key] for key in keep},
            "jobs": [{"task_id": job["task_id"], "attempt_count": job["attempt_count"], "models": job["models_text"],
                      "citable_trajectories": len(job["records"]), "timeout_seconds": job["timeout_seconds"],
                      "candidate_bytes": job["candidate_bytes"],
                      "unreadable_trajectories": [item["pool_file"] for item in job["unreadable_trajectories"]]}
                     for job in plan["jobs"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build", "status", "export"):
        command = commands.add_parser(name)
        command.add_argument("--base", type=Path, default=BASE, help="benchmark directory for relative paths")
        if name != "status":
            command.add_argument("--corpus", required=True, help="prepared/v7_corpus/<build_id>")
        if name != "plan":
            command.add_argument("--work-dir", required=True, help="runs/v7_memory_build_<id>")
        if name == "build":
            command.add_argument("--workers", type=int, default=6)
            command.add_argument("--tasks", nargs="+")
            command.add_argument("--max-attempts", type=int, default=2)
            command.add_argument("--credentials", type=Path, default=CLAUDE_CREDENTIALS)
            command.add_argument("--claude-binary", type=Path)
            command.add_argument("--accept-code-update", action="store_true",
                                 help="record an explicit amendment for builder source-code-only changes")
        if name == "export":
            command.add_argument("--allow-missing", action="store_true",
                                 help="record tasks without a completed job as builder_failed")
    args = parser.parse_args(argv)
    code = 0
    if args.command == "plan":
        result = _plan_view(build_plan(args.corpus, base=args.base))
    elif args.command == "status":
        result = status(args.work_dir, base=args.base)
    elif args.command == "export":
        result = export(args.corpus, args.work_dir, base=args.base, allow_missing=args.allow_missing)
    else:
        result = build(args.corpus, args.work_dir, base=args.base, workers=args.workers, tasks=args.tasks,
                       max_attempts=args.max_attempts, credentials=args.credentials.expanduser(),
                       claude_binary=args.claude_binary, accept_code_update=args.accept_code_update)
        code = 0 if all(row["status"] == "complete" for row in result["jobs"]
                        if row["task_id"] in result["selected_tasks"]) else 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
