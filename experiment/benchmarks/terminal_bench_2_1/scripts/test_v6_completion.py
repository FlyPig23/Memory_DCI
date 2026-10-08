"""V6 score validity is independent of post-submission memory-review success."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import run


class V6CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'trial'
        (self.directory / 'agent').mkdir(parents=True)
        self.task = {'task_id': 'heldout-task', 'official_agent_timeout_seconds': 937,
                     'official_verifier_timeout_seconds': 143}
        self.settings = {'model': 'gpt-5.6-sol', 'reasoning_effort': 'medium', 'verifier_policy': 'unlimited'}
        body_hash = hashlib.sha256(b'# Frozen training failure lesson\n').hexdigest()
        self.seed = {'entry_count': 1, 'manifest_sha256': 'a' * 64,
                     'files_sha256': {'entries/train-lesson.md': body_hash,
                                      '.entry_metadata.json': 'b' * 64,
                                      'MEMORY.md': 'c' * 64, 'memory_summary.md': 'd' * 64}}
        self.result = {'task_id': self.task['task_id'], 'harbor': {'verifier_result': {'rewards': {'reward': 1.0}}},
                       'freeze': {'taken_before_hidden_grading': True, 'solver_stopped': True, 'snapshot_valid': True},
                       'review': {'valid': True, 'received_hidden_grade': False, 'evidence_unchanged': True,
                                  'memory_audit': {'final_decision': {'agent_stage': 'review',
                                                                     'task_id': self.task['task_id'],
                                                                     'decision': 'no_update'}}}}
        self.initial = {'valid': True, 'entry_count': 1, 'inherited_entries': 1, 'event_count': 0,
                        'scope': 'single_test_task', 'task_id': self.task['task_id'],
                        'seed_manifest_sha256': self.seed['manifest_sha256'],
                        'files_sha256': {**self.seed['files_sha256'], 'identity.json': 'e' * 64}}
        self.contract = {'condition': 'v6', 'task_id': self.task['task_id'],
                         'model': self.settings['model'], 'reasoning_effort': self.settings['reasoning_effort'],
                         'solver_timeout_seconds': 937, 'official_verifier_timeout_seconds': 143,
                         'verifier_timeout_seconds': None, 'verifier_timeout_policy': 'unlimited',
                         'cross_task_memory': False, 'initial_skills': 0,
                         'seed_manifest_sha256': self.seed['manifest_sha256'], 'initial_memory_entries': 1}
        self.model = {'model_effort_verified': True, 'models': ['gpt-5.6-sol'], 'efforts': ['medium']}
        self.health = {'blocked': False, 'phases': {'agent': {'actual_inference': True, 'blocked': False},
                                                   'review': {'actual_inference': True, 'blocked': False}}}
        self.flush()
        self.write('agent/usage.json', {'input_tokens': 123, 'output_tokens': 45})
        self.health_patch = patch.object(run.health_guard, 'inspect_trial', side_effect=lambda _: copy.deepcopy(self.health))
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)

    def write(self, name, content):
        (self.directory / name).write_text(json.dumps(content, allow_nan=True))

    def flush(self):
        self.write('v6_result.json', self.result)
        self.write('memory_initial.json', self.initial)
        self.write('runtime_contract.json', self.contract)
        self.write('agent/model_audit.json', self.model)

    def summarize(self, *, seed=True):
        return run.summary(self.directory, 'v6', task=self.task, settings=self.settings,
                           memory_seed=self.seed if seed else None)

    def assert_invalid(self, reason):
        result = self.summarize()
        self.assertFalse(result['valid'], result)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn(reason, result['reasons'])
        return result

    def test_genuine_zero_reward_is_valid_and_preserved(self):
        self.result['harbor']['verifier_result']['rewards']['reward'] = 0.0
        self.flush()
        row = self.summarize()
        self.assertTrue(row['valid'], row)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['reward'], 0.0)
        self.assertEqual(row['reasons'], [])
        self.assertEqual(row['workflow_deviations'], [])
        self.assertEqual((row['input_tokens'], row['output_tokens']), (123, 45))

    def test_initial_seed_manifest_hash_must_match(self):
        self.initial['seed_manifest_sha256'] = 'f' * 64
        self.flush()
        self.assert_invalid('frozen_seed_mismatch')

    def test_initial_count_and_inherited_count_must_match(self):
        for field in ('entry_count', 'inherited_entries'):
            with self.subTest(field=field):
                previous = self.initial[field]
                self.initial[field] = 0
                self.flush()
                self.assert_invalid('initial_memory_audit_failed' if field == 'entry_count' else 'frozen_seed_mismatch')
                self.initial[field] = previous

    def test_each_inherited_body_hash_and_entry_set_must_match(self):
        original = copy.deepcopy(self.initial['files_sha256'])
        variations = [{**original, 'entries/train-lesson.md': '0' * 64},
                      {key: value for key, value in original.items() if not key.startswith('entries/')},
                      {**original, 'entries/test-answer.md': '9' * 64}]
        for files in variations:
            with self.subTest(files=files):
                self.initial['files_sha256'] = files
                self.flush()
                self.assert_invalid('frozen_seed_mismatch')

    def test_empty_or_missing_seed_protocol_is_invalid(self):
        self.assertFalse(self.summarize(seed=False)['valid'])
        self.seed['entry_count'] = 0
        self.assert_invalid('frozen_seed_mismatch')

    def test_task_identity_scope_and_clean_event_ledger_are_checked(self):
        for field, value in (('task_id', 'other-heldout'), ('scope', 'shared'), ('event_count', 1), ('valid', False)):
            with self.subTest(field=field):
                previous = self.initial[field]
                self.initial[field] = value
                self.flush()
                self.assert_invalid('initial_memory_audit_failed')
                self.initial[field] = previous

    def test_seed_contract_changes_are_invalid(self):
        for field, value in (('condition', 'v5'), ('seed_manifest_sha256', '0' * 64), ('initial_memory_entries', 2)):
            with self.subTest(field=field):
                previous = self.contract[field]
                self.contract[field] = value
                self.flush()
                self.assert_invalid('seed_contract_mismatch')
                self.contract[field] = previous

    def test_official_task_timeout_model_and_isolation_contract_are_checked(self):
        variations = [('solver_timeout_seconds', 28800), ('verifier_timeout_seconds', 143),
                      ('verifier_timeout_policy', 'official'), ('official_verifier_timeout_seconds', 28800),
                      ('model', 'gpt-5.6-luna'), ('reasoning_effort', 'xhigh'),
                      ('cross_task_memory', True), ('initial_skills', 1), ('task_id', 'other-heldout')]
        for field, value in variations:
            with self.subTest(field=field):
                previous = self.contract[field]
                self.contract[field] = value
                self.flush()
                self.assert_invalid('runtime_contract_mismatch')
                self.contract[field] = previous

    def test_missing_or_nonfinite_official_reward_stays_unscored(self):
        for reward in (None, True, '1', float('nan'), float('inf')):
            with self.subTest(reward=reward):
                self.result['harbor']['verifier_result']['rewards']['reward'] = reward
                self.flush()
                row = self.assert_invalid('missing_official_reward')
                self.assertIsNone(row['reward'])
        self.result['harbor']['verifier_result']['rewards'] = {}
        self.flush()
        self.assertIsNone(self.assert_invalid('missing_official_reward')['reward'])

    def test_review_timeout_is_deviation_and_preserves_first_scored_answer(self):
        self.result['review'].update(valid=False, timed_out=True, memory_audit={})
        self.health['blocked'] = True
        self.health['phases']['review'].update(actual_inference=False, blocked=True)
        self.flush()
        original_bytes = (self.directory / 'v6_result.json').read_bytes()
        original_paths = sorted(str(path.relative_to(self.directory)) for path in self.directory.rglob('*'))
        row = self.summarize()
        self.assertTrue(row['valid'], row)
        self.assertEqual(row['reward'], 1.0)
        self.assertEqual(row['workflow_deviations'], ['post_submission_review_incomplete'])
        self.assertEqual(row['reasons'], [])
        self.assertEqual((self.directory / 'v6_result.json').read_bytes(), original_bytes)
        self.assertEqual(sorted(str(path.relative_to(self.directory)) for path in self.directory.rglob('*')), original_paths)
        self.write('completion.json', row)
        self.assertEqual(run.saved_completion(self.directory, 'v6', task=self.task, settings=self.settings,
                                             memory_seed=self.seed), row)

    def test_hidden_grade_leak_is_invalid_even_after_submission(self):
        self.result['review'].update(valid=False, received_hidden_grade=True)
        self.flush()
        self.assert_invalid('review_audit_failed')

    def test_changed_frozen_review_evidence_is_invalid(self):
        self.result['review'].update(valid=False, evidence_unchanged=False)
        self.flush()
        self.assert_invalid('review_audit_failed')

    def test_missing_pre_grader_freeze_invalidates_score(self):
        for frozen in ({}, {'taken_before_hidden_grading': False}):
            with self.subTest(frozen=frozen):
                self.result['freeze'] = frozen
                self.flush()
                row = self.assert_invalid('missing_pre_grader_boundary')
                self.assertEqual(row['reward'], 1.0)

    def test_solver_infrastructure_failure_is_not_excused_as_review_deviation(self):
        self.result['review'].update(valid=False, timed_out=True, memory_audit={})
        self.health['blocked'] = True
        self.health['phases']['agent'].update(actual_inference=False, blocked=True)
        self.flush()
        row = self.assert_invalid('infrastructure_failure')
        self.assertIn('no_solver_inference_evidence', row['reasons'])

    def test_actual_model_and_effort_must_match_solver(self):
        self.model['models'] = ['gpt-5.6-luna']
        self.model['efforts'] = ['xhigh']
        self.flush()
        self.assert_invalid('model_audit_failed')

    def test_changed_saved_result_cannot_be_silently_reused_or_retried(self):
        self.write('completion.json', self.summarize())
        self.result['harbor']['verifier_result']['rewards']['reward'] = 0
        self.flush()
        with self.assertRaisesRegex(ValueError, 'changed'):
            run.saved_completion(self.directory, 'v6', task=self.task, settings=self.settings, memory_seed=self.seed)
        (self.directory / 'completion.json').unlink()
        with self.assertRaisesRegex(ValueError, 'cannot be rerun'):
            run.saved_completion(self.directory, 'v6', task=self.task, settings=self.settings, memory_seed=self.seed)


if __name__ == '__main__':
    unittest.main()
