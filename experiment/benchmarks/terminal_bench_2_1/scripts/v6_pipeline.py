"""Build the frozen failure seed, then run the authorized full V6 test sequence.

The serial controller can outlive a terminal session. State files make both
phases inspectable; failed construction never launches a partial-seed test.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import time

from . import build_failure_memory as builder
from . import protocol, run


def execute_pipeline(*, work_dir, seed_dir, run_name, auth_file, timeout_seconds=None,
                     packet_chars=90000, accept_builder_code_update=False,
                     accept_builder_budget_update=False, retry_builder_budget_exhausted=False):
    work_dir, seed_dir = Path(work_dir).resolve(), Path(seed_dir).resolve()
    relative = seed_dir.relative_to(protocol.BASE).as_posix()
    work_dir.mkdir(parents=True, exist_ok=True)
    state_path = work_dir / 'pipeline_state.json'
    with (work_dir / 'pipeline.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {'phase': 'building_failure_memory', 'controller_pid': os.getpid(),
                 'run_name': run_name, 'started_at': time.time(),
                 'builder_model': builder.MODEL, 'builder_effort': builder.EFFORT,
                 'solver_model': 'gpt-5.6-sol', 'solver_effort': 'medium',
                 'verifier_policy': 'unlimited', 'memory_seed': relative,
                 'accept_builder_code_update': accept_builder_code_update,
                 'accept_builder_budget_update': accept_builder_budget_update,
                 'retry_builder_budget_exhausted': retry_builder_budget_exhausted,
                 'builder_budget_mode': 'adaptive' if timeout_seconds is None else 'fixed'}
        run.write_json(state_path, state)
        try:
            built = builder.build(protocol.BASE, work_dir, seed_dir, auth_file=Path(auth_file),
                image=builder.DEFAULT_IMAGE, timeout_seconds=timeout_seconds,
                packet_chars=packet_chars, max_jobs=None, retry_infrastructure=False,
                accept_code_update=accept_builder_code_update,
                accept_budget_update=accept_builder_budget_update,
                retry_budget_exhausted=retry_builder_budget_exhausted)
            if built['status'] == 'paused':
                state.update(phase='paused', completed_builder_jobs=built['complete_jobs'],
                             seed_entries=built['entry_count'])
                return state
            if built['status'] != 'frozen' or not built['entry_count']:
                raise ValueError('A complete, nonempty frozen failure memory is required')
            state.update(phase='testing_v6', seed_entries=built['entry_count'], updated_at=time.time())
            run.write_json(state_path, state)
            plan = protocol.build_plan('v6', run_name, model='gpt-5.6-sol', effort='medium',
                                       verifier_policy='unlimited', memory_seed=relative)
            asyncio.run(run.execute(plan, auth_file=Path(auth_file)))
            result = run.status('v6', run_name)
            state.update(phase=result['status'], completed=result['completed'], total=result['total'],
                         finished_at=time.time())
        except BaseException as exc:
            state.update(phase='needs_review', failed_phase=state['phase'], error_type=type(exc).__name__)
            raise
        finally:
            state['updated_at'] = time.time()
            run.write_json(state_path, state)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--seed-dir', type=Path, required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--auth-file', type=Path,
                        default=Path(os.environ.get('CODEX_AUTH_FILE', '~/.codex/auth.json')).expanduser())
    parser.add_argument('--timeout-seconds', type=int, default=None,
                        help='Explicit fixed builder limit; omit for per-task adaptive budgets')
    parser.add_argument('--packet-chars', type=int, default=90000)
    parser.add_argument('--accept-builder-code-update', action='store_true',
                        help='Record a builder-only code amendment; preserve data, budgets and prior outputs')
    parser.add_argument('--accept-builder-budget-update', action='store_true',
                        help='Record a builder budget amendment; preserve all previous attempts and outputs')
    parser.add_argument('--retry-builder-budget-exhausted', action='store_true',
                        help='Retry an exhausted builder attempt only under a recorded larger budget')
    args = parser.parse_args(argv)
    result = execute_pipeline(work_dir=args.work_dir, seed_dir=args.seed_dir, run_name=args.run_name,
                              auth_file=args.auth_file.expanduser().resolve(),
                              timeout_seconds=args.timeout_seconds, packet_chars=args.packet_chars,
                              accept_builder_code_update=args.accept_builder_code_update,
                              accept_builder_budget_update=args.accept_builder_budget_update,
                              retry_builder_budget_exhausted=args.retry_builder_budget_exhausted)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
