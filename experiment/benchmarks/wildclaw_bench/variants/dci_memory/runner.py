"""V5: one solver rollout, task-local memory review, then original grading."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import tomllib

from experiment.shared.codex_backend import CodexBackend, CodexSettings, MCPServer, read_jsonl, select_main_session
from experiment.shared.task_runtime import Mount, TaskSpec, TaskRuntime, load_task_spec
from experiment.benchmarks.wildclaw_bench.src.runtime_support import make_runtime_config, copy_runtime_auth
from experiment.benchmarks.wildclaw_bench.src.experiment_protocol import atomic_json, sha256, workflow_config
from experiment.benchmarks.wildclaw_bench.src.inference_access import inference_client
from experiment.benchmarks.wildclaw_bench.src.mock_state import capture_mock_state, preflight_mock_state
from experiment.benchmarks.wildclaw_bench.scripts.evaluate_wildclaw import freeze_solver_evidence, inventory_tree
from experiment.benchmarks.wildclaw_bench.scripts.run_wildclaw import grading_setup, reviewed_warmup, restore_captured_mock, check_gateway_evaluation
from .pool import prepare_pool, validate_pool
from experiment.shared.memory.tools import initialize_memory, audit_memory
from .memory_runtime import MemoryTaskRuntime

ROOT = Path(__file__).resolve().parents[5]
VARIANT = Path('experiment/benchmarks/wildclaw_bench/variants/dci_memory')
RUNS = Path('experiment/benchmarks/wildclaw_bench/runs/dci_memory')
SCHEDULE = Path('experiment/benchmarks/wildclaw_bench/manifests/dci_memory_schedule.json')
VERSION = 'V5'
CONDITION = 'DCI_TASK_LOCAL_MEMORY'
MODEL = 'gpt-5.6-sol'
EFFORT = 'medium'


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hashes(directory):
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlink in memory: ' + str(path))
        if path.is_file():
            result[str(path.relative_to(directory))] = sha256(path)
    return result


def prepare(root=ROOT, *, write=True):
    old = workflow_config(root)
    path = root / VARIANT / 'prepared/formal/protocol.json'
    if path.exists() and json.loads(path.read_text()).get('workflow_revision') != old['workflow_revision']:
        raise ValueError('Historical V5 protocol is read-only; prepare a fresh checkout without saved runs/protocols')
    baseline = json.loads((root / 'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json').read_text())
    split = json.loads((root / 'experiment/benchmarks/wildclaw_bench/manifests/split.json').read_text())
    tasks = [{'index': i, 'task_id': r['task_id'], 'run_id': f'dci5-formal-{i:02d}'}
             for i, r in enumerate(r for r in baseline['schedule'] if r['condition'] == 'A0')]
    if len(tasks) != 24 or {t['task_id'] for t in tasks} != set(split['test_task_ids']):
        raise ValueError('Expected unchanged 24-test-task schedule')
    pool_path = root / VARIANT / 'prepared/pool'
    pool = prepare_pool(root, pool_path) if write else validate_pool(pool_path)
    source_files = [*sorted((root / VARIANT).glob('*.py')), root / 'experiment/shared/memory/prompt.txt',
                    root / 'experiment/shared/memory/review_prompt.txt', root / 'experiment/benchmarks/wildclaw_bench/manifests/split.json',
                    root / 'experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json',
                    root / 'experiment/benchmarks/wildclaw_bench/manifests/workflow_config.json',
                    root / 'experiment/benchmarks/wildclaw_bench/manifests/formal_protocol.json', root / old['rg_mount']['source'],
                    *sorted((root / 'experiment/benchmarks/wildclaw_bench/src').glob('*.py')), *sorted((root / 'experiment/shared').glob('*.py')), *sorted((root / 'experiment/shared/memory').glob('*.py')),
                    root / 'experiment/benchmarks/wildclaw_bench/scripts/run_wildclaw.py',
                    root / 'experiment/benchmarks/wildclaw_bench/scripts/evaluate_wildclaw.py']
    if sha256(root / old['rg_mount']['source']) != old['rg_mount']['sha256']:
        raise ValueError('Standard ripgrep binary differs from runtime recipe')
    protected = sorted((root / 'experiment/benchmarks/wildclaw_bench/runs/vanilla_sol/formal').glob('*/result.json'))
    plan = {
        'schema_version': 2, 'workflow_revision': old['workflow_revision'], 'version': VERSION, 'condition': CONDITION,
        'model': MODEL, 'reasoning_effort': EFFORT, 'judge_model': MODEL, 'judge_reasoning_effort': EFFORT,
        'cli_version': old['cli_version'], 'solver_image_id': old['solver_image_id'],
        'timeout_seconds': old['timeout_seconds'], 'grading_timeout_seconds': old['grading_timeout_seconds'],
        'review_timeout_seconds': 180,
        'condition_instructions': baseline['conditions']['A0'] + '\n' + (root / 'experiment/shared/memory/prompt.txt').read_text(),
        'review_instructions': (root / 'experiment/shared/memory/review_prompt.txt').read_text(),
        'tasks': tasks, 'pool_path': str(pool_path.relative_to(root)),
        'pool_manifest_sha256': sha256(pool_path / 'manifest.json'),
        'pool_sha256': pool['pool_sha256'], 'rg_mount': old['rg_mount'],
        'source_hashes': {str(p.relative_to(root)): sha256(p) for p in source_files},
        'historical_results': {str(p.relative_to(root)): sha256(p) for p in protected},
        'split': {'build': 36, 'test': 24, 'sha256': sha256(root / 'experiment/benchmarks/wildclaw_bench/manifests/split.json')},
        'memory_policy': {'initial_entries': 0, 'per_task': True, 'cross_task_transfer': False,
                          'old_distilled_skills_loaded': False, 'online_distillation': True,
                          'final_review_required': True, 'hidden_grade_feedback': False},
        'tools': ['reason', 'DCI_search_task', 'DCI_search_trajectory', 'DCI_search_memory', 'distill'],
        'tool_implementation': 'Domain MCP operations using agent-written local shell queries; native Codex terminal/file tools remain available.',
        'authentication': 'Existing isolated ChatGPT OAuth via Codex CLI; no token conversion to an API key.',
        'official_task_skills': 'preserved; distinct from the removed distilled experience skills',
        'budget': '1800s solver including online retrieval/distillation; separate 180s frozen-evidence final review; grader 1800s. Report costs separately and combined.',
        'retry_policy': 'One solver rollout per task. Preserve failures and valid low scores. Grading recovery reuses frozen evidence only.',
        'interpretation': 'V5 changes scored training filenames, two-stage retrieval instructions and task-local memory together; not a single-factor memory ablation.',
    }
    path = root / VARIANT / 'prepared/formal/protocol.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen V5 protocol differs from current sources; do not silently replace')
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            if any((root / RUNS / 'formal').glob('*')):
                raise ValueError('Existing V5 runs require their original saved protocol')
            atomic_json(path, plan)
        schedule = {'schema_version': 1, 'version': VERSION, 'condition': CONDITION, 'tasks': tasks}
        if (root / SCHEDULE).exists() and json.loads((root / SCHEDULE).read_text()) != schedule:
            raise ValueError('V5 schedule differs')
        atomic_json(root / SCHEDULE, schedule)
    elif not path.exists():
        raise ValueError('Run prepare before launching')
    return plan


def settings(task_id, stage, plan):
    server = MCPServer('v5', 'python3', ('/opt/v5/mcp_server.py', '--pool', '/pool',
                       '--memory', '/memory', '--task-id', task_id, '--stage', stage))
    return CodexSettings(model=plan['model'], reasoning_effort=plan['reasoning_effort'],
                         cli_version=plan['cli_version'], mcp_servers=(server,))


def sync_auth(runtime, root):
    """Preserve CLI refresh without logging credential content."""
    source, target = runtime.codex_home / 'auth.json', root / 'experiment/benchmarks/wildclaw_bench/runtime/codex_auth/auth.json'
    candidate, current = json.loads(source.read_text()), json.loads(target.read_text())
    if candidate.get('tokens', {}).get('account_id') == current.get('tokens', {}).get('account_id') and str(candidate.get('last_refresh', '')) > str(current.get('last_refresh', '')):
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)


def audit_model(run_dir, plan):
    source = select_main_session(list((run_dir / 'codex-home/sessions').rglob('*.jsonl')))
    models, efforts = set(), set()
    if source:
        for event in read_jsonl(source):
            if event.get('type') == 'turn_context':
                p = event['payload']
                models.add(p.get('model'))
                efforts.add(p.get('effort', p.get('reasoning_effort')))
    if models != {plan['model']} or efforts != {plan['reasoning_effort']}:
        raise ValueError('Unobserved or incorrect actual model/effort')
    config = tomllib.loads((run_dir / 'codex-home/config.toml').read_text())
    if set(config.get('mcp_servers', {})) != {'v5'} or config.get('features', {}).get('memories') is not False:
        raise ValueError('Unexpected MCP/global memory configuration')
    proof = {'models': sorted(models), 'efforts': sorted(efforts), 'mcp_servers': ['v5'],
             'global_codex_memories': False, 'session': str(source.relative_to(run_dir))}
    atomic_json(run_dir / 'model_audit.json', proof)
    return proof


def review_memory(root, run_dir, task, plan, gateway):
    """Review in a fresh container that cannot write solver artifacts or see gt."""
    public = run_dir / 'review-evidence'
    public.mkdir(exist_ok=True)
    # Nested read-only mounts need their mountpoint in the read-only parent.
    (public / 'workspace').mkdir(exist_ok=True)
    shutil.copyfile(run_dir / 'transcript.openclaw.jsonl', public / 'transcript.jsonl')
    (public / 'task.txt').write_text(task.prompt)
    inputs = run_dir / 'review-inputs'
    (inputs / 'exec').mkdir(parents=True)
    review_task = TaskSpec(task.task_id, 'memory_review', plan['review_instructions'],
                          inputs, inputs, timeout_seconds=plan['review_timeout_seconds'])
    review_dir = run_dir / 'review'
    review_dir.mkdir()
    review_memory_path = review_dir / 'memory'
    shutil.copytree(run_dir / 'memory', review_memory_path)
    mounts = (
        Mount(root / plan['pool_path'], '/pool'), Mount(review_memory_path, '/memory', False),
        Mount(root / 'experiment/shared/memory', '/opt/v5'), Mount(root / plan['rg_mount']['source'], '/usr/local/bin/rg'),
        Mount(public, '/evidence'), Mount(run_dir / 'solver-evidence/workspace', '/evidence/workspace'),
    )
    config = make_runtime_config(root, plan['solver_image_id'], support_mounts=mounts,
        warmup_override='', environment={'OPENROUTER_API_KEY': gateway.api_key,
                                        'OPENROUTER_BASE_URL': 'http://api.hangxiao.internal/v1'})
    runtime = TaskRuntime(config, review_task, review_dir, run_dir.name + '-review')
    backend = CodexBackend(settings(task.task_id, 'review', plan))
    before = file_hashes(run_dir / 'memory')
    # Final review must create its own decision, not inherit the solver's.
    final_path = review_memory_path / 'final_decision.json'
    if final_path.exists():
        shutil.copyfile(final_path, run_dir / 'solver_final_decision.json')
        final_path.unlink()
    try:
        backend.prepare(runtime)
        copy_runtime_auth(runtime)
        backend.preflight(runtime)
        inspect = json.loads(runtime.docker('inspect', runtime.name).stdout)[0]
        mounted = {m['Destination']: m for m in inspect['Mounts']}
        if any(mounted[p]['RW'] for p in ('/pool', '/evidence', '/evidence/workspace')) or not mounted['/memory']['RW']:
            raise ValueError('Review mount isolation failed')
        if runtime.exec(['test', '-e', '/evidence/verifier'], check=False).returncode == 0:
            raise ValueError('Review sees grader material')
        atomic_json(review_dir / 'isolation.json', {'pool_readonly': True, 'evidence_readonly': True,
            'memory_is_task_local': True, 'hidden_verifier_absent': True,
            'mounts': [{'target': k, 'rw': v['RW']} for k, v in mounted.items()]})
        execution = backend.run(runtime, verify_login=False)
        sync_auth(runtime, root)
        audit = audit_model(review_dir, plan)
        memory = audit_memory(review_memory_path)
        result = {'execution': {**asdict(execution), 'transcript_path': str(execution.transcript_path.relative_to(root))},
                  'model_audit': audit, 'memory_audit': memory,
                  'before_hashes': before, 'after_hashes': file_hashes(review_memory_path),
                  'decision': json.loads(final_path.read_text()) if final_path.is_file() else None,
                  'used_hidden_grade': False, 'contributes_to_solver_score': False}
        atomic_json(run_dir / 'memory_review.json', result)
        if not result['decision'] or result['decision'].get('stage') != 'final':
            raise ValueError('Reviewer did not make the required explicit distill decision')
        if execution.status != 'completed' or not execution.usage.get('total_tokens'):
            raise ValueError('Reviewer did not complete with recorded model usage')
        if result['decision'].get('task_id') != task.task_id or result['decision'].get('agent_stage') != 'review':
            raise ValueError('Final review decision provenance mismatch')
        if not memory.get('valid'):
            raise ValueError('Reviewed memory failed structure/evidence audit')
        # Preserve both snapshots, and publish only this task's reviewed memory.
        (run_dir / 'memory').rename(run_dir / 'memory_before_review')
        shutil.copytree(review_memory_path, run_dir / 'memory')
        return result
    finally:
        runtime.cleanup()


def run_one(row, root=ROOT):
    plan = json.loads((root / VARIANT / 'prepared/formal/protocol.json').read_text())
    if row not in plan['tasks']:
        raise ValueError('Task outside frozen schedule')
    if prepare(root, write=False) != plan:
        raise ValueError('V5 inputs changed')
    run_dir = root / RUNS / 'formal' / row['run_id']
    run_dir.mkdir(parents=True, exist_ok=False)
    memory = run_dir / 'memory'
    initialize_memory(memory, row['task_id'])
    initial = file_hashes(memory)
    atomic_json(run_dir / 'memory_initial.json', {'task_id': row['task_id'], 'entry_count': 0, 'files': initial})
    atomic_json(run_dir / 'protocol.json', plan)
    manifest = json.loads((root / 'experiment/benchmarks/wildclaw_bench/manifests/task_manifest.json').read_text())
    metadata = next(m for m in manifest['tasks'] if m['task_id'] == row['task_id'])
    repo = root / 'experiment/benchmarks/wildclaw_bench/vendor/WildClawBench'
    task_file = repo / metadata['source_path']
    if sha256(task_file) != metadata['source_sha256']:
        raise ValueError('Task source changed')
    task = load_task_spec(task_file, repo, workspace_root=root / 'experiment/benchmarks/wildclaw_bench/runtime/task_inputs', timeout_seconds=plan['timeout_seconds'])
    task = replace(task, skills_root=root / 'experiment/benchmarks/wildclaw_bench/runtime/skills')
    record = {**row, 'version': VERSION, 'condition': CONDITION, 'status': 'starting', 'started_at': time.time(),
              'protocol_sha256': sha256(run_dir / 'protocol.json')}
    runtime = None
    try:
        with inference_client(root, row['run_id']) as gateway:
            record['gateway_authorization_hash'] = gateway.authorization_sha256
            mounts = (Mount(root / plan['pool_path'], '/pool'), Mount(memory, '/memory', False),
                      Mount(root / 'experiment/shared/memory', '/opt/v5'), Mount(root / plan['rg_mount']['source'], '/usr/local/bin/rg'))
            config = make_runtime_config(root, plan['solver_image_id'], support_mounts=mounts,
                grading_timeout_seconds=plan['grading_timeout_seconds'], warmup_override=reviewed_warmup(task),
                before_freeze_hook=capture_mock_state, after_restart_hook=restore_captured_mock,
                environment={'OPENROUTER_API_KEY': gateway.api_key, 'OPENROUTER_BASE_URL': 'http://api.hangxiao.internal/v1'})
            runtime = MemoryTaskRuntime(config, task, run_dir, row['run_id'],
                pool_manifest_sha256=plan['pool_manifest_sha256'], memory_initial_sha256=digest_json(initial))
            backend = CodexBackend(settings(task.task_id, 'solve', plan))
            backend.prepare(runtime)
            record['mock_preflight'] = preflight_mock_state(runtime)
            copy_runtime_auth(runtime)
            record['preflight'] = backend.preflight(runtime)
            record['status'] = 'solving'
            atomic_json(run_dir / 'run.json', record)
            execution = backend.run(runtime, condition_instructions=plan['condition_instructions'], verify_login=False)
            record['execution'] = {**asdict(execution), 'transcript_path': str(execution.transcript_path.relative_to(root))}
            if execution.status not in {'completed', 'timeout'} or not execution.usage.get('total_tokens'):
                raise RuntimeError('No valid solver execution; preserve evidence without retrying')
            sync_auth(runtime, root)
            record['solver_audit'] = audit_model(run_dir, plan)
            evidence = freeze_solver_evidence(runtime, transcript_path=execution.transcript_path,
                transcript_loader=repo / 'src/utils/transcript_loader.py', task_source=task_file, source_sha256=metadata['source_sha256'])
            record['solver_evidence'] = str(evidence.relative_to(root))
            shutil.copytree(memory, run_dir / 'memory_after_solver')
            record['online_memory'] = audit_memory(memory)
            record['status'] = 'reviewing_memory'
            atomic_json(run_dir / 'run.json', record)
            record['memory_review'] = review_memory(root, run_dir, task, plan, gateway)
            record['final_memory'] = audit_memory(memory)
            record['status'] = 'grading'
            atomic_json(run_dir / 'run.json', record)
            grade_started = time.time()
            record['evaluation'] = runtime.grade(transcript_path=execution.transcript_path,
                transcript_loader=repo / 'src/utils/transcript_loader.py', grading_setup=grading_setup(task))
            check_gateway_evaluation(record, run_dir, grade_started)
            record['status'] = 'finished'
    except Exception as error:
        record['status'] = 'infrastructure_error'
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        if runtime:
            try:
                runtime.cleanup()
            except Exception as error:
                record['cleanup_error'] = type(error).__name__
        native = run_dir / 'native_environment.json'
        if native.exists():
            record['native_environment_sha256'] = sha256(native)
        record['finished_at'] = time.time()
        atomic_json(run_dir / 'result.json', record)
        atomic_json(run_dir / 'run.json', record)
    return record


def audit_result(row, root=ROOT):
    run_dir = root / RUNS / 'formal' / row['run_id']
    result = json.loads((run_dir / 'result.json').read_text())
    if result.get('status') != 'finished' or result.get('task_id') != row['task_id']:
        raise ValueError('Incomplete V5 run; inspect frozen evidence, never repeat solver')
    score = result.get('evaluation', {}).get('overall_score')
    if result['evaluation'].get('status') != 'graded' or isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
        raise ValueError('Grading requires infrastructure recovery')
    proof = json.loads((run_dir / 'native_environment.json').read_text())
    if not proof.get('grader', {}).get('resources_absent'):
        raise ValueError('Grader resource isolation missing')
    decision = result.get('memory_review', {}).get('decision')
    if not decision or decision.get('stage') != 'final' or decision.get('task_id') != row['task_id'] or decision.get('agent_stage') != 'review':
        raise ValueError('Final memory decision missing')
    review_execution = result['memory_review']['execution']
    if review_execution.get('status') != 'completed' or not review_execution.get('usage', {}).get('total_tokens'):
        raise ValueError('Final reviewer did not complete')
    if not audit_memory(run_dir / 'memory')['valid']:
        raise ValueError('Final memory failed validation')
    if sha256(run_dir / 'native_environment.json') != result.get('native_environment_sha256'):
        raise ValueError('Native environment evidence changed')
    events = list(read_jsonl(run_dir / 'memory_after_solver/events.jsonl'))
    ordered = [e.get('tool') for e in events if e.get('tool') != 'tool_error']
    follows_initial = bool(ordered and ordered[0] == 'reason' and 'DCI_search_task' in ordered
                           and ordered.index('DCI_search_task') < min(
                               [ordered.index(t) for t in ('DCI_search_trajectory', 'distill') if t in ordered] or [len(ordered)]))
    online_writes = [e for e in events if e.get('tool') == 'distill' and e.get('stage') == 'online' and e.get('decision') == 'write']
    first_write = next((i for i, e in enumerate(events) if e in online_writes), None)
    read_after_write = first_write is not None and any(e.get('tool') == 'DCI_search_memory' for e in events[first_write + 1:])
    workflow = {'initial_reason_then_task_search': follows_initial,
                'online_write_decisions': len(online_writes), 'memory_search_after_online_write': read_after_write,
                'trajectory_search_calls': ordered.count('DCI_search_trajectory'),
                'memory_search_calls': ordered.count('DCI_search_memory'),
                'scope': 'Recorded domain operations, not proof of semantic adoption or causal benefit'}
    return {'run_id': row['run_id'], 'task_id': row['task_id'], 'overall_score': score,
            'solver_tokens': result['execution']['usage']['total_tokens'],
            'solver_seconds': result['execution']['elapsed_seconds'],
            'review_tokens': result['memory_review']['execution']['usage'].get('total_tokens', 0),
            'review_seconds': result['memory_review']['execution']['elapsed_seconds'],
            'final_decision': decision['decision'], 'workflow': workflow,
            'result_sha256': sha256(run_dir / 'result.json')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('prepare', 'run'))
    parser.add_argument('--index', type=int)
    args = parser.parse_args()
    if args.command == 'prepare':
        plan = prepare()
        print(json.dumps({'version': VERSION, 'tasks': len(plan['tasks']), 'pool_sha256': plan['pool_sha256']}))
    else:
        if args.index is None:
            parser.error('--index required')
        plan = json.loads((ROOT / VARIANT / 'prepared/formal/protocol.json').read_text())
        run_one(plan['tasks'][args.index])


if __name__ == '__main__':
    main()
