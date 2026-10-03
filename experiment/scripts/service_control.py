#!/usr/bin/env python3
"""Control only recorded project-owned proxy, bridge and inference processes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiment.src.inference_access import ensure_service_key, service_key_path

SCHEMA = "skill-dci-services-v1"
ORDER = ("proxy", "bridge", "gateway")


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]
    port: int | None = None
    unix_socket: Path | None = None


def default_specs(root: Path, *, proxy_port=18081, gateway_port=18083,
                  image="hangxiao-skill-dci/wildclaw-codex:runtime-v2",
                  docker_host: str | None = None) -> dict[str, ServiceSpec]:
    root = root.resolve()
    python = str(root / "experiment/.venv/bin/python")
    runtime = root / "experiment/runtime"
    socket_path = runtime / "gateways/shared/egress.sock"
    proxy = (python, "-B", str(root / "experiment/.venv/bin/mitmdump"),
             "--listen-host", "127.0.0.1", "--listen-port", str(proxy_port),
             "--set", f"confdir={runtime / 'mitmproxy'}", "--set", "block_global=false",
             "--set", "ssl_insecure=false", "--ignore-hosts",
             "^(chatgpt[.]com|auth[.]openai[.]com|ab[.]chatgpt[.]com|chat[.]openai[.]com):443$",
             "--quiet", "-s", str(root / "experiment/src/egress_policy.py"))
    bridge = (python, "-B", str(root / "experiment/src/network_bridge.py"), "server",
              "--socket", str(socket_path), "--proxy-port", str(proxy_port))
    gateway = (python, "-B", "-m", "experiment.src.codex_inference_gateway",
               "--project-root", str(root), "--image", image,
               "--auth-file", str(runtime / "codex_auth/auth.json"),
               "--ca-bundle", str(runtime / "network-ca.pem"),
               "--remote-image-proxy", f"http://127.0.0.1:{proxy_port}",
               "--port", str(gateway_port), "--model", "gpt-5.6-sol",
               "--reasoning-effort", "medium", "--timeout", "600",
               "--api-key-file", str(service_key_path(root)))
    if docker_host:
        gateway += ("--docker-host", docker_host)
    return {"proxy": ServiceSpec("proxy", proxy, port=proxy_port),
            "bridge": ServiceSpec("bridge", bridge, unix_socket=socket_path),
            "gateway": ServiceSpec("gateway", gateway, port=gateway_port)}


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid: int) -> dict | None:
    """Read metadata only; no process environment or credentials are inspected."""
    try:
        base = Path("/proc") / str(pid)
        uid_line = next(line for line in (base / "status").read_text().splitlines() if line.startswith("Uid:"))
        uids = [int(part) for part in uid_line.split()[1:]]
        # Refuse to inspect argv/cwd of a process belonging to another user.
        if any(uid != os.geteuid() for uid in uids):
            return {"pid": pid, "uid": uids[0], "foreign_uid": True}
        fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "uid": uids[0], "start_ticks": int(fields[19]),
                "process_state": fields[0],
                "argv": [part.decode(errors="surrogateescape") for part in (base / "cmdline").read_bytes().split(b"\0") if part],
                "cwd": os.readlink(base / "cwd") if fields[0] != "Z" else None}
    except (FileNotFoundError, ProcessLookupError):
        return None


def identity_matches(record: dict, current: dict | None) -> bool:
    return bool(current and not current.get("foreign_uid") and current.get("process_state") != "Z" and all(
        current.get(key) == record.get(key) for key in ("pid", "uid", "start_ticks", "argv", "cwd")))


def socket_file_identity(path: Path) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
            "is_socket": stat.S_ISSOCK(info.st_mode)}


def endpoint_available(spec: ServiceSpec) -> bool:
    if spec.unix_socket is not None:
        return not os.path.lexists(spec.unix_socket)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", int(spec.port)))
            return True
        except OSError:
            return False


def owns_listener(pid: int, spec: ServiceSpec) -> bool:
    """Verify the listener's kernel socket inode belongs to this exact process."""
    inodes = set()
    try:
        for fd in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                value = os.readlink(fd)
            except FileNotFoundError:
                continue
            if value.startswith("socket:[") and value.endswith("]"):
                inodes.add(value[8:-1])
        if spec.unix_socket is not None:
            for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
                parts = line.split(maxsplit=7)
                if len(parts) == 8 and parts[7] == str(spec.unix_socket) and parts[6] in inodes:
                    return True
        else:
            target = f"0100007F:{int(spec.port):04X}"
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
                parts = line.split()
                if parts[1] == target and parts[3] == "0A" and parts[9] in inodes:
                    return True
    except (FileNotFoundError, ProcessLookupError):
        return False
    return False


