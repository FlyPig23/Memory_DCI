"""Minimal in-container Codex lifecycle; no retrieval, memory, or MCP server."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

CONTROLLERS = {'/opt/codex/codex', '/opt/codex/codex-code-mode-host'}


def processes():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(entry.name)] = {'parent': int(fields[1]), 'start': int(fields[19])}
        except (OSError, ValueError, IndexError):
            pass
    return result


def controller_pids():
    current = processes()
    protected, cursor = {1}, os.getpid()
    while cursor and cursor not in protected:
        protected.add(cursor)
        cursor = current.get(cursor, {}).get('parent', 0)
    targets = []
    for pid in current:
        if pid in protected:
            continue
        try:
            first = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0', 1)[0]
        except OSError:
            continue
        if first.decode(errors='replace') in CONTROLLERS:
            targets.append(pid)
    return targets


def freeze():
    """Stop exact Codex controls, preserving task-created services and their state."""
    killed = set()
    for _ in range(10):
        targets = controller_pids()
        if not targets:
            break
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        time.sleep(.02)
    remaining = controller_pids()
    if remaining:
        raise RuntimeError('Codex control processes remain after freeze')
    return {'stopped_runtime_process_count': len(killed), 'remaining_runtime_process_count': 0,
            'preserved_initial_services': True, 'preserved_task_created_services': True,
            'scope': 'Exact Codex CLI and code-mode host processes; task services remain live'}


def supervise(args):
    with open(args.prompt, 'rb') as prompt, open(args.events, 'wb') as events, open(args.stderr, 'wb') as errors:
        process = subprocess.Popen(args.command, stdin=prompt, stdout=events, stderr=errors,
                                   start_new_session=True)
        try:
            return process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return 124
        finally:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('processes')
    sub.add_parser('freeze')
    runner = sub.add_parser('supervise')
    for name in ('prompt', 'events', 'stderr'):
        runner.add_argument('--' + name, required=True)
    runner.add_argument('--timeout', type=float, required=True)
    runner.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.mode == 'processes':
        print(json.dumps(processes()))
        return 0
    if args.mode == 'freeze':
        print(json.dumps(freeze()))
        return 0
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]
    if not args.command or args.timeout <= 0:
        raise ValueError('A command and positive solving deadline are required')
    return supervise(args)


if __name__ == '__main__':
    raise SystemExit(main())
