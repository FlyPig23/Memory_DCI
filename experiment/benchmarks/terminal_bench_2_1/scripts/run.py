"""Serial baseline/V5/V6 runner. Planning and status never start Docker or a model."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

from . import health_guard, protocol

BASE = protocol.BASE


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temporary.open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def summary(directory, arm, *, task=None, settings=None, memory_seed=None):
    directory = Path(directory)
    name = {'baseline': 'baseline_result.json', 'v5': 'v5_result.json', 'v6': 'v6_result.json'}[arm]
    path = directory / name
    if not path.is_file():
        return {'run_id': directory.name, 'status': 'incomplete', 'valid': False}
    result = protocol.load(path)
    harbor = result.get('harbor') or {}
    rewards = (harbor.get('verifier_result') or {}).get('rewards') or {}
    reward = rewards.get('reward')
    scored = isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward)
    model_path = directory / 'agent/model_audit.json'
    model = protocol.load(model_path) if model_path.is_file() else {}
    usage_path = directory / 'agent/usage.json'
    usage = protocol.load(usage_path) if usage_path.is_file() else {}
    health = health_guard.inspect_trial(directory)
    frozen = result.get('freeze') or {}
    reasons = []
    deviations = []
    if not scored:
        reasons.append('missing_official_reward')
    if not model.get('model_effort_verified') or (settings is not None and
            (model.get('models') != [settings['model']] or model.get('efforts') != [settings['reasoning_effort']])):
        reasons.append('model_audit_failed')
    if not health['phases']['agent']['actual_inference']:
        reasons.append('no_solver_inference_evidence')
    if health['blocked'] and (arm != 'v6' or health['phases']['agent']['blocked']):
        reasons.append('infrastructure_failure')
    if not frozen.get('taken_before_hidden_grading'):
        reasons.append('missing_pre_grader_boundary')
    if arm == 'baseline':
        if not (result.get('isolation') or {}).get('valid'):
            reasons.append('isolation_audit_failed')
        if not frozen.get('solver_stopped') or not frozen.get('snapshot_valid'):
            reasons.append('freeze_audit_failed')
    else:
        review = result.get('review') or {}
        initial_path = directory / 'memory_initial.json'
        initial = protocol.load(initial_path) if initial_path.is_file() else {}
        expected_count = (memory_seed or {}).get('entry_count') if arm == 'v6' else 0
        if (initial.get('valid') is not True or initial.get('entry_count') != expected_count
                or initial.get('event_count') != 0 or initial.get('scope') != 'single_test_task'
                or initial.get('task_id') != result.get('task_id')):
            reasons.append('initial_memory_audit_failed')
        if arm == 'v6':
            expected_files = (memory_seed or {}).get('files_sha256', {})
            expected_entries = {k: v for k, v in expected_files.items() if k.startswith('entries/')}
            observed_entries = {k: v for k, v in initial.get('files_sha256', {}).items() if k.startswith('entries/')}
            if (not memory_seed or not expected_count or initial.get('inherited_entries') != expected_count
                    or initial.get('seed_manifest_sha256') != memory_seed.get('manifest_sha256')
                    or observed_entries != expected_entries):
                reasons.append('frozen_seed_mismatch')
        decision = (review.get('memory_audit') or {}).get('final_decision') or {}
        if not (review.get('valid') and review.get('received_hidden_grade') is False
                and review.get('evidence_unchanged') and decision.get('agent_stage') == 'review'
                and decision.get('task_id') == result.get('task_id')
                and decision.get('decision') in ('write', 'no_update')):
            # Post-submission review cannot change the answer. Preserve and
            # report a review failure independently of the official reward.
            if (arm == 'v6' and review.get('received_hidden_grade') is False
                    and review.get('evidence_unchanged') is not False):
                deviations.append('post_submission_review_incomplete')
            else:
                reasons.append('review_audit_failed')
    contract_path = directory / 'runtime_contract.json'
    contract = protocol.load(contract_path) if contract_path.exists() else {}
    if arm == 'v6' and (contract.get('condition') != 'v6'
            or contract.get('seed_manifest_sha256') != (memory_seed or {}).get('manifest_sha256')
            or contract.get('initial_memory_entries') != (memory_seed or {}).get('entry_count')):
        reasons.append('seed_contract_mismatch')
    if task is not None and settings is not None:
        effective_verifier = None if settings['verifier_policy'] == 'unlimited' else task['official_verifier_timeout_seconds']
        if (result.get('task_id') != task['task_id'] or contract.get('task_id') != task['task_id']
                or contract.get('solver_timeout_seconds') != task['official_agent_timeout_seconds']
                or contract.get('official_verifier_timeout_seconds') != task['official_verifier_timeout_seconds']
                or contract.get('verifier_timeout_seconds') != effective_verifier
                or contract.get('verifier_timeout_policy') != settings['verifier_policy']
                or contract.get('model') != settings['model']
                or contract.get('reasoning_effort') != settings['reasoning_effort']
                or contract.get('cross_task_memory') is not False or contract.get('initial_skills') != 0):
            reasons.append('runtime_contract_mismatch')
    audits = ('runtime_contract.json', 'agent/model_audit.json', 'agent/usage.json',
              'freeze.json', 'isolation.json', 'memory_initial.json', 'memory_review.json')
    row = {'run_id': directory.name, 'task_id': result.get('task_id'),
            'status': 'completed' if not reasons else 'needs_review', 'valid': not reasons,
            'reward': reward if scored else None, 'reasons': reasons,
            'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
            'result_sha256': protocol.sha(path),
            'audit_sha256': {name: protocol.sha(directory / name) for name in audits if (directory / name).is_file()}}
    if arm == 'v6':
        row['workflow_deviations'] = deviations
    return row


def saved_completion(directory, arm, *, task=None, settings=None, memory_seed=None):
    """Never accept a changed first result or silently retry a partial attempt."""
    directory = Path(directory)
    completion_path = directory / 'completion.json'
    if not completion_path.is_file():
        raise ValueError('Existing incomplete trial cannot be rerun; preserve it and choose a new run name')
    saved = protocol.load(completion_path)
    observed = summary(directory, arm, task=task, settings=settings, memory_seed=memory_seed)
    if not observed['valid'] or saved != observed:
        raise ValueError('Saved trial result or audits changed; automatic continuation refused')
    return observed


def controller_active(directory):
    """Read the held flock, without creating files or relying on a reused PID."""
    path = Path(directory) / 'controller.lock'
    if not path.exists():
        return False
    with path.open('r') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def status(arm, run_name, *, base=BASE):
    directory = protocol.run_directory(base, run_name)
    saved = directory / 'protocol.json'
    if not saved.is_file():
        return {'run_name': run_name, 'arm': arm, 'status': 'not_started', 'completed': 0, 'total': 36}
    plan = protocol.load(saved)
    if plan['arm'] != arm:
        raise ValueError('Run name belongs to a different arm')
    state_path = directory / 'state.json'
    state = protocol.load(state_path) if state_path.exists() else {}
    active = controller_active(directory)
    rows = []
    for task in plan['tasks']:
        trial = directory / 'trials' / task['run_id']
        if trial.exists():
            row = summary(trial, arm, task=task, settings=plan['settings'], memory_seed=plan.get('memory_seed'))
            if not (trial / 'completion.json').exists():
                row.update(valid=False, status=('running' if active and state.get('active_task') == task.get('task_id') else 'incomplete'))
            elif protocol.load(trial / 'completion.json') != row:
                row.update(valid=False, status='changed_after_completion')
            rows.append(row)
    completed = sum(row['valid'] for row in rows)
    scores = [row['reward'] for row in rows if row.get('reward') is not None]
    current_status = state.get('status', 'incomplete')
    if completed == len(plan['tasks']):
        current_status = 'completed'
    elif any(not row['valid'] and row['status'] != 'running' for row in rows) or current_status == 'completed':
        current_status = 'needs_review'
    elif active:
        current_status = 'running'
    elif current_status == 'running':
        current_status = 'interrupted'
    return {'run_name': run_name, 'arm': arm, 'settings': plan['settings'],
            'status': current_status,
            'completed': completed, 'total': len(plan['tasks']), 'attempted': len(rows),
            'mean_reward_over_scored_attempts': sum(scores) / len(scores) if scores else None,
            'scored_attempts': len(scores), 'controller_active': active, 'active_task': state.get('active_task'), 'trials': rows}


def configure_runtime(settings):
    # Harbor imports an agent by this module path in the same controller process.
    # Only one serial run is permitted per process; the frozen protocol records
    # these values and each native session audits the actual model and effort.
    from . import baseline_runtime, harbor_runtime, runtime
    for module in (baseline_runtime, harbor_runtime, runtime):
        module.MODEL = settings['model']
        module.EFFORT = settings['reasoning_effort']
        module.CLI_VERSION = settings['cli_version']
    return baseline_runtime, runtime


async def resolve_image(requested, saved=None):
    """Resolve once per run; resume uses the same image, even if a tag moves."""
    from .harbor_runtime import command
    reference = saved['image_id'] if saved else requested
    code, raw, _ = await command(['docker', 'image', 'inspect', reference], check=False)
    if code:
        pull = saved.get('repo_digest') if saved else requested
        if not pull:
            raise ValueError('Frozen image is missing and has no registry digest; restore that image before resuming')
        await command(['docker', 'pull', pull], timeout=600)
        _, raw, _ = await command(['docker', 'image', 'inspect', reference if saved else pull])
    metadata = json.loads(raw)[0]
    result = {'image_id': metadata['Id'],
              'repo_digest': next(iter(metadata.get('RepoDigests') or []), None)}
    if saved and result['image_id'] != saved['image_id']:
        raise ValueError('Resolved image differs from frozen protocol')
    return saved or result


async def execute(plan, *, auth_file, base=BASE):
    base = Path(base)
    if importlib.metadata.version('harbor') != plan['harbor_version']:
        raise ValueError('Use the pinned Harbor environment from setup_runtime')
    installed = protocol.support(base)
    frozen = {**plan, 'support': installed}
    directory = protocol.run_directory(base, plan['run_name'])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = directory / 'protocol.json'
        previous = protocol.load(saved) if saved.exists() else None
        if previous is not None and {k: v for k, v in previous.items() if k != 'container_image'} != frozen:
            raise ValueError('Run protocol/config/data/support changed; choose a new run name')
        if previous is None and (directory / 'trials').exists():
            raise ValueError('Existing trials have no frozen protocol')
        baseline, v5 = configure_runtime(plan['settings'])
        health_guard.ensure_auth_lifetime(auth_file, max(t['official_agent_timeout_seconds'] for t in plan['tasks']),
                                          plan['review_timeout_seconds'] or 0)
        image = await resolve_image(plan['settings']['review_image'], previous.get('container_image') if previous else None)
        frozen['container_image'] = image
        if previous is None:
            write_json(saved, frozen)
        from .runtime import environment_for
        python_root = base / installed['python_root']
        config = plan['settings']
        state = {'status': 'running', 'active_task': None, 'completed': 0,
                 'controller_pid': os.getpid(), 'started_at': time.time()}
        try:
            for task in plan['tasks']:
                trial = protocol.local_path(directory, 'trials/' + task['run_id'])
                if trial.exists():
                    saved_completion(trial, plan['arm'], task=task, settings=plan['settings'], memory_seed=plan.get('memory_seed'))
                    state['completed'] += 1
                    continue
                if protocol.sources(base) != plan['sources_sha256']:
                    raise ValueError('Runtime sources changed during the run')
                for relative, digest in plan['inputs_sha256'].items():
                    if protocol.sha(protocol.local_path(base, relative)) != digest:
                        raise ValueError('Input manifests changed during the run')
                path = protocol.local_path(base, task['source_path'])
                if protocol.inventory(path) != task['files_sha256']:
                    raise ValueError('Task package changed during the run')
                # Reads local expiration metadata only; no network/model probe.
                health_guard.ensure_auth_lifetime(auth_file, task['official_agent_timeout_seconds'],
                                                  plan['review_timeout_seconds'] or 0)
                state.update(active_task=task['task_id'], updated_at=time.time())
                write_json(directory / 'state.json', state)
                arguments = {'python_root': python_root, 'auth_file': auth_file,
                             'environment_config': environment_for(path),
                             'verifier_policy': config['verifier_policy']}
                if plan['arm'] == 'v6':
                    seed = plan['memory_seed']
                    seed_path = protocol.local_path(base, seed['path'])
                    if protocol.inventory(seed_path) != seed['files_sha256']:
                        raise ValueError('Frozen failure memory changed during the run')
                    arguments['memory_seed'] = seed_path
                if plan['arm'] == 'baseline':
                    await baseline.run_trial(path, trial, task['task_id'], cleanup_image=image['image_id'], **arguments)
                else:
                    await v5.run_trial(path, trial, base / 'prepared/pool', task['task_id'],
                                       review_image=image['image_id'], **arguments)
                row = summary(trial, plan['arm'], task=task, settings=config, memory_seed=plan.get('memory_seed'))
                print(json.dumps(row, ensure_ascii=False), flush=True)
                if not row['valid']:
                    raise RuntimeError('Trial failed its completion audits; first result preserved, no retry')
                write_json(trial / 'completion.json', row)
                state.update(completed=state['completed'] + 1, active_task=None, updated_at=time.time())
                write_json(directory / 'state.json', state)
            state.update(status='completed', finished_at=time.time())
        except BaseException as exc:
            state.update(status='needs_review', error_type=type(exc).__name__)
            raise
        finally:
            state['updated_at'] = time.time()
            write_json(directory / 'state.json', state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'run', 'status'))
    parser.add_argument('--arm', choices=protocol.ARMS, required=True)
    parser.add_argument('--run-name', required=True)
    parser.add_argument('--model', help='Override config.json model; account must have access')
    parser.add_argument('--effort', help='Override config.json reasoning effort')
    parser.add_argument('--verifier-policy', choices=('official', 'unlimited'))
    parser.add_argument('--memory-seed', help='V6 only: frozen seed directory relative to this benchmark directory')
    parser.add_argument('--auth-file', type=Path, help='OAuth auth JSON; otherwise CODEX_AUTH_FILE or ~/.codex/auth.json')
    args = parser.parse_args(argv)
    phase = 'preflight'
    try:
        if args.command == 'status':
            output = status(args.arm, args.run_name)
        else:
            plan = protocol.build_plan(args.arm, args.run_name, model=args.model, effort=args.effort,
                                       verifier_policy=args.verifier_policy, memory_seed=args.memory_seed)
            if args.command == 'run':
                auth_file = (args.auth_file or Path(os.environ.get('CODEX_AUTH_FILE', '~/.codex/auth.json'))).expanduser().resolve()
                phase = 'execution'
                asyncio.run(execute(plan, auth_file=auth_file))
                output = status(args.arm, args.run_name)
            else:
                output = {**plan, 'tasks': [{k: v for k, v in row.items() if k != 'files_sha256'} for row in plan['tasks']]}
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (Exception, KeyboardInterrupt) as exc:
        # External tools and auth parsing can include sensitive error strings.
        # Emit only a stable category; trial-local logs hold execution evidence.
        if isinstance(exc, FileNotFoundError) and exc.filename:
            print(f'Missing required file: {exc.filename}', file=sys.stderr)
        elif isinstance(exc, health_guard.AuthValidityError) or (phase == 'preflight' and isinstance(exc, ValueError)):
            print(str(exc), file=sys.stderr)
        print(f'TB2.1 {args.command} stopped ({type(exc).__name__}); no automatic retry. '
              'Check inputs, setup, and the run state/trial logs.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
