"""Verified downloads and safe archive restoration shared by both benchmarks."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import urllib.request

def sha256(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _contained(path, root):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes destination: {path}")
    current = root.absolute()
    for part in path.absolute().relative_to(root.absolute()).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Unexpected link in staging path: {current}")
    return path


def download(url, destination, expected=None):
    if destination.is_file() and expected and sha256(destination) == expected:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".part", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=120) as response:
            shutil.copyfileobj(response, output, 1024 * 1024)
        if expected and sha256(temporary) != expected:
            raise ValueError(f"Download checksum mismatch: {destination.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore(archive, root, records):
    expected = {row["path"]: row for row in records}
    if len(expected) != len(records):
        raise ValueError("Duplicate manifest paths")
    observed = set()
    with tarfile.open(archive, "r:gz") as handle:
        # This dataset only uses regular files: never restore archive links.
        for member in handle:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or member.name not in expected or member.name in observed):
                raise ValueError(f"Unexpected archive member: {member.name}")
            observed.add(member.name)
            row = expected[member.name]
            target = root.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
                raise ValueError(f"Unsafe destination: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if (not target.is_file() or target.stat().st_size != row["bytes"]
                        or sha256(target) != row["sha256"]):
                    raise ValueError(f"Existing file differs; use a fresh checkout/destination: {member.name}")
                continue
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore-part", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as output, handle.extractfile(member) as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if temporary.stat().st_size != row["bytes"] or sha256(temporary) != row["sha256"]:
                    raise ValueError(f"Restored file checksum mismatch: {member.name}")
                temporary.chmod(0o755 if member.mode & 0o111 else 0o644)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    if observed != set(expected):
        raise ValueError("Archive is missing manifest files")
