"""V8 builder helpers retained from the training-failure builder, without its retired workflow."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid
CLI_VERSION = "0.153.4"
DEFAULT_IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
ADAPTIVE_BUDGET = {"mode": "adaptive_failure_volume_v1", "base_seconds": 300,
                   "seconds_per_trajectory": 30, "seconds_per_ceil_mebibyte": 60,
                   "minimum_seconds": 600, "maximum_seconds": 3600,
                   "rationale": "Heuristic wall-clock ceiling from failed-source count and size; not empirically optimized or a target duration."}

def sha(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)

def checked_file(root: Path, relative: str) -> Path:
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise ValueError("Unsafe pool path")
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Pool root cannot be a symlink")
    path = root
    for piece in part.parts:
        path = path / piece
        if path.is_symlink():
            raise ValueError("Pool paths cannot contain symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Missing or escaping pool source")
    return path

def budget_policy(timeout_seconds: int | None) -> dict:
    if timeout_seconds is None:
        return copy.deepcopy(ADAPTIVE_BUDGET)
    if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 3600:
        raise ValueError("Explicit fixed builder budget must be 60..3600 seconds")
    return {"mode": "fixed_override", "timeout_seconds": timeout_seconds}

def job_budget(records: list[dict], timeout_seconds: int | None = None) -> dict:
    policy = budget_policy(timeout_seconds)
    count, size = len(records), sum(row["bytes"] for row in records)
    mib = (size + 1024 * 1024 - 1) // (1024 * 1024)
    unbounded = 300 + 30 * count + 60 * mib
    seconds = max(600, min(3600, unbounded)) if timeout_seconds is None else timeout_seconds
    return {"candidate_bytes": size, "timeout_seconds": seconds,
            "budget_basis": {"mode": policy["mode"], "candidate_count": count,
                             "candidate_bytes": size, "ceil_mebibytes": mib,
                             "unbounded_seconds": unbounded if timeout_seconds is None else timeout_seconds}}

def line_excerpt(lines: list[str], budget: int) -> tuple[str, list[int]]:
    """Prioritize terminal error observations and final state, preserving line IDs."""
    matcher = re.compile(r"traceback|exception|error:|failed|failure|assertion|not found|timeout|out of memory|killed|permission denied", re.I)
    hits = [i for i, line in enumerate(lines) if matcher.search(line)]
    # Last three failure clusters and tail are deterministic, independent of test tasks.
    priority = []
    for i in reversed(hits[-3:]):
        priority.extend(range(max(0, i - 2), min(len(lines), i + 4)))
    priority.extend(range(max(0, len(lines) - 14), len(lines)))
    priority.extend(range(min(8, len(lines))))
    chosen, used = {}, 0
    for i in priority:
        if i in chosen:
            continue
        line = lines[i]
        rendered = f"L{i + 1}: {line[:450]}" + (" [LONG LINE TRUNCATED]" if len(line) > 450 else "") + "\n"
        if used + len(rendered) <= budget:
            chosen[i] = rendered
            used += len(rendered)
    return "".join(chosen[i] for i in sorted(chosen)), [i + 1 for i in sorted(chosen)]

def evidence_packet(records: list[dict], pool: Path, total_chars: int) -> tuple[str, dict]:
    if not records:
        raise ValueError("A builder job requires at least one source")
    headers = [f"\n## {r['path']}\ntrial={r['trial_id']}; reward=0; source={r['status']}; original_lines={r['line_count']}\n" for r in records]
    available = total_chars - sum(map(len, headers))
    if available < 100 * len(records):
        raise ValueError("Packet budget cannot give every candidate an excerpt")
    each = available // len(records)
    chunks, coverage = [], []
    for record, header in zip(records, headers):
        lines = checked_file(pool, record["pool_file"]).read_text().splitlines()
        excerpt, numbers = line_excerpt(lines, each)
        chunks.append(header + excerpt)
        coverage.append({"path": record["path"], "excerpted_original_lines": numbers,
                         "total_original_lines": len(lines), "source_sha256": record["sha256"]})
    packet = "".join(chunks)
    assert len(packet) <= total_chars
    return packet, {"candidate_count": len(records), "packet_chars": len(packet),
                    "excerpt_coverage": coverage, "complete_reading_claimed": False}

def support(base: Path, image: str) -> dict:
    binary = base / "runtime/bin/codex"
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != "codex-cli " + CLI_VERSION:
        raise ValueError("Pinned builder Codex version mismatch")
    python = list((base / "runtime/python").glob("*/bin/python3"))
    if len(python) != 1:
        raise ValueError("Expected one pinned standalone Python installation")
    certificate = base / "prepared/transport/ca-certificates.crt"
    if not certificate.is_file():
        raise ValueError("Prepared transport CA bundle missing")
    inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                               text=True, capture_output=True)
    if inspected.returncode:
        subprocess.run(["docker", "pull", image], check=True, timeout=600)
        inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                   check=True, text=True, capture_output=True)
    python_root = python[0].parents[1]
    return {"image_id": inspected.stdout.strip(), "cli_version": version,
            "python_root": str(python_root), "python_version": subprocess.check_output([str(python[0]), "--version"], text=True).strip(),
            "binary_sha256": {p.name: sha(p) for p in sorted((base / "runtime/bin").iterdir()) if p.is_file()},
            "python_installation_sha256": {str(p.relative_to(python_root)): sha(p) for p in sorted(python_root.rglob("*"))
                                           if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"},
            "ca_sha256": sha(certificate)}
