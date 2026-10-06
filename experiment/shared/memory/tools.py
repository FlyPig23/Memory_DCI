"""V5 task-local DCI operations; run command execution inside the task container.

The command API deliberately preserves agent-written rg/sed/regex/pipelines.
Its working directories and task selections are navigation aids, NOT a sandbox.
The runner must mount the build-only pool read-only and exactly one task memory.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid

STATUSES = ("source_observed", "locally_verified", "uncertain", "contradicted")
MAX_TEXT = 12000
MAX_OUTPUT = 24000
MAX_CAPTURE = 2 * 1024 * 1024


def _object(properties, required):
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


_text = {"type": "string", "minLength": 1, "maxLength": MAX_TEXT}
_source = _object({"path": _text, "line_start": {"type": "integer", "minimum": 1},
                   "line_end": {"type": "integer", "minimum": 1}},
                  ["path", "line_start", "line_end"])
_entry = _object({
    "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,79}$"},
    "title": _text, "keywords": {"type": "array", "items": _text, "minItems": 1, "maxItems": 30},
    "applicability": _text, "procedure": {"type": "array", "items": _text, "minItems": 1, "maxItems": 30},
    "pitfalls": {"type": "array", "items": _text, "maxItems": 30},
    "verification": _text, "status": {"type": "string", "enum": list(STATUSES)},
    "sources": {"type": "array", "items": _source, "minItems": 1, "maxItems": 20},
    "evidence_summary": _text,
}, ["title", "keywords", "applicability", "procedure", "pitfalls", "verification", "status", "sources", "evidence_summary"])
_search = {"query": _text, "command": _text,
           "assessment": {"type": "string", "maxLength": MAX_TEXT,
                          "description": "Brief public evidence/applicability note: adopt, reject, or undecided and why; no private chain of thought."},
           "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 30}}
TOOLS = [
    {"name": "reason", "description": "Record only a concise task goal, observed blocker, and next action. This is a workflow checkpoint, not a request to expose private reasoning. Native Codex tools still perform the task.",
     "inputSchema": _object({"goal": _text, "blocker": {"type": "string", "maxLength": MAX_TEXT},
                              "next_action": _text,
                              "assessment": {"type": "string", "maxLength": MAX_TEXT,
                                             "description": "Optional concise adopt/reject/undecided decision about evidence just read."}},
                             ["goal", "blocker", "next_action"])},
    {"name": "DCI_search_task", "description": "Search/read build-task descriptions using YOUR bash command (rg regex, multiple patterns, pipes, sed, etc.). Working directory is /pool/tasks. First choose a concise query from the current task. Return matching task IDs, then inspect whether they share operations/tools/constraints; no match is a valid outcome. Use reason to record adopt/reject after observing results.",
     "inputSchema": _object(_search, ["query", "command"])},
    {"name": "DCI_search_trajectory", "description": "Search/read trajectories of selected BUILD tasks using YOUR bash command. Select task IDs discovered by DCI_search_task. Filenames include task name and historical score; model identity is in paths/headers. Score is a retrieval cue, not proof that every step works. With one task cwd is its directory; with several cwd is /pool/trajectories and DCI_SELECTED_DIRS contains the selected paths. Read the action and observation around a hit, then record adopt/reject via reason. Pool is mounted read-only by runner.",
     "inputSchema": _object({**_search, "selected_task_ids": {"type": "array", "items": _text, "minItems": 1, "maxItems": 36}}, ["query", "command", "selected_task_ids"])},
    {"name": "DCI_search_memory", "description": "Search/read only this task's memory using YOUR bash command, cwd /memory. Read memory_summary.md or MEMORY.md to navigate entries/*.md. .tool_outputs and events.jsonl are execution logs, not distilled knowledge. No other test task's memory is present. Reuse relevant evidence; do not read all entries indiscriminately.",
     "inputSchema": _object(_search, ["query", "command"])},
    {"name": "distill", "description": "Make an explicit write or no_update decision for this task's memory. Online writes may occur after learning from build trajectories or observing local execution. Final review must make a final decision before hidden grading. Write only evidence-backed useful lessons with applicability, procedure, pitfalls, verification, source line pointers and honest status. Source_observed means seen in a historical trace; locally_verified requires a current-task observation. No_update is correct when there is no useful new lesson. Update an existing entry by its slug; never overwrite another task's memory.",
     "inputSchema": _object({"decision": {"type": "string", "enum": ["write", "no_update"]},
                              "stage": {"type": "string", "enum": ["online", "final"]},
                              "reason": _text,
                              "entries": {"type": "array", "items": _entry, "maxItems": 12}},
                             ["decision", "stage", "reason"])},
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _validate(value, schema, location="arguments"):
    """Small validator for the restricted schemas above; no third-party imports."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{location} must be an object")
        props = schema["properties"]
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ValueError(f"{location} missing {sorted(missing)}")
        if not schema.get("additionalProperties", True) and set(value) - set(props):
            raise ValueError(f"{location} unknown fields: {sorted(set(value) - set(props))}")
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{location}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{location} must be an array")
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000):
            raise ValueError(f"{location} array length out of bounds")
        for n, item in enumerate(value):
            _validate(item, schema["items"], f"{location}[{n}]")
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"{location} must be a string")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 100000):
            raise ValueError(f"{location} string length out of bounds")
        if schema.get("minLength", 0) and not value.strip():
            raise ValueError(f"{location} cannot be whitespace")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{location} invalid format")
    elif kind in ("integer", "number"):
        if type(value) not in ((int,) if kind == "integer" else (int, float)):
            raise ValueError(f"{location} must be {kind}")
        if not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
            raise ValueError(f"{location} number out of bounds")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} must be one of {schema['enum']}")


