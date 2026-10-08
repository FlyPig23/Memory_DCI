"""Freeze training failure lessons and clone them into isolated test memories.

The seed has no test identity or event ledger. Only training trajectory evidence
is accepted; a lesson's status never certifies that it works on an unseen task.
The experiment protocol must additionally pin manifest.json's SHA256 so a seed
cannot be replaced between test tasks.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .tools import _entry, _json, _now, _render_indexes, _validate, _write, audit_memory, initialize_memory

SCHEMA_VERSION = 1
_ALLOWED_STATUSES = {"source_observed", "uncertain"}
_REQUIRED_PROVENANCE = {"builder_model", "builder_reasoning_effort", "source_selection", "model_audit_verified"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular_path(path: Path | str, *, must_exist: bool = True) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("path traversal is forbidden")
    path = path.absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"symlinks are forbidden: {item}")
    if must_exist and not path.exists():
        raise ValueError(f"missing path: {path}")
    return path


def _training_ids(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value)
        or value in {".", ".."} for value in values
    ):
        raise ValueError("training_task_ids must be nonempty safe task names")
    if len(values) != len(set(values)):
        raise ValueError("duplicate training task IDs")
    return sorted(values)


def _provenance(value: dict) -> dict:
    if not isinstance(value, dict) or not _REQUIRED_PROVENANCE.issubset(value):
        raise ValueError(f"provenance requires {sorted(_REQUIRED_PROVENANCE)}")
    for key in ("builder_model", "builder_reasoning_effort"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"invalid provenance {key}")
    if value["source_selection"] != "failed_training_trajectories":
        raise ValueError("seed provenance must select failed training trajectories")
    if value["model_audit_verified"] is not True:
        raise ValueError("builder model/effort audit must be verified before freezing")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("provenance must be finite JSON data") from exc


def _source(source: dict, pool: Path, training: set[str]) -> dict:
    path = PurePosixPath(source["path"])
    if (not path.is_absolute() or ".." in path.parts
            or len(path.parts) < 5 or path.parts[:3] != ("/", "pool", "trajectories")
            or str(path) != source["path"]):
        raise ValueError("seed sources must be canonical /pool/trajectories/<training-task>/... paths")
    if path.parts[3] not in training:
        raise ValueError(f"source is not a training task: {path.parts[3]}")
    if path.name != f"{path.parts[3]}_0.txt":
        raise ValueError("failure seed sources must retain the training task's score-zero filename")
    actual = _regular_path(pool.joinpath(*path.parts[2:]))
    if not actual.is_file():
        raise ValueError("trajectory source must be a regular file")
    data = actual.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("trajectory source must be UTF-8 text") from exc
    header = text[:4000]
    reward = re.search(r"^Official reward:\s*([^;\r\n]+)", header, re.MULTILINE)
    if reward and reward.group(1).strip() not in {"0", "0.0"}:
        raise ValueError("trajectory header contradicts its score-zero filename")
    scored = re.search(r"^Official scored execution:\s*([^;\r\n]+)", header, re.MULTILINE)
    if scored and scored.group(1).strip() != "True":
        raise ValueError("unscored infrastructure attempts cannot seed failure lessons")
    lines = data.splitlines(keepends=True)
    first, last = source["line_start"], source["line_end"]
    if first < 1 or last < first or last > len(lines):
        raise ValueError("trajectory source line range is invalid or out of bounds")
    window = b"".join(lines[first - 1:last])
    if b"\x00" in data:
        # A rendered terminal transcript can include a binary file dump or
        # fixed-width record padding elsewhere. That does not make a separate
        # textual command/result window binary. Keep the original bytes and
        # line numbering, and permit this only for explicitly scored historical
        # training exports; never accept a NUL inside the cited evidence itself.
        expected_title = f"# Historical training trajectory: {path.parts[3]}"
        if b"\x00" in window:
            raise ValueError("cited trajectory window cannot contain binary NUL bytes")
        if (text.splitlines()[0] != expected_title or reward is None or scored is None
                or reward.group(1).strip() not in {"0", "0.0"}
                or scored.group(1).strip() != "True"):
            raise ValueError("binary NUL outside a citation requires an explicitly scored historical training export")
    return {**source, "sha256": _sha(data), "lines_sha256": _sha(window)}


def _prepare_entry(value: dict, pool: Path, training: set[str], timestamp: str) -> dict:
    _validate(value, _entry, "seed entry")
    if value["status"] not in _ALLOWED_STATUSES:
        raise ValueError("seed status must be source_observed or uncertain; unseen tasks are not locally verified")
    slug = value.get("slug") or re.sub(r"[^a-z0-9]+", "-", value["title"].lower()).strip("-")[:65]
    if not slug:
        slug = "lesson-" + _sha(value["title"].encode())[:12]
    checked = {**value, "slug": slug,
               "sources": [_source(source, pool, training) for source in value["sources"]],
               "updated_at": timestamp}
    checked["content_sha256"] = _sha(_body(checked).encode())
    return checked


def _body(entry: dict) -> str:
    lines = [f"# {entry['title']}", "", f"Status: {entry['status']}",
             f"Keywords: {', '.join(entry['keywords'])}", "", "## Applicability", entry["applicability"],
             "", "## Procedure"]
    lines += [f"{number}. {step}" for number, step in enumerate(entry["procedure"], 1)]
    lines += ["", "## Pitfalls"] + [f"- {item}" for item in entry["pitfalls"]]
    lines += ["", "## Verification", entry["verification"], "", "## Evidence", entry["evidence_summary"], ""]
    lines += [f"- {source['path']}:{source['line_start']}-{source['line_end']} (sha256 {source['sha256']})"
              for source in entry["sources"]]
    lines += ["", "Inherited from frozen training failure memory. Validate applicability on the current task; "
              "a status is an evidence assessment, not an official score."]
    return "\n".join(lines) + "\n"


def _seed_indexes(catalog: dict) -> dict[str, str]:
    full = ["# Frozen training failure memory", "", "Training evidence only. This library contains no test-task state.",
            "Source observations and proposed checks do not prove a repair succeeds on the current task.", ""]
    summary = ["# Training failure memory navigation", "", "Search this index, then read only applicable entries.", ""]
    for slug, entry in sorted(catalog.items()):
        full += [f"## {entry['title']}", f"- File: entries/{slug}.md", f"- Keywords: {', '.join(entry['keywords'])}",
                 f"- Applies when: {entry['applicability']}", f"- Status: {entry['status']}", ""]
        summary.append(f"- {entry['title']} → entries/{slug}.md | {', '.join(entry['keywords'][:8])} | {entry['status']}")
    return {"MEMORY.md": "\n".join(full) + "\n", "memory_summary.md": "\n".join(summary) + "\n"}


def _inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        _regular_path(path)
        if path.is_file():
            result[str(path.relative_to(root))] = _sha(path.read_bytes())
        elif not path.is_dir():
            raise ValueError("seed contains a non-regular filesystem object")
    return result


def create_seed(destination: Path | str, entries: list[dict], *, pool: Path,
                training_task_ids: list[str], provenance: dict) -> dict:
    """Create a new immutable-by-contract seed, refusing any existing content."""
    destination = _regular_path(destination, must_exist=False)
    pool = _regular_path(pool)
    training = _training_ids(training_task_ids)
    provenance = _provenance(provenance)
    if not isinstance(entries, list) or not entries:
        raise ValueError("a frozen failure memory must contain at least one lesson")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("refuse to overwrite an existing seed")
    timestamp = _now()
    catalog = {}
    for entry in entries:
        checked = _prepare_entry(entry, pool, set(training), timestamp)
        if checked["slug"] in catalog:
            raise ValueError(f"duplicate seed slug: {checked['slug']}")
        catalog[checked["slug"]] = checked
    payload = {".entry_metadata.json": _json(catalog), **_seed_indexes(catalog)}
    payload.update({f"entries/{slug}.md": _body(entry) for slug, entry in catalog.items()})
    pool_manifest = pool / "manifest.json"
    if pool_manifest.exists():
        _regular_path(pool_manifest)
    manifest = {"schema_version": SCHEMA_VERSION, "kind": "training_failure_memory_seed", "created_at": timestamp,
                "entry_count": len(catalog), "training_task_ids": training, "provenance": provenance,
                "pool_manifest_sha256": _sha(pool_manifest.read_bytes()) if pool_manifest.is_file() else None,
                "files_sha256": {name: _sha(content.encode()) for name, content in sorted(payload.items())}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for name, content in payload.items():
            _write(temporary, name, content)
        _write(temporary, "manifest.json", _json(manifest))
        validate_seed(temporary, pool=pool, training_task_ids=training)
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def validate_seed(seed_path: Path | str, *, pool: Path, training_task_ids: list[str]) -> dict:
    """Recheck every seed byte, source digest, line window and training membership."""
    root, pool = _regular_path(seed_path), _regular_path(pool)
    training = _training_ids(training_task_ids)
    if not root.is_dir():
        raise ValueError("seed must be a directory")
    actual = _inventory(root)
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        catalog = json.loads((root / ".entry_metadata.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("seed has missing or invalid JSON") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "training_failure_memory_seed":
        raise ValueError("unsupported seed manifest")
    if manifest.get("training_task_ids") != training:
        raise ValueError("seed training membership differs from the frozen split")
    _provenance(manifest.get("provenance"))
    expected = manifest.get("files_sha256")
    if not isinstance(expected, dict) or {key: value for key, value in actual.items() if key != "manifest.json"} != expected:
        raise ValueError("seed file inventory/hash differs from manifest")
    pool_manifest = pool / "manifest.json"
    if pool_manifest.exists():
        _regular_path(pool_manifest)
    pool_digest = _sha(pool_manifest.read_bytes()) if pool_manifest.is_file() else None
    if manifest.get("pool_manifest_sha256") != pool_digest:
        raise ValueError("seed pool manifest hash differs")
    if not isinstance(catalog, dict) or not catalog or manifest.get("entry_count") != len(catalog):
        raise ValueError("seed must contain a positive, consistent entry count")
    timestamp = manifest.get("created_at")
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("seed creation timestamp is missing")
    for slug, checked in catalog.items():
        if not isinstance(checked, dict):
            raise ValueError("invalid seed entry metadata")
        value = {key: value for key, value in checked.items() if key not in {"updated_at", "content_sha256"}}
        value["sources"] = [{key: value for key, value in source.items() if key not in {"sha256", "lines_sha256"}}
                            for source in value.get("sources", [])]
        rebuilt = _prepare_entry(value, pool, set(training), timestamp)
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


def initialize_seeded_memory(memory_path: Path | str, task_id: str, seed_path: Path | str, *,
                             pool: Path, training_task_ids: list[str]) -> dict:
    """Clone a seed once with a fresh identity and zero inherited test events."""
    root = _regular_path(memory_path, must_exist=False)
    seed = _regular_path(seed_path)
    manifest = validate_seed(seed, pool=pool, training_task_ids=training_task_ids)
    if not isinstance(task_id, str) or not task_id.strip() or task_id in manifest["training_task_ids"]:
        raise ValueError("seeded test task must have a non-training identity")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("seed initialization requires a fresh empty task memory")
    manifest_digest = _sha((seed / "manifest.json").read_bytes())
    identity = initialize_memory(root, task_id)
    catalog = json.loads((seed / ".entry_metadata.json").read_text())
    entry_hashes = {name: digest for name, digest in manifest["files_sha256"].items() if name.startswith("entries/")}
    for name in (".entry_metadata.json", *sorted(entry_hashes)):
        _write(root, name, (seed / name).read_text())
    identity.update({"inherited_entries": manifest["entry_count"], "seed_manifest_sha256": manifest_digest,
                     "seed_entry_files_sha256": entry_hashes, "seed_scope": "frozen_training_failures"})
    _write(root, "identity.json", _json(identity))
    _render_indexes(root, task_id, catalog)
    audit = audit_memory(root)
    audit.update({"seed_manifest_sha256": manifest_digest, "inherited_entries": manifest["entry_count"],
                  "initial_seed_file_hashes": entry_hashes})
    if not audit["valid"] or audit["event_count"] != 0 or audit["entry_count"] != manifest["entry_count"]:
        raise ValueError(f"invalid initial seeded memory: {audit['issues']}")
    return audit