def signal_recorded(record: dict, sig: int) -> None:
    """pidfd prevents a PID-reuse race between identity verification and signal."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe stop requires Linux pidfd support")
    fd = os.pidfd_open(record["pid"])
    try:
        if not identity_matches(record, process_identity(record["pid"])):
            raise RuntimeError("Process identity changed; no signal was sent")
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


class ServiceController:
    def __init__(self, root: Path, specs: dict[str, ServiceSpec], *, state_path: Path | None = None):
        self.root = root.resolve()
        self.specs = specs
        self.directory = self.root / "experiment/runtime/services"
        self.state_path = Path(state_path or self.directory / "state.json").absolute()
        if ".." in self.state_path.parts or not self.state_path.is_relative_to(self.directory):
            raise ValueError("Service registry must be under the project's services directory")
        if set(specs) - set(ORDER) or any(spec.name != name for name, spec in specs.items()):
            raise ValueError("Only the three named project services are supported")
        for spec in specs.values():
            if not spec.argv or not Path(spec.argv[0]).is_absolute():
                raise ValueError("Service argv must use an absolute interpreter path")
            if (spec.port is None) == (spec.unix_socket is None):
                raise ValueError("Each service must declare exactly one listener")
            if spec.port is not None and not 1024 <= spec.port <= 65535:
                raise ValueError("Service port must be unprivileged")
            if spec.unix_socket is not None and (".." in spec.unix_socket.parts or not spec.unix_socket.is_absolute() or not spec.unix_socket.is_relative_to(self.root / "experiment/runtime/gateways")):
                raise ValueError("Bridge socket must remain in the project gateway directory")

    def _private_directory(self, path: Path) -> None:
        current = self.root
        for part in path.relative_to(self.root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Service state/log directories must not contain symlinks")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.stat().st_uid != os.geteuid():
            raise PermissionError("Service directory belongs to another user")
        os.chmod(path, 0o700)

    def _load(self) -> dict:
        current = self.root
        for part in self.state_path.relative_to(self.root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Service registry path must not contain symlinks")
        if not self.state_path.exists():
            return {"schema": SCHEMA, "project_root": str(self.root), "owner_uid": os.geteuid(),
                    "boot_id": boot_id(), "services": {}}
        fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise PermissionError("Service registry must be an owned, singly linked mode-0600 file")
            data = json.load(stream)
        if data.get("schema") != SCHEMA or data.get("project_root") != str(self.root) or data.get("owner_uid") != os.geteuid():
            raise ValueError("Service registry identity does not match this project/user")
        if not isinstance(data.get("services"), dict) or set(data["services"]) - set(ORDER):
            raise ValueError("Unknown service in registry")
        for record in data["services"].values():
            if record.get("uid") != os.geteuid() or record.get("cwd") != str(self.root):
                raise ValueError("Recorded service user/cwd is outside this project")
            if not isinstance(record.get("pid"), int) or record["pid"] <= 0 or not isinstance(record.get("start_ticks"), int):
                raise ValueError("Recorded process identity is incomplete")
            if not isinstance(record.get("argv"), list) or not record["argv"] or not all(isinstance(arg, str) for arg in record["argv"]):
                raise ValueError("Recorded argv is incomplete")
            if record.get("unix_socket"):
                endpoint = Path(record["unix_socket"])
                if ".." in endpoint.parts or not endpoint.is_absolute() or not endpoint.is_relative_to(self.root / "experiment/runtime/gateways"):
                    raise ValueError("Recorded socket is outside this project")
            elif not isinstance(record.get("port"), int) or not 1024 <= record["port"] <= 65535:
                raise ValueError("Recorded port is invalid")
        return data

    def _save(self, state: dict) -> None:
        temporary = self.state_path.with_name(".state-" + uuid.uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, self.state_path)

    @contextmanager
    def _locked(self):
        self._private_directory(self.directory)
        fd = os.open(self.directory / "control.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            info = os.fstat(lock.fileno())
            if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise PermissionError("Service lock must be an owned singly linked mode-0600 file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def status(self, name: str) -> dict:
        state = self._load()
        record = state["services"].get(name)
        if record is None:
            return {"service": name, "status": "unmanaged", "action": "No existing process is adopted"}
        if record.get("boot_id", state["boot_id"]) != boot_id():
            return {"service": name, "status": "stale_boot", "pid": record["pid"]}
        current = process_identity(record["pid"])
        if current is None or (current.get("process_state") == "Z" and current.get("start_ticks") == record["start_ticks"]):
            return {"service": name, "status": "stopped", "pid": record["pid"]}
        if not identity_matches(record, current):
            return {"service": name, "status": "identity_mismatch", "pid": record["pid"]}
        spec = ServiceSpec(name, tuple(record["argv"]), port=record.get("port"),
                           unix_socket=Path(record["unix_socket"]) if record.get("unix_socket") else None)
        return {"service": name, "status": "running" if owns_listener(record["pid"], spec) else "running_not_ready",
                "pid": record["pid"], "start_ticks": record["start_ticks"],
                "configuration_matches": list(self.specs[name].argv) == record["argv"]}

    def start(self, name: str, *, timeout: float = 30) -> dict:
        with self._locked():
            state = self._load()
            previous = state["services"].get(name)
            if previous is not None:
                status = self.status(name)
                if status["status"] == "running" and status.get("configuration_matches"):
                    return status
                if status["status"] not in ("stopped", "stale_boot"):
                    raise RuntimeError("Recorded service must be stopped or resolved explicitly before restart")
            for dependency in ORDER[:ORDER.index(name)]:
                dependency_status = self.status(dependency)
                if dependency_status["status"] != "running" or not dependency_status.get("configuration_matches"):
                    raise RuntimeError("Required dependency is not a recorded ready service with the requested configuration: " + dependency)
            spec = self.specs[name]
            if not endpoint_available(spec):
                raise RuntimeError("Listener is occupied or an existing socket is unowned; no process or socket was changed")
            if name == "gateway":
                ensure_service_key(self.root)
            for directory in (self.directory / "logs", self.directory / "tmp", self.directory / "cache"):
                self._private_directory(directory)
            if spec.unix_socket is not None:
                self._private_directory(spec.unix_socket.parent)
            launch_id = name + "-" + uuid.uuid4().hex
            log_paths = [self.directory / "logs" / (launch_id + suffix) for suffix in (".stdout.log", ".stderr.log")]
            handles = [os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") for path in log_paths]
            environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
                           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(self.directory / "tmp"),
                           "XDG_CACHE_HOME": str(self.directory / "cache")}
            try:
                process = subprocess.Popen(list(spec.argv), cwd=self.root, env=environment,
                                           stdin=subprocess.DEVNULL, stdout=handles[0], stderr=handles[1],
                                           start_new_session=True, close_fds=True)
            finally:
                for handle in handles:
                    handle.close()
            current = process_identity(process.pid)
            # Popen returns after exec; a mismatch is never adopted or signalled.
            if not current or current.get("argv") != list(spec.argv) or current.get("uid") != os.geteuid() or current.get("cwd") != str(self.root):
                raise RuntimeError("New process identity could not be verified; inspect private launch logs")
            record = {key: current[key] for key in ("pid", "uid", "start_ticks", "argv", "cwd")}
            record.update(boot_id=boot_id(), port=spec.port,
                          unix_socket=str(spec.unix_socket) if spec.unix_socket else None,
                          launched_at_unix=time.time(), launch_id=launch_id, status="starting",
                          stdout=str(log_paths[0].relative_to(self.root)), stderr=str(log_paths[1].relative_to(self.root)))
            if previous is not None:
                state.setdefault("history", []).append({"service": name, **previous})
            state["services"][name] = record
            self._save(state)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not identity_matches(record, process_identity(process.pid)):
                    record["status"] = "exited_or_identity_changed"
                    self._save(state)
                    raise RuntimeError("Service exited or changed identity before readiness; private logs retained")
                if owns_listener(process.pid, spec):
                    record["status"] = "running"
                    if spec.unix_socket is not None:
                        record["socket_file_identity"] = socket_file_identity(spec.unix_socket)
                    self._save(state)
                    return self.status(name)
                time.sleep(0.05)
            record["status"] = "startup_timeout"
            self._save(state)
            # Leave this exact recorded process inspectable/stoppable. Never
            # escalate to a broad process-name kill when readiness fails.
            raise RuntimeError("Service startup timed out; recorded process can be inspected or stopped explicitly")

    def _remove_owned_stopped_socket(self, record: dict) -> None:
        if not record.get("unix_socket") or not record.get("socket_file_identity"):
            return
        path = Path(record["unix_socket"])
        current = socket_file_identity(path)
        if current is None:
            return
        if current != record["socket_file_identity"] or current["uid"] != os.geteuid() or not current["is_socket"]:
            raise RuntimeError("Bridge socket identity changed; it was not removed")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(str(path)) == 0:
                raise RuntimeError("A live process still owns the socket; it was not removed")
        path.unlink()

    def stop(self, name: str, *, timeout: float = 15, force: bool = False) -> dict:
        with self._locked():
            state = self._load()
            record = state["services"].get(name)
            if record is None:
                raise RuntimeError("Service has no recorded owner identity; no process was signalled")
            status = self.status(name)
            if status["status"] in ("identity_mismatch", "stale_boot"):
                raise RuntimeError("Recorded process identity is stale or differs; no process was signalled")
            if status["status"] != "stopped":
                signal_recorded(record, signal.SIGTERM)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    current = process_identity(record["pid"])
                    if current is None or (current.get("process_state") == "Z" and current.get("start_ticks") == record["start_ticks"]):
                        break
                    if not identity_matches(record, current):
                        raise RuntimeError("Process identity changed during stop; no further signal was sent")
                    time.sleep(0.05)
                else:
                    if not force:
                        raise RuntimeError("Recorded service did not exit; use explicit --force only after inspection")
                    signal_recorded(record, signal.SIGKILL)
                    deadline = time.monotonic() + 5
                    while identity_matches(record, process_identity(record["pid"])) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if identity_matches(record, process_identity(record["pid"])):
                        raise RuntimeError("Recorded service remains alive after targeted SIGKILL")
            self._remove_owned_stopped_socket(record)
            record.update(status="stopped", stopped_at_unix=time.time())
            self._save(state)
            return {"service": name, "status": "stopped", "pid": record["pid"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "start", "status", "stop"))
    parser.add_argument("--service", choices=(*ORDER, "all"), default="all")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--proxy-port", type=int, default=18081)
    parser.add_argument("--gateway-port", type=int, default=18083)
    parser.add_argument("--image", default="hangxiao-skill-dci/wildclaw-codex:runtime-v2")
    parser.add_argument("--docker-host")
    parser.add_argument("--force", action="store_true", help="Allow targeted SIGKILL after a recorded process ignores SIGTERM")
    args = parser.parse_args()
    specs = default_specs(args.project_root, proxy_port=args.proxy_port, gateway_port=args.gateway_port,
                          image=args.image, docker_host=args.docker_host)
    controller = ServiceController(args.project_root, specs)
    names = ORDER if args.service == "all" else (args.service,)
    if args.action == "stop":
        names = tuple(reversed(names))
    for name in names:
        if args.action == "plan":
            result = {"service": name, "argv": list(specs[name].argv), "cwd": str(controller.root),
                      "state_path": str(controller.state_path), "starts_process": False}
        elif args.action == "status":
            result = controller.status(name)
        elif args.action == "start":
            result = controller.start(name)
        else:
            result = controller.stop(name, force=args.force)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
