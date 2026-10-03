"""Auditable serial V5 controller; never silently re-run a solver."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from experiment.src.experiment_protocol import atomic_json
from experiment.scripts.service_control import process_identity, identity_matches
from . import runner


def execute():
    directory = runner.ROOT / runner.RUNS
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'batch.lock').open('a') as lock:
        # Wait for the launcher's short lock hold; duplicate launch is checked
        # separately and never schedules a second solver rollout.
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan = runner.prepare(write=False)
        state = {'version': 'V5', 'status': 'running', 'controller': process_identity(os.getpid()),
                 'started_at': time.time(), 'updated_at': time.time(), 'completed': [], 'active': None}
        try:
            for row in plan['tasks']:
                path = directory / 'formal' / row['run_id']
                state['active'] = row
                state['updated_at'] = time.time()
                atomic_json(directory / 'batch.json', state)
                if not path.exists():
                    runner.run_one(row)
                result = runner.audit_result(row)
                state['completed'].append(result)
                state['active'] = None
                state['updated_at'] = time.time()
                atomic_json(directory / 'batch.json', state)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            state['status'] = 'completed'
            state['finished_at'] = time.time()
        except BaseException as error:
            state['status'] = 'needs_review'
            state['error'] = {'type': type(error).__name__, 'message': str(error)}
            raise
        finally:
            state['updated_at'] = time.time()
            atomic_json(directory / 'batch.json', state)


def launch():
    directory = runner.ROOT / runner.RUNS
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'batch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / 'batch.json'
        if path.exists():
            state = json.loads(path.read_text())
            identity = state.get('controller', {})
            if identity.get('pid') and identity_matches(identity, process_identity(identity['pid'])):
                raise ValueError('V5 controller is already live')
        runner.prepare()
        logs = directory / 'controllers'
        logs.mkdir(exist_ok=True)
        name = str(time.time_ns())
        command = [str(runner.ROOT / 'experiment/.venv/bin/python'), '-B', '-m',
                   'experiment.variants.dci_memory.batch', 'run']
        with (logs / (name + '.log')).open('x') as output:
            proc = subprocess.Popen(command, cwd=runner.ROOT, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        record = {'version': 'V5', 'command': command, 'process': process_identity(proc.pid),
                  'log': str((logs / (name + '.log')).relative_to(runner.ROOT)), 'created_at': time.time()}
        atomic_json(logs / (name + '.json'), record)
        atomic_json(directory / 'batch.json', {'version': 'V5', 'status': 'starting',
                    'controller': record['process'], 'completed': [], 'active': None,
                    'updated_at': time.time(), 'launch_record': str((logs / (name + '.json')).relative_to(runner.ROOT))})
        print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('launch', 'run'))
    args = parser.parse_args()
    launch() if args.command == 'launch' else execute()
