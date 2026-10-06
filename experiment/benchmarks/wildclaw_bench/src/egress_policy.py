"""mitmproxy addon: public web access without benchmark artifacts or host access.

No credentials, request bodies, or response bodies are logged. Native Codex
authentication tunnels are explicitly passed through without TLS inspection.
"""
from __future__ import annotations
import ipaddress
import base64
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import unquote, urlsplit, urlunsplit, parse_qsl, urlencode
from mitmproxy import http
import httpx

ROOT = Path(__file__).resolve().parents[4]
try:
    from .inference_access import (CLIENT_AUTHORIZATION_HEADER, load_routes,
                                   read_service_key, registered_client, service_key_path)
except ImportError:
    # mitmproxy loads addons as stand-alone scripts, outside this package.
    import sys
    sys.path.insert(0, str(ROOT))
    from experiment.benchmarks.wildclaw_bench.src.inference_access import (CLIENT_AUTHORIZATION_HEADER, load_routes,
                                                 read_service_key, registered_client, service_key_path)
ROUTES = ROOT / "experiment/benchmarks/wildclaw_bench/runtime/proxy_routes.json"
AUDIT = ROOT / "experiment/benchmarks/wildclaw_bench/logs/egress.jsonl"
BLOCKED = re.compile(r"wild[-_]?claw[-_]?bench", re.I)
PRIVATE_HOSTS = {"localhost", "host.docker.internal", "gateway.docker.internal", "metadata.google.internal"}
IDENTITY_HOSTS = {"chatgpt.com", "auth.openai.com", "ab.chatgpt.com", "chat.openai.com"}
ADDRESS_CACHE = {}


def contains_real_credential(flow):
    """Block literal/URL/base64 export of the actual experiment login.

    Benchmark-provided dummy credentials are unaffected. Values and request
    bodies never enter the audit log.
    """
    auth = ROOT / "experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json"
    if not auth.is_file() or auth.stat().st_size > 131072:
        return False
    try:
        document = json.loads(auth.read_text())
    except (OSError, ValueError):
        return False
    values = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and len(value) >= 20 and re.search(r"token|api_key", key, re.I):
                    values.append(value.encode())
                elif isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(document)
    request = flow.request
    haystack = unquote(request.pretty_url).encode() + b"\n" + bytes(request.headers) + b"\n" + (request.raw_content or b"")
    return any(value in haystack or base64.b64encode(value) in haystack for value in values)


def audit(kind, url, reason="", status=None):
    parts = urlsplit(url)
    clean_query = [(k, "REDACTED" if re.search(r"token|key|secret|password", k, re.I) else v)
                   for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    safe = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(clean_query), ""))
    with AUDIT.open("a") as stream:
        stream.write(json.dumps({"time": time.time(), "kind": kind, "url": safe[:4096], "reason": reason, "status": status}) + "\n")


def public_host(host):
    host = host.lower().rstrip(".")
    if host in PRIVATE_HOSTS or "." not in host or host.endswith((".local", ".internal")):
        return False
    now = time.monotonic()
    cached = ADDRESS_CACHE.get(host)
    if cached and now - cached[0] < 30:
        return cached[1]
    try:
        addresses = {x[4][0] for x in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        result = bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
    except (OSError, ValueError):
        result = False
    ADDRESS_CACHE[host] = (now, result)
    return result


def denied(flow, reason, *, audit_url=None):
    flow.response = http.Response.make(403, b"Request denied by the experiment's fixed network policy.\n", {"Content-Type": "text/plain"})
    audit("blocked", audit_url or flow.request.pretty_url, reason, 403)


class Policy:
    def http_connect(self, flow):
        host = flow.request.host.lower()
        if host not in IDENTITY_HOSTS and not public_host(host):
            denied(flow, "non-public CONNECT target")
        elif flow.request.port != 443:
            denied(flow, "CONNECT port is not HTTPS")

    async def request(self, flow):
        host, path = flow.request.host.lower(), flow.request.path
        if host not in IDENTITY_HOSTS and contains_real_credential(flow):
            denied(flow, "actual experiment credential in external request",
                   audit_url="https://credential-export-blocked.invalid/")
            return
        # These synthetic endpoints terminate at controller-created services.
        # The solver cannot supply a destination port or path outside /rpc /v1.
        route_key = None
        if host == "dci.hangxiao.internal":
            route_key = "dci"
        elif host == "api.hangxiao.internal":
            route_key = "inference"
        elif host == "openrouter.ai" and path.startswith("/api/v1/"):
            route_key = "inference"
            path = "/v1/" + path[len("/api/v1/"):]
        if route_key:
            try:
                routes = load_routes(ROUTES, ROOT)
            except (OSError, ValueError):
                denied(flow, "invalid private experiment service registry")
                return
            authorization = flow.request.headers.get("Authorization", "")
            key = authorization.removeprefix("Bearer ")
            identity = None
            if route_key == "dci":
                target = routes.get("dci", {}).get(key)
                if path != "/rpc":
                    target = None
            else:
                identity = registered_client(routes, authorization)
                target = routes.get("inference") if identity is not None else None
                if not path.startswith("/v1/"):
                    target = None
            if not isinstance(target, str) or not re.fullmatch(r"http://127\.0\.0\.1:\d+", target):
                denied(flow, "unregistered experiment service")
                return
            headers = {"Content-Type": flow.request.headers.get("Content-Type", "application/json"),
                       "Authorization": authorization}
            if route_key == "inference":
                try:
                    key = read_service_key(service_key_path(ROOT), ROOT)
                except (OSError, ValueError):
                    denied(flow, "private inference service credential unavailable")
                    return
                # Do not forward a client-supplied identity assertion. Only the
                # registered Authorization hash is trusted, after service auth.
                headers["Authorization"] = "Bearer " + key
                headers[CLIENT_AUTHORIZATION_HEADER] = identity
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=1200) as client:
                    response = await client.request(flow.request.method, target + path,
                        content=flow.request.raw_content, headers=headers)
                flow.response = http.Response.make(response.status_code, response.content,
                    {"Content-Type": response.headers.get("content-type", "application/json")})
                audit("service", "http://"+host+path, route_key, response.status_code)
            except Exception as exc:
                audit("service_error", "http://"+host+path,
                      str(exc) if isinstance(exc, ImportError) else type(exc).__name__, 502)
                flow.response = http.Response.make(502, json.dumps({"error": type(exc).__name__}).encode(), {"Content-Type":"application/json"})
            return
        decoded = flow.request.pretty_url
        for _ in range(3):
            decoded = unquote(decoded)
        if BLOCKED.search(decoded):
            denied(flow, "benchmark artifact reference")
        elif host not in IDENTITY_HOSTS and not public_host(host):
            denied(flow, "non-public destination")
        elif flow.request.port not in (80,443):
            denied(flow, "non-web destination port")
        else:
            audit("allowed", flow.request.pretty_url)

    def response(self, flow):
        if flow.request.host.endswith(".hangxiao.internal") or flow.request.host in IDENTITY_HOSTS or flow.request.host == "openrouter.ai":
            return
        content_type = flow.response.headers.get("content-type", "") if flow.response else ""
        if flow.response and any(x in content_type for x in ("text/", "json", "xml")):
            body = flow.response.get_text(strict=False) or ""
            if BLOCKED.search(body):
                denied(flow, "benchmark artifact name in retrieved document")


addons = [Policy()]
