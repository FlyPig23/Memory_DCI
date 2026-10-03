"""Resume the user-interrupted V5 reviewer, never its already-finished solver.

This controller-side recovery helper is outside the frozen variant. Importing
it performs no writes or model calls. The caller must explicitly call
resume_review() after establishing that resumption is authorized.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiment.src.codex_backend import CodexBackend, ExecutionResult, convert_transcript, extract_usage, read_jsonl
from experiment.src.experiment_protocol import atomic_json, sha256
from experiment.src.runtime_support import make_runtime_config
from experiment.src.task_runtime import Mount, OWNER_LABEL, PREFIX, TaskRuntime, TaskSpec
from experiment.scripts.evaluate_wildclaw import load_evidence
from experiment.variants.dci_memory import runner
from experiment.variants.dci_memory.tools import audit_memory


def _load(path):
    return json.loads(path.read_text())


def _time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def _assert(condition, message):
    if not condition:
        raise ValueError(message)


def _session_state(review_dir):
    paths = sorted((review_dir / 'codex-home/sessions').rglob('*.jsonl'))
    _assert(len(paths) == 1, 'Expected exactly the original interrupted reviewer session')
    rows = list(read_jsonl(paths[0]))
    metadata = [r['payload'] for r in rows if r.get('type') == 'session_meta']
    ids = {r.get('id', r.get('session_id')) for r in metadata}
    _assert(len(ids) == 1 and None not in ids, 'Reviewer session identity is ambiguous')
    times = [_time(r['timestamp']) for r in rows if r.get('timestamp')]
    _assert(bool(times), 'Reviewer timestamps missing')
    return paths[0], next(iter(ids)), min(times), max(times)


def resume_review(root: Path, run_dir: Path, task: TaskSpec | None = None, plan: dict | None = None) -> dict:
    """Resume the same stopped reviewer session within its remaining budget.

    Returns the frozen runner's memory_review shape and publishes memory exactly
    as review_memory does. The interrupted interval is measured from the first
    Docker StartedAt through its original FinishedAt (a conservative upper bound
    including setup); this includes tool time and excludes the later interruption
    gap. No solver is invoked.
    """
    root, run_dir = Path(root).resolve(), Path(run_dir).resolve()
    if isinstance(task, dict) and plan is None:
        plan, task = task, None  # Also support the convenient three-argument call.
    _assert(run_dir.parent == root / runner.RUNS / 'formal', 'Not a V5 formal run directory')
    plan = plan or _load(root / runner.VARIANT / 'prepared/formal/protocol.json')
    _assert(runner.prepare(root, write=False) == plan, 'Frozen V5 sources/configuration changed')
    row = next((r for r in plan['tasks'] if r['run_id'] == run_dir.name), None)
    _assert(row is not None, 'Run is outside the frozen V5 schedule')
    record = _load(run_dir / 'run.json')
    _assert(record['task_id'] == row['task_id'], 'Run task identity mismatch')
    _assert(record.get('execution', {}).get('status') in {'completed', 'timeout'}, 'No finished solver to preserve')
    _assert(not (run_dir / 'evaluation.json').exists(), 'Do not review after hidden grading is available')
    _assert(not (run_dir / 'memory_review.json').exists(), 'Completed reviewer record already exists')
    _assert(not (run_dir / 'memory_before_review').exists(), 'Memory already published by a reviewer')
    evidence, frozen, frozen_task = load_evidence(run_dir)
    _assert(frozen_task['task_id'] == row['task_id'], 'Frozen evidence belongs to another task')
    if task is not None:
        _assert(task.task_id == row['task_id'] and task.prompt == frozen_task['prompt'],
                'Caller task differs from frozen solver request')
    before_hashes = runner.file_hashes(run_dir / 'memory')
    _assert(before_hashes == runner.file_hashes(run_dir / 'memory_after_solver'), 'Original online memory changed')
    review_dir = run_dir / 'review'
    session, session_id, first_timestamp, last_timestamp = _session_state(review_dir)
    _assert(not (review_dir / 'execution.json').exists(), 'Reviewer execution was already finalized')
    _assert(not (review_dir / 'memory/final_decision.json').exists(), 'Reviewer already decided; inspect rather than repeat')
    _assert(not (review_dir / 'resume.json').exists(), 'A review resume attempt is already recorded; inspect it explicitly')

    mounts = (Mount(root / plan['pool_path'], '/pool'), Mount(review_dir / 'memory', '/memory', False),
              Mount(root / runner.VARIANT, '/opt/v5'), Mount(root / plan['rg_mount']['source'], '/usr/local/bin/rg'),
              Mount(run_dir / 'review-evidence', '/evidence'), Mount(evidence / 'workspace', '/evidence/workspace'))
    config = make_runtime_config(root, plan['solver_image_id'], support_mounts=mounts, warmup_override='')
    review_task = TaskSpec(row['task_id'], 'memory_review', plan['review_instructions'],
                          run_dir / 'review-inputs', run_dir / 'review-inputs',
                          timeout_seconds=plan['review_timeout_seconds'])
    runtime = TaskRuntime(config, review_task, review_dir, run_dir.name + '-review')
    info = json.loads(runtime.docker('inspect', runtime.name).stdout)[0]
    labels = info['Config'].get('Labels', {})
    _assert(labels.get(OWNER_LABEL) == PREFIX and labels.get('org.hangxiao.skill-dci.task') == row['task_id'],
            'Refuse unowned/wrong-task reviewer container')
    _assert(info['Name'] == '/' + runtime.name and info['Image'] == plan['solver_image_id'], 'Reviewer image/name changed')
    _assert(info['State']['Status'] == 'exited' and not info['State']['Running'], 'Reviewer must be confirmed stopped')
    _assert(info['HostConfig']['ReadonlyRootfs'] is True and info['HostConfig']['NetworkMode'] == 'none',
            'Reviewer runtime isolation changed')
    actual_mounts = {m['Destination']: m for m in info['Mounts']}
    expected_mounts = {m.target: (m.source.resolve(), m.readonly) for m in config.support_mounts}
    expected_mounts.update({
        '/tmp_workspace': (runtime.workspace, False), '/root/.codex': (runtime.codex_home, False),
        '/root/.cache': (review_dir / 'cache', False), '/root/.openclaw': (review_dir / 'openclaw-home', False),
        '/root/.pki': (review_dir / 'browser-pki', False),
    })
    _assert(set(actual_mounts) == set(expected_mounts) and len(actual_mounts) == len(info['Mounts']),
            'Reviewer has unexpected/missing/duplicate mounts')
    for destination, (source, readonly) in expected_mounts.items():
        actual = actual_mounts[destination]
        _assert(actual['Type'] == 'bind' and Path(actual['Source']).resolve() == source.resolve()
                and actual['RW'] is (not readonly), f'Reviewer mount source or mode differs: {destination}')
    public = run_dir / 'review-evidence'
    _assert({str(p.relative_to(public)) for p in public.rglob('*') if p.is_file() or p.is_symlink()}
            == {'task.txt', 'transcript.jsonl'}, 'Unexpected public reviewer evidence files')
    _assert(sha256(public / 'transcript.jsonl') == frozen['transcript_sha256']
            and (public / 'task.txt').read_text() == frozen_task['prompt'], 'Public reviewer evidence changed')
    observed_start = _time(info['State']['StartedAt'])
    observed_end = _time(info['State']['FinishedAt'])
    _assert(observed_start <= first_timestamp <= last_timestamp <= observed_end + 1,
            'Interrupted reviewer clock evidence inconsistent')
    consumed = observed_end - observed_start
    remaining = float(plan['review_timeout_seconds']) - consumed
    _assert(math.isfinite(remaining) and remaining > 0, 'Reviewer budget exhausted; no new model call permitted')
    old_session = session.read_bytes()
    old_usage = extract_usage(session, cumulative=True)
    immutable = {
        'protocol.json': sha256(run_dir / 'protocol.json'),
        'execution.json': sha256(run_dir / 'execution.json'),
        'transcript.openclaw.jsonl': sha256(run_dir / 'transcript.openclaw.jsonl'),
        'solver-evidence/manifest.json': sha256(evidence / 'manifest.json'),
    }

    # Lock and state changes begin only after all preflight checks above pass.
    lock = run_dir / '.review-resume.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    resume_record = {
        'schema_version': 1, 'status': 'starting', 'solver_invocations': 0,
        'task_id': row['task_id'], 'session_id': session_id, 'session_path': str(session.relative_to(root)),
        'session_prefix_sha256': hashlib.sha256(old_session).hexdigest(), 'session_prefix_bytes': len(old_session),
        'prior_recorded_usage': old_usage, 'original_container_id': info['Id'],
        'initial_review_first_timestamp': first_timestamp, 'interrupted_container_finished_at': observed_end,
        'interrupted_container_started_at': observed_start,
        'preinterrupt_session_observation_seconds': observed_end - first_timestamp,
        'preinterrupt_observed_active_seconds': consumed, 'remaining_review_budget_seconds': remaining,
        'timing_method': 'Original Docker StartedAt to FinishedAt (conservative setup-inclusive upper bound), plus resumed CLI monotonic elapsed; interruption gap excluded.',
        'source_hashes_before': immutable, 'memory_before_hashes': before_hashes,
        'actual_mounts': [{'target': target, 'source': str(source.relative_to(root)), 'readonly': readonly}
                          for target, (source, readonly) in sorted(expected_mounts.items())],
    }
    atomic_json(review_dir / 'resume.json', resume_record)
    proc = None
    completed = False
    resumed_elapsed = 0.0
    try:
        # Restart the inert container and its network bridge, never task warmup.
        runtime.docker('start', runtime.name)
        runtime.created = True
        runtime.state = 'prepared'
        runtime.run_startup_commands()
        backend = CodexBackend(runner.settings(row['task_id'], 'review', plan))
        backend.preflight(runtime)
        _assert(runtime.exec(['test', '-e', '/evidence/verifier'], check=False).returncode != 0
                and runtime.exec(['test', '-e', '/evidence/frozen_task.json'], check=False).returncode != 0,
                'Reviewer can access private evaluator material')
        prompt = ('Continue the interrupted final memory review under the existing instructions. '
                  'The solver artifacts remain frozen. Finish by calling distill with stage=final and '
                  'decision=write or no_update, then return briefly. Do not solve the task again or use grading feedback.')
        command = runtime.docker_command('exec', '--interactive', '--workdir', '/tmp_workspace', runtime.name,
            '/opt/codex/codex', 'exec', 'resume', '--json', '--skip-git-repo-check',
            '--dangerously-bypass-approvals-and-sandbox', '-m', plan['model'],
            '-c', 'model_reasoning_effort=' + json.dumps(plan['reasoning_effort']),
            '--output-last-message', '/tmp_workspace/.codex-last-message.txt', session_id, '-')
        (review_dir / 'resume_prompt.txt').write_text(prompt + '\n')
        started = time.monotonic()
        resume_record.update(status='resuming', resume_started_at_unix=time.time(),
                             excluded_interruption_gap_seconds=time.time() - observed_end)
        atomic_json(review_dir / 'resume.json', resume_record)
        status, exit_code, error = 'execution_error', None, None
        try:
            with (review_dir / 'codex.resume.events.jsonl').open('x') as events, (review_dir / 'codex.resume.stderr.log').open('x') as errors:
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors, text=True)
                try:
                    proc.communicate(input=prompt, timeout=remaining)
                    exit_code = proc.returncode
                    status = 'completed' if exit_code == 0 else 'execution_error'
                    error = None if exit_code == 0 else 'Resumed reviewer CLI returned nonzero; inspect private stderr.'
                except subprocess.TimeoutExpired:
                    status, error = 'timeout', 'Remaining review active-time budget exhausted'
        finally:
            resumed_elapsed = time.monotonic() - started
            try:
                runtime.freeze_agent()
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=10)
                    exit_code = proc.returncode
        actual_session, actual_id, _, _ = _session_state(review_dir)
        _assert(actual_session == session and actual_id == session_id, 'Resume changed reviewer session identity')
        _assert(session.read_bytes().startswith(old_session), 'Original reviewer session history was rewritten')
        transcript = review_dir / 'transcript.openclaw.jsonl'
        count = convert_transcript(session, transcript)
        usage = extract_usage(session, cumulative=True)
        elapsed = consumed + resumed_elapsed
        usage.update(elapsed_seconds=elapsed, cost_usd=None,
                     billing='ChatGPT subscription; API dollar cost unavailable')
        execution = ExecutionResult(status, exit_code, elapsed, transcript, usage, error)
        atomic_json(review_dir / 'execution.json', {
            'status': status, 'exit_code': exit_code, 'elapsed_seconds': elapsed,
            'model': plan['model'], 'reasoning_effort': plan['reasoning_effort'], 'cli_version': plan['cli_version'],
            'error': error, 'transcript_records': count, 'session_files': 1, 'usage': usage,
            'recovery': 'same-session resume after user interruption; inactive gap excluded',
        })
        atomic_json(review_dir / 'usage.json', usage)
        runner.sync_auth(runtime, root)
        model = runner.audit_model(review_dir, plan)
        memory = audit_memory(review_dir / 'memory')
        final_path = review_dir / 'memory/final_decision.json'
        decision = _load(final_path) if final_path.is_file() else None
        result = {
            'execution': {**asdict(execution), 'transcript_path': str(transcript.relative_to(root))},
            'model_audit': model, 'memory_audit': memory, 'before_hashes': before_hashes,
            'after_hashes': runner.file_hashes(review_dir / 'memory'), 'decision': decision,
            'used_hidden_grade': False, 'contributes_to_solver_score': False,
            'recovery': {'kind': 'same_session_resume', 'record': str((review_dir / 'resume.json').relative_to(root)),
                         'preinterrupt_active_seconds': consumed, 'resumed_active_seconds': resumed_elapsed,
                         'interruption_gap_excluded': True, 'solver_invocations': 0},
        }
        _assert(status == 'completed' and usage.get('total_tokens', 0) > 0, 'Reviewer did not complete within its remaining budget')
        _assert(decision and decision.get('task_id') == row['task_id'] and decision.get('stage') == 'final'
                and decision.get('agent_stage') == 'review' and memory['valid'], 'Missing or invalid final review decision')
        _assert(all(sha256(run_dir / path) == expected for path, expected in immutable.items()), 'Original solver evidence changed')
        load_evidence(run_dir)
        _assert(runner.file_hashes(run_dir / 'memory') == before_hashes, 'Original online memory changed while reviewing')
        atomic_json(run_dir / 'memory_review.json', result)
        (run_dir / 'memory').rename(run_dir / 'memory_before_review')
        shutil.copytree(review_dir / 'memory', run_dir / 'memory')
        completed = True
        resume_record.update(status='completed', resumed_active_seconds=resumed_elapsed,
                             total_active_seconds=elapsed, final_usage=usage,
                             source_hashes_unchanged=True, session_prefix_unchanged=True)
        return result
    except BaseException as error:
        resume_record.update(status='needs_review', error_type=type(error).__name__,
                             resumed_active_seconds=resumed_elapsed, solver_invocations=0)
        raise
    finally:
        resume_record['finished_at_unix'] = time.time()
        atomic_json(review_dir / 'resume.json', resume_record)
        if runtime.created:
            if completed:
                runtime.cleanup()
            elif runtime.state == 'prepared':
                runtime.freeze_agent()
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    result = resume_review(ROOT, ROOT / runner.RUNS / 'formal' / args.run_id)
    print(json.dumps({'task_id': result['decision']['task_id'], 'decision': result['decision']['decision'],
                      'total_review_tokens': result['execution']['usage']['total_tokens'],
                      'active_review_seconds': result['execution']['elapsed_seconds'], 'solver_invocations': 0}))
