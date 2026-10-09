"""Trial result validation and image resolution reused by V8; retired runner entry points removed."""
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






def pool_digest(files):
    import hashlib
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def summary(directory, arm, *, task=None, settings=None, memory_seed=None):
    directory = Path(directory)
    name = {'baseline': 'baseline_result.json', 'v5': 'v5_result.json', 'v6': 'v6_result.json',
            'v7': 'v7_result.json'}[arm]
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
    if health['blocked'] and (arm not in ('v6', 'v7') or health['phases']['agent']['blocked']):
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
        if arm == 'v7' and memory_seed:
            expected_count = memory_seed.get('entry_count')
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
        if arm == 'v7':
            # This task's own seed (or no seed) must be the one cloned before solving.
            expected_files = (memory_seed or {}).get('files_sha256', {})
            expected_entries = {k: v for k, v in expected_files.items() if k.startswith('entries/')}
            observed_entries = {k: v for k, v in initial.get('files_sha256', {}).items() if k.startswith('entries/')}
            if memory_seed and (initial.get('inherited_entries') != expected_count
                    or initial.get('seed_manifest_sha256') != memory_seed.get('manifest_sha256')
                    or observed_entries != expected_entries):
                reasons.append('task_seed_mismatch')
            if not memory_seed and (initial.get('seed_manifest_sha256') or initial.get('inherited_entries') not in (None, 0)
                                    or observed_entries):
                reasons.append('task_seed_mismatch')
        decision = (review.get('memory_audit') or {}).get('final_decision') or {}
        if not (review.get('valid') and review.get('received_hidden_grade') is False
                and review.get('evidence_unchanged') and decision.get('agent_stage') == 'review'
                and decision.get('task_id') == result.get('task_id')
                and decision.get('decision') in ('write', 'no_update')):
            # Post-submission review cannot change the answer. Preserve and
            # report a review failure independently of the official reward.
            if (arm in ('v6', 'v7') and review.get('received_hidden_grade') is False
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
    if arm == 'v7' and (contract.get('condition') != 'v7' or contract.get('pool_scope') != 'same_task'
            or contract.get('seed_manifest_sha256') != (memory_seed or {}).get('manifest_sha256')
            or contract.get('initial_memory_entries') != ((memory_seed or {}).get('entry_count') or 0)
            or (task is not None and (contract.get('pool_manifest_sha256') != task['pool']['manifest_sha256']
                                      or contract.get('pool_files_sha256_digest') != pool_digest(task['pool']['files_sha256'])
                                      or contract.get('pool_trajectory_count') != task['pool']['trajectory_count']))):
        reasons.append('task_corpus_contract_mismatch')
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
    if arm in ('v6', 'v7'):
        row['workflow_deviations'] = deviations
    return row












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
