#!/usr/bin/env python3
"""A fixed-destination stream bridge; no filesystem or Docker control protocol.

server: project-owned Unix socket -> one host loopback HTTP proxy.
client: container loopback TCP -> the mounted Unix socket (network=none).
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import select
import socket
import socketserver


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        options = self.server.options
        upstream = socket.socket(socket.AF_UNIX if options.mode == "client" else socket.AF_INET, socket.SOCK_STREAM)
        upstream.settimeout(15)
        try:
            upstream.connect(options.socket if options.mode == "client" else ("127.0.0.1", options.proxy_port))
            upstream.settimeout(None)
            peers = {self.request: upstream, upstream: self.request}
            while peers:
                ready, _, _ = select.select(list(peers), [], [], 1800)
                if not ready:
                    return
                for source in ready:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    peers[source].sendall(chunk)
        except (OSError, TimeoutError):
            return
        finally:
            upstream.close()


class TCP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Unix(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["server", "client"])
    parser.add_argument("--socket", required=True)
    parser.add_argument("--proxy-port", type=int, default=18081)
    parser.add_argument("--listen-port", type=int, default=18080)
    args = parser.parse_args()
    if args.mode == "server":
        path = Path(args.socket)
        project = Path(__file__).resolve().parents[4]
        if not path.resolve().is_relative_to(project / "experiment/benchmarks/wildclaw_bench/runtime/gateways"):
            raise SystemExit("Proxy socket must be inside this project's gateway directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise SystemExit("Existing socket must be checked for a live server before replacement")
        server = Unix(str(path), Relay)
        os.chmod(path, 0o600)
    else:
        server = TCP(("127.0.0.1", args.listen_port), Relay)
    server.options = args
    server.serve_forever()


if __name__ == "__main__":
    main()
