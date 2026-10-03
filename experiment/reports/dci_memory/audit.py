"""Read-only V5 requirement audit; write only this report's final_audit.json.

No models, grading, solver reruns, or repairs are invoked. Missing evidence
cannot count as completion. Proof scopes are explicit: structural isolation
and recorded operations do not establish semantic adoption or causal benefit.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from experiment.src.codex_backend import extract_usage, read_jsonl
from experiment.src.experiment_protocol import atomic_json, sha256
from experiment.scripts.evaluate_wildclaw import inventory_tree
from experiment.variants.dci_memory.pool import _plan, validate_pool
from experiment.variants.dci_memory.tools import audit_memory

VARIANT = Path('experiment/variants/dci_memory')
RUNS = Path('experiment/runs/dci_memory/formal')


def load(path):
    return json.loads(path.read_text())


def check(rows, requirement, ok, evidence=None, proof='saved_artifacts'):
    rows.append({'requirement': requirement, 'passed': bool(ok), 'proof_type': proof, 'evidence': evidence})


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens', 'total_tokens')


def nonresumed_usage(session, usage):
    """Prove request totals, including a response recorded after the last counter.

    A timed-out tool can prevent the CLI from emitting its next token_count.
    Each unique response has independent usage and thread counters. Require
    agreement at every response and at the last counter's actual file prefix;
    then account for all later response records exactly once.
    """
    data = session.read_bytes()
    total = dict.fromkeys(USAGE_FIELDS, 0)
    responses, records, counters = set(), [], []
    byte_offset = 0

    def fields(value, label):
        if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0 for k in USAGE_FIELDS):
            raise ValueError(f'{label}: missing or invalid token field')
        answer = {key: value[key] for key in USAGE_FIELDS}
        if answer['input_tokens'] + answer['output_tokens'] != answer['total_tokens']:
            raise ValueError(f'{label}: total differs from input plus output')
        return answer

    for line_number, line in enumerate(data.splitlines(keepends=True), 1):
        byte_offset += len(line)
        if not line.strip():
            continue
        event = json.loads(line)
        payload = event.get('payload', {})
        if event.get('type') == 'token_usage_record':
            response_id = payload.get('response_id')
            if not response_id or response_id in responses:
                raise ValueError('Missing or duplicate response ID in non-resumed usage')
            responses.add(response_id)
            request_usage = fields(payload.get('usage'), 'Request usage')
            for key in USAGE_FIELDS:
                total[key] += request_usage[key]
            thread = fields(payload.get('thread_token_usage'), 'Thread usage')
            if thread != total:
                raise ValueError('Response prefix sum differs from its thread_token_usage')
            records.append({'response_id': response_id, 'line': line_number, 'usage': request_usage})
        elif event.get('type') == 'event_msg' and payload.get('type') == 'token_count':
            counter = fields((payload.get('info') or {}).get('total_token_usage'), 'Token counter')
            counters.append({'line': line_number, 'bytes': byte_offset, 'usage': counter,
                             'prefix_sum': dict(total), 'response_count': len(records)})
    if not records or not counters:
        raise ValueError('Non-resumed usage lacks independent response records or token counters')
    last = counters[-1]
    if last['usage'] != last['prefix_sum']:
        raise ValueError('Last token_count is not proven by the preceding response-record prefix')
    if any(usage.get(key) != last['usage'][key] for key in USAGE_FIELDS):
        raise ValueError('Extracted token counter differs from the verified final counter')
    tail_records = records[last['response_count']:]
    tail = {key: sum(record['usage'][key] for record in tail_records) for key in USAGE_FIELDS}
    if any(last['usage'][key] + tail[key] != total[key] for key in USAGE_FIELDS):
        raise ValueError('Counter prefix plus trailing response usage does not match full thread')
    return {**usage, **total, 'usage_available': True}, {
        'method': 'verified_unique_response_sums_final_thread_and_last_counter_prefix',
        'unique_response_ids': len(records), 'final_thread_counter_matches': True,
        'all_response_thread_prefixes_match': True,
        'last_counter_line': last['line'], 'last_counter_usage': last['usage'],
        'counter_prefix_response_count': last['response_count'],
        'counter_prefix_bytes': last['bytes'],
        'counter_prefix_sha256': hashlib.sha256(data[:last['bytes']]).hexdigest(),
        'tail_sha256': hashlib.sha256(data[last['bytes']:]).hexdigest(),
        'trailing_response_records': tail_records, 'trailing_usage': tail,
        'trailing_counter_lag': bool(tail_records), 'full_session_usage': total,
        'source_session_sha256': hashlib.sha256(data).hexdigest(),
    }


def observed_usage(session, directory):
    """Verify full usage for ordinary, trailing-counter and resumed sessions.

    The resumed session remains one file. Its original byte prefix, independent
    request usage records and final thread_token_usage must all agree; merely
    trusting an edited execution total is insufficient.
    """
    usage = extract_usage(session, cumulative=True)
    resume_path = directory / 'resume.json'
    if not resume_path.exists():
        return nonresumed_usage(session, usage)
    resume = load(resume_path)
    data = session.read_bytes()
    boundary = resume['session_prefix_bytes']
    if (resume.get('status') != 'completed' or not 0 < boundary < len(data)
            or hashlib.sha256(data[:boundary]).hexdigest() != resume['session_prefix_sha256']):
        raise ValueError('Resumed session original prefix or completion proof changed')
    segments = [[json.loads(line) for line in chunk.splitlines() if line.strip()]
                for chunk in (data[:boundary], data[boundary:])]
    fields = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens', 'total_tokens')
    totals, all_responses, final_thread, event_count = [], set(), None, 0
    for rows in segments:
        counters = [r['payload']['info']['total_token_usage'] for r in rows
                    if r.get('payload', {}).get('type') == 'token_count']
        records = [r['payload'] for r in rows if r.get('type') == 'token_usage_record']
        if not counters or not records:
            raise ValueError('Resume usage segment lacks independently checkable counters')
        segment = {k: 0 for k in fields}
        for record in records:
            response = record.get('response_id')
            if not response or response in all_responses:
                raise ValueError('Missing or duplicate response identity in resumed usage records')
            all_responses.add(response)
            for key in fields:
                value = record['usage'].get(key)
                if type(value) is not int or value < 0:
                    raise ValueError('Invalid request usage value in resumed session')
                segment[key] += value
            final_thread = record.get('thread_token_usage')
        if any(segment[k] != counters[-1].get(k) for k in fields):
            raise ValueError('Resume segment request sums disagree with its cumulative counter')
        totals.append(segment)
        event_count += len(counters)
    combined = {key: sum(segment[key] for segment in totals) for key in fields}
    if any(resume['prior_recorded_usage'].get(k) != totals[0][k] for k in fields):
        raise ValueError('Preserved interrupted reviewer usage differs from raw prefix')
    if not final_thread or any(final_thread.get(k) != combined[k] for k in fields):
        raise ValueError('Full reviewer usage disagrees with independent final thread counter')
    return {**usage, **combined, 'usage_events': event_count}, {
        'method': 'verified_original_prefix_plus_resumed_segment_request_sums_and_final_thread_token_usage',
        'original_prefix_sha256': resume['session_prefix_sha256'], 'original_prefix_bytes': boundary,
        'preinterrupt_usage': totals[0], 'resumed_usage': totals[1], 'full_session_usage': combined,
        'unique_response_ids': len(all_responses), 'final_thread_counter_matches': True,
    }


def raw_session(directory, model, effort, stage, task_id):
    paths = sorted((directory / 'codex-home/sessions').rglob('*.jsonl'))
    # One main session is required, not merely selection of the largest one.
    if len(paths) != 1:
        raise ValueError(f'{stage}: expected one recorded session, found {len(paths)}')
    models, efforts, timestamps, ids = set(), set(), [], set()
    tool_names, native_actions = [], []
    for row in read_jsonl(paths[0]):
        if row.get('timestamp'):
            timestamps.append(timestamp(row['timestamp']))
        payload = row.get('payload', {})
        if (row.get('type') == 'event_msg' and payload.get('type') in ('item_started', 'item_completed')
                and payload.get('item', {}).get('type') in ('CommandExecution', 'FileChange')
                and isinstance(payload.get('started_at_ms'), (int, float))):
            native_actions.append(payload['started_at_ms'] / 1000)
        if row.get('type') == 'session_meta':
            ids.add(payload.get('id', payload.get('session_id')))
        if row.get('type') == 'turn_context':
            models.add(payload.get('model'))
            efforts.add(payload.get('effort', payload.get('reasoning_effort')))
        if row.get('type') == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call'):
            rendered = json.dumps(payload, ensure_ascii=False)
            for name in ('reason', 'DCI_search_task', 'DCI_search_trajectory', 'DCI_search_memory', 'distill'):
                if ('mcp__v5__' + name) in rendered:
                    tool_names.append(name)
    config = tomllib.loads((directory / 'codex-home/config.toml').read_text())
    server = config.get('mcp_servers', {}).get('v5', {})
    args = server.get('args', [])
    required_args = ['--task-id', task_id, '--stage', stage]
    usage, usage_proof = observed_usage(paths[0], directory)
    return {
        'valid': models == {model} and efforts == {effort} and len(ids) == 1
                 and config.get('features', {}).get('memories') is False
                 and set(config.get('mcp_servers', {})) == {'v5'}
                 and all(any(args[i:i + 2] == required_args[j:j + 2] for i in range(len(args) - 1))
                         for j in (0, 2)),
        'path': str(paths[0].relative_to(ROOT)), 'sha256': sha256(paths[0]),
        'session_ids': sorted(ids), 'session_count': len(paths),
        'models': sorted(models), 'efforts': sorted(efforts),
        'first_timestamp_unix': min(timestamps), 'last_timestamp_unix': max(timestamps),
        'observed_domain_tool_calls': dict(Counter(tool_names)),
        'first_native_terminal_or_patch_timestamp': datetime.fromtimestamp(min(native_actions), timezone.utc).isoformat() if native_actions else None,
        'native_timing_source': 'Internal CommandExecution/FileChange started_at_ms; outer exec timestamps do not imply native command start.',
        'usage': usage, 'usage_proof': usage_proof,
    }


def stage_metrics(execution, raw, acceptable_statuses):
    usage = execution.get('usage', {})
    duration = execution.get('elapsed_seconds')
    return (execution.get('status') in acceptable_statuses
            and isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0
            and usage.get('usage_available') is True
            and isinstance(usage.get('total_tokens'), int) and usage['total_tokens'] > 0
            and usage.get('input_tokens', 0) + usage.get('output_tokens', 0) == usage['total_tokens']
            and all(usage.get(k) == raw['usage'].get(k) for k in USAGE_FIELDS))


def grader_model_evidence(root, record, plan, grading_start):
    """Join actual grader requests to their raw model/effort evidence.

    Solver auxiliary requests use the same lease, so only requests after the
    hidden grader program was created and before the run finished are included.
    No rubric or model response contents are copied into this report.
    """
    event_path = root / 'experiment/runtime/inference/events.jsonl'
    if not event_path.is_file():
        raise ValueError('Gateway request ledger is missing')
    authorization = record['gateway_authorization_hash']
    events = [e for e in read_jsonl(event_path)
              if e.get('authorization_sha256') == authorization
              and grading_start - .1 <= e.get('started_at_unix', -1) <= record['finished_at'] + .1]
    grouped = {}
    for event in events:
        request_id = event.get('request_id')
        if not request_id or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
            raise ValueError('Grading gateway event has no valid request identity')
        grouped.setdefault(request_id, []).append(event)
    calls = []
    for request_id, request_events in grouped.items():
        directory = event_path.parent / request_id
        manifest = load(directory / 'manifest.json')
        execution = load(directory / 'run/execution.json')
        session_paths = sorted((directory / 'run/codex-home/sessions').rglob('*.jsonl'))
        models, efforts = set(), set()
        for path in session_paths:
            for row in read_jsonl(path):
                if row.get('type') == 'turn_context':
                    payload = row['payload']
                    models.add(payload.get('model'))
                    efforts.add(payload.get('effort', payload.get('reasoning_effort')))
        statuses = Counter(e.get('status') for e in request_events)
        expected_model, expected_effort = plan['judge_model'], plan['judge_reasoning_effort']
        request_hash = sha256(directory / 'request.json')
        valid = (statuses == {'started': 1, 'completed': 1}
                 and all(e.get('actual_model') == expected_model and e.get('request_sha256') == request_hash
                         for e in request_events)
                 and manifest.get('request_id') == request_id and manifest.get('request_sha256') == request_hash
                 and manifest.get('request_context', {}).get('authorization_sha256') == authorization
                 and manifest.get('actual_model') == expected_model
                 and manifest.get('reasoning_effort') == expected_effort
                 and manifest.get('cli_version') == plan['cli_version']
                 and execution.get('status') == 'completed' and execution.get('model') == expected_model
                 and execution.get('reasoning_effort') == expected_effort
                 and len(session_paths) == 1 and models == {expected_model} and efforts == {expected_effort})
        calls.append({'request_id': request_id, 'valid': valid, 'statuses': dict(statuses),
                      'requested_model': manifest.get('requested_model'),
                      'actual_model': manifest.get('actual_model'), 'reasoning_effort': manifest.get('reasoning_effort'),
                      'raw_models': sorted(models), 'raw_efforts': sorted(efforts),
                      'manifest_sha256': sha256(directory / 'manifest.json'),
                      'execution_sha256': sha256(directory / 'run/execution.json'),
                      'session_hashes': {str(p.relative_to(root)): sha256(p) for p in session_paths}})
    return {'valid': all(c['valid'] for c in calls), 'grading_calls': len(calls), 'calls': calls,
            'scope': 'Actual gateway calls associated with this run and grading window; zero means no recorded LLM grading call.',
            'window': {'grader_program_created_unix': grading_start, 'run_finished_unix': record['finished_at']},
            'selected_event_sha256': hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()}


def audit_run(root, row, plan, protocol_hash):
    directory = root / RUNS / row['run_id']
    checks = []
    result = {'run_id': row['run_id'], 'task_id': row['task_id'], 'checks': checks, 'status': 'incomplete'}
    if not (directory / 'result.json').is_file():
        progress = load(directory / 'run.json') if (directory / 'run.json').is_file() else {}
        result['observed_stage'] = progress.get('status', 'not_started')
        return result
    try:
        record = load(directory / 'result.json')
        check(checks, 'finished_matching_scheduled_run', record.get('status') == 'finished'
              and record.get('task_id') == row['task_id'] and record.get('run_id') == row['run_id'],
              {'status': record.get('status'), 'result_sha256': sha256(directory / 'result.json')})
        if record.get('status') != 'finished':
            result['observed_stage'] = record.get('status')
            return result
        check(checks, 'frozen_protocol_unchanged', sha256(directory / 'protocol.json') == protocol_hash
              and record['protocol_sha256'] == protocol_hash)
        solve = raw_session(directory, plan['model'], plan['reasoning_effort'], 'solve', row['task_id'])
        review = raw_session(directory / 'review', plan['model'], plan['reasoning_effort'], 'review', row['task_id'])
        check(checks, 'one_recorded_solver_rollout_correct_model_effort', solve['valid'], solve, 'raw_session_and_config')
        check(checks, 'one_recorded_reviewer_correct_model_effort', review['valid'], review, 'raw_session_and_config')
        check(checks, 'solver_recorded_time_tokens', stage_metrics(record['execution'], solve, {'completed', 'timeout'}),
              {'seconds': record['execution'].get('elapsed_seconds'), 'tokens': record['execution'].get('usage', {}).get('total_tokens')})
        check(checks, 'review_recorded_time_tokens', stage_metrics(record['memory_review']['execution'], review, {'completed'}),
              {'seconds': record['memory_review']['execution'].get('elapsed_seconds'), 'tokens': record['memory_review']['execution'].get('usage', {}).get('total_tokens')})
        if (directory / 'review/resume.json').exists():
            resumed = load(directory / 'review/resume.json')
            active = resumed['preinterrupt_observed_active_seconds'] + resumed['resumed_active_seconds']
            check(checks, 'resumed_review_preserves_session_and_budget', resumed['solver_invocations'] == 0
                  and resumed['session_id'] in review['session_ids']
                  and resumed.get('session_prefix_unchanged') is True and resumed.get('source_hashes_unchanged') is True
                  and 0 < active <= plan['review_timeout_seconds'] + .1
                  and abs(active - record['memory_review']['execution']['elapsed_seconds']) < .001
                  and review['usage_proof']['final_thread_counter_matches'] is True,
                  {'prior_active_seconds': resumed['preinterrupt_observed_active_seconds'],
                   'resumed_active_seconds': resumed['resumed_active_seconds'], 'active_total_seconds': active,
                   'interruption_gap_seconds_excluded': resumed.get('excluded_interruption_gap_seconds'),
                   'full_usage': review['usage_proof']}, 'same_session_prefix_and_independent_request_accounting')
        initial = load(directory / 'memory_initial.json')
        identity = load(directory / 'memory/identity.json')
        check(checks, 'initial_memory_empty_and_current_task', initial.get('entry_count') == 0
              and initial.get('task_id') == row['task_id']
              and not any(p.startswith('entries/') for p in initial['files'])
              and initial['files'].get('.entry_metadata.json') == hashlib.sha256(b'{}\n').hexdigest()
              and initial['files'].get('identity.json') == sha256(directory / 'memory/identity.json')
              and identity.get('task_id') == row['task_id'] and identity.get('inherited_entries') == 0,
              {'task_id': identity.get('task_id'), 'initial_entry_count': initial.get('entry_count')})
        snapshots = {name: audit_memory(directory / name)
                     for name in ('memory_after_solver', 'memory_before_review', 'memory', 'review/memory')}
        check(checks, 'all_memory_snapshots_task_scoped_and_valid', all(m['valid'] and m['task_id'] == row['task_id']
              and m['scope'] == 'single_test_task' for m in snapshots.values()),
              {name: {k: value[k] for k in ('valid', 'task_id', 'entry_count', 'issues')} for name, value in snapshots.items()})
        check(checks, 'memory_review_uses_only_current_solver_snapshot',
              snapshots['memory_after_solver']['files_sha256'] == snapshots['memory_before_review']['files_sha256']
              and snapshots['memory_before_review']['files_sha256'] == record['memory_review']['before_hashes']
              and snapshots['memory']['files_sha256'] == snapshots['review/memory']['files_sha256']
              and snapshots['memory']['files_sha256'] == record['memory_review']['after_hashes'],
              'Online snapshot matches reviewer input; only current reviewer output becomes this task final memory.',
              'complete_memory_snapshot_hash_comparison')
        native = load(directory / 'native_environment.json')
        solver = native['solver']
        expected_memory = str((directory / 'memory').relative_to(root))
        check(checks, 'actual_solver_mount_is_current_task_memory', solver['memory_mount']['source'] == expected_memory
              and solver['memory_mount']['read_only'] is False
              and solver['pool_mount']['source'] == plan['pool_path'] and solver['pool_mount']['read_only'] is True
              and solver.get('memory_shared_between_tasks') is False and solver.get('memory_task_scoped') is True,
              {'memory_mount': solver['memory_mount'], 'pool_mount': solver['pool_mount']}, 'runtime_docker_mount_observation')
        check(checks, 'grader_resources_removed', native.get('grader', {}).get('resources_absent') is True
              and native['grader'].get('solver_stopped_before_recreation') is True
              and native['grader'].get('same_nonresource_configuration') is True
              and sha256(directory / 'native_environment.json') == record.get('native_environment_sha256'),
              native.get('grader'), 'runtime_docker_mount_observation')
        isolation = load(directory / 'review/isolation.json')
        mounts = {m['target']: m['rw'] for m in isolation['mounts']}
        expected_targets = {'/opt/codex', '/opt/network', '/opt/network-ca.pem', '/opt/network_bridge.py',
                            '/opt/nss-tools', '/opt/proxy-ca.pem', '/pool', '/memory', '/opt/v5', '/usr/local/bin/rg',
                            '/evidence', '/evidence/workspace', '/tmp_workspace', '/root/.codex', '/root/.cache',
                            '/root/.openclaw', '/root/.pki'}
        public = directory / 'review-evidence'
        public_paths = {str(p.relative_to(public)) for p in public.rglob('*') if p.is_file() or p.is_symlink()}
        review_source = (root / VARIANT / 'runner.py').read_text()
        source_matches = sha256(root / VARIANT / 'runner.py') == plan['source_hashes'][str(VARIANT / 'runner.py')]
        check(checks, 'review_hidden_inputs_absent_and_current_task_memory', set(mounts) == expected_targets
              and mounts.get('/memory') is True and all(mounts.get(k) is False for k in ('/pool', '/evidence', '/evidence/workspace'))
              and public_paths == {'task.txt', 'transcript.jsonl'}
              and not any(p.is_symlink() for p in public.rglob('*'))
              and sha256(public / 'transcript.jsonl') == sha256(directory / 'transcript.openclaw.jsonl')
              and (public / 'task.txt').read_text() == load(directory / 'solver-evidence/frozen_task.json')['prompt']
              and isolation.get('hidden_verifier_absent') is True and source_matches
              and "review_memory_path = review_dir / 'memory'" in review_source
              and "Mount(review_memory_path, '/memory', False)" in review_source
              and "Mount(run_dir / 'solver-evidence/workspace', '/evidence/workspace')" in review_source,
              {'observed_mount_modes': mounts, 'public_evidence_files': sorted(public_paths),
               'source_mapping': 'Frozen runner constructs review/memory and only public task/transcript + frozen workspace mounts.',
               'limitation': 'Review source paths were not persisted by Docker inspection; sources are proven by frozen code and task-local artifacts, targets/modes by runtime inspection.'},
              'runtime_destinations_and_modes_plus_frozen_code_and_file_allowlist')
        review_record = load(directory / 'memory_review.json')
        decision = load(directory / 'memory/final_decision.json')
        final_events = [e for e in read_jsonl(directory / 'memory/events.jsonl') if e.get('event_id') == decision.get('event_id')]
        check(checks, 'explicit_final_reviewer_distill_decision', decision == review_record['decision']
              and decision == record['memory_review']['decision'] and decision.get('task_id') == row['task_id']
              and decision.get('stage') == 'final' and decision.get('agent_stage') == 'review'
              and decision.get('decision') in {'write', 'no_update'} and len(final_events) == 1
              and final_events[0].get('tool') == 'distill' and review['observed_domain_tool_calls'].get('distill', 0) > 0,
              {'decision': decision.get('decision'), 'event_id': decision.get('event_id'), 'timestamp': decision.get('timestamp')},
              'memory_event_and_raw_reviewer_tool_call')
        frozen = load(directory / 'solver-evidence/manifest.json')
        check(checks, 'frozen_solver_evidence_unchanged', frozen['status'] == 'frozen'
              and frozen['task_id'] == row['task_id']
              and frozen['workspace'] == inventory_tree(directory / 'solver-evidence/workspace')
              and frozen['transcript_sha256'] == sha256(directory / 'transcript.openclaw.jsonl')
              and frozen['transcript_sha256'] == sha256(directory / 'solver-evidence/transcript.openclaw.jsonl'),
              {'frozen_at_unix': frozen['frozen_at_unix'], 'transcript_sha256': frozen['transcript_sha256']})
        review_removed = load(directory / 'review/runtime_state.json')['updated_at_unix']
        grader_created = (directory / 'controller-staging/grade_runner.py').stat().st_mtime
        evaluation_written = (directory / 'evaluation.json').stat().st_mtime
        chronology = {'run_started': record['started_at'], 'solver_first': solve['first_timestamp_unix'],
                      'solver_last': solve['last_timestamp_unix'], 'frozen_at': frozen['frozen_at_unix'],
                      'review_first': review['first_timestamp_unix'], 'final_decision': timestamp(decision['timestamp']),
                      'review_last': review['last_timestamp_unix'], 'review_cleanup': review_removed,
                      'grader_program_created_mtime': grader_created, 'evaluation_written_mtime': evaluation_written,
                      'run_finished': record['finished_at']}
        times = list(chronology.values())
        check(checks, 'solver_then_freeze_then_review_then_grading', all(a <= b + .1 for a, b in zip(times, times[1:])),
              chronology, 'raw_session_event_timestamps_plus_freeze_time_and_filesystem_grader_timestamps')
        evaluation = load(directory / 'evaluation.json')
        score = evaluation.get('overall_score')
        check(checks, 'valid_recorded_official_grade', evaluation == record.get('evaluation')
              and evaluation.get('status') == 'graded' and type(score) in (float, int)
              and math.isfinite(score) and 0 <= score <= 1
              and type(evaluation.get('elapsed_seconds')) in (float, int) and evaluation['elapsed_seconds'] > 0,
              {'score': score, 'status': evaluation.get('status'), 'seconds': evaluation.get('elapsed_seconds')})
        grader = grader_model_evidence(root, record, plan, grader_created)
        check(checks, 'actual_grader_model_and_effort_match_protocol', grader['valid'], grader,
              'gateway_request_identity_and_grading_window_join_to_raw_inference_session')
        events = list(read_jsonl(directory / 'memory_after_solver/events.jsonl'))
        ordered = [e for e in events if e.get('tool') != 'tool_error']
        names = [e.get('tool') for e in ordered]
        reason_first = bool(names and names[0] == 'reason' and 'DCI_search_task' in names)
        search_index = names.index('DCI_search_task') if 'DCI_search_task' in names else None
        earlier_retrieval = bool(search_index is not None and any(t in names[:search_index] for t in ('DCI_search_trajectory', 'distill')))
        first_search_time = timestamp(ordered[search_index]['timestamp']) if search_index is not None else None
        native_time = timestamp(solve['first_native_terminal_or_patch_timestamp']) if solve['first_native_terminal_or_patch_timestamp'] else None
        check(checks, 'observed_initial_reason_then_task_search', reason_first and not earlier_retrieval
              and solve['observed_domain_tool_calls'].get('reason', 0) > 0
              and solve['observed_domain_tool_calls'].get('DCI_search_task', 0) > 0,
              {'domain_sequence': names, 'task_search_before_first_native_terminal_or_patch':
               (first_search_time <= native_time if first_search_time is not None and native_time is not None else None),
               'scope': 'Recorded operation ordering. Native commands require semantic review to classify setup versus substantive solving.'},
              'memory_event_order_and_raw_solver_tool_calls')
        result['observations'] = {'initial_reason_then_task_search': reason_first and not earlier_retrieval,
                                  'online_write_decisions': sum(e.get('tool') == 'distill' and e.get('stage') == 'online'
                                      and e.get('decision') == 'write' for e in events),
                                  'online_entries': snapshots['memory_after_solver']['entry_count'],
                                  'final_entries': snapshots['memory']['entry_count'], 'final_decision': decision['decision'],
                                  'semantic_adoption_proven': False}
        result['status'] = 'valid' if all(c['passed'] for c in checks) else 'invalid'
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['status'] = 'invalid'
        result['error'] = {'type': type(error).__name__, 'message': str(error)}
    return result


def audit(root=ROOT):
    checks, errors = [], []
    protocol = root / VARIANT / 'prepared/formal/protocol.json'
    plan = load(protocol)
    split = load(root / 'experiment/manifests/split.json')
    tasks = plan['tasks']
    check(checks, 'fixed_36_build_24_test_schedule', len(tasks) == 24
          and len({t['run_id'] for t in tasks}) == 24 and len({t['task_id'] for t in tasks}) == 24
          and {t['task_id'] for t in tasks} == set(split['test_task_ids'])
          and len(set(split['build_task_ids'])) == 36
          and not set(split['build_task_ids']) & set(split['test_task_ids']),
          {'build': len(split['build_task_ids']), 'test': len(tasks), 'split_sha256': sha256(root / 'experiment/manifests/split.json')})
    for label, mapping in [('frozen_sources_unchanged', plan['source_hashes']),
                           ('retained_baseline_results_preserved', plan['historical_results'])]:
        mismatches = [p for p, expected in mapping.items() if not (root / p).is_file() or sha256(root / p) != expected]
        check(checks, label, not mismatches,
              {'checked_files': len(mapping), 'mismatches': mismatches}, 'frozen_sha256_allowlist')
    try:
        expected_pool, _ = _plan(root)  # Plans and hashes; does not write exports.
        pool = validate_pool(root / plan['pool_path'], expected_pool)
        check(checks, '432_scored_training_trajectories_original_bytes_and_build_only',
              pool['task_count'] == 36 and pool['trajectory_count'] == 432
              and set(pool['task_ids']) == set(split['build_task_ids'])
              and sha256(root / plan['pool_path'] / 'manifest.json') == plan['pool_manifest_sha256']
              and pool['pool_sha256'] == plan['pool_sha256'],
              {'pool_sha256': pool['pool_sha256'], 'task_cards': pool['task_count'],
               'trajectories': pool['trajectory_count'], 'assets': pool['asset_count'],
               'checks': 'task/model/run score and provenance joins; exact score filenames; all source/export body hashes; strict file allowlist'},
              'frozen_source_provenance_and_complete_export_hash_validation')
    except (OSError, ValueError, KeyError) as error:
        check(checks, '432_scored_training_trajectories_original_bytes_and_build_only', False, str(error))
    formal = root / RUNS
    present = {p.name for p in formal.iterdir() if p.is_dir()} if formal.exists() else set()
    unknown = present - {t['run_id'] for t in tasks}
    check(checks, 'no_extra_recorded_formal_run_directories', not unknown,
          {'recorded_directories': len(present), 'unknown': sorted(unknown),
           'scope': 'All recorded V5 formal runs; synthetic integration probes in archive are excluded.'})
    rows = [audit_run(root, row, plan, sha256(protocol)) for row in tasks]
    valid = sum(row['status'] == 'valid' for row in rows)
    invalid = [row['run_id'] for row in rows if row['status'] == 'invalid']
    memory_sources = []
    for row in tasks:
        native = root / RUNS / row['run_id'] / 'native_environment.json'
        if native.is_file():
            memory_sources.append(load(native)['solver']['memory_mount']['source'])
    check(checks, 'recorded_solver_memory_mount_sources_unique', len(memory_sources) == len(set(memory_sources)),
          {'observed_count': len(memory_sources), 'unique_count': len(set(memory_sources))}, 'runtime_docker_mount_observation')
    complete = valid == 24 and all(c['passed'] for c in checks)
    result = {
        'schema_version': 1, 'variant': 'V5', 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'audit_script_sha256': sha256(Path(__file__)),
        'status': 'complete' if complete else ('invalid' if invalid or any(not c['passed'] for c in checks) else 'partial'),
        'complete': complete, 'scheduled_tasks': 24, 'valid_tasks': valid,
        'incomplete_tasks': [r['run_id'] for r in rows if r['status'] == 'incomplete'], 'invalid_tasks': invalid,
        'protocol_sha256': sha256(protocol), 'checks': checks, 'tasks': rows,
        'limitations': [
            'This audit proves recorded structure, boundaries, model settings, metrics and explicit operations, not semantic adoption or benefit.',
            'One saved solver session per task proves no repeated solve within retained formal evidence; it cannot prove existence of no deleted external history.',
            'Reviewer mount source mapping uses frozen runner source and task-local artifacts; runtime isolation records preserve targets/modes but not source paths.',
            'Grading chronology uses persisted grader-script/evaluation file mtimes because an explicit grade-start timestamp was not recorded.',
        ],
    }
    atomic_json(root / 'experiment/reports/dci_memory/final_audit.json', result)
    return result


if __name__ == '__main__':
    result = audit()
    print(json.dumps({k: result[k] for k in ('status', 'complete', 'scheduled_tasks', 'valid_tasks', 'incomplete_tasks', 'invalid_tasks')}, indent=2))
