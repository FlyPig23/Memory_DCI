"""Project-private inference credentials and per-run registrations.

The proxy stores only SHA256 of the complete client Authorization header.
The gateway service key never enters a solver, route document, or audit record.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

CLIENT_AUTHORIZATION_HEADER = "X-Skill-DCI-Client-Authorization-SHA256"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _inside(path: Path, root: Path) -> Path:
    root = Path(root).resolve()
    path = Path(path).absolute()
    path.relative_to(root)
    current = root
    for part in path.relative_to(root).parts:
        if part in (".", ".."):
            raise ValueError("Unsafe project credential path")
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolic links are prohibited for inference access files")
    path.resolve().relative_to(root)
    return path


def _private_fd(path: Path, flags: int, *, limit: int | None = None) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise PermissionError("Inference access file must be a singly linked, owned regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("Inference access file must have mode 0600")
        if limit is not None and info.st_size > limit:
            raise ValueError("Inference access file exceeds its size bound")
        return fd
    except BaseException:
        os.close(fd)
        raise


def service_key_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / "experiment/benchmarks/wildclaw_bench/runtime/inference-private/service-key"


def read_service_key(path: Path, project_root: Path) -> str:
    path = _inside(path, project_root)
    fd = _private_fd(path, os.O_RDONLY, limit=257)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        value = stream.read(258).strip()
    if not _TOKEN.fullmatch(value):
        raise ValueError("Invalid inference service key file")
    return value


def ensure_service_key(project_root: Path) -> Path:
    """Return the stable private service-key path, generating once if absent."""
    root = Path(project_root).resolve()
    path = _inside(service_key_path(root), root)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("Inference private directory must be owned and mode 0700")
    lock = path.parent / "service-key.lock"
    fd = _private_fd(lock, os.O_RDWR | os.O_CREAT)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if not path.exists():
            key_fd = _private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(key_fd, "w", encoding="ascii") as output:
                output.write(secrets.token_urlsafe(32) + "\n")
                output.flush()
                os.fsync(output.fileno())
        read_service_key(path, root)
    return path


def authorization_sha256(authorization: str) -> str:
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()


def valid_authorization_hash(value: str) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def client_token(authorization: str) -> str | None:
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):]
    return token if _TOKEN.fullmatch(token) else None


def load_routes(path: Path, project_root: Path) -> dict:
    path = _inside(path, project_root)
    try:
        fd = _private_fd(path, os.O_RDONLY, limit=1024 * 1024)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError("Invalid private route document")
    return document


def registered_client(routes: dict, authorization: str) -> str | None:
    if client_token(authorization) is None:
        return None
    digest = authorization_sha256(authorization)
    clients = routes.get("inference_clients", {})
    entry = clients.get(digest) if isinstance(clients, dict) else None
    if not isinstance(entry, dict) or not entry.get("run_id") or not entry.get("lease_id"):
        return None
    return digest


def _update_routes(project_root: Path, mutate) -> None:
    root = Path(project_root).resolve()
    path = _inside(root / "experiment/benchmarks/wildclaw_bench/runtime/proxy_routes.json", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _inside(path.with_suffix(".lock"), root)
    # Share the controller's lock. Its historical lock file may have mode 0664;
    # it contains no secret and is tightened before any access data are touched.
    lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise PermissionError("Route lock must be an owned, singly linked regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = load_routes(path, root)
        mutate(data)
        fd, temporary = tempfile.mkstemp(prefix=".inference-routes-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        os.close(lock_fd)


@dataclass(frozen=True)
class InferenceClient:
    api_key: str = field(repr=False)
    authorization_sha256: str
    run_id: str


@contextmanager
def inference_client(project_root: Path, run_id: str, *, token: str | None = None):
    """Register one run until context exit; token override is migration-only.

    Pass client.api_key as the solver/judge dummy provider API key, and persist
    client.authorization_sha256 as the run's gateway_authorization_hash.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", run_id):
        raise ValueError("Invalid inference run identifier")
    token = secrets.token_urlsafe(32) if token is None else token
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError("Invalid inference client token")
    ensure_service_key(project_root)
    client = InferenceClient(token, authorization_sha256("Bearer " + token), run_id)
    lease = secrets.token_hex(16)

    def register(routes):
        clients = routes.setdefault("inference_clients", {})
        if not isinstance(clients, dict) or client.authorization_sha256 in clients:
            raise ValueError("Inference client is already registered or registry is invalid")
        clients[client.authorization_sha256] = {"run_id": run_id, "lease_id": lease,
                                              "registered_at_unix": time.time()}

    def remove(routes):
        clients = routes.get("inference_clients", {})
        if isinstance(clients, dict) and clients.get(client.authorization_sha256, {}).get("lease_id") == lease:
            clients.pop(client.authorization_sha256)

    _update_routes(project_root, register)
    try:
        yield client
    finally:
        _update_routes(project_root, remove)
