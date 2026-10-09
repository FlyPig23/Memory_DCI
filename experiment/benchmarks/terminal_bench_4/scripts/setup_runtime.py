#!/usr/bin/env python3
"""Install the pinned Linux x86_64 harness locally; never starts Docker or a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PYTHON_DIR = "cpython-3.12.13-linux-x86_64-gnu"
ASSETS = {
    "codex": (
        "https://github.com/openai/codex/releases/download/rust-v0.153.4/codex-x86_64-unknown-linux-musl.tar.gz",
        "f479424eca092484dc40d87ae28c44f4cc40234a60045d6131e493800d814a30",
    ),
    "codex-code-mode-host": (
        "https://github.com/openai/codex/releases/download/rust-v0.153.4/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz",
        "f95830a869590957664bbfc67bccb08773806b693670baf15908176f89b4cd31",
    ),
    "rg": (
        "https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz",
        "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
    ),
    "python": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20260325/cpython-3.12.13%2B20260325-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "86232383051a0950a74a9486949c87f88da45978bafa8d826c2f268629a3f508",
    ),
}


def sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def download(name: str) -> Path:
    url, digest = ASSETS[name]
    cache = BASE / ".downloads"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{name}-{digest[:12]}.tar.gz"
    if dest.is_file() and sha256(dest) == digest:
        return dest
    print(f"Downloading pinned {name} ...", flush=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-", dir=cache)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as source:
            shutil.copyfileobj(source, out)
        if sha256(Path(temporary)) != digest:
            raise ValueError(f"{name}: archive SHA256 mismatch")
        os.replace(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return dest


def extract_binary(name: str, archive: Path) -> None:
    target = BASE / "runtime/bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        matches = [m for m in tf.getmembers() if m.isfile() and
                   (Path(m.name).name == name or Path(m.name).name == f"{name}-x86_64-unknown-linux-musl")]
        if len(matches) != 1:
            raise ValueError(f"Expected one executable for {name}; got {[m.name for m in matches]}")
        with tf.extractfile(matches[0]) as source, target.open("wb") as out:
            shutil.copyfileobj(source, out)
    target.chmod(0o755)


def command(*args: str | Path) -> str:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    return subprocess.check_output([str(a) for a in args], env=env, text=True).strip()


def install_python(archive: Path) -> Path:
    parent = BASE / "runtime/python"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / PYTHON_DIR
    if root.exists():
        if command(root / "bin/python3", "--version") != "Python 3.12.13":
            raise ValueError(f"Unexpected existing Python at {root}")
        return root
    with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as temporary:
        with tarfile.open(archive, "r:gz") as tf:
            # Python 3.11.8+ provides the data filter, including safe internal symlinks.
            tf.extractall(temporary, filter="data")
        candidate = Path(temporary) / "python"
        if not (candidate / "bin/python3").is_file():
            raise ValueError("Unexpected standalone Python archive layout")
        candidate.rename(root)
    return root


def write_transport(extra_ca: Path | None) -> None:
    transport = BASE / "prepared/transport"
    transport.mkdir(parents=True, exist_ok=True)
    cert = transport / "ca-certificates.crt"
    paths = [Path(p) for p in (ssl.get_default_verify_paths().cafile,
                              "/etc/ssl/certs/ca-certificates.crt") if p and Path(p).is_file()]
    if not paths:
        raise RuntimeError("No system CA bundle found; install ca-certificates first")
    contents = paths[0].read_bytes()
    if extra_ca:
        contents += b"\n" + extra_ca.expanduser().resolve().read_bytes()
    ssl.create_default_context(cadata=contents.decode("ascii"))
    cert.write_bytes(contents)
    # JSON is valid YAML and quotes absolute paths safely (spaces, colons, etc.).
    compose = {"services": {"main": {
        "volumes": [{"type": "bind", "source": str(cert),
                     "target": "/opt/v5-ca/ca-certificates.crt", "read_only": True}],
        "environment": {"CODEX_CA_CERTIFICATE": "/opt/v5-ca/ca-certificates.crt",
                        "SSL_CERT_FILE": "/opt/v5-ca/ca-certificates.crt",
                        "REQUESTS_CA_BUNDLE": "/opt/v5-ca/ca-certificates.crt",
                        "CURL_CA_BUNDLE": "/opt/v5-ca/ca-certificates.crt"},
    }}}
    (transport / "docker-compose-ca.yaml").write_text(json.dumps(compose, indent=2) + "\n")


def support_manifest(root: Path) -> dict:
    files, symlinks = [], []
    for directory in [BASE / "runtime/bin", root]:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(BASE).as_posix()
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                symlinks.append({"path": relative, "target": os.readlink(path)})
            elif path.is_file():
                files.append({"path": relative, "sha256": sha256(path), "size": path.stat().st_size})
    return {"schema_version": 1, "python_root": root.relative_to(BASE).as_posix(),
            "versions": {"python": "3.12.13", "codex": "0.153.4", "harbor": "0.23.0", "ripgrep": "15.2.0"},
            "sources": {k: {"url": v[0], "sha256": v[1]} for k, v in ASSETS.items()},
            "files": files, "symlinks": symlinks}


def check() -> None:
    manifest_path = BASE / "runtime/support_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        path = BASE / item["path"]
        if path.is_symlink() or not path.is_file() or sha256(path) != item["sha256"]:
            raise ValueError(f"Runtime checksum mismatch: {item['path']}")
    for item in manifest["symlinks"]:
        path = BASE / item["path"]
        if not path.is_symlink() or os.readlink(path) != item["target"]:
            raise ValueError(f"Runtime symlink mismatch: {item['path']}")
    if command(BASE / "runtime/bin/codex", "--version") != "codex-cli 0.153.4":
        raise ValueError("Unexpected Codex CLI version")
    if command(BASE / "runtime/bin/rg", "--version").splitlines()[0].split()[:2] != ["ripgrep", "15.2.0"]:
        raise ValueError("Unexpected ripgrep version")
    python = BASE / ".venv/bin/python"
    if command(python, "-c", "import importlib.metadata; print(importlib.metadata.version('harbor'))") != "0.23.0":
        raise ValueError("Unexpected Harbor version")
    print("Pinned runtime verified. No Docker containers or model calls were started.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Verify existing installation without downloading")
    parser.add_argument("--ca-file", type=Path, help="Append a local custom CA certificate for container HTTPS")
    args = parser.parse_args()
    if sys.version_info < (3, 11, 8):
        parser.error("Python 3.11.8+ is required for secure archive extraction")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("The frozen harness supports Linux x86_64 only")
    if args.check_only:
        check()
        return
    for name in ("codex", "codex-code-mode-host", "rg"):
        extract_binary(name, download(name))
    root = install_python(download("python"))
    python = BASE / ".venv/bin/python"
    if not python.exists():
        command(root / "bin/python3", "-m", "venv", BASE / ".venv")
    print("Installing locked Python dependencies ...", flush=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(BASE / "requirements.lock.txt")],
                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True)
    write_transport(args.ca_file)
    (BASE / "runtime/support_manifest.json").write_text(json.dumps(support_manifest(root), indent=2) + "\n")
    check()


if __name__ == "__main__":
    main()
