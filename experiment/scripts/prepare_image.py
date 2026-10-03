#!/usr/bin/env python3
"""Load the verified benchmark image under a task-owned, non-conflicting tag."""
from pathlib import Path
import io
import json
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiment"
TAG = "hangxiao-skill-dci/wildclaw-codex:source-75f94557"


def main():
    verified = json.loads((EXP / "manifests/download_image.json").read_text())
    if verified["status"] != "complete":
        raise SystemExit("Image download is not verified")
    source = EXP / "runtime/images/Images/wildclawbench-codex-ubuntu_v0.0.tar"
    target = EXP / "runtime/images/hangxiao-skill-dci-codex.tar"
    with tarfile.open(source, "r:") as archive:
        config = json.load(archive.extractfile("manifest.json"))[0]["Config"]
    config_id = "sha256:" + Path(config).name.removesuffix(".json")
    existing_content = subprocess.run(["docker", "image", "inspect", config_id], capture_output=True)
    if existing_content.returncode == 0:
        subprocess.run(["docker", "tag", config_id, TAG], check=True)
    owned_archive_ok = False
    if target.is_file():
        with tarfile.open(target, "r:") as archive:
            try:
                index = json.load(archive.extractfile("index.json"))
                owned_archive_ok = all(TAG in m.get("annotations", {}).get("io.containerd.image.name", "") for m in index["manifests"])
            except KeyError:
                owned_archive_ok = True
    if existing_content.returncode != 0 and not owned_archive_ok:
        temporary = target.with_suffix(".incomplete")
        with tarfile.open(source, "r:") as src, tarfile.open(temporary, "w:") as dst:
            for member in src:
                stream = src.extractfile(member) if member.isfile() else None
                if member.name == "manifest.json":
                    manifest = json.load(stream)
                    if len(manifest) != 1:
                        raise ValueError("Expected a single source image")
                    manifest[0]["RepoTags"] = [TAG]
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                    stream = io.BytesIO(data)
                elif member.name == "index.json":
                    index = json.load(stream)
                    for image in index["manifests"]:
                        image.setdefault("annotations", {})["io.containerd.image.name"] = "docker.io/" + TAG
                        image["annotations"]["org.opencontainers.image.ref.name"] = TAG.split(":", 1)[1]
                    data = json.dumps(index).encode()
                    member.size = len(data)
                    stream = io.BytesIO(data)
                elif member.name == "repositories":
                    continue
                dst.addfile(member, stream)
        temporary.replace(target)
    present = subprocess.run(["docker", "image", "inspect", TAG], capture_output=True)
    if present.returncode != 0:
        subprocess.run(["docker", "load", "--input", str(target)], check=True)
    inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", TAG], text=True))[0]
    state = {"tag": TAG, "id": inspect["Id"], "source_revision": verified["revision"],
             "source_sha256": verified["files"][0]["sha256"], "size": inspect["Size"],
             "layers": inspect["RootFS"]["Layers"], "config": inspect["Config"]}
    (EXP / "manifests/runtime_image.json").write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps({"tag": TAG, "id": inspect["Id"], "size": inspect["Size"]}), flush=True)


if __name__ == "__main__":
    main()
