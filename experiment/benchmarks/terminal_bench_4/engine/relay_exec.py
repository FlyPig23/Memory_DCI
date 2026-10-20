"""In-container shim: expose a mounted unix socket as http://127.0.0.1:PORT, then become the given command.

Usage: relay_exec.py PORT SOCKET_PATH COMMAND [ARG ...]

The forwarder runs in a forked child and this process is replaced by COMMAND, so the supervisor's child is
the CLI itself and stopping it stops the CLI. serve_memory.py freeze ends the forwarder.
"""
import os
import socket
import sys
import threading


def pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve(listener: socket.socket, path: str) -> None:
    while True:
        client, _ = listener.accept()
        try:
            upstream = socket.socket(socket.AF_UNIX)
            upstream.connect(path)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main() -> None:
    port, path, command = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(128)
    if os.fork() == 0:
        # Detach from the CLI's stdio so the event stream closes when the CLI ends.
        null = os.open(os.devnull, os.O_RDWR)
        for descriptor in (0, 1, 2):
            os.dup2(null, descriptor)
        serve(listener, path)
        os._exit(0)
    listener.close()
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
