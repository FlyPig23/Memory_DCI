#!/usr/bin/env python3
"""Extract public Warmup metadata and prepare minimal proxy-compatible skill copies."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[4]
EXP = ROOT / "experiment/benchmarks/wildclaw_bench"
VENDOR = EXP / "vendor/WildClawBench"

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def warmup_only(path):
    active = False
    lines = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            header = re.match(r"^##\s+(.+)$", line.strip())
            if header:
                if active:
                    break
                active = header.group(1) == "Warmup"
                continue
            if active:
                lines.append((number, line.rstrip("\n")))
    # Match official strip_codeblock behavior without retaining other sections.
    while lines and not lines[0][1].strip():
        lines.pop(0)
    while lines and not lines[-1][1].strip():
        lines.pop()
    fence = chr(96) * 3
    if lines and lines[0][1].strip().startswith(fence):
        lines.pop(0)
    if lines and lines[-1][1].strip() == fence:
        lines.pop()
    return lines

def install_kind(command):
    text = command.strip()
    patterns = {
        "apt": r"^(?:sudo\s+)?apt(?:-get)?\s+(?:update|install)\b",
        "pip": r"^(?:(?:~?/[^\s]+/)?pip3?|python3?\s+-m\s+pip)\s+install\b",
        "npm": r"^npm\s+(?:install|i)\b",
        "playwright": r"^(?:(?:python3?\s+-m\s+)?playwright)\s+install\b",
    }
    for kind, pattern in patterns.items():
        if re.search(pattern, text):
            return kind
    return None

def install_tokens(command, kind):
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    if "install" not in parts and "i" not in parts:
        return []
    index = parts.index("install") if "install" in parts else parts.index("i")
    result = []
    for token in parts[index+1:]:
        if token.startswith((">", "<")) or re.match(r"^[0-9]+[<>]",token):
            break
        if token.startswith("-"):
            continue
        result.append(token)
    return result

def extract_warmups():
    metadata = json.loads((EXP / "manifests/task_manifest.json").read_text())
    rows = []
    union = {kind: set() for kind in ("apt", "pip", "npm", "playwright")}
    all_install = set()
    for task in metadata["tasks"]:
        source = VENDOR / task["source_path"]
        lines = warmup_only(source)
        install = []
        state = []
        mixed = []
        for number, line in lines:
            if not line.strip() or line.strip().startswith("#"):
                state.append(line)
                continue
            commands = [c.strip() for c in line.split("&&")]
            kinds = [install_kind(c) for c in commands]
            if all(kinds):
                install.append({"source_line": number, "command": line,
                                "sha256": hashlib.sha256(line.encode()).hexdigest(),
                                "kinds": kinds})
                all_install.add(line)
                for command, kind in zip(commands, kinds):
                    union[kind].update(install_tokens(command, kind))
            else:
                state.append(line)
                if any(kinds):
                    mixed.append({"source_line":number,"command":line})
        warmup = "\n".join(line for _,line in lines).strip()
        rows.append({"task_id":task["task_id"], "source_path":task["source_path"],
                     "warmup":warmup,"warmup_sha256":hashlib.sha256(warmup.encode()).hexdigest(),
                     "installation_lines":install,"state_setup_preserve":"\n".join(state).strip(),
                     "mixed_lines_requiring_manual_review":mixed})
    doc = {"scope":"Public Warmup sections only; no Prompt/gt/Automated Checks retained or displayed.",
           "source_commit":"316334ccc4a87b9b5635ad73da99b4dfc0b3887e",
           "union":{kind:sorted(items) for kind,items in union.items()},
           "unique_installation_lines":sorted(all_install), "tasks":rows,
           "skip_policy":"These are candidates only. Skip a whole installation line only after image-level validation of all its requirements; retain state_setup_preserve. Review mixed lines manually."}
    (EXP/"manifests/warmup_dependencies.json").write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"task_count":len(rows),"union":doc["union"],
                      "mixed_line_tasks":[r["task_id"] for r in rows if r["mixed_lines_requiring_manual_review"]]},ensure_ascii=False))

def copy_skills():
    source = VENDOR / "skills"
    destination = EXP / "runtime/skills"
    before = {str(p.relative_to(source)):digest(p) for p in source.rglob("*") if p.is_file()}
    if not destination.exists():
        shutil.copytree(source,destination)
    patches = []
    replacements = {
        "4/ddgs-search/scripts/search.py": (
            'if k in ("PATH", "HOME", "LANG", "TERM")',
            'if k in ("PATH", "HOME", "LANG", "TERM", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "TMPDIR", "TEMP", "TMP", "DDGS_PROXY")'),
        "4/academic-literature-search/agent.py": (
            "aiohttp.ClientSession()",
            "aiohttp.ClientSession(trust_env=True)"),
    }
    for path,(old,new) in replacements.items():
        target = destination / path
        text = (source / path).read_text()
        if text.count(old) != 1:
            raise ValueError("unexpected patch match count")
        text = text.replace(old,new)
        if "ddgs" in path:
            text = text.replace("    try:\n        result = subprocess.run(",
                                '    proxy = os.getenv("DDGS_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")\n    try:\n        result = subprocess.run(')
            text = text.replace('["ddgs", "text", "-q", query, "-m", str(max_results), "-b", backend, "-o", tmp],',
                                '["ddgs", "text", "-q", query, "-m", str(max_results), "-b", backend, "-o", tmp] + (["--proxy", proxy] if proxy else []),')
        target.write_text(text)
        patches.append({"path":path,"source_sha256":before[path],"patched_sha256":digest(target),
                        "change":"Preserve controlled proxy/CA environment" if "ddgs" in path else "Enable aiohttp environment proxy support"})
    after = {str(p.relative_to(source)):digest(p) for p in source.rglob("*") if p.is_file()}
    if before != after:
        raise ValueError("vendor skills changed")
    doc = {"source_commit":"316334ccc4a87b9b5635ad73da99b4dfc0b3887e",
           "source_root":str(source.relative_to(ROOT)),"runtime_root":str(destination.relative_to(ROOT)),
           "original_files":before,"patches":patches,
           "recommended_env":{"DDGS_PROXY":"http://127.0.0.1:18080","AGENT_BROWSER_PROXY":"http://127.0.0.1:18080",
                              "AGENT_BROWSER_SOCKET_DIR":"/root/.cache/agent-browser",
                              "XDG_CONFIG_HOME":"/root/.cache/config",
                              "HTTP_PROXY":"http://127.0.0.1:18080","HTTPS_PROXY":"http://127.0.0.1:18080",
                              "http_proxy":"http://127.0.0.1:18080","https_proxy":"http://127.0.0.1:18080"},
           "note":"CA paths must point to the run's mounted CA file; do not disable TLS validation."}
    (EXP/"manifests/runtime_skill_patches.json").write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n")
    print("Prepared runtime skills:",len(before),"files;",len(patches),"minimal network patches")


def prepare_nss_tools():
    """Unpack a pinned public Ubuntu tool package; never install it globally."""
    directory = EXP / "runtime/nss-tools"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "libnss3-tools.deb"
    destination = directory / "unpacked"
    expected = "c3e51c8169da86e6eab22aeac20d95481480034208410686e8863eae0a6cf4df"
    url = "https://archive.ubuntu.com/ubuntu/pool/universe/n/nss/libnss3-tools_3.68.2-0ubuntu1_amd64.deb"
    size = 565460
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=directory) as temporary:
        temporary = Path(temporary)
        if not archive.exists():
            download = temporary / "download.deb"
            with urllib.request.urlopen(url, timeout=120) as response, download.open("wb") as output:
                total = 0
                while block := response.read(65536):
                    total += len(block)
                    if total > size:
                        raise ValueError("NSS archive exceeds pinned size")
                    output.write(block)
            if download.stat().st_size != size or digest(download) != expected:
                raise ValueError("NSS archive hash or size mismatch")
            download.replace(archive)
        if archive.stat().st_size != size or digest(archive) != expected:
            raise ValueError("Cached NSS archive hash or size mismatch")
        extracted = temporary / "unpacked"
        subprocess.run(["dpkg-deb", "-x", str(archive), str(extracted)], check=True)
        # Compare against the pinned package before reusing an existing mount.
        # Never rewrite tools being used by other project-owned runs.
        files = {}
        links = {}
        for path in sorted(extracted.rglob("*")):
            relative = path.relative_to(extracted)
            if path.is_symlink():
                path.resolve().relative_to(extracted.resolve())
                links[str(relative)] = str(path.readlink())
                existing = destination / relative
                if destination.exists() and (not existing.is_symlink() or str(existing.readlink()) != links[str(relative)]):
                    raise ValueError("Existing NSS link differs: " + str(relative))
                continue
            if path.is_file():
                files[str(relative)] = digest(path)
                if destination.exists():
                    existing = destination / relative
                    if existing.is_symlink() or not existing.is_file() or digest(existing) != files[str(relative)]:
                        raise ValueError("Existing NSS extraction differs: " + str(relative))
        if not destination.exists():
            extracted.replace(destination)
        doc = {"url": url, "version": "2:3.68.2-0ubuntu1", "sha256": expected,
               "bytes": size, "verification": "Pinned SHA256 from runtime-v2 Ubuntu apt metadata; extracted files rechecked against archive",
               "installed_globally": False, "files": files, "links": links}
        (directory / "source.json").write_text(json.dumps(doc, indent=2) + "\n")
    print(json.dumps({"nss_tools_ready": True, "files": len(files), "sha256": expected}))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-warmups",action="store_true")
    parser.add_argument("--copy-skills",action="store_true")
    parser.add_argument("--prepare-nss-tools",action="store_true")
    args=parser.parse_args()
    if args.extract_warmups: extract_warmups()
    if args.copy_skills: copy_skills()
    if args.prepare_nss_tools: prepare_nss_tools()
if __name__=="__main__": main()