def _safe_path(root: Path, relative: str) -> Path:
    """Refuse traversal and existing symlinks for all managed writes."""
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError("managed memory path must be relative without traversal")
    if root.is_symlink():
        raise ValueError("memory root cannot be a symlink")
    candidate = root
    for piece in part.parts:
        candidate = candidate / piece
        if candidate.is_symlink():
            raise ValueError("managed memory paths cannot contain symlinks")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("managed path escaped task memory")
    return candidate


def _write(root: Path, relative: str, content: str):
    target = _safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _safe_path(root, str(target.relative_to(root)) + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        _safe_path(root, relative)  # recheck before replacing destination
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _catalog(root: Path) -> dict:
    path = _safe_path(root, ".entry_metadata.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _render_indexes(root: Path, task_id: str, entries: dict):
    lines = [f"# Memory for {task_id}", "", "Task-local memory only. No entries are inherited from another test task.", "",
             "Statuses are evidence assessments, not benchmark grades. Source-observed experience still needs an environment check.", ""]
    summary = [f"# Memory navigation — {task_id}", "", "Read this compact index first, then only relevant entries. Search with rg under entries/.", ""]
    if not entries:
        lines.append("No distilled entries yet. An explicit no_update decision is valid.")
        summary.append("No distilled entries yet.")
    for slug, entry in sorted(entries.items()):
        lines.extend([f"## {entry['title']}", f"- File: entries/{slug}.md", f"- Keywords: {', '.join(entry['keywords'])}",
                      f"- Applies when: {entry['applicability']}", f"- Status: {entry['status']}", ""])
        summary.append(f"- {entry['title']} → entries/{slug}.md | {', '.join(entry['keywords'][:8])} | {entry['status']}")
    _write(root, "MEMORY.md", "\n".join(lines) + "\n")
    _write(root, "memory_summary.md", "\n".join(summary) + "\n")


def initialize_memory(path: Path | str, task_id: str) -> dict:
    root = Path(path).absolute()
    if root.is_symlink():
        raise ValueError("task memory root cannot be symlink")
    root.mkdir(parents=True, exist_ok=True)
    identity_path = _safe_path(root, "identity.json")
    if identity_path.exists():
        identity = json.loads(identity_path.read_text())
        if identity.get("task_id") != task_id:
            raise ValueError("cross-task memory reuse is forbidden")
    else:
        if any(root.iterdir()):
            raise ValueError("refuse nonempty memory without task identity")
        identity = {"task_id": task_id, "created_at": _now(), "schema_version": 1,
                    "scope": "single_test_task", "inherited_entries": 0}
        _write(root, "identity.json", _json(identity))
    _safe_path(root, "entries").mkdir(exist_ok=True)
    if not _safe_path(root, ".entry_metadata.json").exists():
        _write(root, ".entry_metadata.json", "{}\n")
    _render_indexes(root, task_id, _catalog(root))
    return identity


def audit_memory(path: Path | str) -> dict:
    root = Path(path)
    issues = []
    files = {}
    for file in sorted(root.rglob("*")):
        if file.is_symlink():
            issues.append(f"symlink: {file.relative_to(root)}")
        elif file.is_file():
            files[str(file.relative_to(root))] = hashlib.sha256(file.read_bytes()).hexdigest()
    def read(name, default):
        try:
            p = _safe_path(root, name)
            return json.loads(p.read_text()) if p.exists() else default
        except (ValueError, OSError) as exc:
            issues.append(f"{name}: {exc}")
            return default
    identity = read("identity.json", {})
    entries = read(".entry_metadata.json", {})
    final = read("final_decision.json", None)
    events = []
    ledger = _safe_path(root, "events.jsonl")
    if ledger.exists():
        for n, line in enumerate(ledger.read_text().splitlines(), 1):
            try:
                events.append(json.loads(line))
            except ValueError:
                issues.append(f"invalid event line {n}")
    actual = set(files) & {f"entries/{slug}.md" for slug in entries}
    if len(actual) != len(entries):
        issues.append("catalog references missing entry files")
    if {name for name in files if name.startswith("entries/")} != actual:
        issues.append("entry files are not fully represented in the structured catalog")
    for slug, entry in entries.items():
        if entry.get("content_sha256") != files.get(f"entries/{slug}.md"):
            issues.append(f"entry content differs from last distill write: {slug}")
        for index in ("MEMORY.md", "memory_summary.md"):
            p = _safe_path(root, index)
            if not p.is_file() or f"entries/{slug}.md" not in p.read_text():
                issues.append(f"{index} missing entry: {slug}")
    counts = dict(Counter(event.get("tool", "unknown") for event in events))
    if final is not None and (final.get("task_id") != identity.get("task_id") or final.get("stage") != "final"):
        issues.append("final decision task/stage mismatch")
    if final is not None:
        matching = [e for e in events if e.get("event_id") == final.get("event_id") and e.get("tool") == "distill"]
        if len(matching) != 1 or any(matching[0].get(k) != final.get(k) for k in
                                    ("task_id", "decision", "stage", "agent_stage", "reason", "entry_paths")):
            issues.append("final decision has no matching explicit distill event")
    if not identity.get("task_id") or identity.get("scope") != "single_test_task":
        issues.append("missing or invalid task identity")
    return {"task_id": identity.get("task_id"), "scope": identity.get("scope"),
            "entry_count": len(entries), "entry_paths": sorted(actual), "final_decision": final,
            "has_explicit_final_decision": final is not None, "event_counts": counts,
            "event_count": len(events), "files_sha256": files, "issues": issues,
            "valid": not issues}


class Tools:
    def __init__(self, pool: Path | str, memory: Path | str, task_id: str,
                 stage: str = "solve", evidence_root: Path | str = "/evidence",
                 workspace_root: Path | str = "/tmp_workspace"):
        if stage not in ("solve", "review"):
            raise ValueError("stage must be solve or review")
        self.pool = Path(pool).resolve(strict=True)
        self.memory = Path(memory).absolute()
        self.task_id, self.stage = task_id, stage
        initialize_memory(self.memory, task_id)
        self.source_roots = {"/pool": self.pool, "/evidence": Path(evidence_root).resolve(),
                             "/tmp_workspace": Path(workspace_root).resolve()}
        trajectories = self.pool / "trajectories"
        self.build_task_ids = {p.name for p in trajectories.iterdir() if p.is_dir() and not p.is_symlink()}
        if task_id in self.build_task_ids:
            raise ValueError("test task must not belong to build pool")

    def _event(self, tool: str, data: dict):
        record = {"event_id": uuid.uuid4().hex, "timestamp": _now(), "task_id": self.task_id,
                  "process_stage": self.stage, "agent_stage": self.stage, "tool": tool, **data}
        path = _safe_path(self.memory, "events.jsonl")
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def call(self, name, arguments):
        try:
            schema = next((item["inputSchema"] for item in TOOLS if item["name"] == name), None)
            if schema is None:
                raise ValueError("unknown tool")
            _validate(arguments, schema)
            if name == "reason":
                event = self._event(name, arguments)
                result = {"recorded": True, "event_id": event["event_id"], "next_action": arguments["next_action"]}
            elif name == "distill":
                result = self.distill(**arguments)
            else:
                result = self.search(name, **arguments)
            return {"content": [{"type": "text", "text": _json(result)}], "isError": False}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            try:
                self._event("tool_error", {"requested_tool": str(name), "error": f"{type(exc).__name__}: {exc}"})
            except (ValueError, OSError):
                pass  # Do not mask the original invalid-path error.
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}

    def search(self, name: str, query: str, command: str, assessment: str = "",
               timeout_seconds: float = 20, selected_task_ids: list[str] | None = None):
        selected = sorted(set(selected_task_ids or []))
        if name == "DCI_search_task":
            cwd = self.pool / "tasks"
        elif name == "DCI_search_trajectory":
            if not selected or not set(selected).issubset(self.build_task_ids):
                raise ValueError("selected_task_ids must be known build-task IDs")
            cwd = self.pool / "trajectories"
            if len(selected) == 1:
                cwd = cwd / selected[0]
        elif name == "DCI_search_memory":
            cwd = self.memory
        else:
            raise ValueError("not a search tool")
        directories = [str(self.pool / "trajectories" / item) for item in selected]
        env = dict(os.environ, DCI_SELECTED_TASK_IDS=json.dumps(selected), DCI_SELECTED_DIRS=json.dumps(directories),
                   DCI_TASK_ID=self.task_id, DCI_POOL=str(self.pool), DCI_MEMORY=str(self.memory))
        started = time.monotonic()
        proc = subprocess.Popen(["bash", "-o", "pipefail", "-c", command], cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        output = bytearray()
        timed_out = overflow = False
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, .2)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        break
                    output.extend(chunk[:max(0, MAX_CAPTURE - len(output))])
                    if len(output) >= MAX_CAPTURE:
                        overflow = True
                        break
                if overflow:
                    break
        finally:
            selector.close()
            # A child may retain stdout after its parent exits. Kill the whole group
            # on timeout/output overflow rather than leaving background commands.
            if timed_out or overflow:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.stdout.close()
            try:
                returncode = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                returncode = proc.wait(timeout=2)
                timed_out = True
        text = output.decode("utf-8", errors="replace")
        capture = f".tool_outputs/{uuid.uuid4().hex}.txt"
        _write(self.memory, capture, text)
        result = {"query": query, "cwd": str(cwd), "selected_task_ids": selected,
                  "selected_directories": directories, "returncode": returncode,
                  "timed_out": timed_out, "capture_limit_reached": overflow,
                  "output": bytes(output[:MAX_OUTPUT]).decode("utf-8", errors="replace"),
                  "output_truncated": len(output) > MAX_OUTPUT, "captured_bytes": len(output),
                  "captured_output_path": str(self.memory / capture),
                  "elapsed_seconds": round(time.monotonic() - started, 3),
                  "next_step": "Inspect applicable action/result windows. Record an adopt/reject/undecided assessment with reason; distill only a useful evidence-backed lesson. If output is clipped, read a targeted range from captured_output_path with native tools."}
        self._event(name, {"query": query, "command": command, "assessment": assessment,
                           **{k: v for k, v in result.items() if k not in ("query", "output", "next_step")}})
        return result

    def _source(self, source: dict):
        given = Path(source["path"])
        if not given.is_absolute() or ".." in given.parts:
            raise ValueError("source must be an absolute path without traversal")
        path = given
        for alias, root in self.source_roots.items():
            if given.is_relative_to(alias):
                path = root / given.relative_to(alias)
                break
        resolved = path.resolve(strict=True)
        allowed = [root for root in self.source_roots.values() if resolved.is_relative_to(root)]
        if not allowed or not resolved.is_file():
            raise ValueError("source must be a regular file inside pool, current-task evidence, or current workspace")
        if source["line_end"] < source["line_start"]:
            raise ValueError("source line_end precedes line_start")
        count = 0
        with resolved.open("rb") as stream:
            for count, line in enumerate(stream, 1):
                if b"\x00" in line:
                    raise ValueError("source pointer must target text, not a binary artifact")
                if count >= source["line_end"]:
                    break
        if count < source["line_end"]:
            raise ValueError("source line pointer exceeds file length")
        return {**source, "path": str(path), "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}

    def distill(self, decision: str, stage: str, reason: str, entries: list[dict] | None = None):
        if self.stage == "review" and stage != "final":
            raise ValueError("review process requires stage=final")
        entries = entries or []
        if decision == "no_update" and entries:
            raise ValueError("no_update cannot include entries")
        if decision == "write" and not entries:
            raise ValueError("write requires at least one structured entry")
        catalog = _catalog(self.memory)
        changed = []
        prepared = []
        for entry in entries:
            slug = entry.get("slug") or re.sub(r"[^a-z0-9]+", "-", entry["title"].lower()).strip("-")[:65]
            if not slug:
                slug = "lesson-" + hashlib.sha256(entry["title"].encode()).hexdigest()[:12]
            if any(slug == item[0] for item in prepared):
                raise ValueError("duplicate entry slugs in one decision")
            checked = {**entry, "slug": slug, "sources": [self._source(s) for s in entry["sources"]], "updated_at": _now()}
            if checked["status"] == "locally_verified" and all(Path(s["path"]).is_relative_to(self.pool) for s in checked["sources"]):
                raise ValueError("locally_verified requires a current-task evidence/workspace source, not only historical traces")
            prepared.append((slug, checked))
        for slug, entry in prepared:
            lines = [f"# {entry['title']}", "", f"Status: {entry['status']}", f"Keywords: {', '.join(entry['keywords'])}", "",
                     "## Applicability", entry["applicability"], "", "## Procedure"]
            lines += [f"{n}. {step}" for n, step in enumerate(entry["procedure"], 1)]
            lines += ["", "## Pitfalls"] + [f"- {item}" for item in entry["pitfalls"]]
            lines += ["", "## Verification", entry["verification"], "", "## Evidence", entry["evidence_summary"], ""]
            lines += [f"- {s['path']}:{s['line_start']}-{s['line_end']} (sha256 {s['sha256']})" for s in entry["sources"]]
            lines += ["", "This entry belongs only to the current test task. A status is an evidence assessment, not an official score."]
            content = "\n".join(lines) + "\n"
            _write(self.memory, f"entries/{slug}.md", content)
            entry["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            catalog[slug] = entry
            changed.append(f"entries/{slug}.md")
        if changed:
            _write(self.memory, ".entry_metadata.json", _json(catalog))
            _render_indexes(self.memory, self.task_id, catalog)
        record = {"task_id": self.task_id, "decision": decision, "stage": stage, "agent_stage": self.stage, "reason": reason,
                  "entry_paths": changed, "entry_count": len(catalog), "timestamp": _now()}
        event = self._event("distill", record)
        record["event_id"] = event["event_id"]
        if stage == "final":
            _write(self.memory, "final_decision.json", _json(record))
        return {**record, "memory_index": str(self.memory / "MEMORY.md"),
                "next_step": "Continue solving using relevant memory." if stage == "online" else "Final update decision recorded; hidden grading remains separate."}
