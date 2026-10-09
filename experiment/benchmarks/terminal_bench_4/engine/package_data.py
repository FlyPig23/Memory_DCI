"""Original corpus credential filters retained for byte-equivalent V8 rendering."""
from __future__ import annotations
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
PATTERNS = {
    "private_key": (b"PRIVATE KEY", re.compile(
        rb"-----BEGIN ([A-Z ]*PRIVATE KEY)-----[ \t]*(?:(?:\r?\n|\\n)[ \t]*[A-Za-z0-9+/=]{16,})+(?:\r?\n|\\n)[ \t]*-----END \1-----")),
    "huggingface_token": (b"hf_", re.compile(rb"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{20,}")),
    "openai_key": (b"sk-", re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}")),
    "github_token": (b"gh", re.compile(rb"(?<![A-Za-z0-9])(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}")),
    "aws_access_key": (b"AKIA", re.compile(rb"AKIA[0-9A-Z]{16}")),
    "aws_session_key": (b"ASIA", re.compile(rb"ASIA[0-9A-Z]{16}")),
    "google_api_key": (b"AIza", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    "google_oauth_token": (b"ya29.", re.compile(rb"ya29\.[0-9A-Za-z_-]{30,}")),
    "azure_sas": (b".core.windows.net", re.compile(rb'https?://[^\s<>"\\]+\.core\.windows\.net/[^\s<>"\\]*[?&]sig=[^&\s<>"\\]+')),
    "slack_token": (b"xox", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    "gitlab_token": (b"glpat-", re.compile(rb"glpat-[A-Za-z0-9_-]{20,}")),
    "stripe_key": (b"sk_live_", re.compile(rb"sk_live_[A-Za-z0-9]{20,}")),
    "npm_token": (b"npm_", re.compile(rb"npm_[A-Za-z0-9]{36}")),
    "jwt": (b"eyJ", re.compile(rb"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    "bearer_token": (None, re.compile(rb"(?i)\bBearer [A-Za-z0-9._~+/-]{20,}")),
    "credential_assignment": (None, re.compile(
        rb"(?i)(?P<prefix>(?:OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|HF_TOKEN|access_token|refresh_token|client_secret|service[-_]key)[\\\"'\t ]*[:=][\\\"'\t ]*)(?P<secret>[A-Za-z0-9_+/=.-]{24,})")),
}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def local_credentials(root):
    values = set()

    def visit(value, name=""):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, key)
        elif isinstance(value, str) and len(value) >= 16 and any(
                word in name.lower() for word in ("token", "secret", "password", "key")):
            values.add(value.encode())

    auth = root / "experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json"
    if auth.is_file():
        visit(json.loads(auth.read_bytes()))
    key = root / "experiment/benchmarks/wildclaw_bench/runtime/inference-private/service-key"
    if key.is_file() and len(key.read_bytes().strip()) >= 16:
        values.add(key.read_bytes().strip())
    return values

def benchmark_fixtures(base, expected):
    relative = "prepared/tasks/sanitize-git-repo/tests/test_outputs.py"
    data = (base / relative).read_bytes()
    if sha(data) != expected[relative]:
        raise ValueError("Public benchmark fixture source differs from official file hash")
    values, records = set(), []
    for statement in ast.parse(data).body:
        if (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id.startswith("FAKE_"):
                    value = statement.value.value.encode()
                    values.update((value, value.lower()))
                    records.append({"name": target.id, "value_sha256": sha(value),
                                    "source": relative, "source_sha256": sha(data)})
    if len(records) != 5:
        raise ValueError("Unexpected public benchmark fake-credential constants")
    return values, records

def sanitize(data, known, fixtures=()):
    counts = Counter()
    for value in known:
        if value in data:
            counts["known_local_credential"] += data.count(value)
            data = data.replace(value, b"[REDACTED_KNOWN_LOCAL_CREDENTIAL]")
    lower = None
    for name, (marker, pattern) in PATTERNS.items():
        if marker is not None and marker not in data:
            continue
        if marker is None:
            lower = data.lower() if lower is None else lower
            if name == "bearer_token" and b"bearer " not in lower:
                continue
            if name == "credential_assignment" and not any(
                    p in lower for p in (b"api_key", b"hf_token", b"access_token", b"refresh_token", b"client_secret", b"service_key", b"service-key")):
                continue
        replacement = b"[REDACTED_" + name.upper().encode() + b"]"
        n = 0
        def replace(match):
            nonlocal n
            value = match.group("secret") if name == "credential_assignment" else match.group()
            if value in fixtures:
                return match.group()
            n += 1
            return match.group("prefix") + replacement if name == "credential_assignment" else replacement
        data = pattern.sub(replace, data)
        if n:
            counts[name] += n
            lower = None
    return data, counts
