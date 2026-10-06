"""Refresh V5 reports as the existing controller progresses; finalize at exit.

Observes one recorded controller identity. Never starts/stops a solver, resumes
a failed batch, calls a model, or regrades a task. A separate lock prevents
duplicate watchers. Reports run in fresh subprocesses to use current audit code.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256
from experiment.benchmarks.wildclaw_bench.scripts.service_control import process_identity, identity_matches

BASE = ROOT / 'experiment/benchmarks/wildclaw_bench/runs/dci_memory'


def controller_key(value):
    return {key: value.get(key) for key in ('pid', 'uid', 'start_ticks', 'argv', 'cwd')}


def refresh_reports():
    commands = [
        ['-m', 'experiment.benchmarks.wildclaw_bench.variants.dci_memory.report'],
        ['experiment/benchmarks/wildclaw_bench/reports/dci_memory/audit.py'],
        ['experiment/benchmarks/wildclaw_bench/reports/dci_memory/workflow_audit.py'],
        ['experiment/benchmarks/wildclaw_bench/reports/dci_memory/build_comparison.py'],
    ]
    for command in commands:
        subprocess.run([sys.executable, '-B', *command], cwd=ROOT, check=True)


def watch():
    with (BASE / 'report_watch.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((BASE / 'batch.json').read_text())
        expected = controller_key(state['controller'])
        status_path = BASE / 'report_watch.json'
        status = {'status': 'watching', 'process': process_identity(os.getpid()),
                  'controller': expected, 'started_at': time.time(),
                  'script_sha256': sha256(Path(__file__)), 'last_refreshed_completed': None}
        try:
            while True:
                state = json.loads((BASE / 'batch.json').read_text())
                if controller_key(state['controller']) != expected:
                    raise RuntimeError('Controller identity changed; review the new batch before watching it')
                live = identity_matches(expected, process_identity(expected['pid']))
                count = len(state.get('completed', []))
                status.update(updated_at=time.time(), controller_live=live,
                              batch_status=state['status'], observed_completed=count,
                              active=state.get('active'))
                atomic_json(status_path, status)
                if not live:
                    if state['status'] != 'completed' or count != 24:
                        raise RuntimeError('Controller exited without all 24 tasks complete; no automatic solver restart')
                    subprocess.run([sys.executable, '-B', 'experiment/benchmarks/wildclaw_bench/scripts/finalize_v5.py'],
                                   cwd=ROOT, check=True)
                    status.update(status='finalized', last_refreshed_completed=24, finished_at=time.time())
                    atomic_json(status_path, status)
                    return
                # At completion the controller runs its own reports. Wait for
                # it to exit before touching final outputs or batch.json.
                if (state['status'] == 'running' and count < 24
                        and count != status['last_refreshed_completed']):
                    refresh_reports()
                    status.update(last_refreshed_completed=count, last_refreshed_at=time.time())
                    atomic_json(status_path, status)
                time.sleep(15)
        except BaseException as error:
            status.update(status='needs_review', finished_at=time.time(),
                          error={'type': type(error).__name__, 'message': str(error)})
            atomic_json(status_path, status)
            raise


if __name__ == '__main__':
    watch()
