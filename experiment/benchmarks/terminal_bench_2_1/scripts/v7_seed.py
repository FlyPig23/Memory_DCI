"""Freeze one test task's own failure analysis and clone it into that task's memory.

A V7 seed belongs to exactly one task. Its entries may cite only that task's
officially failed attempts in its same-task pool, or its task card; no success,
other task or test-time state is accepted. A status never certifies that a
lesson works on the current task. The protocol must additionally pin
manifest.json's SHA256 so a seed cannot be replaced before its task runs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from experiment.shared.memory.seed import _inventory, _regular_path, _sha
from experiment.shared.memory.tools import (
    _entry, _json, _now, _render_indexes, _validate, _write, audit_memory, initialize_memory,
)

SCHEMA_VERSION = 1
KIND = "same_task_failure_memory_seed"
POOL_KIND = "same_task_failed_official_trajectories"
SOURCE_SELECTION = "failed_same_task_official_trajectories"
REQUESTED_MODEL, REQUESTED_EFFORT = "claude-opus-5-5", "high"
SEED_SCOPE = "frozen_same_task_failures"
_ALLOWED_STATUSES = {"source_observed", "uncertain"}
_REQUIRED_PROVENANCE = {"builder_model_requested", "builder_effort_requested", "models_used",
                        "refusal_fallbacks", "strict_model_audit_verified", "source_selection", "task_id"}
_MANIFEST_KEYS = {"schema_version", "kind", "created_at", "entry_count", "task_id", "provenance",
                  "pool_manifest_sha256", "files_sha256"}
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ALIAS = re.compile(r"[A-Za-z0-9._-]+")
_ATTEMPT = re.compile(r"attempt-\d{3,}")
_FOOTER = ("Inherited from this task's frozen failure analysis, written before testing from its own failed "
           "official attempts. Validate applicability on the current task; a status is an evidence assessment, "
           "not an official score.")


def is_requested_model(name) -> bool:
    """Claude Code may append a context-window or date suffix; the model itself must match."""
    return isinstance(name, str) and re.fullmatch(
        re.escape(REQUESTED_MODEL) + r"(?:\[[^\]]*\]|-\d{8})?", name) is not None


def _task_id(value) -> str:
    if not isinstance(value, str) or not _TASK.fullmatch(value) or value in {".", ".."}:
        raise ValueError("task_id must be a safe task name")
    return value


def _provenance(value: dict, task_id: str) -> dict:
    """Require an honest builder record; a refusal fallback is accepted, never hidden."""
    if not isinstance(value, dict) or not _REQUIRED_PROVENANCE.issubset(value):
        raise ValueError(f"provenance requires {sorted(_REQUIRED_PROVENANCE)}")
    if value["builder_model_requested"] != REQUESTED_MODEL or value["builder_effort_requested"] != REQUESTED_EFFORT:
        raise ValueError(f"seed provenance must request {REQUESTED_MODEL} at {REQUESTED_EFFORT} effort")
    models = value["models_used"]
    if (not isinstance(models, list) or not models
            or any(not isinstance(model, str) or not model.strip() for model in models)
            or len(set(models)) != len(models)):
        raise ValueError("provenance models_used must be a nonempty list of distinct model names")
    fallbacks = value["refusal_fallbacks"]
    if not isinstance(fallbacks, list) or any(not isinstance(item, dict) for item in fallbacks):
        raise ValueError("provenance refusal_fallbacks must be a list of records")
    strict = value["strict_model_audit_verified"]
    if type(strict) is not bool:
        raise ValueError("provenance strict_model_audit_verified must be a boolean")
    # A strict audit may be false (accepted fallback); a true one must be consistent.
    if strict and (fallbacks or not any(is_requested_model(model) for model in models)):
        raise ValueError("a strict model audit cannot include a refusal fallback or omit the requested model")
    if value["source_selection"] != SOURCE_SELECTION:
        raise ValueError("seed provenance must select this task's failed official trajectories")
    if value["task_id"] != task_id:
        raise ValueError("seed provenance belongs to another task")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("provenance must be finite JSON data") from exc


def _pool_manifest_sha256(pool: Path, task_id: str) -> str:
    """Bind the seed to a failure-only pool that belongs to this task."""
    path = _regular_path(pool / "manifest.json")
    if not path.is_file():
        raise ValueError("same-task pool manifest is missing")
    data = path.read_bytes()
    try:
        manifest = json.loads(data)
    except ValueError as exc:
        raise ValueError("same-task pool manifest is not JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("kind") != POOL_KIND or manifest.get("task_id") != task_id
            or manifest.get("successful_trajectories_included") is not False
            or manifest.get("cross_task_memory") is not False):
        raise ValueError("pool is not this task's failure-only same-task pool")
    return _sha(data)


def _header_value(line: str, name: str) -> str | None:
    match = re.match(re.escape(name) + r":\s*([^;\r\n]+)", line)
    return match.group(1).strip() if match else None


def _source(source: dict, pool: Path, task_id: str) -> dict:
    path = PurePosixPath(source["path"])
    parts = path.parts
    if not path.is_absolute() or ".." in parts or str(path) != source["path"]:
        raise ValueError("seed sources must be canonical absolute /pool paths")
    card = parts == ("/", "pool", "tasks", f"{task_id}.md")
    trajectory = (len(parts) == 7 and parts[:4] == ("/", "pool", "trajectories", task_id)
                  and _ALIAS.fullmatch(parts[4]) is not None and _ATTEMPT.fullmatch(parts[5]) is not None
                  and parts[6] == f"{task_id}_0.txt")
    if not card and not trajectory:
        raise ValueError("seed sources must be this task's /pool/trajectories/<task>/<alias>/<attempt_id>/"
                         "<task>_0.txt or exactly /pool/tasks/<task>.md")
    actual = _regular_path(pool.joinpath(*parts[2:]))
    if not actual.is_file():
        raise ValueError("seed source must be a regular file")
    data = actual.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("seed source must be UTF-8 text") from exc
    lines = data.splitlines(keepends=True)
    first, last = source["line_start"], source["line_end"]
    if first < 1 or last < first or last > len(lines):
        raise ValueError("seed source line range is invalid or out of bounds")
    window = b"".join(lines[first - 1:last])
    if b"\x00" in window:
        # A rendered transcript may hold a binary dump elsewhere; the cited evidence must be text.
        raise ValueError("cited source window cannot contain binary NUL bytes")
    if trajectory:
        header = [line.rstrip("\r") for line in text.split("\n", 4)[:4]]
        if (len(header) < 4 or header[0] != f"# Historical failed attempt at this task: {task_id}"
                or _header_value(header[2], "Official reward") != "0"
                or _header_value(header[3], "Official scored execution") != "True"):
            raise ValueError("trajectory source must be this task's explicitly scored failed attempt (reward 0)")
    return {**source, "sha256": _sha(data), "lines_sha256": _sha(window)}


def _body(entry: dict) -> str:
    lines = [f"# {entry['title']}", "", f"Status: {entry['status']}",
             f"Keywords: {', '.join(entry['keywords'])}", "", "## Applicability", entry["applicability"],
             "", "## Procedure"]
    lines += [f"{number}. {step}" for number, step in enumerate(entry["procedure"], 1)]
    lines += ["", "## Pitfalls"] + [f"- {item}" for item in entry["pitfalls"]]
    lines += ["", "## Verification", entry["verification"], "", "## Evidence", entry["evidence_summary"], ""]
    lines += [f"- {source['path']}:{source['line_start']}-{source['line_end']} (sha256 {source['sha256']})"
              for source in entry["sources"]]
    lines += ["", _FOOTER]
    return "\n".join(lines) + "\n"


def _prepare_entry(value: dict, pool: Path, task_id: str, timestamp: str) -> dict:
    _validate(value, _entry, "seed entry")
    if value["status"] not in _ALLOWED_STATUSES:
        raise ValueError("seed status must be source_observed or uncertain; the current task is not locally verified")
    slug = value.get("slug") or re.sub(r"[^a-z0-9]+", "-", value["title"].lower()).strip("-")[:65]
    if not slug:
        slug = "lesson-" + _sha(value["title"].encode())[:12]
    checked = {**value, "slug": slug,
               "sources": [_source(source, pool, task_id) for source in value["sources"]],
               "updated_at": timestamp}
    checked["content_sha256"] = _sha(_body(checked).encode())
    return checked


def _seed_indexes(catalog: dict) -> dict[str, str]:
    full = ["# Frozen same-task failure analysis", "",
            "Written only from this task's officially failed attempts; contains no successful attempt and no test-task state.",
            "Observations and proposed checks do not prove a repair succeeds on the current task.", ""]
    summary = ["# Same-task failure analysis navigation", "", "Search this index, then read only applicable entries.", ""]
    for slug, entry in sorted(catalog.items()):
        full += [f"## {entry['title']}", f"- File: entries/{slug}.md", f"- Keywords: {', '.join(entry['keywords'])}",
                 f"- Applies when: {entry['applicability']}", f"- Status: {entry['status']}", ""]
        summary.append(f"- {entry['title']} → entries/{slug}.md | {', '.join(entry['keywords'][:8])} | {entry['status']}")
    return {"MEMORY.md": "\n".join(full) + "\n", "memory_summary.md": "\n".join(summary) + "\n"}


def create_seed(destination: Path | str, entries: list[dict], *, pool: Path, task_id: str,
                provenance: dict) -> dict:
    """Create a new immutable-by-contract same-task seed, refusing any existing content."""
    destination = _regular_path(destination, must_exist=False)
    pool = _regular_path(pool)
    task_id = _task_id(task_id)
    provenance = _provenance(provenance, task_id)
    pool_digest = _pool_manifest_sha256(pool, task_id)
    if not isinstance(entries, list) or not entries:
        raise ValueError("a frozen failure analysis must contain at least one entry")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("refuse to overwrite an existing seed")
    timestamp = _now()
    catalog = {}
    for entry in entries:
        checked = _prepare_entry(entry, pool, task_id, timestamp)
        if checked["slug"] in catalog:
            raise ValueError(f"duplicate seed slug: {checked['slug']}")
        catalog[checked["slug"]] = checked
    payload = {".entry_metadata.json": _json(catalog), **_seed_indexes(catalog)}
    payload.update({f"entries/{slug}.md": _body(entry) for slug, entry in catalog.items()})
    manifest = {"schema_version": SCHEMA_VERSION, "kind": KIND, "created_at": timestamp,
                "entry_count": len(catalog), "task_id": task_id, "provenance": provenance,
                "pool_manifest_sha256": pool_digest,
                "files_sha256": {name: _sha(content.encode()) for name, content in sorted(payload.items())}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for name, content in payload.items():
            _write(temporary, name, content)
        _write(temporary, "manifest.json", _json(manifest))
        validate_seed(temporary, pool=pool, task_id=task_id)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def validate_seed(seed_path: Path | str, *, pool: Path, task_id: str) -> dict:
    """Recheck every seed byte, source digest, line window and the single-task binding."""
    root, pool = _regular_path(seed_path), _regular_path(pool)
    task_id = _task_id(task_id)
    if not root.is_dir():
        raise ValueError("seed must be a directory")
    actual = _inventory(root)
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        catalog = json.loads((root / ".entry_metadata.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("seed has missing or invalid JSON") from exc
    if (not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS
            or manifest["schema_version"] != SCHEMA_VERSION or manifest["kind"] != KIND):
        raise ValueError("unsupported same-task seed manifest")
    if manifest["task_id"] != task_id:
        raise ValueError("seed belongs to another task")
    _provenance(manifest["provenance"], task_id)
    expected = manifest["files_sha256"]
    if not isinstance(expected, dict) or {key: value for key, value in actual.items() if key != "manifest.json"} != expected:
        raise ValueError("seed file inventory/hash differs from manifest")
    if manifest["pool_manifest_sha256"] != _pool_manifest_sha256(pool, task_id):
        raise ValueError("seed pool manifest hash differs")
    if (not isinstance(catalog, dict) or not catalog or type(manifest["entry_count"]) is not int
            or manifest["entry_count"] != len(catalog)):
        raise ValueError("seed must contain a positive, consistent entry count")
    timestamp = manifest["created_at"]
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("seed creation timestamp is missing")
    for slug, checked in catalog.items():
        if (not isinstance(checked, dict) or not isinstance(checked.get("sources"), list)
                or any(not isinstance(source, dict) for source in checked["sources"])):
            raise ValueError("invalid seed entry metadata")
        value = {key: value for key, value in checked.items() if key not in {"updated_at", "content_sha256"}}
        value["sources"] = [{key: value for key, value in source.items() if key not in {"sha256", "lines_sha256"}}
                            for source in value.get("sources", [])]
        rebuilt = _prepare_entry(value, pool, task_id, timestamp)
        if rebuilt != checked or slug != checked["slug"]:
            raise ValueError(f"seed source provenance or entry metadata differs: {slug}")
        if expected.get(f"entries/{slug}.md") != rebuilt["content_sha256"]:
            raise ValueError(f"seed entry body differs from structured metadata: {slug}")
    allowed = {".entry_metadata.json", "MEMORY.md", "memory_summary.md"} | {f"entries/{slug}.md" for slug in catalog}
    if set(expected) != allowed:
        raise ValueError("seed must not contain test identities, events or unrelated files")
    for name, content in _seed_indexes(catalog).items():
        if _sha(content.encode()) != expected[name]:
            raise ValueError(f"seed navigation does not match entries: {name}")
    return manifest


def initialize_same_task_memory(memory_path: Path | str, task_id: str, seed_path: Path | str, *,
                                pool: Path) -> dict:
    """Clone this task's own seed once into a fresh memory with zero inherited events."""
    root = _regular_path(memory_path, must_exist=False)
    seed = _regular_path(seed_path)
    manifest = validate_seed(seed, pool=pool, task_id=task_id)  # refuses another task's seed
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("seed initialization requires a fresh empty task memory")
    manifest_digest = _sha((seed / "manifest.json").read_bytes())
    identity = initialize_memory(root, task_id)
    catalog = json.loads((seed / ".entry_metadata.json").read_text())
    entry_hashes = {name: digest for name, digest in manifest["files_sha256"].items() if name.startswith("entries/")}
    for name in (".entry_metadata.json", *sorted(entry_hashes)):
        # Decode the exact bytes: text-mode reads would translate a model-written carriage return.
        _write(root, name, (seed / name).read_bytes().decode("utf-8"))
        if _sha((root / name).read_bytes()) != manifest["files_sha256"][name]:
            raise ValueError(f"seed file changed while it was copied: {name}")
    identity.update({"inherited_entries": manifest["entry_count"], "seed_manifest_sha256": manifest_digest,
                     "seed_entry_files_sha256": entry_hashes, "seed_scope": SEED_SCOPE})
    _write(root, "identity.json", _json(identity))
    _render_indexes(root, task_id, catalog)
    audit = audit_memory(root)
    audit.update({"seed_manifest_sha256": manifest_digest, "inherited_entries": manifest["entry_count"],
                  "initial_seed_file_hashes": entry_hashes})
    if not audit["valid"] or audit["event_count"] != 0 or audit["entry_count"] != manifest["entry_count"]:
        raise ValueError(f"invalid initial same-task memory: {audit['issues']}")
    return audit
