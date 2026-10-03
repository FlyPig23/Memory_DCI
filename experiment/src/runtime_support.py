"""Shared, project-owned runtime plumbing; no services start on import."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
from .task_runtime import Mount, RuntimeConfig, TaskRuntime, contained


def prepare_ca_bundle(project_root: Path) -> Path:
    output = project_root / "experiment/runtime/network-ca.pem"
    ca = project_root / "experiment/runtime/mitmproxy/mitmproxy-ca-cert.pem"
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    output.write_bytes(system_ca.read_bytes() + b"\n" + ca.read_bytes())
    return output


def make_runtime_config(project_root: Path, image: str, **overrides) -> RuntimeConfig:
    root = project_root.resolve()
    runtime = root / "experiment/runtime"
    mounts = (
        Mount(runtime / "bin", "/opt/codex"),
        Mount(runtime / "gateways/shared", "/opt/network"),
        Mount(runtime / "network-ca.pem", "/opt/network-ca.pem"),
        Mount(root / "experiment/src/network_bridge.py", "/opt/network_bridge.py"),
        Mount(runtime / "nss-tools/unpacked", "/opt/nss-tools"),
        Mount(runtime / "mitmproxy/mitmproxy-ca-cert.pem", "/opt/proxy-ca.pem"),
    )
    proxy = "http://127.0.0.1:18080"
    env = {key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    env.update({key: "127.0.0.1,localhost" for key in ("NO_PROXY", "no_proxy")})
    env.update({key: "/opt/network-ca.pem" for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")})
    env["AGENT_BROWSER_PROXY"] = proxy
    env["AGENT_BROWSER_SOCKET_DIR"] = "/root/.cache/agent-browser"
    env["DDGS_PROXY"] = proxy
    env["XDG_CONFIG_HOME"] = "/root/.cache/config"
    startup = (
        "python3 /opt/network_bridge.py client --socket /opt/network/egress.sock --listen-port 18080 &",
        "python3 -c 'import socket,time; [(time.sleep(0.1)) for _ in range(20) if socket.socket().connect_ex((\"127.0.0.1\",18080))]; s=socket.create_connection((\"127.0.0.1\",18080),timeout=5); s.close()'",
    )
    # Chromium uses the task-private NSS trust database, not only Python/Node CA
    # environment variables. Import only the public run proxy certificate.
    startup += (
        "mkdir -p /root/.pki/nssdb /root/.cache/config /root/.cache/agent-browser",
        "test -f /root/.pki/nssdb/cert9.db || /opt/nss-tools/usr/bin/certutil -N -d sql:/root/.pki/nssdb --empty-password",
        "/opt/nss-tools/usr/bin/certutil -A -d sql:/root/.pki/nssdb -n skill-dci-egress -t C,, -i /opt/proxy-ca.pem",
    )
    values = dict(project_root=root, image=image, network="none", auth_file=None,
                  support_mounts=mounts + tuple(overrides.pop("support_mounts", ())),
                  startup_commands=startup + tuple(overrides.pop("startup_commands", ())),
                  environment=env | dict(overrides.pop("environment", {})))
    values.update(overrides)
    return RuntimeConfig(**values)


def copy_runtime_auth(runtime: TaskRuntime, source: Path | None = None) -> None:
    source = contained(source or runtime.config.project_root / "experiment/runtime/codex_auth/auth.json", runtime.config.project_root)
    target = runtime.codex_home / "auth.json"
    shutil.copyfile(source, target)
    os.chmod(target, 0o600)
